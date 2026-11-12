# ADR-002: Database and schema — SQLite (WAL) via SQLAlchemy, two tables

Status: Accepted · Requirements: R6, R10, R13, P3

## Context
We need a relational database that persists job and item status (R6) and runs with zero external services (P3). The same database is also the work queue (ADR-004).

## Options
1. **SQLite file, raw `sqlite3`** (simplest). No dependency, but hand-written SQL everywhere and manual row mapping.
2. **SQLite via SQLAlchemy 2.x**, WAL mode. Table definitions and CRUD through SQLAlchemy Core, `text()` for the claim statements. The URL can switch dialects later.
3. **PostgreSQL** (Docker). Real row locks and `SKIP LOCKED`, but it breaks "pip install + one command" (P3).

## Criteria
Correctness under concurrent polling and writing, ease to run, explainability, path to scale.

## Decision
**Option 2.** WAL means "readers do not block writers and a writer does not block readers" ([sqlite.org/wal](https://www.sqlite.org/wal.html)). SQLite's single-writer rule makes the claim atomic without row locks. PostgreSQL is not shipped; the "At 100× scale" section lists the change.

### Two engines: write and read
Every connection sets `journal_mode=WAL`, `foreign_keys=ON` (SQLite's default is OFF), `busy_timeout=5000` and `isolation_level=None` (disables pysqlite's legacy implicit transactions) through a SQLAlchemy `connect` listener, and a `begin` listener emits the transaction statement. There are two engines on the same file, differing only in that `begin` statement:

| Engine | `begin` emits | Used by | Why |
|---|---|---|---|
| `write` | `BEGIN IMMEDIATE` | job creation, claim, completion, retry, schema creation | Takes the write lock **up front**, so a read-then-update (the claim) is atomic across threads **and processes**. |
| `read` | `BEGIN` (deferred) | every `GET`: job status, items, download lookup, archive | A reader never takes the write lock, so polling never blocks workers (WAL). |

With a single `BEGIN IMMEDIATE` engine, an open read transaction made a concurrent write fail with `database is locked` after the full busy timeout; with a deferred read engine the same write completes at once (`tests/test_db.py::test_open_read_does_not_block_claim`).

Rule: **no transaction is held while rendering or doing file I/O.** The archive endpoint reads the item rows, closes the session, and only then streams files.

### Schema creation
`metadata.create_all()` runs at startup inside a `write` transaction. With several processes starting on an empty database, they serialize; the later ones see the tables and create nothing. There is no migration tool yet (the schema has no history); it is the first thing to add on a second schema version.

### Timestamps
All timestamps are stored as **INTEGER epoch milliseconds (UTC)** and rendered as ISO-8601 in the API. SQLite has no datetime type, and integers compare correctly in raw SQL (`lease_expires_at < :now`) and in the ORM.

### Schema
Defined in `certgen/tables.py`; `CHECK` constraints make invalid row shapes impossible rather than merely untested.
```
jobs
  id                TEXT PK            -- uuid4 hex, server-generated; no status column (derived)
  title, issuer     TEXT NOT NULL      -- certificate information
  issued_on         TEXT NOT NULL      -- ISO date (a calendar date, not an instant)
  print_email       INTEGER NOT NULL   -- snapshot of CERTGEN_PRINT_EMAIL_ON_CERTIFICATE at creation (ADR-007)
  idempotency_key   TEXT UNIQUE NULL   -- ADR-006
  request_hash      TEXT NULL          -- sha256 of the canonical body (ADR-006)
  request_json      TEXT NOT NULL      -- the request body (canonical JSON)
  created_at        INTEGER NOT NULL   -- epoch ms

items                                   -- one row per submitted recipient, valid or not
  id                INTEGER PK         -- claim order is FIFO by id
  job_id            TEXT NOT NULL FK -> jobs.id ON DELETE CASCADE
  row_index         INTEGER NOT NULL   -- 0-based position in the request (R2/R10)
  name, email       TEXT NULL          -- NULL when missing or not a string (rejected rows only)
  status            TEXT NOT NULL      -- rejected|pending|processing|succeeded|failed
  attempts          INTEGER NOT NULL DEFAULT 0
  errors            TEXT NULL          -- JSON list [{field, code, message, ...}]: all row errors (ADR-005)
                                       --   or one generation error (ADR-004); NULL when none
  certificate_id    TEXT UNIQUE NULL   -- assigned at insert for valid rows (ADR-013)
  lease_owner       TEXT NULL          -- worker id, for logs only
  lease_token       TEXT NULL          -- random per claim; the fencing token (ADR-004)
  lease_expires_at  INTEGER NULL       -- epoch ms
  available_at      INTEGER NULL       -- epoch ms; retry delay: not claimable before this
  completed_at      INTEGER NULL       -- epoch ms
  input_json        TEXT NOT NULL      -- the row as submitted
  UNIQUE(job_id, row_index)
  INDEX(status, id)                    -- claim query
  INDEX(status, lease_expires_at)      -- expired-lease lookup
  INDEX(job_id, status, row_index)     -- counts and keyset pagination
```
Constraints tie the columns together: a `rejected` row has no `certificate_id` and zero attempts; a `processing` row has a lease token and expiry; a `succeeded` or `failed` row has `completed_at`; every non-rejected row has both `name` and `email`.

`name`/`email` are nullable because a rejected row may lack them or carry a non-string (`"name": 42`). Storing `NULL` instead of failing the insert keeps a validation case from becoming a 500.

There is no `updated_at`: the job row is immutable after creation, and a job's "finished at" is derived from its items' `completed_at`.

### Job status: derived from item counts on read, never stored
A stored status could drift from its items. Let `valid = total − rejected`, `done = succeeded + failed`:

| Condition (checked in order) | Status |
|---|---|
| `valid = 0` (unreachable: all-invalid is a 422; kept as a guard) | `completed_with_errors` |
| `done = valid` and `failed = 0` and `rejected = 0` | `completed` |
| `done = valid` and `succeeded = 0` | `failed` |
| `done = valid` | `completed_with_errors` (some failed **or rejected**) |
| every valid item is `pending` with `attempts = 0` | `queued` |
| otherwise | `processing` |

`progress = done / valid`, rounded to 4 places and capped at 0.9999 while any valid item is unfinished, so `1.0` always means finished. Because `queued` requires `attempts = 0`, a job never regresses from `processing` to `queued` while an item waits out a retry delay. A job whose worker died stays `processing` until some worker reclaims the expired lease; that is true (work remains), and `/healthz` exposes whether the embedded workers are alive. Each row of this table has a test (`tests/test_job_status.py`).

## Consequences
- Single writer: throughput is capped at one write transaction at a time. Fine for one host, since write transactions last about a millisecond (renders happen outside them).
- Two engines to explain. The rule is one sentence: "`GET` uses the read engine; anything that writes uses the write engine."
- The database must be on a local disk (WAL does not work over a network filesystem). Documented in the README.

## At 100× scale
Switch to PostgreSQL and add a migration tool. The claim sub-select gets `FOR UPDATE SKIP LOCKED` (ADR-004). Maintain per-job counters in the same transaction as item transitions if `COUNT` becomes hot. Partition or archive completed items.

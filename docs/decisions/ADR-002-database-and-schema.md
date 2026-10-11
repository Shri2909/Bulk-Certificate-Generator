# ADR-002: Database and schema — SQLite (WAL) via SQLAlchemy 2.0, two tables

Status: Accepted · Requirements: R6, R10, R13, P3 · Evidence: RESEARCH §3, §5

## Context
We need a relational DB that persists job/item status (R6) and runs with zero external services (P3). The same DB is also the work queue (ADR-004).

## Options
1. **SQLite file, raw `sqlite3`** (simplest). No dependency, but hand-written SQL everywhere and manual row mapping.
2. **SQLite via SQLAlchemy 2.0**, WAL mode. ORM for CRUD, `text()` for the one claim statement. The URL can switch dialects later.
3. **PostgreSQL** (Docker). Real row locks and `SKIP LOCKED`, but it breaks "pip install + one command", and you said Postgres ships only if tested.

## Criteria
Correctness under concurrent poll+write, ease to run, explainability, path to scale.

## Decision
**Option 2.** WAL means "readers do not block writers and a writer does not block readers" ([sqlite.org/wal](https://www.sqlite.org/wal.html)). SQLite's single-writer rule makes our claim atomic without row locks. Postgres is **not shipped in v1**; ADR-004 documents the exact change.

### Two engines: write and read (Phase 4 audit, finding #1)
Every connection sets `journal_mode=WAL`, `foreign_keys=ON` (default is OFF), `busy_timeout=5000` and `isolation_level=None` (disables pysqlite's legacy implicit transactions) through a SQLAlchemy `connect` listener. The SQLAlchemy SQLite docs show this listener paired with a `begin` listener that emits a plain `BEGIN`. **Our modification** is to have two engines on the same file, differing only in that `begin` listener:

| Engine | `begin` emits | Used by | Why |
|---|---|---|---|
| `write_engine` | `BEGIN IMMEDIATE` | job creation, claim, completion, retry, schema creation | Takes the write lock **up front**, so a read-then-update (the claim) is atomic across threads **and processes**. |
| `read_engine` | `BEGIN` (deferred) | every `GET`: job status, items, download lookup, archive, verify | A reader never takes the write lock, so polling never blocks workers (WAL). |

[measured] RESEARCH §3: with one IMMEDIATE engine, an open read transaction made a concurrent write fail with `database is locked` after the full busy timeout. With a deferred read engine, the same write completed in 1 ms.

Rule: **no transaction is held while doing file I/O or rendering.** The archive endpoint reads the item rows, closes the session, and only then streams files.

### Schema creation
`metadata.create_all()` runs at startup **inside a `write_engine` transaction** (`BEGIN IMMEDIATE`). With `uvicorn --workers N` on an empty DB, the processes serialize; the second one's `checkfirst` sees the tables and creates nothing. No Alembic in v1 (the schema has no history yet); it's the first thing to add on a second schema version.

### Timestamps
All timestamps are stored as **INTEGER epoch milliseconds (UTC)** and rendered as ISO-8601 in the API. SQLite has no datetime type: comparing `lease_expires_at < :now` on strings depends on formatting (microseconds, `+00:00`), and pysqlite's default datetime adapter is deprecated since Python 3.12. Integers compare correctly in raw SQL and in the ORM (audit #22).

### Schema
```
jobs
  id                TEXT PK            -- uuid4 hex, server-generated; no status column (derived)
  title             TEXT NOT NULL      -- certificate info: course/event name
  issuer            TEXT NOT NULL
  issued_on         TEXT NOT NULL      -- ISO date (a calendar date, not an instant)
  print_email       BOOLEAN NOT NULL   -- snapshot of PRINT_EMAIL_ON_CERTIFICATE at creation (ADR-007)
  verify_base_url   TEXT NULL          -- snapshot of PUBLIC_BASE_URL at creation; NULL = no QR (ADR-013)
  idempotency_key   TEXT UNIQUE NULL   -- ADR-006
  request_hash      TEXT NULL          -- sha256 of canonical body (ADR-006)
  created_at        INTEGER NOT NULL   -- epoch ms

items                                   -- one row per submitted recipient, valid or not
  id                INTEGER PK
  job_id            TEXT NOT NULL FK -> jobs.id ON DELETE CASCADE
  row_index         INTEGER NOT NULL    -- 0-based position in the request (R2/R10)
  name              TEXT NULL           -- NFC-normalized string; NULL if missing or not a string (rejected rows)
  email             TEXT NULL           -- trimmed string; NULL if missing or not a string (rejected rows)
  status            TEXT NOT NULL       -- rejected|pending|processing|succeeded|failed
  attempts          INTEGER NOT NULL DEFAULT 0
  errors            TEXT NULL           -- JSON list [{field, code, message, ...}]; all row errors (ADR-005)
                                        --   or one generation error (ADR-004); NULL when none
  certificate_id    TEXT UNIQUE NULL    -- assigned at insert for valid rows (ADR-013)
  lease_owner       TEXT NULL           -- worker id (uuid4 per worker thread/process) (ADR-004)
  lease_token       TEXT NULL           -- random per claim; the fencing token (ADR-004)
  lease_expires_at  INTEGER NULL        -- epoch ms
  available_at      INTEGER NULL        -- epoch ms; retry delay: not claimable before this
  completed_at      INTEGER NULL        -- epoch ms
  UNIQUE(job_id, row_index)
  INDEX(status, available_at)           -- claim query
  INDEX(job_id, status)                 -- counts
  INDEX(job_id, row_index)              -- keyset pagination (covered by the UNIQUE above)
```
`name`/`email` are nullable because a rejected row may lack them, or carry a non-string (`"name": 42`). Storing `NULL` there instead of failing the insert keeps a validation case from becoming a 500 (audit #4). Valid rows always have both (enforced in code, tested).

There is no `updated_at`: the job row is immutable after creation, and its "last change" is `MAX(items.completed_at)` (audit #28).

### Job status: derived from item counts on read, never stored
A stored status could drift from its items (two writers). Let `valid = total − rejected`, `done = succeeded + failed`:

| Condition (checked in order) | Status |
|---|---|
| `valid = 0` (unreachable: all-invalid is a 422, kept as a guard) | `completed_with_errors` |
| `done = valid` and `failed = 0` and `rejected = 0` | `completed` |
| `done = valid` and `succeeded = 0` | `failed` |
| `done = valid` | `completed_with_errors` (some failed **or rejected**) |
| every valid item is `pending` with `attempts = 0` | `queued` |
| otherwise | `processing` |

`progress = done / valid` (1.0 when `valid = 0`). Because `queued` requires `attempts = 0`, a job never regresses from `processing` to `queued` while an item waits out a retry delay. A job whose worker died stays `processing` until some worker reclaims the expired lease. That's true (work remains), and `/healthz` exposes whether the embedded workers are alive. Every row of this table has a test (`test_job_status.py`).

## Consequences
- Single writer: throughput is capped at one write transaction at a time. Fine for one host, since write transactions last about a millisecond (renders happen outside them).
- Two engines to explain. The rule is one sentence: "`GET` uses the read engine; anything that writes uses the write engine."
- The DB must be on local disk ("WAL does not work over a network filesystem"). Documented in README.

## At 100× scale
Switch `DATABASE_URL` to Postgres. Add Alembic. The claim sub-select gets `FOR UPDATE SKIP LOCKED` (ADR-004). Maintain per-job counters in the same transaction as item transitions if `COUNT` becomes hot. Partition or archive completed items.

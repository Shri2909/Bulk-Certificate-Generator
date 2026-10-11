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
**Option 2.** WAL means "readers do not block writers and a writer does not block readers" ([sqlite.org/wal](https://www.sqlite.org/wal.html)), so clients polling status never stall the worker. SQLite's single-writer rule makes our claim `UPDATE` atomic without row locks. Every connection sets `journal_mode=WAL`, `foreign_keys=ON` (default is OFF), `busy_timeout=5000` through a SQLAlchemy `connect` listener. The listener also disables pysqlite's legacy implicit transactions (`isolation_level=None`), and a `begin` listener emits `BEGIN IMMEDIATE`. This is the pattern in the SQLAlchemy SQLite docs. Every write transaction then takes the write lock up front, which makes the read-then-update claim atomic across threads **and processes** (ADR-004; verified in RESEARCH §6 [measured]). Read-only requests use the same engine; in WAL mode a reader is never blocked, and our transactions last a few ms. Postgres is **not shipped in v1**. ADR-004 documents the exact change.

Tables are created with `metadata.create_all()` at startup. There is no Alembic in v1 because the schema has no history yet. That's a stated limitation, and it's the first thing to add on a second schema version.

### Schema
```
jobs
  id               TEXT PK            -- uuid4 hex, server-generated
                                      -- (no status column: derived from items, see below)
  title            TEXT NOT NULL      -- certificate info: course/event name
  issuer           TEXT NOT NULL
  issued_on        DATE NOT NULL
  idempotency_key  TEXT UNIQUE NULL   -- ADR-006
  request_hash     TEXT NULL          -- sha256 of canonical body (ADR-006)
  created_at, updated_at  TIMESTAMP (UTC)

items                                  -- one row per submitted recipient, valid or not
  id               INTEGER PK
  job_id           TEXT FK -> jobs.id ON DELETE CASCADE
  row_index        INTEGER NOT NULL    -- 0-based position in the request (R2/R10)
  name             TEXT NOT NULL       -- NFC-normalized (raw value if rejected)
  email            TEXT NOT NULL
  status           TEXT NOT NULL       -- rejected|pending|processing|succeeded|failed
  attempts         INTEGER NOT NULL DEFAULT 0
  error_code       TEXT NULL
  error_message    TEXT NULL
  certificate_id   TEXT UNIQUE NULL    -- assigned at insert for valid rows (ADR-013)
  lease_owner      TEXT NULL           -- worker id holding the claim (ADR-004)
  lease_expires_at TIMESTAMP NULL
  available_at     TIMESTAMP NULL      -- retry backoff: not claimable before this
  completed_at     TIMESTAMP NULL
  UNIQUE(job_id, row_index)
  INDEX(status, available_at)          -- claim query
  INDEX(job_id, status)                -- counts + filtered listing
```
**Job status is derived from item counts on read**, not stored. A stored status could drift from its items, a classic two-writers bug. Counting with `GROUP BY status` over the `(job_id, status)` index is cheap at this scale. Derivation: any `processing`, or a mix of `pending` and finished items → `processing`; all non-rejected items still `pending` → `queued`; otherwise `completed` if `failed = 0`, else `completed_with_errors`.

## Consequences
- Single writer: throughput is capped at one write transaction at a time. Fine for one host, since transactions last only a few ms (renders happen outside transactions).
- The DB must be on local disk ("WAL does not work over a network filesystem"). Documented in README.

## At 100× scale
Switch `DATABASE_URL` to Postgres. Add Alembic. The claim sub-select gets `FOR UPDATE SKIP LOCKED` (ADR-004). Maintain per-job counters in the same transaction as item transitions if `COUNT` becomes hot. Partition or archive completed items.

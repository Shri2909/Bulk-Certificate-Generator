# ADR-004: Worker model and crash recovery — single-item leased claims, fencing token, attempt cap

Status: Accepted · Requirements: R3, R6, R9, R11

## Context
Workers can crash mid-item (OOM, deploy, SIGKILL). We need:
- (a) no item stuck in `processing` forever;
- (b) no item processed into two different certificates;
- (c) one bad item never blocks others (R9);
- (d) correctness regardless of how many workers run (embedded threads, several API processes, standalone `python -m certgen.worker`, in any combination).

## Item state machine
```
            (validation fails)
  request ───────────────────────► rejected            (terminal, never attempted)
     │
     ▼
  pending ──claim──► processing ──ok──► succeeded      (terminal)
     ▲                   │
     │  error & attempts < MAX_ATTEMPTS ──(available_at = now + RETRY_DELAY)
     └───────────────────┤
                         │ error & attempts >= MAX_ATTEMPTS ──► failed (GENERATION_FAILED)
                         │
                         │ lease expired (crash; the claim already counted an attempt):
                         │   attempts < MAX_ATTEMPTS  ──► processing again, new claim by another worker
                         └   attempts >= MAX_ATTEMPTS ──► failed (WORKER_LOST)

  failed ──POST /retry──► pending   (attempts reset to 0)
```

## Options for crash recovery
1. **No recovery** (simplest). Items stay in `processing` after a crash forever, so the job never finishes. Unacceptable.
2. **Startup sweep**: on boot, reset all `processing` items to `pending`. Wrong with more than one worker: worker B booting steals live work from worker A.
3. **Lease (timeout) + fencing token** (chosen). Details below.
4. **Lease + heartbeat**. Workers extend leases periodically, allowing short leases for long tasks. **Rejected:** one item takes milliseconds against a 30 s lease (we claim one item at a time), so a heartbeat adds a thread and a failure mode while buying nothing.

**When a heartbeat becomes necessary:** when one claimed unit of work can legitimately run longer than the lease, so that a fixed lease forces a bad trade-off. A long lease makes crash detection slow; a short lease makes healthy workers lose claims. Concretely: email delivery to slow SMTP servers, or image-heavy multi-page templates. Rule of thumb: add one once p99 item duration exceeds about 10% of `CERTGEN_LEASE_SECONDS`.

## Criteria
Correctness under crash, slow workers and any worker count; explainability; deterministic testability.

## Decision
**Option 3**, with these parameters, all configurable by environment so tests can shrink them:

| Setting | Default | Note |
|---|---|---|
| `CERTGEN_LEASE_SECONDS` | 30 | far above a millisecond-scale render |
| `CERTGEN_MAX_ATTEMPTS` | 3 | counted at claim |
| `CERTGEN_RETRY_DELAY_SECONDS` | 2 | fixed delay, no jitter: jitter protects a shared downstream from synchronized retries, and there is none here |
| `CERTGEN_POLL_INTERVAL_SECONDS` | 0.5 | idle sleep when nothing is claimable |

### Worker identity
`worker_id = uuid4().hex`, generated **per worker thread or process** at start (logged). It is for observability only: **correctness never depends on it.**

### Claim: one item, one atomic write transaction
In one `BEGIN IMMEDIATE` transaction (ADR-002) on the write engine, `Worker.claim_next` runs three statements:
```sql
-- 1. Retire items whose lease expired on their last allowed attempt (crash-loop guard).
UPDATE items SET status='failed', errors=:worker_lost_json, lease_owner=NULL, lease_token=NULL,
                 lease_expires_at=NULL, completed_at=:now
WHERE status='processing' AND lease_expires_at < :now AND attempts >= :max_attempts;

-- 2. Pick one item: an expired lease first, else the oldest pending item that is due.
SELECT id FROM items WHERE status='processing' AND lease_expires_at < :now AND attempts < :max_attempts
ORDER BY lease_expires_at LIMIT 1;
SELECT id FROM items WHERE status='pending' AND (available_at IS NULL OR available_at <= :now)
ORDER BY id LIMIT 1;

-- 3. Claim it.
UPDATE items SET status='processing', lease_owner=:worker_id, lease_token=:token,
                 lease_expires_at=:now + :lease_ms, attempts=attempts+1
WHERE id = :id RETURNING id, attempts;
```
- `:token = secrets.token_hex(16)`, fresh **per claim**.
- **Why it is atomic:** `BEGIN IMMEDIATE` takes SQLite's single write lock before the first statement runs, so no other connection in any thread or process can claim between our read and our update. A competing claimer waits (up to the busy timeout) and then sees the row already `processing` with a live lease. The current time is read after the lock is held.
- **One item per claim.** A claim transaction costs about a millisecond; batching would make the lease cover N items, so per-item margins become per-batch margins and a crash charges N items an attempt. One item keeps the lease arithmetic honest.
- Attempts increment **at claim**, so a crash on an item consumes an attempt, and statement 1 ends it as `failed`/`WORKER_LOST` once it has used `CERTGEN_MAX_ATTEMPTS`.

### Process, outside any transaction
Load the item and job snapshot (read engine), render (a pure function of that snapshot, ADR-007), write the file (ADR-008: unique temp name, `fsync`, `os.replace`). No database transaction is open during render or file I/O.

Every renderer exception is treated as retryable. Inputs that are deterministically unrenderable (missing glyphs, RTL) are rejected at validation (ADR-005/007), so what reaches the renderer and fails is presumed transient (disk full, I/O error). If it fails `CERTGEN_MAX_ATTEMPTS` times, the item ends `failed`/`GENERATION_FAILED`. The stored message is only the exception class plus a fixed sentence; details go to the log.

### Fenced transitions: the token, not the worker id
Every transition out of `processing` made by a worker is conditional on the claim token:
```sql
UPDATE items SET status='succeeded', lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                 errors=NULL, available_at=NULL, completed_at=:now
WHERE id=:id AND lease_token=:token AND status='processing';
```
(The retry-later and fail transitions use the same `WHERE`.) If `rowcount == 0`, the lease was lost: another claim replaced the token, the item was retired, or `/retry` reset the row. The worker logs `lease_lost` and does nothing else. Because the token is unique per claim, this holds even if the same thread reclaims the same item, if `/retry` resets attempts, or if a process id is reused.

### Output idempotency: what is and is not guaranteed
- Path `{job_id}/{certificate_id}.pdf`. `certificate_id` is fixed at item creation (ADR-013), so every execution of an item targets the same path.
- Render inputs come **only from the database rows** (item and job snapshot, including `print_email`), never from the worker's environment. The canvas is created with `invariant=1`, so the same inputs give byte-identical PDFs across processes.
- Therefore a stale worker's late `os.replace` (which cannot be fenced, since the file system does not take our token) replaces the file with **equal bytes**. The guarantee is: *one path per certificate, atomic replace, equal content for equal inputs*. That is at-least-once execution with an idempotent effect. We do not claim exactly-once execution.

### Embedded workers and shutdown
Embedded threads are supervised by a monitor thread. It restarts a dead worker thread (backoff 0.25 / 0.5 / 1 s, at most 3 restarts per minute per slot) and runs the retirement statement even when every worker is busy or down. A live but hung thread is never replaced (Python cannot kill threads); its lease expires and another worker recovers the item.

On shutdown a stop event lets each worker finish its current item; threads are joined for up to 10 s. A clean restart costs no attempts. Only a hard kill leaves a lease to expire. Workers also delete orphaned `*.tmp` files older than two leases at startup (ADR-008).

## Consequences
- An item can execute twice (crash after rename but before commit; or a frozen worker past its lease). Harmless: same path, equal bytes.
- **FIFO starvation:** `ORDER BY id` means a 10,000-recipient job delays a later 1-recipient job. Documented in the README; the fix is fair scheduling across jobs (round-robin by `job_id`).
- SQLite lock acquisition is unfair: one worker may win most claims. That affects throughput only, not correctness.
- With no worker running at all, an expired item stays `processing` until a worker or the API's monitor runs.

## Tests
- `test_worker.py::test_two_concurrent_workers_never_double_process`: two workers, each with its own engines (as separate processes have). A slow renderer forces interleaving. Asserts each item was claimed exactly once, all succeeded, and each worker claimed at least one item (so the test cannot pass trivially).
- `test_crash_recovery.py::test_two_worker_processes_never_double_process` (slow): same assertions with two `python -m certgen.worker` subprocesses.
- `test_crash_recovery.py::test_stale_worker_done_write_rejected_by_fencing`: A claims; an injected clock passes the lease; B reclaims and succeeds; A's late completion updates 0 rows; the row shows `succeeded` with `attempts == 2`.
- `test_crash_recovery.py::test_same_worker_reclaim_is_fenced`: the same worker id reclaims; its old token is still rejected.
- `test_crash_recovery.py::test_kill_mid_job_no_duplicates` (slow): a subprocess worker with a short lease and a slow render. The test waits until the database shows a succeeded and a processing item (state-triggered, not timer-triggered), sends SIGKILL, and starts a second worker. It asserts every item succeeded, exactly one PDF per item, unique certificate ids, and the killed item at `attempts == 2`.
- `test_crash_recovery.py::test_crash_loop_ends_failed_after_max_attempts` (injected clock).
- `test_worker.py::test_graceful_shutdown_costs_no_attempts`, `test_monitor_retires_exhausted_lease_without_claims`, `test_restart_budget_is_bounded`.

## At 100× scale
PostgreSQL: add `FOR UPDATE SKIP LOCKED` to the pick statement so workers do not serialize on one lock. Fair scheduling across jobs. Add a heartbeat under the condition above. Add a per-attempt history table if operations need it.

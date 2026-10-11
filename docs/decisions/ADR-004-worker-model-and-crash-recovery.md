# ADR-004: Worker model and crash recovery — single-item leased claims, fencing token, attempt cap

Status: Accepted (Gate 1 Q4, Q5; revised after Phase 4 audit) · Requirements: R3, R6, R9, R11 · Evidence: RESEARCH §2, §3, §6

## Context
Workers can crash mid-item (OOM, deploy, SIGKILL). We need:
- (a) no item stuck in `processing` forever;
- (b) no item processed into two different certificates;
- (c) one bad item never blocks others (R9);
- (d) correctness regardless of how many workers run (Gate 1 Q5: embedded threads, `uvicorn --workers N`, standalone `python -m certgen.worker`, in any combination).

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
     ▲                   │ error & attempts >= MAX_ATTEMPTS ──► failed (GENERATION_FAILED)
     └─ lease expired ───┘
        (crash; the claim already counted an attempt; at MAX_ATTEMPTS ──► failed (WORKER_LOST))

  failed ──POST /retry (D3)──► pending   (attempts reset to 0)
```

## Options for crash recovery
1. **No recovery** (simplest). Items stuck in `processing` after a crash forever, so the job never finishes. Unacceptable.
2. **Startup sweep**: on boot, reset all `processing` → `pending`. Wrong with >1 worker: worker B booting steals live work from worker A.
3. **Lease (timeout) + fencing token** (chosen). Details below.
4. **Lease + heartbeat** (Procrastinate style). Workers extend leases periodically, allowing short leases for long tasks. **Rejected at Gate 1 (Q4):** one item takes ~20 ms against a 30 s lease (1,500× margin, per item, since we claim one item at a time), so heartbeat adds a thread and a failure mode while buying nothing.

**When a heartbeat becomes necessary:** when one claimed unit of work can legitimately run longer than the lease, so that a fixed lease forces a bad trade-off. A long lease makes crash detection slow; a short lease makes healthy workers lose claims. Concretely: email delivery to slow SMTP servers, or image-heavy multi-page templates. Rule of thumb: add one once p99 item duration exceeds ~10% of `LEASE_SECONDS`.

## Criteria
Correctness under crash, slow workers and any worker count; explainability; deterministic testability.

## Decision
**Option 3**, with these parameters, all env-configurable so tests can shrink them:

| Setting | Default | Note |
|---|---|---|
| `LEASE_SECONDS` | 30 | ≫ 20 ms render |
| `MAX_ATTEMPTS` | 3 | counted at claim |
| `RETRY_DELAY_SECONDS` | 2 | fixed delay, no jitter. Jitter protects a shared downstream from synchronized retries, and there is none here (audit cut #1) |
| `POLL_INTERVAL_SECONDS` | 0.5 | idle sleep when nothing is claimable |

### Worker identity
`worker_id = uuid4().hex`, generated **per worker thread or process** at start (logged). It's for observability only: **correctness never depends on it** (audit #2).

### Claim: one item, one atomic write transaction
On `write_engine` (`BEGIN IMMEDIATE`, ADR-002):
```sql
-- 1. Retire items whose lease expired on their last allowed attempt (crash-loop guard).
UPDATE items SET status='failed', errors=:worker_lost_json, lease_owner=NULL, lease_token=NULL,
                 lease_expires_at=NULL, completed_at=:now
WHERE status='processing' AND lease_expires_at < :now AND attempts >= :max_attempts;

-- 2. Claim the oldest claimable item.
UPDATE items SET status='processing', lease_owner=:worker_id, lease_token=:token,
                 lease_expires_at=:now + :lease_ms, attempts=attempts+1
WHERE id = (SELECT id FROM items
            WHERE (status='pending' AND (available_at IS NULL OR available_at <= :now))
               OR (status='processing' AND lease_expires_at < :now AND attempts < :max_attempts)
            ORDER BY id LIMIT 1)
RETURNING id, attempts;
```
- `:token = secrets.token_hex(16)`, fresh **per claim**.
- **Why it is atomic:** `BEGIN IMMEDIATE` takes SQLite's single write lock before the sub-select runs. No other connection in any thread or process can claim between our read and our update. A competing claimer waits (≤ `busy_timeout`) and then sees the row already `processing` with a live lease. [measured] RESEARCH §6: 4 independent engines, 2,000 rows, 2,000 unique claims.
- **One item per claim** (audit #10). A claim transaction costs about a millisecond; batching would make the lease cover N items, so per-item margins become per-batch margins and a crash charges N items an attempt. One item keeps the lease arithmetic honest.
- Attempts increment **at claim**, so a crash on an item consumes an attempt, and statement 1 ends it as `failed/WORKER_LOST` once it has used `MAX_ATTEMPTS`. River documents the same "discard at max attempts" rescue.

### Process, outside any transaction
Load the item + job snapshot (`read_engine`), render (pure function of item + job snapshot, ADR-007), write the file (ADR-008: unique tmp name → `fsync` → `os.replace`). No DB transaction is open during render or I/O.

Every renderer exception is treated as retryable. Inputs that are deterministically unrenderable (missing glyphs, RTL) are rejected at validation (ADR-005/007), so what reaches the renderer and fails is presumed transient (disk full, I/O error). If it fails `MAX_ATTEMPTS` times, it ends `failed/GENERATION_FAILED` with the exception class and message (no traceback) in `errors`.

### Fenced transitions: the token, not the worker id
Every transition out of `processing` is conditional on the claim token:
```sql
UPDATE items SET status='succeeded', lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                 errors=NULL, completed_at=:now
WHERE id=:id AND lease_token=:token AND status='processing';
```
(The retry and fail transitions use the same `WHERE`.) If `rowcount == 0`, the lease was lost: another claim replaced the token, or `/retry` reset the row. The worker logs `lease_lost` and does nothing else. Because the token is unique per claim, this holds even if the same thread reclaims the same item, if `/retry` resets attempts, or if a PID is reused (audit #2).

### Output idempotency: what is and isn't guaranteed
- Path `{job_id}/{certificate_id}.pdf`. `certificate_id` is fixed at item creation (ADR-013), so every execution of an item targets the same path.
- Render inputs come **only from the DB row** (item + job snapshot, including `print_email` and `verify_base_url`), never from the worker's environment (audit #7). Canvas is created with `invariant=1`.
- [measured] same inputs → byte-identical PDF across 3 separate processes with different `PYTHONHASHSEED` (RESEARCH §6).
- Therefore a stale worker's late `os.replace` (it can't be fenced; file systems don't take our token) replaces the file with **equal bytes**. The guarantee is: *one path per certificate, atomic replace, equal content for equal inputs*. That's at-least-once execution + idempotent effect. We do not claim exactly-once execution.

### Graceful shutdown (audit #24)
`lifespan` shutdown sets a stop event; each worker finishes its current item (~20 ms) and exits. Threads are joined with a timeout of `LEASE_SECONDS`. A clean restart (`--reload`, deploy) costs no attempts. Only a hard kill leaves a lease to expire.

## Consequences
- An item can execute twice (crash after rename, before commit; or a frozen worker past its lease). Harmless: same path, equal bytes. A test proves it.
- **FIFO starvation** (audit #23): `ORDER BY id` means a 10,000-recipient job (~3.4 min) delays a later 1-recipient job. Accepted for v1 and documented in README. The fix is fair scheduling across jobs (round-robin by `job_id`), in the 100× section.
- SQLite lock acquisition is unfair: one worker may win most claims ([measured] 1320/0/60/620). That affects throughput only, not correctness.

## Tests
- `test_worker.py::test_two_concurrent_workers_never_double_process`: two workers, **each with its own engines** (as separate processes have). A renderer that sleeps 5 ms forces interleaving; 100 items. Asserts: each item claimed exactly once (claim log), all `succeeded`, **each worker claimed ≥ 1 item** (so the test can't pass trivially; audit #21).
- `test_crash_recovery.py::test_two_worker_processes_never_double_process` (slow): same assertions with two `python -m certgen.worker` subprocesses.
- `test_crash_recovery.py::test_stale_worker_done_write_rejected_by_fencing`: A claims (token tA); injected clock passes the lease; B reclaims (token tB) and succeeds; A's completion with tA updates 0 rows and returns `lease_lost`; the row shows `succeeded`, `attempts == 2`.
- `test_crash_recovery.py::test_same_worker_reclaim_is_fenced`: the same worker id reclaims; its old token is still rejected.
- `test_crash_recovery.py::test_kill_mid_job_no_duplicates` (D2 proof, slow): subprocess worker with `LEASE_SECONDS=2`, `CERTGEN_TEST_SLOW_RENDER_MS=300`. The test waits until the DB shows ≥ 1 succeeded item and 1 processing (state-triggered, not timer-triggered), sends SIGKILL, and starts a second worker. It asserts all items `succeeded`, exactly one `*.pdf` per item, unique certificate IDs, the killed item has `attempts == 2`, and no `*.tmp` remains after the sweep. Expected runtime ≈ 5 s.
- `test_crash_recovery.py::test_crash_loop_ends_failed_after_max_attempts` (injected clock).
- `test_worker.py::test_graceful_shutdown_costs_no_attempts`.
- `test_rendering.py::test_render_is_byte_deterministic`.

## At 100× scale
Postgres: add `FOR UPDATE SKIP LOCKED` to the sub-select so workers don't serialize on one lock. Fair scheduling across jobs. Add a heartbeat under the condition above. Add a per-attempt history table if ops needs it.

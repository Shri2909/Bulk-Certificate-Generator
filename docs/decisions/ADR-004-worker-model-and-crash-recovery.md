# ADR-004: Worker model and crash recovery — leased claims with fencing, attempt cap, idempotent output

Status: Accepted (Gate 1 Q4, Q5) · Requirements: R3, R6, R9, R11 · Evidence: RESEARCH §2, §3

## Context
Workers can crash mid-item (OOM, deploy, SIGKILL). We need: (a) no item stuck in `processing` forever, (b) no item processed into two different certificates, (c) one bad item never blocks others (R9).

## Item state machine
```
            (validation fails)
  request ───────────────────────► rejected            (terminal, never attempted)
     │
     ▼
  pending ──claim──► processing ──ok──► succeeded      (terminal)
     ▲                   │
     │   error & attempts < MAX ──(available_at = now + backoff)
     └───────────────────┤
     ▲                   │ error & attempts >= MAX ──► failed   (terminal; D3 /retry → pending)
     └── lease expired ──┘   (crash; counted as an attempt)
```

## Options for crash recovery
1. **No recovery** (simplest). Items stuck in `processing` after a crash forever. Unacceptable: violates R9 in spirit and makes the job never finish.
2. **Startup sweep**. On boot, reset all `processing` → `pending`. Simple, but wrong with >1 worker process: worker B booting would steal live work from worker A.
3. **Lease (timeout) + fencing** (chosen). Claim sets `lease_owner = <worker_id>` and `lease_expires_at = now + LEASE_SECONDS (30)`. Any worker may reclaim an item whose lease expired. Completion is a conditional `UPDATE … WHERE id = :id AND lease_owner = :me`. If the lease was taken over, the stale worker's write affects 0 rows and is discarded.
4. **Lease + heartbeat** (Procrastinate style). Workers extend leases periodically, which allows short leases for long tasks. **Rejected at Gate 1 (Q4):** our tasks take ~20 ms against a 30 s lease (a 1,500× margin), so heartbeat adds a thread and a failure mode while buying nothing.

**When a heartbeat becomes necessary:** when one claimed unit of work can legitimately run longer than the lease, so that a fixed lease forces a bad trade-off. A long lease makes crash detection slow, and a short lease makes healthy workers lose their claims. Concretely: email delivery to slow SMTP servers, very large batches, or multi-page/image-heavy templates. The rule of thumb is to add a heartbeat once p99 batch duration exceeds ~10% of `LEASE_SECONDS`.

## Criteria
Correctness under crash and slow workers, explainability, testability (deterministic kill test).

## Decision
**Option 3** (Gate 1 Q4): `LEASE_SECONDS = 30`, fencing, `MAX_ATTEMPTS = 3`, no heartbeat.

**Hard requirement (Gate 1 Q5): claiming is a single atomic DB operation.** Two embedded worker threads, several `uvicorn --workers N` processes, and a standalone `python -m certgen.worker` must never process the same item concurrently. Correctness may not depend on there being "only one worker".

- **Claim transaction** (`BEGIN IMMEDIATE`, so the write lock is taken up front, then two statements):
  ```sql
  -- 1. Retire items whose lease expired on their last allowed attempt (crash loop guard).
  UPDATE items SET status='failed', error_code='WORKER_LOST', lease_owner=NULL, completed_at=:now
  WHERE status='processing' AND lease_expires_at < :now AND attempts >= :max_attempts;

  -- 2. Claim a batch atomically.
  UPDATE items SET status='processing', lease_owner=:w, lease_expires_at=:exp, attempts=attempts+1
  WHERE id IN (SELECT id FROM items
               WHERE (status='pending' AND (available_at IS NULL OR available_at <= :now))
                  OR (status='processing' AND lease_expires_at < :now AND attempts < :max_attempts)
               ORDER BY id LIMIT :batch)
  RETURNING id;
  ```
  Why it is atomic: SQLite allows one writer at a time. `BEGIN IMMEDIATE` acquires the RESERVED lock before reading, so no other connection, in any thread or process, can claim between our sub-select and our update. A second claimer waits up to `busy_timeout`, then sees the rows already `processing` with a live lease. With Postgres, the same guarantee needs `FOR UPDATE SKIP LOCKED` in the sub-select (see 100× section).
- Attempts increment **at claim**, so a crash on one item consumes an attempt, and statement 1 ends it as `failed/WORKER_LOST` after `MAX_ATTEMPTS`. River documents the same "discard at max attempts" rescue.
- **Render outside any DB transaction.** Never hold the SQLite write lock during the ~20 ms render.
- **Fenced completion:** `UPDATE items SET status='succeeded', lease_owner=NULL, completed_at=:now WHERE id=:id AND lease_owner=:me AND status='processing'`. If `rowcount == 0`, the lease was lost, so the worker logs `lease_lost` and discards its result without touching the row. Failure/retry transitions are fenced identically.
- **Idempotent output.** File path is `{job_id}/{certificate_id}.pdf`, and `certificate_id` is fixed at item creation (ADR-013). The write goes to `*.tmp` (unique per worker) then `os.replace` (atomic rename). The canvas is created with ReportLab's `invariant=1`, which drops the creation timestamp and random document ID. So a re-render of the same item is **byte-identical**, and a stale worker's late rename overwrites the file with the same bytes. Duplicate or divergent files are impossible by construction: at-least-once execution + idempotent effect = "effectively once".
- **Retry backoff**: `available_at = now + BASE * 2^(attempts-1) * jitter(0.5–1.5)`, base 1 s (Stripe's 2^n + jitter).
- **Batch size** 20 items per claim. The lease must cover the whole batch: 20 × 20 ms = 0.4 s ≪ 30 s.

## Consequences
- An item can execute twice (crash after rename, before commit). Harmless: same path, same bytes. A test proves it.
- A worker frozen > 30 s mid-render loses its lease. Fencing discards its late write.
- A crash with a full batch claimed costs every item in that batch one attempt. Acceptable at batch 20 with 3 attempts. Documented.

## Tests (required by Gate 1)
- `test_worker.py::test_two_concurrent_workers_never_double_process`: two workers, each with **its own engine/connections** (as separate processes have), drain 200 items concurrently from barrier-synchronised threads. Assert every item is claimed exactly once (per-item claim log = 1 entry), all succeeded, and the sum of claimed counts is 200.
- `test_crash_recovery.py::test_two_worker_processes_never_double_process` (slow): the same assertion with two `python -m certgen.worker` subprocesses.
- `test_crash_recovery.py::test_stale_worker_done_write_rejected_by_fencing`: worker A claims an item; the injected clock advances past the lease; worker B reclaims and succeeds; A's completion then returns `lease_lost`, and the row still shows B as completer with `attempts == 2`.
- `test_crash_recovery.py::test_kill_mid_job_no_duplicates` (D2 proof): SIGKILL a subprocess worker mid-job, start another, assert all succeeded, exactly one file per item, unique certificate IDs.
- `test_crash_recovery.py::test_crash_loop_ends_failed_after_max_attempts`.
- `test_rendering.py::test_render_is_byte_deterministic`.

## At 100× scale
Postgres: add `FOR UPDATE SKIP LOCKED` to the sub-select so workers don't serialize. Add a heartbeat under the condition stated above. Add a per-attempt history table if ops needs it.

# ADR-004: Worker model and crash recovery — leased claims with fencing, attempt cap, idempotent output

Status: Proposed (heartbeat question open → Gate Q4) · Requirements: R3, R6, R9, R11 · Evidence: RESEARCH §2, §3

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
4. **Lease + heartbeat** (Procrastinate style). Workers extend leases periodically, which allows short leases for long tasks. Our tasks take ~20 ms against a 30 s lease, a 1,500× margin, so heartbeat adds a thread and a failure mode while buying nothing. → **Gate Q4**.

## Criteria
Correctness under crash and slow workers, explainability, testability (deterministic kill test).

## Decision
**Option 3.**
- **Claim** (one atomic statement, SQLite serializes writers):
  ```sql
  UPDATE items SET status='processing', lease_owner=:w, lease_expires_at=:exp, attempts=attempts+1
  WHERE id IN (SELECT id FROM items
               WHERE (status='pending' AND (available_at IS NULL OR available_at <= :now))
                  OR (status='processing' AND lease_expires_at < :now)
               ORDER BY id LIMIT :batch)
  RETURNING id
  ```
  Attempts increment **at claim**, so a crash that loops on one item still hits `MAX_ATTEMPTS` (3) and ends `failed` (error `WORKER_LOST`) instead of looping forever. River documents the same "discard at max attempts" rescue.
- **Render outside any DB transaction.** Never hold the single SQLite write lock during the 20 ms render.
- **Idempotent output.** File path is `{job_id}/{certificate_id}.pdf`, and `certificate_id` is fixed at item creation (ADR-013). The write goes to `*.tmp` then `os.replace` (atomic rename). Re-processing after a crash overwrites the same file with identical content, so duplicate files are impossible by construction. This is at-least-once execution + idempotent effect = "effectively once".
- **Retry backoff**: `available_at = now + BASE * 2^(attempts-1) * jitter(0.5–1.5)`, base 1 s (Stripe's 2^n + jitter).
- **Batch size** 20 items per claim. The lease must cover the whole batch: 20 × 20 ms = 0.4 s ≪ 30 s.

## Consequences
- An item can execute twice (crash after rename, before commit). Harmless by construction. A test proves it.
- A worker frozen > 30 s mid-render loses its lease. Fencing stops its late write from flipping state.

## Tests
`test_crash_recovery.py::test_kill_mid_job_no_duplicates` (SIGKILL a subprocess worker, restart, assert all succeeded, one file per item, unique certificate IDs), `::test_stale_worker_cannot_overwrite`, `::test_crash_loop_ends_failed_after_max_attempts`, `test_worker.py::test_two_workers_never_claim_same_item`.

## At 100× scale
Postgres: add `FOR UPDATE SKIP LOCKED` to the sub-select so workers don't serialize. Add a heartbeat if tasks become long (e.g. email delivery with slow SMTP). Add a per-attempt history table if ops needs it.

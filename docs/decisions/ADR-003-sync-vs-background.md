# ADR-003: Synchronous vs background generation — background, DB-backed queue, embedded worker

Status: Accepted (Gate 1 Q5) · Requirements: R7, R9, R11, R12 · Evidence: RESEARCH §4, §6 [measured]

## Context
The PDF explicitly asks for this choice and its reasoning (R12). Measured render cost: **20.3 ms per certificate**, so 1,000 recipients ≈ 20 s and 10,000 ≈ 3.4 min of CPU on one worker.

## Options
1. **Synchronous** (simplest). Generate everything inside `POST`, return results.
   - Pro: no queue, no state machine, trivially testable.
   - Con: at 1,000 recipients the client waits ~20 s; common proxy timeouts (often 30–60 s) cut larger jobs, with no partial result. "Progress" (R7) is meaningless because the request is either running or done. A client retry after a timeout regenerates everything.
2. **FastAPI `BackgroundTasks`**. Respond 202, then run in-process after the response.
   - Pro: two lines of code.
   - Con: the task exists only in memory. A process restart loses it with no record. The docs point heavy work to Celery-class tools.
3. **DB-backed queue + worker loop** (chosen). `POST` inserts job + items in one transaction and returns `202`. A worker loop claims pending items from the DB, renders, and records results.
   - Pro: progress is real (item counts). Crash-safe, because state is in the DB. The worker runs embedded (thread, default) **or** as a separate process with the same code.
   - Con: needs a state machine, leases, and a worker loop (~150 lines).
4. **Celery/RQ + Redis**. Industry standard, but violates P3 (zero external services).

## Criteria
Correctness (no lost work), failure isolation, ease to run/explain, testability.

## Decision
**Option 3.** It's the only option that gives real progress (R7), survives restarts, and keeps one-command startup.
- Default: `uvicorn certgen.main:app` starts the API **and** `WORKER_THREADS` (default 2) worker threads via `lifespan`.
- Optional: `CERTGEN_EMBEDDED_WORKER=0` + `python -m certgen.worker` runs workers as separate processes. The crash test (ADR-004) uses this mode so it can `SIGKILL` a worker.
- **Every topology is safe** (Gate 1 Q5): embedded threads, `uvicorn --workers N` (N processes × threads), and standalone workers can run at the same time. Claiming is one atomic DB transaction (ADR-004), so nothing assumes a single worker.
- Tests set `CERTGEN_EMBEDDED_WORKER=0` and call `worker.run_until_idle()` synchronously. No sleeps, no flakiness (ADR-012).

Threads vs processes: ReportLab rendering is pure-Python, CPU-bound work, so threads share the GIL. Two threads overlap DB I/O and file writes but don't double CPU throughput. More throughput = more worker **processes**, documented in README with the measured number.

## Consequences
- The client must poll (`GET /v1/certificate-jobs/{id}`). Webhooks are out of scope (PLAN scope guard) but slot in at job completion.
- Two runtime modes to explain. Mitigation: both run the same `worker.run_forever()` function.

## At 100× scale
Run N worker processes or containers against Postgres with `SKIP LOCKED`. Optionally replace polling of the DB with a broker (SQS/Redis) carrying item IDs, keeping the DB as the source of truth for status.

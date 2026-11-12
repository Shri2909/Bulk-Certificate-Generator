# ADR-003: Synchronous vs background generation — background, DB-backed queue, embedded worker

Status: Accepted · Requirements: R7, R9, R11, R12

## Context
The assignment asks for this choice and its reasoning (R12). Measured cost (README Benchmarks): rendering takes about 4 ms per certificate, and a full item (claim, render, fsynced file write, commit) about 11 ms on one worker. So 1,000 recipients take roughly 11 s and 10,000 roughly 1.8 min on one worker.

## Options
1. **Synchronous** (simplest). Generate everything inside `POST`, return results.
   - Pro: no queue, no state machine, trivially testable.
   - Con: the client waits for the whole run, and common proxy timeouts (often 30–60 s) cut larger jobs with no partial result. "Progress" (R7) is meaningless because the request is either running or done. A client retry after a timeout regenerates everything.
2. **FastAPI `BackgroundTasks`**. Respond 202, then run in-process after the response.
   - Pro: two lines of code.
   - Con: the task exists only in memory. A process restart loses it with no record. The docs point heavy work to Celery-class tools.
3. **DB-backed queue + worker loop** (chosen). `POST` inserts the job and its items in one transaction and returns `202`. A worker loop claims pending items from the database, renders, and records results.
   - Pro: progress is real (item counts). Crash-safe, because state is in the database. The worker runs embedded (threads, default) **or** as a separate process with the same code.
   - Con: needs a state machine, leases and a worker loop.
4. **Celery/RQ + Redis**. Industry standard, but violates P3 (zero external services).

## Criteria
Correctness (no lost work), failure isolation, ease to run and explain, testability.

## Decision
**Option 3.** It is the only option that gives real progress (R7), survives restarts, and keeps one-command startup.
- Default: `python -m certgen` (or `uvicorn certgen.main:app`) starts the API **and** `CERTGEN_WORKER_THREADS` (default 2) worker threads via `lifespan`.
- Optional: `CERTGEN_EMBEDDED_WORKER=false` + `python -m certgen.worker` runs workers as separate processes. The crash test (ADR-004) uses this mode so it can `SIGKILL` a worker.
- **Every topology is safe:** embedded threads, several API processes, and standalone workers can run at the same time. Claiming is one atomic database transaction (ADR-004), so nothing assumes a single worker.
- Tests disable the embedded worker and drain the queue synchronously with `Worker.run_until_idle()`. No sleeps, no flakiness (ADR-012).

Threads vs processes: ReportLab rendering is pure-Python, CPU-bound work, so threads share the GIL. Two threads overlap database I/O and file writes but do not double CPU throughput. More throughput means more worker **processes**; the README reports the measured numbers.

## Consequences
- The client must poll (`GET /v1/certificate-jobs/{id}`). Webhooks are out of scope but would slot in at job completion.
- Two runtime modes to explain. Mitigation: both run the same `Worker.run_forever()` loop.

## At 100× scale
Run N worker processes or containers against PostgreSQL with `SKIP LOCKED`. Optionally replace database polling with a broker (SQS/Redis) carrying item ids, keeping the database as the source of truth for status.

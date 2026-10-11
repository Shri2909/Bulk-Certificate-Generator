# Plan

## 1. Three candidate architectures

| | **A. Synchronous** | **B. DB-backed queue + embedded worker** | **C. Celery + Redis + Postgres** |
|---|---|---|---|
| Shape | `POST` renders all PDFs, returns job with results | `POST` → 202; worker threads in the API process (or separate process) claim items from SQLite | `POST` → 202; Celery tasks per item via Redis broker |
| Run | 1 command | 1 command | 3+ services (docker-compose) |
| Progress (R7) | none until done | real, per item | real, per item |
| Crash safety | request dies, nothing recorded | lease + fencing (ADR-004) | broker acks + visibility timeout |
| 1,000 recipients | ~20 s blocking request | ~20 s in background | ~20 s / N workers |
| Explainability | trivial | ~150 lines of worker you wrote and can defend | lots of framework config you didn't write |

Scored 1–5 (weight):

| Criterion (weight) | A | B | C |
|---|---|---|---|
| Spec coverage incl. progress, failure isolation (×3) | 2 | 5 | 5 |
| Runs first try, zero external services (×3) | 5 | 5 | 1 |
| Crash correctness (×2) | 1 | 4 | 4 |
| Explainability in interview (×2) | 5 | 4 | 2 |
| Testability, deterministic (×2) | 5 | 5 | 2 |
| Path to 100× (×1) | 1 | 4 | 5 |
| **Weighted total** | **44** | **60** | **39** |

**Chosen: B.** C is what a large org would run, but here it loses on P3 and explainability, and the 100× notes in each ADR show B → C is an incremental migration, not a rewrite.

## 2. Pre-mortem: "my submission was rejected" — 20 reasons → mitigations

| # | Failure | Mitigation / test |
|---|---|---|
| 1 | Reviewer's `pip install` fails (system lib, wrong Python) | Pure-wheel deps only (no WeasyPrint/Pango). `requires-python >=3.10`. Phase 6 clean-venv run. Python version printed in README. |
| 2 | App starts but nothing ever generates (worker didn't start) | Embedded worker is the default. `/healthz` reports `worker_threads_alive`. Smoke test starts the real app and polls a job to completion. |
| 3 | A required PDF sentence is missed | `test_spec_traceability.py` fails on any uncovered R#. Phase 4 adversarial audit against the PDF. |
| 4 | Flaky tests (sleep/timing) | `run_until_idle` + injectable clock. The only real-time test (crash) uses generous timeouts and polls the DB state, never sleeps fixed amounts. Suite run 3× in Phase 6. |
| 5 | Over-engineering loses the reviewer | Scope guard below. Differentiators capped at 4, each droppable. `v1.0-required` tag first. |
| 6 | Can't explain the code | WALKTHROUGH + quiz. Every module < ~200 lines. No metaprogramming. |
| 7 | Race: two workers process the same item | Atomic claim `UPDATE … RETURNING`. Test `test_two_workers_never_claim_same_item` (threads hammering claim). |
| 8 | Crash leaves item `processing` forever | Lease expiry reclaim. Test `test_kill_mid_job_no_duplicates`. |
| 9 | Crash loop on a poison item | Attempts counted at claim → `failed/WORKER_LOST` at max. Test `test_crash_loop_ends_failed_after_max_attempts`. |
| 10 | One bad recipient kills the job | Per-item try/except + per-item transaction. Test R18 with injected renderer failure. |
| 11 | Unicode name produces blank boxes silently | cmap + bidi validation. Test `test_unrenderable_name_rejected`. |
| 12 | Path traversal via certificate ID | IDs only looked up in DB. Path built from DB values. Test with `..%2F` IDs. |
| 13 | "database is locked" under load | WAL + `busy_timeout=5000` + no transaction held during render. Concurrency test with 4 worker threads + concurrent polling. |
| 14 | Double job on client retry | Idempotency-Key (D4). |
| 15 | Huge payload OOMs the server | `MAX_BODY_BYTES` + `MAX_RECIPIENTS`. Tests in `test_limits.py`. |
| 16 | README rots (commands no longer work) | `test_docs.py` checks sections and ADR links. Phase 6 runs README commands verbatim from a clean clone. |
| 17 | Verify endpoint leaks PII / is enumerable | Random 128-bit IDs. No email in response. Uniform invalid response. Test asserts no `@` in body. |
| 18 | Download before generation returns 500 | `409 CERTIFICATE_NOT_READY`. Test. |
| 19 | Job status lies (stored status drifts from items) | Status derived from item counts, never stored (ADR-002). Test covers every derivation branch. |
| 20 | Leaked stack traces / 500 with HTML | Global exception handler → problem+json `INTERNAL_ERROR`, logged with request ID. Test forces an exception in a route. |

## 3. Milestones (each = commit; acceptance = listed tests green + traceability for its R#s)

| M | Scope | Acceptance criteria |
|---|---|---|
| M0 | Docs (this gate) | REQUIREMENTS, RESEARCH, 13 ADRs, PLAN approved. Traceability test red (expected). |
| M1 | Walking skeleton: settings, DB, models, `POST` (no validation beyond Pydantic) → 202, `run_until_idle`, minimal renderer, `GET` job, `GET` certificate | One end-to-end test: create 2 recipients → drain → status `completed` → download returns `%PDF`. |
| M2 | Row validation (ADR-005, ADR-007 glyph check), problem+json errors (ADR-010), limits (ADR-011) | `test_validation.py`, `test_errors.py`, `test_limits.py` green. R2, R15. |
| M3 | Real template + rendering assertions (R4, R5) | `test_rendering.py` text extraction asserts all fields. |
| M4 | Worker hardening: leases, fencing, retries/backoff, attempts cap, failure isolation, embedded threads, standalone `python -m certgen.worker` | `test_worker.py`, `test_failure_isolation.py`, `test_crash_recovery.py` (incl. SIGKILL) green. R3, R6, R9, R18. |
| M5 | Items listing + pagination, ZIP archive + manifest.csv | `test_retrieval.py` green. R8, R10, R19. |
| M6 | README (R20–R25), `test_docs.py`, ruff + mypy clean | Traceability **green**. Tag **`v1.0-required`**. |
| M7 | D1 verification (+ QR) | `test_verify.py`. README section. |
| M8 | D2 crash-safety proof surfaced in README (test exists from M4) + benchmark script | `scripts/benchmark.py` output in README. |
| M9 | D3 `dry_run` + `/retry` | `test_retry.py`, `test_dry_run.py`. |
| M10 | D4 Idempotency-Key | `test_idempotency.py`. |
| M11 | Phase 6 verification + hostile review fixes; Phase 7 WALKTHROUGH | Clean-venv transcript, 3× suite runs, numbers in README. |

Note on order: your brief lists D2 (crash safety) as a differentiator, but crash recovery is in **required** scope (5b). So M4 builds it, and M8 only surfaces the proof (benchmark + kill demo).

## 4. Scope guard — we will NOT build
- Auth / API keys / multi-tenancy (README: "first change for production", with files to touch).
- Template editor, multiple templates, custom branding uploads (spec says not needed).
- Email delivery, webhooks (WALKTHROUGH covers them as changed-requirement scenarios).
- Postgres, Docker, Redis, Celery, any broker (ADR-002/003 100× sections describe the path).
- CSV upload (ADR-005 100× path).
- Alembic migrations (single schema version, documented limitation).
- Frontend, dashboard, admin UI, metrics stack.
- Rate limiting (ADR-011).
- Retention/cleanup of old jobs and idempotency keys (documented limitation).

## 5. Differentiator plan (one branch each; dropped if it lacks tests + README section)

| D | What | Why it's worth it | Risk / cost |
|---|---|---|---|
| D1 | Certificate ID + verify endpoint + QR on PDF (+ HMAC sig, pending Gate Q3) | Certificates are checkable, not just downloadable. A visible feature in the PDF itself. | Key management if HMAC is kept. ~120 lines. |
| D2 | Kill-the-worker proof: SIGKILL test + README transcript + benchmark | Turns "I handled crashes" into evidence. | Subprocess test needs care on CI. POSIX only. |
| D3 | `?dry_run=true` + `POST /{id}/retry` | Closes the fix-and-retry loop that partial acceptance (ADR-005) creates. | ~60 lines. Retry must respect leases. |
| D4 | `Idempotency-Key` | Bulk POSTs are the ones that time out. | Concurrency test. ~50 lines. |
| + | `manifest.csv` in ZIP | Auditable offline result. ~20 lines, part of M5. | — |

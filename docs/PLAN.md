# Plan

## 0. Gate 1 decisions (2026-10-11)

| # | Decision | Reason | Recorded in |
|---|---|---|---|
| Q1 | **Partial acceptance.** Bad rows → `rejected` items with error codes; valid rows generated. Whole-request `422` only for structurally malformed requests (bad JSON, missing required top-level fields, empty or over-limit recipients) or when **every** row is invalid. | The spec says "generate … for each **valid** recipient". One typo must not block thousands of valid certificates. `dry_run` (D3) gives strictness on demand. | ADR-005, REQUIREMENTS A1 |
| Q2 | **Duplicates:** same email (trimmed, case-insensitive) twice in one job → later row `DUPLICATE_RECIPIENT`, message includes the first occurrence's row index. Allowed across jobs. | One person, one certificate per job. Pointing at the first row makes the error fixable. Re-issuing across jobs (next cohort, re-run) is legitimate. | ADR-005, REQUIREMENTS A4 |
| Q3 | **Random 128-bit certificate ID + DB as source of truth; no HMAC.** D1 renamed **"Verifiable certificates"**. Never claim "tamper-evident". | Verification is online, so the DB lookup already proves authenticity, and HMAC adds a secret to manage for no security gain. Add a signature (Ed25519 preferred) only for offline verification without DB access. | ADR-013 |
| Q4 | **30 s lease + fencing + max 3 attempts; no heartbeat.** Required test: a slow worker returning after lease expiry has its "done" write rejected. | Items take ~20 ms vs a 30 s lease. A heartbeat adds a thread and a failure mode for nothing. ADR-004 states when it becomes necessary. | ADR-004 |
| Q5 | **Worker threads inside the API process by default**; `python -m certgen.worker` optional. Claim must be one atomic DB operation; test with two concurrent workers. | One command for the reviewer. Correctness may not depend on worker count (`uvicorn --workers N` must be safe). | ADR-003, ADR-004 |
| A3 | **Email stored and returned, not printed by default**; `PRINT_EMAIL_ON_CERTIFICATE` (default false) prints it. | Keeps PII off public documents, while a strict reading of the spec is one config change away. | ADR-007, REQUIREMENTS A3 |
| D2 | Crash recovery is built in **required** milestone M4; D2 is only the proof layer (kill-the-worker test + benchmark in README). | Crash recovery is required scope (5b), so it can't be optional. | §3, §5 below |

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
| 1 | Reviewer's `pip install` fails (system lib, wrong Python) | Pure-wheel deps only (no WeasyPrint/Pango). `requires-python >=3.11` (StrEnum; audit #5), stated on the README's first screen. Phase 6 clean-venv run on 3.11 **and** 3.14. |
| 2 | App starts but nothing ever generates (worker didn't start) | Embedded worker is the default. `/healthz` reports `embedded_workers_alive`/`expected`. Smoke test starts the real app and polls a job to completion. |
| 3 | A required PDF sentence is missed | `test_spec_traceability.py` fails on any uncovered R#. Phase 4 adversarial audit against the PDF. |
| 4 | Flaky tests (sleep/timing) | `run_until_idle` + injectable clock. The only real-time test (crash) uses generous timeouts and polls the DB state, never sleeps fixed amounts. Suite run 3× in Phase 6. |
| 5 | Over-engineering loses the reviewer | Scope guard below. Differentiators capped at 4, each droppable. `v1.0-required` tag first. |
| 6 | Can't explain the code | WALKTHROUGH + quiz. Every module < ~200 lines. No metaprogramming. |
| 7 | Race: two workers process the same item | Atomic claim (`BEGIN IMMEDIATE` + `UPDATE … RETURNING`). Tests `test_two_concurrent_workers_never_double_process` (separate engines) + 2-subprocess variant. |
| 8 | Crash leaves item `processing` forever | Lease expiry reclaim. Test `test_kill_mid_job_no_duplicates`. |
| 9 | Crash loop on a poison item | Attempts counted at claim → `failed/WORKER_LOST` at max. Test `test_crash_loop_ends_failed_after_max_attempts`. |
| 10 | One bad recipient kills the job | Per-item try/except + per-item transaction. Test R18 with injected renderer failure. |
| 11 | Unicode name produces blank boxes silently | cmap + bidi validation. Test `test_unrenderable_name_rejected`. |
| 12 | Path traversal via certificate ID | IDs only looked up in DB. Path built from DB values. Test with `..%2F` IDs. |
| 13 | "database is locked" under load | Separate read engine (deferred `BEGIN`), so polling never takes the write lock (audit #1, measured). WAL + `busy_timeout=5000`. No transaction held during render or file streaming. Test `test_open_read_does_not_block_claim` + a concurrency test with 2 workers and a polling thread. |
| 14 | Double job on client retry | Idempotency-Key (D4). |
| 15 | Huge payload OOMs the server | `MAX_BODY_BYTES` + `MAX_RECIPIENTS`. Tests in `test_limits.py`. |
| 16 | README rots (commands no longer work) | `test_docs.py` checks sections and ADR links. Phase 6 runs README commands verbatim from a clean clone. |
| 17 | Verify endpoint leaks PII / is enumerable | Random 128-bit IDs. No email in response. Uniform invalid response. Test asserts the recipient's email is absent from the body. |
| 18 | Download before generation returns 500 | `409 CERTIFICATE_NOT_READY`. Test. |
| 19 | Job status lies (stored status drifts from items) | Status derived from item counts, never stored (ADR-002). Test covers every derivation branch. |
| 20 | Leaked stack traces / 500 with HTML | Global exception handler → problem+json `INTERNAL_ERROR`, logged with request ID. Test forces an exception in a route. |

## 3. Milestones (each = commit; acceptance = listed tests green + traceability for its R#s)

| M | Scope | Acceptance criteria |
|---|---|---|
| M0 | Docs (this gate) | REQUIREMENTS, RESEARCH, 13 ADRs, PLAN approved. Traceability test red (expected). |
| M1 | Walking skeleton: settings, DB, models, `POST` (no validation beyond Pydantic) → 202, `run_until_idle`, minimal renderer, `GET` job, `GET` certificate | One end-to-end test: create 2 recipients → drain → status `completed` → download returns `%PDF`. |
| M2 | Row validation (ADR-005, ADR-007 glyph check) with all errors per row in `items.errors`, problem+json errors with one flat shape (ADR-010), limits incl. derived `MAX_BODY_BYTES` (ADR-011) | `test_validation.py` (incl. missing/non-string name → rejected, not 500), `test_errors.py`, `test_limits.py` (worst-case boundary) green. R2, R15. |
| M3 | Real template + rendering assertions (R4, R5); certificate ID printed (no QR yet); `print_email` snapshot; `invariant=1` | `test_rendering.py`: text extraction asserts all fields; email absent by default / present when the job snapshot says so; byte-deterministic render. |
| M4 | Worker hardening: two engines, single-item atomic claim, lease + **token** fencing, fixed retry delay, attempts cap + `WORKER_LOST`, unique tmp + orphan sweep, failure isolation, graceful shutdown, embedded threads, standalone `python -m certgen.worker`, derived job status | Green: `test_two_concurrent_workers_never_double_process` (each worker ≥ 1 claim), `test_two_worker_processes_never_double_process`, `test_stale_worker_done_write_rejected_by_fencing`, `test_same_worker_reclaim_is_fenced`, `test_kill_mid_job_no_duplicates`, `test_crash_loop_ends_failed_after_max_attempts`, `test_graceful_shutdown_costs_no_attempts`, `test_open_read_does_not_block_claim`, `test_job_status.py` (every derivation row), `test_failure_isolation.py`. R3, R6, R9, R17, R18. |
| M5 | Items listing with keyset pagination (`after`) + status filter; single download (409 not-ready vs generation-failed, 410); ZIP archive + CSV-injection-safe manifest.csv | `test_retrieval.py` green. R8, R10, R19. |
| M6 | README (R20–R25), `test_docs.py`, ruff + mypy clean | Traceability **green**. Tag **`v1.0-required`**. |
| M7 | D1 **Verifiable certificates**: QR (from `jobs.verify_base_url` snapshot) + public `GET /v1/verify/{id}` | `test_verify.py`. README section (wording: "verifiable", never "tamper-evident"). |
| M8 | D2 proof layer only: kill-the-worker demo transcript + `scripts/benchmark.py` (1,000 recipients) in README. Mechanism already built in M4. | Raw benchmark output pasted in README. |
| M9 | D3 `dry_run` (always 200 report, ignores Idempotency-Key, creates nothing) + `/retry` (full reset statement, ADR-009) | `test_retry.py`, `test_dry_run.py` (incl. dry-run with a key doesn't poison the real submission). |
| M10 | D4 Idempotency-Key | `test_idempotency.py`. |
| M11 | Phase 6 verification + hostile review fixes; Phase 7 WALKTHROUGH | Clean-venv transcript, 3× suite runs, numbers in README. |


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
| D1 | **Verifiable certificates**: random certificate ID + QR on PDF + public `GET /v1/verify/{id}` | Certificates are checkable by third parties, not just downloadable. Visible in the PDF itself. | ~80 lines. No secrets (no HMAC, Gate 1 Q3). |
| D2 | Proof layer only: SIGKILL test (built in M4) surfaced as a README transcript + 1,000-recipient benchmark | Turns "I handled crashes" into evidence. | Subprocess test is POSIX-only (skipped with a reason on Windows). |
| D3 | `?dry_run=true` + `POST /{id}/retry` | Closes the fix-and-retry loop that partial acceptance (ADR-005) creates. | ~60 lines. Retry must respect leases. |
| D4 | `Idempotency-Key` | Bulk POSTs are the ones that time out. | Concurrency test. ~50 lines. |
| + | `manifest.csv` in ZIP | Auditable offline result. ~20 lines, part of M5. | — |

## 6. Phase 4 adversarial audit log (2026-10-11)

A fresh-context reviewer saw only the PDF, `docs/`, `pyproject.toml` and the traceability test. Scores before fixes: spec 7, failure correctness 5, simplicity 4, tests 6, docs 6, explainability 6, differentiators 5. 28 findings + 12 cross-doc contradictions.

### Accepted and changed
| # | Sev | Finding | Change | Where |
|---|---|---|---|---|
| 1 | BLOCKER | Engine-wide `BEGIN IMMEDIATE` makes every read take the write lock | **Reproduced** (write failed with `database is locked`); fix measured (write in 0.001 s). Two engines: write = `BEGIN IMMEDIATE`, read = deferred `BEGIN`. No transaction during file streaming. | ADR-002, ADR-009, RESEARCH §6 |
| 2 | MAJOR | Fencing on `lease_owner` unsafe (shared/reused ids, `/retry` resets attempts) | Fence on a random **per-claim `lease_token`**. `worker_id` is uuid4 per thread, for logs only. New test `test_same_worker_reclaim_is_fenced`. | ADR-002, ADR-004 |
| 3 | MAJOR | Single `error_code` column can't hold all row errors / `field` | `items.errors` JSON list; items return `errors[]`. | ADR-002, ADR-005, ADR-009 |
| 4 | MAJOR | `NOT NULL` name/email turns `REQUIRED`/`INVALID_TYPE` into a 500 | Nullable columns for rejected rows. Tests through HTTP. | ADR-002, ADR-005 |
| 5 | MAJOR | `StrEnum` vs `requires-python >=3.10` | `>=3.11`. Phase 6 verifies 3.11 + 3.14 (3.10 isn't installable here to test). | pyproject, ADR-010 |
| 6 | MAJOR | Fixed tmp name races; orphan tmp files | Unique tmp per write + orphan sweep at worker start. | ADR-008, ADR-004 |
| 7 | MAJOR | Byte-identity assumed across processes/configs | Render inputs snapshotted on the job row (`print_email`, `verify_base_url`), never env. **Measured** identical across 3 processes / hash seeds. Wording downgraded to "equal content for equal inputs". | ADR-002, -004, -007, -013 |
| 8 | MAJOR | 202 body / dry-run contract undefined; dry-run could poison an Idempotency-Key | Bodies specified. Dry-run always 200, ignores the key, creates nothing. Test added. | ADR-006, ADR-009 |
| 9 | MAJOR | SIGKILL test fights the 30 s lease | Env-configurable lease. State-triggered kill. ≈ 5 s runtime. | ADR-004, ADR-012 |
| 10 | MAJOR | Batch lease margins wrong | **Claim one item at a time.** Margin text corrected. | ADR-004 |
| 12 | MINOR | `create_all` races under `--workers N` | DDL inside a `BEGIN IMMEDIATE` transaction. | ADR-002 |
| 13 | MINOR | Status derivation edge cases (`completed` despite rejects, regression to `queued`, ÷0) | New derivation table incl. `failed` status, rejects → `completed_with_errors`, `queued` needs attempts = 0, ÷0 guard. Test per row. | ADR-002 |
| 14 | MINOR | 5 MB body cap vs legal 10k worst-case rows | `MAX_BODY_BYTES` derived from worst legal row (≈ 26 MB). Boundary test. | ADR-011 |
| 15 | MINOR | 413 vs 422 contradiction | 413 only. | ADR-005 |
| 16 | MINOR | QR tangled into required scope | QR moves to M7 (D1). v1.0 prints certificate ID only. | ADR-007, -013, REQUIREMENTS A3 |
| 17 | MINOR | Offset pagination unstable with status filter | Keyset on `row_index` (`after`). | ADR-009 |
| 18 | MINOR | `issued_on` future check timezone-naive | Allow ≤ today UTC + 1 day. | ADR-005 |
| 19 | MINOR | `/retry` reset under-specified | Full `UPDATE` written out. | ADR-009 |
| 20 | MINOR | Failed item download says "not ready" | `409 CERTIFICATE_GENERATION_FAILED`. | ADR-009, ADR-010 |
| 21 | MINOR | Two-worker test can pass trivially | Assert each worker ≥ 1 claim; renderer delay forces interleaving. | ADR-004 |
| 22 | MINOR | Datetime string comparison in raw SQL | All timestamps INTEGER epoch ms. | ADR-002 |
| 23 | MINOR | FIFO starvation | Documented as v1 limitation + 100× fix. | ADR-004 |
| 24 | MINOR | Restarts burn attempts | Graceful shutdown finishes the current item. Test. | ADR-004 |
| 25 | NIT | Traceability scan ≠ proof | Wording: "every requirement is cited by ≥ 1 test". | ADR-012 |
| 26 | NIT | Over-claimed SQLAlchemy citation | **Confirmed** by re-fetch (docs emit plain `BEGIN`). Reworded as our modification. | RESEARCH, ADR-002 |
| 27 | NIT | IntegrityError race path is dead under IMMEDIATE | Stated as a backstop, not claimed tested. | ADR-006 |
| 28 | NIT | `jobs.updated_at` has no writer | Column dropped. | ADR-002 |
| Cut 1 | — | Batch claiming + jittered backoff | Both cut: single-item claim, fixed `RETRY_DELAY_SECONDS`. | ADR-004 |
| Contradictions 1–12 | — | tmp naming, 413/422, error shape, reads, test names, QR scope, retry classification, `/healthz`, README-command checking, lease margin, endpoint count, `validating` | All resolved. One flat error shape everywhere. A8 rewritten. `/healthz` body defined. README commands verified in Phase 6, not pytest. | multiple |

Also found while fixing (not raised by the auditor): ADR-010's flat `errors[]` and the new ADR-009 bodies briefly disagreed. Unified on the flat RFC 9457 shape.

### Disagreed with the auditor (kept as is), with reasons
| Finding | Auditor's position | Why we keep it |
|---|---|---|
| #11 / cut 3: email not printed by default; `PRINT_EMAIL_ON_CERTIFICATE` | Default build fails a literal spec sentence. Pick one behaviour. | **Your explicit Gate 1 A3 decision.** It's recorded as a binding interpretation (REQUIREMENTS A3), with a one-line way to the literal reading. The auditor's technical concern (config-dependent render bytes and validation, #7) is fixed by snapshotting the flag onto the job. **Flagged for you at Gate 2.** |
| Cut 2: drop D4 Idempotency-Key | Cost (hash rule, dead race path, dry-run interaction) vs a documentable failure mode. | You listed D4 explicitly. The dry-run interaction and dead-path wording are fixed. It remains last in build order (M10) and droppable under P1, so it can't cost required scope. |
| #16 (part): print no QR unless `PUBLIC_BASE_URL` is set | A localhost default bakes a dead link into certificates. | In the reviewer's local run the localhost link **works**, which is D1's demo value. The startup warning + README note cover real deployments, and the snapshot means a misconfiguration is visible per job. |
| Process cuts: WALKTHROUGH quiz, P2 "no code without R#/ADR" | Process theatre a reviewer reads as padding. | Your rules for the competition. They live in `docs/` and the interview prep, not in the product surface a reviewer runs. |
| #13 (part): a job with a dead worker shows `processing` forever | Lying status. | It's accurate: work remains and will resume when any worker runs. `/healthz` shows worker liveness. Inventing a `stalled` status would need a timeout heuristic. Documented. |


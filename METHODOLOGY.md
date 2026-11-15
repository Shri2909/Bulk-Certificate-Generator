# Methodology: how certgen was built and how to check it

This is the evidence file. Every claim below names a test, a file or a command. For what the system is and how to run it, see [README.md](README.md); for components and flows, see [ARCHITECTURE.md](ARCHITECTURE.md).

## 60-second version

| Claim | Evidence |
|-------|----------|
| Spec first | The assignment is transcribed in [docs/problem_statement.md](docs/problem_statement.md) and split into 25 atomic requirements (R1-R25) in [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md), with 10 binding interpretations of ambiguous wording (A1-A10). |
| Decisions recorded | 14 ADRs in [docs/decisions/](docs/decisions/), each with options, a decision and consequences. |
| Requirement traced to a test | Every test carries `@pytest.mark.req("R#")`; `tests/test_spec_traceability.py` fails if any R1-R25 has no test or a test cites an unknown ID. |
| Failure injected on purpose | Renderer, storage, lease clock, SIGKILL of a real worker process, low disk and damaged files are all injected (table below). |
| Measured, not guessed | `scripts/benchmark.py` writes JSON evidence; the README benchmark table is checked against it by `test_readme_numbers_match_evidence`. |
| Security-reviewed | Auth, Host allowlist, body and slot limits, slow-client deadlines, symlink refusal and per-token limits each have named regression tests. |

Result of the gates on this checkout: the full suite (473 tests) passes in about 22 s (about 15 s with `-m 'not slow'`); ruff, ruff format and `mypy --strict certgen` are clean (source only; `mypy --strict tests` reports 11 errors in 6 files, so the tests are not held to it).

## The workflow

```mermaid
flowchart LR
    P[Problem statement] --> R[Requirements R1-R25]
    R --> A[ADRs 001-014]
    A --> I[Implementation]
    I --> T[Tests tagged with R#]
    T --> H[Review and hardening]
    H --> B[Benchmark]
```

1. Transcribe the assignment and resolve each ambiguity once, in writing (A1-A10 in `docs/REQUIREMENTS.md`). Where this repo and the assignment disagree, the assignment wins.
2. Split it into atomic, testable requirements and add one heading per requirement.
3. Record each non-obvious choice as an ADR before writing the code that depends on it.
4. Implement in vertical slices: job workflow, validation and text fitting, PDF rendering and storage, worker recovery, pagination and ZIP, then optional features (dry run, retry, idempotency). `git log --oneline` shows this order.
5. Tag every test with the requirement it proves; the traceability test enforces coverage of R1-R25.
6. Review for security and robustness; each finding became a fix plus a regression test (`tests/test_hardening.py`, `tests/test_audit_regressions.py`), then measure with the benchmark.

## Traceability: requirement, decision, test

Machine-checked part: `test_every_requirement_has_a_test`, `test_no_test_cites_unknown_requirement`, `test_every_requirement_has_a_heading` in `tests/test_spec_traceability.py`. It is a static scan of decorators: it does not prove a test asserts the right thing. The ADR column is maintained by hand (each ADR header lists its requirements).

| Requirement group | IDs | ADR | Representative tests |
|-------------------|-----|-----|----------------------|
| Bulk submit, one job per request | R1, R14 | [009](docs/decisions/ADR-009-retrieval-api.md) | `test_create_job_returns_202_with_location_and_counts`, `test_job_and_items_are_atomic` (`test_jobs_api.py`) |
| Per-row validation | R2, R15 | [005](docs/decisions/ADR-005-validation.md) | `test_partial_acceptance_three_row_trace`, `test_duplicate_email_case_and_whitespace`, `test_cjk_unsupported` (`test_validation.py`) |
| One certificate per valid recipient | R3, R16 | [004](docs/decisions/ADR-004-worker-model-and-crash-recovery.md) | `test_drain_creates_one_pdf_per_valid_item_and_none_for_rejected` (`test_worker.py`) |
| Single template, recipient data on it | R4, R5 | [007](docs/decisions/ADR-007-pdf-library-and-template.md) | `test_pdf_text_contains_all_fields`, `test_email_printed_by_default_and_opt_out`, `test_render_is_byte_deterministic` (`test_rendering.py`) |
| Status and progress | R6, R7, R17 | [002](docs/decisions/ADR-002-database-and-schema.md), [009](docs/decisions/ADR-009-retrieval-api.md) | `test_derivation_table`, `test_no_regression_to_queued_during_retry_delay` (`test_job_status.py`) |
| Retrieval, single and ZIP | R8, R19 | [008](docs/decisions/ADR-008-file-storage.md), [009](docs/decisions/ADR-009-retrieval-api.md) | `test_download_succeeded_pdf`, `test_archive_contains_all_succeeded_pdfs`, `test_manifest_lists_every_row` (`test_retrieval.py`) |
| Failure isolation, who failed and why | R9, R10, R18 | [004](docs/decisions/ADR-004-worker-model-and-crash-recovery.md), [010](docs/decisions/ADR-010-error-model.md) | `test_one_failing_recipient_does_not_block_others`, `test_items_listing_identifies_failed_and_succeeded` (`test_failure_isolation.py`) |
| Many recipients: non-blocking, limits, pagination | R11 | [003](docs/decisions/ADR-003-sync-vs-background.md), [011](docs/decisions/ADR-011-limits.md) | `test_post_does_not_render_synchronously`, `test_items_keyset_pagination` (`test_jobs_api.py`), `test_too_many_recipients_422`, `test_global_capacity_503` (`test_limits.py`) |
| Sync-vs-background documented | R12 | [003](docs/decisions/ADR-003-sync-vs-background.md) | tagged tests in `test_docs.py` (the README must carry the reasoning); the reasoning itself is the ADR |
| Python, FastAPI, relational DB | R13 | [001](docs/decisions/ADR-001-framework.md), [002](docs/decisions/ADR-002-database-and-schema.md) | `test_stack_is_fastapi_and_sqlite` (`test_docs.py`), `test_pragmas_applied` (`test_db.py`) |
| README content | R20-R25 | none | `test_docs.py` (sections, links resolve, curl example accepted by the API) |

Not requirements, but tested features: idempotent submit ([006](docs/decisions/ADR-006-idempotency.md), `test_idempotency.py`), dry run (`test_dry_run.py`), retry of failed items (`test_retry.py`).

## Test strategy by layer

| Layer | What it proves | How it is isolated | Run |
|-------|----------------|--------------------|-----|
| Unit and component | Layout fitting, validation rules, job-status derivation, config parsing, schema constraints (`test_layout.py`, `test_validation.py`, `test_job_status.py`, `test_config.py`, `test_db.py`) | Pure functions or one temp SQLite file | `pytest tests/test_layout.py` |
| API in process | HTTP contract, error shapes, limits, retrieval, with the worker driven synchronously | Fresh `create_app(settings)` per test, `FakeClock`, embedded worker off, `drain` fixture (`tests/conftest.py`) | `pytest -m 'not slow'` |
| Worker with a fake clock | Lease expiry, fencing, retry delay, crash loop without sleeping | `FakeClock.advance(...)`; renderer injected as a dependency | `pytest tests/test_crash_recovery.py tests/test_worker.py` |
| Real processes (marked `slow`, 6 tests) | SIGKILL recovery, two worker processes, real server token file, SIGTERM shutdown, concurrent schema creation | Subprocesses with shortened lease through env vars; the kill is state-triggered (polls the DB), not timed | `pytest -m slow` |
| Static docs checks | README sections, links and anchors, curl example, benchmark table versus fixtures | `tests/test_docs.py` | in the default run |
| Live demo, end to end | Real HTTP, ZIP and manifest, real worker kill, retry, replay | Temp data dir, owned processes cleaned up | `.venv/bin/python scripts/check_live_demo.py` |

Counts (`pytest --collect-only -q`): 473 tests in 23 files. Largest files: `test_audit_regressions.py` 80, `test_validation.py` 76, `test_hardening.py` 71, `test_jobs_api.py` 29. `-m slow` selects 7; the rest run in about 15 s. There are no property-based tests (no `hypothesis` dependency); table-driven `parametrize` is used for boundaries (name 120/121 characters, host variants, token header variants). Coverage is not measured.

## Failure injection

| Failure | How injected | Observed recovery | Test |
|---------|--------------|-------------------|------|
| One recipient's render raises | `failing_for({...})` renderer in `tests/conftest.py` | Other items succeed; the item ends `failed` with a code and no traceback text | `test_one_failing_recipient_does_not_block_others`, `test_failed_error_message_has_no_traceback_or_exception_text` |
| Transient render error | Renderer fails N times, then succeeds | Retried after the delay, then `succeeded` | `test_transient_failure_then_success`, `test_retry_after_transient_failure_needs_delay` |
| Storage write error; PDF over the size cap | `Storage.save_atomic` patched to raise for one item; oversized bytes | Isolated to that item | `test_storage_error_is_isolated`, `test_oversized_pdf_is_item_failure` |
| Worker dies, lease expires | Fake clock advanced past the lease | Item reclaimed by another claim | `test_expired_lease_is_reclaimed` |
| Stale worker writes after reclaim | Old lease token used for the done write | Write rejected by the fencing token | `test_stale_worker_done_write_rejected_by_fencing` |
| Crash loop on one item | Three claims, clock advanced each time | `failed` with `WORKER_LOST` after 3 attempts | `test_crash_loop_ends_failed_after_max_attempts` |
| SIGKILL of a real worker mid-render | State-triggered `SIGKILL`, then a second worker | All 6 items succeed, killed item has 2 attempts, exactly 6 PDFs, no `.tmp` | `test_kill_mid_job_no_duplicates` (slow) |
| Concurrent claimers | Two processes; many threads | Each item processed exactly once | `test_two_worker_processes_never_double_process`, `test_many_threads_claim_each_item_exactly_once` |
| SIGTERM; dead worker thread; DB error in loop | Signal; killed thread; failing DB call | Shutdown costs no attempt; monitor restarts thread; loop survives | `test_graceful_shutdown_costs_no_attempts`, `test_monitor_restarts_dead_thread`, `test_loop_survives_db_error` |
| Disk error during a PDF write; stale `.tmp` from a killed worker | `os.fsync` raises `OSError("disk full")`; temp files of different ages | Previous complete file untouched, no temp left; orphans swept by age | `test_atomic_write_no_partial_file`, `test_orphan_tmp_sweep_age_rule` |
| Certificate file damaged or missing | Six damage variants (truncated, empty, no header or trailer, oversized) or file removed | `410`, archive marks and skips it | `test_damaged_certificate_file_is_410`, `test_archive_marks_a_damaged_file_and_leaves_it_out` |
| Low free disk | Patched free-space reading | New jobs and retries refused, nothing stored | `test_new_jobs_are_refused_when_free_disk_is_low`, `test_retry_is_refused_when_free_disk_is_low` |
| Client retries a POST | Same key, concurrent same key | One job | `test_replay_returns_same_job`, `test_concurrent_same_key_creates_one_job` |
| Database locked; internal error | Operational error injected | `503` / problem+json without traceback | `test_db_locked_maps_to_503`, `test_internal_error_is_problem_json_without_traceback` |

## Quality gates and commands

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy --strict certgen
.venv/bin/python -m pytest -q                 # all layers, 450+ tests
.venv/bin/python -m pytest -q -m 'not slow'   # everything except the 7 slow tests
.venv/bin/python scripts/check_live_demo.py   # end-to-end, includes a real worker kill
```

All of the above were run on this checkout and passed. `pyproject.toml` sets `--strict-markers` (a mistyped `req` marker is an error), a 60 s per-test timeout, and ruff rules `E,F,W,I,B,UP,SIM,RUF`. Pinned tool versions are in `requirements-dev.txt`. There is no CI workflow in the repository; the gates are run locally.

## Security and hardening review

| Control | Tests |
|---------|-------|
| Bearer-token auth on `/v1`, refused before the body is read; token file created once | `test_v1_routes_require_a_valid_bearer_token`, `test_unauthenticated_request_is_refused_before_its_body_is_read`, `test_real_server_creates_and_enforces_its_token_file` |
| Idempotency keys scoped per token | `test_idempotency_keys_are_scoped_per_token` |
| Host allowlist (DNS-rebinding defence), checked before the body | `test_foreign_hosts_are_refused_with_problem_json`, `test_host_is_checked_before_the_body_is_read`, `test_missing_host_header_is_refused` |
| Request limits: body size, recipient count, concurrent requests and submissions | `test_body_over_limit_413`, `test_chunked_body_over_limit_413`, `test_too_many_recipients_422`, `test_request_limit_admits_64_and_refuses_the_65th_with_problem_json`, `test_at_most_two_submissions_are_processed_at_once` |
| Slow-client deadlines on request and response | `test_body_that_does_not_arrive_in_time_is_408`, `test_a_response_the_client_does_not_read_is_aborted_and_its_slot_released`, `test_a_streamed_download_the_client_does_not_read_is_aborted` |
| One token cannot hold every request slot | `test_one_token_cannot_hold_every_request_slot`, `test_token_slot_is_released_when_the_app_raises` |
| Path safety: server-generated names, traversal ids, symlinks, private files | `test_path_traversal_id_404`, `test_symlinked_certificate_refused`, `test_symlinked_file_is_refused_even_without_o_nofollow`, `test_symlinked_job_dir_is_refused_on_write`, `test_created_files_are_private` |
| Input robustness: lone surrogates, oversized emails, unknown-field error amplification, NaN, deep nesting | `test_lone_surrogate_never_500`, `test_email_validator_never_sees_an_email_longer_than_254`, `test_worst_case_unknown_field_body_is_not_amplified`, `test_nan_and_infinity_422`, `test_nesting_depth_422` |
| CSV formula injection in `manifest.csv` | `test_manifest_neutralizes_formula_cells` |
| No leakage of tracebacks, SQL parameters or exception text | `test_sql_errors_do_not_carry_parameters`, `test_failure_log_has_no_exception_text` |
| Production invariants not left to `assert` (removed under `python -O`) | `test_runtime_checks_are_not_assert_statements` |

Accepted risks: one trust level per token (any valid token can read any job), no TLS in the app (terminate it at a proxy and add that host to `CERTGEN_ALLOWED_HOSTS`), no retention or cleanup of files. See [ADR-011](docs/decisions/ADR-011-limits.md) for the limits and their rationale.

## Performance

```bash
.venv/bin/python scripts/benchmark.py --recipients 1000 --processes 4 --output /tmp/bench-4p.json
```

Flags: `--recipients` (default 1000), `--processes` (default 1), `--output` (required), `--work-dir` (default `./data`). Phase A times each item through one in-process worker; phase B drains the same corpus with N worker processes. The script exits non-zero if an acceptance check fails (all items succeeded, PDF under the size cap, slowest item under 10% of the 30 s lease).

Committed evidence in `tests/fixtures/benchmarks/` (1,000 recipients, Linux 7.0.0-38 x86_64, Python 3.11.9, SQLite 3.46.1):

| Processes | Wall time | Throughput | Per-item p50 / p95 | Peak RSS |
|-----------|-----------|------------|--------------------|----------|
| 1 | 11.95 s | 83.7 certificates/s | 10.8 / 13.6 ms (render p50 4.2 ms) | 88.6 MB |
| 2 | 7.18 s | 139.3 certificates/s | not compared | 88.1 MB |
| 4 | 5.97 s | 167.6 certificates/s | not compared | 88.2 MB |

POST latency for the 1,000-recipient request was about 113-119 ms in all three runs. Re-running the same command on this checkout (10-thread laptop CPU, scratch directory, uncommitted tree) gave 198 certificates/s with 1 process and 430 with 4, so absolute numbers vary by about 2x with disk and machine; compare scaling, not absolutes.

What it does not prove: behaviour with a real PostgreSQL, a network filesystem or concurrent API load during generation, jobs larger than 1,000 recipients, or the effect of fsync on other storage. The three fixtures were produced at two different commits and the CPU model is not recorded in them.

## Decision records

Nygard-style: status, options, decision, consequences. Superseding is preferred to rewriting history.

| ADR | Decision |
|-----|----------|
| [001](docs/decisions/ADR-001-framework.md) | FastAPI |
| [002](docs/decisions/ADR-002-database-and-schema.md) | SQLite (WAL) via SQLAlchemy, two tables |
| [003](docs/decisions/ADR-003-sync-vs-background.md) | Background generation, DB-backed queue, embedded worker |
| [004](docs/decisions/ADR-004-worker-model-and-crash-recovery.md) | Single-item leased claims, fencing token, attempt cap |
| [005](docs/decisions/ADR-005-validation.md) | Structural errors reject the request; recipient errors are per row |
| [006](docs/decisions/ADR-006-idempotency.md) | Optional `Idempotency-Key` header |
| [007](docs/decisions/ADR-007-pdf-library-and-template.md) | ReportLab canvas, template as code, bundled DejaVu fonts |
| [008](docs/decisions/ADR-008-file-storage.md) | Local directory, server-generated names, atomic writes |
| [009](docs/decisions/ADR-009-retrieval-api.md) | Submission, status and retrieval API surface |
| [010](docs/decisions/ADR-010-error-model.md) | RFC 9457 problem+json with stable codes |
| [011](docs/decisions/ADR-011-limits.md) | Explicit limits |
| [012](docs/decisions/ADR-012-test-strategy.md) | Deterministic, no sleeps, requirement-tagged tests |
| [013](docs/decisions/ADR-013-certificate-identity-and-verification.md) | Unguessable ids now, public verification deferred |
| [014](docs/decisions/ADR-014-metrics.md) | Metrics: gauges read from the database, worker timings per process |

## Known gaps

- No lease renewal: an item stalled longer than the lease is re-run, and after 3 expiries ends `WORKER_LOST`; with no workers running, an expired item stays `processing`.
- No offline or public verification of a certificate and no QR code (ADR-013).
- Single node: SQLite has one writer on local disk; PostgreSQL with `SKIP LOCKED` is the scale-out path and would need its own CI leg (ADR-012).
- At-least-once rendering: after a crash an item may render again with identical bytes (`test_render_is_byte_deterministic`); exactly-once is not claimed.
- Latin, Greek and Cyrillic names only; other scripts are rejected as `UNSUPPORTED_CHARACTERS`.
- Not measured: code coverage, mutation score, load with concurrent API traffic. No CI workflow; no property-based tests.
- PDF checks cover extracted text and layout bounds, not human visual review. Crash tests are skipped on Windows (no SIGKILL).

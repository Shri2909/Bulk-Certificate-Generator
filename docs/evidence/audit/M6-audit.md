# M6 fresh-context audit (2026-10-11)

Auditor: a fresh-context subagent given only `docs/problem_statement.pdf`, `README.md` and the repository
(told not to open `docs/BUILD_LOG.md` or `docs/evidence/gates/`). Worked in its own clean clone at HEAD 1c6b5a5,
followed the README verbatim, and attacked loss, duplication, setup and PDF conformance.

## Scores (before fixes)

| Dimension | Score | Auditor's justification |
|---|---|---|
| Spec conformance | 9 | Every PDF sentence, Testing and Documentation bullet covered; gap: transient failures become permanent with no retry |
| Failure correctness | 7 | 15 worker SIGKILLs + 3 API SIGKILLs: no loss, no duplicate; but short disk outage fails items permanently, over-lease renders end as WORKER_LOST, some valid JSON → 500 |
| Simplicity | 8 | One process, DB queue, clear claim/lease/fence; some reserved columns/settings for unbuilt features |
| Tests | 8 | 291 tests, real assertions; one concurrency test missed a realistic mutant |
| Docs | 8 | Thorough; every named test exists; a few wrong/unproven claims |
| Explainability | 8 | Small, commented modules; async lifespan adapter, anyio.from_thread and body middleware need rehearsal |
| Differentiators | 5 | No optional features (as documented); extras: ZIP+manifest, CSV guard, problem+json, crash recovery, standalone workers, benchmark, traceability |

## Findings and disposition

| # | Severity | Finding | Disposition |
|---|---|---|---|
| 1 | major | Renders/stalls longer than the lease are re-run and can end `WORKER_LOST` while workers are healthy (no lease renewal) | **Documented, not changed.** Heartbeats are rejected by BLUEPRINT §7.3/§20/§23 (frozen); adding one is not a pre-approved amendment. README Limitations and Recovery now state the consequence and the mitigation (`CERTGEN_LEASE_SECONDS`). Benchmark max item stays far under 10% of the lease. |
| 2 | major | A storage outage longer than the fixed retry budget (2 s × 3) fails items permanently; no retry endpoint | **Documented.** Retry policy is frozen (§23 "Fixed 2 s"); `POST …/retry` is differentiator D3 (M9), which must not exist at `v1.0-required` (§24.17). README Limitations states it. |
| 3 | major | SQLAlchemy errors logged with bound parameters (names/emails), contradicting README | **Fixed:** `create_engine(..., hide_parameters=True)`; test `test_db.py::test_sql_errors_do_not_carry_parameters`; README Operations wording updated. |
| 4 | major | `test_two_concurrent_workers_never_double_process` passes on a mutant that runs SELECT and UPDATE in two write transactions | **Fixed:** added `test_worker.py::test_many_threads_claim_each_item_exactly_once` (8 threads × 400 items); mutation 5b recorded in `docs/evidence/mutation-checks.txt`, killed 5/5 runs (item claimed twice, attempts 2). Production SQL unchanged (normative §7.3). |
| 5 | minor | `1e400` / > 4300-digit integers → 500 | **Fixed:** strict decoder rejects non-finite floats and over-long integers as `INVALID_JSON`; test `test_validation.py::test_out_of_range_numbers_422`. |
| 6 | minor | `CERTGEN_PUBLIC_BASE_URL` described as printing links that are not printed; warning logged twice | **Fixed (docs + code):** README says the URL is a per-job snapshot and no link is printed in this version; `serve()` reuses the import-time settings (one parse, one warning). The column/setting are required by §4/§15 and stay. |
| 7 | minor | Quick start `python -m venv` fails where `python` is not ≥ 3.11 | **Fixed:** `python3 -m venv .venv  # any Python >= 3.11 (python3.11; Windows: py -3.11)`. |
| 8 | nit | README example error message differs from email-validator 2.3 text | **Fixed** (message copied from the real response). |
| 9 | nit | "throughput scales with worker processes" unproven; benchmark recorded dirty on an older commit | **Fixed:** wording now "expected to scale … only the single-process run is measured in this version"; benchmark re-run on clean commit 6dd3010 (`git_dirty: false`). 2-/4-process evidence is D2 (M8). |
| 10 | nit | "Tests run without sleeping"; validation comment says W; `/healthz` 0/0 ok with standalone workers | **Fixed:** README test wording, validation comment, `/healthz` limitation documented. |
| 11 | suspected | Archive built fully before streaming; body buffering | **Documented** in README Limitations (spills to disk above 8 MiB; 2 concurrent builds). |
| 12 | suspected | Per-process `MAX_ATTEMPTS`/lease could disagree | **Documented** in README Operations ("run every process with the same settings"). |

What held up (auditor): README verbatim in a clean clone (291 passed, lint/type clean); 800/800 under 15 random worker SIGKILLs; 300/300 under 3 API SIGKILLs; graceful SIGTERM; 10,000-recipient POST in ~1.2 s; 8×400 stress on original code all attempts=1; path traversal, 405/415/413/422 shapes; CSV guard; 3000-case fuzz: validation and rendering never disagree; BLUEPRINT sha256 matches.

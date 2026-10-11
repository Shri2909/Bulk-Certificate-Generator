CURRENT MILESTONE: M0 | STATUS: gate | NEXT ACTION: Freeze docs/BLUEPRINT.md (sha256 + HEAD into docs/BLUEPRINT.sha256), commit, tag blueprint-locked, then start M1 pin resolution in a clean venv.

# Build log (Phase 5)

Build state for the autonomous build of `docs/BLUEPRINT.md`. Authority: PDF > BLUEPRINT §24 > BLUEPRINT §§0–23 > Phase 5 prompt.

## Environment

| Item | Value |
|---|---|
| OS | Linux 7.0.0-38-generic x86_64 (Ubuntu). POSIX signals available: kill-test proof **can** be demonstrated (no Windows skip applies). |
| Python | 3.11.9 (pyenv, primary), 3.14 (`/usr/bin/python3.14`, newest CPython for M11) |
| SQLite runtime | 3.46.1 (≥ 3.35 required) |
| Rasterizer for visual check | `pdftoppm` (poppler, system package; outside the project venv, not in the repo) |
| Gate 3 approval | Explicit, in the Phase 5 prompt, for all five §0 decisions (2026-10-11) |

## Milestones

| M | Scope | Status | Tag |
|---|---|---|---|
| M0 | Freeze | in progress | `blueprint-locked` |
| M1 | Pins + walking skeleton | pending | `m1-done` |
| M2 | Validation, fonts, fit_text, errors, middleware, limits | pending | `m2-done` |
| M3 | Real template, print_email snapshot, determinism, visual check | pending | `m3-done` |
| M4 | Worker hardening, status, healthz, monitor, standalone worker, mutation proofs | pending | `m4-done` |
| M5 | Listing, downloads, archive | pending | `m5-done` |
| M6 | README, docs tests, traceability, benchmark-1p, audit | pending | `m6-done`, `v1.0-required` |
| M7 | D1 verify | pending | `m7-done` |
| M8 | D2 proof | pending | `m8-done` |
| M9 | D3 dry-run + retry | pending | `m9-done` |
| M10 | D4 Idempotency-Key | pending | `m10-done` |
| M11 | Final verification, audit, walkthrough | pending | `v1.0-rc` |

## Effective rules: where §24 supersedes earlier text

| Earlier text | Superseded by | Rule that applies |
|---|---|---|
| §6.1 bullet 1 (FastAPI/Pydantic parses the body; wrong Content-Type → 422) | §24.5 | `POST /v1/certificate-jobs` reads the raw body itself: Content-Type must be `application/json` (optional `charset=utf-8`, case-insensitive) else **415 `UNSUPPORTED_MEDIA_TYPE`**; strict UTF-8; `json.loads` with duplicate-key and NaN/Infinity rejection; depth > 8 rejected; all → 422 `INVALID_JSON`; then `CreateJobRequest.model_validate`, Pydantic locs treated as prefixed with `body`. |
| §7.1 step 2 | §24.5 | Same as above. |
| §6.9 invalid verify shape `{"valid": false}` | §24.7 | Every verify response carries `"verification_scope": "issued_record_only"`; invalid → `{"valid": false, "verification_scope": "issued_record_only"}` (D1). |
| §12 limits table | §24.1, §24.3, §24.12 | Adds `CERTGEN_MAX_ACTIVE_ITEMS` (default 50,000) global capacity → 503 `CAPACITY_EXCEEDED` + `Retry-After: 1`; `MAX_PDF_BYTES = 2_097_152` (oversize render = item failure); uvicorn `limit_concurrency=64`, `timeout_keep_alive=5`. |
| §15 config table | §24.1, §24.6 | Adds `CERTGEN_MAX_ACTIVE_ITEMS` (`MAX_RECIPIENTS`–1,000,000, ValueError if below `MAX_RECIPIENTS`). `CERTGEN_PUBLIC_BASE_URL` rules tightened: ASCII, ≤ 100 chars after stripping one trailing `/`, absolute http(s) with host, no userinfo/query/fragment, path prefix allowed, `http` only for `localhost`/`127.0.0.1`/`::1`. |
| §16 case lists | §24.1–§24.15 | New cases: `test_limits::test_global_capacity_503`, `::test_capacity_check_is_atomic_under_concurrent_posts`, `::test_wrong_media_type_415`; `test_worker::test_monitor_restarts_dead_thread`, `::test_restart_budget_is_bounded`, `::test_monitor_retires_exhausted_lease_without_claims`, `::test_created_files_are_private`; `test_jobs_api::test_healthz_degraded`, `::test_original_request_is_stored_canonically`, `::test_id_collision_is_retried`; `test_failure_isolation::test_oversized_pdf_is_item_failure`; `test_validation::test_duplicate_json_keys_422`, `::test_nan_and_infinity_422`, `::test_nesting_depth_422`, `::test_invalid_utf8_422`, `::test_error_order_is_stable`; `test_config.py` (new file) `::test_public_base_url_rules` + every §15 range; `test_retrieval::test_symlinked_certificate_refused`; `test_db::test_unknown_schema_version_refused`, `::test_query_plans_use_indexes`; `test_runtime.py` (new file) `::test_server_limits_passed_to_uvicorn` + `python -m certgen` entry point; `test_docs::test_readme_curl_example_is_valid`; D-only: `test_retry::test_retry_respects_capacity` (D3), `test_verify::test_scope_field_present` (D1), `test_idempotency::test_hash_matches_stored_request_json` (D4). |
| §21 K13 (expired item waits for a worker's next claim) | §24.2 | Embedded mode runs a monitor thread every 1 s: restarts dead worker threads (≤ 3 per slot per rolling 60 s, backoff 0.25/0.5/1 s, then `worker_slot_exhausted`) and runs the T8 retire step in its own short write transaction. `/healthz` → `"status": "degraded"` (HTTP 200) when alive < expected. |
| §4 DDL | §24.4, §24.9 | `jobs.request_json TEXT NOT NULL CHECK (json_valid(request_json))`, `items.input_json TEXT NOT NULL CHECK (json_valid(input_json))`, canonical JSON (`sort_keys`, `(",",":")`, `ensure_ascii=False`), never returned, immutable. Create transaction sets `PRAGMA user_version = 1`; startup: 0 + no tables → create, 1 → continue, other → refuse. |
| §13 / §7.1 step 3 `request_hash` canonicalization | §24.4 | D4 `request_hash = sha256(request_json)`. |
| §6.1 ProblemCode list | §24 preamble | Adds `UNSUPPORTED_MEDIA_TYPE` 415 and `CAPACITY_EXCEEDED` 503 (`Retry-After: 1`). |
| §11.2 file writes | §24.8 | `umask(0o077)` at startup, dirs 0700, files 0600; DATA_DIR resolved to an absolute canonical path once; `os.lstat` symlink checks on job dir and file, resolved path must stay inside `{DATA_DIR}/certificates`, `O_NOFOLLOW` where available. |
| §10.2/§9 error ordering (implicit) | §24.11 | Request-level errors first, then rows by ascending `row_index`; within a row `name`, `email`, unknown keys sorted; `DUPLICATE_RECIPIENT` only on otherwise error-free rows. |
| §15 run line | §24.12 | `python -m certgen` → `uvicorn.run(..., limit_concurrency=64, timeout_keep_alive=5)`. |
| §17 M6/M8 benchmark fields | §24.14 | 10 warm-ups; fixed corpus (names cycle `Zoë Martin`, `Νίκος Παπαδόπουλος`, `Екатерина Иванова`; emails `recipient0000@example.org`…); extra fields `render_ms`, `storage_ms`, `peak_rss_bytes`; acceptance: all succeeded, every PDF ≤ `MAX_PDF_BYTES`, max per-item < 10% of lease. |
| §17 M6 README heading list | §24.15 | Adds `## Supported names`, `## Configuration`, `## Benchmarks`, `## Recovery and guarantees`, `## Operations`; first-screen quickstart with one complete curl; lost-response note. New test `test_docs::test_readme_curl_example_is_valid`. |
| §1 / existing traceability test | §24.16 | `docs/REQUIREMENTS.md` gets one `### R<n> — <obligation>` heading per §1 row; the traceability guard also fails on an empty marker. |
| §17 tagging | §24.17 | At `v1.0-required` no D1–D4 code, dependency, endpoint, test file or README section exists (checked with `git ls-tree`); later tags must have `v1.0-required` as ancestor. |

## Contradictions and gaps found, and how they are resolved

| # | Where | Issue | Resolution (rule used) |
|---|---|---|---|
| G1 | §0 header "Gate 3 candidate. Not approved, not frozen." | The file is frozen unchanged, so the header keeps that wording. | Approval is recorded in the Phase 5 prompt and in this log; `docs/BLUEPRINT.sha256` + tag `blueprint-locked` are the freeze evidence. The blueprint is not edited (prompt rule). |
| G2 | §17 M3 DoD "visual check … shown to the user and approved as legible" vs. the prompt's autonomous AUTOMATED VISUAL CHECK | The build cannot wait for a human. | Run the automated check (rasterize + inspect) and record verdicts; human approval of the M3 DoD is reported as **pending human review** in the final report. |
| G3 | §17 M11 "human walkthrough + one live modification", tag `v1.0` vs. prompt "tag `v1.0-rc`; do NOT tag `v1.0`" | Human-only steps. | Tag `v1.0-rc`; the user performs the live modification and tags `v1.0`. WALKTHROUGH.md is still written. |
| G4 | §16 lists D-marked cases inside required files: `test_limits::test_idempotency_key_too_long_422` (D4), `test_retry::test_retry_respects_capacity` (D3), `test_idempotency::test_hash_matches_stored_request_json` (D4) | §24.17 forbids D code/tests at `v1.0-required`. | §24.17 wins: these cases are written in their D milestone (M9/M10), not earlier. |
| G5 | §2 "No dependency is added" vs. `requirements.txt` = full `pip freeze` incl. transitive pins | Invariant grep "no dependency not in §2" would flag transitive pins. | §2 explicitly mandates a full freeze, so transitive pins of §2 packages are allowed. The grep checks that every **top-level** requirement (`pip`'s `Required-by` empty) is a §2 package. |
| G6 | §3 "Middleware is the only async code" / prompt invariant "`async def` only in certgen/middleware.py" vs. §3 `main.py` owning `lifespan` (Starlette expects an async context manager) | Writing `lifespan` as `async def` in main.py would break the invariant. | `main.py` owns a **sync** `contextlib.contextmanager` lifespan (start/stop workers and monitor); `middleware.py` provides the single async adapter that runs it. Simplest reading that keeps both rules; no new file. |
| G7 | `docs/REQUIREMENTS.md` A2/A3 say email is **not printed** by default and the traceability table names old paths (`certgen/api/jobs.py`, `schemas.py`) | REQUIREMENTS.md is below the blueprint; §0 decision 1 (approved) prints email by default. | Update REQUIREMENTS.md A2/A3 and the §6 traceability table to the blueprint's files and decision (done incrementally per milestone). |
| G8 | §24.16 requires the traceability guard to fail on empty markers; the current `tests/test_spec_traceability.py` does not | Prompt: never touch it "to pass a gate". | Strengthening it to the §24.16 rule is a spec requirement at M6, not a gate workaround; it only adds a failing condition. Logged at M6. |
| G9 | §16 D-test cases and new test files (`test_config.py`, `test_runtime.py`) carry no R# in the blueprint, but §16.1 requires a `req` marker on every test | Gap. | Use the closest existing R#: config/runtime → R11/R13; D1 verify → R5/R8; D3 dry-run → R2, retry → R9/R10; D4 → R1/R11. No new R# is invented. |
| G10 | §6.5 invalid query params → `field` = param name, but §6.1's mapping only defines locs after `body` | Gap. | For `query` locs, `field` = the parameter name, code `INVALID_VALUE` for any non-missing type, `row_index` null. |
| G11 | §16 M1 skeleton uses a "placeholder renderer" then M3 replaces it | None; noted. | M1 renders a minimal ReportLab PDF via the same `render_certificate` signature; M3 replaces the body. |
| G12 | §6.10 `/healthz` with embedded workers off | Gap: expected/alive both 0. | Reports `0/0` and `"status": "ok"`. |
| G13 | §24.8 umask "at startup" in tests that call `create_app` in-process | Process-wide umask affects pytest's tmp dirs. | `lifespan` and `worker.main()` set the umask; `storage.py` additionally creates dirs with mode 0700 and files with `os.open(..., 0o600)` so permissions do not depend on who set the umask. |

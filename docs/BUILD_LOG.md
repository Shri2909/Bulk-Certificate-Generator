CURRENT MILESTONE: M6 | STATUS: red | NEXT ACTION: Write test_docs.py + test_errors::test_error_codes_listed_in_readme + §24.16 traceability strengthening (RED), then scripts/benchmark.py, benchmark-1p.json, README.md, REQUIREMENTS.md R-headings; then M6 audit and tag v1.0-required.

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
| M0 | Freeze | done (sha256 93537bde…, inputs commit cd7d461) | `blueprint-locked` |
| M1 | Pins + walking skeleton | done — 64 passed; gate PASS (`docs/evidence/gates/M1.txt`) | `m1-done` |
| M2 | Validation, fonts, fit_text, errors, middleware, limits | done — 169 passed; gate PASS (`docs/evidence/gates/M2.txt`) | `m2-done` |
| M3 | Real template, print_email snapshot, determinism, visual check | done — 184 passed; gate PASS (`docs/evidence/gates/M3.txt`); visual check PASS (automated) | `m3-done` |
| M4 | Worker hardening, status, healthz, monitor, standalone worker, mutation proofs | done — 250 passed; gate PASS; flake rule 3/3; 9/9 mutations killed (`docs/evidence/gates/M4.txt`, `docs/evidence/mutation-checks.txt`) | `m4-done` |
| M5 | Listing, downloads, archive | done — 279 passed; gate PASS; flake 3/3 (`docs/evidence/gates/M5.txt`) | `m5-done` |
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
| G14 | §24.10 "regenerates the colliding ID" with a bulk insert of many items | SQLite's IntegrityError does not say which row collided. | Regenerate every certificate ID of the batch and retry the savepoint (≤ 5 times). No ID has been exposed yet, so this is equivalent and needs no per-row lookup. |
| G15 | §6.3 "422 with every request-level and row-level error" vs. Pydantic structural failures | When the envelope itself is malformed (missing/mistyped/unknown top-level field, non-object row) there is no model to validate rows against. | Structural (Pydantic) failure → 422 with the Pydantic errors only. Domain request-level errors (title/issuer/issued_on/recipients count) → 422 with request errors first, then every row error (§24.11). TOO_MANY_RECIPIENTS skips row validation per §9.1. |
| G16 | §2/§9 "allow_smtputf8=False (ASCII only)" | email-validator 2.3.0 still accepts internationalized **domains** (e.g. `zoe@exämple.com`) with `allow_smtputf8=False`; the blueprint's premise that emails are ASCII is then false. | Explicit `email.isascii()` check before `validate_email` → `INVALID_EMAIL` ("The email address must be ASCII only."). Implements the blueprint's stated rule; no new code or field. |
| G17 | §10.3 "widest printable ASCII glyph in DejaVu Sans (`W`, ≈ 0.99 em)" | Measured: `@` is widest (1.000 em), `W` 0.989 em. | Conclusion unaffected: 254 × `@` (wider than any valid address, which has one `@`) still fits at 8 pt in 3 lines. `test_worst_case_email_fits` checks both 254 × `W` (blueprint case) and 254 × widest glyph. No amendment needed. |
| G18 | §3 "worker.py / service.py must NOT import FastAPI" vs. `FieldCode`/`FieldError` living in `errors.py`, which also holds the FastAPI exception handlers | Gap. | Neither module imports FastAPI directly; they import codes from `errors.py`, which pulls FastAPI in transitively. Accepted (no new file); the standalone worker only pays import cost. |
| G19 | §4 "PRAGMA journal_mode=WAL on every connection" + `busy_timeout` | SQLite returns SQLITE_BUSY for a journal-mode change without calling the busy handler when another connection is mid-switch; concurrent first boots of N processes then fail (`test_concurrent_schema_creation_two_processes` flaked ~1/4; raw sqlite3 harness: 39 failures in 60 rounds × 6 processes). | Root-cause fix in `db._enable_wal`: skip the switch when the file is already WAL (persistent), else retry that one statement within the same 5 s bound busy_timeout gives every other lock. Harness after fix: 0 failures in 360 concurrent boots. Not a test retry. |
| G20 | §24.2 "at most 3 restarts per slot per rolling 60 s … after the budget is spent the slot stays down" | Ambiguous whether "stays down" is permanent. | Permanent: once a slot needs a 4th restart within 60 s of its previous 3, it is marked exhausted and never restarted (`/healthz` then reports degraded). Conservative; a crash-looping thread cannot spin. |
| G13 | §24.8 umask "at startup" in tests that call `create_app` in-process | Process-wide umask affects pytest's tmp dirs. | `lifespan` and `worker.main()` set the umask; `storage.py` additionally creates dirs with mode 0700 and files with `os.open(..., 0o600)` so permissions do not depend on who set the umask. |

## Milestone notes

### M1 (2026-10-11)
- Pins: all 13 §2 pins resolved together on CPython 3.11.9 (`pip install --dry-run` first, then the real install). No amendment. `requirements.txt` = freeze of the runtime rows (20 lines incl. transitive); `requirements-dev.txt` = `-r requirements.txt` + dev freeze.
- Tooling: `pythonpath = ["."]` in pytest config so `certgen` imports without installing the package. Ruff excludes `docs/` (frozen specs with illustrative code) and, until M6, `tests/test_spec_traceability.py` (pre-existing file, unformatted; §24.16 changes it at M6).
- Full suite at M1–M5 gates deselects `tests/test_spec_traceability.py`: §17 says traceability turns green at M6.
- `test_jobs_api::test_created_items_have_ids_only_when_valid` needs rejected rows, i.e. M2 validation; it is written in M2.
- Built ahead of its own test milestone because the M1 skeleton needs it: `status.derive_job_status` (full §5.2 table; tested in M4 `test_job_status.py`) and `storage.save_atomic` (tmp + fsync + replace; tested in M4). Their M4 RED runs will therefore be partly green; recorded here so that is not mistaken for a weak test. Mutation proofs at M4 cover both.
- Sanity mutation (not in the official list): counting active items in a read transaction outside the write lock makes `test_capacity_check_is_atomic_under_concurrent_posts` fail (verified, file restored).

### M2 (2026-10-11)
- Fonts vendored from the system `fonts-dejavu-core` package; sha256 of both TTFs equals §14 exactly. License text copied from the package's copyright file into `certgen/assets/LICENSE-DejaVu.txt`.
- §10.3 probe outcomes depend on the exact string; `test_layout.py` uses realistic strings of the named lengths that reproduce every blueprint outcome (25→40/1, 61→28/2, 120→16/2, 120×W→None, 85→24/2, 200 title→16/3, 200 issuer→13/2, 48-char email→13/1).
- `fit_text` measures each word once at 1 pt and scales (TTF widths are linear, no kerning). 10,000-row validation measured at 864 ms on this laptop (probe said 534 ms); routes are sync `def`, so the event loop is not blocked (K7).
- `test_errors::test_error_codes_listed_in_readme` needs README `## Error codes`; written in M6 with the README.
- The Write tool decoded `\uXXXX` escapes in test sources into literal invisible characters; they were converted back to escapes so tests are readable and ruff RUF001 passes.
- RED: 77 failed + 1 collection error (missing `certgen.fonts`), 32 already passing (Pydantic structural cases inherited from M1).

### M3 (2026-10-11)
- Template drawn exactly per §10.2 coordinates; every variable block goes through `layout.fit_text`; the renderer raises `ValueError` rather than overflow if a block cannot fit (validation prevents this).
- §24.8: `os.umask(0o077)` + explicit 0700/0600 modes; `storage.read()`/`exists()` refuse symlinked job dirs/files (lstat + containment + `O_NOFOLLOW`); a refused or missing file → 410 `CERTIFICATE_FILE_MISSING`.
- RED: 12 failed, 4 passed (determinism and the worker-output-equals-render property already held for the placeholder renderer).

#### Automated visual check (pdftoppm, poppler system package; not in the venv or repo)
Files in `docs/evidence/visual-check/` (PDF from the real renderer + PNG at 80 dpi; zoomed 300 dpi crops inspected for the tight regions).
| Image | Verdict |
|---|---|
| 1-short-name.png | PASS — balanced; name 40 pt one line; nothing near borders. |
| 2-name-120.png | PASS — name 16 pt on 2 lines, centered, clear of "This is to certify that" and the email. |
| 3-greek.png | PASS — "Νίκος Παπαδόπουλος" all glyphs render (incl. accented ί, ό), no boxes. |
| 4-cyrillic.png | PASS — "Екатерина Иванова" all glyphs render, no boxes. |
| 5-email-254.png | PASS — email 8 pt on 3 lines; ~5 pt gap above "has successfully completed" (computed descender 337.7 pt vs cap top 332.6 pt; confirmed at 300 dpi); within the inner border. |
| 6-title-3-lines.png | PASS — title 16 pt on 3 lines, clear of "has successfully completed" (~17 pt) and "Issued by" (~13 pt). |
| 7-combined-worst-case.png (extra) | PASS — 28 pt 2-line name + 3-line 254-char email + 3-line 200-char title + 2-line 200-char issuer together: no overlap or touching (300 dpi crop checked). |
No layout amendment needed. **Human approval of the M3 DoD (§17 "shown to the user and approved as legible") is pending human review** (G2).

### M4 (2026-10-11)
- RED: 16 failed + 3 collection errors (missing `WorkerPool`, `MAX_PDF_BYTES`, SQL constants, `/healthz`, `serve`, standalone `main`).
- `test_failure_isolation::test_items_listing_identifies_failed_and_succeeded` needs `GET …/items` (M5) and is written in M5.
- Kill test uses a state trigger: SIGKILL when ≥ 1 item succeeded and one item was claimed < 200 ms ago (its 300 ms render is certainly running), so the killed item is always mid-render.
- `test_stale_worker_done_write_rejected_by_fencing` is parametrized with distinct and reused worker ids, so fencing on `worker_id` (mutation 1) is caught by it as well as by `test_same_worker_reclaim_is_fenced`.
- Flake: `test_concurrent_schema_creation_two_processes` failed 1 in ~4 runs → root cause found and fixed (G19), not retried in the test.
- Mutation proofs 1–9: all killed; spot-checked that 5 and 6 fail for the intended reason.
- Amendments A-01 (`certgen/__main__.py`) and A-02 (snapshot test) logged in `docs/BLUEPRINT_AMENDMENTS.md` (auto-approved).
- Kill-test proof **demonstrated** on Linux (POSIX signals available).

### M5 (2026-10-11)
- RED: 9 failed + 2 collection errors (no listing endpoint, no `certgen.archive`, no listing SQL constants).
- Archive concurrency slot is held while rows are loaded and the ZIP is built, then released before streaming; the spooled file is closed by the streaming generator (§6.7 limits *builds*).
- The CSV guard also applies to rejected rows (their stored name/email reach the manifest); test uses a full-width "＝" name, which the font lacks, so it is a rejected row.
- `test_query_plans_use_indexes` now covers both listing queries (unfiltered → `sqlite_autoindex_items_1` on (job_id,row_index); filtered → `ix_items_job_status`).

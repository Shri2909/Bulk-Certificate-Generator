# certgen — Stage 2 Remediation Report

| Field | Value |
|---|---|
| Baseline | `audit-baseline-stage1` = `261108d` (the audited revision, Stage 1 report: `audit/AUDIT_REPORT.md`) |
| Branch | `audit/remediation` in a separate git worktree (`../certgen-remediation`); the original working tree, including its uncommitted README change and untracked demo files, was not touched |
| Approval | User reply "approved" to the Stage 1 gate (2026-10-26); four product decisions answered before any code change: F-02 "10 per row + summary", F-10 "Loopback + new setting", F-11 "In-app request limit", F-09 "Raise the budget" |
| Environment | CPython 3.11.9, Linux 7.0.0-38, pins from `requirements*.txt` (after F-06: pillow 13.0.0, pypdf 6.21.2) in the worktree's `.venv` |
| Status | FINAL (Stage 2). Phase 8 ran under code freeze; no defect found in Phase 8 was fixed. |

Labels as in Stage 1: EXECUTED results carry their command or evidence file; nothing below is estimated unless it says so.

## 1. Status of every finding

| ID | Sev | Status | Commit | Regression tests (all EXECUTED, passing) |
|---|---|---|---|---|
| F-01 | HIGH | **FIXED & VERIFIED** | `6762eec` | `test_a1::test_overlong_email_is_rejected_in_bounded_time` (13.4 s → < 2 s), `test_a4::test_overlong_email_over_http…` (3.69 s → 0.02 s) |
| F-02 | HIGH | **FIXED & VERIFIED** (bounded, see §6) | `5647e29`, `815f4a1` | `test_a1::test_unknown_field_errors_are_bounded`; `test_a7` cap boundaries (1/10/11/5,000 keys), 422 + dry-run paths, 1 M-char key, ordering, 1/20-scale attack |
| F-03 | MEDIUM | **FIXED & VERIFIED** | `e6dd6b9` | `test_a1::test_lone_surrogate_never_500` (12 cases), `test_a2` property; `test_a7` surrogates in certificate fields, top-level keys, nested arrays, bare string, `/retry` body; paired escapes still valid |
| F-04 | LOW | **FIXED & VERIFIED** | `16434cd` | `test_a1::test_items_after_out_of_range_is_422`; `test_a7` exact boundary (2⁶³−1 → 200, 2⁶³ → 422) |
| F-05 | MEDIUM | **FIXED & VERIFIED** | `b8b3562` | `test_a1::test_accepted_name_round_trips_through_pdf_text_layer`; `test_a7` exhaustive ToUnicode check over every allowed character (baseline: 412 malformed; now 0) and 4 non-BMP rejections |
| F-06 | MEDIUM | **FIXED & VERIFIED** | `4cd76d2` | `pip-audit` clean (runtime and dev); PDF bytes identical across the bump (same SHA-256) |
| F-07 | LOW | **FIXED & VERIFIED** | `32c31e1` | `test_a1::test_key_violation_reaches_replay_backstop`; `test_a7::test_violates_unique_matches_the_exact_column_only` |
| F-08 | LOW | **FIXED & VERIFIED** | `c13a0c5` | `test_a1::test_progress_is_below_one_until_terminal`, `test_a2::test_progress_one_only_when_terminal` |
| F-09 | LOW | **FIXED & VERIFIED** | `2185309` | `test_a1::test_body_limit_admits_every_valid_request`; `test_a7` worst row recomputed from the allowlist; 1,000 fully-escaped NFD worst-case rows accepted |
| F-10 | MEDIUM | **FIXED & VERIFIED** | `a60153a` | `test_a4::test_foreign_host_header_is_rejected` (real server); `test_a7` 8 allowed + 9 refused Host forms, check before body read, missing Host, env parsing/validation, configured host |
| F-11 | MEDIUM | **FIXED & VERIFIED** (idle connections); residual for slow uploads, see §7 | `330e6c0` | `test_a4::test_idle_connections_do_not_lock_out_clients` (real server); `test_a7` 64 admitted / 65th refused / slot released, slot released on exception; `tests/test_runtime.py` pins the in-app limit |
| F-12 | INFO | **UNRESOLVED** — needs authentication/tenancy (architectural; outside approved scope) | — | `test_a6::test_idempotency_key_existence_oracle_and_squatting` documents the behaviour |
| F-13 | INFO | **UNRESOLVED** — fix conflicts with BLUEPRINT §15 and `tests/test_config.py`; decision needed | — (reverted) | proposed red test kept in `audit/evidence/stage2/F-13-proposed-red-test.txt` |
| F-14 | INFO | **FIXED & VERIFIED** | `97e3dc0` | `test_a7` import with invalid env succeeds silently; `m.app` still validates; uvicorn import string resolves |
| F-15 | INFO | **UNRESOLVED** — semantics decision (should a damaged file be 410, and by which check?) | — | `test_a3::test_truncated_file_on_disk_is_served_as_is` documents the behaviour |
| F-16 | LOW | **FIXED & VERIFIED** (M24, M11 now killed) | `ade1e43` | `test_a7::test_declared_oversize_body_is_refused_without_reading_it`, `::test_symlinked_file_is_refused_even_without_o_nofollow` — both shown to fail against their mutant |
| F-17 | INFO | **UNRESOLVED** — the uncommitted README change and untracked demo files are the user's own work; not touched | — | — |
| F-18 | INFO | **FIXED & VERIFIED** (documented; `/docs` stays enabled per BLUEPRINT §6.10) | `f8283e9` | `test_a7::test_readme_cdn_note_matches_the_docs_page` |
| F-19 | INFO | **FIXED & VERIFIED** | `4433171` | `bandit -t B101 certgen`: 7 → 0 |
| N-01 | LOW | **NEWLY DISCOVERED** (pre-existing; not fixed — not an approved finding) | — | see §6.2 |

Counts: 15 FIXED & VERIFIED, 4 UNRESOLVED (all INFO, each needing a decision), 1 NEWLY DISCOVERED in Stage 2 (pre-existing), plus Phase 8 results in §6.

## 2. Code changes and justification

20 commits on `audit/remediation` (`git log audit-baseline-stage1..HEAD`); one logical fix per commit, messages `fix(<ID>): …`. Product files changed: 21, +346 / −66 lines (`git diff --stat`).

| Commit | Change | Justification |
|---|---|---|
| `0f204b8` | Stage 1 report, evidence and audit tests committed | the red tests become the acceptance contract |
| `617b414` | audit secret scan skips its own pattern list | tracked audit files made the scanner match itself |
| `6762eec` F-01 | `validation._check_email`: `len(email) > 254` → `INVALID_EMAIL` before `validate_email` | library parse is O(n²); length was enforced only after parsing |
| `98a88df` | ruff formatting of audit code; invisible/bidi literals → `\u` escapes; body-limit E2E test derives the limit from `Settings`; `audit/tools/gate.sh` | keep the project gate (`ruff check .`) green; no trojan-source characters; tests must not hard-code a limit that F-09 changes |
| `5647e29`, `815f4a1` F-02 | `validation._unknown_field_errors`: first 10 unknown keys (sorted, via `heapq.nsmallest`) + 1 summary error; messages quote ≤ 64 characters of a key; README states the real bound | user decision "10 per row + summary" |
| `e6dd6b9` F-03 | `validation.decode_json_body`: unpaired surrogates (U+D800–U+DFFF) in any key or string → `INVALID_JSON` | I-JSON (RFC 7493); such strings cannot be stored, hashed or echoed |
| `16434cd` F-04 | `api.list_items`: `after ≤ 2⁶³−1` | sqlite3 overflow → 500 |
| `a60153a` F-10 | `config`: `CERTGEN_ALLOWED_HOSTS`, `normalize_host`, `trusted_hosts`; `middleware.HostAllowlistMiddleware` (400 `INVALID_HOST`); fixtures allow `testserver`; benchmark sends `Host: localhost` | user decision "Loopback + new setting" |
| `330e6c0` F-11 | `middleware.ConcurrencyLimitMiddleware` (64 requests, 503 `SERVER_BUSY`); `serve()` drops `limit_concurrency`; `test_runtime` assertion replaced by one pinning the in-app limit | user decision "In-app request limit" |
| `b8b3562` F-05 | `fonts.SUPPORTED_CHARS` limited to code points ≤ U+FFFF; README scope corrected | ReportLab writes invalid ToUnicode entries above the BMP |
| `4cd76d2` F-06 | `pillow==13.0.0`, `pypdf==6.21.2` | published advisories; fixed releases |
| `f8283e9` F-18 | README note on the CDN-hosted Swagger UI | `/docs` must stay (§6.10) |
| `32c31e1` F-07 | `service.violates_unique` exact constraint comparison (3 call sites) | substring "jobs.id" matched "jobs.idempotency_key" |
| `c13a0c5` F-08 | `status`: progress capped at 0.9999 while unfinished | rounding reached 1.0 early |
| `2185309` F-09 | `BODY_BYTES_PER_RECIPIENT` 2,600 → 4,500 (derived); `test_config` derived values updated; ADR-011 note | user decision "Raise the budget" |
| `97e3dc0` F-14 | `main.get_app()` + module `__getattr__("app")`; `serve()` uses `get_app()` | no environment parsing at import |
| `ade1e43` F-16 | two tests (no product change) | kill surviving mutants M24, M11 |
| `4433171` F-19 | `cast()` in handlers; `service._load_existing_job` raises explicitly | asserts vanish under `-O` |
| `8868a43` | README "Blueprint conformance" lists A-04…A-12 | documentation consistency |

Every behavioural deviation from the frozen blueprint has an amendment row (A-04 … A-12 in `docs/BLUEPRINT_AMENDMENTS.md`), each citing the finding and the approval.

**Test changes to existing project tests (explicitly approved requirement changes or fixture adaptation; no assertion removed or weakened):**
- `tests/conftest.py`, `tests/test_rendering.py`, `tests/test_smoke.py`, `tests/test_crash_recovery.py`: `allowed_hosts=("testserver",)` — the Host header Starlette's TestClient sends (F-10). Configuration only.
- `tests/test_runtime.py::test_server_limits_passed_to_uvicorn`: `limit_concurrency == 64` replaced by "uvicorn gets no `limit_concurrency`" **and** "the app installs `ConcurrencyLimitMiddleware(max_requests=64)`" (F-11; user decision).
- `tests/test_config.py::test_derived_values`: 26,065,536 → 45,065,536 and 100 × 2,600 → 100 × 4,500 (F-09; user decision).

## 3. TDD red → green evidence

For every fix: RED run (the regression test fails for the reason the finding describes), GREEN run after the smallest fix, then the full gate (project suite, ruff, ruff format, mypy --strict, full audit suite). Raw files: `audit/evidence/stage2/<ID>-red.txt`, `-green.txt`, `-gate.txt`.

| ID | RED (failure reason) | GREEN | Gate after the commit (project / audit) |
|---|---|---|---|
| F-01 | "validating one over-long email took 13.4 s"; HTTP `3.69 < 2.0` false | 2 passed; HTTP 0.02 s, `/healthz` 14 ms | 324 / 21 red left (`F-01-tdd.txt`, transcribed: original lost in a reboot) |
| F-02 | "50000 errors returned for one row" | 1 passed; project validation tests 76 passed | 324 / 20 red left |
| F-03 | 9 × 500 INTERNAL_ERROR; property `UnicodeEncodeError '\ud800'` | 12 passed; property passed | 324 / 10 red left |
| F-04 | 2 × 500 | 2 passed | 324 / 8 red left |
| F-10 | `assert 200 == 400` | 1 passed (+ bypass `[::1].evil` found by the new tests during GREEN and fixed before commit) | 324 / 7 red left |
| F-11 | `/healthz statuses … [503 × 11]` | 1 passed | 324 / 6 red left |
| F-05 | "text layer has 'Zoë  Smith'" | 1 passed; exhaustive check 0 malformed (baseline 412) | 323 + 1 intermittent N-01 (re-runs 324, 324) / 5 red left |
| F-06 | pip-audit: 37 (pillow) + 9 (pypdf) known vulnerabilities | "No known vulnerabilities found" ×2; identical PDF SHA-256 | 323 + N-01 (5 re-runs: 4 pass, 1 fail) / 5 red left |
| F-07 | 500 (`RuntimeError: no unique job id after 5 attempts`) | 1 passed | 324 / 4 red left |
| F-08 | `assert 1.0 < 1.0` ×2; property counterexample 99,999/100,000 | 2 passed; 32/32 properties | 324 / 1 red left |
| F-09 | "413 for a 2884126-byte request of valid rows (limit 2665536)" | 1 passed; worst-case 1,000-row request 202 | **324 / 226 passed, 0 failed** |
| F-14 | import with `CERTGEN_PORT=abc` → traceback | 3 passed; E2E 7 passed | 324 / 229 passed |
| F-16 | (test-only) both new tests fail against their mutant: "body was read…", "secret … is None" | pass on real code | 324 / 231 passed |
| F-19 | bandit B101: 7 locations | 0 | 324 / 231 passed |
| F-13 | 3 × "DID NOT RAISE ValueError" | fix conflicted with `tests/test_config.py` (§15 range) → **reverted, not committed** | — |

## 4. Post-remediation test results and coverage (Phase 7, EXECUTED)

Raw: `audit/evidence/stage2/P7-01-suites-static.txt` … `P7-05-live-demo-on-remediated.txt`.

| Check | Result |
|---|---|
| `pytest tests` (project), 3 consecutive runs | **324 passed** ×3 (17.0 s, 22.4 s, 17.3 s) |
| `pytest tests -m slow`, 5 consecutive runs | **5 passed** ×5 |
| `pytest audit/tests` (unit, Hypothesis properties, integration, real-server E2E, stress, red team, Stage 2 regressions) | **231 passed, 0 failed** (a1 21 · a2 32 · a3 27 · a4 7 · a5 2 · a6 80 · a7 62) |
| `ruff check .` / `ruff format --check .` | clean / 57 files formatted |
| `mypy --strict certgen` | no issues (20 files) |
| `bandit -r certgen` | **0 issues** (Stage 1: 7 low, B101) ; `scripts/benchmark.py` keeps 6 low subprocess notes (fixed argv, no shell) |
| `pip-audit` runtime / dev | **no known vulnerabilities** / **no known vulnerabilities** (Stage 1: 37 / 46) |
| Coverage, project + audit suites, in-process | **97 %** line+branch (1,680 stmts, 53 missed; Stage 1: 95 % project-only, 96 % combined). `service.create_job`'s backstop (Stage 1: never executed) is now covered |
| Mutation campaign (`audit/tools/mutate.py --with-audit --stage2`) | **45/45 killed**: the 34 Stage 1 mutants (incl. the 3 former survivors M10, M11, M24) + 11 mutants that each undo one fix (S01–S11). 32 are killed by `tests/`, 13 only by `audit/tests` (see P8-N3) |
| Benchmarks, baseline vs remediated, alternated in one session (`P7-04`) | 1 p 64.4 → 68.3 certs/s · 2 p 108.4 → 110.9 · 4 p 136.8 → 139.6; POST of 1,000 rows 141/146/168 → 143/149/197 ms (single runs; the 4-process POST difference is within the run-to-run spread seen in Stage 1 and not claimed as a regression); all acceptance checks true |
| User's live demo (`scripts/check_live_demo.py`, untracked files overlaid on the remediated code in a scratch copy) | **5/5 PASS**, exit 0 |
| Real-server E2E incl. API SIGKILL + restart, idle-socket lockout, foreign Host, 600 k-char email | 7/7 passed |
| Flaky test | `tests/test_runtime.py::test_standalone_worker_starts_on_empty_data_dir_and_stops_on_sigterm` failed intermittently in 3 gate runs (also 2/20 on the untouched baseline) → N-01; it passed in all 3 Phase 7 runs |

Documentation consistency: README (validation rules, field/problem codes incl. `INVALID_HOST` and `SERVER_BUSY`, configuration incl. `CERTGEN_ALLOWED_HOSTS`, body limit, supported names, progress, limitations, conformance), `docs/BLUEPRINT_AMENDMENTS.md` (A-04 … A-12) and ADR-011 match the code; `tests/test_docs.py` and `tests/test_errors.py::test_error_codes_listed_in_readme` pass.

## 5. Before vs after

| Dimension | Stage 1 (baseline `261108d`) | Stage 2 (freeze `72bc2f3`) |
|---|---|---|
| Health score | 7.0 / 10 | **8.5 / 10** — no HIGH open; two MEDIUM availability/storage limits found in Phase 8 are pre-existing and bounded per request |
| Open findings (HIGH / MEDIUM / LOW / INFO) | 2 / 5 / 5 / 7 | 0 / 2 (P8-N1, P8-N2) / 2 (N-01, P8-N3) / 4 (F-12, F-13, F-15, F-17) |
| Red tests | 23 failing | 0 failing (26 red-marked tests pass) |
| Tests | 324 project | 324 project + 231 audit |
| Coverage | 95 % | 97 % |
| Mutation score | 31/34 | 45/45 |
| Known-vulnerable dependencies | pillow (37), pypdf (9) | none |
| F-01 over-long email | 1 M chars 12.7 s; 26 M chars > 300 s | 600 k chars 0.02 s (HTTP); 1 M chars < 2 s (in-process test) |
| F-02 worst error response / stored | 192 MB / 164 MB, ~2 GB RSS (26 MB body) | 1.4 KB / 1 KB for the same attack at 45 MB; worst legitimate-shape case 11 MB response (13 errors × 10,000 rows) |
| F-03 / F-04 | 500 on 8/12 shapes / 500 | 422 everywhere |
| F-05 non-BMP name | accepted, wrong text layer (412 chars affected) | rejected; every allowed char maps back (exhaustive check) |
| F-10 foreign Host | 200 | 400 `INVALID_HOST` |
| F-11 64 idle sockets | every client 503 (plain text) | 200; overload is 503 `SERVER_BUSY` problem+json |
| Body limit (default) | 26,065,536 B | 45,065,536 B (approved) |
| Max persisted per accepted request (P8-N1) | 104 MB (4.0 × 26 MB) | 135 MB (3.0 × 45 MB) |
| Max parse memory of one request | ~1.9–2.0 GB (amplification) | ~0.7–0.9 GB (45 MB body; no amplification) |
| Throughput (1/2/4 processes) | 64/108/137 certs/s | 68/111/140 certs/s (same session; equal within noise) |

## 6. Phase 8 — fresh-eyes red team (code freeze at `72bc2f3`, tag `audit-remediation-freeze`)

No product file changed after the freeze; only `audit/tools/phase8.py` (a response-reading bug in the attack script) and `audit/tools/compare_cost.py` were written. Raw: `audit/evidence/stage2/P8-01-attacks.txt` … `P8-04-original-exploits.txt`.

### 6.1 Original exploits re-run
`pytest audit/tests -m red` → **26 passed** (all Stage 1 red tests, incl. the 3 E2E ones); full audit suite 231 passed; project suite 324 passed. Stage 1 probes: F-01 1 M-char email rejected in < 2 s; F-02 26/45 MB unknown-key attack → 1.4 KB; F-05 census → 0 non-BMP allowed; F-07 backstop reachable; F-08 cap holds.

### 6.2 Bypass attempts per fix

| Fix | Attempt | Result |
|---|---|---|
| F-01 | 45 MB name of `"a "` pairs (NFC + space collapse before the length check) | 202 in 1.00 s (row rejected `TOO_LONG`); closed |
| F-01 | 45 MB title | 422 in 0.46 s; closed |
| F-01 | 254-char email with 127 labels; quoted local part | 13.0 ms / 2.8 ms; closed |
| F-02 | many tiny rows (9,999 rows × 11 one-character keys, 680 KB) | 11.2 MB response (16.4 ×), 9.0 MB stored — the designed ceiling (13 errors × rows); README corrected before freeze; **bounded, not amplified without limit** |
| F-02 | one 44 MB unknown key | response 45.1 MB (the key is echoed once as `field`, 1 ×); message quotes 64 chars; see P8-N1 for storage |
| F-03 | percent-encoded surrogate in query / path | 422 / 404; closed |
| F-05 | BMP characters whose NFD contains non-BMP code points | 7 exist (U+FA6C, U+FACF–U+FAD7, CJK compatibility ideographs); none is allowed; closed |
| F-10 | `127.0.0.1:`, `127.1`, `localhost:8000:80`, `[::ffff:127.0.0.1]`, `LOCALHOST.`, `localhost%00`, `xn--localhost`, `[::1].evil` | all 400; closed (`[::1].evil` was found by the new tests during GREEN and fixed before commit) |
| F-10 | duplicate Host headers (either order) over real HTTP | 400 from the HTTP parser; closed |
| F-10 | absolute-form target `GET http://attacker.example/healthz` with loopback Host | 404; no bypass |
| F-11 | 64 idle sockets / 64 unfinished header blocks | 200 / 200; closed |
| F-11 | 64 stalled uploads sent as each connects | 503 `SERVER_BUSY` while held; 200 within 1 s after they disconnect (slots released). **Same as baseline** (baseline: 503, connection count) — documented residual, not a regression (`F-11-conn-probe.txt`) |
| F-11 | 64 archive downloads that never read the response (0.9 MB archive) | `/healthz` 200 — slots not held; large (hundreds of MB) archives NOT VERIFIED |
| F-14 | `from certgen.main import app`, `uvicorn certgen.main:app`, `python -m certgen` | all work |
| F-06 | bumped dependencies vs determinism | identical PDF SHA-256 before/after |

### 6.3 Does any fix violate another requirement?
- **F-02 vs R2** ("invalid recipients are reported individually: row index, field, and code"): rows are still reported individually; unknown fields beyond the 10th are counted in one summary error instead of one error each. This is the user's decision, recorded in A-04 and the README.
- **F-05 vs R5**: names with non-BMP characters, accepted before, are now rejected rows; the README's scope statement was corrected to match. No previously valid Latin/Greek/Cyrillic name changes.
- **F-10 vs P3/R13**: `python -m certgen` and the README commands work unchanged on loopback; deployments reached through another hostname must set `CERTGEN_ALLOWED_HOSTS` (documented). The user's live demo still passes.
- **F-09 vs BLUEPRINT §12 memory threat**: the body limit rose 26 → 45 MB (approved); one maximal request now needs ~0.7–0.9 GB of RSS to parse (P8-N2 context).
- No other conflicts found; the full project suite is unchanged apart from the three approved assertion/fixture updates (§2).

### 6.4 New defects (documented, **not fixed**)

| ID | Sev | Status | Where | Entry → Input → Expected → Actual | Impact | Recommended fix (text only) |
|---|---|---|---|---|---|---|
| **P8-N1** | MEDIUM | CONFIRMED (pre-existing; ceiling raised by F-09) | `service._insert_job` (`request_json`), `service._item_values` (`input_json`, `errors`), `validation._unknown_field_errors` (`field` echo) | `POST /v1/certificate-jobs` → one valid row + one row with a single key filling the body → expected: persisted size bounded well below the body → actual: request_json 45.1 MB + input_json 45.1 MB + errors 45.1 MB = **135 MB per request** (3.0 × body; baseline 104 MB = 4.0 × 26 MB), never deleted (`P8-02-cost-compare.txt`) | unauthenticated disk exhaustion by repeated requests (each needs one valid row); the capacity check counts queued items, not bytes | cap what is stored for rejected rows (e.g. truncate stored unknown keys and `input_json` beyond the field limits), add a retention/size quota, and authentication/rate limits before exposure |
| **P8-N2** | MEDIUM | CONFIRMED (pre-existing; baseline 7.08 s) | `api.create_job` → `validation.validate_request` before any admission check | 10,000 maximal valid rows → dry run: 200 after **6.55 s** of validation; real request on a full queue: 503 `CAPACITY_EXCEEDED` after **7.24 s**; nothing stored in either case (`P8-03-validation-cpu.txt`) | CPU exhaustion with no storage cost: ~40 such requests occupy every threadpool thread; dry runs skip the capacity check entirely | check capacity (active + submitted row count) before validating; bound or rate-limit dry runs; consider a per-process validation semaphore |
| **P8-N3** | LOW | CONFIRMED | `pyproject.toml` (`testpaths = ["tests"]`), README `## Run tests` | the regression tests for F-01…F-11 live in `audit/tests`; mutation run: 13/45 mutants (10 of the 11 that undo a fix) are caught **only** there | `pytest` — the documented gate — stays green if a later change reverts a fix | move the Stage 2 regression tests into `tests/` (with `@pytest.mark.req`) or run `pytest audit/tests` in the gate/CI (the two `conftest.py` modules cannot be collected in one run without renaming) |
| **N-01** | LOW | CONFIRMED (pre-existing; found in Phase 6) | `certgen/worker.py:main` (schema created at line 373 before handlers at 380–381) | SIGTERM shortly after start → expected graceful exit 0 → actual exit −15; `tests/test_runtime.py::…stops_on_sigterm` flaky (baseline 2/20, branch 5/20 and 1/5 under load) | flaky gate; non-graceful stop during startup (no claim held yet, so no data effect) | install the signal handlers before `create_engines`/`create_schema` |

## 7. Unresolved findings and residual risks

| Item | Status | Why unresolved / what remains | Needs |
|---|---|---|---|
| F-12 Idempotency-Key oracle / squatting | UNRESOLVED | keys are global because there are no tenants | authentication + per-client key scope (architectural) |
| F-13 test knob can exceed the lease | UNRESOLVED | the fix contradicts BLUEPRINT §15 (range 0–60,000) and `tests/test_config.py`; reverted | decision: reject (changes §15) or startup warning (no requirement change) |
| F-15 damaged PDF served with 200 | UNRESOLVED | which check (header/trailer heuristic vs stored checksum, a schema change) and which status (410?) | product decision |
| F-17 working tree vs HEAD | UNRESOLVED | the uncommitted README section and untracked demo files are the user's work | user |
| P8-N1, P8-N2, P8-N3, N-01 | NEW, not fixed (code freeze) | see §6.4 | approval for a further round |
| Slow uploads hold request slots (F-11 residual) | ACCEPTED/DOCUMENTED | pre-existing; holding a slot bounds buffered-body memory | reverse proxy with header/body timeouts for exposed deployments |
| Idle sockets bounded only by `ulimit -n` (524,288 here) | DOCUMENTED | uvicorn has no idle-connection timeout before the first request | proxy / OS limits |
| Max parse memory ~0.7–0.9 GB per 45 MB request; 64 concurrent ones are not bounded by RAM | DOCUMENTED | inherent to buffering + Python JSON objects; raised by the approved F-09 budget | streaming/CSV ingestion (BLUEPRINT §22) or a lower `CERTGEN_MAX_RECIPIENTS` |
| Large-archive slow readers holding slots | NOT VERIFIED | needs a multi-hundred-MB archive | — |
| Windows (no `O_NOFOLLOW`, no POSIX signals) | NOT VERIFIED | Linux-only host; the lstat path is now tested by simulation | — |
| Python 3.14 | NOT VERIFIED in Stage 2 | only 3.11.9 executed | — |
| Concurrent F-02/P8-N2 load to OOM / threadpool saturation | NOT VERIFIED | not run, to protect the host; per-request costs measured | — |

## 8. Release-readiness verdict

**CONDITIONAL PASS.** On evidence, the remediated branch is release-ready for the documented deployment (single host, loopback bind, trusted local clients): every approved finding with a decided fix is FIXED & VERIFIED, all 23 Stage 1 red tests and all original exploits are closed, no bypass of a fix was found, no requirement or performance regression was measured, and every gate passes (324 + 231 tests, 97 % coverage, 45/45 mutants, clean lint/type/bandit/pip-audit).

It is **not** ready for network exposure: P8-N1 (unbounded disk use across requests) and P8-N2 (aggregate validation CPU) are pre-existing availability limits that need authentication, quotas/rate limiting and retention, and slow uploads can still occupy all request slots without a fronting proxy. The branch `audit/remediation` is not merged; merging, and the decisions on F-12, F-13, F-15, F-17, P8-N1–N3 and N-01, are the user's.

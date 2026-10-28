# certgen — Stage 2 Remediation Report

| Field | Value |
|---|---|
| Baseline | `audit-baseline-stage1` = `261108d` (the audited revision, Stage 1 report: `audit/AUDIT_REPORT.md`) |
| Branch | `audit/remediation` in a separate git worktree (`../certgen-remediation`); the original working tree, including its uncommitted README change and untracked demo files, was not touched |
| Approval | User reply "approved" to the Stage 1 gate (2026-10-26); four product decisions answered before any code change: F-02 "10 per row + summary", F-10 "Loopback + new setting", F-11 "In-app request limit", F-09 "Raise the budget". Round 2: user request "fix everything there should be nothing left unfixed.." (2026-10-26) with six decisions (§9) |
| Environment | CPython 3.11.9, Linux 7.0.0-38, pins from `requirements*.txt` (after F-06: pillow 13.0.0, pypdf 6.21.2) in the worktree's `.venv` |
| Status | FINAL (Stage 2 + round 2). Phase 8 ran under code freeze; its defects were fixed in round 2 (§9), which supersedes the statuses in §1–§8. No finding is open; documented residuals in §9.7. |

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

## 9. Round 2 — "fix everything" (2026-10-26 → 2026-10-28)

Requested by the user after the Stage 2 gate ("fix everything there should be nothing left unfixed.."). Every open item from §6.4/§7 was fixed under the same TDD rules (RED → GREEN → full gate, one fix per commit). Six product decisions were asked once and answered with the recommended option: F-12 "Token by default", P8-N1 "Row cap + free-space guard", P8-N2 "Submission limit + deadline", F-13 "Reject at startup", F-15 "Header/trailer check", F-17 "Commit demo, then merge"; after the round-2 gate a seventh, for the residual R2-O1: "Per-token request cap" (2026-10-28). Statuses in §1–§8 are those at the end of round 1; this section supersedes them.

### 9.1 Status of every item open after round 1

| ID | Sev | Status | Commit(s) | Regression evidence (EXECUTED, passing) |
|---|---|---|---|---|
| F-12 Idempotency-Key oracle / squatting | INFO | **FIXED & VERIFIED**: bearer token on every `/v1` route (401 + `WWW-Authenticate`), keys scoped per token (A-17) | `f56fec1` | `tests/test_hardening.py` token tests (12); mutants R10, R11 killed; real server: 401 without/with a wrong/query/Basic token, `/healthz`·`/docs`·`/openapi.json` open, unauthenticated 40 MB POST refused before its body, same key under two tokens → 202 + 202 (`R2-P8-01-attacks.txt`) |
| F-13 slow-render knob ≥ lease | INFO | **FIXED & VERIFIED**: refused at startup (A-13) | `6e86b5e` | 9 parametrized cases; `tests/test_config.py` range updated; R02 killed; `python -m certgen` with 30,000 ms → exit 1 naming the variable |
| F-15 damaged PDF served with 200 | INFO | **FIXED & VERIFIED**: size cap + `%PDF-`/`%%EOF` framing → 410 `CERTIFICATE_FILE_MISSING`, manifest likewise (A-14) | `c6d0116`, `d9c31a3`, `946b267` | `test_damaged_certificate_file_is_410[…]`, size-cap boundary test; R03, R04 killed |
| F-17 working tree vs HEAD | INFO | **FIXED & VERIFIED**: the user's demo files committed unchanged (`f6bd596`), then adapted to tokens (`609b2c8`); README section identical to the user's text (`39327ed`); merged into `main` (§9.6) | `f6bd596`, `609b2c8`, `39327ed` | `scripts/check_live_demo.py` 5/5 PASS (also under `python -O`); the 7 other untracked files byte-identical to the committed ones |
| P8-N1 disk exhaustion | MEDIUM | **FIXED & VERIFIED**: a row over 4,500 B → 422 `TOO_LONG` with nothing stored; `CERTGEN_MIN_FREE_DISK_MB` (1024) → 503 `STORAGE_FULL` (A-15) | `eb6b702` | `test_oversized_row_rejects_the_request_and_stores_nothing`, `test_new_jobs_are_refused_when_free_disk_is_low`; R05, R06 killed; worst case 104 MB stored (baseline) → 0 (`R2-P8-02-cost-compare.txt`) |
| P8-N2 validation CPU / slot exhaustion | MEDIUM | **FIXED & VERIFIED**: ≤ 2 submissions at once, 30 s body deadline (408), full-queue pre-check (A-16); 30 s send deadline (A-18) | `ac6de25`, `3ebea3a` | `test_at_most_two_submissions_are_processed_at_once`, `test_body_that_does_not_arrive_in_time_is_408`, `test_full_queue_is_refused_before_validation`, 3 send-deadline tests; R07–R09, R12–R15 killed |
| P8-N3 fix regressions only in `audit/tests` | LOW | **FIXED & VERIFIED**: round-1 regression cases ported into `tests/test_audit_regressions.py`; two symlink tests strengthened after M10/M11 survived `tests/` (§9.4) | `08e6ced`, `f0a1e11` | S01–S11 all killed by `tests/` alone (`R2-mutation-fixes-tests-only.tsv`; round 1: 10 of 11 only by `audit/tests`); M10, M11 killed by `tests/` (`R2-M10-M11-mutants.tsv`) |
| N-01 SIGTERM during worker startup | LOW | **FIXED & VERIFIED**: handlers installed before database setup | `9f359c2` | `test_worker_installs_signal_handlers_before_touching_the_database`; formerly flaky test 20/20; R01 killed |
| F-11 residual: slow uploads hold slots | — | **CLOSED** for submissions (only 2 can hold a slot); every other body bounded to 30 s | `ac6de25` | `conn_probe`: 64 stalled uploads → 62 refused at once, 2 waiting, `/healthz` 200 ×3 (round 1: 503) (`R2-conn-probe.txt`) |
| Slow readers hold slots (round 1: NOT VERIFIED) | MEDIUM | **CONFIRMED, then FIXED & VERIFIED**: send deadline (A-18) | `3ebea3a` | 64 unread 1,500-recipient archive downloads: before (`946b267`): `/healthz` 503 at every sample for 60 s, 200 only after the readers closed (`R2-slow-reader-probe-before.txt`); after: `/healthz` 200 from t = 40 s with the readers still open, 0 tracebacks (`R2-slow-reader-probe-after.txt`) |
| **N-02** (new, round-2 re-validation) | LOW | **FIXED & VERIFIED**: `scripts/check_live_demo.py` made its 30 checks with `assert` (vacuous PASS under `python -O`); unused `TOKEN_FILE_NAME` flagged by bandit B105 | `395bc96` | `test_runtime_checks_are_not_assert_statements` (all 21 `certgen` modules + the checker); bandit `certgen` 1 → 0 |
| **R2-O1** (round-2 attacks) token holder occupies every request slot | LOW | **FIXED & VERIFIED**: at most `CERTGEN_MAX_REQUESTS_PER_TOKEN` (32) of the 64 slots per token; requests without a token count only towards the 64 (A-19) | `e6f7761`, `5f0d534` | `test_one_token_cannot_hold_every_request_slot`, `test_token_slot_is_released_when_the_app_raises`, default/range tests, runtime pin; R16–R19 killed (`R2-O1-mutants.tsv`); same attack on a real server: `/healthz` and a second token 200 at every sample (before: 503 at every sample) (`R2-O1-probe-after.txt`) |
| **N-03** (new, found while preparing the merge) | LOW | **FIXED & VERIFIED**: `pytest audit/tests`, as the README described it, failed at collection on a dev install (Hypothesis in no requirements file); `audit/requirements.txt` pins the audit tools, README points to it | `a9a578b` | RED `R2-N-03-red.txt`; GREEN on the user's main `.venv` after the merge (§9.6) |

### 9.2 Code changes and justification (round 2)

| Commit | Change | Justification |
|---|---|---|
| `9f359c2` N-01 | `worker.main`: signal handlers before `check_sqlite_version`/`prepare_data_dir`/schema | a SIGTERM during startup killed the process (exit −15) |
| `6e86b5e` F-13 | `Settings.__post_init__`: `test_slow_render_ms < lease_seconds × 1000` | user decision "Reject at startup" |
| `c6d0116`, `d9c31a3`, `946b267` F-15 | `Storage.read`: bounded read of `MAX_PDF_BYTES + 1`, header/trailer check, `damaged_certificate_file` log; `MAX_PDF_BYTES` moved to `storage.py`; unused `Storage.exists` removed; a redundant `fstat` check removed after mutant R04 showed it was untestable | user decision "Header/trailer check" |
| `eb6b702` P8-N1 | `validation`: per-row canonical size > 4,500 B → request-level 422; `service.check_free_disk` on create and `/retry`; `STORAGE_FULL` | user decision "Row cap + free-space guard" |
| `ac6de25` P8-N2 | `SubmissionLimitMiddleware`, body deadline in `BodySizeLimitMiddleware`, `service.queue_is_full` pre-check, `REQUEST_TIMEOUT` | user decision "Submission limit + deadline" |
| `f56fec1` F-12 | `certgen/auth.py` (token store, 0600 token file via `O_EXCL`/`O_NOFOLLOW`, SHA-256 digests, constant-time compare), `TokenAuthMiddleware`, OpenAPI bearer scheme, key scoping; README/demo/benchmark send the token | user decision "Token by default" |
| `08e6ced` P8-N3 | `tests/test_audit_regressions.py`, `tests/test_hardening.py` | `pytest` alone must catch a reverted fix |
| `f6bd596`, `609b2c8`, `39327ed` F-17 | user's demo committed as-is, then token header injected; README whitespace restored | user decision "Commit demo, then merge" |
| `3ebea3a` P8-N2 | `SendDeadlineMiddleware` (`SEND_TIMEOUT_SECONDS = 30`) inside the request limit; logs `slow_client_aborted` without request data | slow readers pinned every slot (§9.1) |
| `395bc96` N-02 | `check()` helper in the demo checker (same expressions, AST-verified); unused constant removed | `-O` strips asserts |
| `a9a578b` N-03 | `audit/requirements.txt` (Hypothesis, coverage, bandit, pip-audit + dependencies, `-r ../requirements-dev.txt`); README | reproducible audit runs |
| `e6f7761` R2-O1 | `ConcurrencyLimitMiddleware(max_requests, max_per_client)` counts per client id (set by the token check); `CERTGEN_MAX_REQUESTS_PER_TOKEN`; per-token `SERVER_BUSY` detail | user decision "Per-token request cap" |
| `5f0d534` R2-O1 | settings field renamed `max_requests_per_client` (env var unchanged) | bandit B105 matched the key name `…_token` (a false positive on an env var name) |
| `f0a1e11` P8-N3 | M11 test: the planted secret is framed like a PDF; M10 test: added a link to another job's directory inside the root (both suites); `mutate.py` deselects tests failing on the unmutated copy | F-15's damage check and the root check masked the two symlink checks; a broken baseline test produced false kills |

Up to this report, 22 commits follow the freeze tag (43 since the baseline). Product code (`certgen/`): since the baseline 12 files, +619 / −60 lines; round 2 alone 10 files, +385 / −36 (`git diff --shortstat`). Amendments A-13 … A-19 in `docs/BLUEPRINT_AMENDMENTS.md`, each citing the finding and the decision.

**Changes to existing tests (round 2; disclosed in full):**
- Fixtures/config only (F-12): `tests/conftest.py` sends a fixture token; direct `TestClient` sites in `test_jobs_api`, `test_errors`, `test_rendering`, `test_smoke`, `test_crash_recovery` pass it; `audit/tests` conftest and real-server helpers send the audit token.
- `tests/test_idempotency.py`: the stored key is now `<client_id>:<key>` (A-17); the 8-thread same-key race sets `max_concurrent_submissions=8` so it still races (assertions unchanged). `audit/tests/test_a5_stress.py` likewise (8 producers).
- `tests/test_config.py`: slow-render range now accepts 0 and 29,999 and rejects −1, 30,000, 60,001 (A-13, approved); a row for `CERTGEN_MAX_REQUESTS_PER_TOKEN` added (A-19).
- `tests/test_runtime.py::test_server_limits_passed_to_uvicorn`: the in-app limit is now pinned as `{max_requests: 64, max_per_client: 32}` (A-19, approved; stricter).
- `audit/tests/test_a7_stage2_regressions.py`: `BodySizeLimitMiddleware` gets the new `body_timeout_seconds` argument.
- Adapted to the approved row cap (A-15): three F-02 cap tests use rows that fit the 4,500 B budget (5,000 → 300 and 3,000 → 300 unknown keys, a 1 M-character key → 4,000), so they still exercise the per-row cap instead of being refused earlier (the size bound became stricter: `< 1.2 × 4,000 + 1,000` B); `test_a1::test_overlong_email…` and `test_a1::test_unknown_field_errors_are_bounded` accept the new 422 `TOO_LONG` as well as the old 202 — their time, response-size and stored-size assertions are unchanged.
- `audit/tests/test_a3_integration.py::test_truncated_file_on_disk_is_served_as_is` (a Stage 1 observation, not a contract) became `…_is_refused` (410), the approved F-15 behaviour.
- Strengthened (`f0a1e11`): `test_symlinked_file_is_refused_even_without_o_nofollow` plants a PDF-framed secret; `test_symlinked_job_dir_is_refused_on_write` gained the inside-the-root case (both in `tests/` and `audit/tests`).

No test was deleted or skipped; no security check was disabled.

### 9.3 TDD red → green evidence (round 2)

Raw: `audit/evidence/stage2/R2-<ID>-red.txt`, `-green.txt`, `-gate.txt`.

| ID | RED (failure reason) | GREEN | Gate after the commit (project / audit) |
|---|---|---|---|
| N-01 | `SIG_DFL` still installed when the schema is created | 1 passed; formerly flaky test 20/20 | 325 / 231 |
| F-13 | 4 × "DID NOT RAISE ValueError" | 50 passed | 334 / 231 |
| F-15 | 6 failed (damaged file served / listed in the archive) | 7 passed | 341 / 231 |
| P8-N1 | 3 failed + 1 error (no row cap, no `check_free_disk`) | 5 passed | 347 / 231 |
| P8-N2 (submissions, body deadline) | 4 failed (`body_timeout_seconds` unknown, no limit) | 4 passed | 351 / 230 + 1 failed: an audit test still constructed `BodySizeLimitMiddleware` without the new argument; signature adapted in the same commit; every later gate 231/231 |
| F-12 | 12 failed (`api_tokens` unknown, no 401, no token file) | 12 passed | 365 / 231 |
| P8-N3 | (move only; the moved tests pass) | — | no separate gate file; the next gate (F-17) 448 / 231 |
| F-17 | `check_live_demo.py` against the token-protected API: `AssertionError` at its dry-run check (`status == 200 and … counts`, line 136) | 5/5 PASS | 448 / 231 |
| F-15 (R04) | mutant R04 survived (redundant `fstat`) | boundary test kills it | 449 / 231 |
| P8-N2 (send deadline) | 2 × `ImportError: SendDeadlineMiddleware` | 2 passed (+1 through FastAPI `StreamingResponse`) | 452 / 231 |
| N-02 | 1 failed: 30 assert statements in the checker | 22 passed; checker 5/5 normally and under `-O` | 474 / 231 |
| N-03 | `ModuleNotFoundError: No module named 'hypothesis'` collecting `audit/tests` in a dev-only venv | the user's `.venv` after `pip install -r audit/requirements.txt`: 232 passed (§9.6) | — (docs + requirements only; README tests 13 passed, ruff clean) |
| R2-O1 | 5 failed: `max_per_client` unknown (×2), no `max_requests_per_token` setting (field since renamed `max_requests_per_client`, see below), env var not range-checked, runtime pin | 6 passed | 479 / 232 |
| P8-N3 (M10/M11) | both mutants survive `tests/` (`R2-P7-03-mutation-first-run.tsv`; reproduced by hand: the 4 symlink tests pass with either mutant) | both killed by the strengthened tests (`R2-M10-M11-mutants.tsv`) | 475 / 232 |

### 9.4 Re-validation after round 2 (Phase 7 checks, EXECUTED)

Raw: `R2-P7-01-suites-static.txt` (final HEAD), `R2-P7-01-suites-static-5893f60.txt` (the run that found N-02), `R2-P7-01-suites-static-e6f7761.txt` (the run that found the B105 on the new settings key), `R2-P7-03-mutation.tsv`, `R2-P7-04-benchmarks.txt`, `R2-demo-auto.txt`.

Every run below used the final product code (`certgen/` as of `e6f7761`): attacks, benchmarks, demos and cost at `e6f7761`, the mutation campaign at `e6f7761` (R16–R19 re-run after the rename, `R2-O1-rename-mutants.tsv`), the suites at the final HEAD `5f0d534`. (The A-18 slow-reader measurements in §9.1 predate A-19; with A-19 one token can no longer reach the 64 slots at all.)

| Check | Result |
|---|---|
| `pytest tests`, 3 consecutive runs (Python 3.11.9) | **479 passed** ×3 (21.7 s, 21.8 s, 22.8 s); round 1: 324 |
| `pytest tests -m slow`, 5 consecutive runs | **6 passed** ×5 |
| `pytest audit/tests` | **232 passed** (Python 3.11.9) |
| Python 3.14.4 (`~/.cache/certgen-audit/venv314`) | **479 + 232 passed** |
| `ruff check .` / `ruff format --check .` / `mypy --strict certgen` | clean / 66 files formatted / no issues (21 files) |
| `bandit -r certgen` | **0 issues** (at `5893f60`: 1 low, B105 on an unused constant → N-02; at `e6f7761`: 1 low, B105 on the new settings key → renamed in `5f0d534`) |
| `bandit -r scripts` | 11 low notes: subprocess calls with fixed argv (benchmark, live demo, checker) and one `urlopen` of the checker's fixed `http://127.0.0.1` base; the checker's 30 B101 asserts are gone (N-02) |
| `pip-audit` runtime / dev / `audit/requirements.txt` | **no known vulnerabilities** in all three (runtime and dev at the final HEAD; `audit/requirements.txt`, unchanged since, at `a9a578b` after retries — the first attempts hit pypi.org read timeouts) |
| Coverage, project + audit suites, in-process | **97 %** line+branch (1,876 stmts, 44 missed; round 1: 97 %, 1,680 / 53) |
| Mutation campaign (`mutate.py --with-audit --stage2 --round2`) | **64/64 killed, every one by `tests/` alone**: 34 Stage 1 (M), 11 round-1 fix (S), 19 round-2 fix (R) mutants. One test deselected because it fails on the unmutated copy (the secret scan; see the correction below). The first run (`R2-P7-03-mutation-first-run.tsv`) exposed M10/M11 → `f0a1e11` |
| Benchmarks, baseline vs remediated, alternated in one session (`R2-P7-04`, two runs) | per-certificate render and item medians equal in all 12 runs (≈ 4.2 / 11.3 ms); certs/s 1 p 86.1 → 87.3 and 84.3 → 84.2 · 2 p 137.6 → 100.9 and 141.3 → 137.3 · 4 p 169.1 → 175.1 and 159.3 → 166.0 — the 100.9 (wall time only, same per-item medians) did not reproduce in the repeat; POST of 1,000 rows differs by −9 … +15 ms per pair, median **+5.5 ms** (the per-request token hash, free-disk and queue checks, per-row size check); all acceptance checks true. An earlier attempt was refused with `503 STORAGE_FULL` — the new guard working — while the host disk had about 1.1 GB free; another run was discarded because the machine was busy (baseline render p50 10.2 ms instead of ~4.1) |
| `scripts/check_live_demo.py` | **5/5 PASS** (`R2-live-demo.txt`), also under `python -O` (N-02) |
| `scripts/demo.sh --auto` | 11 chapters, exit 0; the token does not appear in the transcript (`R2-demo-auto.txt`) |

**Correction to round 1 (§4, §5, §8).** Round 1 reported the mutation campaign as 45/45 killed. Four of those kills were false: M10, M11, S10 and S11 survived `tests/`, and the audit-suite fallback marked them killed only because `audit/tests/test_a6_redteam.py::test_no_secrets_committed` runs `git ls-files`, which fails in the mutation copy (made without `.git`), so that test failed for every mutant (`P7-03-mutation.tsv`, first-failure column). The correct round-1 figure is 41 genuine kills and 4 unknown. In round 2, S10 and S11 are killed by `tests/` (`R2-mutation-fixes-tests-only.tsv`), M10 and M11 by the strengthened tests above, and `mutate.py` now runs both suites on the unmutated copy first and deselects any test that fails there, listing it in the output.

### 9.5 Round-2 fresh-eyes attacks (EXECUTED; raw `R2-P8-01-attacks.txt`, `R2-P8-02-cost-compare.txt`, `R2-conn-probe.txt`, `R2-slow-reader-probe-*.txt`)

| Target | Attempt | Result |
|---|---|---|
| F-12 | no token / wrong token / `?access_token=` / `Basic` / `Bearer` + appended char / empty `Bearer ` | 401 each; `bearer` (lower case) and extra spaces around a valid token are accepted (RFC 7235 schemes are case-insensitive) |
| F-12 | unauthenticated `POST` announcing 40 MB, body never sent | `401` at once, before any body |
| F-12 | same Idempotency-Key under two tokens, different bodies | 202 + 202 (no cross-token 409 oracle); same token, other body → 409 `IDEMPOTENCY_KEY_REUSED` |
| F-12 | token appears in server logs | 0 lines, except uvicorn's access-log line for the attack URL that carried it in the query string (a client mistake; such a request is refused) |
| P8-N1 | one row holding a 44 MB key | 422 in 0.30 s, 264 B response, database grew 0 MB (baseline: 104 MB stored per request) |
| P8-N1 | free-disk floor above the free space | `POST` 503 `STORAGE_FULL`; dry run still 200 |
| P8-N2 | 8 concurrent maximal dry runs (9.5 MB, 10,000 rows) | 2 × 200 (15.1 s together), 6 refused; `/healthz` 200 on all 19 polls (max 1.0 s while two validations share the GIL) |
| P8-N2 | the 6 refused clients streamed their whole body | they saw `ECONNRESET`, not the 503 the server sent (logged): the server answers before reading the body and closes; with `Expect: 100-continue` a third submission gets the 503 in 0.75 s, body never sent. Documented for clients in the README |
| P8-N2 | maximal real request on a full queue | 503 `CAPACITY_EXCEEDED` after 0.07 s (round 1: 7.24 s of validation first) |
| P8-N2 | body trickled at 1 B/s | 408 after 30.0 s |
| P8-N2 | 64 stalled uploads / 64 idle sockets / 64 unfinished headers | `/healthz` 200 ×3 in each case |
| A-18 | 64 archive downloads never read, never re-requested | `/healthz` 200 from t = 40 s, readers still open; 53 aborts logged, 0 tracebacks |
| A-18 | same, but each stalled download renewed every 31 s (old socket kept) | before A-19: 1,500-recipient archive `/healthz` 200 from t = 40 s; **300-recipient archive 503 at every sample** (R2-O1) |
| A-19 | the 300-recipient renewal attack again, plus a second token polling `GET /v1/certificate-jobs/{id}` | `/healthz` **200** and the second token **200** at every sample over 75 s; 64 aborts, 0 tracebacks |
| F-13 | `CERTGEN_TEST_SLOW_RENDER_MS=30000` | exit 1: "must be shorter than CERTGEN_LEASE_SECONDS (30 s)" |
| cost | 10,000 maximal valid rows | 7.24 s (baseline) vs 7.54 s (remediated): per-request validation CPU about the same; only concurrency is bounded |

### 9.6 Merge

Done 2026-10-28, local only; nothing was pushed.

1. **Backup.** In the main working tree, `git stash push -u` saved the user's uncommitted README change and every untracked file (the demo files and the untracked Stage 1 copy of `audit/`) as `stash@{0}` ("pre-merge backup (2026-10-28) …"). The stash is kept, not dropped.
2. **Fast-forward.** `git merge --ff-only audit/remediation`: `main` `261108d` → `1121904`, no merge commit; working tree clean.
3. **Backup vs `main`.** Of the 32 stashed untracked files, 21 are byte-identical in `main`; the 11 others differ only by committed changes — 9 audit tests/tools (the Stage 1 copy, updated during remediation), `scripts/live_demo.py` (token, `609b2c8`) and `scripts/check_live_demo.py` (N-02). Of the user's 33 added README lines, all are present verbatim except one, which `609b2c8` extended with a sentence on the per-session API token.
4. **The user's `.venv`.** `pip install -r audit/requirements.txt` → pillow 13.0.0, pypdf 6.21.2 and the audit tools; `pip check` clean (N-03 GREEN, `R2-N-03-green.txt`). Its 3 `activate` scripts and 17 console scripts (`pytest`, `mypy`, `uvicorn`, `pip`, …) still named the project's former location (`/home/abc/Desktop/bulk certificate generator/`), so `pytest` or `mypy` after activation failed (pre-existing; the gate's mypy step reported `not found`). Re-running `python -m venv .venv` in place (no `--clear`) and force-reinstalling the same pinned packages with `--no-deps` fixed every one; the installed versions are unchanged.
5. **Checks in `main` with that venv.** 475 passed, ruff clean, 66 files formatted, mypy clean, `pytest audit/tests` 232 passed — the secret scan included, now with real git metadata (`R2-main-merged-gate.txt`); `scripts/check_live_demo.py` 5/5 PASS (`R2-main-live-demo.txt`).
6. **Cleanup.** The worktree `../certgen-remediation` was removed after `8d019c3` was fast-forwarded into `main`; the branch `audit/remediation` and the tags `audit-baseline-stage1` and `audit-remediation-freeze` stay.
7. **After the R2-O1 decision (2026-10-28).** The worktree was recreated on `audit/remediation` with a fresh venv from `audit/requirements.txt`; `e6f7761`, `5f0d534` and this report were committed there and `main` was fast-forwarded again (no merge commit), then checked as in step 5 (`R2-main-merged-2-gate.txt`, recorded in the next commit).

### 9.7 Remaining items and residual risks

| ID | Sev | Status | What remains | Needs |
|---|---|---|---|---|
| R2-O1 token holder can keep every request slot busy | — | **FIXED** (A-19, §9.1) | — | — |
| R2-O5 clients sharing one token share its 32 slots; the 2 submission slots are shared by all tokens | INFO | DOCUMENTED (README Limitations) | a token holder can keep its own 32 slots busy, which affects every client using the same token, and two slow uploads by one token can hold both submission slots (each up to 30 s, renewable) | separate tokens per client; a per-token submission share would be a further product decision |
| R2-O2 aborted connection keeps its file descriptor | INFO | DOCUMENTED (README Limitations) | uvicorn's `transport.close()` waits for the unread buffer; fd freed when the client closes (server fds 350 → 158 when the probe closed its sockets) | OS/proxy limits, as for idle sockets |
| R2-O3 reset instead of 503 on a refused submission | INFO | DOCUMENTED (README) | uvicorn has no lingering close | clients send `Expect: 100-continue` or retry on reset |
| R2-O4 a nearly full queue still validates first | INFO | DOCUMENTED (by design) | the pre-check refuses only when the queue is already full; a request that would overflow it is refused after validation (≤ 2 at once) | — |
| Parse memory per maximal request | INFO | DOCUMENTED | ~0.7–0.9 GB per 45 MB body; now at most 2 at once (P8-N2) instead of up to 64 | streaming ingestion or a lower `CERTGEN_MAX_RECIPIENTS` |
| Windows | — | NOT VERIFIED | Linux-only host | a Windows run |
| Human legibility sign-off and live walkthrough (P2) | — | NOT DONE | needs a person | the user |

### 9.8 Release-readiness verdict (round 2)

**PASS for the documented deployment** (single host, loopback bind, clients holding the API token); **CONDITIONAL for wider exposure.** Every finding from Stage 1, Phase 8, the round-2 re-validation (N-02, N-03) and the round-2 attacks (R2-O1) is FIXED & VERIFIED (§9.1); no bypass of a round-2 fix was found (§9.5); all gates pass at the final HEAD (479 + 232 tests on Python 3.11 and 3.14, 97 % coverage, 64/64 mutants killed by `tests/` alone, clean ruff/mypy/bandit/pip-audit); throughput is unchanged and a 1,000-row job submission is about 5.5 ms slower (median).

Before exposing the API to clients that are not trusted: give each client its own token (R2-O5) and front the API with a proxy that limits idle and aborted connections (R2-O2). Not verifiable here: a Windows run and the human legibility sign-off / live walkthrough (P2).

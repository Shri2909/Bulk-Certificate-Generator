# certgen — Stage 2 Remediation Report

| Field | Value |
|---|---|
| Baseline | `audit-baseline-stage1` = `261108d` (the audited revision, Stage 1 report: `audit/AUDIT_REPORT.md`) |
| Branch | `audit/remediation` in a separate git worktree (`../certgen-remediation`); the original working tree, including its uncommitted README change and untracked demo files, was not touched |
| Approval | User reply "approved" to the Stage 1 gate (2026-10-26); four product decisions answered before any code change: F-02 "10 per row + summary", F-10 "Loopback + new setting", F-11 "In-app request limit", F-09 "Raise the budget" |
| Environment | CPython 3.11.9, Linux 7.0.0-38, pins from `requirements*.txt` (after F-06: pillow 13.0.0, pypdf 6.21.2) in the worktree's `.venv` |
| Status | DRAFT — Phase 7 measurements and Phase 8 results are being added |

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

## 4. Post-remediation test results and coverage

PENDING (Phase 7).

## 5. Before vs after

PENDING.

## 6. Phase 8 — fresh-eyes red team (code freeze)

PENDING.

## 7. Unresolved findings and residual risks

PENDING.

## 8. Release-readiness verdict

PENDING.

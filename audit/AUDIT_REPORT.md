# certgen — Stage 1 Forensic Audit Report

| Field | Value |
|---|---|
| Audited revision | `261108d` (`main`, HEAD) **plus the uncommitted working tree**: `README.md` modified (+33 lines, "Live visual demo"), untracked `demo/`, `docs/DEMO.md`, `docs/evidence/live-demo-*`, `scripts/{live_demo.py,check_live_demo.py,demo-live.sh}` |
| Audit dates | 2026-10-25 → 2026-10-26 |
| Mode | **Read-only.** No project file was modified. All execution ran in a disposable rsync copy in the session scratchpad with a separate audit venv (CPython 3.11.9, the exact pins of `requirements-dev.txt` + `coverage`, `hypothesis`, `pip-audit`, `bandit`). Only `audit/` was written in the repository. |
| Platform | Linux 7.0.0-38-generic x86_64, SQLite 3.46.1, ext4 |
| Status | **FINAL (Stage 1). No corrective change has been made to the project.** |

Labels: **EXECUTED** = run, with command and result; **PROPOSED** = not run. Finding status: **CONFIRMED** (reproduced by an executed test), **SUSPECTED** (code reading only), **TOOL WARNING** (tool output, reachability not demonstrated). Extrapolations are labelled as such and are never presented as measurements.

---

## 1. Executive summary

**Health score: 7.0 / 10.** **Verdict: CONDITIONAL PASS** — for the documented deployment (single host, loopback bind, trusted local clients). **FAIL for any network-exposed deployment** until remediation batches B1–B3 (§14) are done.

Justification. Every functional requirement of the PDF is implemented and was verified by execution, not by reading: the original 324 tests pass; 146 independent audit tests pass (Hypothesis properties, real-HTTP E2E, multi-process stress with SIGKILL of workers and of the API, 80 red-team attacks blocked); the documented benchmarks reproduce within single-digit percent; the crash-safety claims hold; an independent 34-mutant campaign killed 31, and the 3 survivors are redundant defences. The concurrency design (BEGIN IMMEDIATE claim, per-claim token fence, retire step) withstood every attack the audit made on it. Points are deducted because the input limits that the threat model relies on do not bound the resources that run out: one unauthenticated request can pin a CPU core for hours (F-01) or consume ~2 GB of RAM and 164 MB of disk (F-02). A web page can reach the loopback API via DNS rebinding (F-10). 64 idle sockets lock out all clients (F-11). Some malformed inputs return 500 (F-03, F-04). Non-BMP names produce a wrong PDF text layer (F-05). A runtime dependency carries 37 published advisories (F-06).

**Top 10 risks**

| # | ID | Sev | Risk |
|---|---|---|---|
| 1 | F-01 | HIGH | O(n²) email validation: one request ≈ hours of CPU (measured up to 12.7 s at 1 M chars; > 300 s at 26 M) |
| 2 | F-02 | HIGH | Error amplification: 26 MB request → 192 MB response, ~2 GB RSS, 164 MB persisted |
| 3 | F-10 | MEDIUM | No Host validation: DNS rebinding lets any web page drive the loopback API (and deliver F-01/F-02) |
| 4 | F-11 | MEDIUM | 64 idle TCP connections → plain-text 503 for everyone (≥ 10 s observed) |
| 5 | F-03 | MEDIUM | Lone-surrogate JSON escapes → 500 on 8 of 12 request shapes |
| 6 | F-05 | MEDIUM | 412 non-BMP characters accepted; PDF text layer reads a different name |
| 7 | F-06 | MEDIUM | Pillow 12.3.0 (runtime) 37 advisories; pypdf 6.20.0 (dev) 9 |
| 8 | F-09 | LOW | Valid NFD-encoded requests can be refused with 413, contrary to ADR-011 |
| 9 | F-07 | LOW | Idempotency replay backstop is unreachable (substring match) and untested |
| 10 | F-08 | LOW | `progress = 1.0` while still processing for jobs ≥ 20,000 rows |

**Numbers at a glance:** 19 findings (0 CRITICAL, 2 HIGH, 5 MEDIUM, 5 LOW, 7 INFO). 15 are CONFIRMED by execution, 2 are tool warnings, 2 are documented behaviour. 23 TDD red tests define the Stage 2 contract. Test runs: project 324/324 passed; audit 146 passed + 23 red (failing as designed); coverage 95 % → 96 % (in-process).

---

## 2. Scope and inventory

### 2.1 Files inspected (all first-party files were read in full)

| Area | Files | Lines |
|---|---|---|
| Application | `certgen/*.py` (20 modules) + `certgen/assets/` (2 vendored TTFs, licence) | 1,558 statements (coverage count) |
| Tests | `tests/*.py` (21 files, 184 test functions, 324 collected cases) | ~3,800 |
| Scripts | `scripts/benchmark.py`, `demo.sh`, `demo-live.sh`, `live_demo.py`, `check_live_demo.py` | ~900 |
| Demo UI (untracked) | `demo/index.html`, `app.js`, `style.css`, `favicon.svg` | ~640 |
| Docs | `problem_statement.{md,pdf}`, `REQUIREMENTS.md`, `BLUEPRINT.md` (1,132 lines), `BLUEPRINT_AMENDMENTS.md`, `BUILD_LOG.md`, `PLAN.md`, `RESEARCH.md`, `WALKTHROUGH.md`, `DEMO.md`, ADR-001…013, `docs/evidence/**` (benchmarks, gates M1–M11, kill demo, mutation checks, two prior audits, visual checks) | ~3,000 |
| Config | `pyproject.toml`, `requirements.txt` (20 pins), `requirements-dev.txt` (17 pins), `.gitignore` | — |

`problem_statement.pdf` was not re-transcribed; the audit used the committed transcription, which states it was checked word for word against `pdftotext` (NOT VERIFIED by this audit; risk: low, the transcription is consistent with every requirement row).

### 2.2 Baseline git state (EXECUTED)

- 24 commits on `main`; tags `blueprint-locked`, `m1-done`…`m10-done`, `v1.0-required`, `v1.0-rc`; branches `d2-proof`, `d3-fix-retry`, `d4-idempotency`, `dropped/d1-verify`.
- `git merge-base --is-ancestor v1.0-required HEAD` → true. `git ls-tree -r v1.0-required` contains no D1–D4 test files or 2p/4p evidence (§24.17 gate: **PASS**).
- `sha256sum docs/BLUEPRINT.md` = `93537bde…1758`, equal to `docs/BLUEPRINT.sha256` (freeze intact: **PASS**).
- Secret scan of all commits (`git log --all -p`, key/token/password patterns) and of tracked files (`audit/tests/test_a6_redteam.py::test_no_secrets_committed`): **no hits**.

### 2.3 Dependencies (pinned, from `requirements*.txt`)

Runtime: fastapi 0.143.0, starlette 1.7.0, uvicorn 0.54.0, pydantic 2.14.0 / pydantic_core 2.50.0, email-validator 2.3.0, SQLAlchemy 2.1.4, reportlab 5.0.1, **pillow 12.3.0** (transitive, reportlab), anyio 4.15.1, h11 0.16.0, idna 3.20, dnspython 2.9.0, opentelemetry-api 1.45.1, and 7 more transitive pins. Dev: pytest 9.1.1, pytest-timeout 2.4.0, httpx2 2.13.1, **pypdf 6.20.0**, ruff 0.17.0, mypy 2.4.0. Lockfile discipline: exact pins, full freeze (good). Licences: DejaVu (Bitstream Vera licence, vendored with text); all Python deps are permissive (MIT/BSD/Apache) — licence metadata not machine-audited (NOT VERIFIED, low risk).

### 2.4 Baseline test run (EXECUTED)

| Run | Command | Result | Duration |
|---|---|---|---|
| Project suite | `python -m pytest -q -p no:cacheprovider` | **324 passed, 0 failed, 0 skipped**, exit 0 | 12.0–17.2 s (3 runs) |
| Coverage | `coverage run --branch --source=certgen -m pytest` | **95 %** line+branch (1,558 stmts, 63 missed; 336 branches, 18 partial) — in-process only, subprocess code is not measured | 20.7 s |
| ruff check / format | `ruff check .` / `ruff format --check .` | clean / 45 files formatted, exit 0 | — |
| mypy | `mypy --strict certgen` | no issues (20 files), exit 0 | — |
| mypy (tests, scripts; not a project gate) | `mypy tests scripts` | 7 errors in 6 files | — |
| bandit | `bandit -r certgen scripts` | 47 Low (B101 assert ×7 in `certgen`, B404/B603 subprocess in scripts), 1 Medium (B310 `urlopen` in `scripts/check_live_demo.py:42`, fixed `http://127.0.0.1` URL → false positive) | — |
| pip-audit | `pip-audit -r requirements.txt` / `-r requirements-dev.txt` | **pillow 12.3.0: 37 advisories** (fix 13.0.0); **pypdf 6.20.0: 9 advisories** (dev only; fixes 6.20.1–6.21.2) | — |

Per-module coverage (baseline): api 99 %, archive 100 %, config 100 %, db 92 %, errors 98 %, fonts 88 %, layout 100 %, main 100 %, middleware 94 %, models 100 %, rendering 100 %, **service 93 % (idempotency IntegrityError backstop lines 141–146 never executed)**, status 100 %, storage 89 %, validation 98 %, **worker 87 %** (standalone `main()` runs only in subprocesses), `__main__` 0 % (subprocess only). Raw: `audit/evidence/02-baseline-coverage.txt`.

Evidence files: `audit/evidence/01-baseline-pytest.txt` … `05-pip-audit-*.json`.

---

## 3. Architecture reconstruction and documentation-vs-code consistency

### 3.1 Reconstructed architecture (verified against code)

```
client ──HTTP──► NoStoreMiddleware ─► BodySizeLimitMiddleware (buffers ≤ MAX_BODY_BYTES) ─► FastAPI router (sync def routes, threadpool)
                                                                                            │
   POST /v1/certificate-jobs: raw body → strict JSON (decode_json_body) → [Idempotency-Key fast path: read engine]
      → Pydantic envelope → validation.validate_request (pure; fonts + layout.fit_text) → 422 | dry-run 200 | service.create_job
      service.create_job: ONE write txn (BEGIN IMMEDIATE): key re-check → capacity count → INSERT job → executemany items → 202
   GET job / items (keyset) / certificate / archive: read engine (deferred BEGIN, WAL snapshot) → storage (lstat + O_NOFOLLOW)
   POST …/retry: one write txn: T9 for every failed item (capacity-checked)

worker (embedded threads + monitor, or `python -m certgen.worker` processes):
   claim_next: BEGIN IMMEDIATE { retire T8 → reclaim expired (T4) | oldest available pending (T3) → UPDATE … RETURNING }
   render (no txn) → MAX_PDF_BYTES → storage.save_atomic (unique tmp, fsync, os.replace, dir fsync)
   record_success / record_failure: UPDATE … WHERE id AND lease_token AND status='processing' (fence)
SQLite file (WAL, busy_timeout 5 s, FK on, user_version 1) + data/certificates/{job_id}/{certificate_id}.pdf (0700/0600)
```

Trust boundaries: (1) the HTTP client (unauthenticated, any process that can reach the bind address — and, through DNS rebinding, any web page the operator visits, F-10); (2) the data directory (assumed private to the service user; symlink defences exist); (3) third-party libraries (email-validator, ReportLab, Pillow) processing attacker-controlled strings (F-01, F-05, F-06). There is no authentication, no tenant model, no rate limiting (documented).

### 3.2 Documentation vs code (EXECUTED where stated)

| Claim (source) | Verdict | Evidence |
|---|---|---|
| "324 passed" (README conformance, M11 gate) | **PASS** | reproduced: 324 passed (3 runs) |
| Two engines; polling never blocks a claim (README, ADR-002) | **PASS** | project test `test_open_read_does_not_block_claim` passes; audit stress tests pass with concurrent polling |
| Claim/lease/fence; no double processing; crash-safe (README "Recovery and guarantees", ADR-004) | **PASS** | project slow tests + audit `test_a5_stress` (3 processes, SIGKILL) + `test_a4::test_api_process_sigkill_mid_job_then_restart_completes` |
| Stale file of a failed item is never served (BLUEPRINT §11.2) | **PASS** | `test_a3::test_stale_file_of_failed_item_is_never_served` |
| Routes are sync, so validation never blocks the event loop (README, WALKTHROUGH Q14) | **PASS** | during a 6.7 s validation over real HTTP, `/healthz` max latency 143 ms (`test_a4`, F-01 test output) |
| "A request within every documented limit can never be 413'd" (ADR-011, BLUEPRINT §12) | **FAIL** | F-09; README hedges ("normally encoded JSON") |
| `MAX_BODY_BYTES` addresses memory exhaustion; "a 26 MB body takes roughly 10× that in Python objects" (BLUEPRINT §12, ADR-011) | **FAIL** | F-02: 26 MB body → 1.9–2.0 GB peak RSS (~74×), 192 MB response |
| "Storing NULL … keeps a validation case from becoming a 500" (ADR-002) | **FAIL** | F-03: lone-surrogate strings → 500 |
| "Names … Latin, Greek and Cyrillic" (README `## Supported names`) | **PARTIAL** | the first clause ("any character both fonts can draw") is accurate; the allowlist admits 4,947 characters incl. Armenian, Georgian, Tifinagh, Lao, Braille, box drawing and 412 non-BMP symbols/emoji (census: `audit/evidence/08-probes.txt`); non-BMP ones break the PDF text layer (F-05) |
| "Importing this module does no I/O" (`certgen/main.py:1`) | **FAIL (minor)** | module-level `Settings.from_env()`: `CERTGEN_PORT=abc python -c "import certgen.main"` → `ValueError` at import; `Path.resolve()` and a warning log at import (F-14) |
| Benchmarks (README table, `docs/evidence/benchmark-*.json`) | **PASS** | re-run within +3.2 % / +3.5 % / +8.7 % wall time (§9.1) |
| Kill test 5 consecutive passes (README, D2) | **PASS** | `pytest -m slow` 5/5 (§9.3) |
| Mutation proofs (README, `docs/evidence/mutation-checks.txt`) | **PASS** | independent campaign: 31/34 killed, 3 survivors are redundant defences (§8.4) |
| Live demo verification (`docs/evidence/live-demo-checks.txt`) | **PASS** (scripted part) | `scripts/check_live_demo.py` re-run: 5/5 PASS; the browser/Playwright part was not re-run (NOT VERIFIED) |
| D1 dropped honestly; branch kept (README) | **PASS** | branch `dropped/d1-verify` exists; no D1 code on `main` |
| `/docs` "useful for review" (BLUEPRINT §6.10) | **INFO** | page loads unpinned `swagger-ui-dist@5` from `cdn.jsdelivr.net` without SRI; fails offline (F-18) |

## 4. Requirements traceability matrix

Verdicts: Implemented / Partial / Missing / Deviates / Undocumented extra. "Observed" cites executed evidence (project suite = P, audit suite = A).

| Req | Implementation | Tests | Observed result | Verdict |
|---|---|---|---|---|
| R1 bulk request | `api.create_job`, `service.create_job`, `models.CreateJobRequest` | P `test_jobs_api`, A `test_a4::test_readme_quickstart_flow`, A `test_a3::test_ten_thousand_rows_single_request` | 10,000 rows in one POST → 202 | **Implemented** |
| R2 validate recipient data | `validation.py`, `errors.py` | P `test_validation`, A `test_a2` (Hypothesis), A `test_a6` | per-row codes correct; **500 on lone surrogates** (F-03); **unbounded error lists** (F-02) | **Partial** |
| R3 one certificate per valid recipient | `worker.Worker.process`, `service.create_job` | P `test_worker`, A `test_a5` | 960 items/32 jobs under concurrency: exactly one terminal outcome each, one PDF per success | **Implemented** |
| R4 single template | `rendering.render_certificate` | P `test_rendering` | — | **Implemented** |
| R5 recipient info on the certificate | `rendering.py`, `layout.py` | P `test_rendering`, A `test_a1::test_accepted_name_round_trips_through_pdf_text_layer` | visual OK; **text layer wrong for non-BMP characters** (F-05) | **Partial** |
| R6 status per job and item | `tables.py`, `worker.py`, `status.py` | P `test_worker`, `test_job_status` | CHECK constraints hold; `PRAGMA integrity_check` ok after workloads | **Implemented** |
| R7 progress/result | `api.get_job`, `service._load_job`, `status.derive_job_status` | P `test_jobs_api`, A `test_a2` properties | **progress = 1.0 while processing for ≥ 20,000 valid rows** (F-08; needs non-default `CERTGEN_MAX_RECIPIENTS`) | **Partial (minor)** |
| R8 retrieve certificates (single + all) | `api.download_certificate`, `api.download_archive`, `archive.py`, `storage.py` | P `test_retrieval`, A `test_a3`, A `test_a4` | single PDF, ZIP + manifest over real HTTP | **Implemented** |
| R9 failure isolation | `worker.process` try/except, fenced transitions | P `test_failure_isolation`, A `test_a3::test_full_lifecycle…`, A `test_a5` | injected failures isolated; retries bounded | **Implemented** |
| R10 identify successes/failures | `service.list_items`, `archive.py` | P, A `test_a3` | manifest = item list, codes + safe messages | **Implemented** |
| R11 designed for many recipients | `worker.py`, `middleware.py`, `config.py`, keyset pagination | P `test_limits`, A `test_a1`, `test_a4` | background + pagination OK; **limits do not bound CPU (F-01), memory (F-02) or connections (F-11)**; body budget too small for NFD input (F-09) | **Partial** |
| R12 sync-vs-background documented | README `## Design decisions`, ADR-003 | P `test_docs` | — | **Implemented** |
| R13 Python + FastAPI + relational DB | `main.py`, `db.py` | P `test_docs`, `test_db` | — | **Implemented** |
| R14–R19 mandatory test areas | — | P (each area has ≥ 1 test; traceability guard green) | 324 pass | **Implemented** (quality: §8) |
| R20–R25 README sections | `README.md` | P `test_docs`; A `test_a4::test_readme_quickstart_flow` ran the README curl body against a real server | 202 + expected `INVALID_EMAIL` on row 1 | **Implemented** |
| P1 optional not at cost of required | tags | `git merge-base`, `git ls-tree` (EXECUTED) | PASS | **Implemented** |
| P2 explain/modify code | `docs/WALKTHROUGH.md` | human-only | NOT VERIFIED (human walkthrough/live modification pending per BUILD_LOG) | **NOT VERIFIED** |
| P3 pip + one command | `requirements.txt`, `python -m certgen` | A `test_a4` (every E2E test starts `python -m certgen` in a fresh data dir) | PASS | **Implemented** |
| — | `CERTGEN_PUBLIC_BASE_URL` / `jobs.verify_base_url` stored but unused (D1 dropped) | — | documented in README Configuration | **Undocumented extra? No — documented dead data (INFO)** |
| — | Live demo (`scripts/live_demo.py`, `demo/`) — untracked | `scripts/check_live_demo.py` | 5/5 PASS (real server, worker kill, retry) | **Undocumented extra in HEAD** (documented only in the uncommitted README) |

## 5. Findings register

Ranked by severity. Effort: S < ½ day, M ≤ 2 days, L > 2 days. "Red test" = the failing acceptance test written in Stage 1 (§13).

| ID | Sev | Category | Status | File:Function:Line | Summary | Red test | Effort |
|---|---|---|---|---|---|---|---|
| F-01 | **HIGH** | Security / Performance (CWE-407) | CONFIRMED | `certgen/validation.py:_check_email:300` | Unbounded email string passed to email-validator, whose syntax pass is O(n²): one request can pin a worker thread and a CPU core for hours | `test_a1::test_overlong_email_is_rejected_in_bounded_time`, `test_a4::test_overlong_email_over_http_is_fast_and_does_not_starve_others` | S |
| F-02 | **HIGH** | Security / Reliability (CWE-400, CWE-770) | CONFIRMED | `certgen/validation.py:_validate_rows:244-248`; echoed by `api.create_job:177`, `errors.problem_response:126`; persisted by `service._item_values:237` | One error object per unknown row key, unbounded: a 26 MB body yields a 192 MB response, ~2 GB peak RSS and 164 MB persisted | `test_a1::test_unknown_field_errors_are_bounded` | S–M (cap is a product decision) |
| F-03 | MEDIUM | Correctness / Error handling | CONFIRMED | `certgen/validation.py:decode_json_body:43-65` (root); fails at `service.request_hash:81`, sqlite3 bind in `service._insert_items:221`/`_insert_job:193`, JSON encode in `errors.problem_response:130` | JSON `\ud800`-style escapes produce lone surrogates; 8 of 12 tested request shapes return 500 INTERNAL_ERROR | `test_a1::test_lone_surrogate_never_500` (8 red), `test_a2::test_canonical_form_of_any_accepted_body_is_utf8_encodable` | S |
| F-05 | MEDIUM | Data integrity (R5) | CONFIRMED | `certgen/fonts.py:21-23` (`SUPPORTED_CHARS` includes 412 non-BMP code points); ReportLab 5.0.1 ToUnicode writer | Non-BMP characters (emoji, math, cards…) are accepted; the PDF draws them, but the ToUnicode CMap entry is malformed (`<02> <1F63C>`), so the text layer reads "Zoë ὣ Smith" (pdftotext) / "Zoë  Smith" (pypdf) | `test_a1::test_accepted_name_round_trips_through_pdf_text_layer` | S |
| F-06 | MEDIUM | Supply chain | TOOL WARNING | `requirements.txt:12` (pillow 12.3.0), `requirements-dev.txt` (pypdf 6.20.0) | 37 advisories on Pillow (runtime, imported by ReportLab at render time; no user-controlled image decoding found → reachability not demonstrated); 9 on pypdf (dev/test only) | PROPOSED: CI `pip-audit` gate | S |
| F-10 | MEDIUM | Security (CWE-350 / DNS rebinding) | CONFIRMED | `certgen/main.py:create_app:67-79` (no Host validation) | Any `Host` header is served; a malicious web page can rebind its hostname to 127.0.0.1 and drive the loopback API (submit jobs, trigger F-01/F-02, read `/openapi.json`, read jobs whose ids it knows) | `test_a4::test_foreign_host_header_is_rejected` | S (allowlist config = product decision) |
| F-11 | MEDIUM | Availability (CWE-400) | CONFIRMED | `certgen/main.py:serve:87-94` (`limit_concurrency=64`, no idle-connection timeout) | 64 TCP connections that never send a byte make every new request get uvicorn's plain-text 503; observed for ≥ 10 s (longer duration NOT VERIFIED) | `test_a4::test_idle_connections_do_not_lock_out_clients` | S–M |
| F-04 | LOW | Input validation | CONFIRMED | `certgen/api.py:list_items:237` (`after: int, ge=-1`, no upper bound) | `?after=` > 2⁶³−1 → `OverflowError` in the sqlite3 bind → 500 | `test_a1::test_items_after_out_of_range_is_422` | S |
| F-07 | LOW | Correctness (latent) / Test gap | CONFIRMED | `certgen/service.py:_insert_job:209` | `"jobs.id" in str(exc.orig)` also matches `"jobs.idempotency_key"`; a key UNIQUE violation is retried 5× as an id collision and raises `RuntimeError` (500), so the documented replay backstop (`create_job:139-146`) is unreachable; lines 141–146 are never executed by the project suite | `test_a1::test_key_violation_reaches_replay_backstop` | S |
| F-08 | LOW | Correctness (R7) | CONFIRMED | `certgen/status.py:derive_job_status:23` | `round(done/valid, 4)` reports `progress = 1.0` with `status = processing` when valid ≥ 20,000 (19,999/20,000 = 0.99995 rounds up; allowed: `CERTGEN_MAX_RECIPIENTS` ≤ 100,000) | `test_a1::test_progress_is_below_one_until_terminal`, `test_a2::test_progress_one_only_when_terminal` | S |
| F-09 | LOW | Spec deviation (BLUEPRINT §12, ADR-011) | CONFIRMED | `certgen/config.py:19-20` (`BODY_BYTES_PER_RECIPIENT = 2_600`) | Budget assumes one code point per character; NFD input (up to 4 code points per NFC character) of rows that are valid after normalization exceeds it → 413 for a valid request | `test_a1::test_body_limit_admits_every_valid_request` | S (or document) |
| F-12 | INFO | Security (design) | CONFIRMED (documented limitation) | `certgen/service.py:_job_for_key:91-100` | Global, never-expiring Idempotency-Keys: key-existence oracle (409 vs 202) and key squatting (a pre-registered key refuses the owner forever) | `test_a6::test_idempotency_key_existence_oracle_and_squatting` (documents current behaviour) | M (needs auth/tenancy) |
| F-13 | INFO | Configuration hygiene | CONFIRMED (static + Settings call) | `certgen/config.py:RANGES:45-55` | Test-only `CERTGEN_TEST_SLOW_RENDER_MS` (≤ 60 s) is accepted in production with no relation to `CERTGEN_LEASE_SECONDS` (≥ 1 s): a misconfiguration turns every item into lease expiries / `WORKER_LOST` | PROPOSED | S |
| F-14 | INFO | Maintainability | CONFIRMED | `certgen/main.py:97` | Import-time `Settings.from_env()` contradicts the module docstring; an invalid env var breaks `import certgen.main` (tests, scripts, tooling) | PROPOSED | S |
| F-15 | INFO | Fault tolerance | CONFIRMED (observation) | `certgen/storage.py:read:104-114` | No integrity check on read: a PDF damaged on disk is served with 200 | `test_a3::test_truncated_file_on_disk_is_served_as_is` (documents behaviour) | S |
| F-16 | LOW | Test architecture | CONFIRMED | coverage report; mutation campaign (§8.4) | Subprocess-only code is unmeasured (worker `main()` 0 % in-process); backstop path untested (F-07); see mutation survivors | §8.4 | S–M |
| F-17 | INFO | DevOps | CONFIRMED | working tree | README modified and demo files untracked: HEAD and working tree disagree; committing README alone would publish broken links | — | S |
| F-18 | INFO | Supply chain | CONFIRMED | FastAPI default `/docs` | Swagger UI from `cdn.jsdelivr.net/npm/swagger-ui-dist@5` (floating major, no SRI); `/docs` needs internet | — | S |
| F-19 | INFO | Static analysis | TOOL WARNING | `certgen/errors.py:165,171,181,200`, `certgen/service.py:163,176,399` | bandit B101: `assert` used for type narrowing; under `python -O` they vanish (behaviour unchanged in every case inspected). B310 in `scripts/check_live_demo.py:42` is a false positive (constant loopback URL) | — | S |

Counts by severity: CRITICAL 0 · HIGH 2 · MEDIUM 5 · LOW 5 · INFO 7 (19 total; 15 CONFIRMED, 2 TOOL WARNING, 2 CONFIRMED-as-documented-behaviour). Mutation-testing gaps are listed in §8.4.
By category: Security 5 (F-01, F-02, F-10, F-11, F-12) · Correctness/validation 5 (F-03, F-04, F-07, F-08, F-09) · Data integrity 1 (F-05) · Supply chain 2 (F-06, F-18) · Test/maintainability/ops 6 (F-13–F-17, F-19).

## 6. Functional and architectural defects (full records)

Format: Entry point → Preconditions → Input → Expected → Actual → Root cause → Impact → Evidence → Repro → Recommended fix (text only) → Regression test → Dependencies/risks.

### F-03 — Lone UTF-16 surrogates crash request handling (MEDIUM, CONFIRMED)
- **Entry point:** `POST /v1/certificate-jobs` (plain and with `Idempotency-Key`; dry run only for keys echoed as `field`).
- **Input:** legal JSON containing a lone surrogate escape, e.g. `{"certificate":{…},"recipients":[{"name":"A","email":"a@example.com"},{"name":"B\ud800","email":"b@example.com"}]}`.
- **Expected:** 202 with row 1 `rejected/INVALID_CHARACTERS`, or 422 `INVALID_JSON` (ADR-002: invalid input must not become a 500).
- **Actual:** `500 INTERNAL_ERROR` in 8/12 tested shapes. Traces: `UnicodeEncodeError: 'utf-8' codec can't encode character '\ud800' … surrogates not allowed` raised from (a) `service.request_hash` (`.encode("utf-8")` of the canonical JSON) when a key is sent, (b) the sqlite3 bind of the rejected row's `name`/`email`/`input_json`/`errors`, (c) Starlette `JSONResponse.render` when the raw key is echoed in `errors[].field` (all-invalid 422 and dry run).
- **Root cause:** `decode_json_body` decodes bytes strictly (a raw UTF-8-encoded surrogate *is* rejected) but `json.loads` turns `\uD800` escapes into lone surrogates; nothing downstream expects non-encodable `str`.
- **Impact:** error-contract violation, an exception traceback in the log per request, trivially triggerable; no partial writes (the create transaction rolls back) — verified by the red test leaving no job.
- **Repro:** `python -m pytest audit/tests/test_a1_red_contract.py -k surrogate`.
- **Fix:** after `json.loads`, walk keys and string values and reject any code point in U+D800–U+DFFF with `INVALID_JSON` ("lone surrogate"), or attempt `canonical_json(value).encode("utf-8")` inside `decode_json_body` and map the failure to `INVALID_JSON`.
- **Regression tests:** the 12 parametrized cases + the Hypothesis property.

### F-04 — `?after=` overflow (LOW, CONFIRMED)
- `GET /v1/certificate-jobs/{id}/items?after=9223372036854775808` → `OverflowError: Python int too large to convert to SQLite INTEGER` → 500. Expected 422 `INVALID_VALUE` on `after` (BLUEPRINT §6.5). Fix: `Query(ge=-1, le=2**63-1)` (or the max `row_index`, `MAX_RECIPIENTS`). Same check for any future integer query parameter.

### F-07 — Replay backstop unreachable (LOW, CONFIRMED, latent)
- `service._insert_job` treats any IntegrityError whose message contains `jobs.id` as a job-id collision. SQLite's message for the key constraint is `UNIQUE constraint failed: jobs.idempotency_key`, which contains `jobs.id`. Executed proof: with the existing key inserted, a direct `_insert_job(…, key="K")` drew 5 job ids and raised `RuntimeError: no unique job id after 5 attempts` instead of propagating `IntegrityError`; red test forces the race the comment in `create_job:140` describes and gets 500 instead of the documented 200 replay.
- Reachability: the in-transaction re-check under `BEGIN IMMEDIATE` normally prevents it (the blueprint states the backstop is "unreachable"). Risk is in future changes (e.g. a PostgreSQL port without `BEGIN IMMEDIATE`, §22).
- Fix: compare the constraint exactly (`str(exc.orig).endswith("jobs.id")` / `== "UNIQUE constraint failed: jobs.id"`), mirror for `items.certificate_id`, add the red test.

### F-08 — progress reaches 1.0 early (LOW, CONFIRMED)
- `derive_job_status({"succeeded": 99_999, "pending": 1}, 99_999)` → `("processing", 1.0)`. Any `valid ≥ 20,000` with one item left rounds to 1.0 (probe: 10,000 → 0.9999; 20,000, 20,001, 100,000 → 1.0). Default `MAX_RECIPIENTS` (10,000) is not affected; the allowed maximum (100,000) is. Fix: `math.floor(done / valid * 10_000) / 10_000` or clamp to 0.9999 unless `done == valid`.

### F-09 — body budget too small for normalization-equivalent input (LOW, CONFIRMED)
- 1,000 rows each valid after NFC (119-character polytonic-Greek name with 4 code points per character in NFD; 254-character email), serialized with Python's default `json.dumps`, = 2,884,126 bytes vs limit 2,665,536 → 413. The rule "never 413'd" (ADR-011, BLUEPRINT §12) is false; the README's "normally encoded JSON" hedge does not mention normalization. Fix options (product decision): raise the per-row budget to the true worst case (120 × 4 × 6 + 254 × 6 + ~40 ≈ 4,450 B) or document the NFC assumption.

### F-05 — non-BMP characters accepted; PDF text layer wrong (MEDIUM, CONFIRMED)
- **Input:** name `Zoë 😼 Smith` (U+1F63C). **Expected** (README scope): rejected `UNSUPPORTED_CHARACTERS`, or a PDF whose text is the name. **Actual:** accepted; the glyph is drawn correctly (rasterized check), but the font's ToUnicode CMap contains `<02> <1F63C>` — an odd-length hex string, invalid as UTF-16BE. `pdftotext` → `Zoë ὣ Smith` (U+1F63, a Greek letter), pypdf → `Zoë  Smith` with "Odd-length string" warnings. 412 non-BMP code points are in `SUPPORTED_CHARS` (cmap ∩ cmap of both DejaVu weights).
- **Impact:** copy/paste, search, screen readers and any text-based verification read a different name than the one requested and printed; the project's own R5 proof method (pypdf extraction) fails for these names.
- **Fix:** restrict `SUPPORTED_CHARS` to `ord(c) <= 0xFFFF` (simplest; matches README scope), or post-process the ToUnicode stream to UTF-16 surrogate pairs. Re-run determinism tests.

## 7. Security and threat-model assessment

Threat model (BLUEPRINT §12, README Limitations): no authentication; loopback bind by default; 128-bit random ids act as capability URLs; limits on body, rows, fields, page size, archive builds; strict JSON; problem+json without internals; private files; symlink refusal.

| Control | Status | Evidence |
|---|---|---|
| Injection (SQL) | **PASS** | bound parameters everywhere (code review of every `text()`), `test_a6::test_sql_injection_is_stored_verbatim` (4 payloads, tables intact) |
| Path traversal / malformed ids | **PASS** | 36 encoded variants × 4 routes → 404 (`test_a6`); `HEX32.fullmatch` before every path build |
| IDOR / enumeration | **PASS** (by design) | ids are `uuid4`/`token_hex(16)`; neighbour ids 404 (`test_a6::test_idor_requires_unguessable_ids`) |
| CSRF / cross-site simple requests | **PASS** | text/plain and form bodies → 415; no CORS headers (`test_a6`). `POST …/retry` with an empty body has no media-type check, but needs an unguessable job id |
| DNS rebinding / Host validation | **FAIL** | F-10 |
| XSS | **PASS** (API is JSON-only); demo UI escapes all interpolated values (`demo/app.js:129` `escape()` used at every `innerHTML` sink: lines 147, 217, 278–286) |
| CSV injection (manifest) | **PASS** | `=`, `+`, `-`, `@`, full-width variants neutralized, incl. emails like `=cmd@example.com` (`test_a3`); Hypothesis property on `neutralize` |
| Header injection | **PASS** | `Idempotency-Key` CR/LF/NUL/DEL/non-ASCII → 422 (`test_a6`); `Content-Disposition` built from hex ids only |
| PDF injection | **PASS** | `Robert') (Tables\ ) Tj ET <<` renders and round-trips (`test_a3::test_pdf_string_escaping_round_trips`) |
| Bidi / homoglyph spoofing | **PASS** for control characters and RTL (9 payloads rejected, `test_a6`); **INFO**: cross-script homoglyphs (Latin `a` vs Cyrillic `а`) are allowed by design |
| Resource exhaustion — CPU | **FAIL** | F-01 |
| Resource exhaustion — memory/disk | **FAIL** | F-02 |
| Resource exhaustion — connections | **FAIL** | F-11 |
| Error-message leakage | **PASS** | storage error with a path + email in its message → item shows `OSError: Certificate generation failed.` only (`test_a6`); 500 bodies are generic |
| Secrets | **PASS** | none in history or tracked files |
| Supply chain | **WARN** | F-06, F-18 |
| Privacy | **INFO** | emails printed by default (documented); archive temp files land in `TMPDIR` with mode 0600 (tempfile default, code review) |

### F-01 — Quadratic email validation (HIGH, CONFIRMED)
- **Entry point:** `POST /v1/certificate-jobs` (also `?dry_run=true`), any row's `email`.
- **Preconditions:** none (no auth). Body ≤ 26,065,536 bytes.
- **Input:** a row with `"email": "a" * n + "@example.com"`.
- **Expected:** `INVALID_EMAIL` in milliseconds (an email is at most 254 characters).
- **Actual (EXECUTED):** standalone `validate_email` — 128k chars 0.164 s, 256k 0.598 s, 512k 2.432 s (×~4 per doubling = O(n²)); in-process API 1,000,000 chars → 12.7 s; real HTTP 600,000 chars → 6.73 s (3.82 s on a second run); a request with a ~26 M-char email did not finish within a 300 s timeout (killed). cProfile hot spot: `email_validator/syntax.py:50 split_string_at_unquoted_special` (+ `unquote_quoted_string`).
- **Extrapolated (not measured):** (26.07 M / 512 k)² × 2.43 s ≈ 6,300 s ≈ 1.75 h of CPU for one maximal request.
- **Impact:** one request ties up one threadpool thread and one core; concurrent requests multiply it; with enough of them all `def` routes queue behind the threadpool (default anyio limit not measured — NOT VERIFIED). The event loop itself stays responsive (`/healthz` ≤ 143 ms during the 6.7 s request).
- **Root cause:** certgen relies on email-validator to enforce the 254-character limit, but the library parses the whole string before checking length.
- **Fix:** reject `len(email) > 254` (and, defensively, any field longer than its limit × a small factor) **before** calling `validate_email`; consider a per-field raw-length cap during JSON decoding.
- **Regression tests:** `test_a1::test_overlong_email_is_rejected_in_bounded_time` (< 2 s for 1 M chars), `test_a4::test_overlong_email_over_http…`.

### F-02 — Unbounded error amplification (HIGH, CONFIRMED)
- **Entry point:** `POST /v1/certificate-jobs` (also dry run, replay via `rejected_row_errors`, archive manifest).
- **Input:** a row object with N unknown keys (`{"k0000000":0, …}`), N ≈ 2 M within the body limit.
- **Expected:** bounded response/persistence (BLUEPRINT §12 lists `MAX_BODY_BYTES` as the memory-exhaustion control).
- **Actual (EXECUTED, `ulimit -v 6 GB`):** all-invalid → 422, request 26,065,414 B, response **192,482,723 B (7.4×)**, **peak RSS 1,914 MB**, 12.7 s. With one valid row → 202, same response size, **2,025 MB** RSS, 15.7 s, **164,411,609 B persisted** in `items.errors` for one row. Baseline 10,000-row valid request: 106 MB RSS, 0.93 s.
- **Impact:** a handful of concurrent requests can exhaust memory (concurrent run NOT VERIFIED); permanent disk growth with no retention; every replay of that job re-sends 192 MB.
- **Root cause:** one `FieldError` per unknown key (`_validate_rows`), no cap on errors per row or per response, no cap on keys per row object.
- **Fix:** cap reported unknown fields per row (e.g. first 10 + a summary error with the count), cap total errors per response, or reject row objects with more than a small number of keys as a structural 422. The cap values are a product decision.

### F-10 — No Host validation (MEDIUM, CONFIRMED)
- `GET /healthz` and `POST /v1/certificate-jobs` with `Host: attacker.example` → 200/202 from a real server (`test_a4`). The live-demo launcher already uses `TrustedHostMiddleware` (`scripts/live_demo.py:184`); the production app does not. Fix: Starlette `TrustedHostMiddleware` with `localhost`, `127.0.0.1`, `[::1]` plus a configurable list.

### F-11 — Idle-connection lockout (MEDIUM, CONFIRMED)
- 64 `socket.create_connection()` calls that send nothing → `/healthz` returns `503 Service Unavailable` (plain text) on 11/11 attempts over 10 s (`test_a4`). Partially stalled uploads did not trigger it in an exploratory run. The README documents the 64-connection limit but not that idle sockets count toward it indefinitely. Fix: put a reverse proxy with header/idle timeouts in front, or lower-level server settings (h11 header timeout); raise the limit; document. Needs a deployment decision.

### 7.1 Audit dimensions (Phase 2, A–L)

| Dim | Status | Justification / findings |
|---|---|---|
| A Functional correctness | **PARTIALLY VERIFIED → FAIL on edges** | Core flows correct under unit, property, integration, E2E and stress tests; edge defects F-03, F-04, F-07, F-08 |
| B Architecture | **PASS** | Layering matches BLUEPRINT §3 (no SQL in api.py, no FastAPI in service/worker except via `errors.py`, documented as G18); no circular imports (imports load in a clean interpreter); one deviation: import-time settings (F-14) |
| C Code quality | **PASS (minor)** | Small, well-named modules, strict mypy clean; dead code `Storage.exists` (no caller); asserts for narrowing (F-19) |
| D Error handling / resilience | **PARTIALLY VERIFIED** | Per-item isolation, bounded retries, rollback on create, fenced completion all verified; unhandled `UnicodeEncodeError`/`OverflowError` → 500 (F-03, F-04); no integrity check on read (F-15) |
| E Security (static) | **FAIL** | No authn (documented). Injection, traversal, CSV, header, PDF escaping PASS; F-01, F-02, F-10, F-11 |
| F Performance | **FAIL (adversarial) / PASS (nominal)** | Nominal throughput reproduces (§9); adversarial inputs: quadratic CPU (F-01), ~74× memory amplification (F-02) |
| G Concurrency / state | **PASS** | BEGIN IMMEDIATE claim, per-claim token fence, retire step, concurrent retry, concurrent same-key POSTs; audit stress: 32 concurrent jobs + 4 threads + transient failures, 3 processes + SIGKILL, API SIGKILL + restart — all invariants hold |
| H Data integrity | **PARTIALLY VERIFIED** | CHECK constraints, FK, `integrity_check` ok after workloads; F-05 (PDF text layer), F-02 (unbounded `items.errors`) |
| I Dependencies / supply chain | **FAIL (tool)** | F-06 (Pillow 37 advisories runtime, pypdf 9 dev), F-18; exact pins and full freeze are good practice |
| J DevOps / operations | **PARTIALLY VERIFIED** | Reproducible install from pins (audit venv built from them); README commands work; no CI configuration in the repo (gates are run by hand, transcripts committed); logs carry ids/codes; tracebacks of unhandled errors are logged (expected); F-13, F-17 |
| K Documentation / research integrity | **PARTIALLY VERIFIED** | Very thorough and mostly accurate; contradicted claims listed in §3.2 (F-02, F-03, F-05, F-09, F-14) |
| L Test architecture | **PASS (strong) with gaps** | Behaviour-focused, deterministic (FakeClock, barriers), traceability guard, mutation proofs; independent campaign 31/34 killed, 3 equivalent-by-redundancy; gaps: subprocess code unmeasured, backstop untested (F-07, F-16), tests not type-checked (7 mypy errors) |

## 8. Test results

### 8.1 Suites executed (all EXECUTED in the isolated copy; command: `python -m pytest <path> -p no:cacheprovider`)

| Suite | Kind | Result | Notes |
|---|---|---|---|
| `tests/` (project) | unit / integration / E2E-ish / slow subprocess | **324 passed**, 0 failed, 0 skipped | 12.0–17.2 s |
| `audit/tests/test_a1_red_contract.py` | TDD red spec | **18 failed (as designed), 3 passed** | the 3 passing cases are dry-run variants of F-03 that are genuinely safe (they neither hash nor store input) and stay as regression guards |
| `audit/tests/test_a2_unit_property.py` | unit + Hypothesis (≤ 300 examples each) | **30 passed, 2 failed (red: F-03, F-08)** | properties: strict JSON decoding never raises except `JsonBodyError`; JSON round trip; `norm` idempotent/canonical; charset rule = its definition; `fit_text` never loses/reorders/overflows; any ≤ 254 ASCII email fits; accepted emails printable ASCII; `validate_request` structural invariants; Pydantic error mapping never raises; any accepted BMP name renders; status invariants; CSV guard; id check; `iso_ms` round trip; `Settings.from_env` fails only with `ValueError` |
| `audit/tests/test_a3_integration.py` | integration (HTTP → service → SQLite → worker → storage → archive) | **27 passed** | full lifecycle with failures + retry + byte-identical re-render; keyset walk for 5 page sizes; 10,000-row request; concurrent `/retry` (4 threads → one 202); retry body rules; manifest formula emails; PDF escaping; stale file of a failed item never served; symlinked job dir refused on write; fault injection: truncated PDF (served, F-15), corrupt DB (fails loudly), read-only DB (problem+json, reads still work); methods; `no-store`; DB/file invariants |
| `audit/tests/test_a4_e2e_server.py` | E2E, real `python -m certgen` | **4 passed, 3 failed (red: F-10, F-11, F-01)** | README quick-start flow; API **SIGKILL mid-job → restart → completed, 8 PDFs, no tmp**; 413 over real HTTP (Content-Length and chunked); graceful SIGINT exit 0 |
| `audit/tests/test_a5_stress.py` | concurrency / stress | **2 passed** | 8 producer threads × 4 jobs × 30 rows with 4 embedded workers and 10 % transient failures → all 960 items `completed`, attempts ∈ {1, 2} exactly as injected; 3 worker processes + random SIGKILL → 150/150, max attempts 2, survivors exit 0 |
| `audit/tests/test_a6_redteam.py` | adversarial | **80 passed** | every attack in §11 that is expected to be blocked was blocked |

Combined coverage of project + audit suites: 96 % (§8.3).

### 8.2 Untested-path inventory (from coverage + review)
- `service.create_job:141-146` IntegrityError backstop (never executed; latent bug F-07).
- `worker.main()` and `certgen/__main__.py` run only in subprocesses (exercised by project slow tests and audit E2E, but not measured).
- `storage.py:59-60, 111-112, 126-127` (race branches: job dir vanished, file replaced after check, concurrent sweep) — not reachable deterministically without fault injection at syscall level.
- `db._enable_wal` retry loop (lines 58–61) — exercised only by concurrent first boots (project slow test), not measured.
- `fonts.py:28` (startup assertion) — unreachable with the vendored fonts.

### 8.3 Coverage after the audit suites (EXECUTED, `audit/evidence/02b-combined-coverage.txt`)
Project suite + audit suites, in-process only: **96 %** line+branch (57 statements and 13 branches missed, from 63/18). Modules now at 100 %: `api.py` (was 99 %), `archive`, `config`, `layout`, `main`, `models`, `rendering`, `status`, `tables`; `storage` 89 → 92 %, `validation` 98 → 99 %, `worker` 87 → 88 %. Still unexecuted: `service.py:141-146` (the F-07 red test fails *before* reaching it — itself evidence that the backstop is unreachable), `worker.main` (subprocess only), syscall-race branches in `storage.py`.

Full audit run (EXECUTED, `audit/evidence/07-audit-suites.txt`): `python -m pytest audit/tests` → **23 failed (exactly the red catalogue in §13), 146 passed**, 55 s.

### 8.4 Independent mutation campaign (EXECUTED; `audit/tools/mutate.py`, results `audit/evidence/06-mutation-campaign.tsv`)
34 hand-written mutants targeting areas the project's own proofs (`docs/evidence/mutation-checks.txt`, 11 mutants) did not cover; each run against `pytest tests -m "not slow" -x`.

| Result | Count | Mutants |
|---|---|---|
| KILLED | **31** | M01–M09, M12–M23, M25–M34 (fence, lease boundary, retry delay, both capacity checks, CSV guard, ASCII email, duplicate case, depth, date slack, pagination end, J5, PDF cap, base-URL userinfo, Retry-After, failed-download state, RTL, name length, retry attempts reset, idempotency hash, chunked counting, extra attempt, key length, NBSP, sweep age, busy_timeout, FIFO, dry-run flag, email trim, token reuse) |
| SURVIVED | **3** | **M10** job-dir `lstat` symlink check removed — still refused because `_job_dir_is_safe` also requires `resolve().parent == root` (redundant defence, equivalent on this platform); **M11** file `lstat` check removed — still refused by `O_NOFOLLOW` on POSIX (the lstat check is the only defence where `O_NOFOLLOW` is 0, e.g. Windows; untested there); **M24** Content-Length pre-check removed — still 413 because bytes are counted, but the server now reads up to `MAX_BODY_BYTES` before refusing; no test asserts early rejection |

Verdict: the project's tests are strong; the three survivors are defence-in-depth redundancy, not missing assertions. PROPOSED tests: assert a declared-oversize request is answered without the body being sent (kills M24); a Windows/`O_NOFOLLOW=0` monkeypatched variant of the symlink test (kills M11).

## 9. Performance, concurrency and reliability

### 9.1 Documented benchmarks re-run (EXECUTED: `scripts/benchmark.py --recipients 1000 --processes N --work-dir <ext4 dir under audit/, deleted afterwards>`; raw JSON in `audit/evidence/09-benchmarks-slow-repeat-live-demo.txt`)

| Processes | Wall (s) documented → re-run | Throughput (certs/s) | Render p50 (ms) | Per item p50 / max (ms) | POST (ms) | Acceptance |
|---|---|---|---|---|---|---|
| 1 | 11.95 → **12.33** (+3.2 %) | 83.7 → **81.1** | 4.2 → 4.3 | 10.8 / 38.2 → 11.2 / 28.8 | 114 → 124 | all true |
| 2 | 7.18 → **7.43** (+3.5 %) | 139.3 → **134.5** | 4.1 → 4.3 | 11.3 / 35.0 → 11.6 / 22.7 | 119 → 117 | all true |
| 4 | 5.97 → **6.49** (+8.7 %) | 167.6 → **154.2** | 4.1 → 4.4 | 11.3 / 33.4 → 11.4 / 24.6 | 113 → 124 | all true |

Verdict: **reproducible** (same machine class, within single-digit %; the re-run is marked `git_dirty: true` because the copy includes the uncommitted working tree). REQUIREMENTS A10 target "1,000 recipients in < 60 s with one worker": **PASS** (12.3 s). Max per-item time ≪ 10 % of the 30 s lease: PASS. Sublinear scaling explanation (single SQLite writer, fsync per commit) is consistent with the numbers.

### 9.2 Adversarial performance (EXECUTED)
- F-01: O(n²) email validation — 512 k chars 2.35–2.43 s standalone; 1 M chars 12.7 s in-process; 600 k chars over HTTP 6.73 s and 3.82 s (two runs; variance from host load); event loop stayed responsive (`/healthz` max 143 ms / 201 ms).
- F-02: 26 MB → 1.83–2.06 GB peak RSS, 192 MB responses, 11–16 s per request (three runs across the session).
- Baseline 10,000-row valid request: 0.93 s, +24 MB RSS (106 MB peak).

### 9.3 Concurrency and reliability (EXECUTED)
- Project slow tests (`pytest -m slow`): **5/5 consecutive runs passed** (5 tests each, 5.8–6.2 s) — matches the README D2 claim.
- `scripts/check_live_demo.py` (real server, real worker kill): **all 5 PASS lines**, exit 0, 18 s.
- Audit stress and E2E (§8.1): no lost item, no duplicate claim, no duplicate PDF, no leftover temp file, `PRAGMA integrity_check` ok, in every scenario (threads, processes, SIGKILL of a worker, SIGKILL of the API with embedded workers and restart).
- Known, documented reliability limits confirmed by code review: no lease renewal (stall > lease ⇒ re-run, `WORKER_LOST` after 3), wall-clock leases, FIFO starvation of small jobs behind large ones, `/healthz` blind to standalone workers.

## 10. Dependency, config, build and DevSecOps

| Area | Status | Notes |
|---|---|---|
| Pins / lockfile | PASS | exact pins, full freeze; audit venv reproduced from them on CPython 3.11.9 |
| Known vulnerabilities | **FAIL (TOOL WARNING)** | F-06: Pillow 12.3.0 → 13.0.0 (runtime), pypdf 6.20.0 → ≥ 6.21.2 (dev). The pin policy (BLUEPRINT §2) requires an amendment for any bump |
| Unused dependencies | PASS (spot check) | every top-level runtime package is imported; `opentelemetry-api` is a transitive pin (pulled by a dependency), not imported by certgen |
| Build reproducibility | PASS | no build step; `pip install -r requirements.txt` + `python -m certgen` |
| CI/CD | **NOT VERIFIED / absent** | no CI config in the repo; quality gates are manual with committed transcripts (`docs/evidence/gates/`). Risk: gates depend on discipline (BUILD_LOG M10 records one slip) |
| Config hygiene | PASS with notes | all settings validated at startup with the variable named; F-13 (test knob in production), F-14 (import-time parsing); `CERTGEN_HOST=0.0.0.0` is accepted without warning despite no auth (documented limitation) |
| Logging | PASS with note | ids/codes only on the worker path; `hide_parameters=True`; unhandled exceptions are logged with tracebacks (F-03 produces one per request — log-flooding vector) |
| File permissions | PASS | `umask 077`, 0700/0600 verified by project test; note: pre-existing `data/` (created by scripts before umask) is 0775 on this machine, its contents are 0600/0700 |
| Observability | INFO | `/healthz` cannot see standalone workers (documented); no metrics |

## 11. Red-team scenarios and reproducible failures

All attacks ran against local, disposable instances with synthetic data (`audit/tests/test_a6_redteam.py`, `test_a4_e2e_server.py`, exploratory probes for F-01/F-02).

| # | Entry point | Attack | Expected | Actual | Result |
|---|---|---|---|---|---|
| 1 | `POST /v1/certificate-jobs` | 1 M / 600 k / 26 M-char email | fast `INVALID_EMAIL` | 12.7 s / 6.73 s / > 300 s | **Exploitable — F-01** |
| 2 | `POST` | ~2 M unknown keys in one row (26 MB) | bounded response | 192 MB response, ~2 GB RSS, 164 MB stored | **Exploitable — F-02** |
| 3 | `POST` | JSON `\ud800` escapes in key/name/email/value | 202/422 | 500 (8/12 shapes) | **Exploitable — F-03** |
| 4 | `GET …/items` | `after=2^63` | 422 | 500 | **F-04** |
| 5 | `POST` | non-BMP emoji in a name | reject or correct text | accepted, wrong text layer | **F-05** |
| 6 | any route | `Host: attacker.example` (DNS rebinding precondition) | 400 | 200 | **F-10** |
| 7 | TCP | 64 idle sockets | service stays available | plain-text 503 for ≥ 10 s | **F-11** |
| 8 | `Idempotency-Key` | pre-register a known/guessable key | — | owner refused with 409 forever; 409/202 reveals key existence | **F-12 (documented)** |
| 9 | all id routes | traversal / encoded / null / unicode-slash / uppercase ids | 404 | 404, no file bytes | Blocked |
| 10 | neighbour ids | enumeration (IDOR) | 404 | 404 | Blocked |
| 11 | name/title/status | SQL injection payloads | stored verbatim | stored verbatim, schema intact; `status=` injection → 422 | Blocked |
| 12 | `Content-Type` | utf-16, text/json, multipart, list, extra params | 415 | 415 | Blocked |
| 13 | cross-site | text/plain and form POSTs with `Origin` | refused | 415, no CORS headers | Blocked |
| 14 | `Idempotency-Key` | CRLF, NUL, DEL, Cyrillic | 422 | 422, no injected header | Blocked |
| 15 | items query | `after=-2`, `limit=0/1001/1e3`, `after=0x10`, `status=PENDING`, `after=` | 422 | 422 | Blocked |
| 16 | body | duplicate keys, deep nesting, non-object rows, invalid UTF-8, scalars | 422 | 422 | Blocked |
| 17 | rows | type confusion (lists, dicts, floats, bools, nulls) | rejected rows | `INVALID_TYPE`/`REQUIRED`, never 500 | Blocked |
| 18 | names | RLO, ZWJ, NBSP, Arabic, Hebrew, leftover combining marks, CJK, BOM, tag characters | rejected | rejected with the right code | Blocked |
| 19 | workflow | retry with nothing failed, retry rejected rows, unknown job, malformed dates (10 variants) | 409/404/422 | as expected; `0001-01-01` accepted (documented) | Blocked |
| 20 | storage errors | exception text with path + email | not exposed | fixed safe message | Blocked |
| 21 | data dir | job dir pre-planted as symlink to an outside dir | write refused | `GENERATION_FAILED`, outside dir untouched | Blocked |
| 22 | worker race | stale worker writes after `WORKER_LOST` retirement | file never served | 409; not in archive | Blocked |
| 23 | process kill | SIGKILL API (embedded workers) mid-render, SIGKILL one of 3 worker processes | no loss, no duplicates | completed, one PDF per item, no tmp | Blocked |
| 24 | body limit | declared and chunked oversize over real HTTP | 413 | 413 | Blocked |
| 25 | secrets | git history + tracked files | none | none | Blocked |
| — | LLM-specific attacks | — | — | **NOT APPLICABLE** (no LLM component) | — |

## 12. Root-cause and defect dependency map

| Root cause | Findings | Notes |
|---|---|---|
| RC1 — attacker-controlled sizes are bounded only in aggregate (body bytes), not per field / per error list / per connection | F-01, F-02, F-11 (and F-09 is the inverse: the aggregate bound is too tight for some valid input) | Fixing F-01 and F-02 with per-field and per-row caps changes the effective worst-case row size, so **F-09's budget must be recomputed after F-01/F-02** |
| RC2 — strings from `json.loads` are assumed UTF-8-encodable and inside BMP | F-03, F-05 | F-05's fix (BMP-only allowlist) does not fix F-03 (surrogates are rejected earlier or never reach fonts); fix F-03 in `decode_json_body` first |
| RC3 — trust in the network position (loopback = trusted) | F-10, F-11, F-12 | F-10 is the enabler that lets a browser deliver F-01/F-02 to a loopback-only instance |
| RC4 — substring matching on driver error text | F-07 | independent |
| RC5 — float rounding for a state indicator | F-08 | independent |
| RC6 — third-party pins frozen by policy | F-06, F-18 | bumping Pillow/ReportLab must re-run the byte-determinism tests |
| RC7 — import-time configuration / test knobs in prod config | F-13, F-14 | independent |

Dependency order: F-03 → F-05 (shared validation path); F-01, F-02 → F-09; F-10 independent but should precede publishing any non-loopback deployment guidance.

## 13. TDD red-test catalog (acceptance contract for Stage 2)

| Test (file::name) | Assertion | Finding | Fails today because |
|---|---|---|---|
| `test_a1::test_overlong_email_is_rejected_in_bounded_time` | 1 M-char email → 202, row 1 `INVALID_EMAIL`, < 2 s | F-01 | 12.7 s |
| `test_a4::test_overlong_email_over_http_is_fast_and_does_not_starve_others` | 600 k-char email over HTTP < 2 s | F-01 | 6.73 s |
| `test_a1::test_unknown_field_errors_are_bounded` | ≤ 11 errors for the row, response ≤ 64 KiB, stored ≤ 64 KiB | F-02 | 50,000 errors returned |
| `test_a1::test_lone_surrogate_never_500[*]` (8 red of 12) | status ∈ {200, 202, 422}, JSON body | F-03 | 500 |
| `test_a2::test_canonical_form_of_any_accepted_body_is_utf8_encodable` | decode rejects or canonical form encodes | F-03 | `UnicodeEncodeError` |
| `test_a1::test_items_after_out_of_range_is_422[*]` (2) | 422 `INVALID_VALUE` on `after` | F-04 | 500 |
| `test_a1::test_accepted_name_round_trips_through_pdf_text_layer` | rejected `UNSUPPORTED_CHARACTERS`, or name in PDF text | F-05 | text layer `'Zoë  Smith'` |
| `test_a1::test_key_violation_reaches_replay_backstop` | 200 + `Idempotent-Replayed: true` | F-07 | 500 (`RuntimeError`) |
| `test_a1::test_progress_is_below_one_until_terminal[*]` (2) | progress < 1.0 while processing | F-08 | 1.0 |
| `test_a2::test_progress_one_only_when_terminal` | progress 1.0 ⇒ terminal | F-08 | counterexample 99,999/100,000 |
| `test_a1::test_body_limit_admits_every_valid_request` | 202 | F-09 | 413 (2,884,126 B vs 2,665,536 B) |
| `test_a4::test_foreign_host_header_is_rejected` | 400 | F-10 | 200 |
| `test_a4::test_idle_connections_do_not_lock_out_clients` | `/healthz` 200 within 10 s with 64 idle sockets | F-11 | 503 × 11 |

Total red: **23 failing collected test cases** (`test_a1`: 18, `test_a2`: 2, `test_a4`: 3), covering 10 findings. Each was checked to fail for the reason in the last column (failure messages in `audit/evidence/07-audit-suites.txt`). Findings without a red test (F-06, F-12–F-19) are tool warnings, documented behaviour or hygiene items; their PROPOSED checks are listed in §5/§8.4.

## 14. Prioritized remediation plan

| Batch | Findings | Change (text only) | Depends on | Risk | Acceptance criteria |
|---|---|---|---|---|---|
| B1 (security, HIGH) | F-01, F-02 | Pre-check field lengths before `validate_email` (and before costly work for every field); cap unknown-field errors per row and total errors per response (cap values: **decision required**) | — | Low; changes error lists for abusive inputs only | red tests F-01 (both), F-02 green; full project suite green; dry run and 422 paths use the same caps |
| B2 (robustness, MEDIUM/LOW) | F-03, F-04 | Reject lone surrogates in `decode_json_body` (`INVALID_JSON`); bound `after` | — | Low | 12 surrogate cases + property green; `after` overflow → 422 |
| B3 (exposure, MEDIUM) | F-10, F-11 | Host allowlist middleware (config var name: **decision required**); connection hygiene (proxy guidance / timeouts / limit) — **deployment decision required** | — | Medium: a wrong allowlist locks out legitimate hostnames | E2E red tests F-10, F-11 green; README updated |
| B4 (integrity, MEDIUM) | F-05 | BMP-only allowlist (or fix the CMap) | B2 | Low; determinism tests must stay green | F-05 red test green; README scope sentence matches the census |
| B5 (supply chain, MEDIUM) | F-06, F-18 | Amendment + bump Pillow ≥ 13.0.0 and pypdf ≥ 6.21.2; consider self-hosting Swagger assets or disabling `/docs` outside development | — | Medium: ReportLab/Pillow bump may change PDF bytes → re-baseline determinism | `pip-audit` clean; full suite green incl. byte-determinism |
| B6 (correctness, LOW) | F-07, F-08, F-09 | Exact constraint matching + backstop test; non-rounding progress; recompute body budget after B1 (**decision**: raise budget vs document NFC assumption) | B1 for F-09 | Low | red tests green |
| B7 (hygiene, INFO/LOW) | F-13, F-14, F-15, F-16, F-17, F-19 | Reject slow-render ≥ lease; move settings parsing out of import; optional size/`%%EOF` check on read; add M24/M11 killing tests; commit or remove demo files consistently; replace narrowing asserts | — | Low | new tests green; no behaviour change for valid input |

## 15. Residual risks, blockers and NOT VERIFIED items

| Item | Status | Blocker / reason | Risk |
|---|---|---|---|
| Duration of the F-11 lockout beyond 10 s | NOT VERIFIED | the longer (150 s) measurement was declined during the session; only ≥ 10 s is evidenced | Medium: may be indefinite |
| Concurrent F-02 requests → OOM | NOT VERIFIED | not run to protect the host; single-request RSS (~2 GB) measured | High if exposed |
| anyio threadpool saturation by F-01 (≈ 40 concurrent requests) | NOT VERIFIED | not run (would pin all cores for minutes) | High if exposed |
| Pillow / pypdf CVE reachability | NOT VERIFIED (TOOL WARNING) | no exploit path found; ReportLab imports PIL at render time | Low–medium |
| Windows behaviour (no `O_NOFOLLOW`, no POSIX signals) | NOT VERIFIED | Linux-only host | Medium for the symlink defence on Windows |
| `problem_statement.pdf` ↔ transcription | NOT VERIFIED | relied on the committed transcription | Low |
| P2 (author can explain/modify) | NOT VERIFIED | human-only | — |
| Licence compliance of transitive deps | NOT VERIFIED | not machine-audited | Low |
| Visual legibility sign-off (BUILD_LOG G2) | NOT VERIFIED (human) | automated raster check only (audit viewed one rasterized certificate) | Low |
| Python 3.14 behaviour | NOT VERIFIED by this audit | only 3.11.9 executed (project transcript claims 3.14.4) | Low |

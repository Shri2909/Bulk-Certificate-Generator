# Requirements (spec lock)

Source of truth: `docs/problem_statement.pdf` (transcribed in `docs/problem_statement.md`).
Every row below quotes the PDF. **If anything in this repo conflicts with the PDF, the PDF wins.**

Rules:
- Every `R#` must be covered by at least one test decorated `@pytest.mark.req("R#")`.
  `tests/test_spec_traceability.py` fails the build otherwise, and also fails if a test cites an unknown ID.
- `P#` rows are process constraints. A gate review checks them, not pytest.
- Status of code/test columns: **planned** until Phase 5 builds them. The paths are the intended locations.

## 1. Functional requirements

| ID  | Requirement (atomic) | PDF quote |
|-----|----------------------|-----------|
| R1  | A single API request accepts a **list** of recipients plus shared certificate information and creates one generation job. The client does not make one call per certificate. | "submit a list of recipients along with the certificate information"; "support bulk generation rather than requiring the client to make one API request per certificate" |
| R2  | Recipient data is validated server-side. Invalid recipients are reported individually: row index, field, and machine-readable code. | "Validate the provided recipient data"; "handle invalid recipient data appropriately" |
| R3  | Exactly one certificate is generated for each **valid** recipient. | "Generate a certificate for each valid recipient" |
| R4  | All certificates use one predefined template. There is no template editor and no multiple designs. | "Use a single predefined certificate template" |
| R5  | Each generated certificate contains the recipient-specific information from the request (see interpretation A3: name always; email when `PRINT_EMAIL_ON_CERTIFICATE=true`). | "should contain the recipient-specific information provided in the request" |
| R6  | Generation status is persisted per job and per recipient (item). | "Track the generation status" |
| R7  | The client can query a job's progress (counts/percentage) and its result. | "check the progress/result of the request" |
| R8  | The client can retrieve generated certificates (single certificate and whole job). | "Allow generated certificates to be retrieved" |
| R9  | A failure generating one certificate does not prevent other valid certificates in the same job from being generated. | "should not unnecessarily prevent other valid certificates in the same job from being generated" |
| R10 | Job status identifies **which** recipients succeeded and **which** failed, and why. | "enough information to identify successful and failed generations" |
| R11 | The API and processing are designed for requests with many recipients: they do not block the HTTP request for the full generation time, limits are explicit, and listings are paginated. | "designed around the fact that a single request may contain many recipients"; "handle the generation process efficiently" |
| R12 | The sync-vs-background choice and its reasoning are documented. | "The choice and reasoning should be documented" |
| R13 | Stack: Python + FastAPI + a relational database. | "Technology" section |

## 2. Mandatory test areas (PDF "Testing")

| ID  | Test area |
|-----|-----------|
| R14 | Creating a generation job |
| R15 | Input validation |
| R16 | Certificate generation |
| R17 | Job status/progress |
| R18 | Handling an individual certificate failure |
| R19 | Retrieving generated certificates |

## 3. Mandatory README content (PDF "Documentation")

`tests/test_docs.py` checks that each section exists and that linked ADRs exist. That the commands actually work is verified by running them verbatim in a clean venv in Phase 6 (transcript kept), not by pytest.

| ID  | README must explain |
|-----|---------------------|
| R20 | How to set up the project |
| R21 | How to run the application |
| R22 | How to run tests |
| R23 | How to submit a certificate generation request |
| R24 | How to retrieve generated certificates |
| R25 | Important implementation/design decisions |

## 4. Process constraints (not pytest-enforced)

| ID | Constraint | How it is enforced |
|----|-----------|--------------------|
| P1 | Optional features must not cost required functionality. | `v1.0-required` git tag before any differentiator branch, with the full suite green at that tag. |
| P2 | Author can explain and modify every line. | `docs/WALKTHROUGH.md` + quiz (Phase 7). No code without an R#/ADR reference. |
| P3 | Default setup is `pip install` + one run command, with no Docker/Redis/broker. | Phase 6 clean-venv run of README commands verbatim. |

## 5. Ambiguities and interpretations (decided at Gate 1, 2026-10-11)

| #  | Ambiguity | Interpretation (binding) | Decided in |
|----|-----------|-------------------------|-----------|
| A1 | "Validate" + "each **valid** recipient": should one bad row reject the whole request? | **No.** The job is accepted (`202`): invalid rows are stored as items with status `rejected` + error codes, and valid rows are generated. Whole-request `422` **only** when the request is structurally malformed (bad JSON, missing/invalid required top-level fields, empty `recipients`, over `MAX_RECIPIENTS`) or **every** row is invalid (the 422 then lists every row's errors). | ADR-005 · Gate 1 Q1 |
| A2 | What is "recipient data"? | `name` (required, printed) and `email` (required, used for de-duplication, **printed by default**; see A3). Job-level "certificate information": `title` (course/event), `issued_on` (date), `issuer` (organization). | ADR-005 · BLUEPRINT §9 |
| A3 | Does "recipient-specific information" require printing the email? | **Yes, by default** (Gate 3 decision 1, BLUEPRINT §0/§10.2): name and email are printed. `CERTGEN_PRINT_EMAIL_ON_CERTIFICATE=false` is an explicit privacy opt-out. The setting is snapshotted onto each job at creation, so it never changes already-accepted jobs. Both modes tested (R5). *Superseded the Gate 1 default of "not printed".* | BLUEPRINT §0, §10, §15 |
| A4 | Duplicate recipients in one request. | Same email (`strip()` + case-insensitive) twice **in one job** → the later row is `rejected` with `DUPLICATE_RECIPIENT`; the error message names the first occurrence's row index (and carries `duplicate_of_row_index`). Duplicates **across** jobs are allowed. | ADR-005 · Gate 1 Q2 |
| A5 | "Retrieve generated certificates": one or all? | Both: `GET /v1/certificates/{certificate_id}` (single PDF) and `GET /v1/certificate-jobs/{id}/archive` (ZIP of all succeeded + `manifest.csv`). | ADR-009 |
| A6 | "Progress/result": what shape? | Job resource with `status`, `counts` per item status, `progress` (0–1 over valid items), plus a paginated, status-filterable item list. | ADR-009 |
| A7 | "Failure while generating" vs "invalid data". | Two distinct failure classes: `rejected` (input invalid, never attempted) and `failed` (attempted, generation error). Both are visible in job status (R10). | ADR-010 |
| A8 | "Unnecessarily prevent": is retry expected? | Any renderer exception is retried up to `MAX_ATTEMPTS` (3) after a fixed `RETRY_DELAY_SECONDS`. No transient/permanent classification at runtime, because inputs that are deterministically unrenderable (missing glyphs, RTL) are rejected at validation (A9) and never reach the renderer. Exhausted → `failed` with `GENERATION_FAILED`; a crash-looping item → `failed` with `WORKER_LOST`. | ADR-004 |
| A9 | Unicode names. | Allowed when every character is renderable by the bundled font (Latin/Greek/Cyrillic). Characters the font lacks (e.g. CJK) or right-to-left scripts that ReportLab cannot shape → `rejected` with `UNSUPPORTED_CHARACTERS`. A certificate never silently renders blank boxes (verified: Helvetica draws CJK without raising). | ADR-007 |
| A10 | "Efficiently". | Background processing with single-item atomic claims (~1 ms each vs ~20 ms render). Measured target: 1,000 recipients complete in under 60 s on a laptop with one worker (measured ~20 ms/PDF in Phase 1). | ADR-003, PLAN |

## 6. Traceability (Requirement → code → test → README)

Updated at every Phase 5 milestone. "Status" is the milestone at which the listed tests were green.

| ID  | Code (BLUEPRINT §3) | Tests | README section | Status |
|-----|---------------------|-------|----------------|--------|
| R1  | `api.py:create_job`, `service.py:create_job`, `models.py:CreateJobRequest` | `test_jobs_api.py` | Submit a certificate generation request | M1 |
| R2  | `validation.py`, `errors.py` | `test_validation.py`, `test_errors.py` | Submit a certificate generation request | M2 |
| R3  | `worker.py:Worker.process`, `service.py:create_job` | `test_worker.py` | Design decisions | planned (M2/M4) |
| R4  | `rendering.py:render_certificate` | `test_rendering.py` | Design decisions | M3 |
| R5  | `rendering.py`, `layout.py` | `test_rendering.py`, `test_layout.py` | Design decisions | M3 |
| R6  | `tables.py`, `worker.py`, `status.py` | `test_jobs_api.py`, `test_worker.py`, `test_job_status.py` | Design decisions | M1 (partial) |
| R7  | `api.py:get_job`, `service.py:get_job`, `status.py` | `test_jobs_api.py` | Retrieve generated certificates | M1 |
| R8  | `api.py:download_certificate`, `api.py:download_archive`, `archive.py`, `storage.py` | `test_retrieval.py` | Retrieve generated certificates | M1 (single download) |
| R9  | `worker.py:Worker.process` | `test_failure_isolation.py`, `test_crash_recovery.py`, `test_worker.py` | Recovery and guarantees | planned (M4) |
| R10 | `service.py:list_items`, `status.py`, `archive.py` | `test_failure_isolation.py`, `test_retrieval.py` | Retrieve generated certificates | planned (M4/M5) |
| R11 | `worker.py`, `middleware.py`, `config.py`, `service.py` | `test_limits.py`, `test_jobs_api.py`, `test_config.py` | Design decisions | M2 (partial) |
| R12 | `README.md` | `test_docs.py` | Design decisions | planned (M6) |
| R13 | `main.py`, `db.py` | `test_db.py`, `test_docs.py` | Setup | M1 (partial) |
| R14 | — | `test_jobs_api.py` | Run tests | M1 |
| R15 | — | `test_validation.py`, `test_limits.py` | Run tests | M2 |
| R16 | — | `test_rendering.py`, `test_worker.py` | Run tests | M3 (rendering); M4 planned |
| R17 | — | `test_jobs_api.py`, `test_job_status.py` | Run tests | M1 (partial) |
| R18 | — | `test_failure_isolation.py` | Run tests | planned (M4) |
| R19 | — | `test_retrieval.py` | Run tests | M1 (partial) |
| R20–R25 | `README.md` | `test_docs.py` | each section | planned (M6) |

# Requirements

Source of truth: [problem_statement.md](problem_statement.md), a transcription of the original
assignment. **If anything in this repo conflicts with the assignment text, the assignment text wins.**
Quoted excerpts below come from that text.

Rules:
- Every `R#` must be covered by at least one test decorated `@pytest.mark.req("R#")`.
  `tests/test_spec_traceability.py` fails the build otherwise, and also fails if a test cites an unknown ID.
- `P#` rows are process constraints, not enforced by pytest.

## 1. Functional requirements

| ID  | Requirement (atomic) | Assignment quote |
|-----|----------------------|-----------|
| R1  | A single API request accepts a **list** of recipients plus shared certificate information and creates one generation job. The client does not make one call per certificate. | "submit a list of recipients along with the certificate information"; "support bulk generation rather than requiring the client to make one API request per certificate" |
| R2  | Recipient data is validated server-side. Invalid recipients are reported individually: row index, field, and machine-readable code. | "Validate the provided recipient data"; "handle invalid recipient data appropriately" |
| R3  | Exactly one certificate is generated for each **valid** recipient. | "Generate a certificate for each valid recipient" |
| R4  | All certificates use one predefined template. There is no template editor and no multiple designs. | "Use a single predefined certificate template" |
| R5  | Each generated certificate contains the recipient-specific information from the request (see interpretation A3: name always; email when `CERTGEN_PRINT_EMAIL_ON_CERTIFICATE=true`, the default). | "should contain the recipient-specific information provided in the request" |
| R6  | Generation status is persisted per job and per recipient (item). | "Track the generation status" |
| R7  | The client can query a job's progress (counts/percentage) and its result. | "check the progress/result of the request" |
| R8  | The client can retrieve generated certificates (single certificate and whole job). | "Allow generated certificates to be retrieved" |
| R9  | A failure generating one certificate does not prevent other valid certificates in the same job from being generated. | "should not unnecessarily prevent other valid certificates in the same job from being generated" |
| R10 | Job status identifies **which** recipients succeeded and **which** failed, and why. | "enough information to identify successful and failed generations" |
| R11 | The API and processing are designed for requests with many recipients: they do not block the HTTP request for the full generation time, limits are explicit, and listings are paginated. | "designed around the fact that a single request may contain many recipients"; "handle the generation process efficiently" |
| R12 | The sync-vs-background choice and its reasoning are documented. | "The choice and reasoning should be documented" |
| R13 | Stack: Python + FastAPI + a relational database. | "Technology" section |

## 2. Mandatory test areas (assignment "Testing")

| ID  | Test area |
|-----|-----------|
| R14 | Creating a generation job |
| R15 | Input validation |
| R16 | Certificate generation |
| R17 | Job status/progress |
| R18 | Handling an individual certificate failure |
| R19 | Retrieving generated certificates |

## 3. Mandatory README content (assignment "Documentation")

`tests/test_docs.py` checks that the README has the matching sections, stays within 200 lines, that every relative link in the README, ARCHITECTURE.md and METHODOLOGY.md resolves, and that the curl example is accepted by the API. It does not execute the setup commands; run them in a clean virtual environment to verify them.

| ID  | README must explain |
|-----|---------------------|
| R20 | How to set up the project |
| R21 | How to run the application |
| R22 | How to run tests |
| R23 | How to submit a certificate generation request |
| R24 | How to retrieve generated certificates |
| R25 | Important implementation/design decisions |

## 4. Process constraints

| ID | Constraint | How it is checked |
|----|-----------|-------------------|
| P1 | Optional features must not cost required functionality. | Required behaviour is covered by the `R#` tests, which run in the same default suite as the optional features. |
| P2 | The author can explain and modify every line. | Not testable; a human responsibility. |
| P3 | Default setup is `pip install` + one run command, with no Docker/Redis/broker. | Run the README setup and launch commands verbatim in a clean virtual environment. |

## 5. Ambiguities and interpretations

| #  | Ambiguity | Interpretation (binding) | Decision record |
|----|-----------|-------------------------|-----------------|
| A1 | "Validate" + "each **valid** recipient": should one bad row reject the whole request? | **No.** The job is accepted (`202`): invalid rows are stored as items with status `rejected` + error codes, and valid rows are generated. Whole-request `422` **only** when the request is structurally malformed (bad JSON, missing/invalid required top-level fields, empty `recipients`, over `CERTGEN_MAX_RECIPIENTS`) or **every** row is invalid (the 422 then lists every row's errors). | ADR-005 |
| A2 | What is "recipient data"? | `name` (required, printed) and `email` (required, used for de-duplication, **printed by default**; see A3). Job-level "certificate information": `title` (course/event), `issued_on` (date), `issuer` (organization). | ADR-005 |
| A3 | Does "recipient-specific information" require printing the email? | **Yes, by default:** name and email are printed. `CERTGEN_PRINT_EMAIL_ON_CERTIFICATE=false` is an explicit privacy opt-out. The setting is snapshotted onto each job at creation, so it never changes already-accepted jobs. Both modes are tested (R5). | ADR-007 |
| A4 | Duplicate recipients in one request. | Same email (`strip()` + case-insensitive) twice **in one job** → the later row is `rejected` with `DUPLICATE_RECIPIENT`; the error message names the first occurrence's row index (and carries `duplicate_of_row_index`). Duplicates **across** jobs are allowed. | ADR-005 |
| A5 | "Retrieve generated certificates": one or all? | Both: `GET /v1/certificates/{certificate_id}` (single PDF) and `GET /v1/certificate-jobs/{id}/archive` (ZIP of all succeeded + `manifest.csv`). | ADR-009 |
| A6 | "Progress/result": what shape? | Job resource with `status`, `counts` per item status, `progress` (0–1 over valid items), plus a paginated, status-filterable item list. | ADR-009 |
| A7 | "Failure while generating" vs "invalid data". | Two distinct failure classes: `rejected` (input invalid, never attempted) and `failed` (attempted, generation error). Both are visible in job status (R10). | ADR-010 |
| A8 | "Unnecessarily prevent": is retry expected? | Any renderer exception is retried up to `CERTGEN_MAX_ATTEMPTS` (3) times with a fixed `CERTGEN_RETRY_DELAY_SECONDS` (2 s). No transient/permanent classification at runtime, because inputs that are deterministically unrenderable (missing glyphs, RTL) are rejected at validation (A9) and never reach the renderer. Exhausted → `failed` with `GENERATION_FAILED`; a crash-looping item → `failed` with `WORKER_LOST`. | ADR-004 |
| A9 | Unicode names. | Allowed when every character is renderable by the bundled font (Latin/Greek/Cyrillic). Characters the font lacks (e.g. CJK) or right-to-left scripts that ReportLab cannot shape → `rejected` with `UNSUPPORTED_CHARACTERS`. A certificate never silently renders blank boxes (a standard PDF font draws CJK as missing glyphs without raising). | ADR-007 |
| A10 | "Efficiently". | Background processing with short single-item atomic claims; rendering happens outside any transaction. 1,000 recipients complete in about 12 s with one worker process (see the README benchmark table). | ADR-003 |

## 6. Traceability (Requirement → code → test → README)

| ID  | Code | Tests | README section |
|-----|---------------------|-------|----------------|
| R1  | `api.py:create_job`, `service.py:create_job`, `models.py:CreateJobRequest` | `test_jobs_api.py` | Quick start |
| R2  | `validation.py`, `errors.py` | `test_validation.py`, `test_errors.py` | Quick start |
| R3  | `worker.py:Worker.process`, `service.py:create_job` | `test_worker.py` | Key design decisions |
| R4  | `rendering.py:render_certificate` | `test_rendering.py` | Key design decisions |
| R5  | `rendering.py`, `layout.py` | `test_rendering.py`, `test_layout.py` | Key design decisions |
| R6  | `tables.py`, `worker.py`, `status.py` | `test_jobs_api.py`, `test_worker.py`, `test_job_status.py` | Key design decisions |
| R7  | `api.py:get_job`, `service.py:get_job`, `status.py` | `test_jobs_api.py` | Quick start, API |
| R8  | `api.py:download_certificate`, `api.py:download_archive`, `archive.py`, `storage.py` | `test_retrieval.py` | Quick start, API |
| R9  | `worker.py:Worker.process` | `test_failure_isolation.py`, `test_crash_recovery.py`, `test_worker.py` | Key design decisions, Known limitations |
| R10 | `service.py:list_items`, `status.py`, `archive.py` | `test_failure_isolation.py`, `test_retrieval.py` | Retrieve generated certificates |
| R11 | `worker.py`, `middleware.py`, `config.py`, `service.py` | `test_limits.py`, `test_jobs_api.py`, `test_config.py` | Key design decisions |
| R12 | `README.md` | `test_docs.py` | Key design decisions |
| R13 | `main.py`, `db.py` | `test_db.py`, `test_docs.py`, `test_runtime.py` | Quick start |
| R14 | — | `test_jobs_api.py` | Tests and benchmarks |
| R15 | — | `test_validation.py`, `test_limits.py` | Tests and benchmarks |
| R16 | — | `test_rendering.py`, `test_worker.py` | Tests and benchmarks |
| R17 | — | `test_jobs_api.py`, `test_job_status.py` | Tests and benchmarks |
| R18 | — | `test_failure_isolation.py` | Tests and benchmarks |
| R19 | — | `test_retrieval.py` | Tests and benchmarks |
| R20–R25 | `README.md` | `test_docs.py` | each section |

## 7. Obligations

One heading per requirement (checked by `tests/test_spec_traceability.py`).

### R1 — Accept a bulk request: a list of recipients plus certificate information in one call

### R2 — Validate recipient data and report invalid rows with row index, field and code

### R3 — Generate exactly one certificate per valid recipient

### R4 — Use a single predefined template

### R5 — Put the recipient-specific information provided on the certificate

### R6 — Track generation status per job and per item

### R7 — Let the client check progress and result

### R8 — Let the client retrieve generated certificates (single and whole job)

### R9 — Keep one certificate's failure from blocking the other valid certificates

### R10 — Make job status identify which generations succeeded and which failed, and why

### R11 — Design for many recipients: non-blocking, explicit limits, paginated listings

### R12 — Document the synchronous-vs-background choice and its reasoning

### R13 — Use Python, FastAPI and a relational database

### R14 — Test creating a generation job

### R15 — Test input validation

### R16 — Test certificate generation

### R17 — Test job status and progress

### R18 — Test handling an individual certificate failure

### R19 — Test retrieving generated certificates

### R20 — README: how to set up the project

### R21 — README: how to run the application

### R22 — README: how to run tests

### R23 — README: how to submit a certificate generation request

### R24 — README: how to retrieve generated certificates

### R25 — README: important implementation and design decisions

### P1 — Optional features must not cost required functionality

### P2 — The author can explain, debug and modify the code

### P3 — Setup is pip install plus one run command; no Docker, Redis or broker

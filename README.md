# certgen — Bulk Certificate Generator

A FastAPI service that accepts one request with many recipients, validates every row, generates one PDF
certificate per valid recipient from a single predefined template in the background, tracks per-item
status, and serves the PDFs one by one or as a ZIP with a manifest. Storage is SQLite (WAL) plus files
on local disk; nothing else needs to be installed.

**Quick start** (Python 3.11 or newer, pip):

```bash
python3 -m venv .venv               # any Python >= 3.11 (e.g. python3.11; Windows: py -3.11)
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m certgen                    # serves http://127.0.0.1:8000 with 2 embedded workers
```

In a second terminal, submit a job:

```bash
curl -s -X POST http://127.0.0.1:8000/v1/certificate-jobs \
  -H 'Content-Type: application/json' \
  -d '{"certificate": {"title": "Python for Data Engineering", "issuer": "Acme Academy", "issued_on": "2026-10-01"}, "recipients": [{"name": "Zoë Łukasz", "email": "zoe@example.com"}, {"name": "Bob", "email": "not-an-email"}, {"name": "Νίκος Παπαδόπουλος", "email": "nikos@example.org"}]}'
```

The response is `202 Accepted` with a `Location` header pointing at the job. Row 1 is reported as
`INVALID_EMAIL`; rows 0 and 2 are generated. Interactive API docs: http://127.0.0.1:8000/docs.

## Setup

Requirements: Python ≥ 3.11 (uses `enum.StrEnum`), pip, a local disk. No Docker, Redis, broker or
database server.

```bash
python3 -m venv .venv               # any Python >= 3.11 (e.g. python3.11; Windows: py -3.11)
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt      # runtime only (exact pins, full freeze)
pip install -r requirements-dev.txt  # adds pytest, pypdf, ruff, mypy for development
```

Every dependency is pinned exactly (`requirements.txt` is a `pip freeze` of a clean venv). The two
certificate fonts (DejaVu Sans 2.37, regular and bold) are vendored in `certgen/assets/` with their
license, so output does not depend on fonts installed on the machine.

## Run

```bash
python -m certgen
```

This starts uvicorn on `127.0.0.1:8000` (change with `CERTGEN_HOST` / `CERTGEN_PORT`) and two worker
threads inside the same process. Data goes to `./data/` (`CERTGEN_DATA_DIR`): `certgen.sqlite3` and
`certificates/{job_id}/{certificate_id}.pdf`. `uvicorn certgen.main:app` also works.

To scale generation, turn the embedded threads off and run separate worker processes against the same
data directory; any mix is safe because every claim is atomic:

```bash
CERTGEN_EMBEDDED_WORKER=false python -m certgen      # API only
python -m certgen.worker                             # run one or more of these
```

Workers stop gracefully on Ctrl-C / SIGTERM: the item in progress finishes, nothing is lost.
`GET /healthz` reports the database and the number of live embedded worker threads
(`"status": "degraded"` when fewer are alive than configured). It cannot see standalone worker
processes: with `CERTGEN_EMBEDDED_WORKER=false` it reports `0/0` and `ok` even if no worker runs.

## Run tests

```bash
pip install -r requirements-dev.txt
pytest                         # full suite, including slow subprocess crash tests
pytest -m "not slow"           # fast loop, skips the subprocess tests
ruff check . && ruff format --check . && mypy --strict certgen
```

Every test carries `@pytest.mark.req("R#")` linking it to a requirement in
[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md); [tests/test_spec_traceability.py](tests/test_spec_traceability.py)
fails if any requirement has no test. The PDF's minimum test areas map to:
creating a job → `tests/test_jobs_api.py`; input validation → `tests/test_validation.py`, `tests/test_limits.py`;
generation → `tests/test_rendering.py`, `tests/test_worker.py`; status/progress → `tests/test_job_status.py`;
individual failure → `tests/test_failure_isolation.py`; retrieval → `tests/test_retrieval.py`.
In-process tests do not sleep: a fake clock drives lease expiry and a synchronous `drain()` runs the
worker. Subprocess and threaded tests poll with bounded timeouts.
The POSIX-signal tests (`tests/test_crash_recovery.py` kill tests, `tests/test_runtime.py`) are skipped on
Windows.

## Submit a certificate generation request

`POST /v1/certificate-jobs` with `Content-Type: application/json` (other media types → 415):

```json
{
  "certificate": {"title": "Python for Data Engineering", "issuer": "Acme Academy", "issued_on": "2026-10-01"},
  "recipients": [
    {"name": "Zoë Łukasz", "email": "zoe@example.com"},
    {"name": "Bob", "email": "not-an-email"},
    {"name": "Zoe Again", "email": " ZOE@example.com "}
  ]
}
```

- `certificate` is shared by every recipient: `title` and `issuer` (≤ 200 characters each, must fit the
  template), `issued_on` (`YYYY-MM-DD`, at most one day after today in UTC).
- Each recipient has `name` (≤ 120 characters, must fit) and `email`. Both are printed on the certificate
  (set `CERTGEN_PRINT_EMAIL_ON_CERTIFICATE=false` to leave the email off; the choice is stored per job).
- Up to 10,000 recipients per request (`CERTGEN_MAX_RECIPIENTS`). The body limit is derived from it, so
  a request within the field limits is never rejected for size.

**Partial acceptance.** Invalid rows do not reject the request: valid rows become `pending` items,
invalid rows are stored as `rejected` with their errors, and the response lists every row error.

```json
{
  "id": "3f2c0d7e9b1a4c6d8e0f1a2b3c4d5e6f",
  "status": "queued",
  "counts": {"total": 3, "rejected": 2, "pending": 1, "processing": 0, "succeeded": 0, "failed": 0},
  "progress": 0.0,
  "links": {"self": "/v1/certificate-jobs/3f2c…", "items": "/v1/certificate-jobs/3f2c…/items", "archive": "/v1/certificate-jobs/3f2c…/archive"},
  "errors": [
    {"row_index": 1, "field": "email", "code": "INVALID_EMAIL", "message": "An email address must have an @-sign."},
    {"row_index": 2, "field": "email", "code": "DUPLICATE_RECIPIENT", "message": "Duplicate of row 0 (same email, case-insensitive).", "duplicate_of_row_index": 0}
  ]
}
```

(abridged; the full resource also has `certificate`, `print_email`, `created_at`, `finished_at`.)
The whole request is rejected with `422 VALIDATION_FAILED` only when it is structurally wrong (bad JSON,
duplicate JSON keys, `NaN`, nesting deeper than 8, missing or unknown fields, a recipient that is not an
object), when `certificate` fields are invalid, when there are no or too many recipients, or when every
row is invalid. Nothing is stored in those cases. Errors are ordered: request-level first, then rows by
`row_index`; within a row `name`, `email`, then unknown fields.

Validation rules in brief: names are NFC-normalized and spaces collapsed; control, format and non-space
whitespace characters are rejected (`INVALID_CHARACTERS`); characters the font cannot draw, combining
marks and right-to-left scripts are rejected (`UNSUPPORTED_CHARACTERS`); text that cannot fit its box is
`DOES_NOT_FIT`. Emails must be valid ASCII addresses (`email-validator`, no deliverability check);
a repeated email within one request (trimmed, case-insensitive) is `DUPLICATE_RECIPIENT` on the later row.
Duplicates across jobs are allowed.

**Lost responses.** If the `202` response is lost, look the job up by its id rather than resubmitting:
a blind resubmission creates a second job. Keep the returned `id`/`Location`.

## Retrieve generated certificates

Poll the job until it is terminal (`completed`, `completed_with_errors` or `failed`):

```bash
curl -s http://127.0.0.1:8000/v1/certificate-jobs/{job_id}
```

`status` is derived from item counts on every read; `progress` = finished valid items / valid items.
Which rows succeeded or failed, and why, is in the paginated item list (filter with `status`, page with
`after` = the previous page's `next_after`, `limit` 1–1000):

```bash
curl -s "http://127.0.0.1:8000/v1/certificate-jobs/{job_id}/items?status=failed&limit=100"
```

Each item has `row_index`, `name`, `email`, `status`, `attempts`, `errors`, `certificate_id` and, when
`succeeded`, a `download_url`.

One certificate:

```bash
curl -s -o certificate.pdf http://127.0.0.1:8000/v1/certificates/{certificate_id}
```

`200 application/pdf` when ready; `409 CERTIFICATE_NOT_READY` while pending or processing;
`409 CERTIFICATE_GENERATION_FAILED` after it failed; `410 CERTIFICATE_FILE_MISSING` if the file was
removed from disk; `404` for unknown or malformed ids.

Everything at once:

```bash
curl -s -o certificates.zip http://127.0.0.1:8000/v1/certificate-jobs/{job_id}/archive
```

The ZIP holds `certificates/{row_index:05d}_{certificate_id}.pdf` for every succeeded item plus
`manifest.csv` with one line per submitted row (`row_index,name,email,status,certificate_id,error_codes,error_messages`).
It can be downloaded in any job state (a snapshot). Cells that a spreadsheet would run as a formula are
prefixed with `'`. At most two archives are built at once per process; a third gets `503 ARCHIVE_BUSY`.

## Fix and retry: dry run and retry

Two optional helpers for large submissions (differentiator D3):

- **Dry run:** add `?dry_run=true` to the POST to get the exact admission result without storing
  anything: `200 {"dry_run": true, "would_create_job": …, "counts": {"total", "valid", "rejected"}, "errors": [...]}`.
  Row errors are the same as a real submission would report; request-level errors are still `422`.
  `would_create_job` is `false` (still `200`) when every row is invalid. A dry run never checks capacity.

  ```bash
  curl -s -X POST "http://127.0.0.1:8000/v1/certificate-jobs?dry_run=true" -H 'Content-Type: application/json' --data @job.json
  ```

- **Retry failed certificates:** after fixing the cause of `GENERATION_FAILED`/`WORKER_LOST` failures
  (e.g. a full disk), re-queue every `failed` item of a job in one call:

  ```bash
  curl -s -X POST http://127.0.0.1:8000/v1/certificate-jobs/{job_id}/retry
  ```

  `202` with the job resource. Failed items go back to `pending` with attempts, errors, lease and
  completion cleared, and keep their `certificate_id`, so download links stay valid. `409 NOTHING_TO_RETRY`
  if nothing failed, `404` for an unknown job, `503 CAPACITY_EXCEEDED` if the queue cannot take them.
  No body, or `{}`. A worker still holding an old claim on such an item cannot publish over the new attempt
  (`tests/test_retry.py` `test_retry_fences_out_stale_worker`).

## Design decisions

The full specification is [docs/BLUEPRINT.md](docs/BLUEPRINT.md); the ADRs record the reasoning at the
time each decision was made (where they differ, the blueprint is authoritative).

**Background processing, not synchronous** ([ADR-003](docs/decisions/ADR-003-sync-vs-background.md)).
A request may carry 10,000 recipients. Rendering them inside the POST would hold the connection for the
whole run (the serial estimate in [Benchmarks](#benchmarks) is several seconds for 1,000), hit proxy
timeouts, give no progress, and lose all work if the client disconnects or retries. So the POST only
validates and stores the job and its items in one transaction and returns `202`; workers generate in the
background and the client polls for progress. FastAPI `BackgroundTasks` was rejected because tasks live in
memory and vanish on restart; Celery/RQ were rejected because they need a broker. The cost is polling.

**The database is the queue** ([ADR-002](docs/decisions/ADR-002-database-and-schema.md),
[ADR-004](docs/decisions/ADR-004-worker-model-and-crash-recovery.md)). Each recipient row is an `items`
row with a status; status, progress and the work queue are one source of truth, so they cannot disagree.
The same `Worker` runs as threads inside the API process (default, one command) or as separate
`python -m certgen.worker` processes.

**Claim, lease, fence.** A worker claims one item in a single `BEGIN IMMEDIATE` transaction, which takes
SQLite's write lock first, so two workers can never claim the same item. The claim sets a 30 s lease and
a fresh random `lease_token`. Rendering and file writing happen with no transaction open. The worker then
records the outcome with `UPDATE … WHERE id = ? AND lease_token = ?`. If the worker died, its lease
expires and the next claim takes the item over with a new token; if the old worker then wakes up, its
token no longer matches and its write changes nothing. After 3 attempts an item ends `failed`.

**Two SQLite engines.** Writers begin with `BEGIN IMMEDIATE`; readers use a plain `BEGIN`. Under WAL,
status polling therefore never blocks a claim and a claim never blocks polling
([tests/test_db.py](tests/test_db.py) `test_open_read_does_not_block_claim`). A lock wait longer than 5 s
returns `503 DB_UNAVAILABLE` with `Retry-After: 1`.

**Failure isolation** ([ADR-010](docs/decisions/ADR-010-error-model.md)). Each item is processed in its
own `try/except` and its own transactions. A rendering or disk error is retried after 2 s, up to 3 attempts,
then the item is `failed` with `GENERATION_FAILED`; the other items are unaffected. Clients see a fixed
message (`"<ExceptionType>: Certificate generation failed."`), never exception text or paths; details go to
the log. Invalid input (`rejected`) and failed generation (`failed`) are distinct states.

**Validation: accept exactly what renders correctly** ([ADR-005](docs/decisions/ADR-005-validation.md)).
The same `fit_text` function decides whether text fits during validation and positions it during
rendering, so an accepted row never overflows the page and a valid email is never rejected for layout
(254 of the widest ASCII glyph still fits: `tests/test_layout.py` `test_worst_case_email_fits`).

**PDF generation** ([ADR-007](docs/decisions/ADR-007-pdf-library-and-template.md)). ReportLab's canvas
with one fixed A4-landscape template and vendored fonts; `invariant=1` makes output byte-identical for the
same inputs across processes (`tests/test_rendering.py` `test_render_is_byte_deterministic`). The worker
renders only from the stored job and item rows, so a deployment's settings never change an accepted job.

**Storage** ([ADR-008](docs/decisions/ADR-008-file-storage.md)). Paths are built only from
server-generated hex ids (`{job_id}/{certificate_id}.pdf`), written to a unique temp file, `fsync`ed and
renamed into place, so a reader sees the whole file or none. Files are private (`0600`), and symlinks are
refused when reading.

**Identifiers and retrieval** ([ADR-013](docs/decisions/ADR-013-certificate-identity-and-verification.md),
[ADR-009](docs/decisions/ADR-009-retrieval-api.md)). Job ids (`uuid4`) and certificate ids (128 random
bits) are unguessable. Retrieval mirrors submission: bulk in, bulk out (ZIP + manifest), plus single
downloads and a keyset-paginated item list.

**Errors and limits** ([ADR-010](docs/decisions/ADR-010-error-model.md), [ADR-011](docs/decisions/ADR-011-limits.md),
[ADR-001](docs/decisions/ADR-001-framework.md)). Every error is RFC 9457 `application/problem+json` with a
machine-readable `code` (see [Error codes](#error-codes)). Routes are plain `def` functions, so FastAPI
runs them in its threadpool and validating a large request never blocks the event loop.
Testing approach: [ADR-012](docs/decisions/ADR-012-test-strategy.md).

## Recovery and guarantees

- **At-least-once execution with an idempotent effect, one current claimant.** After a crash an item may
  be rendered again, but the same item always produces the same file at the same path with the same
  bytes, and only the worker holding the current token can record the result. Exactly-once computation
  is **not** claimed.
- **Crash-safe:** a worker process killed with SIGKILL mid-job loses nothing and duplicates nothing; a
  second worker finishes the job with one PDF per item and the killed item at 2 attempts
  (`tests/test_crash_recovery.py` `test_kill_mid_job_no_duplicates`, a real subprocess; passed 5
  consecutive runs). A hand-run transcript with a real server, `curl` and `kill -9` is in
  [docs/evidence/kill-demo.txt](docs/evidence/kill-demo.txt).
- **No duplicates under concurrency:** two workers never process the same item
  (`tests/test_worker.py` `test_two_concurrent_workers_never_double_process`,
  `tests/test_crash_recovery.py` `test_two_worker_processes_never_double_process`); a stale worker's late
  write is rejected (`test_stale_worker_done_write_rejected_by_fencing`, `test_same_worker_reclaim_is_fenced`).
- **Crash loops end:** an item whose worker dies on every attempt becomes `failed` with `WORKER_LOST`
  after 3 attempts (`test_crash_loop_ends_failed_after_max_attempts`). In the API process a monitor
  thread also retires such items and restarts dead worker threads, at most 3 times per minute per slot
  (`tests/test_worker.py` `test_monitor_retires_exhausted_lease_without_claims`, `test_restart_budget_is_bounded`).
- **Atomic job creation:** the job and all its items are inserted in one transaction
  (`tests/test_jobs_api.py` `test_job_and_items_are_atomic`).
- **Byte-identical output** for identical inputs (`tests/test_rendering.py` `test_render_is_byte_deterministic`).
- The proofs above were checked against deliberately broken code: each test fails when its guarantee is
  removed ([docs/evidence/mutation-checks.txt](docs/evidence/mutation-checks.txt)).

Not claimed: exactly-once rendering; lease renewal (a render or stall longer than the lease is treated
like a crash, see [Limitations](#limitations)); recovery when no worker runs at all (an expired item then stays
`processing` until a worker or the API's monitor runs); durability of `./data` beyond the local disk.

## Supported names

Names, titles and issuers may use any character that both vendored DejaVu Sans fonts can draw: Latin
(including accented and Vietnamese letters), Greek and Cyrillic, plus common punctuation such as
apostrophes and hyphens (`Seán O'Connor-Smith`, `Ελένη`, `Дмитрий`). Rejected, with a code instead of a
wrong certificate:

- CJK and other scripts the font lacks → `UNSUPPORTED_CHARACTERS` (the message names the first character);
- right-to-left scripts (Hebrew, Arabic) and leftover combining marks → `UNSUPPORTED_CHARACTERS`, because
  ReportLab cannot shape them and would print them wrongly;
- tabs, zero-width and bidi control characters, non-breaking and other non-ASCII spaces → `INVALID_CHARACTERS`.

Limits: name ≤ 120 characters, title and issuer ≤ 200. Long text shrinks and wraps (name 40→16 pt on up to
2 lines, title 24→12 pt on up to 3 lines); text that still cannot fit, such as 120 capital `W`s, is
`DOES_NOT_FIT`. Supporting more scripts would need text shaping (HarfBuzz) and more fonts.

## Configuration

All settings are environment variables, validated at startup (an invalid value stops the process with a
message naming the variable).

| Variable | Default | Allowed | Meaning |
|---|---|---|---|
| `CERTGEN_DATA_DIR` | `./data` | path on a local disk | SQLite file and certificate files |
| `CERTGEN_HOST` | `127.0.0.1` | IP address | bind address for `python -m certgen` |
| `CERTGEN_PORT` | `8000` | 1–65535 | port for `python -m certgen` |
| `CERTGEN_EMBEDDED_WORKER` | `true` | `1/0/true/false` | run worker threads inside the API process |
| `CERTGEN_WORKER_THREADS` | `2` | 1–16 | embedded worker threads |
| `CERTGEN_LEASE_SECONDS` | `30` | 1–3600 | claim lease; an unfinished item is reclaimed after this |
| `CERTGEN_MAX_ATTEMPTS` | `3` | 1–10 | attempts per item before `failed` |
| `CERTGEN_RETRY_DELAY_SECONDS` | `2` | 0–3600 | wait before retrying a failed attempt |
| `CERTGEN_POLL_INTERVAL_SECONDS` | `0.5` | 0.05–10 | worker idle sleep |
| `CERTGEN_MAX_RECIPIENTS` | `10000` | 1–100000 | recipients per request (also sets the body limit) |
| `CERTGEN_MAX_ACTIVE_ITEMS` | `50000` | `MAX_RECIPIENTS`–1000000 | unfinished items across all jobs; more → `503 CAPACITY_EXCEEDED` |
| `CERTGEN_PRINT_EMAIL_ON_CERTIFICATE` | `true` | `1/0/true/false` | print the email under the name; stored per job |
| `CERTGEN_PUBLIC_BASE_URL` | `http://localhost:8000` | absolute http(s) URL, ≤ 100 chars; `http` only for localhost | stored per job (a snapshot) as the base for links on certificates; no link is printed in this version |
| `CERTGEN_LOG_LEVEL` | `INFO` | `DEBUG/INFO/WARNING/ERROR` | `certgen` logger level |
| `CERTGEN_TEST_SLOW_RENDER_MS` | `0` | 0–60000 | **test only**: delay each render so crash tests can kill a worker mid-item |

`python -m certgen` also limits uvicorn to 64 concurrent connections (`limit_concurrency`) with a 5 s
keep-alive; requests over that limit get uvicorn's own plain-text `503`.

## Error codes

Every non-2xx response is `application/problem+json`:
`{"type": "about:blank", "title": …, "status": …, "code": …, "detail": …, "errors": [...]}`;
`errors` appears only on 422, one entry per problem: `{"row_index", "field", "code", "message"}`.

| Code | HTTP | When |
|---|---|---|
| `VALIDATION_FAILED` | 422 | request-level errors, every row invalid, or invalid query parameters |
| `PAYLOAD_TOO_LARGE` | 413 | body over the derived limit |
| `UNSUPPORTED_MEDIA_TYPE` | 415 | `Content-Type` is not `application/json` (optionally `; charset=utf-8`) |
| `NOT_FOUND` | 404 | unknown or malformed job/certificate id, unknown path |
| `METHOD_NOT_ALLOWED` | 405 | wrong HTTP method |
| `CERTIFICATE_NOT_READY` | 409 | certificate still pending or processing |
| `CERTIFICATE_GENERATION_FAILED` | 409 | certificate failed after all attempts |
| `NOTHING_TO_RETRY` | 409 | `POST …/retry` on a job with no failed certificates |
| `CERTIFICATE_FILE_MISSING` | 410 | item succeeded but its file is gone from disk |
| `INTERNAL_ERROR` | 500 | unexpected error (details only in the server log) |
| `DB_UNAVAILABLE` | 503 | database lock wait over 5 s, or the database cannot be read (`Retry-After: 1`) |
| `ARCHIVE_BUSY` | 503 | two archives already being built in this process (`Retry-After: 1`) |
| `CAPACITY_EXCEEDED` | 503 | accepting the job would exceed `CERTGEN_MAX_ACTIVE_ITEMS` (`Retry-After: 1`) |

Field codes (in `errors[].code`, and stored on items):

| Field code | Meaning |
|---|---|
| `INVALID_JSON` | body is not strict JSON (syntax, invalid UTF-8, duplicate keys, `NaN`/`Infinity`, depth > 8) |
| `REQUIRED` | missing, `null` or blank |
| `INVALID_TYPE` | wrong JSON type (e.g. a number for `name`, a recipient that is not an object) |
| `UNKNOWN_FIELD` | a field the API does not accept |
| `INVALID_VALUE` | any other shape error, including bad query parameters |
| `INVALID_FORMAT` | `issued_on` is not a real `YYYY-MM-DD` date |
| `TOO_LONG` | over the length limit |
| `TOO_MANY_RECIPIENTS` | more than `CERTGEN_MAX_RECIPIENTS` rows |
| `DATE_IN_FUTURE` | `issued_on` more than one day after today (UTC) |
| `INVALID_CHARACTERS` | control, format or non-space whitespace character |
| `UNSUPPORTED_CHARACTERS` | character the template cannot render correctly |
| `DOES_NOT_FIT` | text cannot fit its box at the minimum size |
| `INVALID_EMAIL` | not a valid ASCII email address |
| `DUPLICATE_RECIPIENT` | same email as an earlier valid row (`duplicate_of_row_index` names it) |
| `GENERATION_FAILED` | rendering or writing failed on every attempt |
| `WORKER_LOST` | the worker died on every attempt |

## Benchmarks

Produced only by [scripts/benchmark.py](scripts/benchmark.py) on this repository's machine; the rows below
are checked against the JSON files by `tests/test_docs.py` `test_readme_numbers_match_evidence`.
Per-item and render times come from a single in-process worker; wall time and throughput from
`--processes` real `python -m certgen.worker` processes (from spawning them until the last item finished).
The data directory was on the local disk (ext4), so every commit and PDF write paid a real `fsync`.

| Processes | Recipients | Wall (s) | Throughput (certs/s) | Render p50 (ms) | Per item p50 (ms) | Per item max (ms) | POST latency (ms) | Evidence |
|---|---|---|---|---|---|---|---|---|
| 1 | 1000 | 11.95 | 83.7 | 4.2 | 10.8 | 38.2 | 114 | [benchmark-1p.json](docs/evidence/benchmark-1p.json) |
| 2 | 1000 | 7.18 | 139.3 | 4.1 | 11.3 | 35.0 | 119 | [benchmark-2p.json](docs/evidence/benchmark-2p.json) |
| 4 | 1000 | 5.97 | 167.6 | 4.1 | 11.3 | 33.4 | 113 | [benchmark-4p.json](docs/evidence/benchmark-4p.json) |

`serial_estimate_ms` in each file (recipients × median per-item time) is what a synchronous POST would
have to take at minimum; it is an estimate, not a measured synchronous latency. Rendering is CPU-bound
and threads share the GIL, so throughput scales with worker processes rather than threads. Scaling is
sublinear: every claim and completion is a write to the single SQLite writer and each commit and PDF
write is `fsync`ed, so adding processes helps less each time (compare the 1-, 2- and 4-process rows).

## Operations

- **Local disk only.** SQLite WAL does not work on network filesystems; keep `CERTGEN_DATA_DIR` local.
- **Backup:** stop all processes, then copy the whole data directory (`certgen.sqlite3`, its `-wal`/`-shm`
  files if present, and `certificates/`).
- **Full reset:** stop all processes, delete the data directory, start again; the schema is recreated.
- **Offline orphan cleanup:** workers delete stale `*.tmp` files older than two leases at startup. For a
  full cleanup: stop all processes, back up, delete any `*.tmp` and any PDF not referenced by a
  `succeeded` item, then restart.
- **Retention:** none. Jobs, items and files are kept until the operator removes the data directory.
- **Health:** `GET /healthz` (`503` if the database cannot be read; `"degraded"` if embedded workers died).
- **Logs** carry ids and codes; SQL parameters (names, emails) are never included in database error
  messages (`tests/test_db.py` `test_sql_errors_do_not_carry_parameters`).
- **Run every process with the same settings** (`CERTGEN_LEASE_SECONDS`, `CERTGEN_MAX_ATTEMPTS`,
  `CERTGEN_RETRY_DELAY_SECONDS`): each process applies its own values when it reclaims or retires items.

## Limitations

- **No authentication or rate limiting.** Job and certificate ids are unguessable (128 bits) and act as
  capability URLs, which expose names and emails to anyone holding them. The server binds to `127.0.0.1`
  by default; authentication is the first change before any wider deployment.
- **No idempotent resubmission** in this version: keep the job id from the first response.
- **SQLite is a single writer.** Fine for one machine: every write is a short single-row transaction. Larger scale would
  move to PostgreSQL with `FOR UPDATE SKIP LOCKED` (see BLUEPRINT §22).
- **FIFO claiming:** a very large job delays smaller jobs submitted after it.
- **Scripts:** only Latin, Greek and Cyrillic text (see [Supported names](#supported-names)).
- **Embedded threads share the GIL** with request handling; under load run `CERTGEN_EMBEDDED_WORKER=false`
  plus worker processes.
- **No worker, no recovery:** with zero workers running, an expired item stays `processing` until one starts.
- **No lease renewal (by design, no heartbeat):** if one item takes longer than the lease (30 s by
  default; a normal render takes milliseconds, see [Benchmarks](#benchmarks)) because of a stalled disk,
  a paused VM or a stopped process, another worker re-runs it. The output is identical, but after
  3 such expiries the item ends `failed` with `WORKER_LOST` although no worker died. Raise
  `CERTGEN_LEASE_SECONDS` if renders can stall that long.
- **Fixed retry budget:** a failed attempt is retried after 2 s, 3 attempts in total. An outage of the
  certificate storage lasting longer than that fails the affected items; after fixing the cause, re-queue
  them with `POST …/retry` (see [Fix and retry](#fix-and-retry-dry-run-and-retry)).
- **Archives are built before the first byte is sent** (spilling to a temp file above 8 MiB), which holds
  one request thread per build; at most two builds run at once. Request bodies are buffered up to the
  body limit before parsing.
- **Over 64 concurrent connections** uvicorn answers with its own plain-text `503`, outside the problem+json contract.
- Emails are printed on certificates by default, which puts contact details on a document that may be
  shared; set `CERTGEN_PRINT_EMAIL_ON_CERTIFICATE=false` to opt out.

## Blueprint conformance

Built from the frozen [docs/BLUEPRINT.md](docs/BLUEPRINT.md) (sha256 in
[docs/BLUEPRINT.sha256](docs/BLUEPRINT.sha256), tag `blueprint-locked`).

- **Build status:** required scope (R1–R25) complete. Last full test run (M6 gate, raw pytest summary): `296 passed in 11.32s` — transcript in [docs/evidence/gates/M6.txt](docs/evidence/gates/M6.txt).
- **Amendments** ([docs/BLUEPRINT_AMENDMENTS.md](docs/BLUEPRINT_AMENDMENTS.md)):
  A-01 `certgen/__main__.py` for `python -m certgen` (auto-approved); A-02 test for the per-job email
  snapshot (auto-approved).
- **Interpretations** where the blueprint was silent or a library behaved differently than it assumed
  (no behavior added): email-validator accepts internationalized domains even with SMTPUTF8 off, so emails
  are checked to be ASCII explicitly; the widest ASCII glyph is `@`, not `W`, and still fits; the first
  concurrent boot of several processes needed a bounded wait for SQLite's WAL switch.
- **Optional features (differentiators, BLUEPRINT §18):** required functionality was completed and tagged
  `v1.0-required` before any of them.
  - D1 verifiable certificates (QR code + public verify endpoint): **omitted.** Its kill criterion
    ("any required test regresses") tripped: embedding the job's verify URL changes every worker-rendered
    PDF, which breaks the required test `tests/test_rendering.py` `test_generated_file_matches_direct_render`.
    The implementation is kept, unmerged, on branch `dropped/d1-verify`.
  - D3 fix-and-retry (`?dry_run=true`, `POST …/retry`): **included** (`tests/test_dry_run.py`, `tests/test_retry.py`).
  - D2 crash-safety proof: **included** — 2- and 4-process benchmarks and the kill-demo transcript
    (kill check: the slow crash tests passed 5 consecutive runs).

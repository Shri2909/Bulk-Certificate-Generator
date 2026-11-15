# ADR-009: API surface for submission, status and retrieval

Status: Accepted · Requirements: R1, R7, R8, R10, R11, R19

## Options for retrieval
1. **Single PDF by certificate id only** (simplest). 1,000 downloads are 1,000 requests: the per-certificate pattern the assignment warns against for creation.
2. **ZIP only**. Cannot fetch one certificate, or link to it.
3. **Both + a paginated item listing** (chosen).

## Decision — endpoints
Every `/v1` route requires `Authorization: Bearer <token>` (see "Authentication" below). All non-2xx bodies are problem+json (ADR-010).

| Method & path | Purpose | Codes |
|---|---|---|
| `POST /v1/certificate-jobs` | Create a job (R1). `?dry_run=true` validates only. Header `Idempotency-Key` optional (ADR-006). | 202 created · 200 idempotent replay · 200 dry-run report · 409 key reused · 413 body too large · 415 wrong media type · 422 invalid · 503 busy/full |
| `GET /v1/certificate-jobs/{job_id}` | Job status, counts, progress (R7, R10) | 200, 404 |
| `GET /v1/certificate-jobs/{job_id}/items?status=&after=&limit=` | Per-recipient results, keyset-paginated (R10) | 200, 404, 422 |
| `GET /v1/certificates/{certificate_id}` | Download one PDF (R8) | 200, 404, 409, 410 |
| `GET /v1/certificate-jobs/{job_id}/archive` | ZIP of all succeeded PDFs + `manifest.csv` (R8) | 200, 404, 503 |
| `POST /v1/certificate-jobs/{job_id}/retry` | Re-queue `failed` items | 202, 404, 409, 503 |
| `GET /healthz` | Open, no token. `{"status":"ok"\|"degraded","db":"ok","embedded_workers_alive":n,"embedded_workers_expected":m}`. 503 if the database is unreachable. Worker liveness is reported, not judged: 0 alive is correct when workers run as a separate process. | 200, 503 |

That is seven endpoints, plus `GET /metrics` ([ADR-014](ADR-014-metrics.md)); `/docs` and `/openapi.json` are also served without a token.

### Authentication
Anything but a valid bearer token gets `401 UNAUTHORIZED`; `/healthz`, `/docs` and `/openapi.json` are open. Tokens come from `CERTGEN_API_TOKENS` (comma-separated, 16-256 characters). If it is unset, the server creates a random token in `{CERTGEN_DATA_DIR}/api-token` (mode `0600`) on first start and reuses it; the log shows the file's path, never the token. To rotate, change `CERTGEN_API_TOKENS` (or delete the file) and restart. Only SHA-256 digests are kept in memory. Every valid token can read every job (ids are unguessable); only idempotency keys are per token.

### Response bodies
**Job resource** (returned by `GET` job, by `POST` 202, and by an idempotent replay):
```json
{ "id": "…", "status": "queued|processing|completed|completed_with_errors|failed",
  "certificate": {"title": "…", "issuer": "…", "issued_on": "2026-10-01"},
  "print_email": true,
  "counts": {"total": 3, "rejected": 2, "pending": 1, "processing": 0, "succeeded": 0, "failed": 0},
  "progress": 0.0, "created_at": "…Z", "finished_at": null,
  "links": {"self": "…", "items": "…", "archive": "…"} }
```
`finished_at` is set once the job is terminal.

**`POST` 202** = job resource **+ `"errors": [ {row_index, field, code, message, …} ]`**: one flat entry per row error, the same shape as the all-invalid `422` (ADR-010), so the client sees row errors without a second call. The body is bounded by `CERTGEN_MAX_RECIPIENTS`. Header `Location: /v1/certificate-jobs/{id}`.

**`POST ?dry_run=true`** returns `200` even if every row is invalid (it is a report, not a failed command):
```json
{ "dry_run": true, "would_create_job": false, "counts": {"total": 3, "valid": 0, "rejected": 3},
  "errors": [ … same flat shape as above … ] }
```
Request-level structural errors still give `422`/`413` in a dry run (there is nothing to report on). A dry run never creates rows, never checks capacity, and never reads or stores an `Idempotency-Key`, so it cannot poison a real submission's key.

**Item** (in the items listing):
```json
{ "row_index": 1, "name": "…", "email": "…", "status": "rejected|pending|processing|succeeded|failed",
  "attempts": 0, "errors": [ {"field": "email", "code": "INVALID_EMAIL", "message": "…"} ],
  "certificate_id": "…|null", "download_url": "…|null" }
```
`errors` is a list (all row errors, or one generation error) and is empty for healthy items.

### Pagination: keyset on `row_index`
`?after=<row_index>&limit=<1..1000, default 100>&status=<optional>`. Ordered by `row_index`; the response has `next_after` (the last `row_index`, or `null` on the last page). Offset pagination was rejected: with a status filter, items move between statuses while the job runs, so offsets skip or repeat rows. A keyset cursor on the immutable `row_index` cannot. Same complexity.

### Downloads
- Lookup by `certificate_id` on the read engine; the path is built from database values only (ADR-008).
- `succeeded` gives 200 `application/pdf` with `Content-Disposition: attachment; filename="certificate-{certificate_id}.pdf"`. No user-supplied text in the header.
- `pending|processing` gives `409 CERTIFICATE_NOT_READY` (poll again).
- `failed` gives `409 CERTIFICATE_GENERATION_FAILED` (do not poll; see `/retry`).
- Unknown or malformed id, or a rejected row (it has no id): 404.
- `succeeded` but the file is missing or damaged on disk: `410 CERTIFICATE_FILE_MISSING`.

### Archive
- Read all item rows for the job, **close the session**, then build the ZIP. No transaction is held during file I/O (ADR-002).
- Entries: `certificates/{row_index:05d}_{certificate_id}.pdf` (sortable; no user text in paths) + `manifest.csv`.
- `manifest.csv` has one line per submitted row, with columns `row_index, name, email, status, certificate_id, error_codes, error_messages` (multiple errors joined with `; `). A succeeded row whose file is missing is listed with `CERTIFICATE_FILE_MISSING` and its PDF is omitted. Cells are CSV-injection-safe per OWASP: a cell starting with `= + - @`, tab, CR, LF or a full-width equivalent is prefixed with `'`. OWASP notes this is not fully reliable in Excel after save-and-reopen; that is a stated limitation, since the manifest is for audit, not re-import.
- Allowed while the job runs: it is a snapshot (`succeeded` is terminal, and a `succeeded` row's file was renamed into place before the commit), and the manifest shows what is still pending.
- Built in a `SpooledTemporaryFile` (spills to disk above 8 MiB) and then streamed. PDFs are `ZIP_STORED` (already compressed), the manifest `ZIP_DEFLATED`. At most two archives are built at once per process; a third gets `503 ARCHIVE_BUSY`.

### Retry
One write transaction counts the job's `failed` items, checks free disk and queue capacity, and runs:
```sql
UPDATE items SET status='pending', attempts=0, errors=NULL, available_at=NULL,
                 lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, completed_at=NULL
WHERE job_id=:job_id AND status='failed';
```
- No failed items: `409 NOTHING_TO_RETRY`. `rejected` rows are never retried (same data, same errors); the client submits a new job for fixed rows.
- Only `failed` rows are touched. `failed` rows hold no live lease, and a stale worker still finishing an old claim is fenced out by the token (ADR-004).
- Items keep their `certificate_id`, so download links stay valid. Returns `202` with the job resource. The request takes no body (or `{}`).

## Consequences
The ZIP is rebuilt per request (1,000 × ~45 KB ≈ 45 MB sequential read) and is not cached.

## At 100× scale
Pre-signed S3 URLs for both single and archive downloads. A worker builds the archive once when the job turns terminal.

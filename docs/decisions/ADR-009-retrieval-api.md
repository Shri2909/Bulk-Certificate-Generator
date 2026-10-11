# ADR-009: API surface for submission, status and retrieval

Status: Accepted (revised after Phase 4 audit) · Requirements: R1, R7, R8, R10, R11, R19 · Evidence: RESEARCH §4, §5

## Options for retrieval
1. **Single PDF by certificate ID only** (simplest). 1,000 downloads = 1,000 requests: the per-certificate pattern the spec warns against for creation.
2. **ZIP only**. Can't fetch one certificate, or link to it.
3. **Both + paginated item listing** (chosen).

## Decision — endpoints
All non-2xx bodies are problem+json (ADR-010).

| Method & path | Purpose | Codes |
|---|---|---|
| `POST /v1/certificate-jobs` | Create job (R1). `?dry_run=true` validates only (D3). Header `Idempotency-Key` optional (D4). | 202 created · 200 idempotent replay · 200 dry-run report · 409 key reused · 413 body too large · 422 invalid |
| `GET /v1/certificate-jobs/{job_id}` | Job status + counts + progress (R7, R10) | 200, 404 |
| `GET /v1/certificate-jobs/{job_id}/items?status=&after=&limit=` | Per-recipient results, keyset-paginated (R10) | 200, 404, 422 |
| `GET /v1/certificates/{certificate_id}` | Download one PDF (R8) | 200, 404, 409, 410 |
| `GET /v1/certificate-jobs/{job_id}/archive` | ZIP of all succeeded PDFs + `manifest.csv` (R8) | 200, 404 |
| `POST /v1/certificate-jobs/{job_id}/retry` (D3) | Re-queue `failed` items | 202, 404, 409 |
| `GET /v1/verify/{certificate_id}` (D1, M7) | Public verification (ADR-013) | 200 |
| `GET /healthz` | `{"status":"ok","db":"ok","embedded_workers_alive": n, "embedded_workers_expected": m}`. 503 if the DB is unreachable. Worker liveness is reported, not judged: 0 alive is correct when workers run as a separate process. | 200, 503 |

That's 8 endpoints.

### Response bodies (audit #8)
**Job resource** (returned by `GET` job, by `POST` 202, and by an idempotent replay):
```json
{ "id": "…", "status": "queued|processing|completed|completed_with_errors|failed",
  "certificate": {"title": "…", "issuer": "…", "issued_on": "2026-10-01"},
  "counts": {"total": 3, "rejected": 2, "pending": 1, "processing": 0, "succeeded": 0, "failed": 0},
  "progress": 0.0, "created_at": "…Z",
  "links": {"self": "…", "items": "…", "archive": "…"} }
```
**`POST` 202** = job resource **+ `"errors": [ {row_index, field, code, message, …} ]`**: one flat entry per row error, the same shape as the all-invalid `422` (ADR-010), so the client sees row errors without a second call. The body is bounded by `MAX_RECIPIENTS`. Header `Location: /v1/certificate-jobs/{id}`.

**`POST ?dry_run=true`** → always `200` (even if every row is invalid; it's a report, not a failed command):
```json
{ "dry_run": true, "would_create_job": false, "counts": {"total": 3, "valid": 0, "rejected": 3},
  "errors": [ … same flat shape as above … ] }
```
Request-level structural errors still give `422`/`413` in dry-run (there is nothing to report on). **Dry-run never creates rows and never reads or stores an `Idempotency-Key`**: the header is ignored. So a dry-run can't poison the real submission's key (audit #8).

**Item** (in the items listing):
```json
{ "row_index": 1, "name": "…", "email": "…", "status": "rejected|pending|processing|succeeded|failed",
  "attempts": 0, "errors": [ {"field": "email", "code": "INVALID_EMAIL", "message": "…"} ],
  "certificate_id": "…|null", "download_url": "…|null" }
```
`errors` is a list (all row errors, or one generation error) and is empty for healthy items (audit #3).

### Pagination: keyset on `row_index` (audit #17)
`?after=<row_index>&limit=<1..1000, default 100>&status=<optional>`. Ordered by `row_index`, returns `next_after` (the last `row_index`, or `null`). Offset pagination was rejected: with a status filter, items move between statuses while the job runs, so offsets skip or repeat rows. A keyset cursor on the immutable `row_index` can't. Same complexity.

### Downloads
- Lookup by `certificate_id` on the read engine; the path is built from DB values only (ADR-008).
- `succeeded` → 200, `application/pdf`, `Content-Disposition: attachment; filename="certificate-{certificate_id}.pdf"`. No user-supplied text in the header.
- `pending|processing` → `409 CERTIFICATE_NOT_READY` (poll again).
- `failed` → `409 CERTIFICATE_GENERATION_FAILED` (don't poll; see `/retry`) (audit #20).
- Unknown or malformed ID, or a rejected row (it has no ID) → 404.
- `succeeded` but the file is missing on disk → `410 CERTIFICATE_FILE_MISSING`.

### Archive
- Session usage: read all item rows for the job, **close the session**, then build the ZIP. No transaction is held during file I/O (ADR-002, audit #1).
- Entries: `certificates/{row_index:05d}_{certificate_id}.pdf` (sortable; no user text in paths) + `manifest.csv`.
- `manifest.csv` has one line per submitted row, with columns `row_index, name, email, status, certificate_id, error_codes, error_messages` (multiple errors joined with `; `). Cells are CSV-injection-safe per OWASP: a cell starting with `= + - @`, tab, CR, LF or full-width `＝＋－＠` is prefixed with `'`. OWASP notes this is not fully reliable in Excel after save-and-reopen; that's a stated limitation, since the manifest is for audit, not re-import.
- Allowed while the job runs: it is a snapshot (`succeeded` is terminal, and a `succeeded` row's file was renamed into place before the commit), and the manifest shows what's still pending.
- Built in a `SpooledTemporaryFile` (spills to disk > 8 MB) and streamed. PDFs `ZIP_STORED` (already compressed), manifest `ZIP_DEFLATED`.

### Retry (D3) (audit #19)
One `write_engine` transaction:
```sql
UPDATE items SET status='pending', attempts=0, errors=NULL, available_at=NULL,
                 lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, completed_at=NULL
WHERE job_id=:job_id AND status='failed';
```
- `rowcount == 0` → `409 NOTHING_TO_RETRY`. `rejected` rows are never retried (same data, same errors); the client submits a new job for fixed rows.
- Only `failed` rows are touched. `failed` rows hold no live lease, and a stale worker still finishing an old claim is fenced out by the token (ADR-004).
- Returns `202` with the job resource.

## Consequences
The ZIP is rebuilt per request (1,000 × 24 KB ≈ 24 MB sequential read). It isn't cached in v1.

## At 100× scale
Pre-signed S3 URLs for both single and archive. A worker builds the archive once when the job turns terminal.

# ADR-009: API surface for status and retrieval

Status: Accepted · Requirements: R1, R7, R8, R10, R11, R19 · Evidence: RESEARCH §4

## Options for retrieval
1. **Single PDF by certificate ID only** (simplest). 1,000 downloads = 1,000 requests: the per-certificate pattern the spec warns against for creation.
2. **ZIP only**. Can't fetch one certificate, or link to it.
3. **Both + paginated item listing** (chosen).

## Decision — endpoints (all JSON errors are problem+json, ADR-010)
| Method & path | Purpose | Codes |
|---|---|---|
| `POST /v1/certificate-jobs` | Create job (R1). `?dry_run=true` validates only (D3). Header `Idempotency-Key` optional (D4). | 202, 200 (replay / dry-run), 409, 422 |
| `GET /v1/certificate-jobs/{job_id}` | Status: `status`, `counts{pending,processing,succeeded,failed,rejected,total}`, `progress` = (succeeded+failed)/(total−rejected), timestamps, links | 200, 404 |
| `GET /v1/certificate-jobs/{job_id}/items?status=&offset=&limit=` | Per-recipient results (R10): `row_index, name, email, status, attempts, error{code,message}, certificate_id, download_url` | 200, 404, 422 |
| `GET /v1/certificates/{certificate_id}` | Download one PDF (`Content-Disposition: attachment`) | 200, 404, 409 (not yet generated), 410 |
| `GET /v1/certificate-jobs/{job_id}/archive` | ZIP of all **succeeded** PDFs + `manifest.csv` (every row: row_index, name, email, status, certificate_id, error_code, error_message; CSV-injection-safe per OWASP: any cell starting with `= + - @`, tab, CR, LF or their full-width forms `＝＋－＠` is prefixed with `'`. OWASP notes this is not fully reliable in Excel after save-and-reopen, a stated limitation; the manifest is for audit, not re-import). Allowed while the job is running: it is a snapshot, and the manifest shows what's pending. | 200, 404 |
| `POST /v1/certificate-jobs/{job_id}/retry` (D3) | Reset `failed` items to `pending`, attempts reset to 0. `rejected` rows are not retried (same data would fail again). | 202, 404, 409 if none failed |
| `GET /v1/verify/{certificate_id}` (D1) | Public verification: `{valid, recipient_name, title, issuer, issued_on}` or `{valid: false}` (ADR-013) | 200 |
| `GET /healthz` | Liveness + DB reachable | 200 |

- `202` responses include a `Location` header pointing to the job.
- **Pagination**: `offset`/`limit` (default 100, max 1000), ordered by `row_index`. Order is stable because items are immutable in count and order. Cursor pagination is rejected: there's no insertion during reads, and offset is simpler to explain. Response includes `total` and `next` link.
- PDF archive entries are named `{row_index:05d}_{certificate_id}.pdf`. That's sortable, and it avoids user names in paths (no traversal, no duplicate-name collisions).
- ZIP built into a `SpooledTemporaryFile` (spills to disk > 8 MB) and streamed. PDFs `ZIP_STORED` (already compressed), manifest `ZIP_DEFLATED`.

## Consequences
The ZIP is rebuilt on each request, with no cache. 1,000 × 24 KB ≈ 24 MB of sequential reads is cheap. At 100× it's cached per job once terminal.

## At 100× scale
Pre-signed S3 URLs for both single and archive. The archive is built once by a worker when the job turns terminal.

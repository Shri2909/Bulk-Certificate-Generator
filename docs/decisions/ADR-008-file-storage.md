# ADR-008: File storage layout — local directory, server-generated names, atomic writes

Status: Accepted · Requirements: R8, R9 · Evidence: RESEARCH §5 (OWASP File Upload), §2

## Options
1. **PDF bytes as BLOBs in SQLite** (simplest: one artifact to back up). 1,000 × 24 KB = 24 MB of writes through the single SQLite writer, which contends with status updates.
2. **Local filesystem under `STORAGE_DIR`** (chosen). Path derived only from server IDs.
3. **Object storage (S3/MinIO)**. Production answer, but it's an external service (P3).

## Decision
**Option 2**, behind a tiny `storage.py` interface (`save(job_id, cert_id, bytes)`, `open(job_id, cert_id)`, `path_for(...)`). That's the seam for swapping in S3.
- Layout: `STORAGE_DIR/{job_id}/{certificate_id}.pdf`, default `./data/certificates`. Both IDs are server-generated hex. **No user input reaches a path.** Download endpoints look up the item by ID in the DB, then build the path (OWASP: ID→file handler).
- Writes: `tmp = dir / f"{certificate_id}.{uuid4().hex}.tmp"` (**unique per write**, so a stale and a current worker never share a tmp file; audit #6), write, `fsync`, `os.replace(tmp, path)`. On start, each worker deletes `*.tmp` files older than `2 × LEASE_SECONDS` (orphans of killed workers). Readers never see a half-written PDF, and re-processing is idempotent (ADR-004).
- The DB stores no path, because it's derivable. That avoids drift between DB and disk.

## Consequences
- Single-host only. That matches SQLite (ADR-002).
- If someone deletes a file from disk, download returns `410 CERTIFICATE_FILE_MISSING` instead of a 500.

## Tests
`test_retrieval.py::test_path_traversal_id_rejected` (`../../etc/passwd`-style IDs → 404), `test_worker.py::test_atomic_write_no_partial_file`.

## At 100× scale
S3 with the same key layout. Serve downloads via pre-signed URLs (redirect), so the API never streams bytes.

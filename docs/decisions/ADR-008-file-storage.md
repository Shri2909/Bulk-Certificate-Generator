# ADR-008: File storage layout — local directory, server-generated names, atomic writes

Status: Accepted · Requirements: R8, R9

## Options
1. **PDF bytes as BLOBs in SQLite** (simplest: one artifact to back up). 1,000 × ~45 KB = 45 MB of writes through the single SQLite writer, which contends with status updates.
2. **Local filesystem under `{CERTGEN_DATA_DIR}/certificates`** (chosen). Path derived only from server ids.
3. **Object storage (S3/MinIO)**. The production answer, but an external service (P3).

## Decision
**Option 2**, behind a small `Storage` class in `certgen/storage.py` (`save_atomic`, `read`, `path_for`, `sweep_orphans`). That is the seam for swapping in S3.
- Layout: `certificates/{job_id}/{certificate_id}.pdf`. Both ids are server-generated hex and checked against a strict pattern. **No user input reaches a path.** Download endpoints look up the item by id in the database, then build the path (the OWASP "id to file handler" pattern).
- Writes: a temp file named `{certificate_id}.{uuid4}.tmp` (**unique per write**, so a stale and a current worker never share a temp file), opened exclusively with `O_NOFOLLOW` and mode `0600`, written, `fsync`ed, then `os.replace`d into place, and the directory is `fsync`ed. Readers never see a half-written PDF, and re-processing is idempotent (ADR-004). Job directories are `0700`.
- Orphans: on start, each worker deletes `*.tmp` files older than `2 × CERTGEN_LEASE_SECONDS` (left by killed workers).
- The database stores no path, because it is derivable. That avoids drift between database and disk.
- Reads are defensive: the job directory must be a real directory (not a symlink) directly under the root, the file must be a regular file opened with `O_NOFOLLOW`, at most 2 MiB (`MAX_PDF_BYTES`, also enforced when writing), and framed by `%PDF-` … `%%EOF`. Anything else is treated as missing.

## Consequences
- Single host only. That matches SQLite (ADR-002).
- If someone deletes or damages a file on disk, the download returns `410 CERTIFICATE_FILE_MISSING` instead of a 500 or a corrupt PDF.
- The data directory is a credential-adjacent location (it also holds the API token); keep it private.

## Operating notes
- **Local disk only:** SQLite WAL does not work on network filesystems.
- **Backup:** stop all processes, then copy the whole data directory (`certgen.sqlite3`, its `-wal`/`-shm` files if present, `certificates/`, and `api-token`, which is a credential).
- **Full reset:** stop all processes, delete the data directory, start again; the schema is recreated.
- **Orphans:** workers delete stale `*.tmp` files at startup. For a full cleanup, stop all processes, back up, delete any `*.tmp` and any PDF not referenced by a `succeeded` item, then restart.
- **Retention:** none. Jobs, items and files are kept until the operator removes the data directory.
- **Archive temp space:** ZIPs are built in the system temp directory (`TMPDIR`), up to two at once; a 10,000-certificate archive is several hundred MB.
- **Health:** `GET /healthz` returns `503` if the database cannot be read and `"degraded"` if embedded workers died. It cannot see standalone worker processes. Logs carry ids and codes, never names or emails.

## Tests
`tests/test_retrieval.py` (malformed and traversal-style ids give 404; missing and damaged files give 410), `tests/test_worker.py::test_atomic_write_no_partial_file`.

## At 100× scale
S3 with the same key layout. Serve downloads via pre-signed URLs (redirect), so the API never streams bytes.

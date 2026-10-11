# ADR-006: Idempotent job creation — optional `Idempotency-Key` header (differentiator D4)

Status: Accepted · Requirements: R1, R11 (bulk POST retried after timeout must not double-generate) · Evidence: RESEARCH §1

## Context
A bulk POST is the request most likely to time out on the client while succeeding on the server. A naive retry creates a second job and a second set of certificate IDs. That's a real defect for certificates, which are meant to be unique per recipient.

## Options
1. **Nothing** (simplest). Document "don't retry POST". Pushes the problem to every client.
2. **Natural-key dedupe**: same body hash within N minutes → same job. Implicit and surprising: two legitimately identical runs collapse.
3. **Client `Idempotency-Key` header** (Stripe model, chosen). Optional. Explicit intent.

## Decision
**Option 3.**
- `jobs.idempotency_key UNIQUE`, `jobs.request_hash = sha256(canonical JSON of the validated body)`.
- Same key + same hash → `200 OK`, the existing job, header `Idempotent-Replayed: true`. It returns **current** job state, not a frozen snapshot. That's a stated deviation from Stripe's "saved response", and it's more useful for polling clients.
- Same key + different hash → `409 IDEMPOTENCY_KEY_REUSED` (Stripe: "errors if they're not the same").
- Request-level `422`s are **not** recorded, so the client fixes the body and retries with the same key (Stripe: validation failures are not saved).
- Concurrent duplicates: creation runs in one `write_engine` transaction (`BEGIN IMMEDIATE`, ADR-002), so look-up-key-then-insert is already serialized and two same-key requests can't interleave. The `UNIQUE` index is a **backstop** (brandur): if it ever fires, the handler re-reads and applies the rules above. That branch is not reachable under the current transaction mode, and we say so rather than claim a test exercises it (audit #27). The concurrent test still asserts exactly one job.
- `?dry_run=true` ignores the header entirely: it neither reads nor stores a key (ADR-009, audit #8).
- Key: 1–255 chars (Stripe's limit), printable ASCII.

## Consequences
- Keys never expire in v1. That's a documented limitation, and the 100× fix is a reaper after 24 h.
- Keys are global, not per-client, because there is no auth. With auth it becomes `UNIQUE(client_id, key)`.

## Tests
`test_idempotency.py`: replay returns same job id and no new items; different body → 409; 422 not stored; concurrent same-key POSTs (threads) → exactly one job.

## At 100× scale
Scope keys per tenant, add expiry and a reaper, and store keys in the same Postgres transaction.

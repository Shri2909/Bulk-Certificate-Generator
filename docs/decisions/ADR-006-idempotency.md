# ADR-006: Idempotent job creation — optional `Idempotency-Key` header

Status: Accepted · Requirements: R1, R11 (a bulk POST retried after a timeout must not double-generate)

## Context
A bulk POST is the request most likely to time out on the client while succeeding on the server. A naive retry creates a second job and a second set of certificate ids. That is a real defect for certificates, which are meant to be unique per recipient.

## Options
1. **Nothing** (simplest). Document "don't retry POST". Pushes the problem to every client.
2. **Natural-key dedupe**: same body hash within N minutes gives the same job. Implicit and surprising: two legitimately identical runs collapse.
3. **Client `Idempotency-Key` header** (the Stripe model, chosen). Optional, with explicit intent.

## Decision
**Option 3.**
- The key is 1–255 printable ASCII characters. It is stored in `jobs.idempotency_key` (`UNIQUE`) **prefixed with the id of the API token that sent it** (`{client_id}:{key}`; the client id is the first 16 hex digits of the token's SHA-256). Keys are therefore scoped per token: two tokens may use the same key independently.
- `jobs.request_hash` is the SHA-256 of the canonical JSON of the request body, taken **before** normalization (so `Zoë` composed vs. decomposed counts as a different body; key order and whitespace between tokens do not matter).
- Same key + same hash gives `200 OK`, the existing job, header `Idempotent-Replayed: true`. It returns the job's **current** state, not a frozen snapshot. That deviates from Stripe's "saved response" and is more useful for polling clients. The body is not validated again and no capacity check runs, because no new work is admitted.
- Same key + different hash gives `409 IDEMPOTENCY_KEY_REUSED`.
- Request-level `422`/`413`/`415` responses are **not** recorded, so the client fixes the body and retries with the same key.
- Concurrent duplicates: the key lookup and the insert run in one write transaction (`BEGIN IMMEDIATE`, ADR-002), so two requests with the same key cannot interleave, and the second becomes a replay. The `UNIQUE` index is a **backstop**: if it ever fires, the handler re-reads and applies the rules above. That branch is not reachable under the current transaction mode, and no test claims to exercise it. The concurrency test asserts exactly one job.
- `?dry_run=true` ignores the header entirely: it neither reads nor stores a key (ADR-009).

## Consequences
- Keys never expire. That is a documented limitation; the fix is a reaper after about 24 h.
- A client that loses the response to a request **without** a key and resubmits creates a second job; the README tells clients to keep the returned `id`/`Location`.

## Tests
`tests/test_idempotency.py`: replay returns the same job id and no new items; different body gives 409; a 422 is not stored; concurrent same-key POSTs (threads) create exactly one job (`test_concurrent_same_key_creates_one_job`). Per-token scoping is tested in `tests/test_hardening.py`.

## At 100× scale
Add key expiry and a reaper, and keep keys in the same PostgreSQL transaction as the job.

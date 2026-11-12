# ADR-011: Limits

Status: Accepted · Requirements: R11

## Options
1. **No limits** (simplest). One 1M-row POST exhausts memory and fills the disk.
2. **Explicit limits on the scarce resources** (chosen): rows per job, field lengths, body size, queue depth, free disk, concurrent requests.
3. **Rate limiting (token bucket per client)**. Needs shared state and tuning; belongs at a gateway.

## Decision
All limits are configurable by environment variable (full list below), validated at startup. Defaults:

| Limit | Default | Why |
|---|---|---|
| `CERTGEN_MAX_RECIPIENTS` | 10,000 | About 2 minutes of rendering on one worker and several hundred MB of PDFs. The request validates in well under a second per thousand rows. |
| Request body | derived: `CERTGEN_MAX_RECIPIENTS × 4,500 B + 64 KiB` ≈ **45 MB** at 10,000 | Derived from the **worst legal row** rather than a guess: a 120-character name sent decomposed (NFD, up to 4 code points per accepted character) with every code point `\uXXXX`-escaped is 2,880 B, a 254-character email fully escaped is 1,524 B, plus keys and punctuation. A request within every documented field limit can therefore never be 413'd. Checked on `Content-Length` **and** while reading (chunked bodies); `413 PAYLOAD_TOO_LARGE`. Trade-off: a 45 MB JSON body takes several times that in Python objects while parsing, which is acceptable on one host, and the 100× path is CSV upload (ADR-005). |
| Row size | 4,500 B as canonical JSON | The largest a valid row can be; a larger row fails the whole request with `TOO_LONG` instead of being stored. |
| name / email / title, issuer | 120 / 254 / 200 chars | Name and title/issuer must also fit the template (ADR-007); 254 is the RFC 5321 path limit. |
| `Idempotency-Key` | 1–255 chars | Stripe's limit. |
| Items page `limit` | 1–1000 | Bounded response size. |
| `CERTGEN_MAX_ACTIVE_ITEMS` | 50,000 | Unfinished items across all jobs; more gets `503 CAPACITY_EXCEEDED`. A submission to a full queue is refused before it is validated. |
| `CERTGEN_MIN_FREE_DISK_MB` | 1,024 | New jobs and retries get `503 STORAGE_FULL` below this much free disk. |
| Requests in progress | 64 per process, `CERTGEN_MAX_REQUESTS_PER_TOKEN` (32) per API token | Counted in the app (not as uvicorn connections), from the headers on, so idle sockets cannot lock clients out. One token cannot take every slot from other tokens or from `/healthz`. More gets `503 SERVER_BUSY`. |
| `CERTGEN_MAX_CONCURRENT_SUBMISSIONS` | 2 per process | Parsing and validating a large body is the expensive step; more get `503 SERVER_BUSY` before the body is read. |
| Archive builds | 2 per process | A 10,000-PDF ZIP is several hundred MB; more gets `503 ARCHIVE_BUSY`. |
| Timeouts | 30 s | A request body must arrive within 30 s (`408 REQUEST_TIMEOUT`); the client must accept each piece of a response within 30 s, else the download is aborted. A slow client therefore holds a slot for about 30 s, not indefinitely. |

Identity for limits is the API token (ADR-009, "Authentication"). This is a concurrency cap per token, not a request-rate limit: rate limiting and per-tenant quotas stay a gateway concern.

## Settings reference
All settings are environment variables, read only in `certgen/config.py` and validated at startup (an invalid value stops the process with a message naming the variable).

| Variable | Default | Allowed | Meaning |
|---|---|---|---|
| `CERTGEN_DATA_DIR` | `./data` | local path | SQLite file and certificate files |
| `CERTGEN_HOST` / `CERTGEN_PORT` | `127.0.0.1` / `8000` | IP address / 1-65535 | bind address and port for `python -m certgen` |
| `CERTGEN_EMBEDDED_WORKER` | `true` | `1/0/true/false` | run worker threads inside the API process |
| `CERTGEN_WORKER_THREADS` | `2` | 1-16 | embedded worker threads |
| `CERTGEN_LEASE_SECONDS` | `30` | 1-3600 | claim lease; an unfinished item is reclaimed after this |
| `CERTGEN_MAX_ATTEMPTS` | `3` | 1-10 | attempts per item before `failed` |
| `CERTGEN_RETRY_DELAY_SECONDS` | `2` | 0-3600 | wait before retrying a failed attempt |
| `CERTGEN_POLL_INTERVAL_SECONDS` | `0.5` | 0.05-10 | worker idle sleep |
| `CERTGEN_MAX_RECIPIENTS` | `10000` | 1-100000 | recipients per request (also sets the body limit) |
| `CERTGEN_MAX_ACTIVE_ITEMS` | `50000` | `MAX_RECIPIENTS`-1000000 | unfinished items across all jobs |
| `CERTGEN_PRINT_EMAIL_ON_CERTIFICATE` | `true` | `1/0/true/false` | print the email under the name; stored per job (ADR-007) |
| `CERTGEN_LOG_LEVEL` | `INFO` | `DEBUG/INFO/WARNING/ERROR` | `certgen` logger level |
| `CERTGEN_ALLOWED_HOSTS` | empty | comma-separated host names / IPs | `Host` names accepted in addition to the loopback names (DNS-rebinding defence); add the public name behind a proxy |
| `CERTGEN_API_TOKENS` | empty | comma-separated, 16-256 chars each | bearer tokens accepted on `/v1`; empty means use (or create) `{CERTGEN_DATA_DIR}/api-token` |
| `CERTGEN_MIN_FREE_DISK_MB` | `1024` | 0-1000000 | free-disk floor for new jobs and retries; `0` disables the check |
| `CERTGEN_MAX_CONCURRENT_SUBMISSIONS` | `2` | 1-64 | submissions parsed and validated at once per process |
| `CERTGEN_MAX_REQUESTS_PER_TOKEN` | `32` | 1-64 | requests in progress per API token (64 per process) |
| `CERTGEN_TEST_SLOW_RENDER_MS` | `0` | 0-60000, below the lease | **test only**: delay each render so crash tests can kill a worker mid-item |

Run every process (API and standalone workers) with the same lease, attempt and retry-delay settings: each process applies its own values when it reclaims or retires items.

## Consequences
- A refused submission is answered before its body is read and the connection is closed, so a client still sending a large body may see a connection reset instead of the `503`; `Expect: 100-continue` avoids that.
- Clients sharing one token share its slots, and a token holder that keeps two slow uploads going can hold both submission slots. Give each client its own token.
- Parsing one maximal request (about 45 MB) takes roughly 0.7-0.9 GB of memory; at most 2 run at once. Streaming ingestion or a lower `CERTGEN_MAX_RECIPIENTS` would reduce it.
- An aborted or idle connection keeps its file descriptor until the client closes it. Before exposing the API to untrusted clients, put it behind a reverse proxy that limits idle connections and header/body timeouts.

## Tests
`tests/test_limits.py` and `tests/test_hardening.py`: `MAX_RECIPIENTS + 1` gives 422; a body over the limit gives 413; a worst-case body (all rows at maximum length, `\u`-escaped decomposed text) at `MAX_RECIPIENTS` is **accepted**; 120-character name valid, 121 gives `TOO_LONG`; request slots are limited per token and released when the app raises; submissions and archives are capped.

## At 100× scale
Per-tenant quotas (jobs in flight, recipients per day) using a token bucket in PostgreSQL or Redis. Large jobs move to CSV upload (ADR-005).

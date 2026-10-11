# ADR-011: Limits

Status: Accepted · Requirements: R11 · Evidence: RESEARCH §4 (AlgoMaster rate limiting: "limit the resource that can actually run out"), §5 (OWASP size limits)

## Options
1. **No limits** (simplest). One 1M-row POST exhausts memory and fills the disk.
2. **Static limits on the scarce resources** (chosen): rows per job, field lengths, body size.
3. **Rate limiting (token bucket per client)**. Needs client identity (no auth in scope) and shared state.

## Decision — all configurable via env, defaults:
| Limit | Default | Why |
|---|---|---|
| `MAX_RECIPIENTS` | 10,000 | ~3.4 min of rendering on one worker (20 ms each), ~240 MB of PDFs. The request validates in well under a second. |
| `MAX_BODY_BYTES` | 5 MB | 10,000 rows × ~300 bytes ≈ 3 MB plus headroom. Checked on `Content-Length` **and** while reading (chunked bodies), → 413 `PAYLOAD_TOO_LARGE`. |
| name length | 120 chars | Fits the template at the minimum font size. |
| email length | 254 chars | RFC 5321 path limit (enforced by `email-validator`). |
| title / issuer | 200 chars | Fits two lines on the template. |
| `Idempotency-Key` | 255 chars | Stripe's limit. |
| items page `limit` | 1–1000 | Bounded response size. |

Rate limiting → **rejected for v1**: there's no identity to key on, and it belongs at the gateway. Documented as the first add with auth.

## Tests
`test_limits.py`: `MAX_RECIPIENTS + 1` → 422 (using a monkeypatched small limit for speed); body > `MAX_BODY_BYTES` → 413; 120-char name valid, 121 → `TOO_LONG`.

## At 100× scale
Per-tenant quotas (jobs in flight, recipients/day) using a token bucket in Postgres or Redis. Large jobs move to CSV upload (ADR-005).

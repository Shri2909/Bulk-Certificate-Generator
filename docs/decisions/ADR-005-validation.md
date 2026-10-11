# ADR-005: Validation — structural errors reject the request; recipient errors are per-row

Status: Proposed (→ Gate Q1, Q2) · Requirements: R2, R3, R10, R15 · Evidence: RESEARCH §5

## Context
"Validate the provided recipient data" + "Generate a certificate for **each valid** recipient" implies some recipients may be invalid while others proceed.

## Options
1. **Reject the whole request on any invalid row** (simplest). One typo in row 4,812 of 5,000 blocks everything, and the client must fix and resubmit all of them.
2. **Partial acceptance** (chosen). Structural errors → `422`. Row errors → the row becomes an item with status `rejected` + `error_code`, and valid rows proceed. All rows invalid → `422`.
3. **Client-selected mode** (`?strict=true|false`). Most flexible, but two code paths and two test matrices, for a choice the client can already make with D3 `?dry_run=true`.

## Criteria
Correctness (spec says "each valid"), R10 visibility, simplicity.

## Decision
**Option 2**, with D3 `dry_run` as the "check before you commit" path instead of a strict mode.

Two layers:
1. **Request layer (Pydantic, → 422 whole request):** JSON shape. `certificate.title` 1–200 chars, `certificate.issuer` 1–200 chars, `certificate.issued_on` an ISO date not in the future (semantic check, OWASP). `recipients` is a list of 1..`MAX_RECIPIENTS` (10,000) objects. Unknown fields forbidden (`extra="forbid"`), so a typo like `nmae` fails loudly.
2. **Row layer (`validation.py`, → per-row errors):** each recipient is checked independently, and **all** errors per row are collected:
   | Code | Rule |
   |---|---|
   | `REQUIRED` | `name` / `email` missing or blank after trim |
   | `INVALID_TYPE` | not a string |
   | `TOO_LONG` | name > 120 chars after NFC normalization |
   | `INVALID_CHARACTERS` | contains control / format characters (Unicode category `C*`) |
   | `UNSUPPORTED_CHARACTERS` | a char missing from the bundled font, or a strong-RTL char (ADR-007) |
   | `INVALID_EMAIL` | fails `email-validator` syntax check (no DNS lookup; deterministic tests) |
   | `DUPLICATE_RECIPIENT` | normalized email (casefold) already seen **earlier** in this request |

   Row-level checks are done by hand, not by Pydantic per row, so one bad row can't fail the request model. The row index is the original array position.

Names are an allowlist of **printable Unicode the font can draw**, not a regex of "letters". `O'Brien`, `Jean-Luc`, `Zoë` are valid (OWASP: blocking apostrophes "rejects legitimate names").

## Consequences
- `202` responses carry `counts.rejected`, so a client that ignores row errors still sees them in job status.
- Duplicate policy (first wins, later rows rejected) is an interpretation → Gate Q2.

## At 100× scale
For 100k+ recipients, accept CSV upload streamed to disk and validate in the worker (the job enters `validating`). JSON bodies of 100k rows (~10 MB) are too large to validate in-request.

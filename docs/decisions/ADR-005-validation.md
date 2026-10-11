# ADR-005: Validation — structural errors reject the request; recipient errors are per-row

Status: Accepted (Gate 1 Q1, Q2) · Requirements: R2, R3, R10, R15 · Evidence: RESEARCH §5

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
   | `UNSUPPORTED_CHARACTERS` on `email` | only when `PRINT_EMAIL_ON_CERTIFICATE=true` (ADR-007), because the email is then rendered (internationalized addresses may contain non-ASCII) |
   | `DUPLICATE_RECIPIENT` | `email.strip().casefold()` already seen **earlier in this request**. Message names the first occurrence: `"duplicate of row 3"`, and the error also carries `"duplicate_of_row_index": 3` for machines. Only valid rows register as first occurrences: if row 3's email is malformed, row 7 with the same string is reported for its own malformed email, not as a duplicate. **Duplicates across different jobs are allowed** (re-issuing next year's course to the same person is normal). |

   Row-level checks are done by hand, not by Pydantic per row, so one bad row can't fail the request model. The row index is the original array position.

Names are an allowlist of **printable Unicode the font can draw**, not a regex of "letters". `O'Brien`, `Jean-Luc`, `Zoë` are valid (OWASP: blocking apostrophes "rejects legitimate names").

## Consequences
- `202` responses carry `counts.rejected`, so a client that ignores row errors still sees them in job status.
- Duplicate policy (first occurrence wins, later rows rejected, scoped to one job) is the Gate 1 Q2 decision.
- `422` for the whole request applies only to: malformed JSON, missing/invalid top-level fields (`certificate.*`, `recipients`), empty or over-limit `recipients`, oversized body, or **every** row invalid (Gate 1 Q1). The all-invalid 422 still returns every row's errors.

## At 100× scale
For 100k+ recipients, accept CSV upload streamed to disk and validate in the worker (the job enters `validating`). JSON bodies of 100k rows (~10 MB) are too large to validate in-request.

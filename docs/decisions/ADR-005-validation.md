# ADR-005: Validation — structural errors reject the request; recipient errors are per-row

Status: Accepted · Requirements: R2, R3, R10, R15

## Context
"Validate the provided recipient data" + "Generate a certificate for **each valid** recipient" implies some recipients may be invalid while others proceed.

## Options
1. **Reject the whole request on any invalid row** (simplest). One typo in row 4,812 of 5,000 blocks everything, and the client must fix and resubmit all of them.
2. **Partial acceptance** (chosen). Structural errors give `422`. Row errors turn the row into an item with status `rejected` plus `errors`, and valid rows proceed. All rows invalid gives `422`.
3. **Client-selected mode** (`?strict=true|false`). Most flexible, but two code paths and two test matrices, for a choice the client can already make with `?dry_run=true`.

## Criteria
Correctness (the assignment says "each valid"), R10 visibility, simplicity.

## Decision
**Option 2**, with `?dry_run=true` as the "check before you commit" path instead of a strict mode.

Two layers:
1. **Request layer (→ 422 for the whole request).**
   - *Shape* (`models.py`, Pydantic): JSON must be strict (no duplicate keys, `NaN`/`Infinity`, unpaired surrogates, nesting deeper than 8); `certificate` and `recipients` are required; unknown fields are forbidden (`extra="forbid"`), so a typo like `nmae` fails loudly. Recipients stay plain dicts so one bad row cannot fail the request model.
   - *Certificate fields* (`validation.py`): `title` and `issuer` 1–200 characters after normalization and must fit the template; `issued_on` a real `YYYY-MM-DD` date no later than **today (UTC) + 1 day** (the slack allows for clients up to UTC+14, whose "today" is already tomorrow in UTC).
   - *Recipients*: a list of 1..`CERTGEN_MAX_RECIPIENTS` (10,000) entries, each at most 4,500 bytes as JSON (the largest a valid row can be; a larger one is `TOO_LONG` on `recipients` and nothing is stored).
2. **Row layer (`validation.py`, → per-row errors).** Each recipient is checked independently and **all** errors per row are collected, in the order `name`, `email`, then unknown fields:

   | Code | Rule |
   |---|---|
   | `REQUIRED` | `name` / `email` missing, `null` or blank after trim |
   | `INVALID_TYPE` | not a string (the column is stored as `NULL`, ADR-002) |
   | `TOO_LONG` | name > 120 characters after NFC normalization |
   | `INVALID_CHARACTERS` | control, format or non-space whitespace characters (Unicode categories `C*`, `Z*` other than the plain space) |
   | `UNSUPPORTED_CHARACTERS` | a character missing from the bundled font, above U+FFFF, a strong right-to-left character, or a combining mark (ADR-007) |
   | `DOES_NOT_FIT` | the text cannot fit its box at the minimum font size (`fit_text`, shared with the renderer) |
   | `INVALID_EMAIL` | longer than 254 characters (checked first), not ASCII, or fails the `email-validator` syntax check (no DNS lookup, so tests are deterministic) |
   | `UNKNOWN_FIELD` | a key other than `name` and `email`; at most 10 are listed (sorted), further ones are counted in one summary error with `field: null` |
   | `DUPLICATE_RECIPIENT` | the lower-cased, trimmed email was already seen on an **earlier valid row of this request**. The message names the first occurrence, and the error also carries `"duplicate_of_row_index"` for machines. Only valid rows register as first occurrences: if row 3's email is malformed, row 7 with the same string is reported for its own malformed email, not as a duplicate. **Duplicates across different jobs are allowed** (re-issuing next year's course to the same person is normal). |

   All errors of a row are stored in `items.errors` and returned verbatim in the `202` body, the all-invalid `422`, the dry-run report and the items listing. The row index is the original array position.

Row-level checks are done by hand, not by Pydantic per row. Names are an allowlist of **printable Unicode the font can draw**, not a regex of "letters": `O'Brien`, `Jean-Luc` and `Zoë` are valid, because blocking apostrophes "rejects legitimate names" (OWASP). Emails are ASCII-only, so no character-set check beyond that is needed, and a valid email (254 of the widest ASCII glyph) always fits when printed.

### Supported names
Names, titles and issuers may use any Basic Multilingual Plane character (U+0000-U+FFFF) that both vendored DejaVu Sans fonts can draw: Latin (including accented and Vietnamese letters), Greek and Cyrillic, plus common punctuation (`Seán O'Connor-Smith`, `Ελένη`, `Дмитрий`). Rejected with a code instead of printing a wrong certificate:
- CJK and other scripts the font lacks, characters above U+FFFF (emoji), right-to-left scripts (Hebrew, Arabic) and leftover combining marks: `UNSUPPORTED_CHARACTERS` (the reasons are in ADR-007);
- tabs, zero-width and bidi control characters, non-breaking and other non-ASCII spaces: `INVALID_CHARACTERS`.

Long text shrinks and wraps (name 40 to 16 pt on up to 2 lines, title 24 to 12 pt on up to 3 lines); text that still cannot fit, such as 120 capital `W`s, is `DOES_NOT_FIT`. Names are NFC-normalized with spaces collapsed. More scripts would need text shaping (HarfBuzz) and more fonts.

## Consequences
- `202` responses carry `counts.rejected`, so a client that ignores row errors still sees them in job status.
- `422` for the whole request applies only to: malformed JSON, missing or invalid top-level fields (`certificate.*`, `recipients`), empty or over-limit `recipients`, an oversized row, or **every** row invalid. (An oversized body is `413 PAYLOAD_TOO_LARGE`, ADR-011.) The all-invalid `422` still returns every row's errors.
- Because validation shares `fit_text` with the renderer, an accepted row never overflows the page (ADR-007).

## At 100× scale
For 100k+ recipients, accept a CSV upload streamed to disk and validate in the worker (this would add a new job status, `validating`, which does not exist today). JSON bodies of 100k rows (tens of MB) are too large to validate in-request.

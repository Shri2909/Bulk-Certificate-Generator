# ADR-007: PDF library and template — ReportLab canvas, template-as-code, bundled DejaVu fonts

Status: Accepted · Requirements: R4, R5, R16

## Context
One fixed design (R4) showing recipient data (R5). It must `pip install` with no system libraries (P3) and handle real-world names.

## Options
1. **ReportLab canvas** (chosen). Pure pip wheel. `stringWidth` and `drawCentredString` for layout; about 4 ms per PDF in the benchmark.
2. **WeasyPrint (HTML/CSS template)**. Nicest authoring, but needs Pango system libraries (MSYS2 on Windows), which breaks P3.
3. **fpdf2**. Pure Python, TTF plus text shaping for RTL. A real contender, but shaping needs extra packages.
4. **Fill a static PDF form (pypdf)** (simplest "template"). Design in a PDF editor, fill fields. Font and Unicode handling of form fields is viewer-dependent, so it is hard to guarantee R5 renders.

## Criteria
P3 compliance, Unicode correctness, speed, explainability.

## Decision
**ReportLab canvas**, with the template defined in `certgen/rendering.py` as one function and named layout constants (`certgen/layout.py`). Landscape A4: double border, "Certificate of Completion", the recipient name, the email (optional), the course `title`, the `issuer`, the issue date and the certificate id.

**Text fitting.** Each variable text sits in a block with a maximum and minimum font size and a maximum number of lines; `fit_text` picks the largest size whose greedy word wrap fits:

| Field | Size (pt) | Max lines |
|---|---|---|
| name | 40 down to 16 | 2 |
| title | 24 down to 12 | 3 |
| issuer | 16 down to 10 | 2 |
| email | 13 down to 7 | 3 (breaks anywhere) |

Validation calls the same `fit_text` (ADR-005), so a row is accepted only if it will be drawn inside its box. The renderer raises instead of truncating if it ever receives text that does not fit.

**Render inputs come only from the database** (item + job row), never from process environment: `print_email` is snapshotted onto the job at creation. A standalone worker with different settings renders the same bytes (ADR-004).

**Email on the certificate:** the name and email are both printed by default, so the certificate contains all the recipient-specific information in the request (R5). Printing puts contact details on a document that may be shared, so `CERTGEN_PRINT_EMAIL_ON_CERTIFICATE=false` is an explicit opt-out. The setting is read **at job creation** and stored on the job (`jobs.print_email`), so changing it never alters already-accepted jobs. Both modes are tested.

**Determinism:** canvases are created with `invariant=1`, so the same inputs produce byte-identical PDFs (needed by the idempotent rewrite in ADR-004). The title and author metadata are derived from the job, not from the clock.

**Fonts:** DejaVu Sans and DejaVu Sans Bold are **vendored** in `certgen/assets/` with their license (Bitstream Vera-derived, free redistribution). Relying on system fonts would make output machine-dependent. At import the app checks that both fonts cover all printable ASCII, which is why emails need no character-set check.

**Never print a wrong certificate silently.** A standard PDF font renders `李小龍` without error as missing glyphs. So at validation time (ADR-005) a character must exist in both DejaVu fonts, be in the Basic Multilingual Plane (above U+FFFF ReportLab writes an invalid text mapping, so copied text would differ), have no right-to-left bidi class (ReportLab does no shaping, so Hebrew/Arabic would print reversed and disconnected), and not be a leftover combining mark. Otherwise the row is `UNSUPPORTED_CHARACTERS`. Covered: Latin including Vietnamese and Polish, Greek, Cyrillic. Not covered: CJK, RTL. The fix path is fpdf2 with shaping, or a Noto font set.

## Consequences
- The template is Python, not a designer-editable file. That is acceptable because the assignment rules out a template editor.
- Vendored fonts add about 1.4 MB to the repository.
- No QR code or verification link is printed (ADR-013).

## Tests
`tests/test_rendering.py`: output is a PDF; extracted text (via `pypdf`, a test-only dependency) contains name, title, issuer, date and certificate id; the email is printed by default and absent with the opt-out; Unicode names round-trip through text extraction; text that cannot fit raises instead of overflowing; two renders of the same input are byte-identical. `tests/test_layout.py` covers the fitting rules, including 254 wide characters in an email.

## At 100× scale
Rendering is CPU-bound and embarrassingly parallel: scale worker processes. To support more designs, keep the `render(data) -> bytes` signature and add a template registry keyed by a `template_id` on the job.

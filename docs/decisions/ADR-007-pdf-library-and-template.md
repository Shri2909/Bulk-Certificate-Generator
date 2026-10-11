# ADR-007: PDF library and template — ReportLab canvas, template-as-code, bundled DejaVu font

Status: Accepted (Gate 1 A3) · Requirements: R4, R5, R16 · Evidence: RESEARCH §6 [measured]

## Context
One fixed design (R4) showing recipient data (R5). Must `pip install` with no system libraries (P3), and must handle real-world names.

## Options
1. **ReportLab canvas** (chosen). Pure pip wheel. `drawCentredString`/`stringWidth` for layout. Built-in QR widget. Measured 20.3 ms/PDF.
2. **WeasyPrint (HTML/CSS template)**. Nicest authoring, but needs Pango ≥ 1.44 system libs (MSYS2 on Windows), which breaks P3.
3. **fpdf2**. Pure Python, TTF + text shaping for RTL. A real contender, but QR needs another dependency, and shaping needs extra packages.
4. **Fill a static PDF form (pypdf)** (simplest "template"). Design in a PDF editor, fill fields. Font/Unicode handling of form fields is viewer-dependent, so it's hard to guarantee R5 renders.

## Criteria
P3 compliance, Unicode correctness, speed, explainability.

## Decision
**ReportLab canvas**, template defined in `certgen/rendering.py` as one function with named layout constants. Landscape A4: border, title "Certificate of Completion", recipient name (auto-shrunk from 40 pt down to 18 pt via `stringWidth` to fit the width), course `title`, `issuer`, `issued_on`, certificate ID. The verification QR is added by D1 in milestone M7 (ADR-013); the required-scope template (`v1.0-required`) prints the certificate ID only (audit #16).

**Render inputs come only from the DB** (item + job row), never from process env: `print_email` and `verify_base_url` are snapshotted onto the job at creation. A standalone worker with different env renders the same bytes (ADR-004, audit #7).

**Email on the certificate (Gate 1 A3):** stored and returned by the API, **not printed by default**. Certificates are shared publicly (LinkedIn, portfolios), and printing an email there leaks contact PII for no verification benefit. The name + certificate ID + QR identify the recipient. One setting, `PRINT_EMAIL_ON_CERTIFICATE` (env, default `false`), adds an email line under the name. It is read **at job creation** and stored on the job (`jobs.print_email`), so changing it never alters already-accepted jobs. It exists so a strict reading of "recipient-specific information provided in the request" can be satisfied by one config change. Both modes are tested.

**Determinism:** canvases are created with `invariant=1`, so the same inputs produce byte-identical PDFs (needed by ADR-004's idempotent rewrite; [measured] in RESEARCH §6).

**Fonts:** DejaVu Sans + DejaVu Sans Bold are **vendored** in `certgen/fonts/` with their license (Bitstream Vera-derived, free redistribution). Relying on system fonts would make output machine-dependent.

**Never print a wrong certificate silently.** [measured] Helvetica renders `李小龍` without error as missing glyphs. So at validation time (ADR-005):
- every character must exist in the DejaVu cmap, **and**
- no character may have bidi class `R`/`AL` (Hebrew/Arabic). ReportLab has no shaping, so these would print letter-reversed and disconnected.

Otherwise the row is `UNSUPPORTED_CHARACTERS`. Covered: Latin incl. Vietnamese and Polish, Greek, Cyrillic. Not covered: CJK, RTL. This is a README limitation, and the fix path is swapping in fpdf2 + shaping or a Noto font.

## Consequences
- The template is Python, not a designer-editable file. That's acceptable because the spec rules out a template editor.
- Vendored fonts add ~1.4 MB to the repo.

## Tests
`test_rendering.py`: output starts with `%PDF`; extracted text (via `pypdf`, test-only dependency) contains name, title, issuer, date, certificate ID; a 120-char name stays within page width; a Unicode name (`Zoë Łukasz Ngọc`) round-trips through text extraction; email absent from extracted text by default and present with `PRINT_EMAIL_ON_CERTIFICATE=true`; two renders of the same input are byte-identical.

## At 100× scale
Rendering is CPU-bound and embarrassingly parallel: scale worker processes. To support more designs, keep the `render(item) -> bytes` signature and add a template registry keyed by `job.template_id`.

"""The single predefined certificate template (§10): CertificateData -> PDF bytes.

No DB, files, environment or clock: the output depends only on CertificateData, and ReportLab's
invariant mode removes timestamps and random IDs, so equal inputs give byte-identical PDFs.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

from certgen.fonts import FONT_BOLD, FONT_REGULAR
from certgen.layout import EMAIL, ISSUER, NAME, TITLE, Block, baselines, fit_text

PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)  # 841.89 x 595.28 pt
CENTER_X = PAGE_WIDTH / 2
PRIMARY = HexColor("#1F3A5F")
BODY = HexColor("#333333")
RECIPIENT = HexColor("#111111")
FOOTER_X = 60
MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


@dataclass(frozen=True)
class CertificateData:
    """Everything that can influence the output bytes; built only from the job and item rows (§10.4)."""

    job_id: str
    certificate_id: str
    name: str
    email: str | None  # None when the job's print_email snapshot is false
    title: str
    issuer: str
    issued_on: str  # YYYY-MM-DD
    verify_url: str | None = None


def format_date(iso_date: str) -> str:
    """'2026-10-01' -> '1 October 2026', independent of the process locale."""
    d = date.fromisoformat(iso_date)
    return f"{d.day} {MONTH_NAMES[d.month - 1]} {d.year}"


def render_certificate(data: CertificateData) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(PAGE_WIDTH, PAGE_HEIGHT), invariant=1)
    pdf.setTitle(f"Certificate {data.certificate_id}")
    pdf.setAuthor(data.issuer)
    pdf.setCreator("certgen")

    pdf.setStrokeColor(PRIMARY)
    pdf.setLineWidth(2.5)
    pdf.rect(20, 20, PAGE_WIDTH - 40, PAGE_HEIGHT - 40)
    pdf.setLineWidth(0.75)
    pdf.rect(28, 28, PAGE_WIDTH - 56, PAGE_HEIGHT - 56)

    _centered(pdf, "Certificate of Completion", FONT_BOLD, 34, 500, PRIMARY)
    _centered(pdf, "This is to certify that", FONT_REGULAR, 14, 458, BODY)
    _block(pdf, "name", data.name, NAME, 405, RECIPIENT)
    if data.email is not None:
        _block(pdf, "email", data.email, EMAIL, 352, BODY)
    _centered(pdf, "has successfully completed", FONT_REGULAR, 14, 322, BODY)
    _block(pdf, "title", data.title, TITLE, 276, PRIMARY)
    _centered(pdf, "Issued by", FONT_REGULAR, 11, 226, BODY)
    _block(pdf, "issuer", data.issuer, ISSUER, 196, BODY)

    pdf.setFillColor(BODY)
    pdf.setFont(FONT_REGULAR, 11)
    pdf.drawString(FOOTER_X, 110, f"Date of issue: {format_date(data.issued_on)}")
    pdf.setFont(FONT_REGULAR, 9)
    pdf.drawString(FOOTER_X, 92, f"Certificate ID: {data.certificate_id}")

    pdf.showPage()
    pdf.save()
    return buf.getvalue()


def _centered(
    pdf: canvas.Canvas, text: str, font: str, size: float, baseline: float, color: HexColor
) -> None:
    pdf.setFillColor(color)
    pdf.setFont(font, size)
    pdf.drawCentredString(CENTER_X, baseline, text)


def _block(pdf: canvas.Canvas, field: str, text: str, block: Block, center_y: float, color: HexColor) -> None:
    fit = fit_text(text, block)
    if fit is None:
        # Validation rejects such text first; refusing here keeps a bad row from printing off the page.
        raise ValueError(f"{field} does not fit its box")
    for line, baseline in zip(fit.lines, baselines(fit, center_y), strict=True):
        _centered(pdf, line, block.font, fit.size, baseline, color)

"""Certificate rendering: CertificateData -> PDF bytes. No DB, files, environment or clock."""

from __future__ import annotations

import io
from dataclasses import dataclass

from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas


@dataclass(frozen=True)
class CertificateData:
    """Everything that can influence the output bytes; built only from the job and item rows (§10.4)."""

    job_id: str
    certificate_id: str
    name: str
    email: str | None  # None when the job's print_email snapshot is false
    title: str
    issuer: str
    issued_on: str
    verify_url: str | None = None


def render_certificate(data: CertificateData) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=landscape(A4), invariant=1)
    pdf.drawString(60, 500, data.name)
    pdf.showPage()
    pdf.save()
    return buf.getvalue()

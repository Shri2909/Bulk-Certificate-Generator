"""Vendored DejaVu fonts (§10.1). Registered once at import so every process measures and draws alike."""

from __future__ import annotations

from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

ASSETS = Path(__file__).resolve().parent / "assets"
FONT_REGULAR = "CertSans"
FONT_BOLD = "CertSans-Bold"

_regular = TTFont(FONT_REGULAR, str(ASSETS / "DejaVuSans.ttf"))
_bold = TTFont(FONT_BOLD, str(ASSETS / "DejaVuSans-Bold.ttf"))
pdfmetrics.registerFont(_regular)
pdfmetrics.registerFont(_bold)

# A character is allowed only if both weights have a glyph for it; otherwise ReportLab silently
# draws an empty box, which would be a wrong certificate rather than an error.
SUPPORTED_CHARS: frozenset[str] = frozenset(
    chr(cp) for cp in set(_regular.face.charToGlyph) & set(_bold.face.charToGlyph)
)

# Emails are ASCII-only (§9), so they need no charset check as long as this holds.
_missing_ascii = {chr(cp) for cp in range(0x20, 0x7F)} - SUPPORTED_CHARS
if _missing_ascii:
    raise RuntimeError(f"vendored fonts lack printable ASCII characters: {sorted(_missing_ascii)}")

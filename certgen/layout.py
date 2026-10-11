"""Pure text fitting shared by validation and rendering (§10.3).

Validation calls the same function the renderer uses, so a row is accepted only if it will be drawn
inside its box, and the renderer never has to truncate or overflow.
"""

from __future__ import annotations

from dataclasses import dataclass

from reportlab.pdfbase import pdfmetrics

from certgen.fonts import FONT_BOLD, FONT_REGULAR

MAX_WIDTH = 680.0  # pt; the page is 841.89 pt wide and the inner border is inset 28 pt
LINE_HEIGHT = 1.2  # leading as a multiple of the font size
BASELINE_DROP = 0.35  # shifts a line's baseline below the block center by this fraction of the size


@dataclass(frozen=True)
class Block:
    font: str
    max_size: int
    min_size: int
    max_lines: int
    box_height: float
    break_anywhere: bool  # True for emails, which contain no spaces to break at

    def line_width(self, text: str, size: float) -> float:
        return float(pdfmetrics.stringWidth(text, self.font, size))


NAME = Block(FONT_BOLD, 40, 16, 2, 70, break_anywhere=False)
TITLE = Block(FONT_BOLD, 24, 12, 3, 66, break_anywhere=False)
ISSUER = Block(FONT_REGULAR, 16, 10, 2, 40, break_anywhere=False)
EMAIL = Block(FONT_REGULAR, 13, 7, 3, 34, break_anywhere=True)


@dataclass(frozen=True)
class Fit:
    size: int
    lines: tuple[str, ...]


def fit_text(text: str, block: Block) -> Fit | None:
    """Largest size (1 pt steps) whose greedy wrap fits the block's line count and height, else None."""
    # Widths scale linearly with size (TTF advance widths, no kerning), so measure each unit once.
    units = list(text) if block.break_anywhere else text.split(" ")
    unit_widths = [block.line_width(u, 1) for u in units]
    space = 0.0 if block.break_anywhere else block.line_width(" ", 1)
    joiner = "" if block.break_anywhere else " "
    for size in range(block.max_size, block.min_size - 1, -1):
        lines = _wrap(units, unit_widths, space, MAX_WIDTH / size)
        if lines is None:
            continue
        if len(lines) <= block.max_lines and len(lines) * LINE_HEIGHT * size <= block.box_height:
            return Fit(size=size, lines=tuple(joiner.join(line) for line in lines))
    return None


def _wrap(units: list[str], widths: list[float], space: float, limit: float) -> list[list[str]] | None:
    """Greedy wrap in unit-size widths; None if a single unit is wider than the line."""
    lines: list[list[str]] = []
    current: list[str] = []
    current_width = 0.0
    for unit, width in zip(units, widths, strict=True):
        if width > limit:
            return None
        if current and current_width + space + width <= limit:
            current.append(unit)
            current_width += space + width
        else:
            if current:
                lines.append(current)
            current, current_width = [unit], width
    lines.append(current)
    return lines


def baselines(fit: Fit, center_y: float) -> list[float]:
    """Baseline y of each line so the block is vertically centered on center_y (§10.3)."""
    leading = LINE_HEIGHT * fit.size
    n = len(fit.lines)
    return [center_y + (n - 1) * leading / 2 - i * leading - BASELINE_DROP * fit.size for i in range(n)]

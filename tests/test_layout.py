"""Text fitting shared by validation and rendering (§10.3), measured with the real vendored font metrics."""

from __future__ import annotations

import pytest

from certgen.fonts import FONT_REGULAR, SUPPORTED_CHARS
from certgen.layout import EMAIL, ISSUER, MAX_WIDTH, NAME, TITLE, Block, Fit, baselines, fit_text

# Strings of the lengths named in §10.3, chosen so they reproduce the blueprint's probe outcomes.
NAME_25 = "Alexandra Maria Rodriguez"
NAME_61 = "Wolfgang Nguyen Hohenberg Bartholomew Montgomery Mohammed Lee"
NAME_120 = (
    "Quintanilla Wadsworth Anna Quintanilla Wolfgang Hohenberg Montgomery Wadsworth "
    "Konstantin Whitmore Oluwaseun Worthington"
)
TITLE_85 = "Scalable Fundamentals with for Engineering Professional Systems Fundamentals Security"
TITLE_200 = (
    "Design Architecture Advanced Scalable Operations Analytics Security Data Cloud Professional "
    "Data Patterns Security Systems and Environments Cloud Cloud Program with Program Production "
    "Learning Systems"
)
ISSUER_200 = (
    "Design in Scalable Engineering and Engineering Scalable Architecture and Security for "
    "Infrastructure Patterns Python Engineering Operations Security Modern Architecture Systems "
    "Advanced Infrastructure"
)
EMAIL_48 = "alexandra.rodriguez.montenegro@example-group.org"


@pytest.mark.req("R5")
@pytest.mark.parametrize(
    ("text", "block", "length", "size", "lines"),
    [
        (NAME_25, NAME, 25, 40, 1),
        (NAME_61, NAME, 61, 28, 2),
        (NAME_120, NAME, 120, 16, 2),
        (TITLE_85, TITLE, 85, 24, 2),
        (TITLE_200, TITLE, 200, 16, 3),
        (ISSUER_200, ISSUER, 200, 13, 2),
        (EMAIL_48, EMAIL, 48, 13, 1),
    ],
)
def test_fit_outcomes_match_blueprint(text: str, block: Block, length: int, size: int, lines: int) -> None:
    assert len(text) == length
    fit = fit_text(text, block)
    assert fit is not None
    assert (fit.size, len(fit.lines)) == (size, lines)
    assert " ".join(fit.lines) == text or "".join(fit.lines) == text  # wrapping never drops characters


@pytest.mark.req("R5")
def test_120_wide_letters_do_not_fit() -> None:
    assert fit_text("W" * 120, NAME) is None


@pytest.mark.req("R5")
def test_single_word_wider_than_box_fails() -> None:
    # One unbreakable word wider than 680 pt at every allowed size cannot fit, however many lines exist.
    assert fit_text("W" * 60, NAME) is None
    # A word too wide at the maximum size is accepted at the first smaller size where it fits.
    fit = fit_text("W" * 20, NAME)
    assert fit is not None
    assert fit.size < NAME.max_size
    assert fit.lines == ("W" * 20,)


@pytest.mark.req("R5")
def test_lines_respect_width_height_and_count_limits() -> None:
    for text, block in [(NAME_120, NAME), (TITLE_200, TITLE), (ISSUER_200, ISSUER), ("W" * 254, EMAIL)]:
        fit = fit_text(text, block)
        assert fit is not None
        assert len(fit.lines) <= block.max_lines
        assert len(fit.lines) * 1.2 * fit.size <= block.box_height
        assert all(block.line_width(line, fit.size) <= MAX_WIDTH for line in fit.lines)


@pytest.mark.req("R5")
def test_baseline_formula() -> None:
    # L = 1.2 * size; baseline_i = center + (n - 1) * L / 2 - i * L - 0.35 * size
    assert baselines(Fit(size=20, lines=("a", "b")), center_y=405) == pytest.approx([410.0, 386.0])
    assert baselines(Fit(size=40, lines=("a",)), center_y=405) == pytest.approx([391.0])
    assert baselines(Fit(size=10, lines=("a", "b", "c")), center_y=100) == pytest.approx([108.5, 96.5, 84.5])


@pytest.mark.req("R5")
def test_worst_case_email_fits() -> None:
    # §10.3: a valid email is never rejected for layout. The blueprint's case is 254 x "W"; the
    # widest printable ASCII glyph in DejaVu Sans is actually "@" (1.000 em vs 0.989 em for "W"), so
    # 254 x "@" is checked too: a strictly wider bound than any real address, which has one "@".
    widest = max((chr(c) for c in range(0x21, 0x7F)), key=lambda ch: EMAIL.line_width(ch, 1))
    for text in ("W" * 254, widest * 254):
        fit = fit_text(text, EMAIL)
        assert fit is not None, text[:3]
        assert fit.size >= 7
        assert len(fit.lines) <= 3
    assert EMAIL.font == FONT_REGULAR


@pytest.mark.req("R5")
def test_font_covers_printable_ascii() -> None:
    assert {chr(c) for c in range(0x20, 0x7F)} <= SUPPORTED_CHARS

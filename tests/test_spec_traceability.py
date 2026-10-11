"""Fails the build if any requirement in docs/REQUIREMENTS.md has no test, or a test cites an unknown ID.

Static AST scan rather than a pytest collection hook, so the check is independent of which tests
were selected with -k / a file path: running one file can't make coverage look complete.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_FILE = ROOT / "docs" / "REQUIREMENTS.md"
TESTS_DIR = ROOT / "tests"

# A requirement is declared by a markdown table row that starts with its ID, e.g. "| R7  | ...".
REQ_ROW = re.compile(r"^\|\s*(R\d+)\s*\|", re.MULTILINE)


def declared_requirements() -> set[str]:
    return set(REQ_ROW.findall(REQUIREMENTS_FILE.read_text(encoding="utf-8")))


def _is_req_marker(func: ast.expr) -> bool:
    # Matches `pytest.mark.req(...)` and `mark.req(...)`.
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "req"
        and (
            (isinstance(func.value, ast.Attribute) and func.value.attr == "mark")
            or (isinstance(func.value, ast.Name) and func.value.id == "mark")
        )
    )


def req_markers() -> list[tuple[str, int, list[object]]]:
    """Every @pytest.mark.req(...) call in the tests: (file name, line, positional args)."""
    found = []
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_req_marker(node.func):
                found.append((path.name, node.lineno, list(node.args)))
    return found


def covered_requirements() -> dict[str, list[str]]:
    """Map requirement ID -> test files citing it."""
    covered: dict[str, list[str]] = {}
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_req_marker(node.func):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        covered.setdefault(arg.value, []).append(path.name)
    return covered


def test_requirements_file_declares_requirements() -> None:
    # Guards against the regex silently matching nothing after a formatting change.
    assert len(declared_requirements()) >= 25


def test_every_requirement_has_a_test() -> None:
    missing = sorted(declared_requirements() - covered_requirements().keys(), key=lambda r: int(r[1:]))
    assert not missing, f"Requirements without a @pytest.mark.req test: {missing}"


def test_no_test_cites_unknown_requirement() -> None:
    unknown = sorted(covered_requirements().keys() - declared_requirements())
    assert not unknown, f"Tests cite requirement IDs not in REQUIREMENTS.md: {unknown}"


def test_no_empty_or_non_literal_markers() -> None:
    # §24.16: an empty marker, or one whose IDs are not string literals, declares nothing.
    bad = [
        f"{name}:{line}"
        for name, line, args in req_markers()
        if not args or not all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in args)
    ]
    assert not bad, f"@pytest.mark.req markers without literal requirement IDs: {bad}"


def test_every_requirement_has_a_heading() -> None:
    # §24.16: REQUIREMENTS.md has one "### R<n> — <obligation>" heading per requirement row.
    text = REQUIREMENTS_FILE.read_text(encoding="utf-8")
    headings = set(re.findall(r"^### (R\d+) — \S", text, flags=re.MULTILINE))
    missing = sorted(declared_requirements() - headings, key=lambda r: int(r[1:]))
    assert not missing, f"Requirements without a '### R<n> — <obligation>' heading: {missing}"

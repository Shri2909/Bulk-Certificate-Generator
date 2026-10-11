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
    return isinstance(func, ast.Attribute) and func.attr == "req" and (
        isinstance(func.value, ast.Attribute) and func.value.attr == "mark"
        or isinstance(func.value, ast.Name) and func.value.id == "mark"
    )


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

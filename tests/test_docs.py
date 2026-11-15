"""Docs contract: the README stays short and true, links resolve, figures are backed by code or evidence."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import ROOT
from fastapi import FastAPI
from fastapi.testclient import TestClient

from certgen.config import ENV_NAMES, Settings
from certgen.errors import PROBLEMS, FieldCode, ProblemCode

README = ROOT / "README.md"
EVIDENCE = ROOT / "tests" / "fixtures" / "benchmarks"
DECISIONS = ROOT / "docs" / "decisions"
ADR_010 = DECISIONS / "ADR-010-error-model.md"
LINKED_DOCS = [README, ROOT / "ARCHITECTURE.md", ROOT / "METHODOLOGY.md"]
MAX_README_LINES = 200
# Headings the assignment requires the README to cover (R20-R25), as substrings of the "## " lines.
REQUIRED_HEADINGS = [
    "Architecture",
    "Quick start",
    "API",
    "Tests",
    "design decisions",
    "Documentation",
    "limitations",
]


def readme() -> str:
    return README.read_text(encoding="utf-8")


def prose(path: Path) -> str:
    """The file's text without fenced code blocks, so example URLs and `[x](y)` snippets are not links."""
    return re.sub(r"^```.*?^```", "", path.read_text(encoding="utf-8"), flags=re.S | re.M)


def slug(heading: str) -> str:
    """GitHub's heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    return {slug(h) for h in re.findall(r"^#+\s+(.*?)\s*$", prose(path), flags=re.M)}


@pytest.mark.req("R20", "R21", "R22", "R23", "R24", "R25")
def test_readme_is_short_and_has_the_required_sections() -> None:
    text = readme()
    assert len(text.splitlines()) <= MAX_README_LINES, "README is the front door: move detail to docs/"
    assert "TODO" not in text and "FIXME" not in text
    headings = re.findall(r"^## (.*)$", text, flags=re.M)
    for needle in REQUIRED_HEADINGS:
        assert any(needle in h for h in headings), f"README has no '## ...{needle}...' section"
    quick = text[text.index("## Quick start") :]
    for needle in ("3.11", "-m venv .venv", "pip install -r requirements.txt", "python -m certgen", "curl"):
        assert needle in quick, f"Quick start lacks {needle!r}"
    tests_section = text[text.index("## Tests and benchmarks") :]
    assert "pytest" in tests_section and "scripts/benchmark.py" in tests_section
    assert "/archive" in quick and "/v1/certificate-jobs/{job_id}" in quick
    for script in ("scripts/demo-live.sh", "scripts/check_live_demo.py"):
        assert script in text and (ROOT / script).exists()


@pytest.mark.req("R25")
def test_readme_has_one_mermaid_diagram_and_links_the_adrs() -> None:
    text = readme()
    diagrams = re.findall(r"^```mermaid\n(.*?)^```", text, flags=re.S | re.M)
    assert len(diagrams) == 1 and diagrams[0].lstrip().startswith("flowchart")
    assert "%%{init" not in diagrams[0], "a theme directive breaks GitHub dark mode"
    adrs = set(re.findall(r"\(docs/decisions/(ADR-\d+-[^)#]+\.md)", text))
    on_disk = {p.name for p in DECISIONS.glob("ADR-*.md")}
    assert {"ADR-003-sync-vs-background.md", "ADR-010-error-model.md"} <= adrs
    assert adrs <= on_disk


@pytest.mark.req("R25")
def test_relative_links_resolve() -> None:
    """Every relative link (and #anchor) in the README, ARCHITECTURE.md and METHODOLOGY.md resolves."""
    for doc in LINKED_DOCS:
        assert doc.exists(), f"{doc.name} is missing"
        broken = []
        for target in re.findall(r"\]\(([^)\s]+)\)", prose(doc)):
            if re.match(r"[a-z][a-z0-9+.-]*:", target):  # http(s), mailto
                continue
            path_part, _, fragment = target.partition("#")
            dest = (doc.parent / path_part).resolve() if path_part else doc
            missing_anchor = bool(fragment) and dest.suffix == ".md" and fragment not in anchors(dest)
            if not dest.exists() or missing_anchor:
                broken.append(target)
        assert not broken, f"{doc.name} has broken links: {broken}"


@pytest.mark.req("R12")
def test_readme_numbers_match_evidence() -> None:
    files = sorted(EVIDENCE.glob("benchmark-*.json"))
    assert files, "no benchmark evidence: run scripts/benchmark.py"
    text = readme()
    rows = {
        m[1]: m
        for m in re.finditer(r"^\| (\d+) \| ([\d.]+) \| ([\d.]+) \| \[(\S+?)\]\(\S+?\) \|$", text, flags=re.M)
    }
    assert len(rows) == len(files), "the README benchmark table needs exactly one row per evidence file"
    by_procs: dict[int, dict[str, Any]] = {}
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["schema_version"] == 1 and data["counts"]["succeeded"] == data["recipients"] == 1000
        by_procs[data["processes"]] = data
        row = rows[str(data["processes"])]
        assert row.group(2, 3, 4) == (
            f"{data['wall_ms'] / 1000:.2f}",
            f"{data['throughput_per_s']:.1f}",
            path.name,
        )
        assert f"({EVIDENCE.relative_to(ROOT)}/{path.name})" in text
    # The prose figures are the same measurements, rounded.
    assert f"{round(by_procs[1]['throughput_per_s'])} certificates/s with one worker process" in text
    assert f"and {round(by_procs[4]['throughput_per_s'])}/s with four" in text


@pytest.mark.req("R11")
def test_readme_limits_and_settings_match_the_code() -> None:
    text = readme()
    defaults = Settings.from_env({})
    assert f"up to {defaults.max_recipients:,} recipients" in text
    env_by_value = {env: getattr(defaults, field) for field, env in ENV_NAMES.items()}
    checked = 0
    for name, default in re.findall(r"^\| `(CERTGEN_[A-Z_]+)` \| `([^`]+)` \|", text, flags=re.M):
        assert name in env_by_value, f"{name} is not a setting"
        value = env_by_value[name]
        if isinstance(value, bool):
            assert default == str(value).lower(), name
        elif isinstance(value, int | float):
            assert float(default) == value, name
        checked += 1
    assert 4 <= checked <= 10, "the README lists the key settings only; certgen/config.py has the rest"


@pytest.mark.req("R25")
def test_readme_test_count_floor_is_current() -> None:
    """'450+ tests' is a floor rounded down to 50, so it only changes when the suite grows by a step."""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    collected = int(re.search(r"(\d+) tests? collected", out).group(1))  # type: ignore[union-attr]
    floor = collected // 50 * 50
    assert f"{floor}+ tests" in readme(), (
        f"{collected} tests are collected; update the README to '{floor}+ tests'"
    )


@pytest.mark.req("R2")
def test_error_code_tables_in_adr_010_match_the_code() -> None:
    adr = ADR_010.read_text(encoding="utf-8")
    start = adr.index("**Request-level codes**")
    request_table = adr[start : adr.index("**Row-level codes**")]
    for code in ProblemCode:
        status, _title = PROBLEMS[code]
        assert re.search(rf"`{code.value}`\s*\|\s*{status}\s*\|", request_table), f"{code.value} ({status})"
    documented = adr + (DECISIONS / "ADR-005-validation.md").read_text(encoding="utf-8")
    for field_code in FieldCode:
        assert f"`{field_code.value}`" in documented, (
            f"{field_code.value} is documented in neither ADR-010 nor ADR-005"
        )


@pytest.mark.req("R13")
@pytest.mark.usefixtures("client")
def test_stack_is_fastapi_and_sqlite(app: FastAPI) -> None:
    pins = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "fastapi==" in pins and "sqlalchemy==" in pins
    assert isinstance(app, FastAPI)
    assert app.state.engines.write.dialect.name == "sqlite"
    assert app.state.engines.read.dialect.name == "sqlite"


@pytest.mark.req("R23")
def test_readme_api_table_lists_every_endpoint(app: FastAPI) -> None:
    table = re.findall(r"^\| `([A-Z]+) (/\S+?)(?:\?\S+)?` \|", readme(), flags=re.M)
    documented = {(method, path) for method, path in table}
    served = {
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        for method in ops
        if path.startswith(("/v1", "/healthz", "/metrics"))
    }
    assert documented == served


@pytest.mark.req("R23")
def test_readme_curl_example_is_valid(client: TestClient) -> None:
    payloads = [json.loads(body) for body in re.findall(r"-d '(\{.*?\})'", readme(), flags=re.S)]
    assert payloads, "README has no curl submission example"
    for payload in payloads:
        resp = client.post("/v1/certificate-jobs", json=payload)
        assert resp.status_code == 202, resp.text


@pytest.mark.req("R25")
def test_readme_cdn_note_matches_the_docs_page(client: TestClient) -> None:
    # If /docs stops using the CDN, update the README note.
    assert "cdn.jsdelivr.net" in client.get("/docs").text
    assert "cdn.jsdelivr.net" in readme() and "/openapi.json" in readme()
    assert client.get("/openapi.json").status_code == 200

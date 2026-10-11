"""README contract (PDF "Documentation", §17 M6, §24.15) and evidence-backed numbers (§17)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import ROOT
from fastapi import FastAPI
from fastapi.testclient import TestClient

README = ROOT / "README.md"
EVIDENCE = ROOT / "docs" / "evidence"
REQUIRED_HEADINGS = [
    "## Setup",
    "## Run",
    "## Run tests",
    "## Submit a certificate generation request",
    "## Retrieve generated certificates",
    "## Design decisions",
    "## Error codes",
    "## Limitations",
    "## Blueprint conformance",
    "## Supported names",
    "## Configuration",
    "## Benchmarks",
    "## Recovery and guarantees",
    "## Operations",
]


def readme() -> str:
    return README.read_text(encoding="utf-8")


def section(heading: str) -> str:
    """Body of one `## ` section, up to the next `## ` heading."""
    text = readme()
    start = text.index(heading + "\n") + len(heading) + 1
    nxt = text.find("\n## ", start)
    return text[start : nxt if nxt != -1 else len(text)]


@pytest.mark.req("R20", "R21", "R22", "R23", "R24", "R25")
def test_readme_has_required_sections() -> None:
    lines = readme().splitlines()
    for heading in REQUIRED_HEADINGS:
        assert heading in lines, f"README is missing {heading!r}"
        assert len(section(heading).strip()) > 80, f"{heading} is too thin to explain anything"
    first_screen = "\n".join(lines[:60])
    for needle in (
        "3.11",
        "-m venv .venv",
        "pip install -r requirements.txt",
        "python -m certgen",
        "curl",
    ):
        assert needle in first_screen, f"first screen lacks {needle!r}"
    assert "pytest" in section("## Run tests")
    assert "/archive" in section("## Retrieve generated certificates")
    assert "/v1/certificates/" in section("## Retrieve generated certificates")


@pytest.mark.req("R25")
def test_readme_adr_links_exist() -> None:
    links = re.findall(r"\]\(((?:docs|scripts|tests|certgen)/[^)#\s]+)", readme())
    adrs = [link for link in links if "/decisions/ADR-" in link]
    assert len(adrs) >= 5, "design decisions should link the ADRs that justify them"
    missing = [link for link in links if not (ROOT / link).exists()]
    assert not missing, f"README links to files that do not exist: {missing}"


@pytest.mark.req("R12")
def test_readme_documents_background_processing_choice() -> None:
    text = section("## Design decisions").lower()
    for needle in ("background", "synchronous", "worker", "poll", "lease", "progress"):
        assert needle in text, f"design decisions do not discuss {needle!r}"
    assert "docs/decisions/ADR-003-sync-vs-background.md" in section("## Design decisions")


def _benchmark_files() -> list[Path]:
    return sorted(EVIDENCE.glob("benchmark-*.json"))


def benchmark_row(data: dict[str, Any], name: str) -> str:
    """The README table row for one evidence file; the README must contain it verbatim."""
    cells = [
        f"{data['processes']}",
        f"{data['recipients']}",
        f"{data['wall_ms'] / 1000:.2f}",
        f"{data['throughput_per_s']:.1f}",
        f"{data['render_ms']['p50']:.1f}",
        f"{data['per_item_ms']['p50']:.1f}",
        f"{data['per_item_ms']['max']:.1f}",
        f"{data['post_latency_ms']:.0f}",
        f"[{name}](docs/evidence/{name})",
    ]
    return "| " + " | ".join(cells) + " |"


@pytest.mark.req("R12")
def test_readme_numbers_match_evidence() -> None:
    files = _benchmark_files()
    assert files, "no benchmark evidence: run scripts/benchmark.py"
    bench = section("## Benchmarks")
    table_rows = [line for line in bench.splitlines() if re.match(r"\| \d", line)]
    expected = []
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["schema_version"] == 1
        assert data["counts"]["succeeded"] == data["recipients"]
        expected.append(benchmark_row(data, path.name))
    # Every evidence file has its exact row, and no row in the table lacks an evidence file.
    assert sorted(table_rows) == sorted(expected)


@pytest.mark.req("R13")
def test_stack_is_fastapi_and_sqlite(app: FastAPI, client: TestClient) -> None:
    pins = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "fastapi==" in pins and "sqlalchemy==" in pins
    assert isinstance(app, FastAPI)
    assert app.state.engines.write.dialect.name == "sqlite"
    assert app.state.engines.read.dialect.name == "sqlite"


def _curl_payloads() -> list[Any]:
    bodies = re.findall(r"-d '(\{.*?\})'", readme(), flags=re.S)
    return [json.loads(body) for body in bodies]


@pytest.mark.req("R23")
def test_readme_curl_example_is_valid(client: TestClient) -> None:
    payloads = _curl_payloads()
    assert payloads, "README has no curl submission example"
    for payload in payloads:
        resp = client.post("/v1/certificate-jobs", json=payload)
        assert resp.status_code == 202, resp.text

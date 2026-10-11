"""problem+json contract (§6.1): one shape for every error, no internals leaked."""

from __future__ import annotations

import re
import sqlite3

import pytest
from conftest import JOBS, job_payload, post_job, recipient
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from certgen import service


def assert_problem(resp: object, status: int, code: str) -> dict[str, object]:
    assert resp.status_code == status  # type: ignore[attr-defined]
    assert resp.headers["content-type"] == "application/problem+json"  # type: ignore[attr-defined]
    body: dict[str, object] = resp.json()  # type: ignore[attr-defined]
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    assert isinstance(body["title"], str) and body["title"]
    assert isinstance(body["detail"], str) and body["detail"]
    return body


@pytest.mark.req("R2")
def test_problem_json_shape_for_422(client: TestClient) -> None:
    resp = post_job(client, job_payload(recipients=[{"email": "a@example.com"}]))
    body = assert_problem(resp, 422, "VALIDATION_FAILED")
    assert set(body) == {"type", "title", "status", "code", "detail", "errors"}
    errors = body["errors"]
    assert isinstance(errors, list)
    assert errors == [{"row_index": 0, "field": "name", "code": "REQUIRED", "message": "name is required."}]


@pytest.mark.req("R10")
def test_problem_json_for_404_and_405(client: TestClient) -> None:
    body = assert_problem(client.get("/v1/nope"), 404, "NOT_FOUND")
    assert "errors" not in body
    resp = client.delete(JOBS)
    assert_problem(resp, 405, "METHOD_NOT_ALLOWED")
    assert resp.headers["allow"] == "POST"


@pytest.mark.req("R10")
def test_internal_error_is_problem_json_without_traceback(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object) -> None:
        raise RuntimeError("secret detail at /home/user/data/certgen.sqlite3")

    monkeypatch.setattr(service, "get_job", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get(f"{JOBS}/{'0' * 32}")
    body = assert_problem(resp, 500, "INTERNAL_ERROR")
    assert body["detail"] == "Internal server error"
    assert "secret" not in resp.text
    assert "Traceback" not in resp.text
    assert "RuntimeError" not in resp.text
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.req("R11")
@pytest.mark.parametrize("message", ["database is locked", "database is busy"])
def test_db_locked_maps_to_503(client: TestClient, monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    def locked(*_args: object) -> None:
        raise OperationalError("BEGIN IMMEDIATE", {}, sqlite3.OperationalError(message))

    monkeypatch.setattr(service, "create_job", locked)
    resp = post_job(client, job_payload(recipients=[recipient(0)]))
    assert_problem(resp, 503, "DB_UNAVAILABLE")
    assert resp.headers["retry-after"] == "1"


@pytest.mark.req("R11")
def test_other_operational_errors_are_500(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object) -> None:
        raise OperationalError("SELECT", {}, sqlite3.OperationalError("no such table: jobs"))

    monkeypatch.setattr(service, "get_job", broken)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get(f"{JOBS}/{'0' * 32}")
    assert_problem(resp, 500, "INTERNAL_ERROR")
    assert "no such table" not in resp.text


@pytest.mark.req("R2")
def test_error_codes_listed_in_readme() -> None:
    from conftest import ROOT

    from certgen.errors import PROBLEMS, FieldCode, ProblemCode

    text = (ROOT / "README.md").read_text(encoding="utf-8")
    start = text.index("## Error codes\n")
    end = text.find("\n## ", start + 1)
    codes_section = text[start:end]
    for code in [*ProblemCode, *FieldCode]:
        assert f"`{code.value}`" in codes_section, f"{code.value} is not documented in README ## Error codes"
    for code, (status, _title) in PROBLEMS.items():
        assert re.search(rf"`{code.value}`\s*\|\s*{status}\b", codes_section), f"{code.value} status {status}"

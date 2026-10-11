"""Request limits: global queue capacity (§24.1), body size (§12), media type (§24.5)."""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from conftest import JOBS, job_payload, post_job
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Connection

from certgen import service


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"max_recipients": 3, "max_active_items": 4}


def _job_count(app: FastAPI) -> int:
    with app.state.engines.read.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one())


@pytest.mark.req("R11", "R15")
def test_global_capacity_503(client: TestClient, app: FastAPI, drain: Any) -> None:
    assert post_job(client, job_payload(n=3)).status_code == 202
    resp = post_job(client, job_payload(n=2))  # 3 active + 2 new > 4
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "1"
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["code"] == "CAPACITY_EXCEEDED"
    assert _job_count(app) == 1  # nothing stored
    assert post_job(client, job_payload(n=1)).status_code == 202  # exactly at the cap
    drain()  # finished items no longer count as active
    assert post_job(client, job_payload(n=3)).status_code == 202


@pytest.mark.req("R11")
def test_capacity_check_is_atomic_under_concurrent_posts(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If the count ran outside the write lock, both requests would reach it together, pass the
    # barrier, see 0 active items and both insert. Under BEGIN IMMEDIATE the second request cannot
    # count until the first commits, so the first one's barrier wait times out instead.
    barrier = threading.Barrier(2, timeout=1.0)
    real_count = service.count_active_items

    def count_then_rendezvous(conn: Connection) -> int:
        active = real_count(conn)
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        return active

    monkeypatch.setattr(service, "count_active_items", count_then_rendezvous)
    start = threading.Barrier(2)
    statuses: list[int] = []

    def submit() -> None:
        start.wait()
        statuses.append(post_job(client, job_payload(n=3)).status_code)

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(statuses) == [202, 503]
    assert _job_count(app) == 1


def worst_case_row(i: int) -> dict[str, str]:
    # Every name character needs a 6-byte \uXXXX escape and the email is the longest valid one.
    name = ("Екатерина Александровна Иванова-Петрова " * 3)[:119] + "\u0430"  # Cyrillic a
    email = f"{i}" + "a" * 63 + "@" + ".".join(["b" * 63, "c" * 63, "d" * 57]) + ".org"
    return {"name": name, "email": email}


@pytest.mark.req("R11", "R15")
def test_too_many_recipients_422(client: TestClient, app: FastAPI) -> None:
    # Rows are not validated once the count is over the limit: the bad emails produce no row errors.
    rows = [{"name": "A", "email": "bad"} for _ in range(4)]
    resp = post_job(client, job_payload(recipients=rows))
    assert resp.status_code == 422
    assert [(e["row_index"], e["field"], e["code"]) for e in resp.json()["errors"]] == [
        (None, "recipients", "TOO_MANY_RECIPIENTS")
    ]
    assert _job_count(app) == 0


@pytest.mark.req("R11")
def test_body_over_limit_413(client: TestClient, settings: Any) -> None:
    body = b'{"x": "' + b"a" * settings.max_body_bytes + b'"}'
    resp = client.post(JOBS, content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 413
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.req("R11")
def test_chunked_body_over_limit_413(client: TestClient, settings: Any) -> None:
    def chunks() -> Iterator[bytes]:
        # No Content-Length: the limit must be enforced while reading, not only from the header.
        yield b'{"x": "'
        for _ in range(settings.max_body_bytes // 4096 + 2):
            yield b"a" * 4096
        yield b'"}'

    resp = client.post(JOBS, content=chunks(), headers={"Content-Type": "application/json"})
    assert resp.status_code == 413
    assert resp.json()["code"] == "PAYLOAD_TOO_LARGE"


@pytest.mark.req("R11")
def test_worst_case_body_at_limit_accepted(client: TestClient, settings: Any) -> None:
    payload = job_payload(
        recipients=[worst_case_row(i) for i in range(settings.max_recipients)],
        certificate={
            "title": "Ελληνικά " * 21 + "Τέλος",
            "issuer": "Издатель " * 22,
            "issued_on": "2026-10-01",
        },
    )
    raw = json.dumps(payload).encode()  # ensure_ascii=True: the escaped (largest) encoding
    assert len(raw) <= settings.max_body_bytes
    resp = client.post(JOBS, content=raw, headers={"Content-Type": "application/json"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["errors"] == []


@pytest.mark.req("R11", "R15")
@pytest.mark.parametrize(
    ("content_type", "status"),
    [
        ("application/json", 202),
        ("application/json; charset=utf-8", 202),
        ("APPLICATION/JSON; Charset=UTF-8", 202),
        ("text/plain", 415),
        ("application/x-www-form-urlencoded", 415),
        ("application/json; charset=latin-1", 415),
        ("application/jsonx", 415),
        (None, 415),
    ],
)
def test_wrong_media_type_415(client: TestClient, content_type: str | None, status: int) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    resp = client.post(JOBS, content=json.dumps(job_payload(n=1)).encode(), headers=headers)
    assert resp.status_code == status
    if status == 415:
        assert resp.json()["code"] == "UNSUPPORTED_MEDIA_TYPE"

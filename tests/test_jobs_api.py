"""POST/GET job endpoints: creation, atomicity, IDs, status and progress (§6.3, §6.4, §24.4, §24.10)."""

from __future__ import annotations

import json
import re
from itertools import count
from typing import Any

import pytest
from conftest import JOBS, FakeClock, job_payload, post_job, recipient
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from certgen import service
from certgen.rendering import CertificateData, render_certificate

HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _rows(app: FastAPI, sql: str, **params: Any) -> list[Any]:
    with app.state.engines.read.connect() as conn:
        return list(conn.execute(text(sql), params).mappings())


@pytest.mark.req("R1", "R14")
def test_create_job_returns_202_with_location_and_counts(client: TestClient) -> None:
    resp = post_job(client, job_payload(n=3))
    assert resp.status_code == 202
    body = resp.json()
    assert HEX32.match(body["id"])
    assert resp.headers["location"] == f"{JOBS}/{body['id']}"
    assert body["status"] == "queued"
    assert body["certificate"] == {
        "title": "Python for Data Engineering",
        "issuer": "Acme Academy",
        "issued_on": "2026-10-01",
    }
    assert body["print_email"] is True
    assert body["counts"] == {
        "total": 3,
        "rejected": 0,
        "pending": 3,
        "processing": 0,
        "succeeded": 0,
        "failed": 0,
    }
    assert body["progress"] == 0.0
    assert body["created_at"] == "2026-10-11T08:00:00.000Z"
    assert body["finished_at"] is None
    assert body["links"] == {
        "self": f"{JOBS}/{body['id']}",
        "items": f"{JOBS}/{body['id']}/items",
        "archive": f"{JOBS}/{body['id']}/archive",
    }
    assert body["errors"] == []
    assert client.get(f"{JOBS}/{body['id']}").json()["counts"] == body["counts"]


@pytest.mark.req("R1", "R14")
def test_job_and_items_are_atomic(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = count()

    def explode_on_third() -> str:
        # Certificate IDs are drawn after the job row is inserted, so this fails mid-transaction.
        n = next(ids)
        if n == 2:
            raise RuntimeError("simulated DB failure mid-insert")
        return f"{n:032x}"

    monkeypatch.setattr(service, "new_certificate_id", explode_on_third)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = post_job(c, job_payload(n=3))
        assert resp.status_code == 500
        assert resp.json()["code"] == "INTERNAL_ERROR"
        assert _rows(app, "SELECT id FROM jobs") == []
        assert _rows(app, "SELECT id FROM items") == []


@pytest.mark.req("R1", "R14")
def test_id_collision_is_retried(client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    first = post_job(client, job_payload(n=1)).json()
    taken_job = first["id"]
    taken_cert = _rows(app, "SELECT certificate_id FROM items")[0]["certificate_id"]

    job_ids = iter([taken_job, taken_job, "a" * 32])
    cert_ids = iter([taken_cert, "b" * 32, "c" * 32, "d" * 32])
    monkeypatch.setattr(service, "new_job_id", lambda: next(job_ids))
    monkeypatch.setattr(service, "new_certificate_id", lambda: next(cert_ids))

    resp = post_job(client, job_payload(n=2))
    assert resp.status_code == 202
    assert resp.json()["id"] == "a" * 32
    certs = [
        r["certificate_id"]
        for r in _rows(app, "SELECT certificate_id FROM items WHERE job_id = :j", j="a" * 32)
    ]
    assert len(certs) == 2
    assert taken_cert not in certs
    assert len(_rows(app, "SELECT id FROM jobs")) == 2


@pytest.mark.req("R1", "R14")
def test_id_collision_exhaustion_rolls_back(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app, raise_server_exceptions=False) as c:
        taken_job = post_job(c, job_payload(n=1)).json()["id"]
        monkeypatch.setattr(service, "new_job_id", lambda: taken_job)
        resp = post_job(c, job_payload(n=1))
        assert resp.status_code == 500
        assert resp.json()["code"] == "INTERNAL_ERROR"
        assert len(_rows(app, "SELECT id FROM jobs")) == 1
        assert len(_rows(app, "SELECT id FROM items")) == 1


@pytest.mark.req("R1", "R6")
def test_original_request_is_stored_canonically(client: TestClient, app: FastAPI) -> None:
    raw = (
        '{ "recipients": [ {"email": " Zoe@Example.com ", "name": "  Zo\\u00eb   \\u0141ukasz "} ],'
        '  "certificate": {"title": "Python  for Data Engineering", "issuer": "Acme Academy",'
        ' "issued_on": "2026-10-01"} }'
    )
    resp = client.post(JOBS, content=raw.encode(), headers={"Content-Type": "application/json"})
    assert resp.status_code == 202
    job = _rows(app, "SELECT request_json, title FROM jobs")[0]
    item = _rows(app, "SELECT input_json, name, email FROM items")[0]
    # Canonical form of the parsed body *before* normalization: sorted keys, compact, UTF-8 kept.
    assert job["request_json"] == json.dumps(
        json.loads(raw), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert '"name":"  Zoë   Łukasz "' in job["request_json"]
    assert item["input_json"] == '{"email":" Zoe@Example.com ","name":"  Zoë   Łukasz "}'
    # The working columns hold normalized values; the audit columns keep the original.
    assert job["title"] == "Python for Data Engineering"
    assert (item["name"], item["email"]) == ("Zoë Łukasz", "Zoe@Example.com")
    assert "request_json" not in resp.text and "input_json" not in resp.text


@pytest.mark.req("R11")
def test_post_does_not_render_synchronously(settings: Any, fake_clock: FakeClock) -> None:
    from certgen.main import create_app

    calls: list[str] = []

    def counting(data: CertificateData) -> bytes:
        calls.append(data.certificate_id)
        return render_certificate(data)

    app = create_app(settings, clock=fake_clock, renderer=counting)
    with TestClient(app) as c:
        resp = post_job(c, job_payload(recipients=[recipient(i) for i in range(1000)]))
        assert resp.status_code == 202
        assert resp.json()["counts"]["pending"] == 1000
    assert calls == []


@pytest.mark.req("R7")
def test_get_job_unknown_404(client: TestClient) -> None:
    resp = client.get(f"{JOBS}/{'0' * 32}")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["code"] == "NOT_FOUND"


@pytest.mark.req("R7")
@pytest.mark.parametrize("bad_id", ["abc", "G" * 32, "A" * 32, "0" * 33, "..%2F..%2Fetc"])
def test_get_job_malformed_id_404(client: TestClient, bad_id: str) -> None:
    resp = client.get(f"{JOBS}/{bad_id}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


@pytest.mark.req("R7", "R17")
def test_status_queued_then_completed(client: TestClient, drain: Any, fake_clock: FakeClock) -> None:
    job_id = post_job(client, job_payload(n=3)).json()["id"]
    assert client.get(f"{JOBS}/{job_id}").json()["status"] == "queued"
    fake_clock.advance(1_500)
    assert len(drain()) == 3
    body = client.get(f"{JOBS}/{job_id}").json()
    assert body["status"] == "completed"
    assert body["progress"] == 1.0
    assert body["counts"]["succeeded"] == 3
    assert body["finished_at"] == "2026-10-11T08:00:01.500Z"
    assert body["created_at"] == "2026-10-11T08:00:00.000Z"


@pytest.mark.req("R7", "R17")
def test_progress_after_partial_drain(client: TestClient, make_worker: Any) -> None:
    job_id = post_job(client, job_payload(n=3)).json()["id"]
    assert make_worker().run_once() is not None
    body = client.get(f"{JOBS}/{job_id}").json()
    assert body["progress"] == 0.3333
    assert body["status"] == "processing"
    assert body["counts"]["succeeded"] == 1
    assert body["counts"]["pending"] == 2


@pytest.mark.req("R7")
def test_no_store_header_on_json_and_pdf(client: TestClient, drain: Any, app: FastAPI) -> None:
    created = post_job(client, job_payload(n=1))
    job_id = created.json()["id"]
    drain()
    cert = _rows(app, "SELECT certificate_id FROM items")[0]["certificate_id"]
    responses = [
        created,
        client.get(f"{JOBS}/{job_id}"),
        client.get(f"/v1/certificates/{cert}"),
        client.get(f"{JOBS}/{'0' * 32}"),  # problem+json
    ]
    for resp in responses:
        assert resp.headers["cache-control"] == "no-store", resp.request.url

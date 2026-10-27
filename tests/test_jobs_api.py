"""POST/GET job endpoints: creation, atomicity, IDs, status and progress (§6.3, §6.4, §24.4, §24.10)."""

from __future__ import annotations

import json
import re
from itertools import count
from typing import Any

import pytest
from conftest import AUTH_HEADERS, JOBS, FakeClock, job_payload, post_job, recipient, wait_until
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from certgen import service
from certgen.main import create_app
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
    with TestClient(app, headers=AUTH_HEADERS, raise_server_exceptions=False) as c:
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
    with TestClient(app, headers=AUTH_HEADERS, raise_server_exceptions=False) as c:
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
    calls: list[str] = []

    def counting(data: CertificateData) -> bytes:
        calls.append(data.certificate_id)
        return render_certificate(data)

    app = create_app(settings, clock=fake_clock, renderer=counting)
    with TestClient(app, headers=AUTH_HEADERS) as c:
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


@pytest.mark.req("R1", "R3")
def test_created_items_have_ids_only_when_valid(client: TestClient, app: FastAPI) -> None:
    rows = [recipient(0), recipient(1, email="nope"), recipient(2), recipient(3, name="")]
    assert post_job(client, job_payload(recipients=rows)).status_code == 202
    stored = _rows(app, "SELECT row_index, status, certificate_id, attempts FROM items ORDER BY row_index")
    assert [r["status"] for r in stored] == ["pending", "rejected", "pending", "rejected"]
    valid_ids = [r["certificate_id"] for r in stored if r["status"] == "pending"]
    assert all(HEX32.match(cid) for cid in valid_ids)
    assert len(set(valid_ids)) == 2
    assert [r["certificate_id"] for r in stored if r["status"] == "rejected"] == [None, None]
    assert all(r["attempts"] == 0 for r in stored)


@pytest.mark.req("R7")
def test_healthz_ok_without_embedded_workers(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "db": "ok",
        "embedded_workers_alive": 0,
        "embedded_workers_expected": 0,
    }


@pytest.mark.req("R7")
# The crashes are deliberate; pytest would otherwise report each dead thread as a warning.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_healthz_degraded(settings: Any, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from certgen.worker import Worker

    def crash(self: Worker, stop: Any) -> None:
        raise SystemExit("worker thread crashed")  # BaseException: escapes the loop's except Exception

    monkeypatch.setattr(Worker, "run_forever", crash)
    app = create_app(replace(settings, embedded_worker=True, worker_threads=2), clock=fake_clock)
    with TestClient(app, headers=AUTH_HEADERS) as c:

        def degraded() -> bool:
            body = c.get("/healthz").json()
            return body["status"] == "degraded" and body["embedded_workers_alive"] < 2

        assert wait_until(degraded, timeout=5)
        resp = c.get("/healthz")
        assert resp.status_code == 200  # degraded is still serving
        assert resp.json()["embedded_workers_expected"] == 2


@pytest.mark.req("R7")
def test_healthz_db_failure_503(client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    from sqlalchemy.exc import OperationalError

    def broken(*_args: Any) -> None:
        raise OperationalError("SELECT 1", {}, sqlite3.OperationalError("unable to open database file"))

    monkeypatch.setattr(service, "ping", broken)
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["code"] == "DB_UNAVAILABLE"
    assert resp.headers["retry-after"] == "1"


def _items_page(client: TestClient, job_id: str, **params: Any) -> Any:
    return client.get(f"{JOBS}/{job_id}/items", params=params)


@pytest.mark.req("R10", "R11")
def test_items_keyset_pagination(client: TestClient) -> None:
    job_id = post_job(client, job_payload(n=5)).json()["id"]
    seen: list[int] = []
    cursors: list[int | None] = []
    after = -1
    while True:
        page = _items_page(client, job_id, limit=2, after=after).json()
        assert page["job_id"] == job_id and page["limit"] == 2
        seen += [item["row_index"] for item in page["items"]]
        cursors.append(page["next_after"])
        if page["next_after"] is None:
            break
        after = page["next_after"]
    assert cursors == [1, 3, None]
    assert seen == [0, 1, 2, 3, 4]
    default = _items_page(client, job_id).json()
    assert default["limit"] == 100 and default["next_after"] is None and len(default["items"]) == 5


@pytest.mark.req("R10")
def test_items_status_filter(client: TestClient, drain: Any, make_worker: Any) -> None:
    rows = [recipient(0), recipient(1, email="bad"), recipient(2), recipient(3, name="")]
    job_id = post_job(client, job_payload(recipients=rows)).json()["id"]
    make_worker().run_once()  # row 0 succeeds, row 2 stays pending
    rejected = _items_page(client, job_id, status="rejected").json()
    assert [i["row_index"] for i in rejected["items"]] == [1, 3]
    assert all(i["certificate_id"] is None and i["download_url"] is None for i in rejected["items"])
    assert rejected["items"][0]["errors"] == [
        {"field": "email", "code": "INVALID_EMAIL", "message": rejected["items"][0]["errors"][0]["message"]}
    ]
    succeeded = _items_page(client, job_id, status="succeeded").json()["items"]
    assert [i["row_index"] for i in succeeded] == [0]
    assert succeeded[0]["download_url"] == f"/v1/certificates/{succeeded[0]['certificate_id']}"
    assert succeeded[0]["errors"] == [] and succeeded[0]["attempts"] == 1
    pending = _items_page(client, job_id, status="pending", limit=1).json()
    assert [i["row_index"] for i in pending["items"]] == [2] and pending["next_after"] is None
    assert pending["items"][0]["download_url"] is None
    assert _items_page(client, job_id, status="failed").json()["items"] == []
    assert _items_page(client, job_id, status="pending", after=2).json()["items"] == []


@pytest.mark.req("R11")
@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 1001}, "limit"),
        ({"limit": "x"}, "limit"),
        ({"after": -2}, "after"),
        ({"after": "1.5"}, "after"),
        ({"status": "done"}, "status"),
    ],
)
def test_items_invalid_params_422(client: TestClient, params: dict[str, Any], field: str) -> None:
    job_id = post_job(client, job_payload(n=1)).json()["id"]
    resp = _items_page(client, job_id, **params)
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    assert [(e["row_index"], e["field"], e["code"]) for e in resp.json()["errors"]] == [
        (None, field, "INVALID_VALUE")
    ]
    assert _items_page(client, job_id, unknown="ignored").status_code == 200  # unknown params are ignored


@pytest.mark.req("R10")
@pytest.mark.parametrize("job_id", ["0" * 32, "not-a-job"])
def test_items_unknown_job_404(client: TestClient, job_id: str) -> None:
    resp = _items_page(client, job_id)
    assert resp.status_code == 404 and resp.json()["code"] == "NOT_FOUND"

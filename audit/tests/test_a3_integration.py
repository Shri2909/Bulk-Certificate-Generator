"""Integration tests across real boundaries: HTTP (TestClient) -> api -> service -> SQLite -> worker ->
storage -> archive. Only the clock and, where stated, the renderer are injected. Expected to PASS unless
marked red.
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import threading
import zipfile
from pathlib import Path
from typing import Any

import pytest
from conftest import JOBS, FakeClock, payload, post_raw
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import text

from certgen.config import Settings
from certgen.db import create_engines, create_schema
from certgen.rendering import CertificateData, render_certificate
from certgen.worker import Worker


def items(client: TestClient, job_id: str, **params: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    after = -1
    while True:
        page = client.get(f"{JOBS}/{job_id}/items", params={"after": after, "limit": 1000, **params}).json()
        out += page["items"]
        if page["next_after"] is None:
            return out
        after = page["next_after"]


def failing_for(names: set[str]) -> Any:
    def render(data: CertificateData) -> bytes:
        if data.name in names:
            raise RuntimeError("boom " + data.name)  # text must never reach the API
        return render_certificate(data)

    return render


def test_full_lifecycle_with_failures_retry_and_archive(
    client: TestClient, app: FastAPI, drain: Any, fake_clock: FakeClock, settings: Settings
) -> None:
    rows = [{"name": f"Person {i}", "email": f"p{i}@example.com"} for i in range(30)]
    rows[3]["email"] = "bad"
    rows[7] = {"name": "Dup", "email": " P0@EXAMPLE.com "}
    resp = client.post(JOBS, json=payload(rows))
    assert resp.status_code == 202 and resp.headers["location"] == f"{JOBS}/{resp.json()['id']}"
    job_id = resp.json()["id"]
    assert [(e["row_index"], e["code"]) for e in resp.json()["errors"]] == [
        (3, "INVALID_EMAIL"),
        (7, "DUPLICATE_RECIPIENT"),
    ]
    broken = {"Person 11", "Person 12"}
    for _ in range(settings.max_attempts):
        drain(failing_for(broken))
        fake_clock.advance(settings.retry_delay_ms + 1)
    job = client.get(f"{JOBS}/{job_id}").json()
    assert job["status"] == "completed_with_errors"
    assert job["counts"] == {
        "total": 30,
        "rejected": 2,
        "pending": 0,
        "processing": 0,
        "succeeded": 26,
        "failed": 2,
    }
    assert job["progress"] == 1.0 and job["finished_at"] is not None
    failed = items(client, job_id, status="failed")
    assert [i["row_index"] for i in failed] == [11, 12]
    assert all(i["attempts"] == settings.max_attempts for i in failed)
    assert all(i["errors"][0]["message"] == "RuntimeError: Certificate generation failed." for i in failed)
    assert all("boom" not in str(i) for i in failed)
    for i in failed:
        assert (
            client.get(f"/v1/certificates/{i['certificate_id']}").json()["code"]
            == "CERTIFICATE_GENERATION_FAILED"
        )
    # archive snapshot is consistent with the item list
    z = zipfile.ZipFile(io.BytesIO(client.get(f"{JOBS}/{job_id}/archive").content))
    manifest = list(csv.DictReader(io.StringIO(z.read("manifest.csv").decode())))
    assert [int(m["row_index"]) for m in manifest] == list(range(30))
    assert {m["status"] for m in manifest} == {"succeeded", "failed", "rejected"}
    assert len([n for n in z.namelist() if n.endswith(".pdf")]) == 26
    # fix and retry: certificate ids are stable and the PDFs equal a direct render of the stored row
    before = {i["row_index"]: i["certificate_id"] for i in items(client, job_id)}
    assert client.post(f"{JOBS}/{job_id}/retry").status_code == 202
    assert client.post(f"{JOBS}/{job_id}/retry").json()["code"] == "NOTHING_TO_RETRY"
    drain()
    after = items(client, job_id)
    assert {i["row_index"]: i["certificate_id"] for i in after} == before
    assert client.get(f"{JOBS}/{job_id}").json()["counts"]["succeeded"] == 28
    item = after[11]
    pdf = client.get(item["download_url"]).content
    direct = render_certificate(
        CertificateData(
            job_id,
            item["certificate_id"],
            item["name"],
            item["email"],
            "Python for Data Engineering",
            "Acme Academy",
            "2026-10-01",
        )
    )
    assert pdf == direct


@pytest.mark.parametrize("limit", [1, 2, 3, 7, 1000])
def test_keyset_walk_returns_every_row_once_in_order(client: TestClient, limit: int) -> None:
    rows = [{"name": f"N {i}", "email": f"n{i}@example.com" if i % 4 else "x"} for i in range(23)]
    job_id = client.post(JOBS, json=payload(rows)).json()["id"]
    seen: list[int] = []
    after = -1
    while True:
        page = client.get(f"{JOBS}/{job_id}/items", params={"after": after, "limit": limit}).json()
        seen += [i["row_index"] for i in page["items"]]
        assert len(page["items"]) <= limit
        if page["next_after"] is None:
            break
        after = page["next_after"]
    assert seen == list(range(23))
    rejected = [i["row_index"] for i in items(client, job_id, status="rejected")]
    assert rejected == [i for i in range(23) if i % 4 == 0]


def test_ten_thousand_rows_single_request(client: TestClient, app: FastAPI) -> None:
    rows = [{"name": f"Recipient {i}", "email": f"r{i}@example.com"} for i in range(10_000)]
    resp = client.post(JOBS, json=payload(rows))
    assert resp.status_code == 202
    assert resp.json()["counts"]["pending"] == 10_000
    assert len(items(client, resp.json()["id"])) == 10_000
    with app.state.engines.read.connect() as conn:
        ids = conn.execute(text("SELECT COUNT(DISTINCT certificate_id) FROM items")).scalar_one()
    assert ids == 10_000


def test_concurrent_retry_calls_requeue_once(
    client: TestClient, app: FastAPI, drain: Any, fake_clock: FakeClock, settings: Settings
) -> None:
    job_id = client.post(JOBS, json=payload(n=4)).json()["id"]
    for _ in range(settings.max_attempts):
        drain(failing_for({"Recipient 0", "Recipient 1", "Recipient 2", "Recipient 3"}))
        fake_clock.advance(settings.retry_delay_ms + 1)
    barrier = threading.Barrier(4)
    codes: list[int] = []

    def go() -> None:
        barrier.wait()
        codes.append(client.post(f"{JOBS}/{job_id}/retry").status_code)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(codes) == [202, 409, 409, 409]


@pytest.mark.parametrize(
    "body, ctype, status",
    [
        (b"", None, 202),
        (b"{}", "application/json", 202),
        (b"[]", "application/json", 422),
        (b"{bad", "application/json", 422),
        (b'{"x":1}', "application/json", 422),
        (b"{}", "text/plain", 415),
    ],
)
def test_retry_body_rules(
    client: TestClient,
    drain: Any,
    fake_clock: FakeClock,
    settings: Settings,
    body: bytes,
    ctype: str | None,
    status: int,
) -> None:
    job_id = client.post(JOBS, json=payload(n=1)).json()["id"]
    for _ in range(settings.max_attempts):
        drain(failing_for({"Recipient 0"}))
        fake_clock.advance(settings.retry_delay_ms + 1)
    headers = {"Content-Type": ctype} if ctype else {}
    assert client.post(f"{JOBS}/{job_id}/retry", content=body, headers=headers).status_code == status


def test_manifest_neutralizes_formula_emails(client: TestClient, drain: Any) -> None:
    rows = [
        {"name": "A", "email": "=cmd@example.com"},
        {"name": "B", "email": "+1@example.com"},
        {"name": "C", "email": "-x@example.com"},
    ]
    job_id = client.post(JOBS, json=payload(rows)).json()["id"]
    drain()
    z = zipfile.ZipFile(io.BytesIO(client.get(f"{JOBS}/{job_id}/archive").content))
    emails = [m["email"] for m in csv.DictReader(io.StringIO(z.read("manifest.csv").decode()))]
    assert emails == ["'=cmd@example.com", "'+1@example.com", "'-x@example.com"]


def test_pdf_string_escaping_round_trips(client: TestClient, drain: Any) -> None:
    name = "Robert') (Tables\\ ) Tj ET <<"
    job_id = client.post(JOBS, json=payload([{"name": name, "email": "r@example.com"}])).json()["id"]
    drain()
    url = items(client, job_id)[0]["download_url"]
    assert name in PdfReader(io.BytesIO(client.get(url).content)).pages[0].extract_text()


def test_stale_file_of_failed_item_is_never_served(
    app: FastAPI, client: TestClient, settings: Settings, fake_clock: FakeClock
) -> None:
    """Worker A claims (attempt 1 of 1), stalls past its lease, the retire step fails the item WORKER_LOST,
    then A wakes and writes the PDF. The file exists, the item is failed: downloads must refuse it and the
    archive must not include it (BLUEPRINT §11.2)."""
    s1 = Settings(data_dir=settings.data_dir, embedded_worker=False, max_attempts=1)
    job_id = client.post(JOBS, json=payload(n=1)).json()["id"]
    release, entered = threading.Event(), threading.Event()

    def stalled(data: CertificateData) -> bytes:
        entered.set()
        assert release.wait(20)
        return render_certificate(data)

    a = Worker(app.state.engines, app.state.storage, s1, fake_clock, stalled)
    t = threading.Thread(target=a.run_once)
    t.start()
    assert entered.wait(10)
    fake_clock.advance(s1.lease_ms + 1)
    Worker(
        app.state.engines, app.state.storage, s1, fake_clock, render_certificate
    ).run_once()  # retire step runs in the claim
    release.set()
    t.join(20)
    item = items(client, job_id)[0]
    assert item["status"] == "failed" and item["errors"][0]["code"] == "WORKER_LOST"
    assert app.state.storage.path_for(job_id, item["certificate_id"]).exists()  # the stale write landed
    assert client.get(f"/v1/certificates/{item['certificate_id']}").status_code == 409
    z = zipfile.ZipFile(io.BytesIO(client.get(f"{JOBS}/{job_id}/archive").content))
    assert [n for n in z.namelist() if n.endswith(".pdf")] == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
def test_symlinked_job_dir_is_refused_on_write(
    client: TestClient, app: FastAPI, drain: Any, tmp_path: Path, settings: Settings, fake_clock: FakeClock
) -> None:
    job_id = client.post(JOBS, json=payload(n=1)).json()["id"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (settings.certificates_dir / job_id).symlink_to(outside, target_is_directory=True)
    for _ in range(settings.max_attempts):
        drain()
        fake_clock.advance(settings.retry_delay_ms + 1)
    item = items(client, job_id)[0]
    assert item["status"] == "failed" and item["errors"][0]["code"] == "GENERATION_FAILED"
    assert list(outside.iterdir()) == []


def test_truncated_file_on_disk_is_served_as_is(client: TestClient, app: FastAPI, drain: Any) -> None:
    """Observation (fault injection): there is no integrity check on read; a PDF damaged on disk is
    served with 200. Recorded as INFO, not a contract."""
    job_id = client.post(JOBS, json=payload(n=1)).json()["id"]
    drain()
    item = items(client, job_id)[0]
    path = app.state.storage.path_for(job_id, item["certificate_id"])
    path.write_bytes(path.read_bytes()[:100])
    resp = client.get(item["download_url"])
    assert resp.status_code == 200 and len(resp.content) == 100


def test_startup_on_corrupt_database_fails_loudly(tmp_path: Path) -> None:
    db = tmp_path / "certgen.sqlite3"
    db.write_bytes(b"this is not a database" * 100)
    engines = create_engines(db)
    with pytest.raises(Exception) as exc:
        create_schema(engines)
    assert "not a database" in str(exc.value)


def test_readonly_database_maps_to_problem_json(client: TestClient, app: FastAPI, settings: Settings) -> None:
    """Fault injection: the DB file becomes read-only while the API runs.
    Observed: problem+json 500, reads still work."""
    job_id = client.post(JOBS, json=payload(n=1)).json()["id"]
    app.state.engines.dispose()
    os.chmod(settings.db_path, 0o400)
    try:
        resp = client.post(JOBS, json=payload(n=1))
        assert resp.headers["content-type"] == "application/problem+json"
        assert resp.status_code in (500, 503)
        assert client.get(f"{JOBS}/{job_id}").status_code == 200
    finally:
        os.chmod(settings.db_path, 0o600)


@pytest.mark.parametrize("method", ["HEAD", "PUT", "DELETE", "PATCH"])
def test_unsupported_methods_are_problem_json(client: TestClient, method: str) -> None:
    resp = client.request(method, JOBS)
    assert resp.status_code == 405
    if method != "HEAD":
        assert resp.json()["code"] == "METHOD_NOT_ALLOWED"


def test_every_response_is_no_store(client: TestClient) -> None:
    for resp in (
        client.get("/healthz"),
        client.get("/nope"),
        post_raw(client, b"{"),
        client.get(f"{JOBS}/{'a' * 32}"),
    ):
        assert resp.headers["cache-control"] == "no-store"


def test_db_invariants_after_mixed_workload(
    client: TestClient, app: FastAPI, drain: Any, settings: Settings, fake_clock: FakeClock
) -> None:
    for j in range(5):
        rows = [
            {"name": f"J{j} R{i}", "email": f"j{j}r{i}@example.com" if i != 2 else "bad"} for i in range(9)
        ]
        client.post(JOBS, json=payload(rows))
    for _ in range(settings.max_attempts):
        drain(failing_for({"J1 R4", "J3 R0"}))
        fake_clock.advance(settings.retry_delay_ms + 1)
    conn = sqlite3.connect(settings.db_path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            conn.execute("SELECT COUNT(*) FROM items WHERE status IN ('pending','processing')").fetchone()[0]
            == 0
        )
        succeeded = conn.execute(
            "SELECT job_id, certificate_id FROM items WHERE status='succeeded'"
        ).fetchall()
    finally:
        conn.close()
    files = {p.stem for p in settings.certificates_dir.rglob("*.pdf")}
    assert files == {c for _, c in succeeded}
    assert list(settings.certificates_dir.rglob("*.tmp")) == []

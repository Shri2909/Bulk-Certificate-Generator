"""Certificate downloads and the job archive (§6.6, §6.7, §11.3)."""

from __future__ import annotations

import csv
import io
import os
import threading
import zipfile
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock, failing_for, job_payload, post_job, recipient
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from certgen import archive as archive_module


def _certificate_ids(app: FastAPI) -> list[str]:
    with app.state.engines.read.connect() as conn:
        return list(
            conn.execute(
                text("SELECT certificate_id FROM items WHERE certificate_id IS NOT NULL ORDER BY row_index")
            )
            .scalars()
            .all()
        )


def _job_id(app: FastAPI) -> str:
    with app.state.engines.read.connect() as conn:
        return str(conn.execute(text("SELECT id FROM jobs")).scalar_one())


@pytest.mark.req("R8", "R19")
def test_download_succeeded_pdf(client: TestClient, app: FastAPI, drain: Any) -> None:
    post_job(client, job_payload(n=3))
    drain()
    for cert in _certificate_ids(app):
        resp = client.get(f"/v1/certificates/{cert}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.headers["content-disposition"] == f'attachment; filename="certificate-{cert}.pdf"'
        assert resp.content.startswith(b"%PDF-")
        assert resp.content.rstrip().endswith(b"%%EOF")


@pytest.mark.req("R19")
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symlinks")
@pytest.mark.parametrize("target", ["file", "job_dir"])
def test_symlinked_certificate_refused(
    client: TestClient, app: FastAPI, drain: Any, tmp_path: Path, target: str
) -> None:
    post_job(client, job_payload(n=1))
    drain()
    cert = _certificate_ids(app)[0]
    pdf = app.state.storage.path_for(_job_id(app), cert)
    secret = tmp_path / "outside" / f"{cert}.pdf"
    secret.parent.mkdir()
    secret.write_bytes(b"%PDF-1.4 secret bytes outside the data dir")
    if target == "file":
        pdf.unlink()
        pdf.symlink_to(secret)
    else:
        job_dir = pdf.parent
        for child in job_dir.iterdir():
            child.unlink()
        job_dir.rmdir()
        job_dir.symlink_to(secret.parent, target_is_directory=True)
    resp = client.get(f"/v1/certificates/{cert}")
    assert resp.status_code == 410
    assert resp.json()["code"] == "CERTIFICATE_FILE_MISSING"
    assert b"secret" not in resp.content


def _archive(client: TestClient, job_id: str) -> tuple[Any, zipfile.ZipFile]:
    resp = client.get(f"/v1/certificate-jobs/{job_id}/archive")
    assert resp.status_code == 200, resp.text
    return resp, zipfile.ZipFile(io.BytesIO(resp.content))


def _manifest(archive: zipfile.ZipFile) -> list[dict[str, str]]:
    raw = archive.read("manifest.csv")
    assert not raw.startswith(b"\xef\xbb\xbf")  # UTF-8 without BOM
    assert b"\r\n" in raw  # csv module default line terminator
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8"), newline="")))


@pytest.mark.req("R19")
def test_download_pending_409(client: TestClient, app: FastAPI) -> None:
    post_job(client, job_payload(n=1))
    resp = client.get(f"/v1/certificates/{_certificate_ids(app)[0]}")
    assert resp.status_code == 409
    assert resp.json()["code"] == "CERTIFICATE_NOT_READY"


@pytest.mark.req("R19")
def test_unknown_certificate_404(client: TestClient) -> None:
    resp = client.get(f"/v1/certificates/{'f' * 32}")
    assert resp.status_code == 404 and resp.json()["code"] == "NOT_FOUND"


@pytest.mark.req("R19")
@pytest.mark.parametrize(
    "bad_id",
    ["..%2F..%2Fetc%2Fpasswd", "%2e%2e", "a" * 31, "a" * 33, "A" * 32, "a" * 31 + "%00", "a" * 31 + "%0A"],
)
def test_path_traversal_id_404(client: TestClient, bad_id: str) -> None:
    resp = client.get(f"/v1/certificates/{bad_id}")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    archive = client.get(f"/v1/certificate-jobs/{bad_id}/archive")
    assert archive.status_code == 404


@pytest.mark.req("R19")
def test_missing_file_410(client: TestClient, app: FastAPI, drain: Any) -> None:
    post_job(client, job_payload(n=1))
    drain()
    cert = _certificate_ids(app)[0]
    app.state.storage.path_for(_job_id(app), cert).unlink()
    resp = client.get(f"/v1/certificates/{cert}")
    assert resp.status_code == 410
    assert resp.json()["code"] == "CERTIFICATE_FILE_MISSING"


@pytest.mark.req("R19")
def test_rejected_item_has_no_download(client: TestClient, app: FastAPI, drain: Any) -> None:
    job_id = post_job(client, job_payload(recipients=[recipient(0), recipient(1, email="bad")])).json()["id"]
    drain()
    items = client.get(f"/v1/certificate-jobs/{job_id}/items").json()["items"]
    assert items[1]["status"] == "rejected"
    assert items[1]["certificate_id"] is None and items[1]["download_url"] is None
    with app.state.engines.read.connect() as conn:
        assert conn.execute(text("SELECT certificate_id FROM items WHERE row_index = 1")).scalar_one() is None


@pytest.mark.req("R8", "R19")
def test_archive_contains_all_succeeded_pdfs(client: TestClient, app: FastAPI, drain: Any) -> None:
    rows = [recipient(0), recipient(1, email="bad"), recipient(2)]
    job_id = post_job(client, job_payload(recipients=rows)).json()["id"]
    drain()
    resp, archive = _archive(client, job_id)
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"] == f'attachment; filename="certificates-{job_id}.zip"'
    assert resp.headers["cache-control"] == "no-store"
    with app.state.engines.read.connect() as conn:
        stored = conn.execute(
            text("SELECT row_index, certificate_id FROM items WHERE status = 'succeeded' ORDER BY row_index")
        ).all()
    expected = sorted(f"certificates/{r.row_index:05d}_{r.certificate_id}.pdf" for r in stored)
    assert sorted(n for n in archive.namelist() if n != "manifest.csv") == expected
    for r in stored:
        info = archive.getinfo(f"certificates/{r.row_index:05d}_{r.certificate_id}.pdf")
        assert info.compress_type == zipfile.ZIP_STORED  # PDFs are already compressed
        assert archive.read(info) == client.get(f"/v1/certificates/{r.certificate_id}").content
    assert archive.getinfo("manifest.csv").compress_type == zipfile.ZIP_DEFLATED


@pytest.mark.req("R10", "R19")
def test_manifest_lists_every_row(client: TestClient, drain: Any, fake_clock: FakeClock) -> None:
    rows = [
        recipient(0),
        recipient(1, email="bad"),
        recipient(2, name="Broken"),
        recipient(3, name="", phone="1"),
    ]
    job_id = post_job(client, job_payload(recipients=rows)).json()["id"]
    for _ in range(3):
        drain(failing_for({"Broken"}))
        fake_clock.advance(2_000)
    _, archive = _archive(client, job_id)
    manifest = _manifest(archive)
    assert list(manifest[0]) == [
        "row_index", "name", "email", "status", "certificate_id", "error_codes", "error_messages"
    ]  # fmt: skip
    assert [m["row_index"] for m in manifest] == ["0", "1", "2", "3"]
    assert [m["status"] for m in manifest] == ["succeeded", "rejected", "failed", "rejected"]
    assert manifest[0]["error_codes"] == "" and len(manifest[0]["certificate_id"]) == 32
    assert manifest[1]["error_codes"] == "INVALID_EMAIL" and manifest[1]["certificate_id"] == ""
    assert manifest[2]["error_codes"] == "GENERATION_FAILED"
    assert manifest[2]["error_messages"] == "RuntimeError: Certificate generation failed."
    assert manifest[3]["error_codes"] == "REQUIRED; UNKNOWN_FIELD"
    assert manifest[3]["error_messages"].count("; ") == 1


@pytest.mark.req("R19")
def test_manifest_neutralizes_formula_cells(client: TestClient, drain: Any) -> None:
    names = ["=HYPERLINK(1)", "+SUM(A1)", "-2+3", "@cmd", "\uff1dFULLWIDTH", "Plain Name"]
    rows = [recipient(i, name=name) for i, name in enumerate(names)]
    rows.append(recipient(6, email="-dash@example.com"))
    job_id = post_job(client, job_payload(recipients=rows)).json()["id"]
    drain()
    manifest = _manifest(_archive(client, job_id)[1])
    assert [m["name"] for m in manifest[:6]] == ["'" + n for n in names[:5]] + ["Plain Name"]
    assert manifest[6]["email"] == "'-dash@example.com"
    # The full-width row is rejected (the font lacks U+FF1D), yet its stored name is still guarded.
    assert [m["status"] for m in manifest] == ["succeeded"] * 4 + ["rejected", "succeeded", "succeeded"]


@pytest.mark.req("R19")
def test_archive_marks_missing_file(client: TestClient, app: FastAPI, drain: Any) -> None:
    job_id = post_job(client, job_payload(n=2)).json()["id"]
    drain()
    gone = _certificate_ids(app)[0]
    app.state.storage.path_for(job_id, gone).unlink()
    _, archive = _archive(client, job_id)
    manifest = _manifest(archive)
    assert manifest[0]["status"] == "succeeded"
    assert manifest[0]["error_codes"] == "CERTIFICATE_FILE_MISSING"
    assert not any(gone in name for name in archive.namelist())
    assert len([n for n in archive.namelist() if n.endswith(".pdf")]) == 1


@pytest.mark.req("R19")
def test_archive_while_running_is_snapshot(
    client: TestClient, app: FastAPI, make_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = post_job(client, job_payload(n=3)).json()["id"]
    worker = make_worker()
    worker.run_once()  # row 0 done; rows 1 and 2 pending when the archive reads its rows
    real_build = archive_module.build_archive

    def build_after_more_work(rows: Any, storage: Any) -> Any:
        worker.run_until_idle()  # completions land after the rows were read and the DB released
        return real_build(rows, storage)

    monkeypatch.setattr(archive_module, "build_archive", build_after_more_work)
    _, archive = _archive(client, job_id)
    manifest = _manifest(archive)
    assert [m["status"] for m in manifest] == ["succeeded", "pending", "pending"]
    assert len([n for n in archive.namelist() if n.endswith(".pdf")]) == 1
    monkeypatch.setattr(archive_module, "build_archive", real_build)
    assert [m["status"] for m in _manifest(_archive(client, job_id)[1])] == ["succeeded"] * 3


@pytest.mark.req("R19")
def test_archive_unknown_job_404(client: TestClient) -> None:
    resp = client.get(f"/v1/certificate-jobs/{'0' * 32}/archive")
    assert resp.status_code == 404 and resp.json()["code"] == "NOT_FOUND"


@pytest.mark.req("R19")
def test_archive_concurrency_limit_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = post_job(client, job_payload(n=1)).json()["id"]
    release = threading.Event()
    building = threading.Semaphore(0)
    real_build = archive_module.build_archive

    def slow_build(rows: Any, storage: Any) -> Any:
        building.release()
        assert release.wait(timeout=30)
        return real_build(rows, storage)

    monkeypatch.setattr(archive_module, "build_archive", slow_build)
    results: list[int] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(client.get(f"/v1/certificate-jobs/{job_id}/archive").status_code)
        )
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    assert building.acquire(timeout=10) and building.acquire(timeout=10)  # two builds in progress
    third = client.get(f"/v1/certificate-jobs/{job_id}/archive")
    assert third.status_code == 503
    assert third.json()["code"] == "ARCHIVE_BUSY"
    assert third.headers["retry-after"] == "1"
    release.set()
    for t in threads:
        t.join(timeout=30)
    assert results == [200, 200]
    assert client.get(f"/v1/certificate-jobs/{job_id}/archive").status_code == 200  # slots were released

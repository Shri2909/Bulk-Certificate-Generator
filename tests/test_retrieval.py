"""Certificate downloads and the job archive (§6.6, §6.7, §11.3)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from conftest import job_payload, post_job
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text


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

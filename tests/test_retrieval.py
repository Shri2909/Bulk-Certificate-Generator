"""Certificate downloads and the job archive (§6.6, §6.7, §11.3)."""

from __future__ import annotations

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

"""End to end with the real embedded worker threads and the real clock (§16.1: the only threaded test)."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import JOBS, TESTCLIENT_HOSTS, job_payload, post_job, recipient, wait_until
from fastapi.testclient import TestClient

from certgen.config import Settings
from certgen.main import create_app


@pytest.mark.req("R1", "R7", "R8")
def test_end_to_end_embedded_worker(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        worker_threads=2,
        poll_interval_seconds=0.05,
        allowed_hosts=TESTCLIENT_HOSTS,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").json() == {
            "status": "ok",
            "db": "ok",
            "embedded_workers_alive": 2,
            "embedded_workers_expected": 2,
        }
        rows = [recipient(i) for i in range(5)] + [recipient(5, email="invalid")]
        created = post_job(client, job_payload(recipients=rows))
        assert created.status_code == 202
        job_url = created.headers["location"]

        def finished() -> bool:
            return client.get(job_url).json()["status"] not in ("queued", "processing")

        assert wait_until(finished, timeout=10, step=0.02)
        job = client.get(job_url).json()
        assert job["status"] == "completed_with_errors"  # the rejected row is reported, not lost
        assert job["counts"]["succeeded"] == 5 and job["counts"]["rejected"] == 1
        assert job_url.startswith(JOBS)
        import sqlite3

        conn = sqlite3.connect(settings.db_path)
        certs = [r[0] for r in conn.execute("SELECT certificate_id FROM items WHERE status = 'succeeded'")]
        conn.close()
        for cert in certs:
            pdf = client.get(f"/v1/certificates/{cert}")
            assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")

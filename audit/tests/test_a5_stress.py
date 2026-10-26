"""Concurrency / stress under bounded resources: real clock, real threads and processes, bounded waits.
Invariants checked at the end: each valid item reaches a terminal state exactly once, one PDF per
succeeded item, no temp files, no item left pending/processing, DB integrity ok.
"""

from __future__ import annotations

import os
import random
import signal
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from conftest import CERT, JOBS, ROOT, wait_until
from fastapi.testclient import TestClient

from certgen.config import Settings
from certgen.main import create_app
from certgen.rendering import CertificateData, render_certificate

pytestmark = pytest.mark.stress


def db_counts(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        return dict(conn.execute("SELECT status, COUNT(*) FROM items GROUP BY status").fetchall())
    finally:
        conn.close()


def check_invariants(settings: Settings) -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert (
        conn.execute("SELECT COUNT(*) FROM items WHERE status IN ('pending','processing')").fetchone()[0] == 0
    )
    succeeded = {r[0] for r in conn.execute("SELECT certificate_id FROM items WHERE status = 'succeeded'")}
    files = [p.stem for p in settings.certificates_dir.rglob("*.pdf")]
    assert len(files) == len(set(files))
    assert succeeded <= set(
        files
    )  # every success has its file (extra files only from stale writes of failed items)
    assert list(settings.certificates_dir.rglob("*.tmp")) == []
    return conn


def test_embedded_pool_under_concurrent_clients_and_transient_failures(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        embedded_worker=True,
        worker_threads=4,
        poll_interval_seconds=0.05,
        retry_delay_seconds=0.05,
        max_active_items=50_000,
        allowed_hosts=("testserver",),
        max_concurrent_submissions=8,  # 8 producers submit at once (default limit 2, audit P8-N2)
    )
    failed_once: set[str] = set()
    lock = threading.Lock()

    def flaky(data: CertificateData) -> bytes:
        with lock:
            first = data.certificate_id not in failed_once and int(data.certificate_id, 16) % 10 == 0
            failed_once.add(data.certificate_id)
        if first:
            raise OSError("transient")
        return render_certificate(data)

    app = create_app(settings, renderer=flaky)
    job_ids: list[str] = []
    with TestClient(app) as client:
        start = threading.Barrier(8)

        def producer(p: int) -> None:
            start.wait()
            for j in range(4):
                rows = [{"name": f"P{p} J{j} R{i}", "email": f"p{p}j{j}r{i}@example.com"} for i in range(30)]
                resp = client.post(JOBS, json={"certificate": CERT, "recipients": rows})
                assert resp.status_code == 202
                with lock:
                    job_ids.append(resp.json()["id"])
                client.get(f"{JOBS}/{resp.json()['id']}/items", params={"limit": 7})

        threads = [threading.Thread(target=producer, args=(p,)) for p in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert len(job_ids) == 32

        def all_done() -> bool:
            return all(client.get(f"{JOBS}/{j}").json()["status"] == "completed" for j in job_ids)

        assert wait_until(all_done, timeout=120, step=0.5)
        archive = client.get(f"{JOBS}/{job_ids[0]}/archive")
        assert archive.status_code == 200
    conn = check_invariants(settings)
    try:
        hist = dict(conn.execute("SELECT attempts, COUNT(*) FROM items GROUP BY attempts").fetchall())
        assert set(hist) <= {1, 2} and sum(hist.values()) == 32 * 30
        assert hist.get(2, 0) == sum(1 for c in failed_once if int(c, 16) % 10 == 0)
    finally:
        conn.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_three_worker_processes_with_random_sigkill(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir,
        embedded_worker=False,
        lease_seconds=2,
        max_recipients=200,
        allowed_hosts=("testserver",),
    )
    with TestClient(create_app(settings)) as client:
        rows = [{"name": f"Proc {i}", "email": f"proc{i}@example.com"} for i in range(150)]
        assert client.post(JOBS, json={"certificate": CERT, "recipients": rows}).status_code == 202
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("CERTGEN_")},
        "CERTGEN_DATA_DIR": str(data_dir),
        "CERTGEN_LEASE_SECONDS": "2",
        "CERTGEN_TEST_SLOW_RENDER_MS": "20",
        "CERTGEN_POLL_INTERVAL_SECONDS": "0.05",
        "PYTHONPATH": str(ROOT),
    }

    def spawn() -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, "-m", "certgen.worker"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    procs = [spawn() for _ in range(3)]
    try:
        assert wait_until(lambda: db_counts(settings.db_path).get("succeeded", 0) >= 30, timeout=30)
        victim = random.Random(7).choice(procs)
        victim.send_signal(signal.SIGKILL)
        victim.wait(10)
        assert wait_until(lambda: db_counts(settings.db_path).get("succeeded", 0) == 150, timeout=60), (
            db_counts(settings.db_path)
        )
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()
    survivors_rc = sorted(p.returncode for p in procs)
    assert survivors_rc == [-9, 0, 0]
    conn = check_invariants(settings)
    try:
        assert conn.execute("SELECT MAX(attempts) FROM items").fetchone()[0] <= 2
    finally:
        conn.close()

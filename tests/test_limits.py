"""Request limits: global queue capacity (§24.1), body size (§12), media type (§24.5)."""

from __future__ import annotations

import contextlib
import threading
from typing import Any

import pytest
from conftest import job_payload, post_job
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

"""Crash recovery: leases, reclaim, fencing tokens, retirement, and real killed processes (§7.3, §8)."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    TESTCLIENT_HOSTS,
    FakeClock,
    db_rows,
    job_payload,
    post_job,
    spawn_worker,
    wait_until,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from certgen.clock import system_clock
from certgen.config import Settings
from certgen.main import create_app

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="requires POSIX signals")


def items(app: FastAPI) -> list[Any]:
    with app.state.engines.read.connect() as conn:
        return list(conn.execute(text("SELECT * FROM items ORDER BY row_index")).mappings())


@pytest.mark.req("R6", "R9")
def test_expired_lease_is_reclaimed(
    client: TestClient, app: FastAPI, make_worker: Any, fake_clock: FakeClock
) -> None:
    post_job(client, job_payload(n=1))
    crashed = make_worker(worker_id="crashed")
    assert crashed.claim_next() is not None  # then the process "dies" without finishing
    rescuer = make_worker(worker_id="rescuer")
    fake_clock.advance(29_999)
    assert rescuer.run_once() is None
    fake_clock.advance(1)  # exactly at expiry: lease_expires_at < now is still false
    assert rescuer.run_once() is None
    fake_clock.advance(1)
    assert rescuer.run_once() is not None
    row = items(app)[0]
    assert (row["status"], row["attempts"], row["lease_owner"]) == ("succeeded", 2, None)


@pytest.mark.req("R9")
@pytest.mark.parametrize("ids", [("A", "B"), ("same", "same")], ids=["distinct-ids", "reused-id"])
def test_stale_worker_done_write_rejected_by_fencing(
    client: TestClient, app: FastAPI, make_worker: Any, fake_clock: FakeClock, ids: tuple[str, str]
) -> None:
    # A worker id is a log label, not an identity: a restarted process may reuse one. Only the
    # per-claim token decides who may finish an item.
    post_job(client, job_payload(n=1))
    stale, current = make_worker(worker_id=ids[0]), make_worker(worker_id=ids[1])
    old_claim = stale.claim_next()
    fake_clock.advance(30_001)
    new_claim = current.claim_next()
    assert old_claim is not None and new_claim is not None
    assert old_claim.item_id == new_claim.item_id and old_claim.token != new_claim.token
    stale.record_success(old_claim)  # late completion after losing the lease
    stale.record_failure(old_claim, RuntimeError("late"))
    row = items(app)[0]
    assert (row["status"], row["lease_token"], row["attempts"]) == ("processing", new_claim.token, 2)
    current.process(new_claim)
    assert items(app)[0]["status"] == "succeeded"


@pytest.mark.req("R9")
def test_same_worker_reclaim_is_fenced(
    client: TestClient,
    app: FastAPI,
    make_worker: Any,
    fake_clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    post_job(client, job_payload(n=1))
    worker = make_worker(worker_id="only")
    first = worker.claim_next()
    fake_clock.advance(30_001)
    second = worker.claim_next()  # the same worker takes its own expired item back
    assert first is not None and second is not None and first.token != second.token
    worker.record_success(first)
    assert "lease_lost" in caplog.text
    row = items(app)[0]
    assert (row["status"], row["lease_token"]) == ("processing", second.token)
    worker.record_success(second)
    assert items(app)[0]["status"] == "succeeded"


@pytest.mark.req("R9", "R18")
def test_crash_loop_ends_failed_after_max_attempts(
    client: TestClient, app: FastAPI, make_worker: Any, fake_clock: FakeClock
) -> None:
    job_id = post_job(client, job_payload(n=1)).json()["id"]
    worker = make_worker()
    for attempt in (1, 2, 3):
        claim = worker.claim_next()  # each attempt crashes the process before it finishes
        assert claim is not None and claim.attempts == attempt
        fake_clock.advance(30_001)
    assert worker.claim_next() is None  # the claim's retire step ends the item instead
    row = items(app)[0]
    assert (row["status"], row["attempts"], row["lease_token"]) == ("failed", 3, None)
    assert json.loads(row["errors"]) == [
        {"field": None, "code": "WORKER_LOST", "message": "The worker processing this certificate stopped."}
    ]
    assert row["completed_at"] == fake_clock()
    assert client.get(f"/v1/certificate-jobs/{job_id}").json()["status"] == "failed"


def _create_job(data_dir: Path, n: int) -> str:
    settings = Settings(data_dir=data_dir, embedded_worker=False, allowed_hosts=TESTCLIENT_HOSTS)
    with TestClient(create_app(settings, clock=system_clock)) as c:
        resp = post_job(c, job_payload(n=n))
        assert resp.status_code == 202
        return str(resp.json()["id"])


def _stop(proc: Any) -> int:
    proc.send_signal(signal.SIGTERM)
    return int(proc.wait(timeout=15))


@pytest.mark.slow
@POSIX_ONLY
@pytest.mark.req("R9")
def test_kill_mid_job_no_duplicates(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _create_job(data_dir, 6)
    db = data_dir / "certgen.sqlite3"
    env = {
        "CERTGEN_LEASE_SECONDS": "2",
        "CERTGEN_TEST_SLOW_RENDER_MS": "300",
        "CERTGEN_POLL_INTERVAL_SECONDS": "0.05",
    }
    first = spawn_worker(data_dir, **env)

    def mid_render() -> bool:
        # >= 1 done, and an item claimed < 200 ms ago, so its 300 ms render is certainly still running.
        rows = db_rows(db, "SELECT status, lease_expires_at FROM items")
        now_ms = int(time.time() * 1000)
        fresh = [r for r in rows if r["status"] == "processing" and r["lease_expires_at"] - now_ms > 1_800]
        return any(r["status"] == "succeeded" for r in rows) and len(fresh) == 1

    assert wait_until(mid_render, timeout=30, step=0.005), first.stderr.read() if first.stderr else ""
    first.send_signal(signal.SIGKILL)
    first.wait(timeout=10)
    killed = db_rows(db, "SELECT id FROM items WHERE status = 'processing'")
    assert len(killed) == 1
    killed_id = killed[0]["id"]

    second = spawn_worker(data_dir, **env)
    done = wait_until(
        lambda: db_rows(db, "SELECT COUNT(*) AS n FROM items WHERE status = 'succeeded'")[0]["n"] == 6,
        timeout=30,
    )
    assert _stop(second) == 0
    assert done
    rows = {r["id"]: r for r in db_rows(db, "SELECT id, status, attempts FROM items")}
    assert {r["status"] for r in rows.values()} == {"succeeded"}
    assert rows[killed_id]["attempts"] == 2  # reclaimed once after the lease expired
    assert sorted(r["attempts"] for r in rows.values()) == [1, 1, 1, 1, 1, 2]
    assert len(list(data_dir.rglob("*.pdf"))) == 6  # one file per item, no duplicates
    assert list(data_dir.rglob("*.tmp")) == []


@pytest.mark.slow
@POSIX_ONLY
@pytest.mark.req("R9")
def test_two_worker_processes_never_double_process(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _create_job(data_dir, 20)
    db = data_dir / "certgen.sqlite3"
    env = {"CERTGEN_TEST_SLOW_RENDER_MS": "40", "CERTGEN_POLL_INTERVAL_SECONDS": "0.05"}
    workers = [spawn_worker(data_dir, **env) for _ in range(2)]
    done = wait_until(
        lambda: db_rows(db, "SELECT COUNT(*) AS n FROM items WHERE status = 'succeeded'")[0]["n"] == 20,
        timeout=30,
    )
    assert [_stop(w) for w in workers] == [0, 0]  # SIGTERM is a graceful stop
    assert done
    rows = db_rows(db, "SELECT attempts FROM items")
    assert [r["attempts"] for r in rows] == [1] * 20  # no item was ever claimed twice
    assert len(list(data_dir.rglob("*.pdf"))) == 20

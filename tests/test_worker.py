"""Worker: claiming, processing, fenced transitions, loops, supervision and file handling (§7, §11, §24.2)."""

from __future__ import annotations

import contextlib
import io
import logging
import os
import sqlite3
import stat
import threading
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    START_MS,
    FakeClock,
    blocking,
    fail_n_times,
    job_payload,
    post_job,
    recipient,
    wait_until,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from certgen.config import Settings
from certgen.rendering import CertificateData, render_certificate
from certgen.worker import Worker, WorkerPool


def items(app: FastAPI) -> list[Any]:
    with app.state.engines.read.connect() as conn:
        return list(conn.execute(text("SELECT * FROM items ORDER BY row_index")).mappings())


def write_lock_is_free(db_path: Path) -> bool:
    probe = sqlite3.connect(db_path, timeout=0, isolation_level=None)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        probe.close()


@pytest.mark.req("R3", "R16")
def test_drain_creates_one_pdf_per_valid_item_and_none_for_rejected(
    client: TestClient, app: FastAPI, drain: Any, settings: Settings
) -> None:
    rows = [recipient(0), recipient(1, email="bad"), recipient(2), recipient(3, name="")]
    post_job(client, job_payload(recipients=rows))
    assert len(drain()) == 2
    stored = items(app)
    files = sorted(p.name for p in settings.certificates_dir.rglob("*") if p.is_file())
    expected = sorted(f"{r['certificate_id']}.pdf" for r in stored if r["status"] == "succeeded")
    assert files == expected
    assert [r["status"] for r in stored] == ["succeeded", "rejected", "succeeded", "rejected"]
    assert drain() == []  # nothing left; rejected rows are never claimed


@pytest.mark.req("R6")
def test_item_transitions_are_recorded(
    client: TestClient, app: FastAPI, make_worker: Any, fake_clock: FakeClock
) -> None:
    post_job(client, job_payload(n=1))
    worker = make_worker(worker_id="w-1")
    claim = worker.claim_next()
    assert claim is not None and claim.attempts == 1
    row = items(app)[0]
    assert (row["status"], row["lease_owner"], row["lease_token"]) == ("processing", "w-1", claim.token)
    assert row["lease_expires_at"] == START_MS + 30_000
    assert row["attempts"] == 1
    fake_clock.advance(250)
    worker.process(claim)
    row = items(app)[0]
    assert row["status"] == "succeeded"
    assert (row["lease_owner"], row["lease_token"], row["lease_expires_at"]) == (None, None, None)
    assert (row["errors"], row["available_at"]) == (None, None)
    assert row["completed_at"] == START_MS + 250


@pytest.mark.req("R6")
def test_claim_returns_none_when_empty(client: TestClient, make_worker: Any) -> None:
    worker = make_worker()
    assert worker.claim_next() is None
    assert worker.run_once() is None
    assert worker.run_until_idle() == []


@pytest.mark.req("R11")
def test_render_runs_outside_write_transaction(
    client: TestClient, make_worker: Any, settings: Settings, app: FastAPI
) -> None:
    post_job(client, job_payload(n=2))
    observed: list[bool] = []

    def probing(data: CertificateData) -> bytes:
        observed.append(write_lock_is_free(settings.db_path))
        return render_certificate(data)

    make_worker(probing).run_until_idle()
    assert observed == [True, True]
    assert {r["status"] for r in items(app)} == {"succeeded"}


@pytest.mark.req("R3", "R9")
def test_two_concurrent_workers_never_double_process(
    client: TestClient, app: FastAPI, make_worker: Any
) -> None:
    post_job(client, job_payload(n=4))
    rendered: list[str] = []
    lock = threading.Lock()

    def recording(inner: Any) -> Any:
        def render(data: CertificateData) -> bytes:
            with lock:
                rendered.append(data.certificate_id)
            return inner(data)

        return render

    # Rendezvous right after the claim's SELECT: if the SELECT and UPDATE were separate
    # transactions, both workers would pass here together and pick the same item.
    rendezvous = threading.Barrier(2, timeout=1.0)

    def after_select(conn: Any, cursor: Any, statement: str, *_args: Any) -> None:
        if statement.startswith("SELECT id FROM items WHERE status = 'pending'"):
            with contextlib.suppress(threading.BrokenBarrierError):
                rendezvous.wait()

    for engine in (app.state.engines.write, app.state.engines.read):
        event.listen(engine, "after_cursor_execute", after_select)
    release, entered = threading.Event(), threading.Event()
    worker_a = make_worker(recording(blocking(release, entered)), worker_id="A")
    worker_b = make_worker(recording(render_certificate), worker_id="B")
    claims: dict[str, Any] = {}

    def run_a() -> None:
        claims["A"] = worker_a.claim_next()
        worker_a.process(claims["A"])

    thread_a = threading.Thread(target=run_a)
    thread_b = threading.Thread(target=lambda: claims.__setitem__("B", worker_b.claim_next()))
    thread_a.start()
    thread_b.start()
    thread_b.join(timeout=30)
    assert entered.wait(timeout=30)  # A holds its item inside the renderer
    assert claims["A"].item_id != claims["B"].item_id
    worker_b.process(claims["B"])
    release.set()
    thread_a.join(timeout=30)
    for engine in (app.state.engines.write, app.state.engines.read):
        event.remove(engine, "after_cursor_execute", after_select)
    worker_a.run_until_idle()
    worker_b.run_until_idle()
    stored = items(app)
    assert [r["status"] for r in stored] == ["succeeded"] * 4
    assert [r["attempts"] for r in stored] == [1] * 4  # every item claimed exactly once
    assert sorted(rendered) == sorted(r["certificate_id"] for r in stored)


@pytest.mark.req("R9")
def test_clock_read_after_lock(
    client: TestClient, app: FastAPI, settings: Settings, fake_clock: FakeClock
) -> None:
    post_job(client, job_payload(recipients=[recipient(0), recipient(1, name="Fails Always")]))
    lock_free_at_clock_read: list[bool] = []

    def observing_clock() -> int:
        lock_free_at_clock_read.append(write_lock_is_free(settings.db_path))
        return fake_clock()

    def render(data: CertificateData) -> bytes:
        if data.name == "Fails Always":
            raise RuntimeError("boom")
        return render_certificate(data)

    worker = Worker(app.state.engines, app.state.storage, settings, observing_clock, render)
    worker.run_once()  # claim + success
    worker.run_once()  # claim + failure
    worker.run_once()  # empty claim (the failed item waits out its retry delay)
    # Every clock read (claim, success, failure) happened while this worker held the write lock.
    assert len(lock_free_at_clock_read) >= 5
    assert not any(lock_free_at_clock_read)


@pytest.mark.req("R9")
def test_retry_after_transient_failure_needs_delay(
    client: TestClient, app: FastAPI, make_worker: Any, fake_clock: FakeClock
) -> None:
    post_job(client, job_payload(n=1))
    worker = make_worker(fail_n_times(1))
    assert worker.run_once() is not None
    row = items(app)[0]
    assert (row["status"], row["attempts"], row["available_at"]) == ("pending", 1, START_MS + 2_000)
    assert worker.run_once() is None
    fake_clock.advance(1_999)
    assert worker.run_once() is None
    fake_clock.advance(1)
    assert worker.run_once() is not None
    row = items(app)[0]
    assert (row["status"], row["attempts"], row["errors"]) == ("succeeded", 2, None)


@pytest.mark.req("R9")
def test_atomic_write_no_partial_file(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = app.state.storage
    job_id, cert = "a" * 32, "b" * 32
    storage.save_atomic(job_id, cert, b"%PDF-old complete file")
    final = storage.path_for(job_id, cert)

    def failing_fsync(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="disk full"):
        storage.save_atomic(job_id, cert, b"%PDF-new partial")
    # The previous complete file is untouched and no temp file is left behind.
    assert final.read_bytes() == b"%PDF-old complete file"
    assert sorted(p.name for p in final.parent.iterdir()) == [final.name]
    with pytest.raises(OSError):
        storage.save_atomic(job_id, "c" * 32, b"%PDF-never visible")
    assert not storage.path_for(job_id, "c" * 32).exists()
    assert sorted(p.name for p in final.parent.iterdir()) == [final.name]


@pytest.mark.req("R9")
def test_orphan_tmp_sweep_age_rule(
    app: FastAPI, client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    storage = app.state.storage
    job_dir = storage.root / ("a" * 32)
    job_dir.mkdir()
    now_ms = 1_800_000_000_000
    old = job_dir / f"{'b' * 32}.{'1' * 32}.tmp"
    fresh = job_dir / f"{'c' * 32}.{'2' * 32}.tmp"
    pdf = job_dir / f"{'d' * 32}.pdf"
    for path, age_ms in ((old, 60_001), (fresh, 59_999), (pdf, 10**9)):
        path.write_bytes(b"x")
        mtime = (now_ms - age_ms) / 1000
        os.utime(path, (mtime, mtime))
    with caplog.at_level(logging.INFO, logger="certgen"):
        removed = storage.sweep_orphans(older_than_ms=60_000, now_ms=now_ms)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists() and pdf.exists()
    assert "orphan_tmp_removed" in caplog.text


@pytest.mark.req("R9")
def test_run_forever_sweeps_orphans_at_start(app: FastAPI, client: TestClient, make_worker: Any) -> None:
    job_dir = app.state.storage.root / ("a" * 32)
    job_dir.mkdir()
    orphan = job_dir / f"{'b' * 32}.{'1' * 32}.tmp"
    orphan.write_bytes(b"x")
    os.utime(orphan, (0, 0))  # ancient
    stop = threading.Event()
    stop.set()  # loop body never runs; only the start-up sweep
    make_worker().run_forever(stop)
    assert not orphan.exists()


@pytest.mark.req("R9")
def test_loop_survives_db_error(
    client: TestClient, app: FastAPI, settings: Settings, fake_clock: FakeClock
) -> None:
    post_job(client, job_payload(n=1))
    stop = threading.Event()
    fast = Settings(data_dir=settings.data_dir, embedded_worker=False, poll_interval_seconds=0.05)

    def stopping_render(data: CertificateData) -> bytes:
        stop.set()
        return render_certificate(data)

    worker = Worker(app.state.engines, app.state.storage, fast, fake_clock, stopping_render)
    real_claim = worker.claim_next
    calls = {"n": 0}

    def flaky_claim() -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database is locked"))
        return real_claim()

    worker.claim_next = flaky_claim  # type: ignore[method-assign]
    thread = threading.Thread(target=worker.run_forever, args=(stop,))
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert calls["n"] >= 2
    assert items(app)[0]["status"] == "succeeded"


@pytest.mark.req("R6")
def test_graceful_shutdown_costs_no_attempts(client: TestClient, app: FastAPI, make_worker: Any) -> None:
    post_job(client, job_payload(n=3))
    release, entered = threading.Event(), threading.Event()
    stop = threading.Event()
    thread = threading.Thread(target=make_worker(blocking(release, entered)).run_forever, args=(stop,))
    thread.start()
    assert entered.wait(timeout=10)
    stop.set()  # shutdown requested while an item is mid-render
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    stored = items(app)
    assert [(r["status"], r["attempts"]) for r in stored] == [
        ("succeeded", 1),
        ("pending", 0),
        ("pending", 0),
    ]


@pytest.mark.req("R5")
def test_job_snapshot_ignores_worker_settings(
    client: TestClient, app: FastAPI, settings: Settings, fake_clock: FakeClock
) -> None:
    # The job was accepted with print_email=true; a worker started with the opposite setting
    # (e.g. after a redeploy) must still render what the job promised.
    post_job(client, job_payload(recipients=[recipient(0, email="snap@example.com")]))
    flipped = Settings(data_dir=settings.data_dir, embedded_worker=False, print_email=False)
    Worker(app.state.engines, app.state.storage, flipped, fake_clock, render_certificate).run_until_idle()
    row = items(app)[0]
    pdf = app.state.storage.read(_job_id(app), row["certificate_id"])
    text_content = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert "snap@example.com" in text_content


def _job_id(app: FastAPI) -> str:
    with app.state.engines.read.connect() as conn:
        return str(conn.execute(text("SELECT id FROM jobs")).scalar_one())


@pytest.mark.req("R9")
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX permissions")
def test_created_files_are_private(client: TestClient, app: FastAPI, drain: Any, settings: Settings) -> None:
    post_job(client, job_payload(n=2))
    drain()
    root = settings.certificates_dir
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    job_dirs = list(root.iterdir())
    assert len(job_dirs) == 1
    assert stat.S_IMODE(job_dirs[0].stat().st_mode) == 0o700
    pdfs = list(job_dirs[0].iterdir())
    assert len(pdfs) == 2
    assert {stat.S_IMODE(p.stat().st_mode) for p in pdfs} == {0o600}
    assert stat.S_IMODE(settings.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings.db_path.stat().st_mode) == 0o600


# --- Embedded supervision (§24.2) ---------------------------------------------------------------


class FakeTime:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.now += seconds
        return False


class DyingWorker:
    """A worker whose loop ends at once, as if its thread crashed."""

    started = 0

    def run_forever(self, stop: threading.Event) -> None:
        DyingWorker.started += 1


def make_pool(
    app: FastAPI, settings: Settings, fake_clock: FakeClock, fake_time: FakeTime, size: int = 1
) -> Any:
    DyingWorker.started = 0
    return WorkerPool(
        lambda: DyingWorker(),  # type: ignore[arg-type,return-value]
        size,
        app.state.engines,
        settings,
        fake_clock,
        monotonic=fake_time.monotonic,
        wait=fake_time.wait,
    )


@pytest.mark.req("R9")
def test_monitor_restarts_dead_thread(
    client: TestClient, app: FastAPI, settings: Settings, fake_clock: FakeClock
) -> None:
    fake_time = FakeTime()
    pool = make_pool(app, settings, fake_clock, fake_time, size=2)
    pool.start_workers()
    assert wait_until(lambda: pool.alive() == 0)  # both loops returned: the threads are dead
    assert DyingWorker.started == 2
    pool.supervise_once()
    assert wait_until(lambda: DyingWorker.started == 4)
    assert fake_time.waits == [0.25, 0.25]  # first restart of each slot uses the first backoff step
    pool.stop()


@pytest.mark.req("R9")
def test_restart_budget_is_bounded(
    client: TestClient,
    app: FastAPI,
    settings: Settings,
    fake_clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_time = FakeTime()
    pool = make_pool(app, settings, fake_clock, fake_time, size=1)
    pool.start_workers()
    with caplog.at_level(logging.WARNING, logger="certgen"):
        for expected_starts in (2, 3, 4):
            assert wait_until(lambda: pool.alive() == 0)
            pool.supervise_once()
            assert wait_until(lambda n=expected_starts: DyingWorker.started == n)  # type: ignore[misc]
        assert fake_time.waits == [0.25, 0.5, 1.0]
        assert wait_until(lambda: pool.alive() == 0)
        pool.supervise_once()  # fourth death within 60 s: budget spent
        fake_time.now += 120  # even later, the slot stays down
        pool.supervise_once()
    assert DyingWorker.started == 4
    assert pool.alive() == 0
    assert caplog.text.count("worker_slot_exhausted") == 1
    pool.stop()


@pytest.mark.req("R9")
def test_monitor_retires_exhausted_lease_without_claims(
    client: TestClient, app: FastAPI, settings: Settings, make_worker: Any, fake_clock: FakeClock
) -> None:
    post_job(client, job_payload(n=2))
    worker = make_worker()
    for _ in range(settings.max_attempts):  # each attempt "crashes": claimed, never finished
        assert worker.claim_next() is not None
        fake_clock.advance(settings.lease_ms + 1)
    with app.state.engines.write.begin() as conn:  # the second item stays untouched by the monitor
        conn.execute(
            text("UPDATE items SET available_at = :t WHERE row_index = 1"), {"t": fake_clock() + 10**9}
        )
    pool = make_pool(app, settings, fake_clock, FakeTime(), size=0)
    pool.supervise_once()
    stored = items(app)
    assert stored[0]["status"] == "failed"
    assert '"code":"WORKER_LOST"' in stored[0]["errors"]
    assert stored[0]["completed_at"] == fake_clock()
    assert stored[0]["lease_token"] is None
    assert (stored[1]["status"], stored[1]["attempts"]) == ("pending", 0)

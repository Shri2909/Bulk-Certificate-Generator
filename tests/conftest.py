"""Shared fixtures and helpers: fake clock, app without embedded workers, synchronous drain, ASGI drivers."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from certgen.config import Settings
from certgen.main import create_app
from certgen.rendering import CertificateData, render_certificate
from certgen.worker import Worker

ROOT = Path(__file__).resolve().parent.parent
START_MS = int(datetime(2026, 10, 11, 8, 0, tzinfo=UTC).timestamp() * 1000)
JOBS = "/v1/certificate-jobs"
# The Host header Starlette's TestClient sends; the app only serves allowed hosts.
TESTCLIENT_HOSTS = ("testserver",)
# Every /v1 route needs a bearer token; the client fixture sends this one.
TEST_API_TOKEN = "test-token-0123456789abcdef"
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_API_TOKEN}"}

Renderer = Callable[[CertificateData], bytes]


class FakeClock:
    """Epoch-ms clock that only moves when advanced, so lease boundaries are tested without sleeping."""

    def __init__(self, start_ms: int = START_MS) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    """Override in a test module (or parametrize) to change Settings for the app fixture."""
    return {}


@pytest.fixture
def settings(tmp_path: Path, settings_overrides: dict[str, Any]) -> Settings:
    values: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "embedded_worker": False,
        "allowed_hosts": TESTCLIENT_HOSTS,
        "api_tokens": (TEST_API_TOKEN,),
    }
    values.update(settings_overrides)
    return Settings(**values)


@pytest.fixture
def app(settings: Settings, fake_clock: FakeClock) -> FastAPI:
    return create_app(settings, clock=fake_clock)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, headers=AUTH_HEADERS) as c:
        yield c


@pytest.fixture
def make_worker(
    app: FastAPI,
    client: TestClient,  # starts the lifespan, which creates the engines
    settings: Settings,
    fake_clock: FakeClock,
) -> Any:
    """Build a Worker on the app's engines."""

    def _make(renderer: Renderer = render_certificate, worker_id: str | None = None) -> Worker:
        return Worker(
            app.state.engines, app.state.storage, settings, fake_clock, renderer, worker_id=worker_id
        )

    return _make


@pytest.fixture
def drain(make_worker: Any) -> Callable[..., list[int]]:
    """Process every claimable item synchronously, in this thread, with the test's FakeClock."""

    def _drain(renderer: Renderer = render_certificate) -> list[int]:
        worker: Worker = make_worker(renderer)
        return worker.run_until_idle()

    return _drain


def certificate_info(**overrides: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "title": "Python for Data Engineering",
        "issuer": "Acme Academy",
        "issued_on": "2026-10-01",
    }
    info.update(overrides)
    return info


def recipient(i: int, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"name": f"Recipient {i}", "email": f"recipient{i}@example.com"}
    row.update(overrides)
    return row


def job_payload(
    recipients: list[Any] | None = None, n: int = 3, certificate: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "certificate": certificate if certificate is not None else certificate_info(),
        "recipients": recipients if recipients is not None else [recipient(i) for i in range(n)],
    }


def post_job(client: TestClient, payload: Any, **kwargs: Any) -> Any:
    return client.post(JOBS, json=payload, **kwargs)


def post_raw(client: TestClient, body: bytes, headers: dict[str, str] | None = None, path: str = JOBS) -> Any:
    """POST raw bytes as application/json."""
    return client.post(path, content=body, headers={"Content-Type": "application/json", **(headers or {})})


def item_rows(app: FastAPI) -> list[Any]:
    """Every items row, in row order, as mappings."""
    with app.state.engines.read.connect() as conn:
        return list(conn.execute(text("SELECT * FROM items ORDER BY row_index")).mappings())


def job_count(app: FastAPI) -> int:
    with app.state.engines.read.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one())


def only_job_id(app: FastAPI) -> str:
    with app.state.engines.read.connect() as conn:
        return str(conn.execute(text("SELECT id FROM jobs")).scalar_one())


# --- Renderer fault injection -------------------------------------------------------------------


def failing_for(names: set[str]) -> Renderer:
    """Always raises for the given recipient names; renders everyone else normally."""

    def render(data: CertificateData) -> bytes:
        if data.name in names:
            raise RuntimeError(f"simulated render failure for {data.name}")
        return render_certificate(data)

    return render


def fail_n_times(n: int) -> Renderer:
    """Raises on the first n calls overall, then renders normally."""
    calls = {"n": 0}

    def render(data: CertificateData) -> bytes:
        calls["n"] += 1
        if calls["n"] <= n:
            raise RuntimeError("simulated transient failure")
        return render_certificate(data)

    return render


def blocking(release: threading.Event, entered: threading.Event | None = None) -> Renderer:
    """Signals `entered`, then blocks until `release` is set; lets a test act while an item is claimed."""

    def render(data: CertificateData) -> bytes:
        if entered is not None:
            entered.set()
        assert release.wait(timeout=30), "test never released the blocked renderer"
        return render_certificate(data)

    return render


# --- Subprocess helpers for crash tests ---------------------------------------------------------


def spawn_worker(data_dir: Path, **env: str) -> subprocess.Popen[bytes]:
    """Start `python -m certgen.worker` on data_dir; extra env vars override (e.g. lease, slow render)."""
    full_env = {**os.environ, "CERTGEN_DATA_DIR": str(data_dir), "PYTHONPATH": str(ROOT), **env}
    return subprocess.Popen(
        [sys.executable, "-m", "certgen.worker"],
        cwd=ROOT,
        env=full_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def db_rows(db_path: Path, sql: str) -> list[sqlite3.Row]:
    """Read-only query on a database another process is writing (WAL lets this run concurrently)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0, step: float = 0.02) -> bool:
    """Bounded poll for tests that cross a process or thread boundary; never an unbounded sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


# --- ASGI drivers for middleware tests ----------------------------------------------------------

Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]


def http_scope(
    method: str = "GET", path: str = "/", headers: list[tuple[bytes, bytes]] | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "query_string": b"",
        **extra,
    }


async def empty_body() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


async def unreachable_app(_scope: Any, _receive: Any, _send: Any) -> None:
    raise AssertionError("the request reached the wrapped app")


async def asgi_call(
    app: Any,
    scope: dict[str, Any],
    receive: Receive = empty_body,
    send: Callable[[Message], Awaitable[None]] | None = None,
) -> list[Message]:
    """Run one request through an ASGI app and return the messages it sent."""
    sent: list[Message] = []

    async def collect(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send or collect)
    return sent

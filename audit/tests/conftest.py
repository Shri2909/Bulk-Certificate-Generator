"""Audit fixtures. Independent of tests/conftest.py so the audit suite does not inherit its assumptions.

Run from an isolated copy of the repository root (never the original checkout):
    python -m pytest audit/tests -p no:cacheprovider
`pyproject.toml` puts the copy's root on sys.path, so `certgen` is imported from the copy.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from certgen.config import Settings
from certgen.main import create_app
from certgen.rendering import CertificateData, render_certificate
from certgen.worker import Worker

ROOT = Path(__file__).resolve().parents[2]
JOBS = "/v1/certificate-jobs"
START_MS = int(datetime(2026, 10, 11, 8, 0, tzinfo=UTC).timestamp() * 1000)
JSON_HEADERS = {"Content-Type": "application/json"}
CERT = {"title": "Python for Data Engineering", "issuer": "Acme Academy", "issued_on": "2026-10-01"}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "red(finding): TDD red spec; expected to FAIL until the finding is fixed"
    )
    config.addinivalue_line("markers", "e2e: starts a real uvicorn server subprocess")
    config.addinivalue_line("markers", "stress: concurrency/load test under bounded resources")


class FakeClock:
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
    return {}


@pytest.fixture
def settings(tmp_path: Path, settings_overrides: dict[str, Any]) -> Settings:
    # "testserver" is the Host header Starlette's TestClient sends (the app serves allowed hosts only).
    values: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "embedded_worker": False,
        "allowed_hosts": ("testserver",),
    }
    values.update(settings_overrides)
    return Settings(**values)


@pytest.fixture
def app(settings: Settings, fake_clock: FakeClock) -> FastAPI:
    return create_app(settings, clock=fake_clock)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # raise_server_exceptions=False: the audit observes what a real client receives (e.g. a 500),
    # instead of letting the test client re-raise the server-side exception.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def drain(
    app: FastAPI, client: TestClient, settings: Settings, fake_clock: FakeClock
) -> Callable[..., list[int]]:
    def _drain(renderer: Callable[[CertificateData], bytes] = render_certificate) -> list[int]:
        return Worker(app.state.engines, app.state.storage, settings, fake_clock, renderer).run_until_idle()

    return _drain


def payload(
    recipients: list[Any] | None = None, n: int = 3, certificate: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "certificate": certificate if certificate is not None else dict(CERT),
        "recipients": recipients
        if recipients is not None
        else [{"name": f"Recipient {i}", "email": f"r{i}@example.com"} for i in range(n)],
    }


def post_raw(client: TestClient, body: bytes, headers: dict[str, str] | None = None, path: str = JOBS) -> Any:
    return client.post(path, content=body, headers={**JSON_HEADERS, **(headers or {})})


# --- Real server (E2E) -----------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Server:
    base: str
    port: int
    proc: subprocess.Popen[bytes]
    data_dir: Path
    log: Path

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> tuple[int, dict[str, str], bytes]:
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()

    def json(
        self, method: str, path: str, obj: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        body = json.dumps(obj).encode() if obj is not None else None
        status, _, raw = self.request(method, path, body, {**JSON_HEADERS, **(headers or {})})
        return status, json.loads(raw) if raw else None


def start_server(tmp_path: Path, **env: str) -> Server:
    port = free_port()
    data_dir = tmp_path / "server-data"
    log = tmp_path / "server.log"
    full_env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("CERTGEN_")},
        "CERTGEN_DATA_DIR": str(data_dir),
        "CERTGEN_PORT": str(port),
        "CERTGEN_POLL_INTERVAL_SECONDS": "0.05",
        "PYTHONPATH": str(ROOT),
        **env,
    }
    with log.open("wb") as fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "certgen"], cwd=ROOT, env=full_env, stdout=fh, stderr=subprocess.STDOUT
        )
    server = Server(f"http://127.0.0.1:{port}", port, proc, data_dir, log)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if server.request("GET", "/healthz", timeout=1)[0] == 200:
                return server
        except OSError:
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early: {log.read_text()[-2000:]}")
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError("server did not become healthy")


def stop_server(server: Server) -> int:
    if server.proc.poll() is None:
        server.proc.send_signal(signal.SIGINT)
        try:
            server.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.proc.kill()
            server.proc.wait(timeout=5)
    return int(server.proc.returncode)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    srv = start_server(tmp_path)
    try:
        yield srv
    finally:
        stop_server(srv)


def wait_until(predicate: Callable[[], bool], timeout: float = 20.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()

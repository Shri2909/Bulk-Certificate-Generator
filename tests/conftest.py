"""Shared fixtures: fake clock, per-test data dir, app without embedded workers, synchronous drain."""

from __future__ import annotations

from collections.abc import Callable, Iterator
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

START_MS = int(datetime(2026, 10, 11, 8, 0, tzinfo=UTC).timestamp() * 1000)
JOBS = "/v1/certificate-jobs"

Renderer = Callable[[CertificateData], bytes]


class FakeClock:
    """Epoch-ms clock that only moves when a test says so; lease boundaries are tested without sleeping."""

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
    values: dict[str, Any] = {"data_dir": tmp_path / "data", "embedded_worker": False}
    values.update(settings_overrides)
    return Settings(**values)


@pytest.fixture
def app(settings: Settings, fake_clock: FakeClock) -> FastAPI:
    return create_app(settings, clock=fake_clock)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_worker(app: FastAPI, client: TestClient, settings: Settings, fake_clock: FakeClock) -> Any:
    """Build a Worker on the app's engines (the client fixture has run lifespan, so engines exist)."""

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

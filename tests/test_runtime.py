"""Process entry points: `python -m certgen` server limits (§24.12) and the standalone worker."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from conftest import ROOT, wait_until

from certgen import main
from certgen.config import Settings


@pytest.mark.req("R11", "R13")
def test_server_limits_passed_to_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(app: str, **kwargs: Any) -> None:
        captured.update(app=app, **kwargs)

    monkeypatch.setattr(main.uvicorn, "run", fake_run)
    # serve() uses the settings the module-level app parsed from the environment at import.
    monkeypatch.setattr(main.app.state, "settings", Settings(host="127.0.0.1", port=8123))
    main.serve()
    assert captured["app"] == "certgen.main:app"
    assert (captured["host"], captured["port"]) == ("127.0.0.1", 8123)
    assert captured["limit_concurrency"] == 64
    assert captured["timeout_keep_alive"] == 5


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _healthz(port: int) -> dict[str, Any] | None:
    import json

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as resp:
            return dict(json.loads(resp.read()))
    except OSError:
        return None


@pytest.mark.slow
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX signals")
@pytest.mark.req("R13")
def test_python_dash_m_certgen_serves_and_stops(tmp_path: Path) -> None:
    port = _free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "CERTGEN_DATA_DIR": str(tmp_path / "data"),
        "CERTGEN_PORT": str(port),
    }
    proc = subprocess.Popen([sys.executable, "-m", "certgen"], cwd=tmp_path, env=env, stderr=subprocess.PIPE)
    try:
        assert wait_until(lambda: _healthz(port) is not None, timeout=30, step=0.1)
        assert _healthz(port) == {
            "status": "ok",
            "db": "ok",
            "embedded_workers_alive": 2,
            "embedded_workers_expected": 2,
        }
    finally:
        proc.send_signal(signal.SIGINT)
        code = proc.wait(timeout=20)
    assert code == 0
    assert (tmp_path / "data" / "certgen.sqlite3").exists()


@pytest.mark.slow
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX signals")
@pytest.mark.req("R13")
def test_standalone_worker_starts_on_empty_data_dir_and_stops_on_sigterm(tmp_path: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "CERTGEN_DATA_DIR": str(tmp_path / "data")}
    proc = subprocess.Popen([sys.executable, "-m", "certgen.worker"], cwd=ROOT, env=env)
    db = tmp_path / "data" / "certgen.sqlite3"
    assert wait_until(db.exists, timeout=30, step=0.05)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=20) == 0

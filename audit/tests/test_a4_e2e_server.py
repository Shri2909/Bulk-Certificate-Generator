"""End-to-end tests against a real `python -m certgen` process (uvicorn, embedded workers, real clock,
real HTTP on 127.0.0.1 with an ephemeral port and a temporary data directory). Every wait is bounded.
"""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
from conftest import AUDIT_TOKEN, CERT, JOBS, Server, start_server, stop_server, wait_until

from certgen.config import Settings

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(os.name != "posix", reason="POSIX signals")]

README_BODY = {
    "certificate": {
        "title": "Python for Data Engineering",
        "issuer": "Acme Academy",
        "issued_on": "2026-10-01",
    },
    "recipients": [
        {"name": "Zoë Łukasz", "email": "zoe@example.com"},
        {"name": "Bob", "email": "not-an-email"},
        {"name": "Νίκος Παπαδόπουλος", "email": "nikos@example.org"},
    ],
}


def terminal(server: Server, job_id: str) -> dict[str, Any]:
    state: dict[str, Any] = {}

    def done() -> bool:
        state.update(server.json("GET", f"{JOBS}/{job_id}")[1])
        return state["status"] in ("completed", "completed_with_errors", "failed")

    assert wait_until(done, timeout=30), state
    return state


def test_readme_quickstart_flow(server: Server) -> None:
    status, headers, raw = server.request(
        "POST", JOBS, json.dumps(README_BODY).encode(), {"Content-Type": "application/json"}
    )
    assert status == 202
    body = json.loads(raw)
    assert headers["location"] == f"{JOBS}/{body['id']}"
    assert [(e["row_index"], e["code"]) for e in body["errors"]] == [(1, "INVALID_EMAIL")]
    job = terminal(server, body["id"])
    assert job["status"] == "completed_with_errors" and job["counts"]["succeeded"] == 2
    page = server.json("GET", f"{JOBS}/{body['id']}/items?status=succeeded")[1]
    status, headers, pdf = server.request("GET", page["items"][0]["download_url"])
    assert status == 200 and headers["content-type"] == "application/pdf" and pdf.startswith(b"%PDF-")
    status, _, zbytes = server.request("GET", f"{JOBS}/{body['id']}/archive")
    names = zipfile.ZipFile(io.BytesIO(zbytes)).namelist()
    assert status == 200 and len([n for n in names if n.endswith(".pdf")]) == 2 and "manifest.csv" in names


def test_api_process_sigkill_mid_job_then_restart_completes(tmp_path: Path) -> None:
    env = {"CERTGEN_LEASE_SECONDS": "2", "CERTGEN_TEST_SLOW_RENDER_MS": "300", "CERTGEN_WORKER_THREADS": "2"}
    srv = start_server(tmp_path, **env)
    rows = [{"name": f"Crash {i}", "email": f"c{i}@example.com"} for i in range(8)]
    status, body = srv.json("POST", JOBS, {"certificate": CERT, "recipients": rows})
    assert status == 202
    db = srv.data_dir / "certgen.sqlite3"

    def mid_job() -> bool:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            got = dict(conn.execute("SELECT status, COUNT(*) FROM items GROUP BY status").fetchall())
        finally:
            conn.close()
        return got.get("succeeded", 0) >= 1 and got.get("processing", 0) >= 1

    assert wait_until(mid_job, timeout=20, step=0.02)
    srv.proc.send_signal(signal.SIGKILL)  # API + embedded workers die together, mid-render
    srv.proc.wait(10)
    srv2 = start_server(tmp_path, **env)  # same tmp_path -> same data dir
    try:
        job = terminal(srv2, body["id"])
        assert job["status"] == "completed" and job["counts"]["succeeded"] == 8
        pdfs = list((srv2.data_dir / "certificates").rglob("*.pdf"))
        assert len(pdfs) == 8
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        attempts = sorted(r[0] for r in conn.execute("SELECT attempts FROM items"))
        conn.close()
        assert max(attempts) >= 2  # the interrupted items were re-run after their leases expired
    finally:
        assert stop_server(srv2) == 0
    assert list((srv2.data_dir / "certificates").rglob("*.tmp")) == []


def test_body_limit_over_real_http(server: Server) -> None:
    limit = Settings().max_body_bytes  # the server runs with default settings
    head = (
        f"POST {JOBS} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
        f"Authorization: Bearer {AUDIT_TOKEN}\r\n"
    )
    with socket.create_connection(("127.0.0.1", server.port), timeout=10) as sock:
        sock.sendall(f"{head}Content-Length: {limit + 1}\r\n\r\n".encode())
        assert b" 413 " in sock.recv(4096).split(b"\r\n", 1)[0]
    with socket.create_connection(("127.0.0.1", server.port), timeout=30) as sock:
        sock.sendall(f"{head}Transfer-Encoding: chunked\r\n\r\n".encode())
        chunk = b"a" * 1_000_000
        status_line = b""
        for _ in range(limit // len(chunk) + 2):
            try:
                sock.sendall(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                break
            sock.settimeout(0.01)
            try:
                status_line = sock.recv(4096).split(b"\r\n", 1)[0]
                break
            except TimeoutError:
                pass
            finally:
                sock.settimeout(30)
        if not status_line:
            status_line = sock.recv(4096).split(b"\r\n", 1)[0]
        assert b" 413 " in status_line


def test_graceful_sigint_exits_zero(tmp_path: Path) -> None:
    srv = start_server(tmp_path)
    srv.json("POST", JOBS, {"certificate": CERT, "recipients": [{"name": "A", "email": "a@example.com"}]})
    assert stop_server(srv) == 0
    assert "Application shutdown complete" in srv.log.read_text()


@pytest.mark.red("F-10")
def test_foreign_host_header_is_rejected(server: Server) -> None:
    """F-10. The API trusts any Host header, so a web page using DNS rebinding (attacker.example ->
    127.0.0.1) becomes same-origin with the loopback API and can submit jobs and read responses.
    Expected: a Host allowlist (localhost/127.0.0.1/::1 + configured names) -> 400. Fails today: 200."""
    status, _, _ = server.request("GET", "/healthz", headers={"Host": "attacker.example:8000"})
    assert status == 400
    status, _, _ = server.request(
        "POST",
        JOBS,
        json.dumps(README_BODY).encode(),
        {"Content-Type": "application/json", "Host": "attacker.example"},
    )
    assert status == 400


@pytest.mark.red("F-11")
def test_idle_connections_do_not_lock_out_clients(server: Server) -> None:
    """F-11. 64 TCP connections that never send a byte reach uvicorn's limit_concurrency=64; every new
    request then gets a plain-text 503. Expected: idle/unauthenticated connections are timed out (or
    not counted) so a legitimate request succeeds within 10 s. Fails today: still 503 after 10 s."""
    socks = [socket.create_connection(("127.0.0.1", server.port)) for _ in range(64)]
    try:
        time.sleep(0.5)
        statuses: list[Any] = []

        def ok() -> bool:
            try:
                statuses.append(server.request("GET", "/healthz", timeout=2)[0])
            except OSError as exc:
                statuses.append(type(exc).__name__)
            return statuses[-1] == 200

        assert wait_until(ok, timeout=10, step=1.0), (
            f"/healthz statuses while 64 idle sockets are open: {statuses}"
        )
    finally:
        for s in socks:
            s.close()


@pytest.mark.red("F-01")
def test_overlong_email_over_http_is_fast_and_does_not_starve_others(server: Server) -> None:
    """F-01 over real HTTP. A 600,000-character email must be rejected within 2 s, and /healthz must stay
    responsive meanwhile. Fails today on the duration (quadratic email-validator syntax pass)."""
    email = "a" * 600_000 + "@example.com"
    body = json.dumps(
        {
            "certificate": CERT,
            "recipients": [{"name": "A", "email": "a@example.com"}, {"name": "B", "email": email}],
        }
    ).encode()
    result: dict[str, Any] = {}

    def submit() -> None:
        started = time.perf_counter()
        result["status"] = server.request(
            "POST", JOBS, body, {"Content-Type": "application/json"}, timeout=120
        )[0]
        result["seconds"] = time.perf_counter() - started

    t = threading.Thread(target=submit)
    t.start()
    latencies = []
    while t.is_alive() and len(latencies) < 600:
        started = time.perf_counter()
        server.request("GET", "/healthz", timeout=30)
        latencies.append(time.perf_counter() - started)
        time.sleep(0.05)
    t.join(120)
    worst = max(latencies) if latencies else 0.0
    print(
        f"\nPOST with 600k-char email: status={result.get('status')} seconds={result.get('seconds', 0):.2f}; "
        f"/healthz samples={len(latencies)} max latency={worst * 1000:.0f} ms"
    )
    assert result["status"] in (202, 422)  # 422: refused as an oversized row since round 2 (P8-N1)
    assert result["seconds"] < 2.0

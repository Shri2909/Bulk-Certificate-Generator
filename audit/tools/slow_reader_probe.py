"""Slow-reader probe (bounded, local): do archive downloads that are never read pin request slots?

    python audit/tools/slow_reader_probe.py <scratch-dir> [recipients=1500] [seconds=60] [reconnect=0]

Starts `python -m certgen` (embedded workers) from the current directory, generates one job, then keeps
64 connections that request its archive and never read; a connection answered quickly (e.g. 503
ARCHIVE_BUSY) is closed and re-requested. With reconnect=1, a reader whose download started more than
31 s ago is replaced by a fresh one (the old socket stays open, still unread): a sustained attacker
holding a valid token. Reports /healthz every 10 s, the server's open file descriptors, and how many
downloads the server aborted (its `slow_client_aborted` log lines).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

TOKEN = "audit-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def call(port: int, method: str, path: str, body: object = None) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={**AUTH, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()
    except OSError:
        return 0, b""


def main() -> None:
    scratch = Path(sys.argv[1])
    recipients = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
    seconds = int(sys.argv[3]) if len(sys.argv) > 3 else 60
    reconnect = len(sys.argv) > 4 and sys.argv[4] == "1"
    scratch.mkdir(parents=True, exist_ok=True)
    data = Path(tempfile.mkdtemp(dir=scratch))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CERTGEN_")}
    env.update(
        CERTGEN_DATA_DIR=str(data / "d"),
        CERTGEN_PORT=str(port),
        CERTGEN_API_TOKENS=TOKEN,
        PYTHONPATH=os.getcwd(),
    )
    env["CERTGEN_MAX_RECIPIENTS"] = str(max(recipients, 10_000))
    log_path = data / "server.log"
    log_file = log_path.open("wb")
    proc = subprocess.Popen([sys.executable, "-m", "certgen"], env=env, stdout=log_file, stderr=log_file)
    try:
        deadline = time.monotonic() + 20
        while call(port, "GET", "/healthz")[0] != 200 and time.monotonic() < deadline:
            time.sleep(0.1)
        rows = [{"name": f"Reader {i}", "email": f"reader{i}@example.com"} for i in range(recipients)]
        _status, raw = call(
            port,
            "POST",
            "/v1/certificate-jobs",
            {"certificate": {"title": "T", "issuer": "I", "issued_on": "2026-10-01"}, "recipients": rows},
        )
        job = json.loads(raw)["id"]
        started = time.monotonic()
        while json.loads(call(port, "GET", f"/v1/certificate-jobs/{job}")[1])["status"] != "completed":
            time.sleep(0.5)
        print(f"job of {recipients} generated in {time.monotonic() - started:.1f} s", flush=True)
        request = (
            f"GET /v1/certificate-jobs/{job}/archive HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Authorization: Bearer {TOKEN}\r\n\r\n"
        ).encode()

        def open_reader() -> socket.socket:
            sk = socket.create_connection(("127.0.0.1", port))
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sk.sendall(request)
            return sk

        def server_fds() -> int:
            return len(os.listdir(f"/proc/{proc.pid}/fd"))

        readers = [open_reader() for _ in range(64)]
        stalled_since: dict[int, float] = {}  # reader index -> when its 200 was first seen
        kept: list[socket.socket] = []  # replaced readers, held open and unread
        end = time.monotonic() + seconds
        next_report = time.monotonic()
        while time.monotonic() < end:
            for i, sk in enumerate(readers):
                sk.setblocking(False)
                try:
                    head = sk.recv(64, socket.MSG_PEEK)
                except BlockingIOError:
                    continue
                except OSError:
                    head = b""
                status_line = head.split(b"\r\n", 1)[0]
                if not head or b" 200 " not in status_line:
                    sk.close()
                    readers[i] = open_reader()  # refused or closed: ask again, never read a body
                    continue
                since = stalled_since.setdefault(i, time.monotonic())
                if reconnect and time.monotonic() - since > 31:
                    kept.append(sk)
                    readers[i] = open_reader()
                    del stalled_since[i]
            if time.monotonic() >= next_report:
                elapsed = seconds - (end - time.monotonic())
                health = call(port, "GET", "/healthz")[0]
                print(f"t={elapsed:5.1f}s /healthz -> {health}  server fds: {server_fds()}", flush=True)
                next_report += 10
            time.sleep(0.2)
        print(f"end: /healthz -> {call(port, 'GET', '/healthz')[0]}  server fds: {server_fds()}", flush=True)
        for sk in readers + kept:
            sk.close()
        time.sleep(1)
        health = call(port, "GET", "/healthz")[0]
        print(f"after closing the readers: /healthz -> {health}  server fds: {server_fds()}", flush=True)
    finally:
        proc.terminate()
        proc.wait(20)
        log_file.close()
    log = log_path.read_text(encoding="utf-8", errors="replace")
    aborted, tracebacks = log.count("slow_client_aborted"), log.count("Traceback")
    print(f"server log: {aborted} slow_client_aborted, {tracebacks} tracebacks")


if __name__ == "__main__":
    main()

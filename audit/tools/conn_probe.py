"""Connection-exhaustion probe (bounded, local only): idle sockets and stalled uploads vs /healthz.

    python audit/tools/conn_probe.py <repo-root-to-run> <scratch-dir>

Starts `python -m certgen` from <repo-root-to-run> on an ephemeral port, then for each scenario opens
64 sockets, waits 1 s, samples /healthz three times, and records what the 64 sockets received.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SOCKETS = 64


def healthz(port: int) -> object:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code
    except OSError as err:
        return type(err).__name__


def answered(sock: socket.socket) -> str:
    sock.setblocking(False)
    try:
        data = sock.recv(64)
    except BlockingIOError:
        return "waiting"
    except OSError as err:
        return type(err).__name__
    return "closed" if data == b"" else data.split(b"\r\n", 1)[0].decode(errors="replace")


def scenario(port: int, name: str, payload: bytes | None, one_by_one: bool = False) -> None:
    """Open SOCKETS connections; send `payload` on each, either after all are open or as each opens."""
    socks = []
    for _ in range(SOCKETS):
        socks.append(socket.create_connection(("127.0.0.1", port)))
        if one_by_one and payload is not None:
            socks[-1].sendall(payload)
            time.sleep(0.01)
    if payload is not None and not one_by_one:
        for s in socks:
            s.sendall(payload)
    time.sleep(1.0)
    samples = [healthz(port) for _ in range(3)]
    states: dict[str, int] = {}
    for s in socks:
        state = answered(s)
        states[state] = states.get(state, 0) + 1
    print(f"{name}: /healthz x3 = {samples}; the {SOCKETS} sockets: {states}", flush=True)
    for s in socks:
        s.close()
    time.sleep(0.5)


def main() -> None:
    root, scratch = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    data = Path(tempfile.mkdtemp(dir=scratch))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CERTGEN_")}
    env.update(CERTGEN_DATA_DIR=str(data / "d"), CERTGEN_PORT=str(port), PYTHONPATH=str(root))
    log = (data / "server.log").open("wb")
    proc = subprocess.Popen([sys.executable, "-m", "certgen"], cwd=root, env=env, stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 20
        while healthz(port) != 200 and time.monotonic() < deadline:
            time.sleep(0.1)
        print(f"server from {root.name}: healthy={healthz(port) == 200}", flush=True)
        scenario(port, "idle sockets (no bytes)", None)
        stalled = (
            b"POST /v1/certificate-jobs HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\nContent-Length: 100000\r\n\r\n{"
        )
        scenario(port, "stalled uploads, all connected first", stalled)
        scenario(port, "stalled uploads, sent as each connects", stalled, one_by_one=True)
        partial = b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        scenario(port, "stalled headers (no blank line)", partial)
    finally:
        proc.terminate()
        proc.wait(20)


if __name__ == "__main__":
    main()

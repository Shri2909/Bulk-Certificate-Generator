"""Phase 8 fresh-eyes attacks against the remediated code (local, synthetic, bounded).

Prints one line per attack.

python audit/tools/phase8.py <scratch-dir>      (run from the repository root; ~3 GB RSS at peak)
"""

from __future__ import annotations

import json
import resource
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from certgen.config import Settings
from certgen.main import create_app

CERT = {"title": "T", "issuer": "I", "issued_on": "2026-10-01"}
OK_ROW = {"name": "A", "email": "a@example.com"}
H = {"Content-Type": "application/json"}


def rss_mb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024


def app_client(scratch: Path, **overrides: Any) -> tuple[TestClient, Settings]:
    settings = Settings(
        data_dir=Path(tempfile.mkdtemp(dir=scratch)) / "d",
        embedded_worker=False,
        allowed_hosts=("testserver",),
        **overrides,
    )
    return TestClient(create_app(settings), raise_server_exceptions=False), settings


def post(
    c: TestClient, body: bytes, path: str = "/v1/certificate-jobs", headers: dict[str, str] | None = None
) -> tuple[int, int, float]:
    started = time.perf_counter()
    r = c.post(path, content=body, headers={**H, **(headers or {})})
    return r.status_code, len(r.content), time.perf_counter() - started


def first_line(sk: socket.socket) -> str:
    return sk.recv(200).split(b"\r\n", 1)[0].decode(errors="replace")


def line(tag: str, text: str) -> None:
    print(f"[{tag}] {text}", flush=True)


def attacks_in_process(scratch: Path) -> None:
    c, s = app_client(scratch)
    with c:
        limit = s.max_body_bytes
        # F-01 bypass: other unbounded fields go through NFC + space collapse before their length check.
        for field in ("name", "title"):
            big = "a " * ((limit - 400) // 2)
            if field == "name":
                body = json.dumps(
                    {"certificate": CERT, "recipients": [OK_ROW, {"name": big, "email": "b@example.com"}]}
                ).encode()
            else:
                body = json.dumps({"certificate": {**CERT, "title": big}, "recipients": [OK_ROW]}).encode()
            st, n, dt = post(c, body)
            line(
                "F-01 bypass",
                f"{len(body) / 1e6:.0f} MB {field} of 'a ' pairs -> {st}, {dt:.2f} s, response {n} B",
            )
        email = "a@" + "b." * 126 + "co"  # 254 chars, maximal dot count
        st, n, dt = post(
            c,
            json.dumps({"certificate": CERT, "recipients": [OK_ROW, {"name": "B", "email": email}]}).encode(),
        )
        line("F-01 bypass", f"254-char email with 127 labels -> {st}, {dt * 1000:.1f} ms")
        quoted = '"' + "a" * 60 + '"@example.com'
        st, n, dt = post(
            c,
            json.dumps(
                {"certificate": CERT, "recipients": [OK_ROW, {"name": "B", "email": quoted}]}
            ).encode(),
        )
        line("F-01 bypass", f"quoted local part -> {st}, {dt * 1000:.1f} ms")

        # F-02 bypass + storage growth: one valid row + one 44 MB unknown key; what is persisted?
        key = "k" * (limit - 300)
        body = (
            '{"certificate":'
            + json.dumps(CERT)
            + ',"recipients":['
            + json.dumps(OK_ROW)
            + ',{"'
            + key
            + '":0}]}'
        ).encode()
        before = rss_mb()
        st, n, dt = post(c, body)
        con = sqlite3.connect(s.db_path)
        stored = (
            con.execute("select sum(length(request_json)) from jobs").fetchone()[0],
            con.execute("select sum(length(input_json)), sum(length(errors)) from items").fetchone(),
        )
        con.close()
        line(
            "F-02 bypass / storage",
            f"{len(body) / 1e6:.0f} MB body, one 44 MB key -> {st}, response {n / 1e6:.1f} MB, {dt:.1f} s, "
            f"RSS {before}->{rss_mb()} MB; "
            f"persisted request_json {stored[0] / 1e6:.1f} MB + input_json {stored[1][0] / 1e6:.1f} MB "
            f"+ errors {stored[1][1] / 1e6:.1f} MB",
        )

        # F-03 bypass: surrogates outside the JSON body (query, path).
        r = c.get("/v1/certificate-jobs/" + "a" * 32 + "/items?status=%ED%A0%80")
        line("F-03 bypass", f"percent-encoded surrogate in a query parameter -> {r.status_code}")
        r = c.get("/v1/certificates/%ED%A0%80" + "a" * 30)
        line("F-03 bypass", f"percent-encoded surrogate in a path id -> {r.status_code}")

        # F-10 bypass: Host header variants through the ASGI app.
        for host in (
            "127.0.0.1:",
            "127.1",
            "localhost:8000:80",
            "[::ffff:127.0.0.1]",
            "LOCALHOST.",
            "localhost%00",
            "xn--localhost",
        ):
            line("F-10 bypass", f"Host {host!r} -> {c.get('/healthz', headers={'Host': host}).status_code}")

        # F-05 bypass: a BMP character whose NFC form is fine but whose NFD contains a non-BMP code point?
        bad = [
            ch
            for ch in map(chr, range(0x10000))
            if any(ord(x) > 0xFFFF for x in unicodedata.normalize("NFD", ch))
        ]
        line("F-05 bypass", f"BMP characters decomposing to non-BMP code points: {len(bad)}")

    # F-09 consequence: memory for a maximal valid request at the new limit (10,000 longest valid rows).
    c, s = app_client(scratch)
    with c:
        name = ("Αλέξανδρος " * 11)[:120].strip()
        rows = [
            {
                "name": name,
                "email": f"x{i:05d}" + "a" * 58 + "@" + ".".join(["b" * 63, "c" * 63, "d" * 57]) + ".org",
            }
            for i in range(10_000)
        ]
        body = json.dumps({"certificate": CERT, "recipients": rows}).encode()
        before = rss_mb()
        st, n, dt = post(c, body)
        line(
            "F-09 consequence",
            f"10,000 maximal valid rows ({len(body) / 1e6:.1f} MB, ascii-escaped) -> {st} in {dt:.1f} s, "
            f"RSS {before}->{rss_mb()} MB",
        )


def attacks_real_server(scratch: Path) -> None:
    data = Path(tempfile.mkdtemp(dir=scratch))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        "PATH": "/usr/bin:/bin",
        "CERTGEN_DATA_DIR": str(data / "d"),
        "CERTGEN_PORT": str(port),
        "PYTHONPATH": str(Path.cwd()),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "certgen"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    def get(host: str = "127.0.0.1") -> str:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sk:
            sk.sendall(f"GET /healthz HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            return sk.recv(200).split(b"\r\n", 1)[0].decode(errors="replace")

    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if "200" in get():
                    break
            except OSError:
                time.sleep(0.1)
        # F-10: duplicate Host headers, and an absolute-form request target naming another host.
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sk:
            sk.sendall(
                b"GET /healthz HTTP/1.1\r\nHost: attacker.example\r\n"
                b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            line("F-10 bypass", f"two Host headers (foreign first) -> {first_line(sk)}")
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sk:
            sk.sendall(
                b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Host: attacker.example\r\nConnection: close\r\n\r\n"
            )
            line("F-10 bypass", f"two Host headers (loopback first) -> {first_line(sk)}")
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sk:
            sk.sendall(
                b"GET http://attacker.example/healthz HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            line(
                "F-10 bypass", f"absolute-form target naming another host, Host loopback -> {first_line(sk)}"
            )
        # F-11: slots of stalled uploads are released when those clients disconnect.
        socks = []
        for _ in range(64):
            sk = socket.create_connection(("127.0.0.1", port))
            sk.sendall(
                b"POST /v1/certificate-jobs HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\nContent-Length: 100000\r\n\r\n{"
            )
            socks.append(sk)
            time.sleep(0.01)
        time.sleep(0.5)
        during = get()
        for sk in socks:
            sk.close()
        time.sleep(1.0)
        line(
            "F-11 residual",
            f"64 stalled uploads -> /healthz {during!r}; after those clients disconnect -> {get()!r}",
        )
        # F-11: slow *readers* (download a ZIP but never read the response) hold slots too?
        rows = [{"name": f"R {i}", "email": f"r{i}@example.com"} for i in range(20)]
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sk:
            body = json.dumps({"certificate": CERT, "recipients": rows}).encode()
            sk.sendall(
                b"POST /v1/certificate-jobs HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            job = json.loads(sk.recv(65536).split(b"\r\n\r\n", 1)[1])["id"]
        time.sleep(3)  # embedded workers finish 20 certificates
        readers = []
        for _ in range(64):
            sk = socket.create_connection(("127.0.0.1", port))
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            sk.sendall(f"GET /v1/certificate-jobs/{job}/archive HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
            readers.append(sk)
            time.sleep(0.02)
        time.sleep(1.0)
        line("F-11 residual", f"64 archive downloads that never read -> /healthz {get()!r}")
        for sk in readers:
            sk.close()
    finally:
        proc.terminate()
        proc.wait(20)


def attacks_import() -> None:
    done = subprocess.run(
        [sys.executable, "-c", "from certgen.main import app; print(type(app).__name__)"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    line("F-14", f"'from certgen.main import app' -> {done.stdout.strip() or done.stderr.strip()[-120:]}")


if __name__ == "__main__":
    scratch = Path(sys.argv[1])
    scratch.mkdir(parents=True, exist_ok=True)
    attacks_import()
    attacks_in_process(scratch)
    attacks_real_server(scratch)

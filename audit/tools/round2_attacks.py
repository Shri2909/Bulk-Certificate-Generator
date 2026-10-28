"""Round-2 fresh-eyes attacks on the round-2 fixes (local, synthetic, bounded; ~2 minutes, ~1 GB RSS).

    python audit/tools/round2_attacks.py <scratch-dir>      (run from the repository root)

Real server (`python -m certgen`, no embedded worker so the queue stays as filled): F-12 token checks,
P8-N1 oversized row, P8-N2 concurrent maximal dry runs vs /healthz, full queue, trickled body.
In process: P8-N1 free-disk guard. Startup: F-13 slow-render delay at the lease. Prints one line per attack.
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

TOKEN = "audit-token-0123456789abcdef"
OTHER_TOKEN = "audit-other-token-0123456789"
CERT = {"title": "T", "issuer": "I", "issued_on": "2026-10-01"}
JOBS = "/v1/certificate-jobs"


def line(tag: str, text: str) -> None:
    print(f"[{tag}] {text}", flush=True)


def maximal_rows(n: int) -> list[dict[str, str]]:
    name = ("Αλέξανδρος " * 11)[:120].strip()
    domain = ".".join(["b" * 63, "c" * 63, "d" * 57]) + ".org"
    return [{"name": name, "email": f"x{i:05d}" + "a" * 58 + "@" + domain} for i in range(n)]


class Server:
    def __init__(self, scratch: Path, **env: str) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.data = Path(tempfile.mkdtemp(dir=scratch)) / "d"
        full_env = {k: v for k, v in os.environ.items() if not k.startswith("CERTGEN_")}
        full_env.update(
            CERTGEN_DATA_DIR=str(self.data),
            CERTGEN_PORT=str(self.port),
            CERTGEN_API_TOKENS=f"{TOKEN},{OTHER_TOKEN}",
            CERTGEN_EMBEDDED_WORKER="false",
            CERTGEN_MAX_ACTIVE_ITEMS="10000",
            PYTHONPATH=os.getcwd(),
            **env,
        )
        self.log = (self.data.parent / "server.log").open("wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "certgen"], env=full_env, stdout=self.log, stderr=self.log
        )
        deadline = time.monotonic() + 20
        while self.call("GET", "/healthz", token=None)[0] != 200:
            if time.monotonic() > deadline:
                raise SystemExit("server did not start")
            time.sleep(0.1)

    def call(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        token: str | None = TOKEN,
        headers: dict[str, str] | None = None,
        timeout: float = 120,
    ) -> tuple[int, dict[str, str], bytes]:
        h = {"Content-Type": "application/json", **(headers or {})}
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method=method, headers=h
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()
        except OSError as err:
            return 0, {}, repr(err).encode()

    def raw(self, data: bytes, wait: float) -> tuple[str, float]:
        """Send raw bytes, return the status line and seconds until it arrived."""
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", self.port), timeout=wait) as sk:
            sk.sendall(data)
            try:
                head = sk.recv(200)
            except TimeoutError:
                head = b"(no response)"
        return head.split(b"\r\n", 1)[0].decode("latin-1"), time.monotonic() - started

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(20)
        self.log.close()


def code(raw: bytes) -> str:
    try:
        return str(json.loads(raw).get("code"))
    except ValueError:
        return raw[:60].decode("latin-1")


def attacks_auth(srv: Server) -> None:
    job = "/v1/certificate-jobs/" + "0" * 32
    st, h, raw = srv.call("GET", job, token=None)
    line("F-12", f"no Authorization -> {st} {code(raw)}, WWW-Authenticate {h.get('www-authenticate')!r}")
    line("F-12", f"wrong token -> {srv.call('GET', job, token='audit-wrong-token-0123456789')[0]}")
    line(
        "F-12",
        f"token as ?access_token= query -> {srv.call('GET', f'{job}?access_token={TOKEN}', token=None)[0]}",
    )
    for header in (f"bearer {TOKEN}", f"Bearer  {TOKEN} ", f"Basic {TOKEN}", f"Bearer {TOKEN}x", "Bearer "):
        st = srv.call("GET", job, token=None, headers={"Authorization": header})[0]
        line("F-12", f"Authorization {header[:14]!r}... -> {st} (404 = authorized, no such job)")
    for path in ("/healthz", "/openapi.json", "/docs"):
        line("F-12", f"{path} without token -> {srv.call('GET', path, token=None)[0]}")
    status, took = srv.raw(
        f"POST {JOBS} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
        "Content-Length: 40000000\r\n\r\n".encode(),
        wait=10,
    )
    line(
        "F-12",
        f"unauthenticated 40 MB POST, body never sent -> {status!r} after {took:.2f} s (before any body)",
    )
    body = json.dumps({"certificate": CERT, "recipients": [{"name": "A", "email": "a@example.com"}]}).encode()
    other = json.dumps(
        {"certificate": CERT, "recipients": [{"name": "B", "email": "b@example.com"}]}
    ).encode()
    a = srv.call("POST", JOBS, body, headers={"Idempotency-Key": "shared-key"})
    b = srv.call("POST", JOBS, other, token=OTHER_TOKEN, headers={"Idempotency-Key": "shared-key"})
    a2 = srv.call("POST", JOBS, other, headers={"Idempotency-Key": "shared-key"})
    line(
        "F-12",
        f"same Idempotency-Key, token A then token B (other body) -> {a[0]}, {b[0]} (no 409: no oracle"
        f" across tokens); token A reusing it with B's body -> {a2[0]} {code(a2[2])}",
    )


def db_bytes(srv: Server) -> int:
    paths = [srv.data / "certgen.sqlite3", srv.data / "certgen.sqlite3-wal"]
    return sum(p.stat().st_size for p in paths if p.exists())


def attacks_rows(srv: Server) -> None:
    before = db_bytes(srv)
    key = "k" * 44_000_000
    body = (
        '{"certificate":'
        + json.dumps(CERT)
        + ',"recipients":[{"name":"A","email":"a@example.com"},{"'
        + key
        + '":0}]}'
    ).encode()
    started = time.monotonic()
    st, _, raw = srv.call("POST", JOBS, body)
    after = db_bytes(srv)
    line(
        "P8-N1",
        f"one row holding a 44 MB key -> {st} {code(raw)} in {time.monotonic() - started:.2f} s, response"
        f" {len(raw)} B; DB+WAL grew {(after - before) / 1e6:.2f} MB",
    )


def attacks_cpu(srv: Server) -> None:
    body = json.dumps({"certificate": CERT, "recipients": maximal_rows(10_000)}).encode()
    started = time.monotonic()
    st, _, _ = srv.call("POST", f"{JOBS}?dry_run=true", body)
    line(
        "P8-N2",
        f"one maximal dry run ({len(body) / 1e6:.1f} MB) -> {st} in {time.monotonic() - started:.2f} s",
    )

    results: list[str] = []
    health: list[float] = []
    done = threading.Event()

    def submit() -> None:
        st, _, raw = srv.call("POST", f"{JOBS}?dry_run=true", body)
        results.append(f"{st} {code(raw)}" if st != 200 else "200")

    def poll() -> None:
        while not done.is_set():
            started = time.monotonic()
            st = srv.call("GET", "/healthz", token=None, timeout=30)[0]
            health.append(time.monotonic() - started if st == 200 else float("inf"))
            time.sleep(0.25)

    poller = threading.Thread(target=poll)
    poller.start()
    started = time.monotonic()
    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    done.set()
    poller.join()
    ok = [x for x in health if x != float("inf")]
    line(
        "P8-N2",
        f"8 concurrent maximal dry runs -> {dict(collections.Counter(results))} in"
        f" {time.monotonic() - started:.1f} s; /healthz during: {len(health)} polls, max"
        f" {max(ok, default=0) * 1000:.0f} ms, non-200 {len(health) - len(ok)}",
    )

    busy = [threading.Thread(target=submit) for _ in range(2)]
    for t in busy:
        t.start()
    time.sleep(1.5)  # both submissions are now being validated
    status, took = srv.raw(
        f"POST {JOBS}?dry_run=true HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {TOKEN}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        "Expect: 100-continue\r\n\r\n".encode(),
        wait=10,
    )
    for t in busy:
        t.join()
    line(
        "P8-N2",
        f"third submission sent with 'Expect: 100-continue' while 2 run -> {status!r} after {took:.2f} s"
        " (body never sent)",
    )

    fill = json.dumps(
        {
            "certificate": CERT,
            "recipients": [{"name": f"R {i}", "email": f"r{i}@example.com"} for i in range(10_000)],
        }
    ).encode()
    st = srv.call("POST", JOBS, fill)[0]
    started = time.monotonic()
    st2, _, raw = srv.call("POST", JOBS, body)
    line(
        "P8-N2",
        f"queue filled with 10,000 items ({st}); maximal real request -> {st2} {code(raw)} after"
        f" {time.monotonic() - started:.2f} s (baseline: 7.24 s of validation first)",
    )

    head = (
        f"POST {JOBS}?dry_run=true HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {TOKEN}\r\n"
        "Content-Type: application/json\r\nContent-Length: 100000\r\n\r\n"
    ).encode()
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", srv.port), timeout=60) as sk:
        sk.sendall(head)
        sk.setblocking(False)
        status = b""
        while time.monotonic() - started < 45 and not status:
            with contextlib.suppress(OSError):
                sk.send(b"{")  # one byte per second, never finishing
            time.sleep(1)
            try:
                status = sk.recv(200)
            except BlockingIOError:
                continue
    status_line = status.split(b"\r\n", 1)[0].decode("latin-1")
    line("P8-N2", f"body trickled at 1 B/s -> {status_line!r} after {time.monotonic() - started:.1f} s")


def attacks_in_process(scratch: Path) -> None:
    sys.path.insert(0, os.getcwd())  # the certgen under test: the repository root this runs from
    from fastapi.testclient import TestClient

    from certgen.config import Settings
    from certgen.main import create_app

    settings = Settings(
        data_dir=Path(tempfile.mkdtemp(dir=scratch)) / "d",
        embedded_worker=False,
        allowed_hosts=("testserver",),
        api_tokens=(TOKEN,),
        min_free_disk_mb=1_000_000,  # 1 TB: more than this disk has
    )
    body = json.dumps({"certificate": CERT, "recipients": [{"name": "A", "email": "a@example.com"}]})
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {TOKEN}"}) as c:
        real = c.post(JOBS, content=body, headers={"Content-Type": "application/json"})
        dry = c.post(f"{JOBS}?dry_run=true", content=body, headers={"Content-Type": "application/json"})
    line(
        "P8-N1",
        f"CERTGEN_MIN_FREE_DISK_MB above the free space -> POST {real.status_code} {real.json().get('code')},"
        f" dry run {dry.status_code}",
    )


def attacks_startup(scratch: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CERTGEN_")}
    env.update(
        CERTGEN_DATA_DIR=str(Path(tempfile.mkdtemp(dir=scratch)) / "d"),
        CERTGEN_TEST_SLOW_RENDER_MS="30000",
        CERTGEN_API_TOKENS=TOKEN,
        PYTHONPATH=os.getcwd(),
    )
    proc = subprocess.run(
        [sys.executable, "-m", "certgen"], env=env, capture_output=True, text=True, timeout=30, check=False
    )
    last = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
    line(
        "F-13", f"CERTGEN_TEST_SLOW_RENDER_MS=30000 with a 30 s lease -> exit {proc.returncode}: {last[:150]}"
    )


def main() -> None:
    scratch = Path(sys.argv[1])
    scratch.mkdir(parents=True, exist_ok=True)
    logs = []
    for attacks in ((attacks_auth, attacks_rows), (attacks_cpu,)):  # a fresh server (empty queue) each
        srv = Server(scratch)
        try:
            for attack in attacks:
                attack(srv)
        finally:
            srv.stop()
        logs.append((srv.data.parent / "server.log").read_text(encoding="utf-8", errors="replace"))
    log = "\n".join(logs)
    leaked = [x for x in log.splitlines() if TOKEN in x and "?access_token=" not in x]
    line(
        "server",
        f"logs: {log.count('Traceback')} tracebacks; token in {len(leaked)} lines besides the access-log line"
        " of the ?access_token= attack URL itself",
    )
    attacks_in_process(scratch)
    attacks_startup(scratch)


if __name__ == "__main__":
    main()

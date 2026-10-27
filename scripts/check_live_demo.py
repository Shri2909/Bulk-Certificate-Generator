"""Exercise the demo over real HTTP, including real process death and recovery.

Run: .venv/bin/python scripts/check_live_demo.py
Uses a temporary data directory and stops its server and worker on completion.
"""

from __future__ import annotations

import csv
import io
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def check(condition: object, detail: object = "check failed") -> None:
    """An explicit check: unlike `assert`, it still runs under `python -O`."""
    if not condition:
        raise AssertionError(detail)


def main() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    def request(
        path: str, body: Any = None, *, post: bool = False, headers: dict[str, str] | None = None
    ) -> tuple[int, Any, bytes]:
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else b"" if post else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            response = urllib.request.urlopen(req, timeout=12)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.headers, response.read()

    def get(path: str) -> Any:
        status, _, raw = request(path)
        check(status == 200, (status, raw))
        return json.loads(raw)

    def control(action: str) -> Any:
        status, _, raw = request(f"/demo/worker/{action}", post=True, headers={"X-Demo-Control": "local"})
        check(status == 200, (status, raw))
        return json.loads(raw)

    jobs = "/api/v1/certificate-jobs"

    def payload(recipients: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "certificate": {
                "title": "Live Demo Verification",
                "issuer": "Acme Academy",
                "issued_on": datetime.now(UTC).date().isoformat(),
            },
            "recipients": recipients,
        }

    def submit(body: Any, key: str) -> str:
        status, _, raw = request(jobs, body, headers={"Idempotency-Key": key})
        check(status == 202, (status, raw))
        return str(json.loads(raw)["id"])

    def finish(job_id: str) -> tuple[Any, list[Any]]:
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            job = get(f"{jobs}/{job_id}")
            if job["status"] in {"completed", "completed_with_errors", "failed"}:
                return job, get(f"{jobs}/{job_id}/items?limit=1000")["items"]
            time.sleep(0.1)
        raise AssertionError(f"Job {job_id} did not finish within 35 seconds")

    with (
        tempfile.TemporaryDirectory(prefix="certgen-demo-check-") as tmp,
        (Path(tmp) / "server.log").open("w+") as log,
    ):
        server = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts/live_demo.py"),
                "--no-browser",
                "--port",
                str(port),
                "--data-dir",
                str(Path(tmp) / "data"),
            ],
            cwd=ROOT,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 12
            while True:
                try:
                    initial = get("/demo/state")
                    break
                except (OSError, AssertionError):
                    if server.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise AssertionError(f"Server failed to start:\n{log.read()}") from None
                    time.sleep(0.1)
            check(initial["jobs"] == [] and not initial["worker_running"])
            for path in ["/", "/app.js", "/style.css", "/favicon.svg", "/api/docs"]:
                check(request(path)[0] == 200, path)
            check(request("/demo/worker/start", post=True)[0] == 403)
            check(
                request(
                    "/demo/worker/start",
                    post=True,
                    headers={"X-Demo-Control": "local", "Origin": "https://unrelated.example"},
                )[0]
                == 403
            )
            print("PASS: assets, API docs, isolated empty session, same-origin controls", flush=True)

            body = payload(
                [
                    {"name": "Zoë Łukasz", "email": "zoe@example.com"},
                    {"name": "Priya Raman", "email": "priya@example.org"},
                    {"name": "Bob", "email": "invalid"},
                    {"name": "Duplicate", "email": " ZOE@example.com "},
                ]
            )
            status, _, raw = request(jobs + "?dry_run=true", body)
            check(status == 200 and json.loads(raw)["counts"] == {"total": 4, "valid": 2, "rejected": 2})
            check(get("/demo/state")["jobs"] == [])
            job_id = submit(body, "smoke-original")
            check(get(f"{jobs}/{job_id}")["counts"]["pending"] == 2)
            control("start")
            job, rows = finish(job_id)
            check(job["counts"]["succeeded"] == 2 and job["counts"]["rejected"] == 2)
            cert_id = next(row["certificate_id"] for row in rows if row["status"] == "succeeded")
            status, _, raw = request(f"/api/v1/certificates/{cert_id}")
            check(status == 200 and raw.startswith(b"%PDF"))
            status, _, raw = request(f"{jobs}/{job_id}/archive")
            check(status == 200)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                check(len([name for name in archive.namelist() if name.endswith(".pdf")]) == 2)
                manifest = list(csv.DictReader(io.StringIO(archive.read("manifest.csv").decode())))
                check(len(manifest) == 4)
            status, headers, raw = request(jobs, body, headers={"Idempotency-Key": "smoke-original"})
            check(status == 200 and headers["Idempotent-Replayed"] == "true")
            check(json.loads(raw)["id"] == job_id and len(get("/demo/state")["jobs"]) == 1)
            print("PASS: dry run, partial acceptance, PDF, ZIP + manifest, safe replay", flush=True)

            control("stop")
            crash_id = submit(
                payload(
                    [{"name": f"Recovery Person {i}", "email": f"recovery{i}@example.org"} for i in range(6)]
                ),
                "smoke-crash",
            )
            crashed = control("crash")
            check(not crashed["worker_running"])
            check(get(f"{jobs}/{crash_id}")["counts"]["processing"] == 1)
            check(len(crashed["leases"]) == 1)
            control("start")
            job, rows = finish(crash_id)
            check(job["counts"]["succeeded"] == 6)
            recovered = next(row for row in rows if row["certificate_id"] == crashed["crashed_certificate"])
            check(recovered["attempts"] == 2)
            files = list((Path(tmp) / "data" / "certificates" / crash_id).glob("*.pdf"))
            check(len(files) == 6)
            print("PASS: real worker kill, lease recovery, same ID, exactly six PDFs", flush=True)

            control("stop")
            failure_id = submit(
                payload(
                    [
                        {"name": "Grace Hopper", "email": "grace@example.org"},
                        {"name": "Alan Turing", "email": "alan@example.org"},
                        {"name": "Katherine Johnson", "email": "katherine@example.org"},
                        {"name": "Barbara Liskov", "email": "barbara@example.org"},
                    ]
                ),
                "smoke-failure",
            )
            control("failure")
            job, rows = finish(failure_id)
            check(job["counts"]["failed"] == 1 and job["counts"]["succeeded"] == 3)
            failed = next(row for row in rows if row["status"] == "failed")
            check(failed["attempts"] == 3 and failed["email"] == "grace@example.org")
            ids = [row["certificate_id"] for row in rows]
            control("start")
            check(request(f"{jobs}/{failure_id}/retry", post=True)[0] == 202)
            job, rows = finish(failure_id)
            check(job["counts"]["succeeded"] == 4)
            check([row["certificate_id"] for row in rows] == ids)
            check(all(row["attempts"] == 1 for row in rows))
            print(
                "PASS: isolated failure, attempt limit, explicit retry, stable certificate IDs",
                flush=True,
            )
            control("stop")
            check(not get("/demo/state")["worker_running"])
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
    print("PASS: demo end-to-end checks complete; temporary data and owned processes cleaned up")


if __name__ == "__main__":
    main()

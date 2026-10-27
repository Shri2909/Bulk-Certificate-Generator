"""Local presentation server. Run with .venv/bin/python scripts/live_demo.py.

The production API is mounted unchanged at /api. Only this launcher exposes demo
controls. Workers use the real queue, renderer, storage and recovery logic.
"""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from sqlalchemy import text  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

from certgen.clock import system_clock  # noqa: E402
from certgen.config import Settings  # noqa: E402
from certgen.db import create_engines, create_schema  # noqa: E402
from certgen.main import create_app  # noqa: E402
from certgen.rendering import CertificateData, render_certificate  # noqa: E402
from certgen.storage import Storage, prepare_data_dir  # noqa: E402
from certgen.worker import Worker  # noqa: E402

LEASE_SECONDS = 8
RENDER_SECONDS = 0.65
FAIL_EMAIL = "grace@example.org"


def demo_settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        embedded_worker=False,
        lease_seconds=LEASE_SECONDS,
        retry_delay_seconds=0.5,
        poll_interval_seconds=0.1,
    )


def run_worker(data_dir: Path, mode: str) -> None:
    """Fault injection stays in the demo; certgen itself is never patched."""
    settings = demo_settings(data_dir)
    prepare_data_dir(settings.data_dir, settings.certificates_dir)
    engines = create_engines(settings.db_path)
    create_schema(engines)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def render(data: CertificateData) -> bytes:
        if mode == "crash":
            # Tell the controller a real claim has committed, then keep this
            # render in flight until it kills this exact child process.
            marker = data_dir / "claimed.tmp"
            marker.write_text(data.certificate_id)
            marker.replace(data_dir / "claimed")
            if not stop.wait(30):
                raise TimeoutError("Demo controller did not terminate the armed worker")
        time.sleep(RENDER_SECONDS)
        if mode == "failure" and data.email == FAIL_EMAIL:
            raise OSError("Intentional demo-only rendering failure")
        return render_certificate(data)

    try:
        Worker(engines, Storage(settings.certificates_dir), settings, system_clock, render).run_forever(stop)
    finally:
        engines.dispose()


class DemoController:
    def __init__(self, data_dir: Path, api: FastAPI) -> None:
        self.data_dir = data_dir
        self.api = api
        self.lock = threading.RLock()
        self.worker: subprocess.Popen[bytes] | None = None
        self.mode = "normal"
        self.crashed_certificate: str | None = None

    def stop(self, *, crash: bool = False) -> None:
        with self.lock:
            if self.worker is not None and self.worker.poll() is None:
                if crash:
                    self.worker.kill()
                else:
                    self.worker.terminate()
                try:
                    self.worker.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.worker.kill()
                    self.worker.wait(timeout=3)

    def start(self, mode: str = "normal") -> None:
        with self.lock:
            self.stop()
            self.mode = mode
            with (self.data_dir / "worker.log").open("ab") as log:
                self.worker = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        mode,
                        "--data-dir",
                        str(self.data_dir),
                    ],
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                )

    def state(self) -> dict[str, Any]:
        with self.lock, self.api.state.engines.read.connect() as conn:
            leases = (
                conn.execute(
                    text("SELECT certificate_id, lease_expires_at FROM items WHERE status = 'processing'")
                )
                .mappings()
                .all()
            )
            jobs = conn.execute(text("SELECT id FROM jobs ORDER BY created_at DESC")).scalars().all()
            return {
                "worker_running": self.worker is not None and self.worker.poll() is None,
                "worker_pid": self.worker.pid if self.worker and self.worker.poll() is None else None,
                "mode": self.mode,
                "lease_seconds": LEASE_SECONDS,
                "render_delay_ms": int(RENDER_SECONDS * 1000),
                "session": self.data_dir.name,
                "jobs": list(jobs),
                "server_time_ms": system_clock(),
                "leases": [dict(row) for row in leases],
                "crashed_certificate": self.crashed_certificate,
            }

    def crash(self) -> dict[str, Any]:
        with self.lock:
            with self.api.state.engines.read.connect() as conn:
                pending = conn.execute(text("SELECT count(*) FROM items WHERE status = 'pending'")).scalar()
            if not pending:
                raise HTTPException(409, "Submit a queued batch before demonstrating a crash.")
            marker = self.data_dir / "claimed"
            marker.unlink(missing_ok=True)
            self.start("crash")
            deadline = time.monotonic() + 6
            while not marker.exists():
                if self.worker is None or self.worker.poll() is not None or time.monotonic() > deadline:
                    self.stop(crash=True)
                    raise HTTPException(503, "Worker did not claim in time. See the session's worker.log.")
                time.sleep(0.025)
            self.stop(crash=True)
            self.crashed_certificate = marker.read_text()
            return self.state()


def build_demo(data_dir: Path) -> FastAPI:
    api = create_app(demo_settings(data_dir))
    controller = DemoController(data_dir, api)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with api.router.lifespan_context(api):
            try:
                yield
            finally:
                controller.stop()

    app = FastAPI(title="certgen live demo", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.middleware("http")
    async def local_controls(request: Request, call_next: Any) -> Any:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if (origin and origin != f"{request.url.scheme}://{request.headers.get('host')}") or (
                request.url.path.startswith("/demo/") and request.headers.get("x-demo-control") != "local"
            ):
                return JSONResponse(
                    {"detail": "Demo controls require a same-origin request."}, status_code=403
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/demo/state")
    def state() -> dict[str, Any]:
        return controller.state()

    @app.post("/demo/worker/{action}")
    def worker_action(action: str) -> dict[str, Any]:
        with controller.lock:
            if action == "start":
                controller.start()
            elif action == "failure":
                controller.start("failure")
            elif action == "stop":
                controller.stop()
            elif action == "crash":
                return controller.crash()
            else:
                raise HTTPException(404, "Unknown demo action")
            return controller.state()

    app.mount("/api", api)
    app.mount("/", StaticFiles(directory=ROOT / "demo", html=True), name="presentation")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--no-browser", action="store_true", help="Print the URL without opening a browser")
    parser.add_argument("--worker", choices=["normal", "crash", "failure"], help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if args.worker:
        if not args.data_dir:
            parser.error("--worker requires --data-dir")
        run_worker(args.data_dir, args.worker)
        return
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    session = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    data_dir = args.data_dir or ROOT / "data" / "live-demo" / session
    url = f"http://127.0.0.1:{args.port}"
    print(f"\n  certgen · live demo\n  Open {url}\n  Session: {data_dir}\n  Ctrl+C to stop.\n", flush=True)
    if not args.no_browser:
        import webbrowser

        timer = threading.Timer(1.5, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    uvicorn.run(build_demo(data_dir), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

"""Background worker: claim one item, render outside any transaction, publish with a fenced update (§7).

Guarantee: at-least-once execution with an idempotent effect. A crashed worker's item is reclaimed
after its lease expires and rendered again; the same item always yields the same file at the same
path, and a per-claim token ensures only the current claimant can record the outcome.
"""

from __future__ import annotations

import collections
import logging
import secrets
import signal
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FrameType

from sqlalchemy import text
from sqlalchemy.engine import Connection

from certgen.clock import Clock, system_clock
from certgen.config import Settings
from certgen.db import Engines, check_sqlite_version, create_engines, create_schema
from certgen.errors import FieldCode, FieldError
from certgen.rendering import CertificateData, render_certificate
from certgen.service import dump_errors
from certgen.storage import Storage, prepare_data_dir

log = logging.getLogger("certgen.worker")

Renderer = Callable[[CertificateData], bytes]

MAX_PDF_BYTES = 2_097_152  # §24.3: bounds worker memory, disk use and download size
MAX_ERROR_MESSAGE = 500
WORKER_LOST_JSON = dump_errors(
    [
        FieldError(
            None, FieldCode.WORKER_LOST, "The worker processing this certificate stopped."
        ).as_item_error()
    ]
)

# T8: a lease expired on the last allowed attempt. Repeated crashes on one item end here.
RETIRE_SQL = (
    "UPDATE items SET status = 'failed', errors = :worker_lost, lease_owner = NULL, lease_token = NULL, "
    "lease_expires_at = NULL, completed_at = :now "
    "WHERE status = 'processing' AND lease_expires_at < :now AND attempts >= :max_attempts"
)
RECLAIM_SQL = (
    "SELECT id FROM items WHERE status = 'processing' AND lease_expires_at < :now "
    "AND attempts < :max_attempts ORDER BY lease_expires_at LIMIT 1"
)
CLAIM_PENDING_SQL = (
    "SELECT id FROM items WHERE status = 'pending' AND (available_at IS NULL OR available_at <= :now) "
    "ORDER BY id LIMIT 1"
)
CLAIM_SQL = (
    "UPDATE items SET status = 'processing', lease_owner = :worker_id, lease_token = :token, "
    "lease_expires_at = :now + :lease_ms, attempts = attempts + 1 "
    "WHERE id = :id RETURNING id, attempts"
)
# T5/T6/T7 share one fence: the claim's token. A reclaim (T4) or retirement (T8) replaces or clears
# the token, so a worker that lost its lease matches 0 rows and changes nothing.
SUCCEED_SQL = (
    "UPDATE items SET status = 'succeeded', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, "
    "errors = NULL, available_at = NULL, completed_at = :now "
    "WHERE id = :id AND lease_token = :token AND status = 'processing'"
)
RETRY_LATER_SQL = (
    "UPDATE items SET status = 'pending', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, "
    "available_at = :available_at, errors = :errors "
    "WHERE id = :id AND lease_token = :token AND status = 'processing'"
)
FAIL_SQL = (
    "UPDATE items SET status = 'failed', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, "
    "errors = :errors, completed_at = :now "
    "WHERE id = :id AND lease_token = :token AND status = 'processing'"
)


@dataclass(frozen=True)
class Claim:
    item_id: int
    token: str
    attempts: int


def retire_exhausted(conn: Connection, now: int, max_attempts: int) -> int:
    """Run T8 inside the caller's write transaction; returns the number of items retired."""
    result = conn.execute(
        text(RETIRE_SQL), {"worker_lost": WORKER_LOST_JSON, "now": now, "max_attempts": max_attempts}
    )
    if result.rowcount:
        log.warning("worker_lost_retired count=%s", result.rowcount)
    return int(result.rowcount)


def with_slow_render(renderer: Renderer, delay_ms: int) -> Renderer:
    """Test-only (CERTGEN_TEST_SLOW_RENDER_MS): widens the render window so a kill lands mid-item."""
    if delay_ms <= 0:
        return renderer

    def slow(data: CertificateData) -> bytes:
        time.sleep(delay_ms / 1000)
        return renderer(data)

    return slow


class Worker:
    def __init__(
        self,
        engines: Engines,
        storage: Storage,
        settings: Settings,
        clock: Clock,
        renderer: Renderer,
        worker_id: str | None = None,
    ) -> None:
        self.engines = engines
        self.storage = storage
        self.settings = settings
        self.clock = clock
        self.renderer = renderer
        self.worker_id = worker_id or uuid.uuid4().hex  # logs only; never part of a guard

    def run_forever(self, stop: threading.Event) -> None:
        self.storage.sweep_orphans(older_than_ms=2 * self.settings.lease_ms, now_ms=self.clock())
        while not stop.is_set():
            try:
                did_work = self.run_once() is not None
            except Exception:  # incl. "database is locked" past busy_timeout: log, back off, go on
                log.exception("worker_iteration_failed worker_id=%s", self.worker_id)
                did_work = False
            if not did_work:
                stop.wait(self.settings.poll_interval_seconds)

    def run_until_idle(self) -> list[int]:
        """Process until nothing is claimable. Used by tests and the benchmark: no sleeping."""
        processed = []
        while (item_id := self.run_once()) is not None:
            processed.append(item_id)
        return processed

    def run_once(self) -> int | None:
        claim = self.claim_next()
        if claim is None:
            return None
        self.process(claim)
        return claim.item_id

    def process(self, claim: Claim) -> None:
        data = self.load_certificate_data(claim.item_id)
        try:
            pdf = self.renderer(data)  # no DB transaction is open here
            if len(pdf) > MAX_PDF_BYTES:
                raise ValueError(f"rendered PDF is {len(pdf)} bytes; limit is {MAX_PDF_BYTES}")
            self.storage.save_atomic(data.job_id, data.certificate_id, pdf)
        except Exception as exc:  # per item: one bad certificate never stops the others (R9)
            self.record_failure(claim, exc)
            return
        self.record_success(claim)

    def claim_next(self) -> Claim | None:
        """One BEGIN IMMEDIATE transaction: retire, pick, claim. Atomic across threads and processes."""
        with self.engines.write.begin() as conn:
            now = self.clock()  # read after the lock is held, so every claimant sees a later time
            max_attempts = self.settings.max_attempts
            retire_exhausted(conn, now, max_attempts)
            params = {"now": now, "max_attempts": max_attempts}
            item_id = conn.execute(text(RECLAIM_SQL), params).scalar()
            if item_id is None:
                item_id = conn.execute(text(CLAIM_PENDING_SQL), params).scalar()
            if item_id is None:
                return None
            token = secrets.token_hex(16)  # fresh per claim, even when a worker reclaims its own item
            row = conn.execute(
                text(CLAIM_SQL),
                {
                    "worker_id": self.worker_id,
                    "token": token,
                    "now": now,
                    "lease_ms": self.settings.lease_ms,
                    "id": item_id,
                },
            ).one()
        return Claim(item_id=row.id, token=token, attempts=row.attempts)

    def load_certificate_data(self, item_id: int) -> CertificateData:
        """Render inputs come only from the DB rows, never from this process's settings (§10.4)."""
        with self.engines.read.begin() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT i.job_id, i.certificate_id, i.name, i.email, j.title, j.issuer, j.issued_on, "
                        "j.print_email FROM items AS i JOIN jobs AS j ON j.id = i.job_id WHERE i.id = :id"
                    ),
                    {"id": item_id},
                )
                .mappings()
                .one()
            )
        return CertificateData(
            job_id=row["job_id"],
            certificate_id=row["certificate_id"],
            name=row["name"],
            email=row["email"] if row["print_email"] else None,
            title=row["title"],
            issuer=row["issuer"],
            issued_on=row["issued_on"],
        )

    def record_success(self, claim: Claim) -> None:
        """T5."""
        with self.engines.write.begin() as conn:
            result = conn.execute(
                text(SUCCEED_SQL), {"now": self.clock(), "id": claim.item_id, "token": claim.token}
            )
        self._check_fence(claim, result.rowcount)

    def record_failure(self, claim: Claim, exc: Exception) -> None:
        """T6 (retry after a delay) or T7 (attempts exhausted). Details go to the log, never the API."""
        # Exception text may quote recipient data, so the log gets the type and the stack frames only.
        log.error(
            "generation_failed item_id=%s attempt=%s worker_id=%s error=%s\n%s",
            claim.item_id,
            claim.attempts,
            self.worker_id,
            type(exc).__name__,
            "".join(traceback.format_tb(exc.__traceback__)),
        )
        message = f"{type(exc).__name__}: Certificate generation failed."[:MAX_ERROR_MESSAGE]
        errors = dump_errors([FieldError(None, FieldCode.GENERATION_FAILED, message).as_item_error()])
        with self.engines.write.begin() as conn:
            now = self.clock()
            params = {"id": claim.item_id, "token": claim.token, "errors": errors}
            if claim.attempts < self.settings.max_attempts:
                params["available_at"] = now + self.settings.retry_delay_ms
                result = conn.execute(text(RETRY_LATER_SQL), params)
            else:
                params["now"] = now
                result = conn.execute(text(FAIL_SQL), params)
        self._check_fence(claim, result.rowcount)

    def _check_fence(self, claim: Claim, rowcount: int) -> None:
        if rowcount == 0:
            log.warning("lease_lost item_id=%s worker_id=%s", claim.item_id, self.worker_id)


# --- Embedded workers and their monitor (§24.2) -------------------------------------------------

RESTART_BACKOFF_SECONDS = (0.25, 0.5, 1.0)
RESTART_WINDOW_SECONDS = 60.0
MONITOR_INTERVAL_SECONDS = 1.0
SHUTDOWN_DEADLINE_SECONDS = 10.0


@dataclass
class _Slot:
    index: int
    thread: threading.Thread | None = None
    restarts: collections.deque[float] = field(default_factory=collections.deque)
    exhausted: bool = False


class WorkerPool:
    """Worker threads inside the API process, plus a monitor that restarts dead threads and retires
    exhausted leases even when every worker is busy or down.

    A live but hung thread is never replaced (Python cannot kill threads); its lease expires and
    another worker recovers the item.
    """

    def __init__(
        self,
        factory: Callable[[], Worker],
        size: int,
        engines: Engines,
        settings: Settings,
        clock: Clock,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        self.factory = factory
        self.size = size
        self.engines = engines
        self.settings = settings
        self.clock = clock
        self.monotonic = monotonic
        self.stop_event = threading.Event()
        self.wait = wait or self.stop_event.wait
        self.slots = [_Slot(i) for i in range(size)]
        self.monitor: threading.Thread | None = None

    def start(self) -> None:
        self.start_workers()
        self.monitor = threading.Thread(target=self._monitor_loop, name="certgen-monitor", daemon=True)
        self.monitor.start()

    def start_workers(self) -> None:
        for slot in self.slots:
            self._start_thread(slot)

    def _start_thread(self, slot: _Slot) -> None:
        worker = self.factory()
        slot.thread = threading.Thread(
            target=worker.run_forever,
            args=(self.stop_event,),
            name=f"certgen-worker-{slot.index}",
            daemon=True,
        )
        slot.thread.start()

    def alive(self) -> int:
        return sum(1 for slot in self.slots if slot.thread is not None and slot.thread.is_alive())

    def supervise_once(self) -> None:
        for slot in self.slots:
            if slot.exhausted or slot.thread is None or slot.thread.is_alive() or self.stop_event.is_set():
                continue
            now = self.monotonic()
            while slot.restarts and now - slot.restarts[0] > RESTART_WINDOW_SECONDS:
                slot.restarts.popleft()
            if len(slot.restarts) >= len(RESTART_BACKOFF_SECONDS):
                slot.exhausted = True  # a thread that keeps dying would otherwise spin forever
                log.error("worker_slot_exhausted slot=%s", slot.index)
                continue
            log.warning("worker_thread_died slot=%s restart=%s", slot.index, len(slot.restarts) + 1)
            self.wait(RESTART_BACKOFF_SECONDS[len(slot.restarts)])
            if self.stop_event.is_set():
                return
            slot.restarts.append(self.monotonic())
            self._start_thread(slot)
        with self.engines.write.begin() as conn:  # short transaction of its own
            retire_exhausted(conn, self.clock(), self.settings.max_attempts)

    def _monitor_loop(self) -> None:
        while not self.stop_event.wait(MONITOR_INTERVAL_SECONDS):
            try:
                self.supervise_once()
            except Exception:
                log.exception("monitor_iteration_failed")

    def stop(self) -> None:
        """Ask every loop to finish its current item, then wait up to one shared deadline.

        Threads still running after it are daemons and do not block exit; their leases expire and
        another worker recovers the items.
        """
        self.stop_event.set()
        deadline = time.monotonic() + SHUTDOWN_DEADLINE_SECONDS
        threads = [slot.thread for slot in self.slots if slot.thread is not None]
        if self.monitor is not None:
            threads.append(self.monitor)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))


# --- Standalone process: python -m certgen.worker -----------------------------------------------


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    check_sqlite_version()
    prepare_data_dir(settings.data_dir, settings.certificates_dir)
    engines = create_engines(settings.db_path)
    create_schema(engines)
    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        log.info("worker_stopping signal=%s", signum)
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    renderer = with_slow_render(render_certificate, settings.test_slow_render_ms)
    worker = Worker(engines, Storage(settings.certificates_dir), settings, system_clock, renderer)
    log.info("worker_started worker_id=%s", worker.worker_id)
    try:
        worker.run_forever(stop)
    finally:
        engines.dispose()


if __name__ == "__main__":
    main()

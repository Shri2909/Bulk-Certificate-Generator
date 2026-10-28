"""Application factory and lifespan. Importing this module does no I/O; startup work happens in lifespan."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from certgen import __version__
from certgen.api import JOBS_PATH, health_router, router
from certgen.auth import TokenStore, load_or_create_token
from certgen.clock import Clock, system_clock
from certgen.config import (
    ARCHIVE_CONCURRENCY,
    BODY_TIMEOUT_SECONDS,
    MAX_CONCURRENT_REQUESTS,
    SEND_TIMEOUT_SECONDS,
    Settings,
)
from certgen.db import check_sqlite_version, create_engines, create_schema
from certgen.errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    operational_error_handler,
    request_validation_handler,
    unhandled_exception_handler,
)
from certgen.middleware import (
    BodySizeLimitMiddleware,
    ConcurrencyLimitMiddleware,
    HostAllowlistMiddleware,
    NoStoreMiddleware,
    SendDeadlineMiddleware,
    SubmissionLimitMiddleware,
    TokenAuthMiddleware,
    async_lifespan,
)
from certgen.rendering import render_certificate
from certgen.storage import Storage, prepare_data_dir
from certgen.worker import Renderer, Worker, WorkerPool, with_slow_render


def create_app(
    settings: Settings, clock: Clock = system_clock, renderer: Renderer = render_certificate
) -> FastAPI:
    tokens = TokenStore()  # filled in lifespan, before the first request; empty = every /v1 request is 401

    @contextmanager
    def lifespan(app: FastAPI) -> Iterator[None]:
        logging.getLogger("certgen").setLevel(settings.log_level)
        check_sqlite_version()
        prepare_data_dir(settings.data_dir, settings.certificates_dir)
        tokens.set_tokens(settings.api_tokens or (load_or_create_token(settings.token_file),))
        engines = create_engines(settings.db_path)
        create_schema(engines)
        storage = Storage(settings.certificates_dir)
        app.state.engines = engines
        app.state.storage = storage
        pool = None
        if settings.embedded_worker:
            slow_renderer = with_slow_render(renderer, settings.test_slow_render_ms)
            pool = WorkerPool(
                lambda: Worker(engines, storage, settings, clock, slow_renderer),
                settings.worker_threads,
                engines,
                settings,
                clock,
            )
            pool.start()
        app.state.workers = pool
        try:
            yield
        finally:
            if pool is not None:
                pool.stop()
            engines.dispose()

    app = FastAPI(title="certgen", version=__version__, lifespan=async_lifespan(lifespan))
    app.state.settings = settings
    app.state.clock = clock
    app.state.renderer = renderer
    app.state.archive_slots = threading.BoundedSemaphore(ARCHIVE_CONCURRENCY)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.max_body_bytes,
        body_timeout_seconds=BODY_TIMEOUT_SECONDS,
    )
    # Outside the body buffer: an excess submission is refused before its body is read.
    app.add_middleware(
        SubmissionLimitMiddleware, path=JOBS_PATH, max_submissions=settings.max_concurrent_submissions
    )
    # Inside the request limit: an aborted slow download releases its slot.
    app.add_middleware(SendDeadlineMiddleware, send_timeout_seconds=SEND_TIMEOUT_SECONDS)
    # Outside the body buffer, so a request still uploading holds a slot: bounded buffered memory.
    app.add_middleware(
        ConcurrencyLimitMiddleware,
        max_requests=MAX_CONCURRENT_REQUESTS,
        max_per_client=settings.max_requests_per_token,
    )
    # Token check before a request slot is taken or a body is read (audit F-12).
    app.add_middleware(TokenAuthMiddleware, store=tokens, prefix="/v1/")
    # Host check before any body is read: a rebinding page cannot even get its upload buffered.
    app.add_middleware(HostAllowlistMiddleware, allowed_hosts=settings.trusted_hosts)
    app.add_middleware(NoStoreMiddleware)  # added last = outermost, so it also covers 400/413 responses
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(OperationalError, operational_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.include_router(health_router)
    app.include_router(router)
    return app


def serve() -> None:
    """`python -m certgen`: one command, embedded workers included (§15, §24.12)."""
    settings: Settings = get_app().state.settings  # the same app uvicorn loads as "certgen.main:app"
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(
        "certgen.main:app",
        host=settings.host,
        port=settings.port,
        # No limit_concurrency: uvicorn counts open connections, so 64 idle sockets locked every client
        # out (audit F-11). Requests in progress are limited by ConcurrencyLimitMiddleware instead.
        timeout_keep_alive=5,
    )


_app: FastAPI | None = None


def get_app() -> FastAPI:
    """The process's app, built from the environment on first use rather than at import (audit F-14)."""
    global _app
    if _app is None:
        _app = create_app(Settings.from_env())
    return _app


def __getattr__(name: str) -> Any:
    # Keeps `uvicorn certgen.main:app` and `certgen.main.app` working without import-time settings.
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""Application factory and lifespan. Importing this module does no I/O; startup work happens in lifespan."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from certgen import __version__
from certgen.api import router
from certgen.clock import Clock, system_clock
from certgen.config import ARCHIVE_CONCURRENCY, Settings
from certgen.db import check_sqlite_version, create_engines, create_schema
from certgen.errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    operational_error_handler,
    request_validation_handler,
    unhandled_exception_handler,
)
from certgen.middleware import BodySizeLimitMiddleware, NoStoreMiddleware, async_lifespan
from certgen.rendering import render_certificate
from certgen.storage import Storage, prepare_data_dir
from certgen.worker import Renderer, Worker, WorkerPool, with_slow_render


def create_app(
    settings: Settings, clock: Clock = system_clock, renderer: Renderer = render_certificate
) -> FastAPI:
    @contextmanager
    def lifespan(app: FastAPI) -> Iterator[None]:
        logging.getLogger("certgen").setLevel(settings.log_level)
        check_sqlite_version()
        prepare_data_dir(settings.data_dir, settings.certificates_dir)
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
    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=settings.max_body_bytes)
    app.add_middleware(NoStoreMiddleware)  # added last = outermost, so it also covers 413 responses
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(OperationalError, operational_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.include_router(router)
    return app


def serve() -> None:
    """`python -m certgen`: one command, embedded workers included (§15, §24.12)."""
    settings = Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(
        "certgen.main:app",
        host=settings.host,
        port=settings.port,
        # Bounded concurrency and short keep-alive: excess connections get 503 instead of piling up.
        limit_concurrency=64,
        timeout_keep_alive=5,
    )


app = create_app(Settings.from_env())

"""Application factory and lifespan. Importing this module does no I/O; startup work happens in lifespan."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from certgen import __version__
from certgen.api import router
from certgen.clock import Clock, system_clock
from certgen.config import Settings
from certgen.db import check_sqlite_version, create_engines, create_schema
from certgen.errors import (
    ApiError,
    api_error_handler,
    http_exception_handler,
    operational_error_handler,
    request_validation_handler,
    unhandled_exception_handler,
)
from certgen.middleware import NoStoreMiddleware, async_lifespan
from certgen.rendering import render_certificate
from certgen.storage import Storage
from certgen.worker import Renderer


def create_app(
    settings: Settings, clock: Clock = system_clock, renderer: Renderer = render_certificate
) -> FastAPI:
    @contextmanager
    def lifespan(app: FastAPI) -> Iterator[None]:
        logging.getLogger("certgen").setLevel(settings.log_level)
        check_sqlite_version()
        settings.certificates_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        engines = create_engines(settings.db_path)
        create_schema(engines)
        app.state.engines = engines
        app.state.storage = Storage(settings.certificates_dir)
        try:
            yield
        finally:
            engines.dispose()

    app = FastAPI(title="certgen", version=__version__, lifespan=async_lifespan(lifespan))
    app.state.settings = settings
    app.state.clock = clock
    app.state.renderer = renderer
    app.add_middleware(NoStoreMiddleware)
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(OperationalError, operational_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.include_router(router)
    return app


app = create_app(Settings.from_env())

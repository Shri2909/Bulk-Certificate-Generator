"""One error shape everywhere: RFC 9457 problem+json with machine-readable codes (§6.1).

Handlers are plain functions (Starlette runs them in its threadpool); bodies never carry exception
text or tracebacks, which go to the log only.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("certgen.errors")

PROBLEM_MEDIA_TYPE = "application/problem+json"


class ProblemCode(StrEnum):
    INVALID_HOST = "INVALID_HOST"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CERTIFICATE_NOT_READY = "CERTIFICATE_NOT_READY"
    CERTIFICATE_GENERATION_FAILED = "CERTIFICATE_GENERATION_FAILED"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    NOTHING_TO_RETRY = "NOTHING_TO_RETRY"
    CERTIFICATE_FILE_MISSING = "CERTIFICATE_FILE_MISSING"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    DB_UNAVAILABLE = "DB_UNAVAILABLE"
    ARCHIVE_BUSY = "ARCHIVE_BUSY"
    CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
    SERVER_BUSY = "SERVER_BUSY"
    STORAGE_FULL = "STORAGE_FULL"
    REQUEST_TIMEOUT = "REQUEST_TIMEOUT"


# HTTP status and short title per code.
PROBLEMS: dict[ProblemCode, tuple[int, str]] = {
    ProblemCode.INVALID_HOST: (400, "Invalid host"),
    ProblemCode.VALIDATION_FAILED: (422, "Validation failed"),
    ProblemCode.PAYLOAD_TOO_LARGE: (413, "Payload too large"),
    ProblemCode.UNSUPPORTED_MEDIA_TYPE: (415, "Unsupported media type"),
    ProblemCode.NOT_FOUND: (404, "Not found"),
    ProblemCode.METHOD_NOT_ALLOWED: (405, "Method not allowed"),
    ProblemCode.CERTIFICATE_NOT_READY: (409, "Certificate not ready"),
    ProblemCode.CERTIFICATE_GENERATION_FAILED: (409, "Certificate generation failed"),
    ProblemCode.IDEMPOTENCY_KEY_REUSED: (409, "Idempotency key reused"),
    ProblemCode.NOTHING_TO_RETRY: (409, "Nothing to retry"),
    ProblemCode.CERTIFICATE_FILE_MISSING: (410, "Certificate file missing"),
    ProblemCode.INTERNAL_ERROR: (500, "Internal server error"),
    ProblemCode.DB_UNAVAILABLE: (503, "Database unavailable"),
    ProblemCode.ARCHIVE_BUSY: (503, "Archive busy"),
    ProblemCode.CAPACITY_EXCEEDED: (503, "Capacity exceeded"),
    ProblemCode.SERVER_BUSY: (503, "Server busy"),
    ProblemCode.STORAGE_FULL: (503, "Storage full"),
    ProblemCode.REQUEST_TIMEOUT: (408, "Request timeout"),
}


class FieldCode(StrEnum):
    INVALID_JSON = "INVALID_JSON"
    REQUIRED = "REQUIRED"
    INVALID_TYPE = "INVALID_TYPE"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    INVALID_VALUE = "INVALID_VALUE"
    INVALID_FORMAT = "INVALID_FORMAT"
    TOO_LONG = "TOO_LONG"
    TOO_MANY_RECIPIENTS = "TOO_MANY_RECIPIENTS"
    DATE_IN_FUTURE = "DATE_IN_FUTURE"
    INVALID_CHARACTERS = "INVALID_CHARACTERS"
    UNSUPPORTED_CHARACTERS = "UNSUPPORTED_CHARACTERS"
    DOES_NOT_FIT = "DOES_NOT_FIT"
    INVALID_EMAIL = "INVALID_EMAIL"
    DUPLICATE_RECIPIENT = "DUPLICATE_RECIPIENT"
    GENERATION_FAILED = "GENERATION_FAILED"
    WORKER_LOST = "WORKER_LOST"


@dataclass(frozen=True)
class FieldError:
    field: str | None
    code: FieldCode
    message: str
    row_index: int | None = None
    duplicate_of_row_index: int | None = None

    def as_item_error(self) -> dict[str, Any]:
        """Shape stored in items.errors and shown on item resources (no row_index)."""
        out: dict[str, Any] = {"field": self.field, "code": self.code.value, "message": self.message}
        if self.duplicate_of_row_index is not None:
            out["duplicate_of_row_index"] = self.duplicate_of_row_index
        return out

    def as_flat(self) -> dict[str, Any]:
        """Shape used in request-level `errors` arrays (422 and the 202 body)."""
        return {"row_index": self.row_index, **self.as_item_error()}


class ApiError(Exception):
    def __init__(
        self,
        code: ProblemCode,
        detail: str,
        errors: Sequence[FieldError] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.errors = list(errors) if errors is not None else None
        self.headers = dict(headers or {})


def problem_response(
    code: ProblemCode,
    detail: str,
    errors: Sequence[FieldError] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    status, title = PROBLEMS[code]
    body: dict[str, Any] = {"type": "about:blank", "title": title, "status": status, "code": code.value}
    body["detail"] = detail
    if errors is not None:
        body["errors"] = [e.as_flat() for e in errors]
    out_headers = {"Cache-Control": "no-store", **(headers or {})}
    if status == 503:
        out_headers["Retry-After"] = "1"
    return JSONResponse(body, status_code=status, headers=out_headers, media_type=PROBLEM_MEDIA_TYPE)


# Pydantic error type -> FieldCode (§6.1); anything not listed maps to INVALID_VALUE.
PYDANTIC_CODES = {
    "json_invalid": FieldCode.INVALID_JSON,
    "missing": FieldCode.REQUIRED,
    "string_type": FieldCode.INVALID_TYPE,
    "dict_type": FieldCode.INVALID_TYPE,
    "list_type": FieldCode.INVALID_TYPE,
    "model_attributes_type": FieldCode.INVALID_TYPE,
    "model_type": FieldCode.INVALID_TYPE,
    "extra_forbidden": FieldCode.UNKNOWN_FIELD,
}


def body_errors_from_pydantic(errors: Sequence[Mapping[str, Any]]) -> list[FieldError]:
    """Map Pydantic errors for the request body; locs are relative to the body (§24.5 step 5)."""
    out = []
    for err in errors:
        loc = tuple(err["loc"])
        names = [str(part) for part in loc if not isinstance(part, int)]
        row_index = loc[1] if len(loc) > 1 and loc[0] == "recipients" and isinstance(loc[1], int) else None
        out.append(
            FieldError(
                field=".".join(names) or None,
                code=PYDANTIC_CODES.get(err["type"], FieldCode.INVALID_VALUE),
                message=str(err["msg"]),
                row_index=row_index,
            )
        )
    return out


def api_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    exc = cast(ApiError, exc)  # registered for this type only
    return problem_response(exc.code, exc.detail, exc.errors, exc.headers)


def request_validation_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Query and path parameter errors: `field` is the parameter name (§6.5)."""
    exc = cast(RequestValidationError, exc)  # registered for this type only
    errors = []
    for err in exc.errors():
        loc = [str(part) for part in err["loc"][1:] if not isinstance(part, int)]
        code = FieldCode.REQUIRED if err["type"] == "missing" else FieldCode.INVALID_VALUE
        errors.append(FieldError(field=".".join(loc) or None, code=code, message=str(err["msg"])))
    return problem_response(ProblemCode.VALIDATION_FAILED, "Request is invalid.", errors)


def http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    exc = cast(StarletteHTTPException, exc)  # registered for this type only
    if exc.status_code == 404:
        return problem_response(ProblemCode.NOT_FOUND, "The requested resource was not found.")
    if exc.status_code == 405:
        return problem_response(
            ProblemCode.METHOD_NOT_ALLOWED,
            "The method is not allowed for this resource.",
            headers=dict(exc.headers or {}),
        )
    log.error("unexpected_http_exception status=%s", exc.status_code)
    return problem_response(ProblemCode.INTERNAL_ERROR, "Internal server error")


def is_lock_timeout(exc: OperationalError) -> bool:
    message = str(exc.orig).lower()
    return "database is locked" in message or "database is busy" in message


def operational_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    exc = cast(OperationalError, exc)  # registered for this type only
    if is_lock_timeout(exc):
        log.warning("db_lock_timeout")
        return problem_response(ProblemCode.DB_UNAVAILABLE, "The database is busy; retry shortly.")
    log.error("db_operational_error", exc_info=exc)
    return problem_response(ProblemCode.INTERNAL_ERROR, "Internal server error")


def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled_exception", exc_info=exc)
    return problem_response(ProblemCode.INTERNAL_ERROR, "Internal server error")

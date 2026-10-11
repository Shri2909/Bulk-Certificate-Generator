"""HTTP <-> service mapping (§6). Routes are plain `def`, so FastAPI runs them in its threadpool."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import anyio.from_thread
from fastapi import APIRouter, Request, Response
from pydantic import ValidationError

from certgen import service
from certgen.errors import ApiError, FieldCode, FieldError, ProblemCode, body_errors_from_pydantic
from certgen.models import CertificateOut, Counts, CreateJobRequest, JobCreated, JobLinks, JobResource
from certgen.storage import HEX32
from certgen.validation import JsonBodyError, canonical_json, decode_json_body, validate_request

router = APIRouter()

JOBS_PATH = "/v1/certificate-jobs"


def iso_ms(epoch_ms: int) -> str:
    seconds, millis = divmod(epoch_ms, 1000)
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"


def not_found() -> ApiError:
    return ApiError(ProblemCode.NOT_FOUND, "The requested resource was not found.")


def job_resource(view: service.JobView) -> dict[str, Any]:
    base = f"{JOBS_PATH}/{view.id}"
    return JobResource(
        id=view.id,
        status=view.status,
        certificate=CertificateOut(title=view.title, issuer=view.issuer, issued_on=view.issued_on),
        print_email=view.print_email,
        counts=Counts(**view.counts),
        progress=view.progress,
        created_at=iso_ms(view.created_at),
        finished_at=iso_ms(view.finished_at) if view.finished_at is not None else None,
        links=JobLinks(self=base, items=f"{base}/items", archive=f"{base}/archive"),
    ).model_dump()


def invalid(errors: list[FieldError], detail: str = "Request is invalid.") -> ApiError:
    return ApiError(ProblemCode.VALIDATION_FAILED, detail, errors)


def is_json_media_type(content_type: str | None) -> bool:
    """application/json, optionally with charset=utf-8, case-insensitive (§24.5 step 1)."""
    if content_type is None:
        return False
    media_type, *params = (part.strip().lower() for part in content_type.split(";"))
    return media_type == "application/json" and all(p.replace('"', "") == "charset=utf-8" for p in params)


def read_json_body(request: Request) -> Any:
    """§24.5: the body is parsed here instead of by FastAPI, so duplicate keys, NaN and deep nesting
    are rejected rather than silently accepted."""
    if not is_json_media_type(request.headers.get("content-type")):
        raise ApiError(ProblemCode.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json.")
    # Request.body() is async; this sync route runs in a worker thread, which may call into the loop.
    raw: bytes = anyio.from_thread.run(request.body)
    if not raw:
        raise invalid([FieldError(None, FieldCode.REQUIRED, "A JSON request body is required.")])
    try:
        return decode_json_body(raw)
    except JsonBodyError as exc:
        raise invalid([FieldError(None, FieldCode.INVALID_JSON, str(exc))]) from None


@router.post(JOBS_PATH, status_code=202, response_model=JobCreated)
def create_job(request: Request, response: Response) -> JobCreated:
    state = request.app.state
    obj = read_json_body(request)
    try:
        body = CreateJobRequest.model_validate(obj)
    except ValidationError as exc:
        raise invalid(body_errors_from_pydantic(exc.errors())) from None
    today = datetime.fromtimestamp(state.clock() // 1000, UTC).date()
    validated = validate_request(
        body,
        today=today,
        print_email=state.settings.print_email,
        max_recipients=state.settings.max_recipients,
    )
    if validated.request_errors:
        raise invalid(validated.all_errors)
    if validated.valid_count == 0:
        n = len(validated.rows)
        raise invalid(validated.all_errors, f"{n} of {n} recipients are invalid; no job was created.")
    view = service.create_job(state.engines, state.settings, state.clock, validated, canonical_json(obj))
    response.headers["Location"] = f"{JOBS_PATH}/{view.id}"
    return JobCreated(**job_resource(view), errors=[e.as_flat() for e in validated.row_errors])


@router.get(JOBS_PATH + "/{job_id}", response_model=JobResource)
def get_job(request: Request, job_id: str) -> dict[str, Any]:
    view = service.get_job(request.app.state.engines, job_id) if HEX32.fullmatch(job_id) else None
    if view is None:
        raise not_found()
    return job_resource(view)


@router.get("/v1/certificates/{certificate_id}", response_class=Response)
def download_certificate(request: Request, certificate_id: str) -> Response:
    state = request.app.state
    record = (
        service.find_certificate(state.engines, certificate_id) if HEX32.fullmatch(certificate_id) else None
    )
    if record is None:
        raise not_found()
    data = state.storage.path_for(record.job_id, record.certificate_id).read_bytes()
    return Response(
        data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="certificate-{record.certificate_id}.pdf"'},
    )

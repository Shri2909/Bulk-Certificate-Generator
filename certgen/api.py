"""HTTP <-> service mapping (§6). Routes are plain `def`, so FastAPI runs them in its threadpool."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import anyio.from_thread
from fastapi import APIRouter, Request, Response
from pydantic import ValidationError

from certgen import service
from certgen.errors import ApiError, ProblemCode, body_errors_from_pydantic
from certgen.models import CertificateOut, Counts, CreateJobRequest, JobCreated, JobLinks, JobResource
from certgen.storage import HEX32
from certgen.validation import canonical_json, validate_request

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


@router.post(JOBS_PATH, status_code=202, response_model=JobCreated)
def create_job(request: Request, response: Response) -> JobCreated:
    state = request.app.state
    # Request.body() is async; this sync route runs in a worker thread, which may call back into the loop.
    raw = anyio.from_thread.run(request.body)
    obj = json.loads(raw)
    try:
        body = CreateJobRequest.model_validate(obj)
    except ValidationError as exc:
        errors = body_errors_from_pydantic(exc.errors())
        raise ApiError(ProblemCode.VALIDATION_FAILED, "Request is invalid.", errors) from None
    validated = validate_request(body, print_email=state.settings.print_email)
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

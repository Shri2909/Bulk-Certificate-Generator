"""One certificate's failure never blocks the others (§5.1 T6/T7, §7.2, §24.3)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import JOBS, FakeClock, fail_n_times, failing_for, job_payload, post_job, recipient
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from certgen.rendering import CertificateData, render_certificate
from certgen.storage import Storage
from certgen.worker import MAX_PDF_BYTES


def items(app: FastAPI) -> list[Any]:
    with app.state.engines.read.connect() as conn:
        return list(conn.execute(text("SELECT * FROM items ORDER BY row_index")).mappings())


def drain_through_retries(drain: Any, clock: FakeClock, renderer: Any, attempts: int = 3) -> None:
    """Run every attempt the retry policy allows, stepping the clock past each retry delay."""
    for _ in range(attempts):
        drain(renderer)
        clock.advance(2_000)


@pytest.mark.req("R9", "R18")
def test_one_failing_recipient_does_not_block_others(
    client: TestClient, app: FastAPI, drain: Any, fake_clock: FakeClock
) -> None:
    rows = [recipient(0), recipient(1, name="Bob Broken"), recipient(2), recipient(3)]
    job_id = post_job(client, job_payload(recipients=rows)).json()["id"]
    drain(failing_for({"Bob Broken"}))
    # The first pass already produced every other certificate; Bob waits for a retry.
    assert [r["status"] for r in items(app)] == ["succeeded", "pending", "succeeded", "succeeded"]
    drain_through_retries(drain, fake_clock, failing_for({"Bob Broken"}))
    stored = items(app)
    assert [r["status"] for r in stored] == ["succeeded", "failed", "succeeded", "succeeded"]
    assert stored[1]["attempts"] == 3
    assert [e["code"] for e in json.loads(stored[1]["errors"])] == ["GENERATION_FAILED"]
    body = client.get(f"{JOBS}/{job_id}").json()
    assert body["status"] == "completed_with_errors"
    assert body["counts"]["succeeded"] == 3 and body["counts"]["failed"] == 1
    assert body["progress"] == 1.0


@pytest.mark.req("R9", "R18")
def test_transient_failure_then_success(
    client: TestClient, app: FastAPI, drain: Any, fake_clock: FakeClock
) -> None:
    job_id = post_job(client, job_payload(n=1)).json()["id"]
    renderer = fail_n_times(1)
    drain(renderer)
    row = items(app)[0]
    assert row["status"] == "pending"
    assert [e["code"] for e in json.loads(row["errors"])] == ["GENERATION_FAILED"]
    fake_clock.advance(2_000)
    drain(renderer)
    row = items(app)[0]
    assert (row["status"], row["attempts"], row["errors"]) == ("succeeded", 2, None)
    assert client.get(f"{JOBS}/{job_id}").json()["status"] == "completed"


@pytest.mark.req("R9", "R18")
def test_storage_error_is_isolated(
    client: TestClient, app: FastAPI, drain: Any, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    post_job(client, job_payload(n=3))
    doomed = items(app)[1]["certificate_id"]
    real_save = Storage.save_atomic

    def save(self: Storage, job_id: str, certificate_id: str, data: bytes) -> None:
        if certificate_id == doomed:
            raise OSError("No space left on device")
        real_save(self, job_id, certificate_id, data)

    monkeypatch.setattr(Storage, "save_atomic", save)
    drain_through_retries(drain, fake_clock, render_certificate)
    stored = items(app)
    assert [r["status"] for r in stored] == ["succeeded", "failed", "succeeded"]
    assert json.loads(stored[1]["errors"])[0]["message"] == "OSError: Certificate generation failed."


@pytest.mark.req("R8", "R18")
def test_download_failed_item_409(
    client: TestClient, app: FastAPI, drain: Any, fake_clock: FakeClock
) -> None:
    post_job(client, job_payload(recipients=[recipient(0, name="Always Fails")]))
    drain_through_retries(drain, fake_clock, failing_for({"Always Fails"}))
    cert = items(app)[0]["certificate_id"]
    resp = client.get(f"/v1/certificates/{cert}")
    assert resp.status_code == 409
    assert resp.json()["code"] == "CERTIFICATE_GENERATION_FAILED"


@pytest.mark.req("R10")
def test_failed_error_message_has_no_traceback_or_exception_text(
    client: TestClient, app: FastAPI, drain: Any, fake_clock: FakeClock
) -> None:
    post_job(client, job_payload(recipients=[recipient(0, name="Zoë Secret")]))

    def leaky(data: CertificateData) -> bytes:
        raise ValueError(f"cannot open /srv/private/{data.name}.ttf")

    drain_through_retries(drain, fake_clock, leaky)
    row = items(app)[0]
    assert row["status"] == "failed"
    errors = json.loads(row["errors"])
    assert errors == [
        {"field": None, "code": "GENERATION_FAILED", "message": "ValueError: Certificate generation failed."}
    ]
    assert "Secret" not in row["errors"] and "/srv" not in row["errors"] and "Traceback" not in row["errors"]


@pytest.mark.req("R9", "R18")
def test_oversized_pdf_is_item_failure(
    client: TestClient, app: FastAPI, drain: Any, fake_clock: FakeClock, settings: Any
) -> None:
    post_job(client, job_payload(n=2))
    big = items(app)[0]["certificate_id"]

    def bloated(data: CertificateData) -> bytes:
        pdf = render_certificate(data)
        return pdf + b"\0" * (MAX_PDF_BYTES + 1 - len(pdf)) if data.certificate_id == big else pdf

    drain_through_retries(drain, fake_clock, bloated)
    stored = items(app)
    assert MAX_PDF_BYTES == 2_097_152
    assert [r["status"] for r in stored] == ["failed", "succeeded"]
    assert [e["code"] for e in json.loads(stored[0]["errors"])] == ["GENERATION_FAILED"]
    assert not any(big in p.name for p in settings.certificates_dir.rglob("*"))


@pytest.mark.req("R10", "R18")
def test_items_listing_identifies_failed_and_succeeded(
    client: TestClient, drain: Any, fake_clock: FakeClock
) -> None:
    rows = [recipient(0), recipient(1, name="Broken Row"), recipient(2), recipient(3, email="nope")]
    job_id = post_job(client, job_payload(recipients=rows)).json()["id"]
    drain_through_retries(drain, fake_clock, failing_for({"Broken Row"}))
    listing = {i["row_index"]: i for i in client.get(f"{JOBS}/{job_id}/items").json()["items"]}
    assert {k: v["status"] for k, v in listing.items()} == {
        0: "succeeded",
        1: "failed",
        2: "succeeded",
        3: "rejected",
    }
    assert [e["code"] for e in listing[1]["errors"]] == ["GENERATION_FAILED"]
    assert listing[1]["attempts"] == 3 and listing[1]["download_url"] is None
    assert [e["code"] for e in listing[3]["errors"]] == ["INVALID_EMAIL"]
    failed = client.get(f"{JOBS}/{job_id}/items", params={"status": "failed"}).json()["items"]
    assert [(i["row_index"], i["name"]) for i in failed] == [(1, "Broken Row")]


@pytest.mark.req("R10")
def test_failure_log_has_no_exception_text(
    client: TestClient, drain: Any, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    # Exception messages may quote recipient data; the log keeps the type and stack frames only.
    post_job(client, job_payload(recipients=[recipient(0, name="Zoë Secret", email="secret@example.com")]))

    def leaky(data: CertificateData) -> bytes:
        raise ValueError(f"cannot render {data.name} <{data.email}>")

    with caplog.at_level("ERROR", logger="certgen"):
        drain(leaky)
    assert "generation_failed" in caplog.text and "error=ValueError" in caplog.text
    assert "Zoë Secret" not in caplog.text and "secret@example.com" not in caplog.text

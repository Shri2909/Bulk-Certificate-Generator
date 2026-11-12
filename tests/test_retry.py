"""POST /v1/certificate-jobs/{id}/retry re-queues failed items."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import JOBS, FakeClock, failing_for, item_rows, job_payload, post_job, recipient
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {"max_attempts": 1, "max_recipients": 3, "max_active_items": 4}


def job_with_failure(client: TestClient, drain: Any, clock: FakeClock) -> str:
    rows = [recipient(0), recipient(1, name="Flaky Disk"), recipient(2, email="bad")]
    job_id = str(post_job(client, job_payload(recipients=rows)).json()["id"])
    for _ in range(3):
        drain(failing_for({"Flaky Disk"}))
        clock.advance(2_000)
    return job_id


def retry(client: TestClient, job_id: str, **kwargs: Any) -> Any:
    return client.post(f"{JOBS}/{job_id}/retry", **kwargs)


@pytest.mark.req("R9", "R10")
def test_retry_requeues_only_failed(
    client: TestClient, app: FastAPI, drain: Any, fake_clock: FakeClock
) -> None:
    job_id = job_with_failure(client, drain, fake_clock)
    before = item_rows(app)
    assert [r["status"] for r in before] == ["succeeded", "failed", "rejected"]
    resp = retry(client, job_id)
    assert resp.status_code == 202
    body = resp.json()
    assert body["id"] == job_id and body["counts"]["pending"] == 1 and body["counts"]["failed"] == 0
    after = item_rows(app)
    assert [r["status"] for r in after] == ["succeeded", "pending", "rejected"]
    assert after[0] == before[0] and after[2] == before[2]  # untouched rows are byte-for-byte equal
    drain()  # the cause is fixed: the default renderer works now
    final = item_rows(app)
    assert final[1]["status"] == "succeeded"
    assert final[1]["certificate_id"] == before[1]["certificate_id"]  # same certificate, same id
    assert client.get(f"/v1/certificates/{final[1]['certificate_id']}").status_code == 200
    assert client.get(f"{JOBS}/{job_id}").json()["status"] == "completed_with_errors"  # row 2 stays rejected


@pytest.mark.req("R9")
def test_retry_resets_all_columns(
    client: TestClient, app: FastAPI, drain: Any, fake_clock: FakeClock
) -> None:
    job_id = job_with_failure(client, drain, fake_clock)
    assert retry(client, job_id).status_code == 202
    row = item_rows(app)[1]
    assert row["status"] == "pending"
    assert (row["attempts"], row["errors"], row["available_at"], row["completed_at"]) == (0, None, None, None)
    assert (row["lease_owner"], row["lease_token"], row["lease_expires_at"]) == (None, None, None)


@pytest.mark.req("R9")
def test_retry_nothing_to_retry_409(client: TestClient, drain: Any) -> None:
    job_id = post_job(client, job_payload(n=2)).json()["id"]
    resp = retry(client, job_id)
    assert resp.status_code == 409 and resp.json()["code"] == "NOTHING_TO_RETRY"
    drain()
    assert retry(client, job_id).json()["code"] == "NOTHING_TO_RETRY"


@pytest.mark.req("R9")
def test_retry_unknown_job_404(client: TestClient) -> None:
    for job_id in ("0" * 32, "not-a-job"):
        resp = retry(client, job_id)
        assert resp.status_code == 404 and resp.json()["code"] == "NOT_FOUND"


@pytest.mark.req("R9")
def test_retry_fences_out_stale_worker(
    client: TestClient,
    app: FastAPI,
    make_worker: Any,
    fake_clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    job_id = post_job(client, job_payload(n=1)).json()["id"]
    stale = make_worker(worker_id="stale")
    old_claim = stale.claim_next()  # the worker then hangs past its lease
    assert old_claim is not None
    fake_clock.advance(30_001)
    assert make_worker().claim_next() is None  # the claim's retire step ends the item: failed, WORKER_LOST
    assert item_rows(app)[0]["status"] == "failed"
    assert retry(client, job_id).status_code == 202
    stale.record_success(old_claim)  # the hung worker wakes up and tries to publish
    stale.record_failure(old_claim, RuntimeError("late"))
    assert "lease_lost" in caplog.text
    row = item_rows(app)[0]
    assert (row["status"], row["attempts"], row["lease_token"]) == ("pending", 0, None)
    current = make_worker(worker_id="current")
    claim = current.claim_next()
    assert claim is not None and claim.token != old_claim.token
    stale.record_success(old_claim)  # still rejected while the new owner holds the item
    assert item_rows(app)[0]["lease_token"] == claim.token
    current.process(claim)
    assert (item_rows(app)[0]["status"], item_rows(app)[0]["attempts"]) == ("succeeded", 1)


@pytest.mark.req("R9", "R11")
def test_retry_respects_capacity(client: TestClient, app: FastAPI, make_worker: Any) -> None:
    # max_attempts=1: one failed render ends an item. Capacity is 4 unfinished items in total.
    failing_job = post_job(
        client, job_payload(recipients=[recipient(0, name="Fails"), recipient(1, name="Fails Too")])
    ).json()["id"]
    worker = make_worker(failing_for({"Fails", "Fails Too"}))
    worker.run_until_idle()
    assert [r["status"] for r in item_rows(app)] == ["failed", "failed"]
    assert post_job(client, job_payload(n=3)).status_code == 202  # 3 active now
    resp = retry(client, failing_job)  # 3 active + 2 to requeue > 4
    assert resp.status_code == 503
    assert resp.json()["code"] == "CAPACITY_EXCEEDED" and resp.headers["retry-after"] == "1"
    assert [r["status"] for r in item_rows(app) if r["job_id"] == failing_job] == [
        "failed",
        "failed",
    ]  # unchanged


@pytest.mark.req("R9")
def test_retry_body_rules(client: TestClient, make_worker: Any) -> None:
    job_id = post_job(client, job_payload(recipients=[recipient(0, name="Fails")])).json()["id"]
    make_worker(failing_for({"Fails"})).run_until_idle()
    unknown = retry(client, job_id, json={"rows": [0]})
    assert unknown.status_code == 422
    assert [(e["field"], e["code"]) for e in unknown.json()["errors"]] == [("rows", "UNKNOWN_FIELD")]
    wrong_type = retry(client, job_id, content=b"{}", headers={"Content-Type": "text/plain"})
    assert wrong_type.status_code == 415
    assert retry(client, job_id, json={}).status_code == 202  # an empty JSON object is accepted


@pytest.mark.req("R9")
def test_failed_rows_never_hold_a_lease_token(client: TestClient, app: FastAPI, make_worker: Any) -> None:
    # Retry needs no token of its own to fence out stale workers: the schema forbids a failed row from
    # carrying one, and every path into "failed" clears it.
    post_job(client, job_payload(n=1))
    make_worker(failing_for({"Recipient 0"})).run_until_idle()  # T7 with max_attempts=1
    assert (item_rows(app)[0]["status"], item_rows(app)[0]["lease_token"]) == ("failed", None)
    with pytest.raises(IntegrityError), app.state.engines.write.begin() as conn:
        conn.execute(text("UPDATE items SET lease_token = 'stale' WHERE status = 'failed'"))

"""D3: ?dry_run=true validates exactly like a real submission and stores nothing (§6.3, §7.1)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import JOBS, certificate_info, job_payload, post_job, recipient
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

ROWS = [
    recipient(0),
    recipient(1, email="bad"),
    recipient(2, name=""),
    recipient(3, email="recipient0@example.com"),
]


def stored_counts(app: FastAPI) -> tuple[int, int]:
    with app.state.engines.read.connect() as conn:
        jobs = conn.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one()
        items = conn.execute(text("SELECT COUNT(*) FROM items")).scalar_one()
    return int(jobs), int(items)


@pytest.mark.req("R2")
def test_dry_run_creates_nothing(client: TestClient, app: FastAPI, settings: Any) -> None:
    resp = client.post(JOBS, params={"dry_run": "true"}, json=job_payload(recipients=ROWS))
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True and body["would_create_job"] is True
    assert body["counts"] == {"total": 4, "valid": 1, "rejected": 3}
    assert "location" not in resp.headers
    assert stored_counts(app) == (0, 0)
    assert list(settings.certificates_dir.iterdir()) == []


@pytest.mark.req("R2")
def test_dry_run_all_invalid_returns_200_report(client: TestClient, app: FastAPI) -> None:
    rows = [recipient(0, email="bad"), recipient(1, name="")]
    resp = client.post(JOBS, params={"dry_run": "true"}, json=job_payload(recipients=rows))
    assert resp.status_code == 200  # a report, not a rejection: nothing would be stored anyway
    body = resp.json()
    assert body["would_create_job"] is False
    assert body["counts"] == {"total": 2, "valid": 0, "rejected": 2}
    assert [(e["row_index"], e["code"]) for e in body["errors"]] == [(0, "INVALID_EMAIL"), (1, "REQUIRED")]
    assert stored_counts(app) == (0, 0)


@pytest.mark.req("R2")
def test_dry_run_ignores_idempotency_key(client: TestClient, app: FastAPI) -> None:
    headers = {"Idempotency-Key": "client-key-1"}
    dry = client.post(JOBS, params={"dry_run": "true"}, json=job_payload(n=1), headers=headers)
    assert dry.status_code == 200
    assert stored_counts(app) == (0, 0)
    # The key was never stored, so a real submission with it and a *different* body is a fresh job.
    real = client.post(JOBS, json=job_payload(n=2), headers=headers)
    assert real.status_code == 202
    assert stored_counts(app) == (1, 2)


@pytest.mark.req("R2")
def test_dry_run_matches_real_admission_errors(client: TestClient) -> None:
    dry = client.post(JOBS, params={"dry_run": "true"}, json=job_payload(recipients=ROWS)).json()
    real = post_job(client, job_payload(recipients=ROWS)).json()
    assert dry["errors"] == real["errors"]
    assert dry["counts"]["valid"] == real["counts"]["pending"]
    # Request-level errors are still a 422, exactly as without dry_run.
    bad = job_payload(recipients=ROWS, certificate=certificate_info(title=""))
    dry_bad = client.post(JOBS, params={"dry_run": "true"}, json=bad)
    real_bad = post_job(client, bad)
    assert dry_bad.status_code == real_bad.status_code == 422
    assert dry_bad.json()["errors"] == real_bad.json()["errors"]


@pytest.mark.req("R2")
def test_dry_run_false_and_invalid_values(client: TestClient) -> None:
    assert client.post(JOBS, params={"dry_run": "false"}, json=job_payload(n=1)).status_code == 202
    resp = client.post(JOBS, params={"dry_run": "maybe"}, json=job_payload(n=1))
    assert resp.status_code == 422
    assert [(e["field"], e["code"]) for e in resp.json()["errors"]] == [("dry_run", "INVALID_VALUE")]


@pytest.mark.req("R2")
def test_dry_run_bypasses_capacity(client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    from certgen import service

    monkeypatch.setattr(service, "count_active_items", lambda conn: 10**9)  # queue "full"
    assert post_job(client, job_payload(n=1)).status_code == 503
    assert client.post(JOBS, params={"dry_run": "true"}, json=job_payload(n=1)).status_code == 200

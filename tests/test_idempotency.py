"""Idempotency-Key makes POST /v1/certificate-jobs safe to retry."""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

import pytest
from conftest import JOBS, TEST_API_TOKEN, job_count, job_payload, post_raw, recipient
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from certgen import service


def post(client: TestClient, payload: Any, key: str | None = "order-42") -> Any:
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post(JOBS, json=payload, headers=headers)


@pytest.mark.req("R1", "R11")
def test_replay_returns_same_job(client: TestClient, app: FastAPI, drain: Any) -> None:
    payload = job_payload(recipients=[recipient(0), recipient(1, email="bad")])
    first = post(client, payload)
    assert first.status_code == 202
    drain()  # the replay reports the *current* state, not the original 202 body
    replay = post(client, payload)
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["status"] == "completed_with_errors"
    assert replay.json()["errors"] == first.json()["errors"]
    assert job_count(app) == 1


@pytest.mark.req("R1", "R11")
def test_replay_header_and_200(client: TestClient) -> None:
    first = post(client, job_payload(n=1))
    replay = post(client, job_payload(n=1))
    assert replay.status_code == 200
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.headers["location"] == first.headers["location"]
    assert "idempotent-replayed" not in first.headers
    assert post(client, job_payload(n=1), key=None).status_code == 202  # no header: a new job


@pytest.mark.req("R1", "R11")
def test_same_key_different_body_409(client: TestClient, app: FastAPI) -> None:
    assert post(client, job_payload(n=1)).status_code == 202
    resp = post(client, job_payload(n=2))
    assert resp.status_code == 409
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert job_count(app) == 1
    assert post(client, job_payload(n=2), key="another-key").status_code == 202  # keys are independent


@pytest.mark.req("R1", "R11")
def test_key_order_and_whitespace_do_not_change_hash(client: TestClient, app: FastAPI) -> None:
    payload = job_payload(n=2)
    assert post(client, payload).status_code == 202
    reordered = json.dumps(
        {"recipients": payload["recipients"], "certificate": payload["certificate"]}, indent=4
    )
    resp = post_raw(client, reordered.encode(), {"Idempotency-Key": "order-42"})
    assert resp.status_code == 200  # same JSON value, different bytes: still a replay
    assert job_count(app) == 1
    changed = job_payload(recipients=[recipient(0, name=" Recipient 0"), recipient(1)])
    assert post(client, changed).status_code == 409  # the fingerprint covers the raw, un-normalized body


@pytest.mark.req("R1", "R11")
def test_validation_failure_not_stored(client: TestClient, app: FastAPI) -> None:
    bad = job_payload(recipients=[recipient(0, email="bad")])
    assert post(client, bad).status_code == 422
    assert job_count(app) == 0
    fixed = job_payload(recipients=[recipient(0)])
    assert post(client, fixed).status_code == 202  # the client fixes the body and reuses the key
    assert post_raw(client, b"{bad", {"Idempotency-Key": "k2"}).status_code == 422
    assert post(client, job_payload(n=1), key="k2").status_code == 202


@pytest.mark.req("R1", "R11")
# Raise the concurrent-submission limit (default 2) so the test exercises the idempotency race, not the limit.
@pytest.mark.parametrize("settings_overrides", [{"max_concurrent_submissions": 8}])
def test_concurrent_same_key_creates_one_job(client: TestClient, app: FastAPI) -> None:
    payload = job_payload(n=3)
    barrier = threading.Barrier(8)
    results: list[tuple[int, str]] = []
    lock = threading.Lock()

    def submit() -> None:
        barrier.wait()
        resp = post(client, payload, key="burst")
        with lock:
            results.append((resp.status_code, resp.json()["id"]))

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 8
    assert sorted(code for code, _ in results) == [200] * 7 + [202]
    assert len({job_id for _, job_id in results}) == 1
    assert job_count(app) == 1


@pytest.mark.req("R1", "R11")
def test_hash_matches_stored_request_json(client: TestClient, app: FastAPI) -> None:
    assert post(client, job_payload(n=2)).status_code == 202
    with app.state.engines.read.connect() as conn:
        row = conn.execute(text("SELECT idempotency_key, request_hash, request_json FROM jobs")).one()
    # Keys are stored per client: "<first 16 hex of sha256(token)>:<key>".
    assert row.idempotency_key == f"{hashlib.sha256(TEST_API_TOKEN.encode()).hexdigest()[:16]}:order-42"
    assert row.request_hash == hashlib.sha256(row.request_json.encode("utf-8")).hexdigest()
    post(client, job_payload(n=1), key=None)
    with app.state.engines.read.connect() as conn:
        unkeyed = conn.execute(
            text("SELECT idempotency_key, request_hash FROM jobs WHERE idempotency_key IS NULL")
        ).one()
    assert (unkeyed.idempotency_key, unkeyed.request_hash) == (None, None)


@pytest.mark.req("R1", "R11")
def test_replay_skips_capacity_check(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert post(client, job_payload(n=1)).status_code == 202
    monkeypatch.setattr(service, "count_active_items", lambda _conn: 10**9)  # queue "full" now
    assert post(client, job_payload(n=1)).status_code == 200  # replays are always answered
    assert post(client, job_payload(n=1), key="new").status_code == 503

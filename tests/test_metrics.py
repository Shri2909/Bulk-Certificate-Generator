"""Prometheus metrics: queue gauges read from the database, worker-side histogram and counter."""

from __future__ import annotations

import socket
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    AUTH_HEADERS,
    FakeClock,
    failing_for,
    job_payload,
    post_job,
    recipient,
    spawn_worker,
    wait_until,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import text

from certgen.config import Settings
from certgen.metrics import ITEM_COUNTS_SQL, Metrics
from certgen.rendering import render_certificate
from certgen.worker import Worker

METRICS = "/metrics"


def scrape(client: TestClient) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Parse /metrics into {(sample name, sorted labels): value}."""
    response = client.get(METRICS, headers=AUTH_HEADERS)
    assert response.status_code == 200
    return parse(response.text)


def parse(body: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    return {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(body)
        for sample in family.samples
    }


def gauge(samples: dict[Any, float], name: str, **labels: str) -> float:
    return samples[(name, tuple(sorted(labels.items())))]


def worker_with_metrics(
    app: FastAPI, settings: Settings, clock: FakeClock, renderer: Any, worker_id: str
) -> Worker:
    return Worker(
        app.state.engines,
        app.state.storage,
        settings,
        clock,
        renderer,
        worker_id=worker_id,
        metrics=app.state.metrics,
    )


@pytest.mark.req("R11")
@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": ""},  # the client fixture sends the token by default; blank it for this request
        {"Authorization": "Bearer wrong-token-0123456789"},
        {"Authorization": "Basic test-token-0123456789abcdef"},
        {"Authorization": "Bearer"},
    ],
)
def test_metrics_refuses_a_missing_or_wrong_token(client: TestClient, headers: dict[str, str]) -> None:
    response = client.get(METRICS, headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    assert "certgen_" not in response.text.replace("UNAUTHORIZED", "")  # no metric leaks into the refusal


@pytest.mark.req("R11")
def test_metrics_serves_text_format_with_the_token(client: TestClient) -> None:
    response = client.get(METRICS, headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.req("R6")
def test_queue_gauges_follow_item_states(client: TestClient, app: FastAPI, drain: Any) -> None:
    empty = scrape(client)
    assert gauge(empty, "certgen_queue_depth") == 0
    assert {s for (n, labels) in empty if n == "certgen_items" for _, s in labels} == {
        "rejected",
        "pending",
        "processing",
        "succeeded",
        "failed",
    }  # every status is reported, zero or not

    payload = job_payload([recipient(0), recipient(1), recipient(2), recipient(3, email="not-an-email")])
    assert post_job(client, payload).status_code == 202
    queued = scrape(client)
    assert gauge(queued, "certgen_queue_depth") == 3
    assert gauge(queued, "certgen_items", status="pending") == 3
    assert gauge(queued, "certgen_items", status="rejected") == 1

    drain(failing_for({"Recipient 1"}))  # one recipient always fails; the retry delay keeps it pending
    after = scrape(client)
    assert gauge(after, "certgen_items", status="succeeded") == 2
    assert gauge(after, "certgen_items", status="pending") == 1
    assert gauge(after, "certgen_queue_depth") == 1


@pytest.mark.req("R6")
def test_claimed_item_counts_as_processing_not_queued(
    client: TestClient, app: FastAPI, settings: Settings, fake_clock: FakeClock
) -> None:
    post_job(client, job_payload(n=2))
    worker = worker_with_metrics(app, settings, fake_clock, render_certificate, "w")
    assert worker.claim_next() is not None
    samples = scrape(client)
    assert gauge(samples, "certgen_items", status="processing") == 1
    assert gauge(samples, "certgen_queue_depth") == 1


@pytest.mark.req("R11")
def test_render_histogram_counts_every_attempt(
    client: TestClient, app: FastAPI, settings: Settings, fake_clock: FakeClock
) -> None:
    post_job(client, job_payload(n=3))
    worker = worker_with_metrics(app, settings, fake_clock, failing_for({"Recipient 0"}), "w")
    worker.run_until_idle()  # two renders succeed, one raises: all three are timed
    samples = scrape(client)
    assert gauge(samples, "certgen_render_seconds_count") == 3
    assert gauge(samples, "certgen_render_seconds_bucket", le="+Inf") == 3
    assert gauge(samples, "certgen_render_seconds_sum") > 0
    # Buckets are cumulative and a real render is far below the 30 s lease.
    assert gauge(samples, "certgen_render_seconds_bucket", le="30.0") == 3


@pytest.mark.req("R9")
def test_lease_lost_counts_fenced_out_writes(
    client: TestClient, app: FastAPI, settings: Settings, fake_clock: FakeClock
) -> None:
    post_job(client, job_payload(n=1))
    stale = worker_with_metrics(app, settings, fake_clock, render_certificate, "stale")
    current = worker_with_metrics(app, settings, fake_clock, render_certificate, "current")
    assert gauge(scrape(client), "certgen_lease_lost_total") == 0
    old_claim = stale.claim_next()
    fake_clock.advance(30_001)
    new_claim = current.claim_next()
    assert old_claim is not None and new_claim is not None
    stale.record_success(old_claim)  # fenced out: counted
    stale.record_failure(old_claim, RuntimeError("late"))  # also fenced out
    assert gauge(scrape(client), "certgen_lease_lost_total") == 2
    current.process(new_claim)  # the current claimant's write is accepted: not counted
    assert gauge(scrape(client), "certgen_lease_lost_total") == 2


@pytest.mark.req("R11")
def test_metrics_carry_no_recipient_data(client: TestClient, drain: Any) -> None:
    post_job(client, job_payload([recipient(0, name="Zelda Secretname", email="zelda.secret@example.com")]))
    drain()
    body = client.get(METRICS, headers=AUTH_HEADERS).text
    assert "Zelda" not in body and "secret" not in body.lower()


@pytest.mark.req("R11")
def test_scrape_query_reads_a_covering_index(client: TestClient, app: FastAPI) -> None:
    with app.state.engines.read.connect() as conn:
        plan = " | ".join(str(row[-1]) for row in conn.execute(text("EXPLAIN QUERY PLAN " + ITEM_COUNTS_SQL)))
    assert "USING COVERING INDEX" in plan, plan  # an index-only scan: the table rows are never read


@pytest.mark.req("R11")
def test_each_metrics_object_has_its_own_registry() -> None:
    first, second = Metrics(), Metrics()
    first.lease_lost.inc()
    assert gauge(parse(first.render().decode()), "certgen_lease_lost_total") == 1
    assert gauge(parse(second.render().decode()), "certgen_lease_lost_total") == 0


@pytest.mark.req("R11")
@pytest.mark.parametrize("value", [-1, 65_536])
def test_worker_metrics_port_is_range_checked(value: int, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="CERTGEN_WORKER_METRICS_PORT"):
        Settings(data_dir=tmp_path, worker_metrics_port=value)


@pytest.mark.slow
@pytest.mark.req("R11")
def test_standalone_worker_serves_its_own_metrics(client: TestClient, settings: Settings) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    post_job(client, job_payload(n=3))
    proc = spawn_worker(settings.data_dir, CERTGEN_WORKER_METRICS_PORT=str(port))
    try:

        def rendered() -> bool:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=1) as page:
                    body = page.read().decode()
            except OSError:
                return False  # not listening yet
            samples = parse(body)
            return samples.get(("certgen_render_seconds_count", ()), 0) == 3

        assert wait_until(rendered, timeout=20), "worker never reported its three renders"
    finally:
        proc.terminate()
        proc.wait(timeout=15)

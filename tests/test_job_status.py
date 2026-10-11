"""Job status derived from item counts (§5.2)."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import JOBS, FakeClock, fail_n_times, failing_for, job_payload, post_job, recipient
from fastapi.testclient import TestClient

from certgen.status import derive_job_status


def counts(**values: int) -> dict[str, int]:
    base = dict.fromkeys(("rejected", "pending", "processing", "succeeded", "failed"), 0)
    base.update(values)
    return base


@pytest.mark.req("R6", "R17")
@pytest.mark.parametrize(
    ("item_counts", "attempted", "expected"),
    [
        (counts(rejected=2), 0, ("completed_with_errors", 1.0)),  # J1 guard
        (counts(succeeded=3), 3, ("completed", 1.0)),  # J2
        (counts(failed=2), 2, ("failed", 1.0)),  # J3
        (counts(failed=2, rejected=1), 2, ("failed", 1.0)),  # J3 wins over J4
        (counts(succeeded=2, failed=1), 3, ("completed_with_errors", 1.0)),  # J4
        (counts(succeeded=2, rejected=1), 2, ("completed_with_errors", 1.0)),  # J4: rejected rows count
        (counts(pending=3), 0, ("queued", 0.0)),  # J5
        (counts(pending=2, rejected=5), 0, ("queued", 0.0)),  # J5
        (counts(pending=1, processing=1, succeeded=1), 2, ("processing", 0.3333)),  # J6
        (counts(processing=3), 3, ("processing", 0.0)),  # J6
        (counts(pending=3), 1, ("processing", 0.0)),  # J6: an attempt was made (retry delay)
    ],
)
def test_derivation_table(item_counts: dict[str, int], attempted: int, expected: tuple[str, float]) -> None:
    assert derive_job_status(item_counts, attempted) == expected


@pytest.mark.req("R17")
def test_progress_zero_valid_guard() -> None:
    assert derive_job_status(counts(), 0) == ("completed_with_errors", 1.0)
    assert derive_job_status(counts(rejected=1), 0)[1] == 1.0  # no division by zero


@pytest.mark.req("R17")
def test_no_regression_to_queued_during_retry_delay(client: TestClient, drain: Any) -> None:
    job_id = post_job(client, job_payload(n=1)).json()["id"]
    assert client.get(f"{JOBS}/{job_id}").json()["status"] == "queued"
    drain(fail_n_times(1))  # first attempt fails; the item is pending again, waiting out the delay
    body = client.get(f"{JOBS}/{job_id}").json()
    assert body["counts"]["pending"] == 1
    assert body["status"] == "processing"  # not "queued": work has started


@pytest.mark.req("R7")
def test_finished_at_only_when_terminal(client: TestClient, drain: Any, fake_clock: FakeClock) -> None:
    rows = [recipient(0), recipient(1, name="Bad Apple")]
    job_id = post_job(client, job_payload(recipients=rows)).json()["id"]
    renderer = failing_for({"Bad Apple"})
    fake_clock.advance(100)
    drain(renderer)
    body = client.get(f"{JOBS}/{job_id}").json()
    assert body["status"] == "processing"
    assert body["finished_at"] is None
    fake_clock.advance(2_000)
    drain(renderer)
    fake_clock.advance(2_000)
    drain(renderer)  # third attempt fails for good
    body = client.get(f"{JOBS}/{job_id}").json()
    assert body["status"] == "completed_with_errors"
    assert body["finished_at"] == "2026-10-11T08:00:04.100Z"  # the last completion, not the first

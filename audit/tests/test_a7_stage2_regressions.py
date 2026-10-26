"""Stage 2 regression tests: boundaries and bypass routes of each fix, beyond the Stage 1 red tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import JOBS, payload, post_raw
from fastapi.testclient import TestClient

# --- F-02: bounded unknown-field errors ---------------------------------------------------------


def _row_errors(resp: Any, row: int) -> list[dict[str, Any]]:
    return [e for e in resp.json()["errors"] if e["row_index"] == row]


@pytest.mark.parametrize(
    ("n", "listed", "summary"), [(1, 1, None), (10, 10, None), (11, 10, 1), (5000, 10, 4990)]
)
def test_unknown_fields_capped_at_ten_plus_summary(
    client: TestClient, n: int, listed: int, summary: int | None
) -> None:
    junk = {f"k{i:05d}": 0 for i in range(n)}
    rows = [{"name": "A", "email": "a@example.com"}, {"name": "B", "email": "b@example.com", **junk}]
    resp = client.post(JOBS, json=payload(rows))
    assert resp.status_code == 202
    errors = _row_errors(resp, 1)
    named = [e for e in errors if e["field"] is not None]
    assert [e["field"] for e in named] == sorted(junk)[:listed]  # sorted, first ten
    assert all(e["code"] == "UNKNOWN_FIELD" for e in errors)
    if summary is None:
        assert len(errors) == listed
    else:
        assert (
            errors[-1]["field"] is None
            and errors[-1]["message"] == f"{summary} more unknown fields not listed."
        )


def test_unknown_field_cap_applies_to_all_invalid_422_and_dry_run(client: TestClient) -> None:
    junk = {f"k{i:05d}": 0 for i in range(3000)}
    body = payload([{"name": "A", "email": "a@example.com", **junk}])
    for path in (JOBS, JOBS + "?dry_run=true"):
        resp = client.post(path, json=body)
        assert len(resp.json()["errors"]) == 11, path


def test_huge_unknown_key_is_not_repeated_in_the_message(client: TestClient) -> None:
    key = "x" * 1_000_000
    resp = client.post(
        JOBS,
        json=payload(
            [{"name": "A", "email": "a@example.com"}, {"name": "B", "email": "b@example.com", key: 1}]
        ),
    )
    err = _row_errors(resp, 1)[0]
    assert err["field"] == key  # the field still names the key exactly
    assert len(err["message"]) < 100
    assert len(resp.content) < 1.2 * 1_000_000


def test_unknown_field_order_name_email_then_keys(client: TestClient) -> None:
    rows = [{"email": "bad", "zeta": 1, "alpha": 2}, {"name": "Ok", "email": "ok@example.com"}]
    errors = _row_errors(client.post(JOBS, json=payload(rows)), 0)
    assert [(e["field"], e["code"]) for e in errors] == [
        ("name", "REQUIRED"),
        ("email", "INVALID_EMAIL"),
        ("alpha", "UNKNOWN_FIELD"),
        ("zeta", "UNKNOWN_FIELD"),
    ]


def test_worst_case_unknown_field_body_is_not_amplified(client: TestClient, settings: Any) -> None:
    """The Stage 1 attack shape at 1/20 scale: response and stored errors stay far below the request."""
    n = (settings.max_body_bytes // 20) // 13
    row = "{" + ",".join(f'"k{i:07d}":0' for i in range(n)) + "}"
    body = (
        '{"certificate":' + json.dumps(payload()["certificate"]) + ',"recipients":[' + row + "]}"
    ).encode()
    resp = post_raw(client, body)
    assert resp.status_code == 422
    assert len(resp.content) < 4096


# --- F-03: lone surrogates are INVALID_JSON wherever they appear ---------------------------------

LONE = "\\" + "ud800"  # written as concatenation so no escape sequence appears in the source


@pytest.mark.parametrize(
    "body",
    [
        '{"certificate":{"title":"T' + LONE + '","issuer":"I","issued_on":"2026-10-01"},"recipients":[]}',
        '{"certificate":{"title":"T","issuer":"I","issued_on":"2026-10-01"},"recipients":[],"x'
        + LONE
        + '":1}',
        '{"certificate":{"title":"T","issuer":"I","issued_on":"2026-10-01"},"recipients":[["' + LONE + '"]]}',
        '"' + LONE + '"',
    ],
)
def test_lone_surrogate_anywhere_is_invalid_json(client: TestClient, body: str) -> None:
    resp = post_raw(client, body.encode())
    assert resp.status_code == 422
    assert [e["code"] for e in resp.json()["errors"]] == ["INVALID_JSON"]


def test_lone_surrogate_in_retry_body_is_invalid_json(client: TestClient) -> None:
    job_id = client.post(JOBS, json=payload(n=1)).json()["id"]
    body = ('{"k' + LONE + '":1}').encode()
    resp = client.post(f"{JOBS}/{job_id}/retry", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 422
    assert [e["code"] for e in resp.json()["errors"]] == ["INVALID_JSON"]


def test_paired_surrogate_escape_is_still_valid_json(client: TestClient) -> None:
    pair = "\\" + "ud83d" + "\\" + "ude00"  # U+1F600 written as a valid escape pair
    body = (
        '{"certificate":{"title":"T","issuer":"I","issued_on":"2026-10-01"},"recipients":[{"name":"A","email":"a@example.com","x":"'
        + pair
        + '"}]}'
    )
    codes = [e["code"] for e in post_raw(client, body.encode()).json()["errors"]]
    assert "INVALID_JSON" not in codes

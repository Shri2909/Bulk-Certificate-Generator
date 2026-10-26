"""TDD red specification (Stage 1). Every test here encodes the CORRECT behaviour for a confirmed finding
and is expected to FAIL against the audited code, for the reason stated in its docstring.
They are the acceptance contract for Stage 2. Finding IDs refer to audit/AUDIT_REPORT.md.
"""

from __future__ import annotations

import io
import json
import time
import unicodedata
from typing import Any

import pytest
from conftest import JOBS, payload, post_raw
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import text

from certgen import service
from certgen.fonts import SUPPORTED_CHARS
from certgen.layout import NAME, fit_text
from certgen.status import derive_job_status
from certgen.validation import _charset_error, norm

# --- F-01: quadratic-time email validation ------------------------------------------------------


@pytest.mark.red("F-01")
def test_overlong_email_is_rejected_in_bounded_time(client: TestClient) -> None:
    """F-01. A row whose email is 1,000,000 characters must be rejected as INVALID_EMAIL quickly.
    Fails today on the time bound: email-validator's syntax pass is O(n^2) and certgen passes it the
    unbounded string (measured 2.43 s at 512k chars; ~9-10 s at 1M)."""
    email = "a" * 1_000_000 + "@example.com"
    body = json.dumps(payload([{"name": "A", "email": "a@example.com"}, {"name": "B", "email": email}])).encode()
    started = time.perf_counter()
    resp = post_raw(client, body)
    elapsed = time.perf_counter() - started
    assert resp.status_code == 202
    assert [(e["row_index"], e["code"]) for e in resp.json()["errors"]] == [(1, "INVALID_EMAIL")]
    assert elapsed < 2.0, f"validating one over-long email took {elapsed:.1f} s"


# --- F-02: unbounded error amplification ----------------------------------------------------------


@pytest.mark.red("F-02")
def test_unknown_field_errors_are_bounded(client: TestClient, app: FastAPI) -> None:
    """F-02. A row with 50,000 unknown keys must not produce a response or stored error list larger than
    the request (proposed contract: per-row UNKNOWN_FIELD errors capped, e.g. 10 + one summary; the exact
    cap is a Stage-2 product decision). Fails today: one error object per key is returned and persisted."""
    junk = {f"k{i:06d}": 0 for i in range(50_000)}
    body = json.dumps(payload([{"name": "A", "email": "a@example.com"}, {"name": "B", "email": "b@example.com", **junk}])).encode()
    resp = post_raw(client, body)
    assert resp.status_code == 202
    with app.state.engines.read.connect() as conn:
        stored = conn.execute(text("SELECT length(errors) FROM items WHERE row_index = 1")).scalar_one()
    row_errors = [e for e in resp.json()["errors"] if e["row_index"] == 1]
    assert len(row_errors) <= 11, f"{len(row_errors)} errors returned for one row"
    assert len(resp.content) <= 64 * 1024, f"response is {len(resp.content)} bytes for a {len(body)}-byte request"
    assert stored <= 64 * 1024, f"{stored} bytes of errors persisted for one row"


# --- F-03: lone UTF-16 surrogates -> 500 ---------------------------------------------------------

_CERT = '"certificate":{"title":"T","issuer":"I","issued_on":"2026-10-01"}'
SURROGATE_BODIES = {
    "unknown-key, all rows invalid": '{%s,"recipients":[{"name":"A","email":"a@example.com","\\ud800":1}]}' % _CERT,
    "name of rejected row": '{%s,"recipients":[{"name":"A","email":"a@example.com"},{"name":"B\\ud800","email":"b@example.com"}]}'
    % _CERT,
    "email of rejected row": '{%s,"recipients":[{"name":"A","email":"a@example.com"},{"name":"B","email":"b\\ud800@x.com"}]}'
    % _CERT,
    "unknown-key value only": '{%s,"recipients":[{"name":"A","email":"a@example.com"},{"name":"B","email":"b@example.com","x":"\\udfff"}]}'
    % _CERT,
}


@pytest.mark.red("F-03")
@pytest.mark.parametrize("case", sorted(SURROGATE_BODIES))
@pytest.mark.parametrize("variant", ["plain", "idempotency-key", "dry-run"])
def test_lone_surrogate_never_500(client: TestClient, case: str, variant: str) -> None:
    """F-03. JSON may legally contain \\ud800 escapes; json.loads turns them into lone surrogates that
    cannot be UTF-8 encoded. The contract is a 4xx/202 problem response, never 500. Fails today with
    UnicodeEncodeError -> 500 INTERNAL_ERROR (sqlite3 bind, sha256 of canonical JSON, or JSON response encode)."""
    key = "k-" + "".join(ch if ch.isalnum() else "-" for ch in case)  # printable ASCII, no spaces
    headers = {"Idempotency-Key": key} if variant == "idempotency-key" else {}
    path = JOBS + "?dry_run=true" if variant == "dry-run" else JOBS
    resp = post_raw(client, SURROGATE_BODIES[case].encode(), headers, path=path)
    assert resp.status_code in (200, 202, 422), f"{resp.status_code}: {resp.text[:200]}"
    resp.json()  # the body must be valid JSON


# --- F-04: integer overflow in keyset parameter ------------------------------------------------


@pytest.mark.red("F-04")
@pytest.mark.parametrize("after", ["9223372036854775808", "1" + "0" * 30])
def test_items_after_out_of_range_is_422(client: TestClient, after: str) -> None:
    """F-04. `after` beyond SQLite's 64-bit INTEGER must be a 422 INVALID_VALUE on field `after`.
    Fails today: OverflowError in the sqlite3 bind -> 500 INTERNAL_ERROR."""
    job_id = client.post(JOBS, json=payload(n=1)).json()["id"]
    resp = client.get(f"{JOBS}/{job_id}/items", params={"after": after})
    assert resp.status_code == 422, resp.text[:200]
    assert [(e["field"], e["code"]) for e in resp.json()["errors"]] == [("after", "INVALID_VALUE")]


# --- F-05: non-BMP characters accepted; PDF text layer is wrong ---------------------------------


@pytest.mark.red("F-05")
def test_accepted_name_round_trips_through_pdf_text_layer(client: TestClient, drain: Any) -> None:
    """F-05. Either a name with a non-BMP character is rejected (UNSUPPORTED_CHARACTERS, matching the
    README's 'Latin, Greek and Cyrillic' scope), or the PDF's text layer must contain it exactly.
    Fails today: the row is accepted and ReportLab writes the ToUnicode entry `<02> <1F63C>` (odd-length
    hex); readers decode it as U+1F63, so the certificate's text says 'Zoë ὣ Smith'."""
    name = "Zoë \U0001f63c Smith"
    resp = client.post(JOBS, json=payload([{"name": name, "email": "zoe@example.com"}, {"name": "Ann", "email": "ann@example.com"}]))
    assert resp.status_code == 202
    row0 = [e for e in resp.json()["errors"] if e["row_index"] == 0]
    if row0:
        assert row0[0]["code"] == "UNSUPPORTED_CHARACTERS"
        return
    drain()
    job_id = resp.json()["id"]
    item = client.get(f"{JOBS}/{job_id}/items").json()["items"][0]
    pdf = client.get(item["download_url"]).content
    extracted = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert name in extracted, f"text layer has {extracted.splitlines()[2]!r}"


# --- F-07: IntegrityError backstop misclassifies a key violation as an id collision ------------------


@pytest.mark.red("F-07")
def test_key_violation_reaches_replay_backstop(client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """F-07. If the in-transaction key re-check ever misses an existing key, the UNIQUE backstop must turn
    the IntegrityError into a replay (200) as service.create_job documents. Fails today: the message
    'UNIQUE constraint failed: jobs.idempotency_key' contains the substring 'jobs.id', so _insert_job
    retries it as a job-id collision 5 times and raises RuntimeError -> 500."""
    body = payload(n=1)
    assert client.post(JOBS, json=body, headers={"Idempotency-Key": "race"}).status_code == 202
    real = service._job_for_key
    calls = {"n": 0}

    def miss_twice(conn: Any, key: str, fingerprint: str) -> str | None:
        calls["n"] += 1  # 1 = API fast path, 2 = re-check under the write lock; both "lose the race"
        return None if calls["n"] <= 2 else real(conn, key, fingerprint)

    monkeypatch.setattr(service, "_job_for_key", miss_twice)
    resp = client.post(JOBS, json=body, headers={"Idempotency-Key": "race"})
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:160]}"
    assert resp.headers.get("idempotent-replayed") == "true"


# --- F-08: progress reaches 1.0 before the job is done --------------------------------------------


@pytest.mark.red("F-08")
@pytest.mark.parametrize("valid", [20_001, 100_000])
def test_progress_is_below_one_until_terminal(valid: int) -> None:
    """F-08. progress must be < 1.0 while any valid item is unfinished. Fails today: round(done/valid, 4)
    gives 1.0 for 99,999/100,000 (reachable because CERTGEN_MAX_RECIPIENTS allows up to 100,000)."""
    status, progress = derive_job_status({"succeeded": valid - 1, "pending": 1}, attempted=valid - 1)
    assert status == "processing"
    assert progress < 1.0


# --- F-09: body limit rejects valid requests -----------------------------------------------------


def _nfd_4cp_name() -> str:
    chars = sorted(
        (c for c in SUPPORTED_CHARS if len(unicodedata.normalize("NFD", c)) == 4 and _charset_error("n", c) is None and c.islower()),
        key=lambda c: NAME.line_width(c, 1),
    )[:10]
    name = " ".join("".join(chars[(w + i) % len(chars)] for i in range(9)) for w in range(12))[:120].strip()
    assert fit_text(norm(name), NAME) is not None
    return unicodedata.normalize("NFD", name)


@pytest.mark.red("F-09")
@pytest.mark.parametrize("settings_overrides", [{"max_recipients": 1000}])
def test_body_limit_admits_every_valid_request(client: TestClient, settings: Any) -> None:
    """F-09. BLUEPRINT §12: the derived body limit is sized so 'a request within field limits is never 413'd'.
    1,000 rows, each valid after NFC (name 119 chars, email 254 chars), serialized with Python's default
    json.dumps, must be accepted. Fails today with 413: NFD input is ~2,880 bytes/row vs the 2,600 budget."""
    name = _nfd_4cp_name()
    rows = [
        {"name": name, "email": f"x{i:04d}" + "a" * 59 + "@" + ".".join(["b" * 63, "c" * 63, "d" * 57]) + ".org"}
        for i in range(1000)
    ]
    body = json.dumps(payload(rows)).encode()
    resp = post_raw(client, body)
    assert resp.status_code == 202, (
        f"{resp.status_code} for a {len(body)}-byte request of valid rows (limit {settings.max_body_bytes})"
    )

"""Regression tests for the round-1 audit fixes (F-01 ... F-18), moved into the default suite so that
`pytest` alone fails if a later change reverts one of them (audit P8-N3). Ported from audit/tests
(test_a1_red_contract.py, test_a7_stage2_regressions.py, test_a3_integration.py); round-2 fixes are
in test_hardening.py.
"""

from __future__ import annotations

import io
import json
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from conftest import JOBS, certificate_info
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import text

from certgen import service
from certgen.fonts import SUPPORTED_CHARS
from certgen.layout import NAME, fit_text
from certgen.status import derive_job_status
from certgen.validation import _charset_error, norm


def payload(
    recipients: list[Any] | None = None, n: int = 3, certificate: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "certificate": certificate if certificate is not None else certificate_info(),
        "recipients": recipients
        if recipients is not None
        else [{"name": f"Recipient {i}", "email": f"r{i}@example.com"} for i in range(n)],
    }


def post_raw(client: TestClient, body: bytes, headers: dict[str, str] | None = None, path: str = JOBS) -> Any:
    return client.post(path, content=body, headers={"Content-Type": "application/json", **(headers or {})})


# --- F-02: unbounded error amplification ----------------------------------------------------------


@pytest.mark.req("R2", "R15")
def test_unknown_field_errors_are_bounded(client: TestClient, app: FastAPI) -> None:
    """F-02. A row with 50,000 unknown keys must not produce a response or stored error list larger than
    the request (proposed contract: per-row UNKNOWN_FIELD errors capped, e.g. 10 + one summary; the exact
    cap is a Stage-2 product decision). Failed before the fix: one error object per key was returned
    and persisted."""
    junk = {f"k{i:06d}": 0 for i in range(50_000)}
    body = json.dumps(
        payload([{"name": "A", "email": "a@example.com"}, {"name": "B", "email": "b@example.com", **junk}])
    ).encode()
    resp = post_raw(client, body)
    # Round 2 (P8-N1) refuses a row this large outright (422, nothing stored); within the row budget the
    # per-row cap applies. Either way the response and the stored errors stay small.
    assert resp.status_code in (202, 422)
    with app.state.engines.read.connect() as conn:
        stored = conn.execute(
            text("SELECT COALESCE(SUM(length(errors)), 0) FROM items WHERE row_index = 1")
        ).scalar_one()
    row_errors = [e for e in resp.json()["errors"] if e["row_index"] == 1]
    assert len(row_errors) <= 11, f"{len(row_errors)} errors returned for one row"
    assert len(resp.content) <= 64 * 1024, (
        f"response is {len(resp.content)} bytes for a {len(body)}-byte request"
    )
    assert stored <= 64 * 1024, f"{stored} bytes of errors persisted for one row"


# --- F-03: lone UTF-16 surrogates -> 500 ---------------------------------------------------------

_CERT = '"certificate":{"title":"T","issuer":"I","issued_on":"2026-10-01"}'
_OK_ROW = '{"name":"A","email":"a@example.com"}'


def _body(recipients: str) -> str:
    return "{" + _CERT + ',"recipients":[' + recipients + "]}"


SURROGATE_BODIES = {
    "unknown-key, all rows invalid": _body('{"name":"A","email":"a@example.com","\\ud800":1}'),
    "name of rejected row": _body(_OK_ROW + ',{"name":"B\\ud800","email":"b@example.com"}'),
    "email of rejected row": _body(_OK_ROW + ',{"name":"B","email":"b\\ud800@x.com"}'),
    "unknown-key value only": _body(_OK_ROW + ',{"name":"B","email":"b@example.com","x":"\\udfff"}'),
}


@pytest.mark.req("R2", "R15")
@pytest.mark.parametrize("case", sorted(SURROGATE_BODIES))
@pytest.mark.parametrize("variant", ["plain", "idempotency-key", "dry-run"])
def test_lone_surrogate_never_500(client: TestClient, case: str, variant: str) -> None:
    """F-03. JSON may legally contain \\ud800 escapes; json.loads turns them into lone surrogates that
    cannot be UTF-8 encoded. The contract is a 4xx/202 problem response, never 500. Failed before the fix with
    UnicodeEncodeError -> 500 INTERNAL_ERROR (sqlite3 bind, sha256 of canonical JSON, or JSON response
    encode)."""
    key = "k-" + "".join(ch if ch.isalnum() else "-" for ch in case)  # printable ASCII, no spaces
    headers = {"Idempotency-Key": key} if variant == "idempotency-key" else {}
    path = JOBS + "?dry_run=true" if variant == "dry-run" else JOBS
    resp = post_raw(client, SURROGATE_BODIES[case].encode(), headers, path=path)
    assert resp.status_code in (200, 202, 422), f"{resp.status_code}: {resp.text[:200]}"
    resp.json()  # the body must be valid JSON


# --- F-04: integer overflow in keyset parameter ------------------------------------------------


@pytest.mark.req("R10", "R11")
@pytest.mark.parametrize("after", ["9223372036854775808", "1" + "0" * 30])
def test_items_after_out_of_range_is_422(client: TestClient, after: str) -> None:
    """F-04. `after` beyond SQLite's 64-bit INTEGER must be a 422 INVALID_VALUE on field `after`.
    Failed before the fix: OverflowError in the sqlite3 bind -> 500 INTERNAL_ERROR."""
    job_id = client.post(JOBS, json=payload(n=1)).json()["id"]
    resp = client.get(f"{JOBS}/{job_id}/items", params={"after": after})
    assert resp.status_code == 422, resp.text[:200]
    assert [(e["field"], e["code"]) for e in resp.json()["errors"]] == [("after", "INVALID_VALUE")]


# --- F-05: non-BMP characters accepted; PDF text layer is wrong ---------------------------------


@pytest.mark.req("R5")
def test_accepted_name_round_trips_through_pdf_text_layer(client: TestClient, drain: Any) -> None:
    """F-05. Either a name with a non-BMP character is rejected (UNSUPPORTED_CHARACTERS, matching the
    README's 'Latin, Greek and Cyrillic' scope), or the PDF's text layer must contain it exactly.
    Failed before the fix: the row was accepted and ReportLab wrote the ToUnicode entry `<02> <1F63C>`
    (odd-length
    hex); readers decode it as U+1F63, so the certificate's text says 'Zoë ὣ Smith'."""
    name = "Zoë \U0001f63c Smith"
    resp = client.post(
        JOBS,
        json=payload(
            [{"name": name, "email": "zoe@example.com"}, {"name": "Ann", "email": "ann@example.com"}]
        ),
    )
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


@pytest.mark.req("R1")
def test_key_violation_reaches_replay_backstop(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-07. If the in-transaction key re-check ever misses an existing key, the UNIQUE backstop must turn
    the IntegrityError into a replay (200) as service.create_job documents. Failed before the fix: the message
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


@pytest.mark.req("R7")
@pytest.mark.parametrize("valid", [20_001, 100_000])
def test_progress_is_below_one_until_terminal(valid: int) -> None:
    """F-08. progress must be < 1.0 while any valid item is unfinished.
    Failed before the fix: round(done/valid, 4)
    gives 1.0 for 99,999/100,000 (reachable because CERTGEN_MAX_RECIPIENTS allows up to 100,000)."""
    status, progress = derive_job_status({"succeeded": valid - 1, "pending": 1}, attempted=valid - 1)
    assert status == "processing"
    assert progress < 1.0


# --- F-09: body limit rejects valid requests -----------------------------------------------------


def _nfd_4cp_name() -> str:
    chars = sorted(
        (
            c
            for c in SUPPORTED_CHARS
            if len(unicodedata.normalize("NFD", c)) == 4 and _charset_error("n", c) is None and c.islower()
        ),
        key=lambda c: NAME.line_width(c, 1),
    )[:10]
    name = " ".join("".join(chars[(w + i) % len(chars)] for i in range(9)) for w in range(12))[:120].strip()
    assert fit_text(norm(name), NAME) is not None
    return unicodedata.normalize("NFD", name)


@pytest.mark.req("R11", "R15")
@pytest.mark.parametrize("settings_overrides", [{"max_recipients": 1000}])
def test_body_limit_admits_every_valid_request(client: TestClient, settings: Any) -> None:
    """F-09. BLUEPRINT §12: the derived body limit is sized so 'a request within field limits is never 413'd'.
    1,000 rows, each valid after NFC (name 119 chars, email 254 chars), serialized with Python's default
    json.dumps, must be accepted. Failed before the fix with 413: NFD input is ~2,880 bytes/row vs the
    2,600 budget."""
    name = _nfd_4cp_name()
    rows = [
        {
            "name": name,
            "email": f"x{i:04d}" + "a" * 59 + "@" + ".".join(["b" * 63, "c" * 63, "d" * 57]) + ".org",
        }
        for i in range(1000)
    ]
    body = json.dumps(payload(rows)).encode()
    resp = post_raw(client, body)
    assert resp.status_code == 202, (
        f"{resp.status_code} for a {len(body)}-byte request of valid rows (limit {settings.max_body_bytes})"
    )


# --- F-02: bounded unknown-field errors ---------------------------------------------------------


def _row_errors(resp: Any, row: int) -> list[dict[str, Any]]:
    return [e for e in resp.json()["errors"] if e["row_index"] == row]


@pytest.mark.req("R2", "R15")
@pytest.mark.parametrize(
    ("n", "listed", "summary"), [(1, 1, None), (10, 10, None), (11, 10, 1), (300, 10, 290)]
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


@pytest.mark.req("R2", "R15")
def test_unknown_field_cap_applies_to_all_invalid_422_and_dry_run(client: TestClient) -> None:
    junk = {f"k{i:05d}": 0 for i in range(300)}  # within the 4,500 B row budget
    body = payload([{"name": "A", "email": "a@example.com", **junk}])
    for path in (JOBS, JOBS + "?dry_run=true"):
        resp = client.post(path, json=body)
        assert len(resp.json()["errors"]) == 11, path


@pytest.mark.req("R2", "R15")
def test_huge_unknown_key_is_not_repeated_in_the_message(client: TestClient) -> None:
    key = "x" * 4_000  # as long as the 4,500 B row budget allows
    resp = client.post(
        JOBS,
        json=payload(
            [{"name": "A", "email": "a@example.com"}, {"name": "B", "email": "b@example.com", key: 1}]
        ),
    )
    err = _row_errors(resp, 1)[0]
    assert err["field"] == key  # the field still names the key exactly
    assert len(err["message"]) < 100
    assert len(resp.content) < 1.2 * 4_000 + 1_000


@pytest.mark.req("R2", "R15")
def test_unknown_field_order_name_email_then_keys(client: TestClient) -> None:
    rows = [{"email": "bad", "zeta": 1, "alpha": 2}, {"name": "Ok", "email": "ok@example.com"}]
    errors = _row_errors(client.post(JOBS, json=payload(rows)), 0)
    assert [(e["field"], e["code"]) for e in errors] == [
        ("name", "REQUIRED"),
        ("email", "INVALID_EMAIL"),
        ("alpha", "UNKNOWN_FIELD"),
        ("zeta", "UNKNOWN_FIELD"),
    ]


@pytest.mark.req("R2", "R15")
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


@pytest.mark.req("R2", "R15")
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


@pytest.mark.req("R2", "R15")
def test_lone_surrogate_in_retry_body_is_invalid_json(client: TestClient) -> None:
    job_id = client.post(JOBS, json=payload(n=1)).json()["id"]
    body = ('{"k' + LONE + '":1}').encode()
    resp = client.post(f"{JOBS}/{job_id}/retry", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 422
    assert [e["code"] for e in resp.json()["errors"]] == ["INVALID_JSON"]


@pytest.mark.req("R2", "R15")
def test_paired_surrogate_escape_is_still_valid_json(client: TestClient) -> None:
    pair = "\\" + "ud83d" + "\\" + "ude00"  # U+1F600 written as a valid escape pair
    body = (
        '{"certificate":{"title":"T","issuer":"I","issued_on":"2026-10-01"},"recipients":[{"name":"A","email":"a@example.com","x":"'
        + pair
        + '"}]}'
    )
    codes = [e["code"] for e in post_raw(client, body.encode()).json()["errors"]]
    assert "INVALID_JSON" not in codes


# --- F-04: the keyset bound is exact ------------------------------------------------------------


@pytest.mark.req("R10", "R11")
@pytest.mark.parametrize(("after", "status"), [("9223372036854775807", 200), ("9223372036854775808", 422)])
def test_after_accepts_sqlite_max_and_rejects_one_more(client: TestClient, after: str, status: int) -> None:
    job_id = client.post(JOBS, json=payload(n=1)).json()["id"]
    resp = client.get(f"{JOBS}/{job_id}/items", params={"after": after})
    assert resp.status_code == status
    if status == 200:
        assert resp.json()["items"] == []


# --- F-10: Host allowlist ------------------------------------------------------------------------


@pytest.mark.req("R11")
@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost:8000",
        "LOCALHOST:1",
        "127.0.0.1",
        "127.0.0.1:65535",
        "[::1]",
        "[::1]:8000",
        "testserver",
    ],
)
def test_loopback_and_configured_hosts_are_served(client: TestClient, host: str) -> None:
    assert client.get("/healthz", headers={"Host": host}).status_code == 200


@pytest.mark.req("R11")
@pytest.mark.parametrize(
    "host",
    [
        "attacker.example",
        "localhost.attacker.example",
        "127.0.0.1.nip.io",
        "localhost.",
        "[::1].evil",
        "",
        "127.0.0.2",
        "[::2]:80",
        "0.0.0.0",
    ],
)
def test_foreign_hosts_are_refused_with_problem_json(client: TestClient, host: str) -> None:
    resp = client.get("/healthz", headers={"Host": host})
    assert resp.status_code == 400
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["code"] == "INVALID_HOST"
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.req("R11")
def test_host_is_checked_before_the_body_is_read(client: TestClient) -> None:
    resp = client.post(
        JOBS, content=b"x" * 10, headers={"Host": "attacker.example", "Content-Length": str(10**9)}
    )
    assert resp.status_code == 400


@pytest.mark.req("R11")
def test_missing_host_header_is_refused() -> None:
    import asyncio

    from certgen.middleware import HostAllowlistMiddleware

    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover - must not be reached
        raise AssertionError("request without Host reached the app")

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/healthz", "headers": [], "query_string": b""}
    asyncio.run(HostAllowlistMiddleware(app, frozenset({"localhost"}))(scope, receive, send))
    assert sent[0]["status"] == 400


@pytest.mark.req("R11")
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("certs.example.org", ("certs.example.org",)),
        (" A.Example.ORG , 10.0.0.5,[2001:db8::1] ", ("a.example.org", "10.0.0.5", "2001:db8::1")),
        ("", ()),
    ],
)
def test_allowed_hosts_env_is_parsed_and_normalized(raw: str, expected: tuple[str, ...]) -> None:
    from certgen.config import Settings

    assert Settings.from_env({"CERTGEN_ALLOWED_HOSTS": raw}).allowed_hosts == expected


@pytest.mark.req("R11")
@pytest.mark.parametrize(
    "raw", ["*.example.com", "exa mple.org", "http://x.org", "x_y.org", "a..b", "-a.org"]
)
def test_allowed_hosts_env_rejects_patterns_and_urls(raw: str) -> None:
    from certgen.config import Settings

    with pytest.raises(ValueError, match="CERTGEN_ALLOWED_HOSTS"):
        Settings.from_env({"CERTGEN_ALLOWED_HOSTS": raw})


@pytest.mark.req("R11")
def test_configured_host_is_served_and_others_refused(tmp_path: Any) -> None:
    from certgen.config import Settings
    from certgen.main import create_app

    app = create_app(
        Settings(data_dir=tmp_path / "d", embedded_worker=False, allowed_hosts=("certs.example.org",))
    )
    with TestClient(app, base_url="http://certs.example.org") as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/healthz", headers={"Host": "testserver"}).status_code == 400


# --- F-11: requests in progress are limited in the app; idle connections hold no slot ------------


@pytest.mark.req("R11")
def test_request_limit_admits_64_and_refuses_the_65th_with_problem_json() -> None:
    import asyncio

    from certgen.middleware import ConcurrencyLimitMiddleware

    release = asyncio.Event()
    started = 0

    async def slow_app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal started
        started += 1
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    limited = ConcurrencyLimitMiddleware(slow_app, max_requests=64)

    async def call() -> int:
        out: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            out.append(message)

        await limited(
            {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}, receive, send
        )
        return int(out[0]["status"])

    async def scenario() -> tuple[list[int], int, int]:
        first = [asyncio.create_task(call()) for _ in range(64)]
        while started < 64:
            await asyncio.sleep(0)
        refused = await call()  # the 65th while 64 are in progress
        release.set()
        statuses = await asyncio.gather(*first)
        after = await call()  # slots are returned when requests finish
        return list(statuses), refused, after

    statuses, refused, after = asyncio.run(scenario())
    assert statuses == [200] * 64 and refused == 503 and after == 200
    assert limited.in_flight == 0


@pytest.mark.req("R11")
def test_server_busy_is_problem_json_with_retry_after() -> None:
    from certgen.errors import ProblemCode, problem_response

    resp = problem_response(ProblemCode.SERVER_BUSY, "busy")
    assert resp.status_code == 503 and resp.headers["retry-after"] == "1"
    assert resp.media_type == "application/problem+json"


@pytest.mark.req("R11")
def test_slot_is_released_when_the_app_raises() -> None:
    import asyncio

    from certgen.middleware import ConcurrencyLimitMiddleware

    async def boom(scope: Any, receive: Any, send: Any) -> None:
        raise RuntimeError("boom")

    limited = ConcurrencyLimitMiddleware(boom, max_requests=1)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            asyncio.run(limited({"type": "http", "headers": []}, None, None))  # type: ignore[arg-type]
    assert limited.in_flight == 0


# --- F-05: every accepted character has a well-formed text-layer mapping --------------------------


@pytest.mark.req("R5")
def test_every_allowed_character_is_bmp_and_maps_back_in_the_pdf_text_layer() -> None:
    import io
    import re

    from pypdf import PdfReader

    from certgen.fonts import SUPPORTED_CHARS
    from certgen.rendering import CertificateData, render_certificate
    from certgen.validation import _charset_error

    allowed = sorted(c for c in SUPPORTED_CHARS if c != " " and _charset_error("n", c) is None)
    assert allowed and max(ord(c) for c in allowed) <= 0xFFFF
    bad: list[str] = []
    missing: list[str] = []
    for start in range(0, len(allowed), 12):
        chunk = "".join(allowed[start : start + 12])
        pdf = render_certificate(CertificateData("a" * 32, "b" * 32, chunk, None, "T", "I", "2026-10-01"))
        mapped: set[str] = set()
        for font in PdfReader(io.BytesIO(pdf)).pages[0]["/Resources"]["/Font"].values():
            cmap = font.get_object().get("/ToUnicode")
            if cmap is None:
                continue
            data = cmap.get_object().get_data()
            for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):  # the code -> text mappings
                for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
                    if len(dst) % 4:
                        bad.append(f"{src!r}->{dst!r}")
                    else:
                        mapped.add(bytes.fromhex(dst.decode()).decode("utf-16-be"))
        missing += [f"U+{ord(c):04X}" for c in chunk if c not in mapped]
    assert bad == []
    assert missing == []  # every drawn name character is recoverable from the text layer


@pytest.mark.req("R5")
@pytest.mark.parametrize("ch", ["\U0001f63c", "\U0001d356", "\U0001f0d6", "\U00010300"])
def test_non_bmp_characters_are_rejected_as_unsupported(client: TestClient, ch: str) -> None:
    resp = client.post(
        JOBS,
        json=payload(
            [{"name": f"Zoe {ch}", "email": "z@example.com"}, {"name": "B", "email": "b@example.com"}]
        ),
    )
    assert [e["code"] for e in _row_errors(resp, 0)] == ["UNSUPPORTED_CHARACTERS"]


# --- F-18: the README's statement about /docs matches what /docs serves ---------------------------


@pytest.mark.req("R25")
def test_readme_cdn_note_matches_the_docs_page(client: TestClient) -> None:
    from conftest import ROOT

    html = client.get("/docs").text
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "cdn.jsdelivr.net" in html  # if this changes (e.g. self-hosted assets), update the README note
    assert "cdn.jsdelivr.net" in readme and "/openapi.json" in readme
    assert client.get("/openapi.json").status_code == 200


# --- F-07: UNIQUE violations are matched by exact column ------------------------------------------


@pytest.mark.req("R1")
def test_violates_unique_matches_the_exact_column_only() -> None:
    import sqlite3

    from sqlalchemy.exc import IntegrityError

    from certgen.service import violates_unique

    def err(message: str) -> IntegrityError:
        return IntegrityError("INSERT", {}, sqlite3.IntegrityError(message))

    assert violates_unique(err("UNIQUE constraint failed: jobs.id"), "jobs.id")
    assert not violates_unique(err("UNIQUE constraint failed: jobs.idempotency_key"), "jobs.id")
    assert violates_unique(err("UNIQUE constraint failed: jobs.idempotency_key"), "jobs.idempotency_key")
    assert not violates_unique(err("UNIQUE constraint failed: items.job_id, items.row_index"), "items.job_id")


# --- F-09: the per-row body budget covers the true worst legal row --------------------------------


def _escaped(s: str) -> str:
    return "".join(f"\\u{ord(c):04x}" for c in s)


def _worst_row(i: int) -> str:
    import unicodedata

    from certgen.fonts import SUPPORTED_CHARS
    from certgen.layout import NAME, fit_text
    from certgen.validation import _charset_error, norm

    widest_decomposition = sorted(
        (
            c
            for c in SUPPORTED_CHARS
            if c.islower() and _charset_error("n", c) is None and len(unicodedata.normalize("NFD", c)) == 4
        ),
        key=lambda c: NAME.line_width(c, 1),
    )[:10]
    name = " ".join("".join(widest_decomposition[(w + k) % 10] for k in range(9)) for w in range(12))[
        :120
    ].strip()
    assert fit_text(norm(name), NAME) is not None
    email = f"w{i:05d}" + "a" * 58 + "@" + ".".join(["b" * 63, "c" * 63, "d" * 57]) + ".org"
    assert len(email) == 254
    return (
        '{"'
        + _escaped("name")
        + '":"'
        + _escaped(unicodedata.normalize("NFD", name))
        + '","'
        + _escaped("email")
        + '":"'
        + _escaped(email)
        + '"}'
    )


@pytest.mark.req("R11", "R15")
def test_budget_covers_the_derived_worst_row() -> None:
    import unicodedata

    from certgen.config import BODY_BYTES_PER_RECIPIENT
    from certgen.fonts import SUPPORTED_CHARS
    from certgen.validation import _charset_error

    decomposition = max(
        len(unicodedata.normalize("NFD", c)) for c in SUPPORTED_CHARS if _charset_error("n", c) is None
    )
    worst = (
        120 * decomposition * 6 + 254 * 6 + len(_escaped("name") + _escaped("email")) + len('{"":"","":""},')
    )
    assert worst <= BODY_BYTES_PER_RECIPIENT, (worst, BODY_BYTES_PER_RECIPIENT)
    assert len(_worst_row(0)) + 1 <= BODY_BYTES_PER_RECIPIENT


@pytest.mark.req("R11", "R15")
@pytest.mark.parametrize("settings_overrides", [{"max_recipients": 1000}])
def test_fully_escaped_nfd_worst_case_request_is_accepted(client: TestClient, settings: Any) -> None:
    body = (
        '{"certificate":'
        + json.dumps(payload()["certificate"])
        + ',"recipients":['
        + ",".join(_worst_row(i) for i in range(1000))
        + "]}"
    )
    # A fitting name needs spaces (1 code point each), so a real worst row is ~4,250 B: far above the old
    # 2,600 B budget, just under the new 4,500 B one.
    assert len(body) > 1000 * 4_000
    resp = post_raw(client, body.encode())
    assert resp.status_code == 202, (resp.status_code, len(body), settings.max_body_bytes)
    assert resp.json()["errors"] == []


# --- F-14: importing certgen.main does not read the environment -----------------------------------


def _python(code: str, **env: str) -> Any:
    import os
    import subprocess
    import sys

    from conftest import ROOT

    clean = {k: v for k, v in os.environ.items() if not k.startswith("CERTGEN_")}
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**clean, "PYTHONPATH": str(ROOT), **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.req("R13")
def test_import_with_invalid_environment_succeeds_and_is_silent() -> None:
    done = _python("import certgen.main", CERTGEN_PORT="abc")
    assert done.returncode == 0, done.stderr[-300:]
    assert done.stderr == ""  # no settings parsed, so no 'PUBLIC_BASE_URL is not set' warning at import


@pytest.mark.req("R13")
def test_app_attribute_still_reads_and_validates_the_environment() -> None:
    bad = _python("import certgen.main as m; m.app", CERTGEN_PORT="abc")
    assert bad.returncode != 0 and "CERTGEN_PORT" in bad.stderr
    ok = _python(
        "import certgen.main as m; print(m.app.state.settings.port, m.app is m.app)", CERTGEN_PORT="8123"
    )
    assert ok.returncode == 0 and ok.stdout.split() == ["8123", "True"]


@pytest.mark.req("R13")
def test_uvicorn_import_string_resolves_the_app() -> None:
    done = _python(
        "from uvicorn.importer import import_from_string as f; print(type(f('certgen.main:app')).__name__)"
    )
    assert done.returncode == 0 and done.stdout.strip() == "FastAPI"


# --- F-16: tests that kill the Stage 1 surviving mutants M24 and M11 ------------------------------


@pytest.mark.req("R8", "R11")
def test_declared_oversize_body_is_refused_without_reading_it() -> None:
    """Kills M24: with only byte counting, the server would read up to the limit before refusing."""
    import asyncio

    from certgen.middleware import BodySizeLimitMiddleware

    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover - must not be reached
        raise AssertionError("oversized request reached the app")

    async def receive() -> dict[str, Any]:
        raise AssertionError("the body was read although Content-Length already exceeds the limit")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-length", b"101")],
        "query_string": b"",
    }
    asyncio.run(
        BodySizeLimitMiddleware(app, max_body_bytes=100, body_timeout_seconds=30)(scope, receive, send)
    )
    assert sent[0]["status"] == 413


@pytest.mark.req("R8", "R11")
@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX symlinks")
def test_symlinked_file_is_refused_even_without_o_nofollow(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kills M11: where the platform has no O_NOFOLLOW (e.g. Windows), the lstat check is the only defence."""
    from certgen import storage

    monkeypatch.setattr(storage, "O_NOFOLLOW", 0)
    store = storage.Storage(tmp_path / "certificates")
    (tmp_path / "certificates").mkdir()
    job, cert = "a" * 32, "b" * 32
    store.save_atomic(job, cert, b"%PDF-real")
    secret = tmp_path / "secret.pdf"
    # Framed like a PDF, so only the symlink check (not the F-15 damage check) can refuse it.
    secret.write_bytes(b"%PDF-secret outside the data dir\n%%EOF\n")
    path = store.path_for(job, cert)
    path.unlink()
    path.symlink_to(secret)
    assert store.read(job, cert) is None


# --- F-16 / M10: a job directory planted as a symlink is refused on write --------------------------


@pytest.mark.req("R8", "R9")
@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX symlinks")
@pytest.mark.parametrize("target", ["outside the root", "another job's directory"])
def test_symlinked_job_dir_is_refused_on_write(
    client: TestClient, drain: Any, tmp_path: Path, settings: Any, fake_clock: Any, target: str
) -> None:
    """Kills M10: a link to another job's directory resolves inside the root, so only the symlink check
    refuses it (a link outside the root is also refused by the root check)."""
    job_id = client.post(JOBS, json=payload(n=1)).json()["id"]
    outside = tmp_path / "outside" if target == "outside the root" else settings.certificates_dir / ("c" * 32)
    outside.mkdir()
    (settings.certificates_dir / job_id).symlink_to(outside, target_is_directory=True)
    for _ in range(settings.max_attempts):
        drain()
        fake_clock.advance(settings.retry_delay_ms + 1)
    item = client.get(f"{JOBS}/{job_id}/items").json()["items"][0]
    assert item["status"] == "failed" and item["errors"][0]["code"] == "GENERATION_FAILED"
    assert list(outside.iterdir()) == []

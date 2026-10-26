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


# --- F-04: the keyset bound is exact ------------------------------------------------------------


@pytest.mark.parametrize(("after", "status"), [("9223372036854775807", 200), ("9223372036854775808", 422)])
def test_after_accepts_sqlite_max_and_rejects_one_more(client: TestClient, after: str, status: int) -> None:
    job_id = client.post(JOBS, json=payload(n=1)).json()["id"]
    resp = client.get(f"{JOBS}/{job_id}/items", params={"after": after})
    assert resp.status_code == status
    if status == 200:
        assert resp.json()["items"] == []


# --- F-10: Host allowlist ------------------------------------------------------------------------


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


def test_host_is_checked_before_the_body_is_read(client: TestClient) -> None:
    resp = client.post(
        JOBS, content=b"x" * 10, headers={"Host": "attacker.example", "Content-Length": str(10**9)}
    )
    assert resp.status_code == 400


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


@pytest.mark.parametrize(
    "raw", ["*.example.com", "exa mple.org", "http://x.org", "x_y.org", "a..b", "-a.org"]
)
def test_allowed_hosts_env_rejects_patterns_and_urls(raw: str) -> None:
    from certgen.config import Settings

    with pytest.raises(ValueError, match="CERTGEN_ALLOWED_HOSTS"):
        Settings.from_env({"CERTGEN_ALLOWED_HOSTS": raw})


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


def test_server_busy_is_problem_json_with_retry_after() -> None:
    from certgen.errors import ProblemCode, problem_response

    resp = problem_response(ProblemCode.SERVER_BUSY, "busy")
    assert resp.status_code == 503 and resp.headers["retry-after"] == "1"
    assert resp.media_type == "application/problem+json"


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


def test_readme_cdn_note_matches_the_docs_page(client: TestClient) -> None:
    from conftest import ROOT

    html = client.get("/docs").text
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "cdn.jsdelivr.net" in html  # if this changes (e.g. self-hosted assets), update the README note
    assert "cdn.jsdelivr.net" in readme and "/openapi.json" in readme
    assert client.get("/openapi.json").status_code == 200

"""Regression tests for fixed defects: error amplification, malformed input, limits, hosts and symlinks."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    JOBS,
    ROOT,
    asgi_call,
    certificate_info,
    http_scope,
    job_payload,
    post_raw,
    unreachable_app,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from certgen import service, storage
from certgen.config import BODY_BYTES_PER_RECIPIENT, Settings
from certgen.errors import ProblemCode, problem_response
from certgen.fonts import SUPPORTED_CHARS
from certgen.layout import NAME, fit_text
from certgen.main import create_app
from certgen.middleware import BodySizeLimitMiddleware, ConcurrencyLimitMiddleware, HostAllowlistMiddleware
from certgen.rendering import CertificateData, render_certificate
from certgen.service import violates_unique
from certgen.status import derive_job_status
from certgen.validation import _charset_error, norm

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")


def _row_errors(resp: Any, row: int) -> list[dict[str, Any]]:
    return [e for e in resp.json()["errors"] if e["row_index"] == row]


# --- Unknown-field errors stay bounded ------------------------------------------------------------


@pytest.mark.req("R2", "R15")
def test_unknown_field_errors_are_bounded(client: TestClient, app: FastAPI) -> None:
    junk = {f"k{i:06d}": 0 for i in range(50_000)}
    rows = [{"name": "A", "email": "a@example.com"}, {"name": "B", "email": "b@example.com", **junk}]
    body = json.dumps(job_payload(rows)).encode()
    resp = post_raw(client, body)
    # A row this large is refused outright (422, nothing stored); a smaller one gets the per-row cap.
    assert resp.status_code in (202, 422)
    with app.state.engines.read.connect() as conn:
        stored = conn.execute(
            text("SELECT COALESCE(SUM(length(errors)), 0) FROM items WHERE row_index = 1")
        ).scalar_one()
    assert len(_row_errors(resp, 1)) <= 11
    assert len(resp.content) <= 64 * 1024, f"response is {len(resp.content)} bytes for {len(body)} sent"
    assert stored <= 64 * 1024


@pytest.mark.req("R2", "R15")
@pytest.mark.parametrize(
    ("n", "listed", "summary"), [(1, 1, None), (10, 10, None), (11, 10, 1), (300, 10, 290)]
)
def test_unknown_fields_capped_at_ten_plus_summary(
    client: TestClient, n: int, listed: int, summary: int | None
) -> None:
    junk = {f"k{i:05d}": 0 for i in range(n)}
    rows = [{"name": "A", "email": "a@example.com"}, {"name": "B", "email": "b@example.com", **junk}]
    resp = client.post(JOBS, json=job_payload(rows))
    assert resp.status_code == 202
    errors = _row_errors(resp, 1)
    named = [e for e in errors if e["field"] is not None]
    assert [e["field"] for e in named] == sorted(junk)[:listed]  # sorted, first ten
    assert all(e["code"] == "UNKNOWN_FIELD" for e in errors)
    if summary is None:
        assert len(errors) == listed
    else:
        assert errors[-1]["field"] is None
        assert errors[-1]["message"] == f"{summary} more unknown fields not listed."


@pytest.mark.req("R2", "R15")
def test_unknown_field_cap_applies_to_all_invalid_422_and_dry_run(client: TestClient) -> None:
    junk = {f"k{i:05d}": 0 for i in range(300)}  # within the 4,500 B row budget
    body = job_payload([{"name": "A", "email": "a@example.com", **junk}])
    for path in (JOBS, JOBS + "?dry_run=true"):
        resp = client.post(path, json=body)
        assert len(resp.json()["errors"]) == 11, path


@pytest.mark.req("R2", "R15")
def test_huge_unknown_key_is_not_repeated_in_the_message(client: TestClient) -> None:
    key = "x" * 4_000  # as long as the 4,500 B row budget allows
    rows = [{"name": "A", "email": "a@example.com"}, {"name": "B", "email": "b@example.com", key: 1}]
    resp = client.post(JOBS, json=job_payload(rows))
    err = _row_errors(resp, 1)[0]
    assert err["field"] == key  # the field still names the key exactly
    assert len(err["message"]) < 100
    assert len(resp.content) < 1.2 * 4_000 + 1_000


@pytest.mark.req("R2", "R15")
def test_unknown_field_order_name_email_then_keys(client: TestClient) -> None:
    rows = [{"email": "bad", "zeta": 1, "alpha": 2}, {"name": "Ok", "email": "ok@example.com"}]
    errors = _row_errors(client.post(JOBS, json=job_payload(rows)), 0)
    assert [(e["field"], e["code"]) for e in errors] == [
        ("name", "REQUIRED"),
        ("email", "INVALID_EMAIL"),
        ("alpha", "UNKNOWN_FIELD"),
        ("zeta", "UNKNOWN_FIELD"),
    ]


@pytest.mark.req("R2", "R15")
def test_worst_case_unknown_field_body_is_not_amplified(client: TestClient, settings: Settings) -> None:
    n = (settings.max_body_bytes // 20) // 13
    row = "{" + ",".join(f'"k{i:07d}":0' for i in range(n)) + "}"
    certificate = json.dumps(certificate_info())
    body = ('{"certificate":' + certificate + ',"recipients":[' + row + "]}").encode()
    resp = post_raw(client, body)
    assert resp.status_code == 422
    assert len(resp.content) < 4096


# --- Lone UTF-16 surrogates ----------------------------------------------------------------------

_CERT = '"certificate":{"title":"T","issuer":"I","issued_on":"2026-10-01"}'
_OK_ROW = '{"name":"A","email":"a@example.com"}'
LONE = "\\" + "ud800"  # concatenated so no escape sequence appears in the source


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
    key = "k-" + "".join(ch if ch.isalnum() else "-" for ch in case)  # printable ASCII, no spaces
    headers = {"Idempotency-Key": key} if variant == "idempotency-key" else {}
    path = JOBS + "?dry_run=true" if variant == "dry-run" else JOBS
    resp = post_raw(client, SURROGATE_BODIES[case].encode(), headers, path=path)
    assert resp.status_code in (200, 202, 422), f"{resp.status_code}: {resp.text[:200]}"
    resp.json()  # the body must be valid JSON


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
    job_id = client.post(JOBS, json=job_payload(n=1)).json()["id"]
    resp = post_raw(client, ('{"k' + LONE + '":1}').encode(), path=f"{JOBS}/{job_id}/retry")
    assert resp.status_code == 422
    assert [e["code"] for e in resp.json()["errors"]] == ["INVALID_JSON"]


@pytest.mark.req("R2", "R15")
def test_paired_surrogate_escape_is_still_valid_json(client: TestClient) -> None:
    pair = "\\" + "ud83d" + "\\" + "ude00"  # U+1F600 written as a valid escape pair
    row = '{"name":"A","email":"a@example.com","x":"' + pair + '"}'
    codes = [e["code"] for e in post_raw(client, _body(row).encode()).json()["errors"]]
    assert "INVALID_JSON" not in codes


# --- Item pagination bound -------------------------------------------------------------------------


@pytest.mark.req("R10", "R11")
@pytest.mark.parametrize(
    ("after", "status"),
    [("9223372036854775807", 200), ("9223372036854775808", 422), ("1" + "0" * 30, 422)],
)
def test_after_accepts_sqlite_max_and_rejects_anything_larger(
    client: TestClient, after: str, status: int
) -> None:
    job_id = client.post(JOBS, json=job_payload(n=1)).json()["id"]
    resp = client.get(f"{JOBS}/{job_id}/items", params={"after": after})
    assert resp.status_code == status, resp.text[:200]
    if status == 200:
        assert resp.json()["items"] == []
    else:
        assert [(e["field"], e["code"]) for e in resp.json()["errors"]] == [("after", "INVALID_VALUE")]


# --- Non-BMP characters and the PDF text layer ---------------------------------------------------


@pytest.mark.req("R5")
@pytest.mark.parametrize("ch", ["\U0001f63c", "\U0001d356", "\U0001f0d6", "\U00010300"])
def test_non_bmp_characters_are_rejected_as_unsupported(client: TestClient, ch: str) -> None:
    rows = [{"name": f"Zoe {ch}", "email": "z@example.com"}, {"name": "B", "email": "b@example.com"}]
    resp = client.post(JOBS, json=job_payload(rows))
    assert [e["code"] for e in _row_errors(resp, 0)] == ["UNSUPPORTED_CHARACTERS"]


@pytest.mark.req("R5")
def test_every_allowed_character_is_bmp_and_maps_back_in_the_pdf_text_layer() -> None:
    allowed = sorted(c for c in SUPPORTED_CHARS if c != " " and _charset_error("n", c) is None)
    assert allowed
    assert max(ord(c) for c in allowed) <= 0xFFFF
    malformed: list[str] = []
    missing: list[str] = []
    for start in range(0, len(allowed), 12):
        chunk = "".join(allowed[start : start + 12])
        pdf = render_certificate(CertificateData("a" * 32, "b" * 32, chunk, None, "T", "I", "2026-10-01"))
        mapped: set[str] = set()
        for font in PdfReader(io.BytesIO(pdf)).pages[0]["/Resources"]["/Font"].values():  # type: ignore[index]
            cmap = font.get_object().get("/ToUnicode")
            if cmap is None:
                continue
            for block in re.findall(rb"beginbfchar(.*?)endbfchar", cmap.get_object().get_data(), re.S):
                for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
                    if len(dst) % 4:
                        malformed.append(f"{src!r}->{dst!r}")
                    else:
                        mapped.add(bytes.fromhex(dst.decode()).decode("utf-16-be"))
        missing += [f"U+{ord(c):04X}" for c in chunk if c not in mapped]
    assert malformed == []
    assert missing == []  # every drawn character is recoverable from the text layer


# --- Idempotency-Key UNIQUE backstop -------------------------------------------------------------


@pytest.mark.req("R1")
def test_key_violation_reaches_replay_backstop(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    body = job_payload(n=1)
    headers = {"Idempotency-Key": "race"}
    assert client.post(JOBS, json=body, headers=headers).status_code == 202
    real = service._job_for_key
    calls = 0

    def miss_twice(conn: Any, key: str, fingerprint: str) -> str | None:
        nonlocal calls
        calls += 1  # call 1 is the API fast path, call 2 the re-check under the write lock
        return None if calls <= 2 else real(conn, key, fingerprint)

    monkeypatch.setattr(service, "_job_for_key", miss_twice)
    resp = client.post(JOBS, json=body, headers=headers)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:160]}"
    assert resp.headers.get("idempotent-replayed") == "true"


@pytest.mark.req("R1")
def test_violates_unique_matches_the_exact_column_only() -> None:
    def err(message: str) -> IntegrityError:
        return IntegrityError("INSERT", {}, sqlite3.IntegrityError(message))

    assert violates_unique(err("UNIQUE constraint failed: jobs.id"), "jobs.id")
    assert not violates_unique(err("UNIQUE constraint failed: jobs.idempotency_key"), "jobs.id")
    assert violates_unique(err("UNIQUE constraint failed: jobs.idempotency_key"), "jobs.idempotency_key")
    assert not violates_unique(err("UNIQUE constraint failed: items.job_id, items.row_index"), "items.job_id")


# --- Progress ------------------------------------------------------------------------------------


@pytest.mark.req("R7")
@pytest.mark.parametrize("valid", [20_001, 100_000])
def test_progress_is_below_one_until_terminal(valid: int) -> None:
    status, progress = derive_job_status({"succeeded": valid - 1, "pending": 1}, attempted=valid - 1)
    assert status == "processing"
    assert progress < 1.0  # naive rounding would report 1.0 at 99,999 of 100,000


# --- Body-size budget ----------------------------------------------------------------------------


def _escaped(s: str) -> str:
    return "".join(f"\\u{ord(c):04x}" for c in s)


def _worst_row(i: int) -> str:
    """The largest legal row: a 120-char name of NFD-widest letters, a 254-char email, every char escaped."""
    widest = sorted(
        (
            c
            for c in SUPPORTED_CHARS
            if c.islower() and _charset_error("n", c) is None and len(unicodedata.normalize("NFD", c)) == 4
        ),
        key=lambda c: NAME.line_width(c, 1),
    )[:10]
    name = " ".join("".join(widest[(w + k) % 10] for k in range(9)) for w in range(12))[:120].strip()
    assert fit_text(norm(name), NAME) is not None
    email = f"w{i:05d}" + "a" * 58 + "@" + ".".join(["b" * 63, "c" * 63, "d" * 57]) + ".org"
    assert len(email) == 254
    nfd_name = unicodedata.normalize("NFD", name)
    return f'{{"{_escaped("name")}":"{_escaped(nfd_name)}","{_escaped("email")}":"{_escaped(email)}"}}'


@pytest.mark.req("R11", "R15")
def test_budget_covers_the_derived_worst_row() -> None:
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
def test_fully_escaped_nfd_worst_case_request_is_accepted(client: TestClient, settings: Settings) -> None:
    rows = ",".join(_worst_row(i) for i in range(1000))
    body = '{"certificate":' + json.dumps(certificate_info()) + ',"recipients":[' + rows + "]}"
    assert len(body) > 1000 * 4_000  # a real worst row is ~4,250 B, far above the old 2,600 B budget
    resp = post_raw(client, body.encode())
    assert resp.status_code == 202, (resp.status_code, len(body), settings.max_body_bytes)
    assert resp.json()["errors"] == []


# --- Host allowlist ------------------------------------------------------------------------------


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
    middleware = HostAllowlistMiddleware(unreachable_app, frozenset({"localhost"}))
    sent = asyncio.run(asgi_call(middleware, http_scope(path="/healthz")))
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
    assert Settings.from_env({"CERTGEN_ALLOWED_HOSTS": raw}).allowed_hosts == expected


@pytest.mark.req("R11")
@pytest.mark.parametrize(
    "raw", ["*.example.com", "exa mple.org", "http://x.org", "x_y.org", "a..b", "-a.org"]
)
def test_allowed_hosts_env_rejects_patterns_and_urls(raw: str) -> None:
    with pytest.raises(ValueError, match="CERTGEN_ALLOWED_HOSTS"):
        Settings.from_env({"CERTGEN_ALLOWED_HOSTS": raw})


@pytest.mark.req("R11")
def test_configured_host_is_served_and_others_refused(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "d", embedded_worker=False, allowed_hosts=("certs.example.org",))
    with TestClient(create_app(settings), base_url="http://certs.example.org") as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/healthz", headers={"Host": "testserver"}).status_code == 400


# --- In-app concurrency limit --------------------------------------------------------------------


@pytest.mark.req("R11")
def test_request_limit_admits_64_and_refuses_the_65th_with_problem_json() -> None:
    release = asyncio.Event()
    started = 0

    async def slow_app(_scope: Any, _receive: Any, send: Any) -> None:
        nonlocal started
        started += 1
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    limited = ConcurrencyLimitMiddleware(slow_app, max_requests=64)

    async def status() -> int:
        return int((await asgi_call(limited, http_scope()))[0]["status"])

    async def scenario() -> tuple[list[int], int, int]:
        first = [asyncio.create_task(status()) for _ in range(64)]
        while started < 64:
            await asyncio.sleep(0)
        refused = await status()  # the 65th while 64 are in progress
        release.set()
        statuses = await asyncio.gather(*first)
        after = await status()  # slots are returned when requests finish
        return list(statuses), refused, after

    statuses, refused, after = asyncio.run(scenario())
    assert statuses == [200] * 64
    assert (refused, after) == (503, 200)
    assert limited.in_flight == 0


@pytest.mark.req("R11")
def test_server_busy_is_problem_json_with_retry_after() -> None:
    resp = problem_response(ProblemCode.SERVER_BUSY, "busy")
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "1"
    assert resp.media_type == "application/problem+json"


@pytest.mark.req("R11")
def test_slot_is_released_when_the_app_raises() -> None:
    async def boom(_scope: Any, _receive: Any, _send: Any) -> None:
        raise RuntimeError("boom")

    limited = ConcurrencyLimitMiddleware(boom, max_requests=1)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            asyncio.run(asgi_call(limited, http_scope()))
    assert limited.in_flight == 0


# --- Body-size limit and symlink defences --------------------------------------------------------


@pytest.mark.req("R8", "R11")
def test_declared_oversize_body_is_refused_without_reading_it() -> None:
    async def receive() -> dict[str, Any]:
        raise AssertionError("the body was read although Content-Length already exceeds the limit")

    middleware = BodySizeLimitMiddleware(unreachable_app, max_body_bytes=100, body_timeout_seconds=30)
    scope = http_scope("POST", headers=[(b"content-length", b"101")])
    sent = asyncio.run(asgi_call(middleware, scope, receive))
    assert sent[0]["status"] == 413


@pytest.mark.req("R8", "R11")
@POSIX_ONLY
def test_symlinked_file_is_refused_even_without_o_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without O_NOFOLLOW (e.g. Windows) the lstat check is the only defence."""
    monkeypatch.setattr(storage, "O_NOFOLLOW", 0)
    store = storage.Storage(tmp_path / "certificates")
    (tmp_path / "certificates").mkdir()
    job, cert = "a" * 32, "b" * 32
    store.save_atomic(job, cert, b"%PDF-real")
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(
        b"%PDF-secret outside the data dir\n%%EOF\n"
    )  # PDF-framed: only the symlink check refuses it
    path = store.path_for(job, cert)
    path.unlink()
    path.symlink_to(secret)
    assert store.read(job, cert) is None


@pytest.mark.req("R8", "R9")
@POSIX_ONLY
@pytest.mark.parametrize("target", ["outside the root", "another job's directory"])
def test_symlinked_job_dir_is_refused_on_write(
    client: TestClient, drain: Any, tmp_path: Path, settings: Settings, fake_clock: Any, target: str
) -> None:
    """A link to another job's directory resolves inside the root, so only the symlink check refuses it."""
    job_id = client.post(JOBS, json=job_payload(n=1)).json()["id"]
    outside = tmp_path / "outside" if target == "outside the root" else settings.certificates_dir / ("c" * 32)
    outside.mkdir()
    (settings.certificates_dir / job_id).symlink_to(outside, target_is_directory=True)
    for _ in range(settings.max_attempts):
        drain()
        fake_clock.advance(settings.retry_delay_ms + 1)
    item = client.get(f"{JOBS}/{job_id}/items").json()["items"][0]
    assert item["status"] == "failed"
    assert item["errors"][0]["code"] == "GENERATION_FAILED"
    assert list(outside.iterdir()) == []


# --- Importing certgen.main does not read the environment ----------------------------------------


def _python(code: str, **env: str) -> subprocess.CompletedProcess[str]:
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
    assert done.stderr == ""  # no settings parsed, so no warnings at import


@pytest.mark.req("R13")
def test_app_attribute_still_reads_and_validates_the_environment() -> None:
    bad = _python("import certgen.main as m; m.app", CERTGEN_PORT="abc")
    assert bad.returncode != 0
    assert "CERTGEN_PORT" in bad.stderr
    ok = _python(
        "import certgen.main as m; print(m.app.state.settings.port, m.app is m.app)", CERTGEN_PORT="8123"
    )
    assert ok.returncode == 0
    assert ok.stdout.split() == ["8123", "True"]


@pytest.mark.req("R13")
def test_uvicorn_import_string_resolves_the_app() -> None:
    done = _python(
        "from uvicorn.importer import import_from_string as f; print(type(f('certgen.main:app')).__name__)"
    )
    assert done.returncode == 0
    assert done.stdout.strip() == "FastAPI"

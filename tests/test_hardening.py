"""Regression tests for the audit findings (audit/AUDIT_REPORT.md, audit/REMEDIATION_REPORT.md).

They live in the default suite so `pytest` alone fails if a later change reverts a fix (audit P8-N3).
"""

from __future__ import annotations

import signal
from pathlib import Path
from typing import Any

import pytest

from certgen import worker


class _StopMain(Exception):
    """Raised by the patched create_schema to leave worker.main() after the check."""


@pytest.mark.req("R9")
def test_worker_installs_signal_handlers_before_touching_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-01: a SIGTERM that arrives while the worker creates its database must stop it gracefully."""
    seen: dict[str, Any] = {}

    def check_handlers(_engines: Any) -> None:
        seen["sigterm"] = signal.getsignal(signal.SIGTERM)
        seen["sigint"] = signal.getsignal(signal.SIGINT)
        raise _StopMain

    monkeypatch.setenv("CERTGEN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(worker, "create_schema", check_handlers)
    before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(_StopMain):
            worker.main()
    finally:
        signal.signal(signal.SIGTERM, before[0])
        signal.signal(signal.SIGINT, before[1])
    assert seen["sigterm"] not in (signal.SIG_DFL, signal.SIG_IGN, None)
    assert seen["sigint"] is not signal.default_int_handler


# --- F-13: the test-only slow-render delay must stay below the lease ------------------------------


@pytest.mark.req("R11")
@pytest.mark.parametrize(("lease", "slow_ms"), [(1, 1000), (2, 2000), (30, 30_000), (30, 60_000)])
def test_slow_render_at_or_above_the_lease_is_refused(lease: int, slow_ms: int) -> None:
    from certgen.config import Settings

    with pytest.raises(ValueError, match="CERTGEN_TEST_SLOW_RENDER_MS"):
        Settings(lease_seconds=lease, test_slow_render_ms=slow_ms)


@pytest.mark.req("R11")
@pytest.mark.parametrize(("lease", "slow_ms"), [(1, 999), (2, 300), (3, 400), (30, 0), (61, 60_000)])
def test_slow_render_below_the_lease_is_accepted(lease: int, slow_ms: int) -> None:
    from certgen.config import Settings

    assert Settings(lease_seconds=lease, test_slow_render_ms=slow_ms).test_slow_render_ms == slow_ms


# --- F-15: a damaged certificate file is treated like a missing one ------------------------------


def _one_certificate(client: Any, app: Any, drain: Any) -> tuple[str, Path]:
    from conftest import job_payload, post_job

    job_id = post_job(client, job_payload(n=1)).json()["id"]
    drain()
    item = client.get(f"/v1/certificate-jobs/{job_id}/items").json()["items"][0]
    return item["certificate_id"], app.state.storage.path_for(job_id, item["certificate_id"])


@pytest.mark.req("R8", "R19")
@pytest.mark.parametrize(
    "damage",
    ["truncated", "no-header", "no-trailer", "empty", "oversized", "oversized-framed"],
)
def test_damaged_certificate_file_is_410(client: Any, app: Any, drain: Any, damage: str) -> None:
    cert, path = _one_certificate(client, app, drain)
    good = path.read_bytes()
    damaged = {
        "truncated": good[: len(good) // 2],
        "no-header": b"garbage" + good[7:],
        "no-trailer": good.replace(b"%%EOF", b"%%XXX"),
        "empty": b"",
        "oversized": good[:-7] + b" " * (2_097_152 + 1) + b"%%EOF\n",
        # exactly one byte over the cap, yet framed like a PDF: only the size limit refuses it
        "oversized-framed": good[:-6] + b" " * (2_097_152 + 1 - len(good)) + b"%%EOF\n",
    }[damage]
    path.write_bytes(damaged)
    resp = client.get(f"/v1/certificates/{cert}")
    assert resp.status_code == 410
    assert resp.json()["code"] == "CERTIFICATE_FILE_MISSING"


@pytest.mark.req("R8", "R19")
def test_archive_marks_a_damaged_file_and_leaves_it_out(client: Any, app: Any, drain: Any) -> None:
    import csv
    import io
    import zipfile

    _cert, path = _one_certificate(client, app, drain)
    path.write_bytes(path.read_bytes()[:100])
    job_id = path.parent.name
    archive = zipfile.ZipFile(io.BytesIO(client.get(f"/v1/certificate-jobs/{job_id}/archive").content))
    assert [n for n in archive.namelist() if n.endswith(".pdf")] == []
    row = next(csv.DictReader(io.StringIO(archive.read("manifest.csv").decode())))
    assert row["error_codes"] == "CERTIFICATE_FILE_MISSING"


@pytest.mark.req("R8", "R19")
def test_intact_certificate_is_still_served(client: Any, app: Any, drain: Any) -> None:
    cert, path = _one_certificate(client, app, drain)
    resp = client.get(f"/v1/certificates/{cert}")
    assert resp.status_code == 200 and resp.content == path.read_bytes()


# --- P8-N1: bounded storage per request and a free-disk guard -------------------------------------


@pytest.mark.req("R11", "R15")
def test_oversized_row_rejects_the_request_and_stores_nothing(client: Any, app: Any) -> None:
    from conftest import JOBS, job_payload, recipient
    from sqlalchemy import text

    from certgen.config import BODY_BYTES_PER_RECIPIENT

    junk = {"x" * (BODY_BYTES_PER_RECIPIENT + 1): 0}
    resp = client.post(JOBS, json=job_payload(recipients=[recipient(0), {**recipient(1), **junk}]))
    assert resp.status_code == 422
    assert [(e["row_index"], e["field"], e["code"]) for e in resp.json()["errors"]] == [
        (1, "recipients", "TOO_LONG")
    ]
    with app.state.engines.read.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one() == 0


@pytest.mark.req("R11", "R15")
def test_row_at_the_legal_maximum_is_still_accepted_as_a_row(client: Any) -> None:
    from conftest import JOBS, job_payload, recipient

    from certgen.config import BODY_BYTES_PER_RECIPIENT

    row = {**recipient(1), "k": "x" * (BODY_BYTES_PER_RECIPIENT - 200)}  # junk, but within the size budget
    resp = client.post(JOBS, json=job_payload(recipients=[recipient(0), row]))
    assert resp.status_code == 202
    assert [e["code"] for e in resp.json()["errors"]] == ["UNKNOWN_FIELD"]


def _low_disk(monkeypatch: pytest.MonkeyPatch, free_bytes: int) -> None:
    import shutil

    from certgen import service

    usage = shutil.disk_usage("/")
    monkeypatch.setattr(service.shutil, "disk_usage", lambda _path: usage._replace(free=free_bytes))


@pytest.mark.req("R11")
def test_new_jobs_are_refused_when_free_disk_is_low(
    client: Any, app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import JOBS, job_payload
    from sqlalchemy import text

    _low_disk(monkeypatch, 1024 * 1024 * 1023)  # 1023 MiB < default 1024 MiB
    resp = client.post(JOBS, json=job_payload(n=1))
    assert resp.status_code == 503 and resp.json()["code"] == "STORAGE_FULL"
    assert resp.headers["retry-after"] == "1"
    with app.state.engines.read.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one() == 0
    assert client.post(JOBS + "?dry_run=true", json=job_payload(n=1)).status_code == 200  # stores nothing


@pytest.mark.req("R11")
def test_retry_is_refused_when_free_disk_is_low(
    client: Any, drain: Any, fake_clock: Any, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import JOBS, failing_for, job_payload

    job_id = client.post(JOBS, json=job_payload(n=1)).json()["id"]
    for _ in range(settings.max_attempts):
        drain(failing_for({"Recipient 0"}))
        fake_clock.advance(settings.retry_delay_ms + 1)
    _low_disk(monkeypatch, 0)
    resp = client.post(f"{JOBS}/{job_id}/retry")
    assert resp.status_code == 503 and resp.json()["code"] == "STORAGE_FULL"


@pytest.mark.req("R11")
@pytest.mark.parametrize("settings_overrides", [{"min_free_disk_mb": 0}])
def test_free_disk_guard_can_be_disabled(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import JOBS, job_payload

    _low_disk(monkeypatch, 0)
    assert client.post(JOBS, json=job_payload(n=1)).status_code == 202


# --- F-01: the email length is checked before email-validator (independent of the row cap) --------


@pytest.mark.req("R2", "R15")
def test_email_validator_never_sees_an_email_longer_than_254(monkeypatch: pytest.MonkeyPatch) -> None:
    from certgen import validation

    calls: list[int] = []
    real = validation.validate_email

    def spy(email: str, **kwargs: Any) -> Any:
        calls.append(len(email))
        return real(email, **kwargs)

    monkeypatch.setattr(validation, "validate_email", spy)
    _email, err = validation._check_email({"email": "a" * 4_000 + "@example.com"}, print_email=True)
    assert err is not None and err.code.value == "INVALID_EMAIL"
    assert calls == []
    validation._check_email({"email": "zoe@example.com"}, print_email=True)
    assert calls == [15]


# --- P8-N2: expensive submissions are bounded ------------------------------------------------------


@pytest.mark.req("R11")
def test_at_most_two_submissions_are_processed_at_once(
    client: Any, app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from conftest import JOBS, job_payload

    from certgen import api

    entered, release = threading.Semaphore(0), threading.Event()
    real = api.validate_request

    def held(*args: Any, **kwargs: Any) -> Any:
        entered.release()
        assert release.wait(20)
        return real(*args, **kwargs)

    monkeypatch.setattr(api, "validate_request", held)
    statuses: list[int] = []
    workers = [
        threading.Thread(
            target=lambda path=path: statuses.append(client.post(path, json=job_payload(n=1)).status_code)
        )
        for path in (JOBS, JOBS + "?dry_run=true")
    ]
    for t in workers:
        t.start()
    assert entered.acquire(timeout=10) and entered.acquire(timeout=10)  # two submissions are being validated
    third = client.post(JOBS, json=job_payload(n=1))
    assert third.status_code == 503 and third.json()["code"] == "SERVER_BUSY"
    assert client.get("/healthz").status_code == 200  # other routes are not affected
    release.set()
    for t in workers:
        t.join(20)
    assert sorted(statuses) == [200, 202]
    assert client.post(JOBS, json=job_payload(n=1)).status_code == 202  # slots are free again


@pytest.mark.req("R11")
def test_body_that_does_not_arrive_in_time_is_408() -> None:
    import asyncio

    from certgen.middleware import BodySizeLimitMiddleware

    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover - must not be reached
        raise AssertionError("an incomplete body reached the app")

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(3600)  # the client never sends the rest
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-length", b"10")],
        "query_string": b"",
    }
    middleware = BodySizeLimitMiddleware(app, max_body_bytes=100, body_timeout_seconds=0.2)
    asyncio.run(asyncio.wait_for(middleware(scope, receive, send), timeout=5))
    assert sent[0]["status"] == 408
    assert b"REQUEST_TIMEOUT" in b"".join(m.get("body", b"") for m in sent)


@pytest.mark.req("R11")
def test_server_uses_a_30_second_body_deadline() -> None:
    from certgen import main
    from certgen.middleware import BodySizeLimitMiddleware

    options = [m.kwargs for m in main.app.user_middleware if m.cls is BodySizeLimitMiddleware]
    assert options and options[0]["body_timeout_seconds"] == 30


@pytest.mark.req("R11")
@pytest.mark.parametrize("settings_overrides", [{"max_recipients": 3, "max_active_items": 3}])
def test_full_queue_is_refused_before_validation(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import JOBS, job_payload

    from certgen import api

    assert client.post(JOBS, json=job_payload(n=3)).status_code == 202  # the queue is now full
    calls: list[int] = []
    real = api.validate_request

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(api, "validate_request", spy)
    resp = client.post(JOBS, json=job_payload(n=1))
    assert resp.status_code == 503 and resp.json()["code"] == "CAPACITY_EXCEEDED"
    assert calls == []  # refused without spending CPU on validation
    assert client.post(JOBS + "?dry_run=true", json=job_payload(n=1)).status_code == 200


# --- F-12: bearer-token authentication by default; Idempotency-Keys scoped per token -------------

TOKEN_A = "token-a-0123456789abcdefghij"
TOKEN_B = "token-b-0123456789abcdefghij"


def _auth_app(tmp_path: Path, **overrides: Any) -> Any:
    from conftest import TESTCLIENT_HOSTS

    from certgen.config import Settings
    from certgen.main import create_app

    values: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "embedded_worker": False,
        "allowed_hosts": TESTCLIENT_HOSTS,
    }
    values.update(overrides)
    return create_app(Settings(**values))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.req("R11")
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token-0123456789"},
        {"Authorization": f"Basic {TOKEN_A}"},
        {"Authorization": "Bearer"},
    ],
)
def test_v1_routes_require_a_valid_bearer_token(tmp_path: Path, headers: dict[str, str]) -> None:
    from conftest import JOBS, job_payload
    from fastapi.testclient import TestClient

    with TestClient(_auth_app(tmp_path, api_tokens=(TOKEN_A,))) as c:
        for method, path in (
            ("POST", JOBS),
            ("GET", f"{JOBS}/{'a' * 32}"),
            ("GET", f"/v1/certificates/{'a' * 32}"),
        ):
            resp = c.request(method, path, json=job_payload(n=1), headers=headers)
            assert resp.status_code == 401, (method, path)
            assert resp.json()["code"] == "UNAUTHORIZED"
            assert resp.headers["www-authenticate"] == "Bearer"


@pytest.mark.req("R11")
def test_token_is_accepted_and_open_routes_stay_open(tmp_path: Path) -> None:
    from conftest import JOBS, job_payload
    from fastapi.testclient import TestClient

    with TestClient(_auth_app(tmp_path, api_tokens=(TOKEN_A, TOKEN_B))) as c:
        assert c.post(JOBS, json=job_payload(n=1), headers=_bearer(TOKEN_A)).status_code == 202
        assert (
            c.post(JOBS, json=job_payload(n=1), headers={"Authorization": f"bearer {TOKEN_B}"}).status_code
            == 202
        )
        for path in ("/healthz", "/docs", "/openapi.json"):
            assert c.get(path).status_code == 200, path


@pytest.mark.req("R11")
def test_unauthenticated_request_is_refused_before_its_body_is_read(tmp_path: Path) -> None:
    from conftest import JOBS
    from fastapi.testclient import TestClient

    with TestClient(_auth_app(tmp_path, api_tokens=(TOKEN_A,))) as c:
        resp = c.post(
            JOBS, content=b"x", headers={"Content-Type": "application/json", "Content-Length": str(10**9)}
        )
        assert resp.status_code == 401


@pytest.mark.req("R1", "R11")
def test_idempotency_keys_are_scoped_per_token(tmp_path: Path) -> None:
    from conftest import JOBS, job_payload
    from fastapi.testclient import TestClient

    with TestClient(_auth_app(tmp_path, api_tokens=(TOKEN_A, TOKEN_B))) as c:
        key = {"Idempotency-Key": "order-42"}
        first = c.post(JOBS, json=job_payload(n=1), headers={**_bearer(TOKEN_A), **key})
        assert first.status_code == 202
        squatter = c.post(JOBS, json=job_payload(n=2), headers={**_bearer(TOKEN_B), **key})
        assert squatter.status_code == 202  # B's key is its own: no 409, no oracle about A's keys
        replay = c.post(JOBS, json=job_payload(n=1), headers={**_bearer(TOKEN_A), **key})
        assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
        assert c.post(JOBS, json=job_payload(n=3), headers={**_bearer(TOKEN_A), **key}).status_code == 409


@pytest.mark.req("R11", "R13")
def test_a_token_file_is_created_once_when_no_token_is_configured(tmp_path: Path) -> None:
    import stat

    from conftest import JOBS, job_payload
    from fastapi.testclient import TestClient

    with TestClient(_auth_app(tmp_path)) as c:
        token_file = tmp_path / "data" / "api-token"
        token = token_file.read_text(encoding="utf-8").strip()
        assert len(token) >= 32 and stat.S_IMODE(token_file.stat().st_mode) == 0o600
        assert c.post(JOBS, json=job_payload(n=1), headers=_bearer(token)).status_code == 202
        assert c.post(JOBS, json=job_payload(n=1)).status_code == 401
    with TestClient(_auth_app(tmp_path)) as c:  # a restart reuses the same token
        assert token_file.read_text(encoding="utf-8").strip() == token
        assert c.post(JOBS, json=job_payload(n=1), headers=_bearer(token)).status_code == 202


@pytest.mark.req("R11")
@pytest.mark.parametrize("raw", ["short", "has space-0123456789abc", "a" * 300, "tokén-0123456789abcdef"])
def test_invalid_configured_tokens_are_refused_at_startup(raw: str) -> None:
    from certgen.config import Settings

    with pytest.raises(ValueError, match="CERTGEN_API_TOKENS"):
        Settings.from_env({"CERTGEN_API_TOKENS": raw})


@pytest.mark.req("R11")
def test_configured_tokens_are_parsed_from_the_environment() -> None:
    from certgen.config import Settings

    assert Settings.from_env({"CERTGEN_API_TOKENS": f" {TOKEN_A} ,{TOKEN_B}"}).api_tokens == (
        TOKEN_A,
        TOKEN_B,
    )


@pytest.mark.req("R11", "R13")
@pytest.mark.slow
@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX signals")
def test_real_server_creates_and_enforces_its_token_file(tmp_path: Path) -> None:
    import os
    import socket
    import subprocess
    import sys
    import urllib.error
    import urllib.request

    from conftest import ROOT, wait_until

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CERTGEN_")}
    env.update(CERTGEN_DATA_DIR=str(tmp_path / "data"), CERTGEN_PORT=str(port), PYTHONPATH=str(ROOT))
    proc = subprocess.Popen([sys.executable, "-m", "certgen"], cwd=ROOT, env=env, stderr=subprocess.PIPE)

    def status(path: str, token: str | None = None) -> int:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            with urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers), timeout=2
            ) as r:
                return int(r.status)
        except urllib.error.HTTPError as err:
            return err.code
        except OSError:
            return 0

    try:
        assert wait_until(lambda: status("/healthz") == 200, timeout=20)
        token = (tmp_path / "data" / "api-token").read_text(encoding="utf-8").strip()
        missing = f"/v1/certificate-jobs/{'a' * 32}"
        assert status(missing) == 401
        assert status(missing, token) == 404  # authenticated: the job simply does not exist
    finally:
        proc.send_signal(__import__("signal").SIGINT)
        _, stderr = proc.communicate(timeout=20)
    assert token not in stderr.decode(errors="replace")  # the token itself is never logged

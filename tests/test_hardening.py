"""Regression tests for hardening: worker signals, damaged files, disk guard, auth, and slow clients."""

from __future__ import annotations

import ast
import asyncio
import csv
import io
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    JOBS,
    ROOT,
    TESTCLIENT_HOSTS,
    asgi_call,
    failing_for,
    http_scope,
    job_count,
    job_payload,
    post_job,
    recipient,
    unreachable_app,
    wait_until,
)
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from certgen import api, main, service, validation, worker
from certgen.config import BODY_BYTES_PER_RECIPIENT, MAX_CONCURRENT_REQUESTS, Settings
from certgen.main import create_app
from certgen.middleware import BodySizeLimitMiddleware, ConcurrencyLimitMiddleware, SendDeadlineMiddleware

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX signals")


# --- Standalone worker ---------------------------------------------------------------------------


class _StopMain(Exception):
    """Raised by the patched create_schema to leave worker.main() after the check."""


@pytest.mark.req("R9")
def test_worker_installs_signal_handlers_before_touching_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SIGTERM that arrives while the worker creates its database must stop it gracefully."""
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


@pytest.mark.req("R11")
@pytest.mark.parametrize(("lease", "slow_ms"), [(1, 1000), (2, 2000), (30, 30_000), (30, 60_000)])
def test_slow_render_at_or_above_the_lease_is_refused(lease: int, slow_ms: int) -> None:
    with pytest.raises(ValueError, match="CERTGEN_TEST_SLOW_RENDER_MS"):
        Settings(lease_seconds=lease, test_slow_render_ms=slow_ms)


@pytest.mark.req("R11")
@pytest.mark.parametrize(("lease", "slow_ms"), [(1, 999), (2, 300), (3, 400), (30, 0), (61, 60_000)])
def test_slow_render_below_the_lease_is_accepted(lease: int, slow_ms: int) -> None:
    assert Settings(lease_seconds=lease, test_slow_render_ms=slow_ms).test_slow_render_ms == slow_ms


# --- Damaged certificate files are treated like missing ones -------------------------------------


def _one_certificate(client: TestClient, app: FastAPI, drain: Any) -> tuple[str, Path]:
    job_id = post_job(client, job_payload(n=1)).json()["id"]
    drain()
    item = client.get(f"{JOBS}/{job_id}/items").json()["items"][0]
    return item["certificate_id"], app.state.storage.path_for(job_id, item["certificate_id"])


@pytest.mark.req("R8", "R19")
@pytest.mark.parametrize(
    "damage", ["truncated", "no-header", "no-trailer", "empty", "oversized", "oversized-framed"]
)
def test_damaged_certificate_file_is_410(client: TestClient, app: FastAPI, drain: Any, damage: str) -> None:
    cert, path = _one_certificate(client, app, drain)
    good = path.read_bytes()
    damaged = {
        "truncated": good[: len(good) // 2],
        "no-header": b"garbage" + good[7:],
        "no-trailer": good.replace(b"%%EOF", b"%%XXX"),
        "empty": b"",
        "oversized": good[:-7] + b" " * (2_097_152 + 1) + b"%%EOF\n",
        # one byte over the cap yet framed like a PDF: only the size limit refuses it
        "oversized-framed": good[:-6] + b" " * (2_097_152 + 1 - len(good)) + b"%%EOF\n",
    }[damage]
    path.write_bytes(damaged)
    resp = client.get(f"/v1/certificates/{cert}")
    assert resp.status_code == 410
    assert resp.json()["code"] == "CERTIFICATE_FILE_MISSING"


@pytest.mark.req("R8", "R19")
def test_archive_marks_a_damaged_file_and_leaves_it_out(client: TestClient, app: FastAPI, drain: Any) -> None:
    _cert, path = _one_certificate(client, app, drain)
    path.write_bytes(path.read_bytes()[:100])
    archive = zipfile.ZipFile(io.BytesIO(client.get(f"{JOBS}/{path.parent.name}/archive").content))
    assert [n for n in archive.namelist() if n.endswith(".pdf")] == []
    row = next(csv.DictReader(io.StringIO(archive.read("manifest.csv").decode())))
    assert row["error_codes"] == "CERTIFICATE_FILE_MISSING"


@pytest.mark.req("R8", "R19")
def test_intact_certificate_is_still_served(client: TestClient, app: FastAPI, drain: Any) -> None:
    cert, path = _one_certificate(client, app, drain)
    resp = client.get(f"/v1/certificates/{cert}")
    assert resp.status_code == 200
    assert resp.content == path.read_bytes()


# --- Bounded storage per request and the free-disk guard -----------------------------------------


@pytest.mark.req("R11", "R15")
def test_oversized_row_rejects_the_request_and_stores_nothing(client: TestClient, app: FastAPI) -> None:
    junk = {"x" * (BODY_BYTES_PER_RECIPIENT + 1): 0}
    resp = client.post(JOBS, json=job_payload(recipients=[recipient(0), {**recipient(1), **junk}]))
    assert resp.status_code == 422
    assert [(e["row_index"], e["field"], e["code"]) for e in resp.json()["errors"]] == [
        (1, "recipients", "TOO_LONG")
    ]
    assert job_count(app) == 0


@pytest.mark.req("R11", "R15")
def test_row_at_the_legal_maximum_is_still_accepted_as_a_row(client: TestClient) -> None:
    row = {**recipient(1), "k": "x" * (BODY_BYTES_PER_RECIPIENT - 200)}  # junk, but within the size budget
    resp = client.post(JOBS, json=job_payload(recipients=[recipient(0), row]))
    assert resp.status_code == 202
    assert [e["code"] for e in resp.json()["errors"]] == ["UNKNOWN_FIELD"]


def _low_disk(monkeypatch: pytest.MonkeyPatch, free_bytes: int) -> None:
    usage = service.shutil.disk_usage("/")
    monkeypatch.setattr(service.shutil, "disk_usage", lambda _path: usage._replace(free=free_bytes))


@pytest.mark.req("R11")
def test_new_jobs_are_refused_when_free_disk_is_low(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _low_disk(monkeypatch, 1024 * 1024 * 1023)  # 1023 MiB < default 1024 MiB
    resp = client.post(JOBS, json=job_payload(n=1))
    assert resp.status_code == 503
    assert resp.json()["code"] == "STORAGE_FULL"
    assert resp.headers["retry-after"] == "1"
    assert job_count(app) == 0
    assert client.post(JOBS + "?dry_run=true", json=job_payload(n=1)).status_code == 200  # stores nothing


@pytest.mark.req("R11")
def test_retry_is_refused_when_free_disk_is_low(
    client: TestClient, drain: Any, fake_clock: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = client.post(JOBS, json=job_payload(n=1)).json()["id"]
    for _ in range(settings.max_attempts):
        drain(failing_for({"Recipient 0"}))
        fake_clock.advance(settings.retry_delay_ms + 1)
    _low_disk(monkeypatch, 0)
    resp = client.post(f"{JOBS}/{job_id}/retry")
    assert resp.status_code == 503
    assert resp.json()["code"] == "STORAGE_FULL"


@pytest.mark.req("R11")
@pytest.mark.parametrize("settings_overrides", [{"min_free_disk_mb": 0}])
def test_free_disk_guard_can_be_disabled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _low_disk(monkeypatch, 0)
    assert client.post(JOBS, json=job_payload(n=1)).status_code == 202


# --- Email length is checked before email-validator ----------------------------------------------


@pytest.mark.req("R2", "R15")
def test_email_validator_never_sees_an_email_longer_than_254(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = validation.validate_email

    def spy(email: str, **kwargs: Any) -> Any:
        calls.append(len(email))
        return real(email, **kwargs)

    monkeypatch.setattr(validation, "validate_email", spy)
    _email, err = validation._check_email({"email": "a" * 4_000 + "@example.com"}, print_email=True)
    assert err is not None
    assert err.code.value == "INVALID_EMAIL"
    assert calls == []
    validation._check_email({"email": "zoe@example.com"}, print_email=True)
    assert calls == [15]


# --- Bounded concurrent submissions --------------------------------------------------------------


@pytest.mark.req("R11")
def test_at_most_two_submissions_are_processed_at_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Semaphore(0), threading.Event()
    real = api.validate_request

    def held(*args: Any, **kwargs: Any) -> Any:
        entered.release()
        assert release.wait(20)
        return real(*args, **kwargs)

    monkeypatch.setattr(api, "validate_request", held)
    statuses: list[int] = []
    threads = [
        threading.Thread(
            target=lambda path=path: statuses.append(client.post(path, json=job_payload(n=1)).status_code)
        )
        for path in (JOBS, JOBS + "?dry_run=true")
    ]
    for t in threads:
        t.start()
    assert entered.acquire(timeout=10)
    assert entered.acquire(timeout=10)  # two submissions are being validated
    third = client.post(JOBS, json=job_payload(n=1))
    assert third.status_code == 503
    assert third.json()["code"] == "SERVER_BUSY"
    assert client.get("/healthz").status_code == 200  # other routes are not affected
    release.set()
    for t in threads:
        t.join(20)
    assert sorted(statuses) == [200, 202]
    assert client.post(JOBS, json=job_payload(n=1)).status_code == 202  # slots are free again


@pytest.mark.req("R11")
@pytest.mark.parametrize("settings_overrides", [{"max_recipients": 3, "max_active_items": 3}])
def test_full_queue_is_refused_before_validation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.post(JOBS, json=job_payload(n=3)).status_code == 202  # the queue is now full
    calls: list[int] = []
    real = api.validate_request

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(api, "validate_request", spy)
    resp = client.post(JOBS, json=job_payload(n=1))
    assert resp.status_code == 503
    assert resp.json()["code"] == "CAPACITY_EXCEEDED"
    assert calls == []  # refused without spending CPU on validation
    assert client.post(JOBS + "?dry_run=true", json=job_payload(n=1)).status_code == 200


# --- Body and response deadlines -----------------------------------------------------------------


@pytest.mark.req("R11")
def test_body_that_does_not_arrive_in_time_is_408() -> None:
    async def receive() -> dict[str, Any]:
        await asyncio.sleep(3600)  # the client never sends the rest
        return {}

    middleware = BodySizeLimitMiddleware(unreachable_app, max_body_bytes=100, body_timeout_seconds=0.2)
    scope = http_scope("POST", headers=[(b"content-length", b"10")])
    sent = asyncio.run(asyncio.wait_for(asgi_call(middleware, scope, receive), timeout=5))
    assert sent[0]["status"] == 408
    assert b"REQUEST_TIMEOUT" in b"".join(m.get("body", b"") for m in sent)


@pytest.mark.req("R11")
def test_server_uses_a_30_second_body_deadline() -> None:
    options = [m.kwargs for m in main.app.user_middleware if m.cls is BodySizeLimitMiddleware]
    assert options
    assert options[0]["body_timeout_seconds"] == 30


async def _stuck_send(message: dict[str, Any]) -> None:
    if message["type"] == "http.response.body":
        await asyncio.sleep(3600)  # the client never reads, so the transport never drains


@pytest.mark.req("R11")
def test_a_response_the_client_does_not_read_is_aborted_and_its_slot_released() -> None:
    async def streaming_app(_scope: Any, _receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:  # an endless download
            await send({"type": "http.response.body", "body": b"x" * 65536, "more_body": True})

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    limited = ConcurrencyLimitMiddleware(
        SendDeadlineMiddleware(streaming_app, send_timeout_seconds=0.2), max_requests=1
    )
    # Returns instead of hanging.
    asyncio.run(asyncio.wait_for(asgi_call(limited, http_scope(), receive, _stuck_send), timeout=5))
    assert limited.in_flight == 0


@pytest.mark.req("R11")
def test_a_streamed_download_the_client_does_not_read_is_aborted() -> None:
    """The same through FastAPI's StreamingResponse, driven the way uvicorn's h11 protocol does."""
    app = FastAPI()

    @app.get("/archive")
    def archive() -> StreamingResponse:
        def chunks() -> Iterator[bytes]:
            while True:
                yield b"x" * 65536

        return StreamingResponse(chunks())

    requested = False

    async def receive() -> dict[str, Any]:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    limited = ConcurrencyLimitMiddleware(
        SendDeadlineMiddleware(app, send_timeout_seconds=0.2), max_requests=1
    )
    scope = http_scope(
        path="/archive",
        headers=[(b"host", b"localhost")],
        asgi={"version": "3.0", "spec_version": "2.3"},
        http_version="1.1",
        scheme="http",
        raw_path=b"/archive",
        root_path="",
        client=("127.0.0.1", 50000),
        server=("127.0.0.1", 8000),
    )
    asyncio.run(asyncio.wait_for(asgi_call(limited, scope, receive, _stuck_send), timeout=5))
    assert limited.in_flight == 0


@pytest.mark.req("R11")
def test_server_uses_a_30_second_send_deadline() -> None:
    options = [m.kwargs for m in main.app.user_middleware if m.cls is SendDeadlineMiddleware]
    assert options == [{"send_timeout_seconds": 30}]


# --- Per-token request slots ---------------------------------------------------------------------


async def _status_and_body(limited: Any, client_id: str | None) -> tuple[int, bytes]:
    """One GET through `limited`, as TokenAuthMiddleware leaves it: client_id in the state, or none."""
    scope = http_scope(state={"client_id": client_id}) if client_id is not None else http_scope()
    sent = await asgi_call(limited, scope)
    return int(sent[0]["status"]), b"".join(m.get("body", b"") for m in sent[1:])


@pytest.mark.req("R11")
def test_one_token_cannot_hold_every_request_slot() -> None:
    release = asyncio.Event()

    async def held(_scope: Any, _receive: Any, send: Any) -> None:
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    limited = ConcurrencyLimitMiddleware(held, max_requests=4, max_per_client=2)

    async def scenario() -> tuple[tuple[int, bytes], list[int], int]:
        mine = [asyncio.create_task(_status_and_body(limited, "a")) for _ in range(2)]
        while limited.in_flight < 2:
            await asyncio.sleep(0)
        # Token "a" already holds its 2, so a third is refused at once (admitted, it would wait forever).
        third = await asyncio.wait_for(_status_and_body(limited, "a"), timeout=5)
        others = [asyncio.create_task(_status_and_body(limited, c)) for c in ("b", None)]
        while limited.in_flight < 4:
            await asyncio.sleep(0)
        release.set()
        statuses = [status for status, _ in await asyncio.gather(*mine, *others)]
        again = (await _status_and_body(limited, "a"))[0]  # its slots come back when its requests finish
        return third, statuses, again

    third, statuses, again = asyncio.run(scenario())
    assert third[0] == 503
    assert b"SERVER_BUSY" in third[1]
    assert statuses == [200, 200, 200, 200]
    assert again == 200
    assert limited.in_flight == 0
    assert limited.per_client == {}


@pytest.mark.req("R11")
def test_token_slot_is_released_when_the_app_raises() -> None:
    async def boom(_scope: Any, _receive: Any, _send: Any) -> None:
        raise RuntimeError("boom")

    limited = ConcurrencyLimitMiddleware(boom, max_requests=4, max_per_client=1)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            asyncio.run(_status_and_body(limited, "a"))
    assert limited.in_flight == 0
    assert limited.per_client == {}


@pytest.mark.req("R11")
def test_requests_per_token_default_is_half_the_request_limit() -> None:
    assert Settings().max_requests_per_client == 32 == MAX_CONCURRENT_REQUESTS // 2


# --- Bearer-token authentication -----------------------------------------------------------------

TOKEN_A = "token-a-0123456789abcdefghij"
TOKEN_B = "token-b-0123456789abcdefghij"


def _auth_app(tmp_path: Path, **overrides: Any) -> FastAPI:
    values: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "embedded_worker": False,
        "allowed_hosts": TESTCLIENT_HOSTS,
        **overrides,
    }
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
    with TestClient(_auth_app(tmp_path, api_tokens=(TOKEN_A, TOKEN_B))) as c:
        assert c.post(JOBS, json=job_payload(n=1), headers=_bearer(TOKEN_A)).status_code == 202
        lowercase_scheme = {"Authorization": f"bearer {TOKEN_B}"}
        assert c.post(JOBS, json=job_payload(n=1), headers=lowercase_scheme).status_code == 202
        for path in ("/healthz", "/docs", "/openapi.json"):
            assert c.get(path).status_code == 200, path


@pytest.mark.req("R11")
def test_unauthenticated_request_is_refused_before_its_body_is_read(tmp_path: Path) -> None:
    with TestClient(_auth_app(tmp_path, api_tokens=(TOKEN_A,))) as c:
        headers = {"Content-Type": "application/json", "Content-Length": str(10**9)}
        assert c.post(JOBS, content=b"x", headers=headers).status_code == 401


@pytest.mark.req("R1", "R11")
def test_idempotency_keys_are_scoped_per_token(tmp_path: Path) -> None:
    with TestClient(_auth_app(tmp_path, api_tokens=(TOKEN_A, TOKEN_B))) as c:
        key = {"Idempotency-Key": "order-42"}
        first = c.post(JOBS, json=job_payload(n=1), headers={**_bearer(TOKEN_A), **key})
        assert first.status_code == 202
        squatter = c.post(JOBS, json=job_payload(n=2), headers={**_bearer(TOKEN_B), **key})
        assert squatter.status_code == 202  # B's key is its own: no 409, no oracle about A's keys
        replay = c.post(JOBS, json=job_payload(n=1), headers={**_bearer(TOKEN_A), **key})
        assert replay.status_code == 200
        assert replay.json()["id"] == first.json()["id"]
        assert c.post(JOBS, json=job_payload(n=3), headers={**_bearer(TOKEN_A), **key}).status_code == 409


@pytest.mark.req("R11", "R13")
def test_a_token_file_is_created_once_when_no_token_is_configured(tmp_path: Path) -> None:
    token_file = tmp_path / "data" / "api-token"
    with TestClient(_auth_app(tmp_path)) as c:
        token = token_file.read_text(encoding="utf-8").strip()
        assert len(token) >= 32
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        assert c.post(JOBS, json=job_payload(n=1), headers=_bearer(token)).status_code == 202
        assert c.post(JOBS, json=job_payload(n=1)).status_code == 401
    with TestClient(_auth_app(tmp_path)) as c:  # a restart reuses the same token
        assert token_file.read_text(encoding="utf-8").strip() == token
        assert c.post(JOBS, json=job_payload(n=1), headers=_bearer(token)).status_code == 202


@pytest.mark.req("R11")
@pytest.mark.parametrize("raw", ["short", "has space-0123456789abc", "a" * 300, "tokén-0123456789abcdef"])
def test_invalid_configured_tokens_are_refused_at_startup(raw: str) -> None:
    with pytest.raises(ValueError, match="CERTGEN_API_TOKENS"):
        Settings.from_env({"CERTGEN_API_TOKENS": raw})


@pytest.mark.req("R11")
def test_configured_tokens_are_parsed_from_the_environment() -> None:
    settings = Settings.from_env({"CERTGEN_API_TOKENS": f" {TOKEN_A} ,{TOKEN_B}"})
    assert settings.api_tokens == (TOKEN_A, TOKEN_B)


@pytest.mark.req("R11", "R13")
@pytest.mark.slow
@POSIX_ONLY
def test_real_server_creates_and_enforces_its_token_file(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CERTGEN_")}
    env.update(CERTGEN_DATA_DIR=str(tmp_path / "data"), CERTGEN_PORT=str(port), PYTHONPATH=str(ROOT))
    proc = subprocess.Popen([sys.executable, "-m", "certgen"], cwd=ROOT, env=env, stderr=subprocess.PIPE)

    def status(path: str, token: str | None = None) -> int:
        headers = _bearer(token) if token else {}
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=2) as r:
                return int(r.status)
        except urllib.error.HTTPError as err:
            return err.code
        except OSError:
            return 0

    try:
        assert wait_until(lambda: status("/healthz") == 200, timeout=20)
        token = (tmp_path / "data" / "api-token").read_text(encoding="utf-8").strip()
        missing = f"{JOBS}/{'a' * 32}"
        assert status(missing) == 401
        assert status(missing, token) == 404  # authenticated: the job simply does not exist
    finally:
        proc.send_signal(signal.SIGINT)
        _, stderr = proc.communicate(timeout=20)
    assert token not in stderr.decode(errors="replace")  # the token itself is never logged


# --- Runtime checks must survive `python -O` -----------------------------------------------------


@pytest.mark.req("R21")
@pytest.mark.parametrize(
    "path", [*sorted((ROOT / "certgen").glob("*.py")), ROOT / "scripts" / "check_live_demo.py"]
)
def test_runtime_checks_are_not_assert_statements(path: Path) -> None:
    """`python -O` strips assert statements, so shipped code must use explicit checks."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)] == []

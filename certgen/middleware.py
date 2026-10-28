"""The only async code in certgen (§3): pure ASGI middleware and the lifespan adapter.

Everything else is synchronous so FastAPI runs it in its threadpool and CPU-heavy validation or
SQLite waits never stall the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from certgen.auth import TokenStore
from certgen.config import normalize_host
from certgen.errors import ProblemCode, problem_response

SyncLifespan = Callable[[Any], AbstractContextManager[None]]
log = logging.getLogger("certgen.middleware")


def async_lifespan(sync_lifespan: SyncLifespan) -> Callable[[Any], Any]:
    """Adapt a synchronous context manager to Starlette's async lifespan protocol."""

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        with sync_lifespan(app):
            yield

    return lifespan


class HostAllowlistMiddleware:
    """Refuse requests whose Host header is not an allowed name, with 400 INVALID_HOST.

    The API has no authentication and trusts its loopback bind. A web page can defeat that with DNS
    rebinding (its own name resolving to 127.0.0.1), but the browser then sends that name as Host,
    which this check rejects (audit F-10).
    """

    def __init__(self, app: ASGIApp, allowed_hosts: frozenset[str]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"host"), None)
        if host is None or normalize_host(_strip_port(host)) not in self.allowed_hosts:
            response = problem_response(
                ProblemCode.INVALID_HOST, "The Host header names a host this server does not serve."
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


BRACKETED_HOST = re.compile(r"(\[[^\]]*\])(?::[0-9]+)?")


def _strip_port(host: str) -> str:
    """'name:8000' -> 'name'; '[::1]:8000' -> '[::1]'; anything else (e.g. '[::1].evil') unchanged."""
    value = host.strip()
    if value.startswith("["):
        match = BRACKETED_HOST.fullmatch(value)
        return match.group(1) if match else value
    name, sep, port = value.rpartition(":")
    return name if sep and port.isdigit() and ":" not in name else value


class TokenAuthMiddleware:
    """Require `Authorization: Bearer <token>` on routes under `prefix`, with 401 UNAUTHORIZED (audit F-12).

    Runs before any request slot is taken or body is buffered, so unauthenticated clients cost almost
    nothing. A valid token's client id is put in the request state; it scopes Idempotency-Keys.
    """

    def __init__(self, app: ASGIApp, store: TokenStore, prefix: str) -> None:
        self.app = app
        self.store = store
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _route_path(scope).startswith(self.prefix):
            await self.app(scope, receive, send)
            return
        header = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"authorization"), "")
        scheme, _, token = header.partition(" ")
        client = (
            self.store.client_for(token.strip()) if scheme.lower() == "bearer" and token.strip() else None
        )
        if client is None:
            detail = "Send a valid API token as 'Authorization: Bearer <token>'."
            await problem_response(ProblemCode.UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"})(
                scope, receive, send
            )
            return
        scope.setdefault("state", {})["client_id"] = client
        await self.app(scope, receive, send)


class _ClientTooSlow(Exception):
    """A send did not complete in time: the client stopped reading the response."""


class SendDeadlineMiddleware:
    """Abort a response that the client stops reading: every send must finish within the deadline.

    A send blocks while the client's receive window is full. Without a deadline, a client that never
    reads a large download held its request slot forever, and 64 of them locked everyone out.
    Clients that keep reading, however slowly, are unaffected (audit, round 2). The server then closes
    the connection, which frees the slot (StreamingResponse re-raises the error out of its task group).
    """

    def __init__(self, app: ASGIApp, send_timeout_seconds: float) -> None:
        self.app = app
        self.send_timeout_seconds = send_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_deadline(message: Message) -> None:
            try:
                await asyncio.wait_for(send(message), timeout=self.send_timeout_seconds)
            except TimeoutError:
                raise _ClientTooSlow from None

        try:
            await self.app(scope, receive, send_with_deadline)
        except _ClientTooSlow:
            log.warning("slow_client_aborted")  # no request data: the path is client-controlled


class ConcurrencyLimitMiddleware:
    """At most `max_requests` HTTP requests in progress, and at most `max_per_client` of them for one API
    token; more get 503 SERVER_BUSY (problem+json).

    Counts requests, not connections: a TCP connection that never completes its request headers holds
    no slot, so idle sockets cannot lock other clients out (audit F-11). A request whose body is still
    arriving does hold one, which keeps the memory spent on buffered bodies bounded. The per-token share
    means one token holder cannot take every slot; requests without a token (/healthz) count only
    towards the total (audit R2-O1).
    """

    def __init__(self, app: ASGIApp, max_requests: int, max_per_client: int | None = None) -> None:
        self.app = app
        self.max_requests = max_requests
        self.max_per_client = max_requests if max_per_client is None else max_per_client
        self.in_flight = 0
        self.per_client: dict[str, int] = {}
        self._lock = threading.Lock()  # TestClient may drive the app from more than one event loop

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client: str | None = scope.get("state", {}).get("client_id")  # set by TokenAuthMiddleware
        with self._lock:
            held = self.per_client.get(client, 0) if client is not None else 0
            client_full = held >= self.max_per_client
            admitted = self.in_flight < self.max_requests and not client_full
            if admitted:
                self.in_flight += 1
                if client is not None:
                    self.per_client[client] = held + 1
        if not admitted:
            detail = (
                "This API token already has the most requests in progress it may have; retry shortly."
                if client_full
                else "Too many requests are in progress; retry shortly."
            )
            await problem_response(ProblemCode.SERVER_BUSY, detail)(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            with self._lock:
                self.in_flight -= 1
                if client is not None:
                    left = self.per_client[client] - 1
                    if left:
                        self.per_client[client] = left
                    else:
                        del self.per_client[client]


class NoStoreMiddleware:
    """Adds Cache-Control: no-store to every response (§6.1): job state changes; certificates are private."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_header)


class BodySizeLimitMiddleware:
    """Reject bodies over `max_body_bytes` with 413 before anything parses them (§7.1 step 1).

    Content-Length is checked first; chunked bodies are counted while buffering, so a client cannot
    bypass the limit by omitting the header. The whole body must arrive within `body_timeout_seconds`,
    else 408: a client trickling its upload cannot hold its request slot indefinitely (audit P8-N2).
    The buffered body is then replayed to the app.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int, body_timeout_seconds: float) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.body_timeout_seconds = body_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self._declared_length(scope) > self.max_body_bytes:
            await self._reject(scope, receive, send, ProblemCode.PAYLOAD_TOO_LARGE)
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.body_timeout_seconds
        body = bytearray()
        while True:
            try:
                message = await asyncio.wait_for(receive(), timeout=max(deadline - loop.time(), 0))
            except TimeoutError:
                await self._reject(scope, receive, send, ProblemCode.REQUEST_TIMEOUT)
                return
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > self.max_body_bytes:
                await self._reject(scope, receive, send, ProblemCode.PAYLOAD_TOO_LARGE)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()  # after the body, only a disconnect can arrive

        await self.app(scope, replay, send)

    @staticmethod
    def _declared_length(scope: Scope) -> int:
        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return 0  # the server rejects a malformed header; counting still applies
        return 0

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, code: ProblemCode) -> None:
        detail = {
            ProblemCode.PAYLOAD_TOO_LARGE: "The request body is too large.",
            ProblemCode.REQUEST_TIMEOUT: "The request body did not arrive in time.",
        }[code]
        await problem_response(code, detail)(scope, receive, send)


class SubmissionLimitMiddleware:
    """At most `max_submissions` job submissions (POST /v1/certificate-jobs, dry runs included) at once.

    A submission is the expensive request: its body is buffered, parsed and validated (seconds of CPU
    and up to ~1 GB of memory at the body limit). Excess ones get 503 SERVER_BUSY before their body is
    read, so polling and downloads stay responsive (audit P8-N2).
    """

    def __init__(self, app: ASGIApp, path: str, max_submissions: int) -> None:
        self.app = app
        self.path = path
        self.max_submissions = max_submissions
        self.in_progress = 0
        self._lock = threading.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST" or _route_path(scope) != self.path:
            await self.app(scope, receive, send)
            return
        with self._lock:
            admitted = self.in_progress < self.max_submissions
            if admitted:
                self.in_progress += 1
        if not admitted:
            detail = "Too many job submissions are being processed; retry shortly."
            await problem_response(ProblemCode.SERVER_BUSY, detail)(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            with self._lock:
                self.in_progress -= 1


def _route_path(scope: Scope) -> str:
    """The path the app routes on: without the mount prefix when the app is mounted (e.g. under /api)."""
    path: str = scope.get("path", "")
    root: str = scope.get("root_path", "")
    return path[len(root) :] if root and path.startswith(root) else path

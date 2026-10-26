"""The only async code in certgen (§3): pure ASGI middleware and the lifespan adapter.

Everything else is synchronous so FastAPI runs it in its threadpool and CPU-heavy validation or
SQLite waits never stall the event loop.
"""

from __future__ import annotations

import re
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from certgen.config import normalize_host
from certgen.errors import ProblemCode, problem_response

SyncLifespan = Callable[[Any], AbstractContextManager[None]]


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


class ConcurrencyLimitMiddleware:
    """At most `max_requests` HTTP requests in progress; more get 503 SERVER_BUSY (problem+json).

    Counts requests, not connections: a TCP connection that never completes its request headers holds
    no slot, so idle sockets cannot lock other clients out (audit F-11). A request whose body is still
    arriving does hold one, which keeps the memory spent on buffered bodies bounded.
    """

    def __init__(self, app: ASGIApp, max_requests: int) -> None:
        self.app = app
        self.max_requests = max_requests
        self.in_flight = 0
        self._lock = threading.Lock()  # TestClient may drive the app from more than one event loop

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with self._lock:
            admitted = self.in_flight < self.max_requests
            if admitted:
                self.in_flight += 1
        if not admitted:
            response = problem_response(
                ProblemCode.SERVER_BUSY, "Too many requests are in progress; retry shortly."
            )
            await response(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            with self._lock:
                self.in_flight -= 1


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
    bypass the limit by omitting the header. The buffered body is then replayed to the app.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self._declared_length(scope) > self.max_body_bytes:
            await self._reject(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > self.max_body_bytes:
                await self._reject(scope, receive, send)
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
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = problem_response(ProblemCode.PAYLOAD_TOO_LARGE, "The request body is too large.")
        await response(scope, receive, send)

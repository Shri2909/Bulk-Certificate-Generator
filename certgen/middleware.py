"""The only async code in certgen (§3): pure ASGI middleware and the lifespan adapter.

Everything else is synchronous so FastAPI runs it in its threadpool and CPU-heavy validation or
SQLite waits never stall the event loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from certgen.errors import ProblemCode, problem_response

SyncLifespan = Callable[[Any], AbstractContextManager[None]]


def async_lifespan(sync_lifespan: SyncLifespan) -> Callable[[Any], Any]:
    """Adapt a synchronous context manager to Starlette's async lifespan protocol."""

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        with sync_lifespan(app):
            yield

    return lifespan


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

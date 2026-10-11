"""The only async code in certgen (§3): pure ASGI middleware and the lifespan adapter.

Everything else is synchronous so FastAPI runs it in its threadpool and CPU-heavy validation or
SQLite waits never stall the event loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

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

"""Pure ASGI middleware setting client_utc_offset ContextVar from query string."""

from __future__ import annotations

from urllib.parse import parse_qs

from starlette.types import ASGIApp, Receive, Scope, Send

from trackseerr.list_index import client_utc_offset, parse_utc_offset


class ClientTzOffsetMiddleware:
    """Pure ASGI middleware reading ``tz_offset`` query parameter into ``client_utc_offset``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_qs = scope.get("query_string", b"")
        qs = raw_qs.decode("latin-1") if isinstance(raw_qs, bytes) else str(raw_qs)
        params = parse_qs(qs)
        vals = params.get("tz_offset")
        value = vals[0] if vals else None

        token = client_utc_offset.set(parse_utc_offset(value))
        try:
            await self.app(scope, receive, send)
        finally:
            client_utc_offset.reset(token)

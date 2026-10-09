"""ASGI middleware for the two-tier (DMZ) security model.

* ``SignedBodyMiddleware`` (all roles) hashes the body of gateway-signed requests so the
  synchronous auth dependency can verify the signature without consuming the body.
* ``GatewayGuardMiddleware`` (active only when ``role == "gateway"``) is deny-by-default:
  every ``/api/*`` path outside ``GATEWAY_LOCAL_ALLOWLIST`` / ``GATEWAY_FORWARD_ALLOWLIST``
  returns 404, and forward-listed paths are relayed to core through ``CoreClient.proxy``
  as the signed-in user.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

import httpx
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from trackseerr.api.sessions import start_session
from trackseerr.boot import boot_state
from trackseerr.clients.core_client import SESSION_ISSUED_AT_KEY, CoreClient
from trackseerr.internal_auth import HEADER_SIGNATURE

logger = logging.getLogger(__name__)

MAX_PROXY_BODY_BYTES = 1024 * 1024  # 1 MiB cap on bodies relayed to core
MAX_SIGNED_BODY_BYTES = 1024 * 1024  # core-side cap for signed requests (1 MiB)

READ = frozenset({"GET", "HEAD"})
ALL_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})

# A template segment "{}" matches exactly one path segment; a trailing "/**" matches the
# prefix itself and anything below it. Everything else must match exactly.

# Endpoints the gateway serves from its own process (no forwarding by the middleware).
GATEWAY_LOCAL_ALLOWLIST: tuple[tuple[frozenset[str], str], ...] = (
    (READ, "/api/health"),  # container/orchestrator health check
    (READ, "/api/health/ready"),  # readiness probe (503 while starting)
    (frozenset({"POST"}), "/api/auth/plex/pin"),  # sign-in: start the Plex PIN flow
    (frozenset({"POST"}), "/api/auth/plex/verify"),  # sign-in: claim the PIN and create the session
    (frozenset({"POST"}), "/api/auth/local/login"),  # sign-in: local account (core verifies the credentials)
    (frozenset({"POST"}), "/api/auth/logout"),  # sign-out
    (READ, "/api/auth/me"),  # session user + tier
    (READ, "/api/users/me"),  # profile, permissions and quota telemetry
    (READ, "/api/discovery/trending"),  # discovery: trending
    (READ, "/api/discovery/new-releases"),  # discovery: new releases
    (READ, "/api/discovery/search"),  # discovery: search
    (READ, "/api/discovery/album/{}"),  # discovery: album detail
    (READ, "/api/discovery/track/{}"),  # discovery: track detail
    (READ, "/api/discovery/artist/{}"),  # discovery: artist detail
    (READ, "/api/discovery/artist-profile"),  # discovery: artist profile (user-scoped; availability resolved on core)
    (frozenset({"POST"}), "/api/requests"),  # create a request (route forwards to core)
    (frozenset({"POST"}), "/api/requests/batch"),  # batch create (route forwards to core)
    (READ, "/api/library/availability"),  # availability lookup (route forwards to core)
)

# Pre-login endpoints the gateway relays to core as the SERVICE principal (no user session exists yet).
# Rate-limited per end-user IP at the gateway.
GATEWAY_FORWARD_SERVICE_ALLOWLIST: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"GET", "POST"}), "/api/auth/invite/{}"),  # invite / password-reset link
    (READ, "/api/system/media-server"),  # which sign-in methods to offer (the media server lives on core)
)

# User-scoped endpoints the gateway relays to core as the signed-in user.
GATEWAY_FORWARD_ALLOWLIST: tuple[tuple[frozenset[str], str], ...] = (
    (READ, "/api/requests"),  # list the signed-in user's own requests (state lives on core)
    (frozenset({"DELETE"}), "/api/requests/{}"),  # cancel own pending request (core checks ownership)
    (ALL_METHODS, "/api/playlists/**"),  # user's own sync playlists (core enforces ownership)
    (frozenset({"GET", "POST"}), "/api/issues"),  # list own issues / report an issue
    (READ, "/api/issues/{}"),  # view own issue (core returns 404 for others'); also /unread-count
    (READ, "/api/issues/{}/comments"),  # read the discussion on an own issue
    (frozenset({"POST"}), "/api/issues/{}/comments"),  # comment on an own issue
    (frozenset({"POST"}), "/api/issues/{}/seen"),  # mark an own issue as seen
    (frozenset({"POST"}), "/api/issues/{}/status"),  # reopen / close an own issue (core enforces the transitions)
    (ALL_METHODS, "/api/plex-playlists/**"),  # user's own Plex playlists (core enforces ownership)
    (ALL_METHODS, "/api/mixes/**"),  # tailored mixes
    (ALL_METHODS, "/api/account/**"),  # own profile, quotas, password and MFA (core enforces)
    (frozenset({"GET", "PUT"}), "/api/scrobbles/config"),  # own scrobble settings
    (READ, "/api/scrobbles/listens"),  # own listen history
    (READ, "/api/scrobbles/lastfm/auth-url"),  # begin Last.fm connect
    (frozenset({"POST"}), "/api/scrobbles/lastfm/complete"),  # finish Last.fm connect under the user's own session
    (READ, "/api/scrobbles/lastfm/callback"),  # Last.fm return; core's 303 Location is passed through
)

# Paths a wildcard above would otherwise forward although core serves them to admins only (answered 404 here).
GATEWAY_FORWARD_DENYLIST: tuple[tuple[frozenset[str], str], ...] = (
    (ALL_METHODS, "/api/issues/open-count"),  # admin nav badge; "/api/issues/{}" would match it
)


# Static SPA route for the invite / reset link. It lives outside /api so the gateway guard passes it
# through to the app; the route itself accepts only this exact shape (no catch-all).
INVITE_TOKEN_PATH_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")

_INVITE_PATH_RE = re.compile(r"(/api/auth/invite/)[^/?]+")


def _redact_path(path: str) -> str:
    """Hides invite/reset tokens so they never reach the logs."""
    return _INVITE_PATH_RE.sub(r"\1<token>", path)


def _template_matches(template: str, path: str) -> bool:
    if template.endswith("/**"):
        prefix = template[:-3]
        return path == prefix or path.startswith(prefix + "/")
    t_parts = template.split("/")
    p_parts = path.split("/")
    if len(t_parts) != len(p_parts):
        return False
    for t, p in zip(t_parts, p_parts):
        if t == "{}":
            if not p:
                return False
        elif t != p:
            return False
    return True


def _allowed(table: tuple[tuple[frozenset[str], str], ...], method: str, path: str) -> bool:
    return any(method in methods and _template_matches(tpl, path) for methods, tpl in table)


def _path_is_clean(raw_path: str) -> bool:
    """Rejects traversal, backslashes, empty segments and encoded separators."""
    decoded = unquote(raw_path)
    if "\\" in decoded or "\x00" in decoded or "//" in decoded or "//" in raw_path:
        return False
    segments = decoded.split("/") + raw_path.lower().split("/")
    if any(seg in ("..", ".", "%2e", "%2e%2e") for seg in segments):
        return False
    return "%2f" not in raw_path.lower() and "%5c" not in raw_path.lower()


def _is_gateway_role(scope: Scope) -> bool:
    fastapi_app = scope.get("app")
    if fastapi_app is None:
        return os.getenv("ROLE", "all-in-one").lower().strip() == "gateway"
    config = _resolve_config(fastapi_app)
    return (getattr(config, "role", None) or os.getenv("ROLE", "all-in-one")).lower().strip() == "gateway"


def _safe_location(value: str, core_url: Any) -> Any:
    """Returns a relative Location safe to hand to a browser, or None to drop it."""
    if value.startswith("/") and not value.startswith("//") and "\\" not in value and "\n" not in value and "\r" not in value:
        return value
    try:
        loc = urlsplit(value)
        core = urlsplit(str(core_url or ""))
    except ValueError:
        return None
    if loc.scheme and loc.netloc and core.scheme and loc.scheme == core.scheme and loc.netloc.lower() == core.netloc.lower():
        path = loc.path or "/"
        if path.startswith("/") and not path.startswith("//") and "\\" not in path:
            return path + (f"?{loc.query}" if loc.query else "") + (f"#{loc.fragment}" if loc.fragment else "")
    return None


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def _resolve_config(app: Any) -> Any:
    from trackseerr.api.dependencies import get_config

    override = app.dependency_overrides.get(get_config)
    return (override or get_config)()


def _resolve_db(app: Any) -> Any:
    from trackseerr.api.dependencies import get_db

    override = app.dependency_overrides.get(get_db)
    return (override or get_db)()


STARTUP_RETRY_AFTER_SECONDS = "5"
# Applied to every response, including the startup 503s produced by the outermost StartupGateMiddleware
# (which sits outside the ``add_security_headers`` HTTP middleware in app.py).
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-eval' 'unsafe-inline' "
        "https://cdn.tailwindcss.com https://unpkg.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "media-src 'self' https: data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

_STARTUP_OPEN_PATHS = frozenset({"/api/health", "/api/health/ready"})


def _startup_role_is_gateway(scope: Scope) -> bool:
    """Role as ``create_app`` recorded it on ``app.state`` (what ``/api/health`` uses), else the environment."""
    state = getattr(scope.get("app"), "state", None)
    config = getattr(state, "config", None)
    role = getattr(config, "role", None) or os.getenv("ROLE", "all-in-one")
    return str(role).lower().strip() == "gateway"


class StartupGateMiddleware:
    """Answers 503 + ``Retry-After`` on every ``/api`` route except health while the process is booting.

    Static assets and the SPA shell are not gated, so the browser can load the "starting" screen.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not boot_state.ready:
            path = str(scope.get("path", ""))
            if (path == "/api" or path.startswith("/api/")) and path.rstrip("/") not in _STARTUP_OPEN_PATHS:
                body = {
                    **boot_state.snapshot(detailed=not _startup_role_is_gateway(scope)),
                    "detail": "TrackSeerr is starting",
                }
                await JSONResponse(
                    body,
                    status_code=503,
                    headers={"Retry-After": STARTUP_RETRY_AFTER_SECONDS, **SECURITY_HEADERS},
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


class SignedBodyMiddleware:
    """Buffers and hashes the body of requests carrying ``X-TS-Signature``, then replays it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        if _is_gateway_role(scope) and any(k.startswith(b"x-ts-") for k in headers):
            # A gateway only ever originates X-TS-* headers; inbound ones are forgery attempts.
            logger.warning("Rejected inbound X-TS-* headers on gateway role")
            await JSONResponse({"detail": "Forbidden headers"}, status_code=400)(scope, receive, send)
            return
        if HEADER_SIGNATURE.lower().encode("latin-1") not in headers:
            await self.app(scope, receive, send)
            return

        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > MAX_SIGNED_BODY_BYTES
            except ValueError:
                await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
                return
            if too_big:
                await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
                return

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > MAX_SIGNED_BODY_BYTES:
                await JSONResponse({"detail": "Request body too large"}, status_code=413)(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        scope.setdefault("state", {})["ts_body_sha256"] = hashlib.sha256(body).hexdigest()

        replayed = False

        async def replay() -> dict[str, Any]:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class GatewayGuardMiddleware:
    """Deny-by-default allowlist and user-scoped forwarding for ``role == "gateway"``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path != "/api" and not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        fastapi_app = scope.get("app")
        config = _resolve_config(fastapi_app) if fastapi_app is not None else None
        role = ((getattr(config, "role", None) or os.getenv("ROLE", "all-in-one")).lower().strip()) if config else ""
        if role != "gateway":
            await self.app(scope, receive, send)
            return

        raw_bytes = scope.get("raw_path")
        raw_path = raw_bytes.decode("latin-1") if raw_bytes else path
        raw_path = raw_path.split("?", 1)[0]
        if not _path_is_clean(raw_path):
            await _not_found()(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        match_path = path.rstrip("/") or "/"
        if _allowed(GATEWAY_LOCAL_ALLOWLIST, method, match_path):
            await self.app(scope, receive, send)
            return
        if _allowed(GATEWAY_FORWARD_SERVICE_ALLOWLIST, method, match_path):
            response = await self._forward(scope, receive, config, fastapi_app, method, raw_path, service=True)
            await response(scope, receive, send)
            return
        if _allowed(GATEWAY_FORWARD_ALLOWLIST, method, match_path) and not _allowed(
            GATEWAY_FORWARD_DENYLIST, method, match_path
        ):
            response = await self._forward(scope, receive, config, fastapi_app, method, raw_path)
            await response(scope, receive, send)
            return

        await _not_found()(scope, receive, send)

    async def _forward(
        self,
        scope: Scope,
        receive: Receive,
        config: Any,
        app: Any,
        method: str,
        raw_path: str,
        service: bool = False,
    ) -> Response:
        request = Request(scope, receive)
        log_path = _redact_path(raw_path)

        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_PROXY_BODY_BYTES:
                    return JSONResponse({"detail": "Request body too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_PROXY_BODY_BYTES:
                return JSONResponse({"detail": "Request body too large"}, status_code=413)
            chunks.append(chunk)
        body = b"".join(chunks)

        user: Optional[dict[str, Any]] = None
        if service:
            limited = await run_in_threadpool(self._invite_rate_limited, request, app, config)
            if limited:
                return JSONResponse(
                    {"detail": "Too many attempts. Please try again later."},
                    status_code=429,
                    headers={"Retry-After": "900"},
                )
        else:
            try:
                user = await run_in_threadpool(self._session_user, request, app, config)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

        core_url = getattr(config, "trackseerr_core_url", None)
        secret = getattr(config, "internal_core_secret", None)
        if not core_url or not secret:
            logger.error("Gateway cannot forward %s %s: TRACKSEERR_CORE_URL / INTERNAL_CORE_SECRET not set", method, log_path)
            return JSONResponse({"detail": "TrackSeerr Core is not configured"}, status_code=503)

        client = CoreClient(core_url=core_url, secret=secret)
        query = scope.get("query_string", b"").decode("latin-1")
        try:
            relayed = await run_in_threadpool(
                client.proxy,
                method,
                raw_path,
                query,
                body,
                (
                    None
                    if user is None
                    else {
                        "id": user["id"],
                        "username": user.get("username"),
                        SESSION_ISSUED_AT_KEY: user.get(SESSION_ISSUED_AT_KEY),
                    }
                ),
                request.headers.get("content-type"),
            )
        except ValueError as exc:
            logger.error("Gateway could not sign %s %s: %s", method, log_path, exc)
            return JSONResponse({"detail": "Request could not be forwarded"}, status_code=400)
        except httpx.HTTPError as exc:
            logger.error("Gateway proxy to core failed for %s %s: %s", method, log_path, type(exc).__name__)
            return JSONResponse({"detail": "Unable to communicate with TrackSeerr Core engine"}, status_code=502)

        headers: dict[str, str] = {}
        for k, v in relayed.headers.items():
            if k.lower() != "location":
                continue
            safe = _safe_location(v, core_url)
            if safe is None:
                logger.warning("Dropped non-relative redirect Location from core for %s %s", method, log_path)
            else:
                headers["Location"] = safe
        content_type = relayed.headers.get("Content-Type")
        body_out = relayed.body
        out = Response(
            content=body_out,
            status_code=relayed.status_code,
            headers=headers,
            media_type=content_type,
        )
        if user is not None and method == "POST" and raw_path.rstrip("/") == "/api/account/password":
            reissued = await run_in_threadpool(self._reissue_after_password_change, request, app, config, user, relayed)
            if reissued is not None:
                return reissued
        return out

    @staticmethod
    def _reissue_after_password_change(
        request: Request, app: Any, config: Any, user: dict[str, Any], relayed: Any
    ) -> Optional[Response]:
        """Core revoked every session; mint this browser a fresh gateway session and strip the marker."""
        if relayed.status_code != 200:
            return None
        try:
            data = json.loads(relayed.body or b"{}")
        except ValueError:
            return None
        if not isinstance(data, dict) or not data.get("reissue_session"):
            return None
        floor = data.get("session_floor_us")
        public = {k: v for k, v in data.items() if k not in ("reissue_session", "session_floor_us")}
        out = JSONResponse(public, status_code=200)
        db = _resolve_db(app)
        db.delete_user_sessions(user["id"])
        start_session(
            db, config, request, out, {**user, "is_admin": False},
            floor_us=int(floor) if isinstance(floor, int) else 0,
        )
        return out

    @staticmethod
    def _invite_rate_limited(request: Request, app: Any, config: Any) -> bool:
        """Records one invite-endpoint hit for the end user's IP; True once over 10 per 15 minutes."""
        from trackseerr.api.dependencies import get_client_ip
        from trackseerr.local_login import check_rate, throttle_key

        db = _resolve_db(app)
        key = throttle_key("invite", get_client_ip(request, config))
        db.record_login_attempt(key, max_rows=11)
        return check_rate(db, key, 11)

    @staticmethod
    def _session_user(request: Request, app: Any, config: Any) -> dict[str, Any]:
        from trackseerr.api.dependencies import get_current_user

        return get_current_user(request, db=_resolve_db(app), config=config)


__all__ = [
    "GATEWAY_FORWARD_ALLOWLIST",
    "GATEWAY_FORWARD_DENYLIST",
    "GATEWAY_FORWARD_SERVICE_ALLOWLIST",
    "GATEWAY_LOCAL_ALLOWLIST",
    "GatewayGuardMiddleware",
    "MAX_PROXY_BODY_BYTES",
    "SignedBodyMiddleware",
]

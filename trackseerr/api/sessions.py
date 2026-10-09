"""Session issuance shared by every login path (Plex PIN, local password, gateway re-issue)."""

from __future__ import annotations

import time
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import Response

from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.storage import Database

COOKIE_NAME = "session_token"


def cookie_is_secure(request: Request) -> bool:
    return (request.url.scheme == "https") or (request.headers.get("x-forwarded-proto", "").lower() == "https")


def start_session(
    db: Database,
    config: Config,
    request: Request,
    response: Response,
    user: dict[str, Any],
    *,
    floor_us: int = 0,
) -> str:
    """Creates a signed session for ``user``, stores it, and sets the HttpOnly cookie on ``response``.

    ``floor_us`` is the user's ``sessions_revoked_at`` (epoch microseconds). The session is stamped
    strictly after it so a gateway whose clock lags core's cannot issue an already-revoked session.
    """
    secret_key = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret_key,
    )
    issued_us = max(int(time.time() * 1_000_000), int(floor_us) + 1) if floor_us else None
    db.create_session(session_id=token, user_id=user["id"], issued_at_us=issued_us)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=cookie_is_secure(request),
    )
    return token


def request_session_token(request: Request) -> Optional[str]:
    token = request.cookies.get(COOKIE_NAME)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    return token or None

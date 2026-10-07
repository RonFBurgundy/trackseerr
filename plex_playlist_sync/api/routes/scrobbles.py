"""Per-user scrobbling API: Plex webhook, Last.fm connect flow, ListenBrainz and admin management."""

import hmac
import json
import logging
import os
from email import policy
from email.parser import BytesParser
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from plex_playlist_sync.api.dependencies import (
    get_config,
    get_current_user,
    get_db,
    require_admin,
)
from plex_playlist_sync.api.schemas.scrobbles import (
    AuthUrl,
    Listen,
    ScrobbleConfig,
    ServerConfig,
    WebhookStatus,
    WebhookUrl,
)
from plex_playlist_sync.clients.scrobbler import (
    LastFmError,
    ListenBrainzClient,
    ListenBrainzError,
)
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import UserListen, UserScrobbleConfig
from plex_playlist_sync.scrobbling import (
    build_lastfm_client,
    forward_listen,
    get_cached_admin_username,
    get_lastfm_credentials,
    resolve_plex_user,
    send_now_playing,
)
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()

NOT_CONFIGURED_DETAIL = "Last.fm is not configured by the server admin"
MAX_WEBHOOK_BYTES = 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_url(request: Request, db: Database, config: Config) -> str:
    general = db.get_general_settings()
    base = (general.get("application_url") or config.application_url or "").strip().rstrip("/")
    if base:
        return base
    role = (config.role or os.getenv("ROLE", "all-in-one")).lower().strip()
    if role == "core":
        # On core, request.base_url is the LAN address; Last.fm must call back through the public gateway.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="APPLICATION_URL must be set to the public gateway URL",
        )
    return str(request.base_url).rstrip("/")


def _request_base_url(request: Request) -> str:
    """The URL this request reached (the admin's LAN URL to core), never APPLICATION_URL.

    Plex must reach the webhook on the LAN, not through the internet-facing hostname.
    """
    return str(request.base_url).rstrip("/")


def _safe_forward_url(raw: Optional[str]) -> Optional[str]:
    """Only same-origin relative paths survive; anything else is dropped."""
    if not raw:
        return None
    value = raw.strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None
    if any(ord(ch) < 32 for ch in value):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    return value


def _with_query(url: str, **extra: str) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend(extra.items())
    return urlunsplit(("", "", parts.path or "/", urlencode(query), parts.fragment))


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)


def _public_config(row: dict[str, Any], username: Optional[str]) -> dict[str, Any]:
    return UserScrobbleConfig.from_row(row, username=username).to_dict()


def _config_for_user(db: Database, user_id: str, username: Optional[str]) -> dict[str, Any]:
    row = db.get_scrobble_config(user_id) or {"user_id": user_id, "scrobbling_enabled": True}
    return _public_config(row, username)


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}{'•' * 8}{key[-4:]}"


def _is_admin(user: dict[str, Any]) -> bool:
    return bool(user.get("is_admin"))


def _optional_user(
    request: Request,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> Optional[dict[str, Any]]:
    try:
        return get_current_user(request, db=db, config=config)
    except HTTPException:
        return None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ScrobbleConfigUpdate(BaseModel):
    scrobbling_enabled: Optional[bool] = None
    listenbrainz_token: Optional[str] = None
    unlink_lastfm: bool = False


class AdminScrobbleConfigUpdate(ScrobbleConfigUpdate):
    lastfm_username: Optional[str] = None
    lastfm_session_key: Optional[str] = None


class ServerConfigUpdate(BaseModel):
    lastfm_api_key: Optional[str] = None
    lastfm_api_secret: Optional[str] = None
    plex_history_poll_minutes: Optional[int] = Field(default=None, ge=0, le=1440)


def _apply_config_update(
    db: Database, user_id: str, body: ScrobbleConfigUpdate, admin: bool
) -> None:
    fields: dict[str, Any] = {}
    sent = body.model_fields_set
    if body.scrobbling_enabled is not None:
        fields["scrobbling_enabled"] = body.scrobbling_enabled
    if body.unlink_lastfm:
        fields["lastfm_username"] = None
        fields["lastfm_session_key"] = None
    if "listenbrainz_token" in sent:
        token = (body.listenbrainz_token or "").strip()
        if not token:
            fields["listenbrainz_token"] = None
            fields["listenbrainz_username"] = None
        else:
            try:
                lb_user = ListenBrainzClient().validate_token(token)
            except ListenBrainzError as exc:
                logger.warning("ListenBrainz token validation failed: %s", exc)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not reach ListenBrainz"
                ) from exc
            if not lb_user:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid ListenBrainz token"
                )
            fields["listenbrainz_token"] = token
            fields["listenbrainz_username"] = lb_user
    if admin and isinstance(body, AdminScrobbleConfigUpdate):
        if "lastfm_username" in sent:
            fields["lastfm_username"] = (body.lastfm_username or "").strip() or None
        if "lastfm_session_key" in sent:
            fields["lastfm_session_key"] = (body.lastfm_session_key or "").strip() or None
    db.upsert_scrobble_config(user_id, **fields)


# ---------------------------------------------------------------------------
# Plex webhook
# ---------------------------------------------------------------------------


def _extract_webhook_payload(content_type: str, body: bytes) -> dict[str, Any]:
    """Parse a Plex webhook body (multipart/form-data with a ``payload`` part, or JSON) with the stdlib."""
    ctype = content_type.lower()
    raw_json: Optional[bytes] = None
    if ctype.startswith("multipart/"):
        message = BytesParser(policy=policy.HTTP).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1", "replace") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
        )
        if message.is_multipart():
            for part in message.iter_parts():
                if part.get_param("name", header="content-disposition") == "payload":
                    raw_json = part.get_payload(decode=True)
                    break
        if raw_json is None:
            raise ValueError("multipart body has no 'payload' part")
    else:
        raw_json = body
    try:
        payload = json.loads(raw_json.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    return payload


@router.post("/plex", response_model=WebhookStatus, response_model_exclude_unset=True, summary="Plex webhook receiver (token auth only)")
async def plex_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str = Query(default=""),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, str]:
    expected = db.get_plex_webhook_secret()
    if not token or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook token")

    too_large = HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Payload too large")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_WEBHOOK_BYTES:
                raise too_large
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length") from exc
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_WEBHOOK_BYTES:
            raise too_large
        chunks.append(chunk)
    body = b"".join(chunks)
    try:
        payload = _extract_webhook_payload(request.headers.get("content-type", ""), body)
    except ValueError as exc:
        logger.warning("Rejected malformed Plex webhook: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed webhook payload") from exc

    event = payload.get("event")
    meta = payload.get("Metadata") if isinstance(payload.get("Metadata"), dict) else {}
    if event not in ("media.scrobble", "media.play", "media.resume") or meta.get("type") != "track":
        return {"status": "ignored"}

    artist = meta.get("originalTitle") or meta.get("grandparentTitle")
    title = meta.get("title")
    if not artist or not title:
        return {"status": "ignored"}

    account = payload.get("Account") if isinstance(payload.get("Account"), dict) else {}
    user = resolve_plex_user(
        db, account.get("id"), account.get("title"), admin_username=get_cached_admin_username(db)
    )
    if user is None:
        return {"status": "ignored"}

    album = meta.get("parentTitle")
    duration = meta.get("duration")
    if event in ("media.play", "media.resume"):
        background_tasks.add_task(
            send_now_playing, db, user["id"], str(artist), str(title), album, duration, config
        )
        return {"status": "ok"}

    listen_id = db.insert_listen(
        user["id"],
        artist=str(artist),
        title=str(title),
        album=album,
        rating_key=str(meta["ratingKey"]) if meta.get("ratingKey") not in (None, "") else None,
        duration_ms=duration if isinstance(duration, int) else None,
        source="plex_webhook",
    )
    if listen_id is None:
        return {"status": "ignored"}
    background_tasks.add_task(forward_listen, db, listen_id, config)
    return {"status": "ok"}


@router.get("/webhook-url", response_model=WebhookUrl, response_model_exclude_unset=True, summary="Plex webhook URL including its token (admin)")
def get_webhook_url(
    request: Request,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    secret = db.get_plex_webhook_secret()
    return {"url": f"{_request_base_url(request)}/api/scrobbles/plex?token={quote(secret)}"}


@router.post("/webhook-secret/rotate", response_model=WebhookUrl, response_model_exclude_unset=True, summary="Regenerate the Plex webhook secret (admin)")
def rotate_webhook_secret(
    request: Request,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, str]:
    secret = db.rotate_plex_webhook_secret()
    logger.info("Plex webhook secret rotated by %s", admin.get("username"))
    return {"url": f"{_request_base_url(request)}/api/scrobbles/plex?token={quote(secret)}"}


# ---------------------------------------------------------------------------
# Last.fm connect flow
# ---------------------------------------------------------------------------


@router.get("/lastfm/auth-url", response_model=AuthUrl, response_model_exclude_unset=True, summary="Begin the Last.fm connect flow")
def lastfm_auth_url(
    request: Request,
    forward_url: Optional[str] = Query(default=None),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    client = build_lastfm_client(db, config)
    if client is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED_DETAIL)
    base_url = _base_url(request, db, config)
    state = db.create_lastfm_auth_state(current_user["id"], _safe_forward_url(forward_url))
    callback = f"{base_url}/api/scrobbles/lastfm/callback?state={quote(state)}"
    return {"url": client.build_auth_url(callback)}


@router.get("/lastfm/callback", summary="Last.fm redirect target", include_in_schema=False)
def lastfm_callback(
    state: str = Query(default=""),
    token: str = Query(default=""),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    current_user: Optional[dict[str, Any]] = Depends(_optional_user),
) -> RedirectResponse:
    # Last.fm appends its own "?token=" to the callback; tolerate a malformed join into the state value.
    if "?token=" in state:
        state, _, embedded = state.partition("?token=")
        token = token or embedded
    consumed = db.consume_lastfm_auth_state(state)
    if consumed is None or current_user is None or str(consumed["user_id"]) != str(current_user["id"]):
        logger.warning("Last.fm callback rejected: invalid, expired, reused or foreign state")
        return _redirect("/?scrobble_error=state")

    target = _safe_forward_url(consumed.get("forward_url")) or "/"
    client = build_lastfm_client(db, config)
    if client is None or not token:
        return _redirect(_with_query(target, scrobble_error="lastfm"))
    try:
        username, session_key = client.exchange_token_for_session(token)
    except LastFmError as exc:
        logger.warning("Last.fm session exchange failed for user %s: %s", current_user["id"], exc)
        return _redirect(_with_query(target, scrobble_error="lastfm"))
    db.upsert_scrobble_config(
        current_user["id"], lastfm_username=username, lastfm_session_key=session_key
    )
    return _redirect(_with_query(target, connected="lastfm"))


# ---------------------------------------------------------------------------
# Config, users, listens
# ---------------------------------------------------------------------------


@router.get("/config", response_model=ScrobbleConfig, response_model_exclude_unset=True, summary="Own scrobble configuration (masked)")
def get_my_config(
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    return _config_for_user(db, current_user["id"], current_user.get("username"))


@router.put("/config", response_model=ScrobbleConfig, response_model_exclude_unset=True, summary="Update own scrobble configuration")
def update_my_config(
    body: ScrobbleConfigUpdate,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    _apply_config_update(db, current_user["id"], body, admin=False)
    return _config_for_user(db, current_user["id"], current_user.get("username"))


@router.get("/users", response_model=list[ScrobbleConfig], response_model_exclude_unset=True, summary="Scrobble configuration for every user (admin)")
def list_user_configs(
    db: Database = Depends(get_db),
    admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    return [
        _public_config(row, row.get("username")) for row in db.list_scrobble_configs()
    ]


@router.put("/users/{user_id}/config", response_model=ScrobbleConfig, response_model_exclude_unset=True, summary="Update any user's scrobble configuration (admin)")
def update_user_config(
    user_id: str,
    body: AdminScrobbleConfigUpdate,
    db: Database = Depends(get_db),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    target = db.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    _apply_config_update(db, target["id"], body, admin=True)
    return _config_for_user(db, target["id"], target.get("username"))


@router.get("/listens", response_model=list[Listen], response_model_exclude_unset=True, summary="Recent listens")
def list_my_listens(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user_id: Optional[str] = Query(default=None),
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    target_id = current_user["id"]
    if user_id and str(user_id) != str(current_user["id"]):
        if not _is_admin(current_user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required")
        target_id = user_id
    return [UserListen.from_row(r).to_dict() for r in db.list_listens(target_id, limit, offset)]


# ---------------------------------------------------------------------------
# Server config
# ---------------------------------------------------------------------------


@router.get("/server-config", response_model=ServerConfig, response_model_exclude_unset=True, summary="Server-level scrobbling settings (admin)")
def get_server_config(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    key, secret, from_env = get_lastfm_credentials(db, config)
    return {
        "lastfm_configured": bool(key and secret),
        "lastfm_api_key_masked": _mask_key(key),
        "lastfm_from_env": from_env,
        "plex_history_poll_minutes": db.get_plex_history_poll_minutes(),
    }


@router.put("/server-config", response_model=ServerConfig, response_model_exclude_unset=True, summary="Update server-level scrobbling settings (admin)")
def update_server_config(
    body: ServerConfigUpdate,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    _, _, from_env = get_lastfm_credentials(db, config)
    wants_credentials = bool((body.lastfm_api_key or "").strip() or (body.lastfm_api_secret or "").strip())
    if from_env and wants_credentials:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Last.fm credentials are set via LASTFM_API_KEY / LASTFM_API_SECRET and cannot be changed here",
        )
    if wants_credentials:
        db.set_lastfm_settings(
            api_key=(body.lastfm_api_key or "").strip() or None,
            api_secret=(body.lastfm_api_secret or "").strip() or None,
        )
    if body.plex_history_poll_minutes is not None:
        db.set_plex_history_poll_minutes(body.plex_history_poll_minutes)
    return get_server_config(db=db, config=config, admin=admin)

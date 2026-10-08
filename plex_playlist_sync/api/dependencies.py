"""FastAPI dependencies for plex-playlist-sync."""

import logging
import hmac
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional, Union

import httpx
from fastapi import Depends, HTTPException, Request, status

from plex_playlist_sync.auth import get_or_create_secret_key, verify_session_token
from plex_playlist_sync.clients.deezer import DeezerClient
from plex_playlist_sync.clients.discovery import DiscoveryClient
from plex_playlist_sync.clients.lidarr import LidarrClient
from plex_playlist_sync.item_history import TRIGGER_USER, GrabTrigger, set_provenance
from plex_playlist_sync.library_manager import build_lidarr_client
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.clients.plex import PlexClient
from plex_playlist_sync.clients.spotify import SpotifyClient
from plex_playlist_sync.clients.spotify_scraper import SpotifyWebScraper
from plex_playlist_sync.clients.core_client import SESSION_ISSUED_AT_KEY, CoreClient
from plex_playlist_sync.config import MEDIA_SERVER_JELLYFIN, MEDIA_SERVER_NONE, MEDIA_SERVER_SUBSONIC, Config
from plex_playlist_sync.media_server import MediaServerUnavailable
from plex_playlist_sync.media_servers import MediaServer, as_media_server, build_jellyfin, build_subsonic
from plex_playlist_sync.internal_auth import (
    HEADER_SIGNATURE,
    InvalidAssertion,
    verify_assertion,
)
from plex_playlist_sync.models import UserPermission
from plex_playlist_sync.security import safe_data_path
from plex_playlist_sync.local_auth import parse_trusted_proxies, resolve_client_ip
from plex_playlist_sync.storage import Database, ts_to_us

logger = logging.getLogger(__name__)

_db_lock = threading.Lock()
_db_instances: dict[str, Database] = {}
_discovery_lock = threading.Lock()
_discovery_client_instance: Optional[DiscoveryClient] = None
_mbid_enricher_lock = threading.Lock()
_mbid_enricher_instance: Optional[MbidEnricherClient] = None


def _db_key(db_path: Union[str, Path]) -> str:
    """Registry key for a database path: ``:memory:`` as is, files by resolved absolute path."""
    return ":memory:" if str(db_path) == ":memory:" else str(Path(db_path).resolve())


def register_db(db: Database) -> None:
    """Makes ``db`` the instance ``get_db`` hands out for its file.

    The server process opens one Database at boot and passes it to ``create_app``; without this ``get_db`` would open a
    second connection to the same file, and two connections contend for SQLite's single write lock (a write that
    arrives while the other connection is mid-transaction fails with "database is locked"). In-memory databases are not
    registered: each ``:memory:`` connection is its own database.
    """
    if str(db.db_path) == ":memory:":
        return
    with _db_lock:
        key = _db_key(db.db_path)
        previous = _db_instances.get(key)
        if previous is not None and previous is not db:
            # Not closed: a request thread may still hold the old instance, and closing it under them would break
            # that request. It is dropped from the registry and released once unreferenced.
            logger.debug("Replacing registered Database for %s with the explicitly provided instance", key)
        _db_instances[key] = db


def get_config() -> Config:
    """Dependency to retrieve system configuration from environment."""
    return Config.from_env()


def get_db() -> Database:
    """Dependency providing a Database instance from /data/sync_db.sqlite or config."""
    config = get_config()
    db_env = os.getenv("DATABASE_PATH")
    if db_env:
        if db_env == ":memory:":
            db_path = ":memory:"
        else:
            db_path = str(Path(db_env).resolve())
    else:
        # Resolve base directory prioritizing CONFIG_DIR, then /config if a dir, then DATA_DIR / config.data_dir
        if os.getenv("CONFIG_DIR"):
            base_dir = str(os.getenv("CONFIG_DIR")).strip()
        elif os.path.isdir("/config"):
            base_dir = "/config"
        else:
            base_dir = os.getenv("DATA_DIR", config.data_dir).strip()
        db_path = str(safe_data_path("sync_db.sqlite", base_dir=base_dir))

    key = _db_key(db_path)
    with _db_lock:
        existing = _db_instances.get(key)
        if existing is not None and existing._conn is None:
            # Closed (e.g. by a context manager): drop it rather than hand out a handle that must silently reopen.
            del _db_instances[key]
        if key not in _db_instances:
            _db_instances[key] = Database(db_path)
            if config.role == "gateway":
                try:
                    _db_instances[key].set_last_role("gateway")
                except sqlite3.Error as exc:
                    logger.warning("Could not record the gateway role: %s", type(exc).__name__)
        return _db_instances[key]


def require_media_server(config: Config = Depends(get_config)) -> None:
    """Route dependency: 409 ``media_server_unavailable`` unless a media server is configured."""
    if config.media_server_type == MEDIA_SERVER_NONE:
        raise MediaServerUnavailable()


def get_plex_client(config: Config = Depends(get_config)) -> Optional[PlexClient]:
    """Dependency providing PlexClient instance if configured."""
    if not config.plex_enabled:
        return None
    try:
        return PlexClient(
            base_url=config.plex_url,
            token=config.plex_token,
            verify_ssl=config.plex_verify_ssl,
            music_section=config.plex_music_section,
        )
    except Exception as e:
        logger.error("Failed to initialize PlexClient: %s", e)
        return None


def get_media_client(
    config: Config = Depends(get_config), plex_client: Optional[PlexClient] = Depends(get_plex_client)
) -> Optional[Union[PlexClient, MediaServer]]:
    """The connected client of whichever media server is configured: the Plex client, or the Subsonic adapter.

    Generic routes depend on this (they only go through ``as_media_server``); Plex-only routes keep
    ``get_plex_client``, which is None whenever Plex is not the active server.
    """
    if plex_client is not None:
        return plex_client
    if config.media_server_type == MEDIA_SERVER_SUBSONIC:
        return build_subsonic(config)
    if config.media_server_type == MEDIA_SERVER_JELLYFIN:
        return build_jellyfin(config)
    return None


def get_active_media_server(client: Optional[Union[PlexClient, MediaServer]] = Depends(get_media_client)) -> Optional[MediaServer]:
    """Dependency providing the configured media-server adapter, or None when none is configured/reachable.

    Built on ``get_plex_client`` (via ``get_media_client``) so existing dependency overrides keep working.
    """
    return as_media_server(client)


def get_spotify_client(
    config: Config = Depends(get_config),
) -> Union[SpotifyClient, SpotifyWebScraper, None]:
    """Dependency providing SpotifyClient if credentials exist, falling back to keyless SpotifyWebScraper."""
    if config.spotify_client_id and config.spotify_client_secret:
        try:
            return SpotifyClient(
                client_id=config.spotify_client_id,
                client_secret=config.spotify_client_secret,
            )
        except Exception as e:
            logger.error("Failed to initialize SpotifyClient: %s. Falling back to web scraper.", e)

    try:
        return SpotifyWebScraper()
    except Exception as e:
        logger.error("Failed to initialize SpotifyWebScraper: %s", e)
        return None


def get_deezer_client() -> Optional[DeezerClient]:
    """Dependency providing DeezerClient instance."""
    try:
        return DeezerClient()
    except Exception as e:
        logger.error("Failed to initialize DeezerClient: %s", e)
        return None


def get_discovery_client() -> DiscoveryClient:
    """Dependency providing singleton DiscoveryClient instance."""
    global _discovery_client_instance
    with _discovery_lock:
        if _discovery_client_instance is None:
            _discovery_client_instance = DiscoveryClient()
        return _discovery_client_instance


def get_mbid_enricher() -> MbidEnricherClient:
    """Dependency providing singleton MbidEnricherClient instance."""
    global _mbid_enricher_instance
    with _mbid_enricher_lock:
        if _mbid_enricher_instance is None:
            _mbid_enricher_instance = MbidEnricherClient()
        return _mbid_enricher_instance


GATEWAY_SERVICE_ID = "gateway_service"


def tier_of(config: Config) -> str:
    """Deployment tier reported to the UI: "gateway", "core" or "all-in-one"."""
    role = (config.role or os.getenv("ROLE", "all-in-one")).lower().strip()
    return role if role in ("gateway", "core") else "all-in-one"


def _forwarded_principal(user: dict[str, Any]) -> dict[str, Any]:
    """Returns a copy of ``user`` that can never be admin: flag forced off, ADMIN and MANAGE_REQUESTS bits stripped."""
    principal = dict(user)
    perms = principal.get("permissions")
    perms = int(UserPermission.DEFAULT) if perms is None else int(perms)
    principal["permissions"] = perms & ~int(UserPermission.ADMIN) & ~int(UserPermission.MANAGE_REQUESTS)
    principal["is_admin"] = False
    principal["forwarded"] = True
    return principal


REVOKED_DETAIL = "Session has expired or was revoked"
MFA_ENROLLMENT_DETAIL = "mfa_enrollment_required"


def get_client_ip(request: Request, config: Config) -> str:
    """Client address for throttling: the peer, or ``X-Forwarded-For`` only via a TRUSTED_PROXIES peer."""
    peer = request.client.host if request.client else None
    trusted = parse_trusted_proxies(config.trusted_proxies or os.getenv("TRUSTED_PROXIES"))
    return resolve_client_ip(peer, request.headers.get("X-Forwarded-For"), trusted)


def _mfa_exempt_path(path: str) -> bool:
    return path == "/api/account" or path.startswith("/api/account/") or path.startswith("/api/auth/")


def _enforce_account_state(
    request: Request, db: Database, user_id: str, session_issued_at_us: Optional[int]
) -> None:
    """Rejects disabled, tombstoned and revoked sessions (401), and un-enrolled MFA sessions (403)."""
    state = db.get_auth_state(user_id)
    if (
        state["tombstoned"]
        or state["disabled"]
        or int(state["sessions_revoked_at_us"]) > int(session_issued_at_us or 0)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=REVOKED_DETAIL)
    if state["auth_type"] == "local" and not state["mfa_enabled"] and not _mfa_exempt_path(request.url.path):
        if db.get_account_settings()["require_mfa_local"]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MFA_ENROLLMENT_DETAIL)


SESSION_STATUS_TTL_SECONDS = 60.0
_SESSION_STATUS_MAX_ENTRIES = 4096
_session_status_lock = threading.Lock()
# (user_id, session_issued_at_us) -> (expires_at_monotonic, result). Gateway-only, in memory.
_session_status_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
CORE_UNAVAILABLE_DETAIL = "Core unavailable"


def _monotonic() -> float:
    return time.monotonic()


def clear_session_status_cache() -> None:
    with _session_status_lock:
        _session_status_cache.clear()


def core_session_status(
    config: Config,
    user_id: str,
    session_issued_at_us: int,
    *,
    use_cache: bool = True,
    record_login: bool = False,
    username: Optional[str] = None,
) -> dict[str, Any]:
    """Core's verdict on a gateway session, cached for 60 s per (user, issue time) unless ``use_cache`` is off.

    Fails CLOSED: if core is unconfigured, unreachable or answers anything unexpected, raises 503.
    """
    key = (str(user_id), int(session_issued_at_us))
    now = _monotonic()
    if use_cache:
        with _session_status_lock:
            hit = _session_status_cache.get(key)
            if hit is not None and hit[0] > now:
                return hit[1]
    unavailable = HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=CORE_UNAVAILABLE_DETAIL)
    if not config.trackseerr_core_url or not config.internal_core_secret:
        logger.error("Gateway cannot check session status: TRACKSEERR_CORE_URL / INTERNAL_CORE_SECRET not set")
        raise unavailable
    client = CoreClient(core_url=config.trackseerr_core_url, secret=config.internal_core_secret)
    try:
        code, body = client.session_status(
            str(user_id), int(session_issued_at_us), record_login=record_login, username=username
        )
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Gateway session-status check failed: %s", type(exc).__name__)
        raise unavailable from exc
    if code != 200 or not isinstance(body.get("valid"), bool):
        logger.error("Core returned unexpected status %s for session-status", code)
        raise unavailable
    result: dict[str, Any] = {"valid": body["valid"]}
    if not body["valid"]:
        reason = body.get("reason")
        result["reason"] = reason if reason in ("disabled", "deleted", "revoked", "mfa_enrollment_required") else "revoked"
    if not use_cache:
        return result
    with _session_status_lock:
        if len(_session_status_cache) >= _SESSION_STATUS_MAX_ENTRIES:
            for stale in [k for k, (exp, _r) in _session_status_cache.items() if exp <= now]:
                del _session_status_cache[stale]
            if len(_session_status_cache) >= _SESSION_STATUS_MAX_ENTRIES:
                _session_status_cache.clear()
        _session_status_cache[key] = (now + SESSION_STATUS_TTL_SECONDS, result)
    return result


def _enforce_core_session_status(
    request: Request, db: Database, config: Config, token: str, user_id: str, issued_us: int
) -> None:
    """Gateway only: core owns disable / delete / revoke / MFA state, so ask it for every session request."""
    verdict = core_session_status(config, user_id, issued_us)
    if verdict["valid"]:
        return
    if verdict.get("reason") == "mfa_enrollment_required":
        if not _mfa_exempt_path(request.url.path):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MFA_ENROLLMENT_DETAIL)
        return
    db.delete_session(token)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=REVOKED_DETAIL)


def resolve_signed_principal(
    request: Request,
    db: Database,
    config: Config,
) -> dict[str, Any]:
    """Authenticates a gateway-signed call (X-TS-* headers) and returns its non-admin principal.

    Raises 401 on any verification failure. Unknown asserted users are created as
    non-admin; an existing row (admin or not) is never modified.
    """
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid gateway assertion",
    )
    role = (config.role or os.getenv("ROLE", "all-in-one")).lower().strip()
    if role == "gateway":
        # A gateway is never a verification endpoint.
        raise unauthorized
    body_hash = getattr(request.state, "ts_body_sha256", None)
    if not isinstance(body_hash, str):
        # Body hash is computed by SignedBodyMiddleware; without it nothing can be verified.
        logger.warning("Gateway assertion rejected: request body hash unavailable")
        raise unauthorized
    raw_path = request.scope.get("raw_path")
    path = raw_path.decode("latin-1") if raw_path else request.url.path
    query = request.scope.get("query_string", b"").decode("latin-1")
    target = f"{path}?{query}" if query else path
    try:
        asserted = verify_assertion(
            config.internal_core_secret,
            request.method,
            target,
            request.headers,
            body_hash,
            nonce_store=db,
        )
    except InvalidAssertion as exc:
        logger.warning("Gateway assertion rejected: %s", exc)
        raise unauthorized from exc

    if not asserted.user_id:
        return {
            "id": GATEWAY_SERVICE_ID,
            "username": GATEWAY_SERVICE_ID,
            "is_admin": False,
            "permissions": 0,
            "forwarded": True,
        }
    # Admins never act through the gateway, even at user level.
    known = db.get_user(asserted.user_id)
    if known is not None and (
        known.get("is_admin") or int(known.get("permissions") or 0) & int(UserPermission.ADMIN)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin accounts must use the TrackSeerr Core admin interface",
        )
    try:
        # ensure_user returns the stored row, so the asserted name never renames an existing user.
        user = db.ensure_user(asserted.user_id, asserted.user_name)
    except PermissionError as exc:
        logger.warning("Gateway assertion rejected: %s", exc)
        raise unauthorized from exc
    _enforce_account_state(request, db, user["id"], asserted.session_issued_at)
    return _forwarded_principal(user)


def get_current_user(
    request: Request,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Reads signed HttpOnly session cookie 'session_token' or Authorization Bearer header,

    validates signature and expiration, retrieves user from DB. Raises 401 if invalid or expired.
    """
    return authenticate_request(request, db, config)


def authenticate_request(
    request: Request,
    db: Database,
    config: Config,
) -> dict[str, Any]:
    """Shared session resolution for get_current_user: signed principal, session cookie, or Bearer header.

    Session tokens are never accepted from the URL (they would land in proxy and access logs); same-origin
    EventSource requests authenticate with the HttpOnly session cookie.
    """
    if request.headers.get(HEADER_SIGNATURE) is not None:
        return resolve_signed_principal(request, db, config)

    token: Optional[str] = None

    # 1. Read from HttpOnly cookie
    cookie_token = request.cookies.get("session_token")
    if cookie_token:
        token = cookie_token

    # 2. Or from Authorization Bearer header
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: No active session or token provided",
        )

    # 3. Validate signature and expiration
    secret_key = get_or_create_secret_key(data_dir=config.data_dir)
    payload = verify_session_token(token, secret_key)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token",
        )

    # 4. Validate session exists in database
    session_row = db.get_session(token)
    if session_row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has expired or was revoked",
        )

    # 5. Retrieve user from DB
    user = db.get_user(payload["user_id"])
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    # 5b. Disabled, tombstoned or revoked users are rejected on direct sessions as well
    issued_us = ts_to_us(session_row.get("created_at"))
    _enforce_account_state(request, db, user["id"], issued_us)
    if tier_of(config) == "gateway":
        _enforce_core_session_status(request, db, config, token, user["id"], issued_us)
    user[SESSION_ISSUED_AT_KEY] = issued_us

    # Ensure permissions and is_admin are synchronized
    if user.get("permissions") is None:
        user["permissions"] = int(UserPermission.ADMIN if user.get("is_admin") else UserPermission.DEFAULT)
    elif int(user["permissions"]) & int(UserPermission.ADMIN):
        user["is_admin"] = True

    return user


def get_current_user_or_api_key(
    request: Request,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Authenticates via a gateway-signed assertion, X-Api-Key / query param, or a user session."""
    # 0. Gateway-signed assertion: forced non-admin, never falls through to other methods
    if request.headers.get(HEADER_SIGNATURE) is not None:
        return resolve_signed_principal(request, db, config)

    # 1. Check X-Api-Key header or query parameter apikey / api_key
    api_key = (
        request.headers.get("X-Api-Key")
        or request.query_params.get("apikey")
        or request.query_params.get("api_key")
    )
    if api_key is not None:
        if db.validate_api_key(api_key):
            return {
                "id": "api_key_user",
                "username": "api_key",
                "is_admin": True,
                "permissions": int(UserPermission.ADMIN),
            }
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    # 2. Fall back to standard session token validation
    return get_current_user(request=request, db=db, config=config)


def require_service_principal(
    request: Request,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Accepts ONLY the signed gateway service principal; every other caller gets 404."""
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    if request.headers.get(HEADER_SIGNATURE) is None:
        raise not_found
    try:
        principal = resolve_signed_principal(request, db, config)
    except HTTPException as exc:
        raise not_found from exc
    if principal.get("id") != GATEWAY_SERVICE_ID:
        raise not_found
    return principal


def require_user(current_user: dict[str, Any] = Depends(get_current_user_or_api_key)) -> dict[str, Any]:
    """Enforces active user session or valid machine authentication."""
    return current_user


def has_permission(user: dict[str, Any], permission: UserPermission) -> bool:
    """Checks whether a user holds the given permission bitflag.

    Admins (is_admin=True or UserPermission.ADMIN) hold all permissions.
    """
    if user.get("forwarded"):
        # Gateway-asserted principals never hold admin, nor any permission implied by it.
        user_perms = user.get("permissions")
        user_perms = 0 if user_perms is None else int(user_perms)
        return bool(user_perms & ~int(UserPermission.ADMIN) & ~int(UserPermission.MANAGE_REQUESTS) & int(permission))
    if user.get("is_admin"):
        return True
    user_perms = user.get("permissions")
    if user_perms is None:
        user_perms = int(UserPermission.DEFAULT)
    else:
        user_perms = int(user_perms)
    if user_perms & int(UserPermission.ADMIN):
        return True
    return bool(user_perms & int(permission))


def require_permission(permission: UserPermission):
    """FastAPI dependency factory enforcing that current user holds the specified permission."""
    def _dependency(current_user: dict[str, Any] = Depends(get_current_user_or_api_key)) -> dict[str, Any]:
        if not has_permission(current_user, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: requires {permission.name}",
            )
        return current_user
    return _dependency


def require_admin_or_permission(permission: UserPermission):
    """FastAPI dependency factory enforcing admin access or a specific permission bit.

    Gateway-forwarded principals are always refused (never hold admin or elevated permissions).
    """
    def _dependency(current_user: dict[str, Any] = Depends(get_current_user_or_api_key)) -> dict[str, Any]:
        if current_user.get("forwarded"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: requires {permission.name} or admin access",
            )
        user_perms = int(current_user.get("permissions") if current_user.get("permissions") is not None else 0)
        is_adm = bool(current_user.get("is_admin")) or bool(user_perms & int(UserPermission.ADMIN))
        if is_adm or has_permission(current_user, permission):
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: requires {permission.name} or admin access",
        )
    return _dependency


def actor_id(user: dict[str, Any]) -> Optional[str]:
    """The user id to persist as an actor: None for non-user principals such as the API key."""
    uid = user.get("id")
    return str(uid) if uid and uid != "api_key_user" else None


def require_admin(current_user: dict[str, Any] = Depends(get_current_user_or_api_key)) -> dict[str, Any]:
    """Enforces is_admin=True or UserPermission.ADMIN, raises 403 otherwise."""
    if current_user.get("forwarded"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    user_perms = int(current_user.get("permissions") if current_user.get("permissions") is not None else 0)
    if not (current_user.get("is_admin") or (user_perms & int(UserPermission.ADMIN))):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    return current_user


async def track_admin_actor(current_user: dict[str, Any] = Depends(require_admin)) -> None:
    """Marks library changes made while serving this request as the admin's (item history provenance).

    ``async`` on purpose: the ContextVar it sets lives in the request task and is copied into the thread the (sync)
    endpoint then runs in. The task ends with the request, so nothing leaks to other requests.
    """
    actor_id = current_user.get("id")
    set_provenance(
        GrabTrigger(
            TRIGGER_USER,
            label=current_user.get("username"),
            actor_user_id=str(actor_id) if actor_id and actor_id != "api_key_user" else None,
        )
    )


def require_core_tier(config: Config = Depends(get_config)) -> None:
    """Enforces that endpoint is running on TrackSeerr Core tier; blocks execution in gateway mode."""
    role = (config.role or os.getenv("ROLE", "all-in-one")).lower().strip()
    if role == "gateway":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Library management is restricted to TrackSeerr Core tier. Gateway tier cannot execute library mutations.",
        )


def get_lidarr_client(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> Optional[LidarrClient]:
    """Dependency providing LidarrClient using DB-backed settings with env fallback."""
    return build_lidarr_client(db, config)


def verify_feed_access(
    request: Request,
    token: Optional[str] = None,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Validates access for the missing-track RSS / text feeds (Lidarr and RSS clients).

    Accepted credentials, all of which are administrative:
    * the configured ``FEED_TOKEN`` (query ``token``, ``X-Api-Key`` or Bearer),
    * a valid API key,
    * an admin session.

    A plain (non-admin) user, a gateway-forwarded principal and anonymous callers are refused.
    """
    if request.headers.get(HEADER_SIGNATURE) is None and config.feed_token:
        provided = (
            token
            or request.headers.get("X-Api-Key")
            or request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        )
        if provided and hmac.compare_digest(
            str(provided).encode("utf-8"), str(config.feed_token).encode("utf-8")
        ):
            return {"id": "feed_token_user", "username": "feed_subscriber", "is_admin": True}

    principal = get_current_user_or_api_key(request=request, db=db, config=config)
    return require_admin(principal)

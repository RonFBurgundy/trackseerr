"""Plex OAuth PIN authentication routes."""

import logging
import os
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from trackseerr import local_auth
from trackseerr.api.dependencies import (
    HEADER_SIGNATURE,
    core_plex_machine_id,
    core_session_status,
    get_client_ip,
    get_config,
    get_current_user,
    get_db,
    get_plex_client,
    require_service_principal,
    tier_of,
)
from trackseerr.api.schemas.auth import (
    InviteAcceptedResponse,
    InviteInfoResponse,
    LocalLoginResponse,
    LogoutResponse,
    MeResponse,
    PlexPinResponse,
    PlexVerifyResponse,
)
from trackseerr.api.sessions import start_session
from trackseerr.clients.core_client import CoreClient
from trackseerr.local_login import (
    DETAIL_INVALID,
    DETAIL_THROTTLED,
    PUBLIC_LOGIN_DETAILS,
    LoginError,
    check_rate,
    throttle_key,
    verify_local_login,
)
from trackseerr.auth import (
    PlexAuthError,
    check_plex_pin,
    create_plex_pin,
    get_plex_user,
    verify_server_access,
)
from trackseerr.clients.plex import PlexClient
from trackseerr.config import MEDIA_SERVER_PLEX, Config
from trackseerr.media_server import MediaServerUnavailable
from trackseerr.storage import Database
from trackseerr.redaction import redact_text
from trackseerr.security import safe_forward_url

logger = logging.getLogger(__name__)

router = APIRouter()


class CreatePinRequest(BaseModel):
    forward_url: Optional[str] = Field(
        default=None,
        description="Optional redirect URL after Plex authorization",
    )


class VerifyPinRequest(BaseModel):
    pin_id: int = Field(..., description="Plex PIN ID returned by create pin endpoint")
    target_machine_id: Optional[str] = Field(
        default=None,
        description="Optional Plex Server machine identifier to verify access against",
    )


def _plex_login_unavailable(config: Optional[Config]) -> bool:
    """Plex sign-in cannot work on a core / all-in-one instance whose media server is not Plex. A gateway signs users in
    on behalf of a core that holds the Plex settings, and an explicit PLEX_MACHINE_IDENTIFIER means Plex
    sign-in is intended, so neither is blocked here."""
    if config is None or config.role == "gateway" or os.getenv("PLEX_MACHINE_IDENTIFIER"):
        return False
    return config.media_server_type != MEDIA_SERVER_PLEX  # Subsonic servers have no Plex OAuth either


@router.post("/plex/pin", response_model=PlexPinResponse, response_model_exclude_unset=True)
def generate_pin(
    req: Optional[CreatePinRequest] = None,
    forward_url: Optional[str] = None,
    request: Request = None,  # type: ignore[assignment]  # None only for direct calls in tests
    config: Optional[Config] = Depends(get_config),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Generates a Plex OAuth PIN and authorization URL.

    Two-tier decision: in the DMZ model this endpoint is served by the gateway process itself
    (GATEWAY_LOCAL_ALLOWLIST), never relayed to core, so the check runs where the public request
    arrives. The allowed set is the UNION of APPLICATION_URL (env, else the stored general setting)
    and the request's own origin (X-Forwarded-Proto/Host count only from a TRUSTED_PROXIES peer), so
    a user on a secondary hostname such as a Tailscale name still gets redirected back. No
    client-supplied header is trusted beyond that. A forward_url on any other origin is dropped (Plex
    then simply does not redirect), which closes the post-login open redirect. Residual: a direct
    caller spoofing Host can get a forwardUrl for that host (defense-in-depth only; the PIN flow
    never exposes the token via the redirect).
    """
    cfg = config if isinstance(config, Config) else None
    if _plex_login_unavailable(cfg):
        raise MediaServerUnavailable()
    app_url = str((cfg.application_url if cfg else "") or "").strip().rstrip("/")
    if not app_url and db is not None:
        try:
            general = db.get_general_settings()
            app_url = str(general.get("application_url") or "").strip().rstrip("/")
        except Exception as e:
            logger.debug("Could not resolve application_url for Plex forward_url: %s", e)

    request_origin: Optional[str] = None
    if request is not None:
        trusted = local_auth.parse_trusted_proxies(
            (cfg.trusted_proxies if cfg else None) or os.getenv("TRUSTED_PROXIES")
        )
        request_origin = local_auth.resolve_request_origin(
            request.url.scheme,
            request.headers.get("host"),
            request.client.host if request.client else None,
            request.headers.get("x-forwarded-proto"),
            request.headers.get("x-forwarded-host"),
            trusted,
        )

    supplied = (req.forward_url if req and req.forward_url else None) or forward_url
    target_forward_url = safe_forward_url(
        supplied, allowed_origins=[request_origin, app_url or None]
    )
    if not supplied and app_url:
        target_forward_url = app_url

    try:
        if target_forward_url:
            pin_data = create_plex_pin(forward_url=target_forward_url)
        else:
            pin_data = create_plex_pin()
        return pin_data
    except PlexAuthError as e:
        logger.error("Failed to generate Plex PIN: %s", redact_text(str(e)))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate Plex PIN: {redact_text(str(e))}",
        )
    except Exception as e:
        logger.error("Unexpected error generating Plex PIN: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error generating Plex PIN",
        )


def _resolve_machine_id(
    req: VerifyPinRequest,
    config: Config,
    plex_client: Optional[PlexClient],
) -> str:
    """Determines authoritative target Plex machine ID and enforces request consistency."""
    server_machine_id = os.getenv("PLEX_MACHINE_IDENTIFIER") or (
        plex_client.machine_identifier if plex_client else None
    )
    if not server_machine_id and tier_of(config) == "gateway":
        server_machine_id = core_plex_machine_id(config)
        if not server_machine_id:
            raise MediaServerUnavailable()

    if req.target_machine_id and server_machine_id and req.target_machine_id != server_machine_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Supplied target_machine_id does not match the configured Plex Media Server",
        )

    if not server_machine_id:
        if _plex_login_unavailable(config):
            raise MediaServerUnavailable()
        logger.error("No Plex machine identifier configured to verify user access")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Plex server machine identifier is not configured on hub",
        )

    return server_machine_id


@router.post("/plex/verify", response_model=PlexVerifyResponse, response_model_exclude_unset=True)
def verify_pin(
    req: VerifyPinRequest,
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[PlexClient] = Depends(get_plex_client),
) -> dict[str, Any]:
    """Claims PIN, verifies server access with target_machine_id, upserts user in DB,

    creates signed session token, sets HttpOnly, SameSite=Lax cookie. Outsiders get 403 Forbidden.
    """
    # 1. Claim PIN and retrieve auth token
    try:
        auth_token = check_plex_pin(req.pin_id)
    except (PlexAuthError, ValueError) as e:
        logger.warning("Error checking Plex PIN %d: %s", req.pin_id, redact_text(str(e)))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to verify Plex PIN: {redact_text(str(e))}",
        )
    except Exception as e:
        logger.error("Unexpected error querying Plex PIN %d: %s", req.pin_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error verifying Plex PIN",
        )

    if not auth_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Plex PIN is not yet authorized or has expired",
        )

    # 2. Determine target Plex machine ID (server machine identifier is authoritative)
    machine_id = _resolve_machine_id(req, config, plex_client)

    # 3. Verify server access and ownership
    has_access, is_owner = verify_server_access(auth_token, machine_id)
    if not has_access:
        logger.warning("User with token attempted login but has no access to server %s", machine_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: User does not have access to this Plex Media Server",
        )

    # 4. Fetch Plex user profile
    try:
        plex_user = get_plex_user(auth_token)
    except PlexAuthError as e:
        logger.error("Failed to fetch user details from Plex: %s", redact_text(str(e)))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to retrieve user details from Plex: {redact_text(str(e))}",
        )
    except Exception as e:
        logger.error("Unexpected error getting user details: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error retrieving Plex user profile",
        )

    user_id = plex_user["id"]
    username = plex_user["username"]
    email = plex_user.get("email")

    # Disabled and removed (tombstoned) accounts can never sign in, on any tier.
    prior = db.get_auth_state(user_id)
    if prior["tombstoned"] or prior["disabled"]:
        logger.warning("Refused Plex login for a disabled or removed account")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: this account is not permitted to sign in",
        )

    # On the gateway core owns disabled / removed state: ask it before any session is created.
    if tier_of(config) == "gateway":
        verdict = core_session_status(
            config,
            str(user_id),
            int(time.time() * 1_000_000),
            use_cache=False,
            record_login=True,
            username=username,
        )
        if not verdict["valid"] and verdict.get("reason") in ("disabled", "deleted"):
            logger.warning("Refused Plex login for a disabled or removed account (per core)")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: this account is not permitted to sign in",
            )

    # A Plex name may not shadow a local account's username (impersonation).
    if any(
        other["id"] != str(user_id) and other.get("auth_type") == "local" for other in db.list_users_by_username(username)
    ):
        logger.warning("Refused Plex login: username collides with a local account")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: this account is not permitted to sign in",
        )

    # 5. Upsert user in database (preserve existing admin status if already granted)
    existing_user = db.get_user(user_id)
    is_admin = is_owner or (bool(existing_user["is_admin"]) if existing_user else False)
    user = db.upsert_user(
        user_id=user_id,
        username=username,
        email=email,
        is_admin=is_admin,
    )

    # 5b. Stamp last_login_at once per successful sign-in (on a gateway, core was told via session-status above).
    db.record_successful_login(user["id"])

    # 6. Create signed session token, store it, and set the HttpOnly, SameSite=Lax cookie
    #    (Secure flag follows HTTPS).
    token = start_session(db, config, request, response, user)

    return {"token": token, "user": user}


@router.post("/logout", response_model=LogoutResponse, response_model_exclude_unset=True)
def logout(
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Clears session from DB and deletes cookie."""
    token: Optional[str] = request.cookies.get("session_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

    if token:
        db.delete_session(token)

    is_secure = (request.url.scheme == "https") or (request.headers.get("x-forwarded-proto", "").lower() == "https")
    response.delete_cookie(
        key="session_token",
        httponly=True,
        samesite="lax",
        secure=is_secure,
    )
    return {"status": "success", "message": "Successfully logged out"}


@router.get("/me", response_model=MeResponse, response_model_exclude_unset=True)
def get_me(
    current_user: dict[str, Any] = Depends(get_current_user),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Returns current user info, role and the deployment tier."""
    public = {k: v for k, v in current_user.items() if not str(k).startswith("_")}
    return {"user": public, "tier": tier_of(config)}


# --------------------------------------------------------------------------- local (password) accounts

INVITE_LIMIT = 10
INVALID_INVITE = "Invite link is invalid or has expired"


class LocalLoginRequest(BaseModel):
    username: str = Field(..., max_length=64)
    password: str = Field(..., max_length=256)
    totp_code: Optional[str] = Field(default=None, max_length=16)
    recovery_code: Optional[str] = Field(default=None, max_length=32)


class InviteAcceptRequest(BaseModel):
    password: str = Field(..., max_length=256)


def _login_http_error(exc: LoginError) -> HTTPException:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return HTTPException(status_code=exc.status_code, detail=exc.detail, headers=headers)


@router.post("/local/login", response_model=LocalLoginResponse, response_model_exclude_unset=True)
def local_login(
    req: LocalLoginRequest,
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Signs in a local (non-Plex) account. On the gateway the credentials are verified by core."""
    client_ip = get_client_ip(request, config)
    if tier_of(config) == "gateway":
        if not config.trackseerr_core_url or not config.internal_core_secret:
            logger.error("Gateway cannot verify local login: TRACKSEERR_CORE_URL / INTERNAL_CORE_SECRET not set")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="TrackSeerr Core is not configured")
        client = CoreClient(core_url=config.trackseerr_core_url, secret=config.internal_core_secret)
        payload = {**req.model_dump(), "client_ip": client_ip}
        try:
            code, body = client.local_verify(payload)
        except (httpx.HTTPError, ValueError) as exc:
            logger.error("Gateway local-login verification failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Unable to communicate with TrackSeerr Core engine",
            ) from exc
        if code != 200:
            detail = body.get("detail")
            if code in (401, 423, 429) and detail in PUBLIC_LOGIN_DETAILS:
                raise HTTPException(status_code=code, detail=detail)
            logger.error("Core returned unexpected status %s for local-login verification", code)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unable to sign in right now")
        core_user = body.get("user") if isinstance(body.get("user"), dict) else {}
        core_id, core_name = str(core_user.get("id") or ""), str(core_user.get("username") or "")
        if not core_id or not core_name:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Unable to sign in right now")
        try:
            user = db.mirror_local_user(core_id, core_name)
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=DETAIL_INVALID) from exc
        floor = body.get("session_floor_us")
        start_session(
            db, config, request, response, {**user, "is_admin": False},
            floor_us=int(floor) if isinstance(floor, int) else 0,
        )
        return {
            "user": {"id": user["id"], "username": user["username"], "is_admin": False},
            "mfa_enrollment_required": bool(body.get("mfa_enrollment_required")),
        }

    try:
        result = verify_local_login(
            db,
            username=req.username,
            password=req.password,
            totp_code=req.totp_code,
            recovery_code=req.recovery_code,
            client_ip=client_ip,
        )
    except LoginError as exc:
        raise _login_http_error(exc) from exc
    user = db.get_user(result.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=DETAIL_INVALID)
    start_session(db, config, request, response, user, floor_us=result.session_floor_us)
    return {"user": user, "mfa_enrollment_required": result.mfa_enrollment_required}


def _invite_guard(request: Request, db: Database, config: Config) -> None:
    """Direct callers are limited to 10 invite lookups per IP per 15 minutes.

    A call relayed by the gateway is authenticated as the service principal and was already
    limited per end-user IP at the gateway (core only sees the gateway's address).
    """
    if tier_of(config) == "gateway":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    if request.headers.get(HEADER_SIGNATURE) is not None:
        require_service_principal(request, db, config)
        return
    key = throttle_key("invite", get_client_ip(request, config))
    db.record_login_attempt(key, max_rows=INVITE_LIMIT + 1)
    if check_rate(db, key, INVITE_LIMIT + 1):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=DETAIL_THROTTLED,
            headers={"Retry-After": "900"},
        )


@router.get("/invite/{token}", response_model=InviteInfoResponse, response_model_exclude_unset=True)
def get_invite(
    token: str,
    request: Request,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Describes a valid invite or reset link; 404 for anything unknown, used or expired."""
    _invite_guard(request, db, config)
    info = db.get_valid_token(token)
    if info is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=INVALID_INVITE)
    return {"username": info["username"], "purpose": info["purpose"], "expires_at": info["expires_at"]}


@router.post("/invite/{token}", response_model=InviteAcceptedResponse, response_model_exclude_unset=True)
def accept_invite(
    token: str,
    req: InviteAcceptRequest,
    request: Request,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Sets the user's password from a valid invite/reset token (single use) and revokes sessions."""
    _invite_guard(request, db, config)
    info = db.get_valid_token(token)
    if info is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=INVALID_INVITE)
    problem = local_auth.validate_password(req.password, info["username"])
    if problem:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=problem)
    user_id = db.consume_token_set_password(token, local_auth.hash_password(req.password))
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=INVALID_INVITE)
    return {"status": "success", "username": info["username"]}

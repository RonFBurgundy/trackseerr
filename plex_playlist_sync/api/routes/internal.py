"""Internal gateway-to-core endpoints. Only the signed service principal may call these; everyone else gets 404."""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from plex_playlist_sync import __version__
from plex_playlist_sync.api.dependencies import get_config, get_db, require_service_principal, tier_of
from plex_playlist_sync.api.schemas.internal import (
    HeartbeatResponse,
    HelloResponse,
    SessionStatusResponse,
    VerifyLocalResponse,
)
from plex_playlist_sync.config import Config
from plex_playlist_sync.gateway_link import record_heartbeat
from plex_playlist_sync.internal_auth import PROTOCOL_VERSION
from plex_playlist_sync.api.routes.auth import LocalLoginRequest, _login_http_error
from plex_playlist_sync.local_login import LoginError, verify_local_login
from plex_playlist_sync.storage import Database
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()


class VerifyLocalRequest(LocalLoginRequest):
    client_ip: str = Field(default="unknown", max_length=64)


@router.post("/auth/local/verify", response_model=VerifyLocalResponse, response_model_exclude_unset=True)
def verify_local(
    req: VerifyLocalRequest,
    request: Request,
    db: Database = Depends(get_db),
    _principal: dict[str, Any] = Depends(require_service_principal),
) -> dict[str, Any]:
    """Verifies local credentials for the gateway. The end user's IP arrives in the (signed) body."""
    try:
        result = verify_local_login(
            db,
            username=req.username,
            password=req.password,
            totp_code=req.totp_code,
            recovery_code=req.recovery_code,
            client_ip=req.client_ip,
        )
    except LoginError as exc:
        raise _login_http_error(exc) from exc
    return {
        "user": {"id": result.user_id, "username": result.username},
        "mfa_required": False,
        "mfa_enrollment_required": result.mfa_enrollment_required,
        "session_floor_us": result.session_floor_us,
    }


class SessionStatusRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    session_issued_at: int = Field(..., ge=0)
    # Set by the gateway only at an actual Plex sign-in, so core can stamp last_login_at once per sign-in.
    record_login: bool = False
    username: Optional[str] = Field(default=None, max_length=128)


@router.post("/auth/session-status", response_model=SessionStatusResponse, response_model_exclude_unset=True)
def session_status(
    req: SessionStatusRequest,
    db: Database = Depends(get_db),
    _principal: dict[str, Any] = Depends(require_service_principal),
) -> dict[str, Any]:
    """Tells the gateway whether a session (user, issue time in epoch microseconds) may still be used."""
    state = db.get_auth_state(req.user_id)
    reason: Optional[str] = None
    if state["tombstoned"]:
        reason = "deleted"
    elif state["disabled"]:
        reason = "disabled"
    elif int(state["sessions_revoked_at_us"]) > req.session_issued_at:
        reason = "revoked"
    elif state["auth_type"] == "local" and not state["mfa_enabled"] and db.get_account_settings()["require_mfa_local"]:
        reason = "mfa_enrollment_required"
    # Plex ids are all-digit. Local accounts (``local-*``) record their own logins on core and a
    # gateway-reported login must never reset their failed-login counter or lockout.
    if reason is None and req.record_login and req.user_id.isdigit():
        try:
            db.ensure_user(req.user_id, req.username or req.user_id)
            db.stamp_last_login(req.user_id)
        except PermissionError as exc:
            logger.warning("Gateway sign-in not recorded on core: %s", exc)
    return {"valid": reason is None} if reason is None else {"valid": False, "reason": reason}


@router.get("/hello", response_model=HelloResponse, response_model_exclude_unset=True)
def hello(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    _principal: dict[str, Any] = Depends(require_service_principal),
) -> dict[str, Any]:
    """Version/protocol handshake for the gateway. Service principal only; 404 for everyone else."""
    return {
        "protocol": PROTOCOL_VERSION,
        "version": __version__,
        "role": tier_of(config),
        "instance_id": db.get_instance_id(),
    }


class GatewayHeartbeat(BaseModel):
    version: str = Field(..., max_length=64)
    protocol: int = Field(..., ge=0, le=1_000_000)
    gateway_id: str = Field(..., min_length=1, max_length=64)
    started_at: float = Field(..., ge=0)
    active_sessions: int = Field(..., ge=0, le=10_000_000)


@router.post("/gateway-heartbeat", response_model=HeartbeatResponse, response_model_exclude_unset=True)
def gateway_heartbeat(
    req: GatewayHeartbeat,
    db: Database = Depends(get_db),
    _principal: dict[str, Any] = Depends(require_service_principal),
) -> dict[str, Any]:
    """Records the gateway's liveness (in memory and in the ``gateway_status`` kv row)."""
    record_heartbeat(db, req.model_dump())
    return {"ok": True, "protocol": PROTOCOL_VERSION, "version": __version__}

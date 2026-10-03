"""Internal gateway-to-core endpoints. Only the signed service principal may call these; everyone else gets 404."""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status

from plex_playlist_sync.api.dependencies import get_db, require_service_principal
from plex_playlist_sync.api.routes.auth import LocalLoginRequest, _login_http_error
from plex_playlist_sync.local_login import LoginError, verify_local_login
from plex_playlist_sync.storage import Database
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()


class VerifyLocalRequest(LocalLoginRequest):
    client_ip: str = Field(default="unknown", max_length=64)


@router.post("/auth/local/verify")
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


@router.post("/auth/session-status")
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
    return {"valid": reason is None} if reason is None else {"valid": False, "reason": reason}

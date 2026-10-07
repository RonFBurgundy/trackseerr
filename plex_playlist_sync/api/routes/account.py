"""Account self-service for any signed-in user: profile, quotas, password and MFA.

On the gateway these routes are forwarded to core as the signed-in user (see
``tier_middleware``); they only ever execute on core or all-in-one.
"""

import logging
import threading
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from plex_playlist_sync import local_auth
from plex_playlist_sync.api.dependencies import get_config, get_current_user, get_db
from plex_playlist_sync.api.schemas.account import (
    AccountResponse,
    ChangePasswordResponse,
    MfaSetupResponse,
    RecoveryCodesResponse,
    StatusResponse,
)
from plex_playlist_sync.api.sessions import start_session
from plex_playlist_sync.config import Config
from plex_playlist_sync.local_login import (
    DETAIL_INVALID_CODE,
    DETAIL_THROTTLED,
    check_rate,
    serialized_attempt,
    throttle_key,
)
from plex_playlist_sync.request_submission import is_auto_approved, quota_snapshot
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()

PENDING_TTL_SECONDS = 10 * 60
PENDING_MAX_ATTEMPTS = 5
ACCOUNT_FAILURE_LIMIT = 5

_NON_ACCOUNT_IDS = frozenset({"api_key_user", "feed_token_user", "gateway_service"})

_pending_lock = threading.Lock()
# user_id -> (secret_b32, expires_at_monotonic, failed_attempts). Held in memory only; never persisted.
_pending_totp: dict[str, tuple[str, float, int]] = {}


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., max_length=256)
    new_password: str = Field(..., max_length=1024)


class CodeRequest(BaseModel):
    code: str = Field(..., max_length=16)


class PasswordRequest(BaseModel):
    password: str = Field(..., max_length=256)


class PasswordAndCodeRequest(BaseModel):
    password: str = Field(..., max_length=256)
    code: str = Field(..., max_length=16)


def account_user(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if str(current_user.get("id")) in _NON_ACCOUNT_IDS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account endpoints require a user session")
    return current_user


def _require_local(db: Database, user: dict[str, Any]) -> None:
    if db.get_auth_state(user["id"])["auth_type"] != "local":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This action is only available for local accounts",
        )


def _throttle_check(db: Database, user: dict[str, Any]) -> str:
    key = throttle_key("acct", user["id"])
    if check_rate(db, key, ACCOUNT_FAILURE_LIMIT):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=DETAIL_THROTTLED,
            headers={"Retry-After": "900"},
        )
    return key


def _fail(db: Database, key: str) -> None:
    db.record_login_attempt(key, max_rows=ACCOUNT_FAILURE_LIMIT)


def _verify_password_and_code(db: Database, user: dict[str, Any], password: str, code: str) -> None:
    """Re-authenticates for a sensitive MFA action. Failures are throttled per account (atomically)."""
    with serialized_attempt("acct:" + str(user["id"])):
        key = _throttle_check(db, user)
        cred = db.get_local_credentials(user["id"])
        if cred is None or not cred["totp_secret"]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MFA is not enabled")
        if not local_auth.verify_password(password, cred["password_hash"]):
            _fail(db, key)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
        counter = local_auth.verify_totp(cred["totp_secret"], code, cred["totp_last_counter"])
        if counter is None or not db.advance_totp_counter(user["id"], counter):
            _fail(db, key)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=DETAIL_INVALID_CODE)
        db.clear_login_attempts(key)


def _verify_password_only(db: Database, user: dict[str, Any], password: str) -> None:
    """Re-authenticates with the password alone. A wrong one counts toward the account lockout."""
    with serialized_attempt("acct:" + str(user["id"])):
        key = _throttle_check(db, user)
        cred = db.get_local_credentials(user["id"])
        if cred is None or not cred["password_hash"] or not local_auth.verify_password(password, cred["password_hash"]):
            _fail(db, key)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid password")
        db.clear_login_attempts(key)


def _effective_quotas(db: Database, user: dict[str, Any]) -> dict[str, Any]:
    """Limits, window and usage. Delegates to the same counting function that enforces the quotas."""
    return quota_snapshot(db, user)


def _auto_approve(user: dict[str, Any], config: Config) -> dict[str, bool]:
    """Which request types skip admin approval for this user (same rule the submission path applies).

    Bit 4 approves tracks, bit 8 albums and bit 64 discographies. Admins and the global
    ``AUTO_APPROVE_REQUESTS`` setting approve every type.
    """
    return {
        "tracks": is_auto_approved(user, config, "track"),
        "albums": is_auto_approved(user, config, "album"),
        "discographies": is_auto_approved(user, config, "discography"),
    }


@router.get("", response_model=AccountResponse, response_model_exclude_unset=True)
def get_account(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    current_user: dict[str, Any] = Depends(account_user),
) -> dict[str, Any]:
    stored = db.get_user(current_user["id"])
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    state = db.get_auth_state(stored["id"])
    is_local = state["auth_type"] == "local"
    return {
        "id": stored["id"],
        "username": stored["username"],
        "auth_type": state["auth_type"] or "plex",
        "mfa_enabled": state["mfa_enabled"],
        "mfa_required": is_local and bool(db.get_account_settings()["require_mfa_local"]),
        "recovery_codes_remaining": db.count_recovery_codes(stored["id"]) if state["mfa_enabled"] else 0,
        "quotas": _effective_quotas(db, current_user),
        "auto_approve": _auto_approve(current_user, config),
    }


@router.post("/password", response_model=ChangePasswordResponse, response_model_exclude_unset=True)
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    current_user: dict[str, Any] = Depends(account_user),
) -> dict[str, Any]:
    """Changes a local account's password and signs out every other session."""
    _require_local(db, current_user)
    with serialized_attempt("acct:" + str(current_user["id"])):
        key = _throttle_check(db, current_user)
        cred = db.get_local_credentials(current_user["id"])
        if cred is None or not local_auth.verify_password(body.current_password, cred["password_hash"]):
            _fail(db, key)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    problem = local_auth.validate_password(body.new_password, current_user["username"])
    if problem:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=problem)
    if body.new_password == body.current_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must differ from the current password",
        )
    revoked_us = db.set_password(current_user["id"], local_auth.hash_password(body.new_password))
    db.clear_login_attempts(key)
    if current_user.get("forwarded"):
        # The gateway owns the browser session: it re-issues it from this marker (and strips it).
        return {"status": "success", "reissue_session": True, "session_floor_us": revoked_us}
    fresh = db.get_user(current_user["id"])
    if fresh is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    start_session(db, config, request, response, fresh, floor_us=revoked_us)
    return {"status": "success"}


def _pending_get(user_id: str) -> Optional[tuple[str, int]]:
    with _pending_lock:
        entry = _pending_totp.get(user_id)
        if entry is None:
            return None
        secret, expires, attempts = entry
        if expires < time.monotonic():
            del _pending_totp[user_id]
            return None
        return secret, attempts


@router.post("/mfa/setup", response_model=MfaSetupResponse, response_model_exclude_unset=True)
def mfa_setup(
    body: PasswordRequest,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(account_user),
) -> dict[str, Any]:
    """Starts TOTP enrollment after re-authenticating with the password. The secret stays pending (in memory, 10 min) until a code confirms it."""
    _require_local(db, current_user)
    if db.get_auth_state(current_user["id"])["mfa_enabled"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="MFA is already enabled")
    _verify_password_only(db, current_user, body.password)
    secret = local_auth.generate_totp_secret()
    with _pending_lock:
        now = time.monotonic()
        for uid in [u for u, (_s, exp, _a) in _pending_totp.items() if exp < now]:
            del _pending_totp[uid]
        _pending_totp[current_user["id"]] = (secret, now + PENDING_TTL_SECONDS, 0)
    return {"secret": secret, "otpauth_uri": local_auth.otpauth_uri(secret, current_user["username"])}


@router.post("/mfa/confirm", response_model=RecoveryCodesResponse, response_model_exclude_unset=True)
def mfa_confirm(
    body: CodeRequest,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(account_user),
) -> dict[str, Any]:
    """Confirms enrollment with a code from the authenticator and returns the recovery codes once."""
    _require_local(db, current_user)
    uid = current_user["id"]
    if db.get_auth_state(uid)["mfa_enabled"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="MFA is already enabled")
    pending = _pending_get(uid)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending MFA setup. Start setup again.",
        )
    secret, attempts = pending
    counter = local_auth.verify_totp(secret, body.code, None)
    if counter is None:
        with _pending_lock:
            if attempts + 1 >= PENDING_MAX_ATTEMPTS:
                _pending_totp.pop(uid, None)
            elif uid in _pending_totp:
                _pending_totp[uid] = (secret, _pending_totp[uid][1], attempts + 1)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=DETAIL_INVALID_CODE)
    codes = local_auth.generate_recovery_codes()
    db.enable_totp(uid, secret, counter, [local_auth.hash_recovery_code(c) for c in codes])
    with _pending_lock:
        _pending_totp.pop(uid, None)
    return {"recovery_codes": codes}


@router.post("/mfa/disable", response_model=StatusResponse, response_model_exclude_unset=True)
def mfa_disable(
    body: PasswordAndCodeRequest,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(account_user),
) -> dict[str, Any]:
    _require_local(db, current_user)
    if db.get_account_settings()["require_mfa_local"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MFA is required by the administrator and cannot be disabled",
        )
    _verify_password_and_code(db, current_user, body.password, body.code)
    db.clear_mfa(current_user["id"])
    return {"status": "success"}


@router.post("/mfa/recovery-codes", response_model=RecoveryCodesResponse, response_model_exclude_unset=True)
def mfa_regenerate_recovery_codes(
    body: PasswordAndCodeRequest,
    db: Database = Depends(get_db),
    current_user: dict[str, Any] = Depends(account_user),
) -> dict[str, Any]:
    """Replaces all recovery codes (old ones stop working) and returns the new ones once."""
    _require_local(db, current_user)
    _verify_password_and_code(db, current_user, body.password, body.code)
    codes = local_auth.generate_recovery_codes()
    db.replace_recovery_codes(current_user["id"], [local_auth.hash_recovery_code(c) for c in codes])
    return {"recovery_codes": codes}

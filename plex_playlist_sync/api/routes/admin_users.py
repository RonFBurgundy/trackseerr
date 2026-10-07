"""Admin user management and account settings (core / all-in-one only, admin sessions only).

Every route requires ``require_admin`` (which already refuses gateway-forwarded principals) and is
additionally refused with 404 on the gateway tier. These routes are deliberately absent from the gateway
allowlists in ``tier_middleware``.

Responses never contain ``password_hash``, ``totp_secret``, token data or recovery codes. The raw invite or
reset token is returned exactly once, embedded in the one-time URL, and is never logged.
"""

import logging
import threading
from functools import reduce
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator

from plex_playlist_sync.api.dependencies import get_config, get_db, require_admin, tier_of
from plex_playlist_sync.api.schemas.admin_users import (
    AccountSettings,
    AdminUser,
    AdminUserAck,
    CreatedAdminUser,
    PermissionLabel,
    ResetPasswordResponse,
)
from plex_playlist_sync.config import Config
from plex_playlist_sync.local_login import throttle_key
from plex_playlist_sync.models import UserPermission
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

ALL_PERMISSION_BITS = reduce(lambda acc, perm: acc | int(perm), UserPermission, 0)
MAX_QUOTA = 1_000_000
MAX_WINDOW_DAYS = 3650

PERMISSION_LABELS: tuple[tuple[UserPermission, str], ...] = (
    (UserPermission.ADMIN, "Administrator"),
    (UserPermission.REQUEST, "Request music"),
    (UserPermission.AUTO_APPROVE, "Auto-approve track requests"),
    (UserPermission.AUTO_APPROVE_ALBUM, "Auto-approve album requests"),
    (UserPermission.AUTO_APPROVE_DISCOGRAPHY, "Auto-approve discography requests"),
    (UserPermission.MANAGE_REQUESTS, "Manage requests"),
    (UserPermission.REPORT_ISSUE, "Report media issues"),
    (UserPermission.AUTO_REQUEST_PLAYLISTS, "Auto-request playlist tracks"),
)

# Serialises guard-check-then-write sequences (last admin, self rules) within this process.
_mutation_lock = threading.RLock()

_QUOTA_FIELDS = ("quota_tracks", "quota_albums", "quota_discographies", "quota_window_days")
_NESTED_QUOTA_KEYS = {
    "tracks": "quota_tracks",
    "albums": "quota_albums",
    "discographies": "quota_discographies",
    "window_days": "quota_window_days",
}


def admin_core_user(
    current_user: dict[str, Any] = Depends(require_admin),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """An admin acting on core. 404 on the gateway tier; API-key callers are refused (a person must act)."""
    if tier_of(config) == "gateway":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    if str(current_user.get("id")) == "api_key_user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User management requires an administrator session, not an API key",
        )
    return current_user


router = APIRouter(dependencies=[Depends(admin_core_user)])


# --------------------------------------------------------------------------- validation helpers


def validate_permissions(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("permissions must be an integer")
    if value < 0 or value & ~ALL_PERMISSION_BITS:
        raise ValueError("Unknown permission bits")
    return int(value)


def validate_email(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    mail = value.strip()
    if not mail:
        return None
    if len(mail) > 254 or "@" not in mail or any(c in mail for c in "\r\n\x00"):
        raise ValueError("Invalid email address")
    return mail


def _validate_quota(value: Optional[int], *, window: bool = False) -> Optional[int]:
    if value is None:
        return None
    low, high = (1, MAX_WINDOW_DAYS) if window else (0, MAX_QUOTA)
    if value < low or value > high:
        raise ValueError(f"Value must be between {low} and {high}")
    return value


def _http_422(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


class _QuotaBody(BaseModel):
    """Quota overrides, flat (``quota_tracks``) and/or nested (``quotas: {tracks, ...}``). null = use default."""

    quota_tracks: Optional[StrictInt] = None
    quota_albums: Optional[StrictInt] = None
    quota_discographies: Optional[StrictInt] = None
    quota_window_days: Optional[StrictInt] = None
    quotas: Optional[dict[str, Optional[StrictInt]]] = None

    @field_validator("quota_tracks", "quota_albums", "quota_discographies")
    @classmethod
    def _check_count(cls, value: Optional[int]) -> Optional[int]:
        return _validate_quota(value)

    @field_validator("quota_window_days")
    @classmethod
    def _check_window(cls, value: Optional[int]) -> Optional[int]:
        return _validate_quota(value, window=True)

    @field_validator("quotas")
    @classmethod
    def _check_nested(cls, value: Optional[dict[str, Optional[int]]]) -> Optional[dict[str, Optional[int]]]:
        if value is None:
            return None
        for key, item in value.items():
            if key not in _NESTED_QUOTA_KEYS:
                raise ValueError(f"Unknown quota field: {key}")
            _validate_quota(item, window=key == "window_days")
        return value

    def quota_updates(self) -> dict[str, Optional[int]]:
        """Only the quota columns the caller actually sent (null included), flat values winning over nested."""
        updates: dict[str, Optional[int]] = {}
        for key, value in (self.quotas or {}).items():
            updates[_NESTED_QUOTA_KEYS[key]] = value
        for column in _QUOTA_FIELDS:
            if column in self.model_fields_set:
                updates[column] = getattr(self, column)
        return updates


class CreateAdminUserBody(_QuotaBody):
    username: str = Field(..., min_length=1, max_length=64)
    email: Optional[str] = Field(default=None, max_length=254)
    permissions: Optional[StrictInt] = None

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: Optional[str]) -> Optional[str]:
        return validate_email(value)

    @field_validator("permissions")
    @classmethod
    def _check_permissions(cls, value: Optional[int]) -> Optional[int]:
        return None if value is None else validate_permissions(value)


class UpdateAdminUserBody(_QuotaBody):
    permissions: Optional[StrictInt] = None
    email: Optional[str] = Field(default=None, max_length=254)

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: Optional[str]) -> Optional[str]:
        return validate_email(value)

    @field_validator("permissions")
    @classmethod
    def _check_permissions(cls, value: Optional[int]) -> Optional[int]:
        return None if value is None else validate_permissions(value)


class DeleteAdminUserBody(BaseModel):
    confirm_username: str = Field(..., max_length=128)


class AccountSettingsBody(BaseModel):
    require_mfa_local: Optional[StrictBool] = None
    default_quota_tracks: Optional[StrictInt] = None
    default_quota_albums: Optional[StrictInt] = None
    default_quota_discographies: Optional[StrictInt] = None
    default_quota_window_days: Optional[StrictInt] = None

    @field_validator("default_quota_tracks", "default_quota_albums", "default_quota_discographies")
    @classmethod
    def _check_count(cls, value: Optional[int]) -> Optional[int]:
        return _validate_quota(value)

    @field_validator("default_quota_window_days")
    @classmethod
    def _check_window(cls, value: Optional[int]) -> Optional[int]:
        return _validate_quota(value, window=True)


# --------------------------------------------------------------------------- serialisation


def serialize_user(db: Database, row: dict[str, Any], defaults: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The documented admin user shape. Built from an explicit allowlist of fields: no secret can leak."""
    defaults = defaults if defaults is not None else db.get_account_settings()
    overrides = {
        "tracks": row.get("quota_tracks"),
        "albums": row.get("quota_albums"),
        "discographies": row.get("quota_discographies"),
        "window_days": row.get("quota_window_days"),
    }
    effective = {
        "tracks": overrides["tracks"] if overrides["tracks"] is not None else defaults["default_quota_tracks"],
        "albums": overrides["albums"] if overrides["albums"] is not None else defaults["default_quota_albums"],
        "discographies": (
            overrides["discographies"]
            if overrides["discographies"] is not None
            else defaults["default_quota_discographies"]
        ),
        "window_days": (
            overrides["window_days"] if overrides["window_days"] is not None else defaults["default_quota_window_days"]
        ),
    }
    used = db.count_user_requests_by_type(row["id"], max(1, int(effective["window_days"])))
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row.get("email"),
        "auth_type": row.get("auth_type") or "plex",
        "is_admin": bool(row.get("is_admin")),
        "permissions": int(row["permissions"]),
        "disabled": bool(row.get("disabled")),
        "mfa_enabled": bool(row.get("mfa_enabled")),
        "last_login_at": row.get("last_login_at"),
        "created_at": row.get("created_at"),
        "quotas": {"effective": effective, "overrides": overrides},
        "usage": {"tracks": used["track"], "albums": used["album"], "discographies": used["discography"]},
    }


def _admin_row(db: Database, user_id: str) -> Optional[dict[str, Any]]:
    for row in db.list_users_admin():
        if row["id"] == user_id:
            return row
    return None


def _require_row(db: Database, user_id: str) -> dict[str, Any]:
    row = _admin_row(db, user_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return row


def _public_base_url(db: Database, config: Config) -> str:
    base = (db.get_general_settings().get("application_url") or config.application_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="APPLICATION_URL is not configured on core; cannot build the invite link",
        )
    return base


# --------------------------------------------------------------------------- guards


def _guard_not_last_admin(db: Database, target: dict[str, Any], action: str) -> None:
    """Blocks ``action`` when the target is the only remaining active administrator."""
    if target.get("is_admin") and not target.get("disabled") and db.count_active_admins(target["id"]) == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot {action} the last administrator",
        )


def _guard_not_self(actor: dict[str, Any], target: dict[str, Any], message: str) -> None:
    if str(actor.get("id")) == str(target["id"]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


def apply_user_changes(
    db: Database, actor: dict[str, Any], target_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Validates and applies admin edits (column names: permissions, email, quota_*). Returns the stored user row.

    Shared by ``PATCH /api/admin/users/{id}`` and the legacy ``PUT /api/users/{id}``. Enforces: no change of
    one's own admin bit, the last active administrator cannot be demoted, unknown permission bits are
    rejected. Raises ``HTTPException`` (404 unknown user, 400/409 guard, 422 validation).
    """
    if "permissions" in fields:
        try:
            fields["permissions"] = validate_permissions(fields["permissions"])
        except ValueError as exc:
            raise _http_422(exc) from exc
    with _mutation_lock:
        target = _require_row(db, target_id)
        if "permissions" in fields:
            new_admin = bool(fields["permissions"] & int(UserPermission.ADMIN))
            if new_admin != bool(target["is_admin"]):
                _guard_not_self(actor, target, "You cannot change your own administrator status")
                if not new_admin:
                    _guard_not_last_admin(db, target, "demote")
        updated = db.update_user_admin_fields(target_id, fields)
        if updated is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return updated


# --------------------------------------------------------------------------- routes


@router.get("/permissions", response_model=list[PermissionLabel], response_model_exclude_unset=True)
def list_permission_labels() -> list[dict[str, Any]]:
    """The assignable permission bits with human labels."""
    return [{"bit": int(perm), "name": perm.name, "label": label} for perm, label in PERMISSION_LABELS]


@router.get("/users", response_model=list[AdminUser], response_model_exclude_unset=True)
def list_admin_users(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    defaults = db.get_account_settings()
    return [serialize_user(db, row, defaults) for row in db.list_users_admin()]


@router.post("/users", status_code=status.HTTP_201_CREATED, response_model=CreatedAdminUser, response_model_exclude_unset=True)
def create_admin_user(
    body: CreateAdminUserBody,
    response: Response,
    actor: dict[str, Any] = Depends(admin_core_user),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Creates a local user with a 48 h invite. ``invite_url`` carries the only copy of the raw token."""
    base = _public_base_url(db, config)  # fail before creating anything we could not hand out
    try:
        user, raw_token = db.create_local_user(body.username, body.email, body.permissions, str(actor.get("id")))
    except ValueError as exc:
        code = status.HTTP_409_CONFLICT if "already taken" in str(exc) else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    quota_updates = body.quota_updates()
    if quota_updates:
        db.update_user_admin_fields(user["id"], quota_updates)
    row = _require_row(db, user["id"])
    response.headers["Cache-Control"] = "no-store"
    logger.info("Admin %s created local user %s", actor.get("id"), user["id"])
    return {"user": serialize_user(db, row), "invite_url": f"{base}/invite/{raw_token}"}


@router.patch("/users/{user_id}", response_model=AdminUser, response_model_exclude_unset=True)
def update_admin_user(
    user_id: str,
    body: UpdateAdminUserBody,
    actor: dict[str, Any] = Depends(admin_core_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    fields: dict[str, Any] = dict(body.quota_updates())
    if "email" in body.model_fields_set:
        fields["email"] = body.email
    if "permissions" in body.model_fields_set:
        if body.permissions is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="permissions cannot be cleared"
            )
        fields["permissions"] = body.permissions
    if not fields:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No changes supplied")
    apply_user_changes(db, actor, user_id, fields)
    logger.info("Admin %s updated user %s (%s)", actor.get("id"), user_id, ",".join(sorted(fields)))
    return serialize_user(db, _require_row(db, user_id))


@router.post("/users/{user_id}/disable", response_model=AdminUser, response_model_exclude_unset=True)
def disable_admin_user(
    user_id: str,
    actor: dict[str, Any] = Depends(admin_core_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    with _mutation_lock:
        target = _require_row(db, user_id)
        _guard_not_self(actor, target, "You cannot disable your own account")
        _guard_not_last_admin(db, target, "disable")
        db.set_disabled(user_id, True)  # also sets sessions_revoked_at and drops stored sessions
    logger.info("Admin %s disabled user %s", actor.get("id"), user_id)
    return serialize_user(db, _require_row(db, user_id))


@router.post("/users/{user_id}/enable", response_model=AdminUser, response_model_exclude_unset=True)
def enable_admin_user(
    user_id: str,
    actor: dict[str, Any] = Depends(admin_core_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    _require_row(db, user_id)
    db.set_disabled(user_id, False)
    logger.info("Admin %s enabled user %s", actor.get("id"), user_id)
    return serialize_user(db, _require_row(db, user_id))


@router.post("/users/{user_id}/reset-password", response_model=ResetPasswordResponse, response_model_exclude_unset=True)
def reset_admin_user_password(
    user_id: str,
    response: Response,
    actor: dict[str, Any] = Depends(admin_core_user),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Issues a single-use reset link (local accounts only) and signs the user out everywhere."""
    target = _require_row(db, user_id)
    if target["auth_type"] != "local":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Password reset is only available for local accounts"
        )
    base = _public_base_url(db, config)
    raw_token = db.issue_token(user_id, "reset", str(actor.get("id")))
    db.revoke_sessions(user_id)
    db.clear_login_attempts(throttle_key("user", str(target["username"]).lower()))
    db.clear_login_attempts(throttle_key("lock", str(target["username"]).lower()))
    response.headers["Cache-Control"] = "no-store"
    logger.info("Admin %s issued a password reset for user %s", actor.get("id"), user_id)
    return {"reset_url": f"{base}/invite/{raw_token}"}


@router.post("/users/{user_id}/reset-mfa", response_model=AdminUser, response_model_exclude_unset=True)
def reset_admin_user_mfa(
    user_id: str,
    actor: dict[str, Any] = Depends(admin_core_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    target = _require_row(db, user_id)
    if target["auth_type"] != "local":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="MFA is only available for local accounts"
        )
    db.clear_mfa(user_id)
    db.revoke_sessions(user_id)
    logger.info("Admin %s reset MFA for user %s", actor.get("id"), user_id)
    return serialize_user(db, _require_row(db, user_id))


@router.post("/users/{user_id}/revoke-sessions", response_model=AdminUserAck, response_model_exclude_unset=True)
def revoke_admin_user_sessions(
    user_id: str,
    actor: dict[str, Any] = Depends(admin_core_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    _require_row(db, user_id)
    db.revoke_sessions(user_id)
    logger.info("Admin %s revoked sessions of user %s", actor.get("id"), user_id)
    return {"status": "success", "id": user_id}


@router.delete("/users/{user_id}", response_model=AdminUserAck, response_model_exclude_unset=True)
def delete_admin_user(
    user_id: str,
    body: DeleteAdminUserBody = Body(...),
    actor: dict[str, Any] = Depends(admin_core_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Deletes the user and their data and writes a tombstone so they cannot come back by signing in."""
    with _mutation_lock:
        target = _require_row(db, user_id)
        _guard_not_self(actor, target, "You cannot delete your own account")
        _guard_not_last_admin(db, target, "delete")
        if body.confirm_username != target["username"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="confirm_username does not match the username"
            )
        if not db.delete_user_cascade(user_id, deleted_by=str(actor.get("id"))):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    logger.info("Admin %s deleted user %s", actor.get("id"), user_id)
    return {"status": "deleted", "id": user_id}


@router.post("/users/{user_id}/restore", response_model=AdminUserAck, response_model_exclude_unset=True)
def restore_admin_user(
    user_id: str,
    actor: dict[str, Any] = Depends(admin_core_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Lifts the tombstone of a deleted Plex user so they can sign in again (as a fresh, default user)."""
    if user_id.startswith("local-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Local accounts cannot be restored; create a new invite instead",
        )
    if not db.remove_tombstone(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No removed user with that id")
    logger.info("Admin %s restored user %s", actor.get("id"), user_id)
    return {"status": "restored", "id": user_id}


@router.get("/settings/accounts", response_model=AccountSettings, response_model_exclude_unset=True)
def get_account_settings(db: Database = Depends(get_db)) -> dict[str, Any]:
    return db.get_account_settings()


@router.put("/settings/accounts", response_model=AccountSettings, response_model_exclude_unset=True)
def put_account_settings(
    body: AccountSettingsBody,
    actor: dict[str, Any] = Depends(admin_core_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    updates = {key: getattr(body, key) for key in body.model_fields_set if getattr(body, key) is not None}
    if not updates:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No changes supplied")
    result = db.update_account_settings(updates)
    logger.info("Admin %s updated account settings (%s)", actor.get("id"), ",".join(sorted(updates)))
    return result


__all__ = ["apply_user_changes", "router", "serialize_user", "validate_permissions"]

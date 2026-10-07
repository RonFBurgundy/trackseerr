"""Plex Home users management routes."""

import logging
from typing import Any, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, status

from pydantic import BaseModel

from plex_playlist_sync.api.dependencies import (
    tier_of,
    get_config,
    get_db,
    get_media_client,
    get_plex_client,
    require_media_server,
    require_admin,
    require_user,
)
from plex_playlist_sync.api.routes.admin_users import MAX_QUOTA, MAX_WINDOW_DAYS, apply_user_changes
from plex_playlist_sync.api.schemas.users import CurrentUserProfile, UserRecord
from plex_playlist_sync.clients.plex import PlexClient
from plex_playlist_sync.media_servers import MediaServer, as_media_server, describe_error, import_server_users
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import UserPermission
from plex_playlist_sync.redaction import safe_exc
from plex_playlist_sync.request_submission import effective_quota_limits, quota_snapshot
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()


class UpdateUserGovernanceBody(BaseModel):
    permissions: Optional[int] = None
    request_limit_quota: Optional[int] = None
    request_limit_days: Optional[int] = None
    is_admin: Optional[bool] = None


@router.get("/me", response_model=CurrentUserProfile, response_model_exclude_unset=True)
def get_current_user_profile(
    current_user: dict[str, Any] = Depends(require_user),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Returns current user profile, permissions bitmask, and rolling request quota telemetry."""
    user_id = str(current_user["id"])
    user = db.get_user(user_id) or current_user

    permissions = int(user.get("permissions") if user.get("permissions") is not None else UserPermission.DEFAULT)
    # Per-type quotas (tracks / albums / discographies) are the source of truth: see ``quotas``. The legacy
    # single-number fields mirror the ALBUM quota (``request_limit_quota`` is the album override), the type the
    # UI has always requested by default, over the same rolling window.
    quotas = quota_snapshot(db, {**user, "forwarded": bool(current_user.get("forwarded"))})
    limits = effective_quota_limits(db, user_id)
    quota_limit = limits["albums"]
    rolling_days = quotas["window_days"]
    active_requests = quotas["used"]["albums"]
    remaining_quota = max(0, quota_limit - active_requests)

    return {
        "id": user["id"],
        "username": user["username"],
        "email": user.get("email"),
        "is_admin": bool(user.get("is_admin")) and not current_user.get("forwarded"),
        "permissions": permissions,
        "request_limit_quota": user.get("request_limit_quota"),
        "quota_limit": quota_limit,
        "request_limit_days": rolling_days,
        "rolling_days": rolling_days,
        "active_requests": active_requests,
        "active_request_count": active_requests,
        "remaining_quota": remaining_quota,
        "quotas": quotas,
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
        "tier": tier_of(config),
    }


@router.get("", response_model=list[UserRecord], response_model_exclude_unset=True)
def list_users(
    _admin: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    """Returns discovered Plex Home users (admin only)."""
    return db.list_users()


@router.put("/{user_id}", response_model=UserRecord, response_model_exclude_unset=True)
def update_user_governance_route(
    user_id: str,
    body: UpdateUserGovernanceBody,
    admin: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Updates user governance, permissions, and request quotas (admin only).

    Legacy shape kept for compatibility. It delegates to the same guarded update as
    ``PATCH /api/admin/users/{id}``: ``request_limit_quota`` sets the user's track and album overrides,
    ``request_limit_days`` the window override, and ``is_admin`` the ADMIN permission bit.
    """
    if str(admin.get("id")) == "api_key_user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User management requires an administrator session, not an API key",
        )
    fields_set = body.model_fields_set
    fields: dict[str, Any] = {}
    if "permissions" in fields_set and body.permissions is not None:
        fields["permissions"] = body.permissions
    if "request_limit_quota" in fields_set:
        fields["quota_tracks"] = body.request_limit_quota
        fields["quota_albums"] = body.request_limit_quota
    if "request_limit_days" in fields_set:
        fields["quota_window_days"] = body.request_limit_days
    if "is_admin" in fields_set and body.is_admin is not None:
        current = db.get_user(user_id)
        if current is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User {user_id} not found")
        base = fields.get("permissions", current["permissions"])
        base = (base | int(UserPermission.ADMIN)) if body.is_admin else (base & ~int(UserPermission.ADMIN))
        fields["permissions"] = base
    for column in ("quota_tracks", "quota_albums"):
        if fields.get(column) is not None and not 0 <= fields[column] <= MAX_QUOTA:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Quota must be between 0 and {MAX_QUOTA}"
            )
    window = fields.get("quota_window_days")
    if window is not None and not 1 <= window <= MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Window must be between 1 and {MAX_WINDOW_DAYS} days"
        )
    if not fields:
        existing = db.get_user(user_id)
        if existing is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User {user_id} not found")
        return existing
    if "permissions" in fields and str(admin.get("id")) == "api_key_user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Changing permissions requires an administrator session, not an API key",
        )
    apply_user_changes(db, admin, user_id, fields)
    updated = db.get_user(user_id)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"User {user_id} not found")
    return updated


@router.post("/refresh", dependencies=[Depends(require_media_server)], response_model=list[UserRecord], response_model_exclude_unset=True)
def refresh_users(
    _admin: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
    plex_client: Optional[Union[PlexClient, MediaServer]] = Depends(get_media_client),
) -> list[dict[str, Any]]:
    """Discovers users from the media server and upserts them to DB (admin only)."""
    server = as_media_server(plex_client)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Plex client is not configured on hub",
        )

    try:
        discovered_users = server.list_users()
    except Exception as e:
        logger.error("Failed to discover Plex Home users: %s", describe_error(e))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to query Plex users: {describe_error(e)}",
        )

    imported, skipped = import_server_users(db, server.kind, discovered_users)
    logger.info("User refresh: %d imported, %d skipped", imported, skipped)

    return db.list_users()

"""Response models for ``/api/users`` (``api/routes/users.py``) and the user record shared by auth, account and admin routes.

``UserRecord`` mirrors ``Database.get_user`` / ``list_users`` (an explicit column allowlist: no password hash, no TOTP secret,
no session data). ``forwarded`` is only present on a gateway-asserted principal, and an API-key principal carries just
``id``/``username``/``is_admin``/``permissions``, so everything beyond those is optional and routes use
``response_model_exclude_unset=True`` to keep absent keys absent.
"""

from typing import Optional

from plex_playlist_sync.api.response_models import ApiModel


class UserRecord(ApiModel):
    id: str
    username: str
    email: Optional[str] = None
    is_admin: bool
    permissions: Optional[int] = None
    request_limit_quota: Optional[int] = None
    request_limit_days: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    auth_type: Optional[str] = None
    disabled: Optional[bool] = None
    forwarded: Optional[bool] = None  # gateway-forwarded principals only


class QuotaUsage(ApiModel):
    tracks: int
    albums: int
    discographies: int


class QuotaSnapshot(ApiModel):
    """``request_submission.quota_snapshot``: limits are ``None`` for an unlimited admin."""

    tracks: Optional[int] = None
    albums: Optional[int] = None
    discographies: Optional[int] = None
    window_days: int
    used: QuotaUsage


class CurrentUserProfile(ApiModel):
    id: str
    username: str
    email: Optional[str] = None
    is_admin: bool
    permissions: int
    request_limit_quota: Optional[int] = None
    quota_limit: int
    request_limit_days: int
    rolling_days: int
    active_requests: int
    active_request_count: int
    remaining_quota: int
    quotas: QuotaSnapshot
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    tier: str

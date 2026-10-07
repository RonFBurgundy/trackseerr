"""Response models for ``/api/admin/users`` (``api/routes/admin_users.py``).

``AdminUser`` is built by ``serialize_user`` from an explicit allowlist, so no password hash, TOTP secret, session or recovery
code can reach it. ``invite_url`` / ``reset_url`` embed the one-time raw token and are returned once, on purpose, to the admin
who issued them.
"""

from typing import Optional

from plex_playlist_sync.api.response_models import ApiModel


class AdminQuotaValues(ApiModel):
    tracks: Optional[int] = None
    albums: Optional[int] = None
    discographies: Optional[int] = None
    window_days: Optional[int] = None


class AdminUserQuotas(ApiModel):
    effective: AdminQuotaValues
    overrides: AdminQuotaValues


class AdminUserUsage(ApiModel):
    tracks: int
    albums: int
    discographies: int


class AdminUser(ApiModel):
    id: str
    username: str
    email: Optional[str] = None
    auth_type: str
    is_admin: bool
    permissions: int
    disabled: bool
    mfa_enabled: bool
    last_login_at: Optional[str] = None
    created_at: Optional[str] = None
    quotas: AdminUserQuotas
    usage: AdminUserUsage


class PermissionLabel(ApiModel):
    bit: int
    name: str
    label: str


class CreatedAdminUser(ApiModel):
    user: AdminUser
    invite_url: str  # one-time link embedding the raw invite token


class ResetPasswordResponse(ApiModel):
    reset_url: str  # one-time link embedding the raw reset token


class AdminUserAck(ApiModel):
    status: str
    id: str


class AccountSettings(ApiModel):
    require_mfa_local: bool
    default_quota_tracks: int
    default_quota_albums: int
    default_quota_discographies: int
    default_quota_window_days: int

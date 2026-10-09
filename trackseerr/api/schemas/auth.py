"""Response models for ``/api/auth`` (``api/routes/auth.py``).

``PlexVerifyResponse.token`` is the freshly minted signed session token, returned to the signing-in user on purpose (the
same value is set as the HttpOnly cookie). No other secret appears in these responses.
"""

from typing import Optional

from trackseerr.api.response_models import ApiModel
from trackseerr.api.schemas.users import UserRecord


class PlexPinResponse(ApiModel):
    id: int
    code: str
    auth_url: str


class PlexVerifyResponse(ApiModel):
    token: str  # the new session token, deliberately returned to its owner
    user: UserRecord


class LogoutResponse(ApiModel):
    status: str
    message: str


class MeResponse(ApiModel):
    user: UserRecord
    tier: str


class LocalLoginUser(ApiModel):
    """Gateway tier answers with just ``{id, username, is_admin}``; core answers with the full user record."""

    id: str
    username: str
    is_admin: bool
    email: Optional[str] = None
    permissions: Optional[int] = None
    request_limit_quota: Optional[int] = None
    request_limit_days: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    auth_type: Optional[str] = None
    disabled: Optional[bool] = None


class LocalLoginResponse(ApiModel):
    user: LocalLoginUser
    mfa_enrollment_required: bool


class InviteInfoResponse(ApiModel):
    username: str
    purpose: str
    expires_at: Optional[str] = None


class InviteAcceptedResponse(ApiModel):
    status: str
    username: str

"""Response models for ``/api/account`` (``api/routes/account.py``).

``MfaSetupResponse.secret`` and ``RecoveryCodesResponse.recovery_codes`` are deliberate one-time reveals to the owning user
after password re-authentication (the pending TOTP secret and freshly generated recovery codes). Nothing stored (password
hash, confirmed TOTP secret, hashed recovery codes) is ever declared here.
"""

from typing import Optional

from trackseerr.api.response_models import ApiModel
from trackseerr.api.schemas.users import QuotaSnapshot


class AutoApprove(ApiModel):
    tracks: bool
    albums: bool
    discographies: bool


class AccountResponse(ApiModel):
    id: str
    username: str
    auth_type: str
    mfa_enabled: bool
    mfa_required: bool
    recovery_codes_remaining: int
    quotas: QuotaSnapshot
    auto_approve: AutoApprove


class ChangePasswordResponse(ApiModel):
    status: str
    # Only on a gateway-forwarded call: the gateway re-issues the browser session from these markers.
    reissue_session: Optional[bool] = None
    session_floor_us: Optional[int] = None


class MfaSetupResponse(ApiModel):
    secret: str  # one-time reveal of the pending TOTP secret to its owner
    otpauth_uri: str


class RecoveryCodesResponse(ApiModel):
    recovery_codes: list[str]  # one-time reveal


class StatusResponse(ApiModel):
    status: str

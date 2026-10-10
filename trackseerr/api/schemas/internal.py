"""Response models for ``/api/internal`` (``api/routes/internal.py``), consumed by ``clients/core_client.py`` on the gateway.

The gateway reads these as plain dicts (``body.get("valid")``, ``body.get("user")``, ...), so key names and presence must not
change; routes use ``response_model_exclude_unset=True``. No credential or token is declared on any of them.
"""

from typing import Optional

from trackseerr.api.response_models import ApiModel


class VerifiedUser(ApiModel):
    id: str
    username: str


class VerifyLocalResponse(ApiModel):
    user: VerifiedUser
    mfa_required: bool
    mfa_enrollment_required: bool
    session_floor_us: int


class SessionStatusResponse(ApiModel):
    valid: bool
    reason: Optional[str] = None  # only when valid is false


class HelloResponse(ApiModel):
    protocol: int
    version: str
    role: str
    instance_id: str


class PlexIdentityResponse(ApiModel):
    machine_identifier: Optional[str] = None


class HeartbeatResponse(ApiModel):
    ok: bool
    protocol: int
    version: str

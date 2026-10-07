"""Response models for the admin deployment endpoints (``api/routes/deployment.py``)."""

from typing import Optional

from plex_playlist_sync.api.response_models import ApiModel


class GatewayStatus(ApiModel):
    configured: bool
    public_url: Optional[str] = None
    last_seen_at: Optional[str] = None
    version: Optional[str] = None
    protocol: Optional[int] = None
    version_match: Optional[bool] = None
    active_sessions: Optional[int] = None
    state: str


class RoleChangeNotice(ApiModel):
    active: bool
    from_role: Optional[str] = None
    to_role: Optional[str] = None
    changed_at: Optional[str] = None
    checklist: list[str]

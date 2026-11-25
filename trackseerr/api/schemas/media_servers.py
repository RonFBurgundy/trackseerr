"""Pydantic schemas for the multi-server media-servers admin API."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from trackseerr.api.response_models import ApiModel


class MediaServerItem(ApiModel):
    id: str
    kind: Literal["plex", "subsonic", "jellyfin"]
    source: str
    name: str
    url: str
    username: str
    password: str
    api_key: str
    music_section: str
    verify_ssl: bool
    enabled: bool
    use_refresh: bool
    use_playlists: bool
    use_users: bool
    use_scrobbles: bool
    is_login: bool
    sort_order: int
    locked: bool
    capabilities: dict[str, bool]


class MediaServerPayload(BaseModel):
    id: Optional[str] = None
    kind: Literal["plex", "subsonic", "jellyfin"]
    name: str = ""
    url: str = ""
    username: str = ""
    password: str = ""
    api_key: str = ""
    music_section: str = ""
    verify_ssl: bool = True
    enabled: bool = True
    use_refresh: bool = True
    use_playlists: bool = True
    use_users: bool = True
    use_scrobbles: bool = True
    sort_order: int = 0


class MediaServerLoginPayload(BaseModel):
    server_id: Optional[str] = None


class MediaServerTestResult(ApiModel):
    ok: bool
    message: str = ""


class MediaServerDeletedResponse(ApiModel):
    status: str
    id: str


class MediaServerLoginResponse(ApiModel):
    status: str
    server_id: Optional[str] = None

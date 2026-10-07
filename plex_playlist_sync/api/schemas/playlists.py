"""Response models for ``/api/playlists`` (``api/routes/playlists.py``)."""

from typing import Optional

from plex_playlist_sync.api.response_models import ApiModel


class PlaylistRecord(ApiModel):
    """A ``playlists`` row plus its target user ids. Non-admins only receive playlists they created or that target them."""

    id: str
    name: str
    service: str
    description: str = ""
    poster_url: str = ""
    enabled: bool
    creator_id: Optional[str] = None
    tracks_json: Optional[str] = None  # JSON text of the stored track list (direct imports); null for service playlists
    last_synced_at: Optional[str] = None
    sync_status: str
    monitor_mode: str
    created_at: str
    updated_at: str
    targets: list[str]


class FeaturedChart(ApiModel):
    id: str
    name: str
    service: str
    url_or_id: str
    description: str
    poster_url: str
    category: str


class SmartMixPreset(ApiModel):
    mix_type: str
    name: str
    description: str
    icon: str


class PlaylistTargetsResponse(ApiModel):
    id: str
    targets: list[str]


class PlaylistEnabledResponse(ApiModel):
    id: str
    enabled: bool
    monitor_mode: Optional[str] = None


class PlaylistMonitorModeResponse(ApiModel):
    id: str
    monitor_mode: str


class PlaylistDeletedResponse(ApiModel):
    status: str
    id: str


class PlaylistImportResponse(ApiModel):
    id: str
    name: str
    service: Optional[str] = None
    track_count: int
    matched_count: int
    missing_count: int
    targets: list[str]
    status: str

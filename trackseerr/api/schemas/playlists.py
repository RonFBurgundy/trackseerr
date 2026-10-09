"""Response models for ``/api/playlists`` (``api/routes/playlists.py``)."""

from typing import Optional

from trackseerr.api.response_models import ApiModel


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
    source_kind: Optional[str] = None  # listening playlists: loved, top_tracks, playlist or created_for
    source_ref: Optional[str] = None  # listening playlists: Last.fm period, ListenBrainz playlist id or created-for kind
    auto_request: bool = False  # listening playlists: request missing tracks automatically (needs permission)
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


class ListeningSourceItem(ApiModel):
    kind: str
    ref: str
    label: str
    date: Optional[str] = None


class ListeningProviderSources(ApiModel):
    linked: bool
    username: Optional[str] = None
    available: bool
    reason: Optional[str] = None
    lists: list[ListeningSourceItem]


class ListeningSourcesResponse(ApiModel):
    lastfm: ListeningProviderSources
    listenbrainz: ListeningProviderSources
    can_auto_request: bool


class PlaylistAutoRequestResponse(ApiModel):
    id: str
    auto_request: bool

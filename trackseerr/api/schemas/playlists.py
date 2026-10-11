"""Response models for ``/api/playlists`` (``api/routes/playlists.py``)."""

from typing import Literal, Optional

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
    tracks_json: Optional[str] = None  # stored track list: direct imports, or the last fetched snapshot for service playlists
    last_synced_at: Optional[str] = None
    sync_status: str
    monitor_mode: str
    source_kind: Optional[str] = None  # listening playlists (loved, top_tracks, playlist, created_for) or smart collections (smart)
    source_ref: Optional[str] = None  # listening playlists: Last.fm period, ListenBrainz playlist id or created-for kind; smart: rules JSON
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


class PlaylistTrackItem(ApiModel):
    position: int
    title: str
    artist: str
    album: str = ""
    status: Literal["pending", "matched", "missing"]
    missing_track_id: Optional[int] = None


class PlaylistTracksResponse(ApiModel):
    tracks: list[PlaylistTrackItem]
    total: int
    missing: int
    synced: bool

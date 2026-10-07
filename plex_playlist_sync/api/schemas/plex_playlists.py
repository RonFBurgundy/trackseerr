"""Response models for ``/api/plex-playlists`` (``api/routes/plex_playlists.py``).

204 routes (delete snapshot, delete playlist, remove item) have no body and stay untyped. ``/adopt`` returns a
``PlaylistRecord`` from ``schemas/playlists.py``. Non-admins only ever see their own Plex profile.
"""

from typing import Optional

from plex_playlist_sync.api.response_models import ApiModel


class PlexUser(ApiModel):
    username: str
    is_admin_account: bool
    is_self: bool


class PlexMix(ApiModel):
    mix_key: str
    title: str
    hub_title: str
    track_count: Optional[int] = None
    thumb_url: Optional[str] = None
    snapshot_id: Optional[str] = None


class MixSnapshot(ApiModel):
    id: str
    plex_user: str
    mix_key: str
    mix_title: str
    playlist_title: str
    rating_key: Optional[str] = None
    auto_refresh: bool
    last_refreshed_at: Optional[str] = None


class PlexPlaylistSummary(ApiModel):
    rating_key: str
    title: str
    kind: str
    owner: str
    ignored: bool
    track_count: int
    duration_ms: int
    thumb_url: Optional[str] = None
    updated_at: Optional[str] = None
    trackseerr_playlist_id: Optional[str] = None
    plex_user: str


class PlexPlaylistItem(ApiModel):
    playlist_item_id: int
    rating_key: str
    title: str
    artist: str
    album: str
    duration_ms: int


class PlaylistCopyResult(ApiModel):
    username: str
    success: bool
    rating_key: Optional[str] = None
    error: Optional[str] = None
    copied_tracks: int
    omitted_tracks: int

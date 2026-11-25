"""Response models for ``/api/media-servers/{server_id}/playlists`` (``api/routes/server_playlists.py``)."""

from typing import Optional

from trackseerr.api.response_models import ApiModel


class ServerPlaylistSummary(ApiModel):
    id: str
    title: str
    owner: str = ""
    track_count: int = 0
    adopted_playlist_id: Optional[str] = None


class ServerPlaylistTrack(ApiModel):
    title: str
    artist: str = ""
    album: str = ""

"""Response models for ``/api/missing`` (``api/routes/missing.py``).

Skipped (file / plain-text / XML bodies): ``/csv``, ``/rss``, ``/text``. Everything here is admin only.
"""

from typing import Optional

from trackseerr.api.response_models import ApiModel


class MissingTrack(ApiModel):
    id: int
    playlist_id: str
    title: str
    artist: str
    album: str = ""
    url: str = ""
    lidarr_status: str
    list_applied_at: Optional[str] = None
    artist_added_by_item: int = 0
    attempts: int = 0
    next_attempt_at: Optional[str] = None
    created_at: str


class MediaTrackHit(ApiModel):
    """A media-server track search hit. Plex adds ``thumb``; Jellyfin and Subsonic add ``id``."""

    id: Optional[str] = None
    rating_key: str
    title: str
    artist: str
    album: Optional[str] = None
    duration: Optional[float] = None
    thumb: Optional[str] = None


class MatchOverride(ApiModel):
    id: int
    source_title: str
    source_artist: str
    plex_rating_key: str
    plex_title: str
    plex_artist: str
    created_by: Optional[str] = None
    created_at: str


class MatchCreatedResponse(ApiModel):
    status: str
    override: MatchOverride


class MatchDeletedResponse(ApiModel):
    status: str
    id: int


class GrabResult(ApiModel):
    """``acquisition_coordinator.search_and_grab`` result: success carries the download, failures a message."""

    success: bool
    message: Optional[str] = None
    candidates_count: Optional[int] = None
    delayed: Optional[bool] = None
    pending_id: Optional[int] = None
    release_at: Optional[str] = None
    mode_changed: Optional[bool] = None
    retryable: Optional[bool] = None
    reason: Optional[str] = None
    download_id: Optional[str] = None
    download_hash: Optional[str] = None
    release: Optional[str] = None
    client: Optional[str] = None
    score: Optional[int] = None

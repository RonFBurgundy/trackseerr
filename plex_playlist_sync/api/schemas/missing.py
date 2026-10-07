"""Response models for ``/api/missing`` (``api/routes/missing.py``).

Skipped (file / plain-text / XML bodies): ``/csv``, ``/rss``, ``/text``. Everything here is admin only.
"""

from typing import Any, Optional

from plex_playlist_sync.api.response_models import ApiModel


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


class LidarrConnectionStatus(ApiModel):
    """``LidarrClient.test_connection()`` or the "not configured" stub."""

    online: bool
    message: Optional[str] = None
    version: Optional[str] = None
    app_name: Optional[str] = None
    error: Optional[str] = None


class LidarrStatusResponse(ApiModel):
    """Native mode: only ``mode`` and ``connected`` (null). Lidarr mode: the rest."""

    mode: Optional[str] = None
    connected: Optional[bool] = None
    configured: Optional[bool] = None
    url: Optional[str] = None
    auto_search: Optional[bool] = None
    status: Optional[LidarrConnectionStatus] = None


class LidarrQueueStatus(ApiModel):
    is_running: bool
    is_paused: bool
    total_items: int
    processed_items: int
    remaining_items: int
    successful_items: int
    failed_items: int
    current_artist: Optional[str] = None
    current_album: Optional[str] = None
    delay_seconds: float
    auto_search: bool
    started_at: Optional[str] = None
    last_processed_at: Optional[str] = None
    is_rate_limited: bool
    rate_limit_seconds_remaining: int
    message: str


class LidarrQueueAction(LidarrQueueStatus):
    action_status: Optional[str] = None
    action_message: Optional[str] = None


class LidarrOutcome(ApiModel):
    status: str
    album_id: Optional[int] = None
    message: str


class LidarrPushItemResult(ApiModel):
    """One ``LidarrClient.search_and_add_track`` result: ``added``/``already_monitored`` or a raw failure dict."""

    status: str
    artist: Optional[str] = None
    album: Optional[str] = None
    lidarr_id: Optional[int] = None
    artist_id: Optional[int] = None
    added: Optional[bool] = None
    matched_album_ids: Optional[list[int]] = None
    searched: Optional[bool] = None
    outcomes: Optional[list[LidarrOutcome]] = None
    retry_after: Optional[int] = None
    message: Optional[str] = None


class LidarrPushResponse(ApiModel):
    """Trickle mode reports the queue; synchronous mode reports counts and per-item results."""

    status: str
    trickle: bool
    queued_count: Optional[int] = None
    auto_search: Optional[bool] = None
    delay_seconds: Optional[float] = None
    message: Optional[str] = None
    queue_status: Optional[LidarrQueueStatus] = None
    total_requested: Optional[int] = None
    deduplicated_items: Optional[int] = None
    added: Optional[int] = None
    already_monitored: Optional[int] = None
    failed: Optional[int] = None
    results: Optional[list[LidarrPushItemResult]] = None


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

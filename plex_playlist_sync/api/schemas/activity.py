"""Response models for ``/api/activity`` (``api/routes/activity.py``), built by ``activity_service``.

Native rows and Lidarr-proxied rows share one record shape per list. Values that Lidarr or the download client may omit are
optional. Messages and reasons are already passed through ``redact_text`` by the service.
"""

from typing import Optional

from plex_playlist_sync.api.response_models import ApiModel
from plex_playlist_sync.api.schemas.library import IndexGroup


class Seeding(ApiModel):
    ratio: float
    ratio_target: Optional[float] = None
    seeding_minutes: int
    time_target_minutes: Optional[int] = None
    removes_in_minutes: Optional[int] = None
    action: str


class QueueRecord(ApiModel):
    id: str
    source: str
    artist: Optional[str] = None
    album: Optional[str] = None
    title: Optional[str] = None
    release_title: Optional[str] = None
    item_type: str
    quality: Optional[str] = None
    protocol: Optional[str] = None
    indexer: Optional[str] = None
    client: Optional[str] = None
    status: str
    progress: float
    size_bytes: int
    sizeleft_bytes: int
    eta_seconds: Optional[int] = None
    added_at: Optional[str] = None
    stalled: bool
    stalled_reason: Optional[str] = None
    messages: list[str]
    request_id: Optional[str] = None
    download_id: Optional[str] = None
    needs_manual_import: bool
    unmatched_count: int
    seeding: Optional[Seeding] = None


class HistoryRecord(ApiModel):
    id: str
    source: str
    event: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    track: Optional[str] = None
    artist_id: Optional[str] = None
    album_id: Optional[str] = None
    title: Optional[str] = None
    release_title: Optional[str] = None
    quality: Optional[str] = None
    indexer: Optional[str] = None
    client: Optional[str] = None
    date: Optional[str] = None
    message: Optional[str] = None
    can_mark_failed: bool


class BlocklistRecord(ApiModel):
    id: str
    source: str
    artist: Optional[str] = None
    artist_id: Optional[str] = None
    album: Optional[str] = None
    title: Optional[str] = None
    release_title: Optional[str] = None
    quality: Optional[str] = None
    indexer: Optional[str] = None
    protocol: Optional[str] = None
    reason: Optional[str] = None
    date: Optional[str] = None


class PagedBase(ApiModel):
    mode: str
    page: int
    page_size: int
    total: int
    sort_key: str
    sort_dir: str


class QueuePage(PagedBase):
    records: list[QueueRecord]


class HistoryPage(PagedBase):
    records: list[HistoryRecord]


class BlocklistPage(PagedBase):
    records: list[BlocklistRecord]


class HistoryIndexResponse(ApiModel):
    """Scrubber groups; ``empty_index`` (Lidarr mode) is the same shape with no groups."""

    sort_key: str
    sort_dir: str
    total: int
    groups: list[IndexGroup]


class ActionResult(ApiModel):
    success: bool
    message: str

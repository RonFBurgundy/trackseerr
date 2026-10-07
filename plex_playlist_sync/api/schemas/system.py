"""Response models for the routes of ``/api/system`` that were untyped (``api/routes/system.py``).

The status and task-list models (``SystemStatusResponse``, ``ScheduledTaskItem``) already lived in ``system.py`` and now
derive from ``ApiModel`` there. The SSE stream and the log download are not modelled.
"""

from typing import Any, Optional

from plex_playlist_sync.api.response_models import ApiModel


class MediaServerCapabilities(ApiModel):
    playlists: bool
    users: bool
    mixes: bool
    library_refresh: bool
    file_paths: bool


class MediaServerStatus(ApiModel):
    type: str
    connected: bool
    capabilities: MediaServerCapabilities


class SystemEvent(ApiModel):
    id: int
    event_type: str
    severity: Optional[str] = None
    source: Optional[str] = None
    message: str
    details_json: Optional[str] = None
    # Free-form: each event type attaches its own context, decoded from ``details_json``.
    details: Optional[dict[str, Any]] = None
    created_at: Optional[str] = None


class SystemEventsPage(ApiModel):
    items: list[SystemEvent]
    total: int
    page: int
    page_size: int


class SuccessFlag(ApiModel):
    success: bool


class LogEntry(ApiModel):
    id: str
    timestamp: str
    level: str
    name: str
    message: str


class TaskActionResult(ApiModel):
    success: bool
    message: str


class JobRecord(ApiModel):
    id: str
    task_id: str
    name: str
    state: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    message: Optional[str] = None


class JobQueueSnapshot(ApiModel):
    running: list[JobRecord]
    queued: list[JobRecord]
    recent: list[JobRecord]


class LidarrHealthCheck(ApiModel):
    source: str
    type: str
    message: str
    wiki_url: Optional[str] = None


class LidarrHealthResponse(ApiModel):
    mode: str
    reachable: Optional[bool] = None  # null in native mode (Lidarr is never contacted)
    version: Optional[str] = None
    health: list[LidarrHealthCheck]

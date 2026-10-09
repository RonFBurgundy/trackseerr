"""Response models for the routes of ``/api/system`` that were untyped (``api/routes/system.py``).

The status and task-list models (``SystemStatusResponse``, ``ScheduledTaskItem``) already lived in ``system.py`` and now
derive from ``ApiModel`` there. The SSE stream and the log download are not modelled.
"""

from typing import Any, Optional

from trackseerr.api.response_models import ApiModel


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


class TaskProgress(ApiModel):
    """How far a running task is. Fields are null when the task cannot tell."""

    current: Optional[int] = None
    total: Optional[int] = None
    message: Optional[str] = None


class TaskRunItem(ApiModel):
    id: int
    task_id: str
    trigger: str  # scheduled | manual | startup | event
    started_at: str
    finished_at: Optional[str] = None
    status: str  # running | success | failed | cancelled
    message: Optional[str] = None
    duration_ms: Optional[int] = None


class ActivityRunningItem(ApiModel):
    task_id: str
    name: str
    started_at: str
    progress: Optional[TaskProgress] = None


class ActivityRecentItem(ApiModel):
    task_id: str
    name: str
    status: str
    finished_at: Optional[str] = None


class ActivityResponse(ApiModel):
    running: list[ActivityRunningItem]
    recent: list[ActivityRecentItem]


class ResourcesResponse(ApiModel):
    """Process-wide stats; every field is null where the platform does not expose it."""

    cpu_percent: Optional[float] = None  # share of one core since the previous sample (100 = one full core)
    rss_bytes: Optional[int] = None
    thread_count: Optional[int] = None
    uptime_seconds: Optional[float] = None


class SystemUpdateResponse(ApiModel):
    current_version: str
    current_commit: Optional[str] = None
    latest_version: Optional[str] = None
    release_url: Optional[str] = None
    published_at: Optional[str] = None
    checked_at: Optional[str] = None
    update_available: bool
    enabled: bool
    error: Optional[str] = None


class UpdateSettingsPayload(ApiModel):
    enabled: bool


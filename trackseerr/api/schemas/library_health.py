"""Response models for ``/api/library-health`` (``api/routes/library_health.py``). Admin only (paths reveal host layout)."""

from typing import Any, Optional

from trackseerr.api.response_models import ApiModel


class LibraryHealthFinding(ApiModel):
    id: str
    kind: str
    cause: str
    group_key: str
    path: str
    # Free-form: the detector-specific payload (torrent hash, download id, matched track...) stored as JSON.
    detail: Optional[Any] = None
    first_seen: str
    last_seen: str


class LibraryHealthGroup(ApiModel):
    group_key: str
    kind: str
    cause: str
    count: int
    sample_path: str
    suggestion: str


class LibraryHealthRun(ApiModel):
    id: int
    started_at: str
    finished_at: Optional[str] = None
    server_kind: Optional[str] = None
    disk_files: int = 0
    server_files: int = 0
    unindexed: int = 0
    stale: int = 0
    error: Optional[str] = None


class LibraryHealthMapping(ApiModel):
    server_prefix: str
    local_prefix: str
    auto: bool


class LibraryHealthServer(ApiModel):
    kind: str
    file_paths: bool


class LibraryHealthResponse(ApiModel):
    findings: list[LibraryHealthFinding]
    groups: list[LibraryHealthGroup]
    last_run: Optional[LibraryHealthRun] = None
    running: bool
    mapping: Optional[LibraryHealthMapping] = None
    server: Optional[LibraryHealthServer] = None
    count: int
    weekly: bool


class LibraryHealthCount(ApiModel):
    count: int


class LibraryHealthStarted(ApiModel):
    started: bool


class LibraryHealthDismissed(ApiModel):
    dismissed: bool
    removed: int


class LibraryHealthMappingResponse(ApiModel):
    mapping: Optional[LibraryHealthMapping] = None


class LibraryHealthWeekly(ApiModel):
    weekly: bool

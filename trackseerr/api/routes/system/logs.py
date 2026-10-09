"""System logs, events, SSE streaming, and rotation API routes."""


import asyncio
import collections
from datetime import (
    datetime,
)
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import (
    Any,
    Optional,
)
import uuid
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from trackseerr.api.response_models import ApiModel
from trackseerr.api.schemas.system import (
    LogEntry,
    SuccessFlag,
    SystemEventsPage,
)
from trackseerr.api.dependencies import (
    get_config,
    authenticate_request,
    get_db,
    require_admin,
)
from trackseerr.log_rotation import (
    CURRENT_LOG_NAME,
    LEVEL_CHOICES,
    MAX_TOTAL_MB_PRESETS,
    RETENTION_DAYS_RANGE,
    ROTATION_HOURS_PRESETS,
    LogSettingsError,
    apply_log_settings,
    list_log_files,
    load_log_settings,
    resolve_log_file,
    save_log_settings,
    validate_log_settings,
)
from trackseerr.redaction import (
    RedactLogFilter,
    safe_exc,
)
from trackseerr.config import (
    Config,
)
from trackseerr.storage import Database



logger = logging.getLogger(__name__)


router = APIRouter()


class LogRingBuffer(logging.Handler):
    """Thread-safe circular in-memory log buffer supporting SSE broadcasting."""

    follows_log_level = True

    def __init__(self, maxlen: int = 1000) -> None:
        super().__init__()
        self.addFilter(RedactLogFilter())  # nothing unredacted ever reaches the buffer or the SSE stream
        self._lock = threading.Lock()
        self.buffer: collections.deque[dict[str, Any]] = collections.deque(maxlen=maxlen)
        self.listeners: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            entry = {
                "id": str(uuid.uuid4()),
                "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "name": record.name,
                "message": msg,
            }
            with self._lock:
                self.buffer.append(entry)
                listeners = list(self.listeners)

            for loop, q in listeners:
                try:
                    if loop.is_running():
                        loop.call_soon_threadsafe(self._safe_put, q, entry)
                except Exception:
                    pass
        except Exception:
            self.handleError(record)

    @staticmethod
    def _safe_put(q: asyncio.Queue, item: dict[str, Any]) -> None:
        try:
            q.put_nowait(item)
        except (asyncio.QueueFull, Exception):
            pass

    def add_listener(self, loop: asyncio.AbstractEventLoop, q: asyncio.Queue) -> None:
        with self._lock:
            self.listeners.append((loop, q))

    def remove_listener(self, q: asyncio.Queue) -> None:
        with self._lock:
            self.listeners = [item for item in self.listeners if item[1] is not q]

    def get_logs(
        self,
        level: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self.buffer)

        if level and level.lower() != "all":
            lvl = level.strip().upper()
            items = [i for i in items if i["level"].upper() == lvl]

        if search:
            s = search.strip().lower()
            items = [
                i for i in items
                if s in i["message"].lower() or s in i["name"].lower()
            ]

        return items[-limit:]

    def clear(self) -> None:
        with self._lock:
            self.buffer.clear()


log_ring_buffer = LogRingBuffer(maxlen=1000)


# Attach log_ring_buffer to root logger so all events are captured
_root_logger = logging.getLogger()


if log_ring_buffer not in _root_logger.handlers:
    _root_logger.addHandler(log_ring_buffer)


def get_log_file_path(config: Optional[Config] = None, log_dir: Optional[str] = None) -> Path:
    """Resolves the active ``trackseerr.txt`` log file destination path."""
    if log_dir:
        target_dir = Path(log_dir)
    elif config and getattr(config, "config_dir", None) and os.path.exists(config.config_dir):
        target_dir = Path(config.config_dir)
    elif config and getattr(config, "data_dir", None) and os.path.exists(config.data_dir):
        target_dir = Path(config.data_dir)
    elif os.path.exists("/config"):
        target_dir = Path("/config")
    elif os.path.exists("/data"):
        target_dir = Path("/data")
    else:
        target_dir = Path(os.environ.get("DATA_DIR", "/tmp"))

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        target_dir = Path("/tmp")

    return target_dir / CURRENT_LOG_NAME


@router.get("/events", response_model=SystemEventsPage, response_model_exclude_unset=True, summary="List system lifecycle events")
def get_system_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    event_type: Optional[str] = None,
    severity: Optional[str] = None,
    search: Optional[str] = None,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Returns paginated and filtered system lifecycle events."""
    limit = page_size
    offset = (page - 1) * limit
    items, total = db.list_events(
        limit=limit,
        offset=offset,
        event_type=event_type,
        severity=severity,
        search=search,
    )
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.delete("/events", response_model=SuccessFlag, response_model_exclude_unset=True, summary="Clear system lifecycle events")
def clear_system_events(
    db: Database = Depends(get_db),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Clears all system lifecycle events from database (admin required)."""
    db.clear_events()
    return {"success": True}


@router.get("/logs", response_model=list[LogEntry], response_model_exclude_unset=True, summary="Get recent in-memory system logs")
def get_system_logs(
    level: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(default=200, ge=1, le=1000),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Returns filtered entries from the circular in-memory log buffer."""
    return log_ring_buffer.get_logs(level=level, search=search, limit=limit)


@router.get("/logs/stream", summary="Live SSE stream of system logs")
async def stream_system_logs(
    request: Request,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
):
    """Server-Sent Events endpoint streaming real-time log records."""
    # Same session resolution as get_current_user (session row, disabled/tombstoned, sessions_revoked_at, gateway
    # status). Same-origin EventSource sends the HttpOnly session cookie, so no token ever appears in the URL.
    user = authenticate_request(request, db, config)
    require_admin(user)

    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    log_ring_buffer.add_listener(loop, q)

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'connected', 'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(entry)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            log_ring_buffer.remove_listener(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/logs", response_model=SuccessFlag, response_model_exclude_unset=True, summary="Clear in-memory log buffer")
def clear_system_logs(
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Clears all buffered log entries from memory (admin required)."""
    log_ring_buffer.clear()
    return {"success": True}


@router.get("/logs/download", summary="Download the current disk log file")
def download_system_logs(
    config: Config = Depends(get_config),
    admin: dict[str, Any] = Depends(require_admin),
):
    """Returns the active ``trackseerr.txt`` for download (admin required). Kept for compatibility; the
    log-file listing endpoints serve rotated files."""
    log_path = get_log_file_path(config)
    if not log_path.exists():
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            logs = log_ring_buffer.get_logs(limit=1000)
            with open(log_path, "w", encoding="utf-8") as f:
                for l in logs:
                    f.write(f"{l.get('timestamp')} [{l.get('level')}] {l.get('name')}: {l.get('message')}\n")
        except OSError as ex:
            logger.warning("Could not generate disk log file: %s", safe_exc(ex))
            raise HTTPException(status_code=404, detail="Log file not found")

    return FileResponse(
        path=str(log_path),
        filename=CURRENT_LOG_NAME,
        media_type="text/plain",
    )


class LogFileEntry(ApiModel):
    name: str
    size_bytes: int
    start_at: Optional[str] = None
    end_at: Optional[str] = None  # null for the file currently being written


class LogSettingsResponse(ApiModel):
    log_rotation_hours: int
    log_retention_days: int
    log_max_total_mb: int
    log_level: str
    rotation_hours_options: list[int]
    retention_days_min: int
    retention_days_max: int
    max_total_mb_options: list[int]
    level_options: list[str]


class LogSettingsUpdate(BaseModel):
    """Body of ``PUT /logs/settings``. Every field is optional; only the ones sent are changed."""

    model_config = ConfigDict(extra="forbid")
    log_rotation_hours: Optional[int] = None
    log_retention_days: Optional[int] = None
    log_max_total_mb: Optional[int] = None
    log_level: Optional[str] = None


def _log_settings_response(db: Database) -> LogSettingsResponse:
    saved = load_log_settings(db)
    return LogSettingsResponse(
        log_rotation_hours=saved.log_rotation_hours,
        log_retention_days=saved.log_retention_days,
        log_max_total_mb=saved.log_max_total_mb,
        log_level=saved.log_level,
        rotation_hours_options=list(ROTATION_HOURS_PRESETS),
        retention_days_min=RETENTION_DAYS_RANGE[0],
        retention_days_max=RETENTION_DAYS_RANGE[1],
        max_total_mb_options=list(MAX_TOTAL_MB_PRESETS),
        level_options=list(LEVEL_CHOICES),
    )


@router.get("/logs/settings", response_model=LogSettingsResponse, summary="Get log rotation, retention and level settings")
def get_log_settings(
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> LogSettingsResponse:
    try:
        return _log_settings_response(db)
    except sqlite3.Error as exc:
        logger.error("Could not read the log settings: %s", safe_exc(exc))
        raise HTTPException(status_code=500, detail="Could not read the log settings")


@router.put("/logs/settings", response_model=LogSettingsResponse, summary="Update log settings (applied live)")
def update_log_settings(
    body: LogSettingsUpdate,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> LogSettingsResponse:
    """Validates, persists and immediately applies the changed settings (admin required)."""
    try:
        changes = validate_log_settings(**body.model_dump())
    except LogSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        save_log_settings(db, changes)
        response = _log_settings_response(db)
    except sqlite3.Error as exc:
        logger.error("Could not store the log settings: %s", safe_exc(exc))
        raise HTTPException(status_code=500, detail="Could not store the log settings")
    apply_log_settings(load_log_settings(db))
    logger.info("Log settings changed: %s", ", ".join(f"{k}={v}" for k, v in sorted(changes.items())) or "none")
    return response


@router.get("/logs/files", response_model=list[LogFileEntry], summary="List log files, newest first")
def list_system_log_files(
    config: Config = Depends(get_config),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[LogFileEntry]:
    log_dir = get_log_file_path(config).parent
    return [
        LogFileEntry(name=i.name, size_bytes=i.size_bytes, start_at=i.start_at, end_at=i.end_at)
        for i in list_log_files(log_dir)
    ]


@router.get("/logs/files/{name}", summary="Download one log file")
def download_system_log_file(
    name: str,
    config: Config = Depends(get_config),
    _admin: dict[str, Any] = Depends(require_admin),
):
    """The name must be one of the listed log files; anything else (traversal, absolute paths) is a 404."""
    path = resolve_log_file(get_log_file_path(config).parent, name)
    if path is None:
        raise HTTPException(status_code=404, detail="Log file not found")
    return FileResponse(path=str(path), filename=path.name, media_type="text/plain")

"""System diagnostics and telemetry API routes for TrackSeerr."""

import asyncio
import collections
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import threading
import time
from typing import Any, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from plex_playlist_sync.acquisition_worker import acquisition_worker
from plex_playlist_sync.artist_refresh_worker import artist_refresh_worker
from plex_playlist_sync.api.dependencies import (
    get_config,
    authenticate_request,
    get_current_user,
    get_db,
    get_deezer_client,
    get_lidarr_client,
    get_plex_client,
    get_spotify_client,
    require_admin,
)
from plex_playlist_sync.api.routes.sync import sync_state
from plex_playlist_sync.auth import get_or_create_secret_key, verify_session_token
from plex_playlist_sync.backlog_worker import backlog_worker, rss_worker
from plex_playlist_sync.clients.acquisition import (
    get_acquisition_driver,
    get_indexer_driver,
)
from plex_playlist_sync.clients.deezer import DeezerClient
from plex_playlist_sync.clients.lidarr import LidarrClient
from plex_playlist_sync.clients.plex import PlexClient
from plex_playlist_sync.clients.spotify import SpotifyClient
from plex_playlist_sync.config import Config
from plex_playlist_sync.library_scanner import library_scanner
from plex_playlist_sync.lidarr_queue import lidarr_worker
from plex_playlist_sync.models import DownloadClientConfig, IndexerConfig, UserPermission
from plex_playlist_sync.security import is_safe_service_url
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

# Track application start timestamp at module import
_APP_START_TIME = time.time()

router = APIRouter()


class LogRingBuffer(logging.Handler):
    """Thread-safe circular in-memory log buffer supporting SSE broadcasting."""

    def __init__(self, maxlen: int = 1000) -> None:
        super().__init__()
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
    """Resolves the active trackseerr.log file destination path."""
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

    return target_dir / "trackseerr.log"


class DiskUsageItem(BaseModel):
    path: str
    label: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent_used: float


class DatabaseStatus(BaseModel):
    path: str
    size_bytes: int
    sqlite_version: str
    table_counts: dict[str, int]


class ServicePingResult(BaseModel):
    id: str
    name: str
    service_type: str
    host_url: str
    enabled: bool
    online: bool
    latency_ms: Optional[float] = None
    message: str


class PlexStatus(BaseModel):
    configured: bool
    url: Optional[str] = None
    music_section: Optional[str] = None
    online: bool
    latency_ms: Optional[float] = None
    user_count: int = 0
    playlist_count: int = 0
    message: str


class WorkerStatus(BaseModel):
    acquisition_worker: dict[str, Any]
    lidarr_worker: dict[str, Any]
    sync_coordinator: dict[str, Any]
    backlog_worker: Optional[dict[str, Any]] = None
    rss_worker: Optional[dict[str, Any]] = None


class EnvironmentStatus(BaseModel):
    version: str = "1.0.0"
    python_version: str
    platform: str
    role: str
    uptime_seconds: float


class SystemStatusResponse(BaseModel):
    environment: EnvironmentStatus
    storage: list[DiskUsageItem]
    database: DatabaseStatus
    plex: PlexStatus
    download_clients: list[ServicePingResult]
    indexers: list[ServicePingResult]
    workers: WorkerStatus


def _get_disk_metrics(config: Config, db: Database) -> list[DiskUsageItem]:
    """Collects disk usage metrics for key system mount paths."""
    candidates: list[tuple[str, str]] = [
        ("/", "System Root (/)"),
        (config.data_dir or "/data", f"Data Directory ({config.data_dir or '/data'})"),
    ]

    if os.path.exists("/config"):
        candidates.append(("/config", "Config Directory (/config)"))

    try:
        mm = db.get_media_management_settings()
        root_folder = mm.get("root_folder_path")
        if root_folder:
            candidates.append((str(root_folder), "Music Library Storage"))
        staging_folder = mm.get("staging_folder_path")
        if staging_folder:
            candidates.append((str(staging_folder), "Download Staging Storage"))
    except Exception as e:
        logger.warning("Failed to retrieve media management paths for disk usage: %s", e)

    results: list[DiskUsageItem] = []
    seen_usages: set[tuple[int, int]] = set()

    for path, label in candidates:
        if not path:
            continue
        try:
            usage = shutil.disk_usage(path)
            usage_key = (usage.total, usage.used)
            if usage_key in seen_usages:
                continue
            seen_usages.add(usage_key)

            total_bytes = int(usage.total)
            used_bytes = int(usage.used)
            free_bytes = int(usage.free)
            percent_used = (
                round((used_bytes / total_bytes) * 100.0, 1)
                if total_bytes > 0
                else 0.0
            )

            results.append(
                DiskUsageItem(
                    path=path,
                    label=label,
                    total_bytes=total_bytes,
                    used_bytes=used_bytes,
                    free_bytes=free_bytes,
                    percent_used=percent_used,
                )
            )
        except (OSError, ValueError, TypeError) as e:
            logger.debug("Disk usage probe skipped for path %s: %s", path, e)

    return results


def _get_db_metrics(db: Database) -> DatabaseStatus:
    """Collects database size, sqlite engine version, and table row counts."""
    db_path_str = str(db.db_path)
    size_bytes = 0
    if db_path_str != ":memory:" and os.path.exists(db_path_str):
        try:
            size_bytes = os.path.getsize(db_path_str)
        except OSError as e:
            logger.warning("Could not read database file size at %s: %s", db_path_str, e)
            size_bytes = 0

    table_counts: dict[str, int] = {}
    try:
        table_counts["users"] = len(db.list_users())
    except Exception as e:
        logger.warning("Failed to count users: %s", e)
        table_counts["users"] = 0

    try:
        table_counts["playlists"] = len(db.list_playlists())
    except Exception as e:
        logger.warning("Failed to count playlists: %s", e)
        table_counts["playlists"] = 0

    try:
        table_counts["requests"] = len(db.list_requests())
    except Exception as e:
        logger.warning("Failed to count requests: %s", e)
        table_counts["requests"] = 0

    try:
        table_counts["missing_tracks"] = len(db.get_missing_tracks())
    except Exception as e:
        logger.warning("Failed to count missing_tracks: %s", e)
        table_counts["missing_tracks"] = 0

    try:
        table_counts["download_clients"] = len(db.list_download_clients())
    except Exception as e:
        logger.warning("Failed to count download_clients: %s", e)
        table_counts["download_clients"] = 0

    try:
        table_counts["indexers"] = len(db.list_indexers())
    except Exception as e:
        logger.warning("Failed to count indexers: %s", e)
        table_counts["indexers"] = 0

    try:
        table_counts["active_downloads"] = len(db.list_active_downloads())
    except Exception as e:
        logger.warning("Failed to count active_downloads: %s", e)
        table_counts["active_downloads"] = 0

    try:
        table_counts["system_events"] = db.list_events(limit=1)[1]
    except Exception as e:
        logger.warning("Failed to count system_events: %s", e)
        table_counts["system_events"] = 0

    return DatabaseStatus(
        path=db_path_str,
        size_bytes=size_bytes,
        sqlite_version=sqlite3.sqlite_version,
        table_counts=table_counts,
    )


def _ping_download_clients(db: Database) -> list[ServicePingResult]:
    """Pings all configured download clients and measures response latency."""
    results: list[ServicePingResult] = []
    try:
        clients = db.list_download_clients()
    except Exception as e:
        logger.warning("Failed to list download clients: %s", e)
        return results

    for client in clients:
        c_id = str(client.get("id", ""))
        c_name = str(client.get("name", "Unknown Client"))
        driver_type = str(client.get("driver_type", "unknown"))
        host_url = str(client.get("host_url", ""))
        enabled = bool(client.get("enabled", True))

        if not enabled:
            results.append(
                ServicePingResult(
                    id=c_id,
                    name=c_name,
                    service_type=driver_type,
                    host_url=host_url,
                    enabled=False,
                    online=False,
                    latency_ms=None,
                    message="Disabled in settings",
                )
            )
            continue

        if not is_safe_service_url(host_url):
            results.append(
                ServicePingResult(
                    id=c_id,
                    name=c_name,
                    service_type=driver_type,
                    host_url=host_url,
                    enabled=True,
                    online=False,
                    latency_ms=None,
                    message="Prohibited or invalid host URL (SSRF defense)",
                )
            )
            continue

        t0 = time.perf_counter()
        try:
            cfg = DownloadClientConfig(
                id=c_id,
                name=c_name,
                driver_type=driver_type,
                host_url=host_url,
                api_key=client.get("api_key"),
                username=client.get("username"),
                password=client.get("password"),
                enabled=enabled,
                priority=int(client.get("priority", 1)),
                extra_settings_json=client.get("extra_settings_json"),
                created_at=client.get("created_at"),
                updated_at=client.get("updated_at"),
            )
            driver = get_acquisition_driver(cfg)
            success, msg = driver.test_connection()
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            results.append(
                ServicePingResult(
                    id=c_id,
                    name=c_name,
                    service_type=driver_type,
                    host_url=host_url,
                    enabled=True,
                    online=bool(success),
                    latency_ms=latency_ms if success else None,
                    message=str(msg),
                )
            )
        except Exception as e:
            logger.warning("Error testing connection to download client %s: %s", c_name, e)
            results.append(
                ServicePingResult(
                    id=c_id,
                    name=c_name,
                    service_type=driver_type,
                    host_url=host_url,
                    enabled=True,
                    online=False,
                    latency_ms=None,
                    message=str(e),
                )
            )

    return results


def _ping_indexers(db: Database) -> list[ServicePingResult]:
    """Pings all configured indexers and measures response latency."""
    results: list[ServicePingResult] = []
    try:
        indexers = db.list_indexers()
    except Exception as e:
        logger.warning("Failed to list indexers: %s", e)
        return results

    for idx in indexers:
        i_id = str(idx.get("id", ""))
        i_name = str(idx.get("name", "Unknown Indexer"))
        indexer_type = str(idx.get("indexer_type", "torznab"))
        host_url = str(idx.get("host_url", ""))
        enabled = bool(idx.get("enabled", True))

        if not enabled:
            results.append(
                ServicePingResult(
                    id=i_id,
                    name=i_name,
                    service_type=indexer_type,
                    host_url=host_url,
                    enabled=False,
                    online=False,
                    latency_ms=None,
                    message="Disabled in settings",
                )
            )
            continue

        if not is_safe_service_url(host_url):
            results.append(
                ServicePingResult(
                    id=i_id,
                    name=i_name,
                    service_type=indexer_type,
                    host_url=host_url,
                    enabled=True,
                    online=False,
                    latency_ms=None,
                    message="Prohibited or invalid host URL (SSRF defense)",
                )
            )
            continue

        t0 = time.perf_counter()
        try:
            cfg = IndexerConfig(
                id=i_id,
                name=i_name,
                indexer_type=indexer_type,
                host_url=host_url,
                api_key=idx.get("api_key"),
                categories=str(idx.get("categories") or "3000,3010,3020,3030,3040"),
                enabled=enabled,
                priority=int(idx.get("priority", 1)),
                created_at=idx.get("created_at"),
                updated_at=idx.get("updated_at"),
            )
            driver = get_indexer_driver(cfg)
            success, msg = driver.test_connection()
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            results.append(
                ServicePingResult(
                    id=i_id,
                    name=i_name,
                    service_type=indexer_type,
                    host_url=host_url,
                    enabled=True,
                    online=bool(success),
                    latency_ms=latency_ms if success else None,
                    message=str(msg),
                )
            )
        except Exception as e:
            logger.warning("Error testing connection to indexer %s: %s", i_name, e)
            results.append(
                ServicePingResult(
                    id=i_id,
                    name=i_name,
                    service_type=indexer_type,
                    host_url=host_url,
                    enabled=True,
                    online=False,
                    latency_ms=None,
                    message=str(e),
                )
            )

    return results


def _ping_plex(
    plex_client: Optional[PlexClient],
    config: Config,
    db: Database,
) -> PlexStatus:
    sec = getattr(config, "plex_music_section", None)
    if not isinstance(sec, str):
        client_sec = getattr(plex_client, "music_section", None) if plex_client else None
        sec = client_sec if isinstance(client_sec, str) else "Music"
    music_sec: Optional[str] = sec if isinstance(sec, str) else "Music"

    if plex_client is None:
        return PlexStatus(
            configured=False,
            url=config.plex_url,
            music_section=music_sec,
            online=False,
            latency_ms=None,
            user_count=0,
            playlist_count=0,
            message="Plex not configured",
        )

    t0 = time.perf_counter()
    try:
        if hasattr(plex_client, "test_connection"):
            res = plex_client.test_connection()
            if isinstance(res, tuple):
                online = bool(res[0])
                msg = str(res[1])
            elif isinstance(res, bool):
                online = res
                msg = "Connected to Plex" if online else "Plex unreachable"
            elif isinstance(res, dict):
                online = bool(res.get("online", False))
                msg = str(
                    res.get("message")
                    or res.get("error")
                    or ("Connected to Plex" if online else "Plex unreachable")
                )
            else:
                online = bool(res)
                msg = "Connected to Plex" if online else "Plex unreachable"
        else:
            online = True
            msg = "Connected to Plex"
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2) if online else None
    except Exception as e:
        logger.warning("Error testing connection to Plex: %s", e)
        online = False
        latency_ms = None
        msg = str(e)

    try:
        user_count = len(db.list_users())
    except Exception:
        user_count = 0

    try:
        playlist_count = len(db.list_playlists())
    except Exception:
        playlist_count = 0

    return PlexStatus(
        configured=True,
        url=config.plex_url,
        music_section=music_sec,
        online=online,
        latency_ms=latency_ms,
        user_count=user_count,
        playlist_count=playlist_count,
        message=msg,
    )


def _get_worker_statuses() -> WorkerStatus:
    """Collects live heartbeat metrics from background workers."""
    # 1. Acquisition worker status
    try:
        from plex_playlist_sync.acquisition_worker import acquisition_worker

        acq_status = {"running": acquisition_worker.is_running()}
    except Exception as e:
        logger.warning("Failed to query acquisition worker: %s", e)
        acq_status = {"running": False, "error": str(e)}

    # 2. Lidarr trickle worker status
    try:
        from plex_playlist_sync.lidarr_queue import lidarr_worker

        lidarr_status = lidarr_worker.get_status()
    except Exception as e:
        logger.warning("Failed to query lidarr worker: %s", e)
        lidarr_status = {"running": False, "error": str(e)}

    # 3. Sync coordinator status
    try:
        from plex_playlist_sync.api.routes.sync import sync_state

        sync_status = {
            "is_syncing": sync_state.is_syncing,
            "last_run_at": sync_state.last_run_at,
            "last_run_stats": sync_state.last_run_stats,
        }
    except Exception as e:
        logger.warning("Failed to query sync state: %s", e)
        sync_status = {"is_syncing": False, "error": str(e)}

    # 4. Wanted backlog worker status
    try:
        from plex_playlist_sync.backlog_worker import backlog_worker

        backlog_status = backlog_worker.get_status()
    except Exception as e:
        logger.warning("Failed to query backlog worker: %s", e)
        backlog_status = {"running": False, "error": str(e)}

    # 5. RSS sync worker status
    try:
        from plex_playlist_sync.backlog_worker import rss_worker

        rss_status = rss_worker.get_status()
    except Exception as e:
        logger.warning("Failed to query rss worker: %s", e)
        rss_status = {"running": False, "error": str(e)}

    return WorkerStatus(
        acquisition_worker=acq_status,
        lidarr_worker=lidarr_status,
        sync_coordinator=sync_status,
        backlog_worker=backlog_status,
        rss_worker=rss_status,
    )


@router.get("/status", response_model=SystemStatusResponse, summary="Get system status and diagnostics")
@router.get("", response_model=SystemStatusResponse, include_in_schema=False)
def get_system_status(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[PlexClient] = Depends(get_plex_client),
    admin: dict[str, Any] = Depends(require_admin),
) -> SystemStatusResponse:
    """Returns comprehensive system telemetry, storage, service connectivity, and worker heartbeats."""
    env_status = EnvironmentStatus(
        version="1.0.0",
        python_version=platform.python_version(),
        platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
        role=os.getenv("ROLE", "all-in-one").lower().strip(),
        uptime_seconds=round(time.time() - _APP_START_TIME, 1),
    )

    storage = _get_disk_metrics(config, db)
    database = _get_db_metrics(db)
    plex = _ping_plex(plex_client, config, db)
    download_clients = _ping_download_clients(db)
    indexers = _ping_indexers(db)
    workers = _get_worker_statuses()

    return SystemStatusResponse(
        environment=env_status,
        storage=storage,
        database=database,
        plex=plex,
        download_clients=download_clients,
        indexers=indexers,
        workers=workers,
    )


# -----------------------------------------------------------------------------
# System Events & Logging Routes
# -----------------------------------------------------------------------------

@router.get("/events", summary="List system lifecycle events")
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


@router.delete("/events", summary="Clear system lifecycle events")
def clear_system_events(
    db: Database = Depends(get_db),
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Clears all system lifecycle events from database (admin required)."""
    db.clear_events()
    return {"success": True}


@router.get("/logs", summary="Get recent in-memory system logs")
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
    token: Optional[str] = None,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
):
    """Server-Sent Events endpoint streaming real-time log records."""
    # Same session resolution as get_current_user (session row, disabled/tombstoned,
    # sessions_revoked_at, gateway status); ?token= only because EventSource cannot set headers.
    user = authenticate_request(request, db, config, query_token=token)
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


@router.delete("/logs", summary="Clear in-memory log buffer")
def clear_system_logs(
    admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Clears all buffered log entries from memory (admin required)."""
    log_ring_buffer.clear()
    return {"success": True}


@router.get("/logs/download", summary="Download rotated disk log file")
def download_system_logs(
    config: Config = Depends(get_config),
    admin: dict[str, Any] = Depends(require_admin),
):
    """Returns active trackseerr.log file for download (admin required)."""
    log_path = get_log_file_path(config)
    if not log_path.exists():
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            logs = log_ring_buffer.get_logs(limit=1000)
            with open(log_path, "w", encoding="utf-8") as f:
                for l in logs:
                    f.write(f"{l.get('timestamp')} [{l.get('level')}] {l.get('name')}: {l.get('message')}\n")
        except Exception as ex:
            logger.warning("Could not generate disk log file: %s", ex)
            raise HTTPException(status_code=404, detail="Log file not found")

    return FileResponse(
        path=str(log_path),
        filename="trackseerr.log",
        media_type="text/plain",
    )


# -----------------------------------------------------------------------------
# Scheduled Tasks Registry & Execution Routes
# -----------------------------------------------------------------------------

class ScheduledTaskItem(BaseModel):
    id: str
    name: str
    description: str
    interval: str
    status: str  # "idle" | "running" | "paused" | "failed"
    last_run_at: Optional[str] = None
    can_trigger: bool = True
    can_cancel: bool = False


VALID_TASK_IDS = {
    "filesystem_scan",
    "playlist_sync",
    "wanted_backlog_sweep",
    "indexer_rss_sync",
    "lidarr_auto_trickle",
    "download_queue_monitor",
    "artist_metadata_refresh",
}

_running_tasks: set[str] = set()
_task_last_run_at: dict[str, str] = {}
_tasks_lock = threading.Lock()


def get_all_scheduled_tasks(
    db: Database,
    config: Optional[Config] = None,
) -> list[ScheduledTaskItem]:
    """Returns the unified registry of scheduled background tasks and worker heartbeats."""
    tasks: list[ScheduledTaskItem] = []

    # 1. filesystem_scan: Media Library Disk Scanner (library_scanner)
    scan_stat = library_scanner.get_status()
    if scan_stat.get("is_scanning") or scan_stat.get("status") == "scanning" or "filesystem_scan" in _running_tasks:
        scan_status_val = "running"
    elif scan_stat.get("status") == "failed":
        scan_status_val = "failed"
    else:
        scan_status_val = "idle"

    scan_last_run = scan_stat.get("completed_at") or scan_stat.get("started_at") or _task_last_run_at.get("filesystem_scan")
    tasks.append(
        ScheduledTaskItem(
            id="filesystem_scan",
            name="Media Library Disk Scanner",
            description="Scans local audio storage, extracts Mutagen tags, and indexes media into the library catalog.",
            interval="Manual / On Demand",
            status=scan_status_val,
            last_run_at=scan_last_run,
            can_trigger=True,
            can_cancel=True,
        )
    )

    # 2. playlist_sync: Plex Playlist Sync (sync_state)
    wait_sec = getattr(config, "wait_seconds", 0) if config else 0
    if wait_sec >= 60:
        sync_interval = f"Every {int(wait_sec // 60)}m"
    elif wait_sec > 0:
        sync_interval = f"Every {int(wait_sec)}s"
    else:
        sync_interval = "Manual / On Demand"

    sync_status_val = "running" if (sync_state.is_syncing or "playlist_sync" in _running_tasks) else "idle"
    sync_last_run = sync_state.last_run_at or _task_last_run_at.get("playlist_sync")
    tasks.append(
        ScheduledTaskItem(
            id="playlist_sync",
            name="Plex Playlist Sync",
            description="Synchronizes enabled Spotify and Deezer playlists with Plex media server users.",
            interval=sync_interval,
            status=sync_status_val,
            last_run_at=sync_last_run,
            can_trigger=True,
            can_cancel=False,
        )
    )

    # 3. wanted_backlog_sweep: Monitored Missing & Upgrade Search Sweep (backlog_worker)
    backlog_stat = backlog_worker.get_status()
    backlog_interval_min = getattr(config, "backlog_search_interval_minutes", 60) if config else 60
    backlog_status_val = "running" if "wanted_backlog_sweep" in _running_tasks else "idle"
    backlog_last_run = backlog_stat.get("last_run_at") or _task_last_run_at.get("wanted_backlog_sweep")
    tasks.append(
        ScheduledTaskItem(
            id="wanted_backlog_sweep",
            name="Monitored Missing & Upgrade Search Sweep",
            description="Sweeps unfulfilled requests and missing library tracks against indexers for new releases or quality upgrades.",
            interval=f"Every {backlog_interval_min or 60}m",
            status=backlog_status_val,
            last_run_at=backlog_last_run,
            can_trigger=True,
            can_cancel=False,
        )
    )

    # 4. indexer_rss_sync: Torznab / Newznab RSS Sync (rss_worker)
    rss_stat = rss_worker.get_status()
    rss_interval_min = getattr(config, "rss_sync_interval_minutes", 15) if config else 15
    rss_status_val = "running" if "indexer_rss_sync" in _running_tasks else "idle"
    rss_last_run = rss_stat.get("last_run_at") or _task_last_run_at.get("indexer_rss_sync")
    tasks.append(
        ScheduledTaskItem(
            id="indexer_rss_sync",
            name="Torznab / Newznab RSS Sync",
            description="Monitors indexer recent releases for incoming tracks and albums matching pending requests.",
            interval=f"Every {rss_interval_min or 15}m",
            status=rss_status_val,
            last_run_at=rss_last_run,
            can_trigger=True,
            can_cancel=False,
        )
    )

    # 5. lidarr_auto_trickle: Lidarr Trickle Worker (lidarr_worker)
    lidarr_stat = lidarr_worker.get_status()
    lidarr_interval_min = getattr(config, "lidarr_auto_trickle_interval_minutes", 30) if config else 30
    if lidarr_stat.get("is_paused"):
        lidarr_status_val = "paused"
    elif lidarr_stat.get("is_running") or "lidarr_auto_trickle" in _running_tasks:
        lidarr_status_val = "running"
    else:
        lidarr_status_val = "idle"

    lidarr_last_run = lidarr_stat.get("last_processed_at") or lidarr_stat.get("started_at") or _task_last_run_at.get("lidarr_auto_trickle")
    tasks.append(
        ScheduledTaskItem(
            id="lidarr_auto_trickle",
            name="Lidarr Trickle Worker",
            description="Trickles missing tracks into Lidarr with paced delays to prevent MusicBrainz rate limits.",
            interval=f"Every {lidarr_interval_min or 30}m",
            status=lidarr_status_val,
            last_run_at=lidarr_last_run,
            can_trigger=True,
            can_cancel=True,
        )
    )

    # 6. download_queue_monitor: Acquisition Worker (acquisition_worker)
    acq_poll_sec = int(getattr(acquisition_worker, "poll_interval", 5.0))
    acq_status_val = "running" if ("download_queue_monitor" in _running_tasks or (hasattr(acquisition_worker, "is_running") and acquisition_worker.is_running())) else "idle"
    acq_last_run = _task_last_run_at.get("download_queue_monitor")
    tasks.append(
        ScheduledTaskItem(
            id="download_queue_monitor",
            name="Acquisition Worker",
            description="Monitors active download clients, processes finished downloads, tags audio files, and moves them to the library.",
            interval=f"Every {acq_poll_sec}s",
            status=acq_status_val,
            last_run_at=acq_last_run,
            can_trigger=True,
            can_cancel=False,
        )
    )

    # 7. artist_metadata_refresh: Artist Metadata & Discography Refresh (artist_refresh_worker)
    ar_stat = artist_refresh_worker.get_status()
    ar_status_val = "running" if (ar_stat.get("running") or "artist_metadata_refresh" in _running_tasks) else "idle"
    ar_last_run = ar_stat.get("last_run_at") or _task_last_run_at.get("artist_metadata_refresh")
    tasks.append(
        ScheduledTaskItem(
            id="artist_metadata_refresh",
            name="Artist Metadata & Discography Refresh",
            description="Refreshes artist metadata, canonical discographies, full tracklists, and artwork cache from BrainzMash / MusicBrainz and Deezer.",
            interval="Every 24h",
            status=ar_status_val,
            last_run_at=ar_last_run,
            can_trigger=True,
            can_cancel=False,
        )
    )

    return tasks


@router.get("/tasks", response_model=list[ScheduledTaskItem], summary="List all scheduled background tasks")
def get_scheduled_tasks(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[ScheduledTaskItem]:
    """Returns registry of background workers, intervals, statuses, and execution metadata."""
    return get_all_scheduled_tasks(db, config)


@router.post("/tasks/{task_id}/run", summary="Trigger a scheduled task manually")
def run_scheduled_task(
    task_id: str,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[PlexClient] = Depends(get_plex_client),
    spotify_client: Optional[SpotifyClient] = Depends(get_spotify_client),
    deezer_client: Optional[DeezerClient] = Depends(get_deezer_client),
    lidarr_client: Optional[LidarrClient] = Depends(get_lidarr_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Dispatches the specified background task asynchronously (admin required)."""
    if task_id not in VALID_TASK_IDS:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    now_iso = datetime.now(timezone.utc).isoformat()
    with _tasks_lock:
        _task_last_run_at[task_id] = now_iso

    try:
        db.record_event(
            "task_triggered",
            f"Scheduled task '{task_id}' triggered by admin",
            source="TaskManager",
            severity="info",
            details={"task_id": task_id},
        )
    except Exception as ev_err:
        logger.warning("Failed to record task_triggered event: %s", ev_err)

    if task_id == "filesystem_scan":
        def _scan_thread():
            with _tasks_lock:
                _running_tasks.add("filesystem_scan")
            try:
                library_scanner.start_scan(db=db, plex_client=plex_client)
            finally:
                with _tasks_lock:
                    _running_tasks.discard("filesystem_scan")

        threading.Thread(target=_scan_thread, daemon=True, name="ManualScanTask").start()

    elif task_id == "playlist_sync":
        def _sync_thread():
            with _tasks_lock:
                _running_tasks.add("playlist_sync")
            try:
                sync_state.execute_sync(
                    db=db,
                    config=config,
                    plex_client=plex_client,
                    spotify_client=spotify_client,
                    deezer_client=deezer_client,
                )
            finally:
                with _tasks_lock:
                    _running_tasks.discard("playlist_sync")

        threading.Thread(target=_sync_thread, daemon=True, name="ManualSyncTask").start()

    elif task_id == "wanted_backlog_sweep":
        def _backlog_thread():
            with _tasks_lock:
                _running_tasks.add("wanted_backlog_sweep")
            try:
                backlog_worker.poll_once(db=db)
            finally:
                with _tasks_lock:
                    _running_tasks.discard("wanted_backlog_sweep")

        threading.Thread(target=_backlog_thread, daemon=True, name="ManualBacklogTask").start()

    elif task_id == "indexer_rss_sync":
        def _rss_thread():
            with _tasks_lock:
                _running_tasks.add("indexer_rss_sync")
            try:
                rss_worker.poll_once(db=db)
            finally:
                with _tasks_lock:
                    _running_tasks.discard("indexer_rss_sync")

        threading.Thread(target=_rss_thread, daemon=True, name="ManualRSSTask").start()

    elif task_id == "lidarr_auto_trickle":
        def _lidarr_thread():
            with _tasks_lock:
                _running_tasks.add("lidarr_auto_trickle")
            try:
                if lidarr_client:
                    all_missing = db.get_missing_tracks()
                    unmonitored = [t for t in all_missing if t.get("lidarr_status") != "monitored"]
                    items_to_push = unmonitored if unmonitored else all_missing
                    if items_to_push:
                        lidarr_settings = db.get_lidarr_settings()
                        batch_size = int(lidarr_settings.get("trickle_batch_size") or config.lidarr_trickle_batch_size or 25)
                        delay_sec = float(lidarr_settings.get("trickle_rate_seconds") or config.lidarr_trickle_rate_seconds or 3.0)
                        auto_srch = bool(lidarr_settings.get("auto_search", config.lidarr_auto_search))
                        lidarr_worker.start_trickle(
                            items=items_to_push,
                            client=lidarr_client,
                            db=db,
                            delay_seconds=delay_sec,
                            auto_search=auto_srch,
                            batch_size=batch_size,
                        )
            finally:
                with _tasks_lock:
                    _running_tasks.discard("lidarr_auto_trickle")

        threading.Thread(target=_lidarr_thread, daemon=True, name="ManualLidarrTask").start()

    elif task_id == "download_queue_monitor":
        def _acq_thread():
            with _tasks_lock:
                _running_tasks.add("download_queue_monitor")
            try:
                acquisition_worker.poll_once(db=db, plex_client=plex_client)
            finally:
                with _tasks_lock:
                    _running_tasks.discard("download_queue_monitor")

        threading.Thread(target=_acq_thread, daemon=True, name="ManualAcquisitionTask").start()

    elif task_id == "artist_metadata_refresh":
        def _refresh_thread():
            with _tasks_lock:
                _running_tasks.add("artist_metadata_refresh")
            try:
                artist_refresh_worker.refresh_once(db=db)
            finally:
                with _tasks_lock:
                    _running_tasks.discard("artist_metadata_refresh")

        threading.Thread(target=_refresh_thread, daemon=True, name="ManualArtistRefreshTask").start()

    return {"success": True, "message": f"Task '{task_id}' dispatched successfully"}


@router.post("/tasks/{task_id}/cancel", summary="Cancel a running scheduled task")
def cancel_scheduled_task(
    task_id: str,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Cancels a running task if supported (admin required)."""
    if task_id not in VALID_TASK_IDS:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    if task_id == "filesystem_scan":
        library_scanner.cancel_scan()
        try:
            db.record_event(
                "task_cancelled",
                "Scheduled task 'filesystem_scan' cancelled by admin",
                source="TaskManager",
                severity="info",
                details={"task_id": "filesystem_scan"},
            )
        except Exception as ev_err:
            logger.warning("Failed to record task_cancelled event: %s", ev_err)
        return {"success": True, "message": "Filesystem scan cancelled"}

    elif task_id == "lidarr_auto_trickle":
        lidarr_worker.cancel()
        try:
            db.record_event(
                "task_cancelled",
                "Scheduled task 'lidarr_auto_trickle' cancelled by admin",
                source="TaskManager",
                severity="info",
                details={"task_id": "lidarr_auto_trickle"},
            )
        except Exception as ev_err:
            logger.warning("Failed to record task_cancelled event: %s", ev_err)
        return {"success": True, "message": "Lidarr trickle worker cancelled"}

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Task '{task_id}' does not support cancellation",
        )

"""System diagnostics and telemetry API routes for TrackSeerr."""

import asyncio
import collections
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import threading
import time
from typing import Any, Callable, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from plex_playlist_sync.api.response_models import ApiModel
from plex_playlist_sync.api.schemas.system import (
    ActivityResponse,
    JobQueueSnapshot,
    LidarrHealthResponse,
    LogEntry,
    MediaServerStatus,
    ResourcesResponse,
    SuccessFlag,
    SystemEventsPage,
    SystemUpdateResponse,
    TaskActionResult,
    TaskProgress,
    TaskRunItem,
    UpdateSettingsPayload,
)

from plex_playlist_sync import art_pipeline, library_health, process_stats, recycle_bin, seed_cleanup
from plex_playlist_sync.acquisition_worker import acquisition_worker
from plex_playlist_sync.update_check import get_update_status, run_update_check
from plex_playlist_sync.artist_refresh_worker import artist_refresh_worker
from plex_playlist_sync.api.dependencies import (
    get_active_media_server,
    get_config,
    authenticate_request,
    get_current_user,
    get_db,
    get_deezer_client,
    get_lidarr_client,
    get_plex_client,
    get_spotify_client,
    require_admin,
    require_core_tier,
)
from plex_playlist_sync.api.routes.sync import sync_state
from plex_playlist_sync.auth import get_or_create_secret_key, verify_session_token
from plex_playlist_sync.log_rotation import (
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
from plex_playlist_sync.redaction import RedactLogFilter, redact_text, safe_exc
from plex_playlist_sync.backlog_worker import backlog_worker, rss_worker
from plex_playlist_sync.clients.acquisition import (
    get_acquisition_driver,
    get_indexer_driver,
)
from plex_playlist_sync.clients.deezer import DeezerClient
from plex_playlist_sync.clients.lidarr import LidarrApiError, LidarrClient
from plex_playlist_sync.clients.plex import PlexClient
from plex_playlist_sync.media_servers import as_media_server, build_jellyfin, build_subsonic
from plex_playlist_sync.media_servers.base import MediaServer
from plex_playlist_sync.clients.spotify import SpotifyClient
from plex_playlist_sync.config import MEDIA_SERVER_JELLYFIN, MEDIA_SERVER_SUBSONIC, Config
from plex_playlist_sync.job_tracker import job_tracker, summarize_result, track_job
from plex_playlist_sync import library_manager
from plex_playlist_sync.library_manager import MODE_LIDARR, MODE_NATIVE, build_lidarr_client, get_library_mode
from plex_playlist_sync.library_scanner import library_scanner
from plex_playlist_sync.media_server import media_server_status
from plex_playlist_sync.lidarr_queue import lidarr_worker
from plex_playlist_sync.mix_worker import mix_worker
from plex_playlist_sync.scrobble_worker import scrobble_worker
from plex_playlist_sync.models import DownloadClientConfig, IndexerConfig, UserPermission
from plex_playlist_sync.security import is_safe_service_url
from plex_playlist_sync.storage import Database
from plex_playlist_sync.task_manager import (
    KIND_INTERVAL,
    TASKS,
    TRIGGER_MANUAL,
    RunHandle,
    default_interval_seconds,
    effective_interval_seconds,
    format_interval,
    next_run_at,
    parse_iso,
    record_task_run,
    schedule_disabled,
    set_interval_override,
)

logger = logging.getLogger(__name__)

# Track application start timestamp at module import
_APP_START_TIME = time.time()

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


class DiskUsageItem(ApiModel):
    path: str
    label: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent_used: float


class DatabaseStatus(ApiModel):
    path: str
    size_bytes: int
    sqlite_version: str
    table_counts: dict[str, int]


class ServicePingResult(ApiModel):
    id: str
    name: str
    service_type: str
    host_url: str
    enabled: bool
    online: bool
    latency_ms: Optional[float] = None
    message: str


class PlexStatus(ApiModel):
    configured: bool
    url: Optional[str] = None
    music_section: Optional[str] = None
    online: bool
    latency_ms: Optional[float] = None
    user_count: int = 0
    playlist_count: int = 0
    message: str


class WorkerStatus(ApiModel):
    acquisition_worker: dict[str, Any]
    lidarr_worker: dict[str, Any]
    sync_coordinator: dict[str, Any]
    backlog_worker: Optional[dict[str, Any]] = None
    rss_worker: Optional[dict[str, Any]] = None


class EnvironmentStatus(ApiModel):
    version: str = "1.0.0"
    python_version: str
    platform: str
    role: str
    uptime_seconds: float


class SystemStatusResponse(ApiModel):
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
            logger.warning("Error testing connection to download client %s: %s", c_name, redact_text(str(e)))
            results.append(
                ServicePingResult(
                    id=c_id,
                    name=c_name,
                    service_type=driver_type,
                    host_url=host_url,
                    enabled=True,
                    online=False,
                    latency_ms=None,
                    message=redact_text(str(e)),
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
            logger.warning("Error testing connection to indexer %s: %s", i_name, redact_text(str(e)))
            results.append(
                ServicePingResult(
                    id=i_id,
                    name=i_name,
                    service_type=indexer_type,
                    host_url=host_url,
                    enabled=True,
                    online=False,
                    latency_ms=None,
                    message=redact_text(str(e)),
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
        result = as_media_server(plex_client).test_connection()
        online = result.ok
        msg = result.message
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2) if online else None
    except Exception as e:
        logger.warning("Error testing connection to Plex: %s", safe_exc(e))
        online = False
        latency_ms = None
        msg = safe_exc(e)

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
        logger.warning("Failed to query acquisition worker: %s", redact_text(str(e)))
        acq_status = {"running": False, "error": redact_text(str(e))}

    # 2. Lidarr trickle worker status
    try:
        from plex_playlist_sync.lidarr_queue import lidarr_worker

        lidarr_status = lidarr_worker.get_status()
    except Exception as e:
        logger.warning("Failed to query lidarr worker: %s", redact_text(str(e)))
        lidarr_status = {"running": False, "error": redact_text(str(e))}

    # 3. Sync coordinator status
    try:
        from plex_playlist_sync.api.routes.sync import sync_state

        sync_status = {
            "is_syncing": sync_state.is_syncing,
            "last_run_at": sync_state.last_run_at,
            "last_run_stats": sync_state.last_run_stats,
        }
    except Exception as e:
        logger.warning("Failed to query sync state: %s", redact_text(str(e)))
        sync_status = {"is_syncing": False, "error": redact_text(str(e))}

    # 4. Wanted backlog worker status
    try:
        from plex_playlist_sync.backlog_worker import backlog_worker

        backlog_status = backlog_worker.get_status()
    except Exception as e:
        logger.warning("Failed to query backlog worker: %s", redact_text(str(e)))
        backlog_status = {"running": False, "error": redact_text(str(e))}

    # 5. RSS sync worker status
    try:
        from plex_playlist_sync.backlog_worker import rss_worker

        rss_status = rss_worker.get_status()
    except Exception as e:
        logger.warning("Failed to query rss worker: %s", redact_text(str(e)))
        rss_status = {"running": False, "error": redact_text(str(e))}

    return WorkerStatus(
        acquisition_worker=acq_status,
        lidarr_worker=lidarr_status,
        sync_coordinator=sync_status,
        backlog_worker=backlog_status,
        rss_worker=rss_status,
    )


def _connected_media_client(config: Config) -> Optional[Any]:
    """A reachable client of the configured media server, or None (probe helper for the status endpoint)."""
    if config.media_server_type in (MEDIA_SERVER_SUBSONIC, MEDIA_SERVER_JELLYFIN):
        server = build_subsonic(config) if config.media_server_type == MEDIA_SERVER_SUBSONIC else build_jellyfin(config)
        if server is None:
            return None
        return True if server.test_connection().ok else None  # the probe only needs "reachable or not"
    return get_plex_client(config)


@router.get("/media-server", response_model=MediaServerStatus, response_model_exclude_unset=True, summary="Active media server and the features it enables")
def get_media_server_status(
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Unauthenticated (the login screen needs it) and non-sensitive: type, connectivity, capability flags."""
    return media_server_status(config, lambda: _connected_media_client(config))


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


# -----------------------------------------------------------------------------
# Scheduled Tasks Registry & Execution Routes
# -----------------------------------------------------------------------------

class ScheduledTaskItem(ApiModel):
    id: str
    name: str
    description: str
    interval: str  # display string, e.g. "Every 24h"
    status: str  # "idle" | "running" | "paused" | "failed"
    last_run_at: Optional[str] = None
    can_trigger: bool = True
    can_cancel: bool = False
    schedule_kind: str = "manual"  # "interval" | "continuous" | "manual"
    interval_seconds: Optional[int] = None  # the interval in force (override > config/env > default)
    default_interval_seconds: Optional[int] = None  # what resetting the schedule returns to
    interval_presets: list[int] = []  # allowed values for PUT /tasks/{id}/schedule; empty when not editable
    editable: bool = False
    next_run_at: Optional[str] = None  # last run end + interval, interval tasks only
    last_run_status: Optional[str] = None  # success | failed | cancelled
    last_duration_ms: Optional[int] = None
    current_run_started_at: Optional[str] = None
    progress: Optional[TaskProgress] = None


VALID_TASK_IDS = set(TASKS)

# Tasks an admin can start by hand (the POST /run branches below).
# pending_releases and import_list_sync stay non-runnable: pending_releases already checks every
# minute and forcing it would bypass the delay profile; import lists have per-list Sync buttons
# and a run-now here would only process lists that are already due.
_MANUAL_RUNNABLE = {
    "filesystem_scan",
    "playlist_sync",
    "wanted_backlog_sweep",
    "indexer_rss_sync",
    "lidarr_auto_trickle",
    "download_queue_monitor",
    "artist_metadata_refresh",
    "art_thumbnail_backfill",
    "seed_cleanup",
    "library_health",
    "recycle_bin_cleanup",
    "lidarr_request_retry",
    "scrobble_sync",
    "mix_generation",
    "backup",
    "update_check",
}

# Display text of the polling loops, whose period is fixed in code.
_CONTINUOUS_DISPLAY = {
    "lidarr_request_retry": "Checks every 1m",
    "pending_releases": "Checks every 1m",
    "import_list_sync": "Checks every 5m",
    "scrobble_sync": "Checks every 30s",
}

_running_tasks: set[str] = set()
_tasks_lock = threading.Lock()


def _later_iso(*values: Optional[str]) -> Optional[str]:
    """The most recent of several ISO timestamps (unparseable ones are ignored)."""
    best: Optional[tuple[datetime, str]] = None
    for value in values:
        parsed = parse_iso(value)
        if parsed is not None and (best is None or parsed > best[0]):
            best = (parsed, str(value))
    return best[1] if best else None


def _task_progress(task_id: str, running: bool) -> Optional[TaskProgress]:
    """Progress from in-memory worker state only (cheap, no external calls). None when the task cannot tell."""
    if not running:
        return None
    if task_id == "filesystem_scan":
        stat = library_scanner.get_status()
        total = int(stat.get("total_files_found") or 0)
        return TaskProgress(
            current=int(stat.get("processed_files") or 0),
            total=total or None,
            message=redact_text(str(stat["current_file"])) if stat.get("current_file") else None,
        )
    if task_id == "lidarr_auto_trickle":
        stat = lidarr_worker.get_status()
        if stat.get("is_running"):
            return TaskProgress(
                current=int(stat.get("processed_items") or 0),
                total=int(stat.get("total_items") or 0) or None,
                message=str(stat.get("message")) if stat.get("message") else None,
            )
    message = job_tracker.running_message(task_id)
    return TaskProgress(message=redact_text(message)) if message else None


def get_all_scheduled_tasks(
    db: Database,
    config: Optional[Config] = None,
) -> list[ScheduledTaskItem]:
    """Returns the unified registry of background tasks: schedule, last run (from the persisted run history),
    next run and live status."""
    try:
        latest = db.latest_task_runs()
        finished = db.latest_finished_task_runs()
    except sqlite3.Error as exc:
        logger.warning("Could not read the task run history: %s", safe_exc(exc))
        latest, finished = {}, {}

    # Worker-owned state, each read once. These are in-memory except the three that read the database.
    scan_stat = library_scanner.get_status()
    backlog_stat = backlog_worker.get_status()
    rss_stat = rss_worker.get_status()
    lidarr_stat = lidarr_worker.get_status()
    ar_stat = artist_refresh_worker.get_status()
    sc_stat = seed_cleanup.get_status(db)
    rb_stat = recycle_bin.get_status()
    try:
        lh_run = db.get_last_library_health_run() or {}
    except sqlite3.Error as exc:
        logger.warning("Could not read the last library health run: %s", safe_exc(exc))
        lh_run = {}

    lidarr_mode = get_library_mode(db) == MODE_LIDARR

    # status, last run known to the worker itself (for runs from before the history existed or not recorded), cancel
    runtime: dict[str, tuple[str, Optional[str], bool]] = {
        "filesystem_scan": (
            "running"
            if (scan_stat.get("is_scanning") or scan_stat.get("status") == "scanning" or "filesystem_scan" in _running_tasks)
            else ("failed" if scan_stat.get("status") == "failed" else "idle"),
            scan_stat.get("completed_at") or scan_stat.get("started_at"),
            True,
        ),
        "playlist_sync": (
            "running" if (sync_state.is_syncing or "playlist_sync" in _running_tasks) else "idle",
            sync_state.last_run_at,
            False,
        ),
        "wanted_backlog_sweep": (
            "running" if "wanted_backlog_sweep" in _running_tasks else "idle",
            backlog_stat.get("last_run_at"),
            False,
        ),
        "indexer_rss_sync": (
            "running" if "indexer_rss_sync" in _running_tasks else "idle",
            rss_stat.get("last_run_at"),
            False,
        ),
        "lidarr_auto_trickle": (
            "paused"
            if lidarr_stat.get("is_paused")
            else ("running" if (lidarr_stat.get("is_running") or "lidarr_auto_trickle" in _running_tasks) else "idle"),
            lidarr_stat.get("last_processed_at") or lidarr_stat.get("started_at"),
            True,
        ),
        "download_queue_monitor": (
            "running"
            if ("download_queue_monitor" in _running_tasks or (hasattr(acquisition_worker, "is_running") and acquisition_worker.is_running()))
            else "idle",
            None,
            False,
        ),
        "artist_metadata_refresh": (
            "running" if (ar_stat.get("running") or "artist_metadata_refresh" in _running_tasks) else "idle",
            ar_stat.get("last_run_at"),
            False,
        ),
        "art_thumbnail_backfill": ("running" if "art_thumbnail_backfill" in _running_tasks else "idle", None, False),
        "seed_cleanup": (
            "running" if (sc_stat.get("running") or "seed_cleanup" in _running_tasks) else "idle",
            (sc_stat.get("last_run") or {}).get("finished_at"),
            False,
        ),
        "library_health": (
            "running" if (library_health.is_running() or "library_health" in _running_tasks) else "idle",
            lh_run.get("finished_at") or lh_run.get("started_at"),
            False,
        ),
        "recycle_bin_cleanup": (
            "running" if (rb_stat.get("running") or "recycle_bin_cleanup" in _running_tasks) else "idle",
            (rb_stat.get("last_run") or {}).get("finished_at"),
            False,
        ),
        "lidarr_request_retry": (
            "running" if "lidarr_request_retry" in _running_tasks else "idle",
            library_manager.last_retry_sweep_at,
            False,
        ),
        "scrobble_sync": (
            "running" if "scrobble_sync" in _running_tasks else "idle",
            scrobble_worker.get_status().get("last_poll_at"),
            False,
        ),
        "mix_generation": (
            "running" if "mix_generation" in _running_tasks else "idle",
            mix_worker.get_status().get("last_run_at"),
            False,
        ),
    }

    tasks: list[ScheduledTaskItem] = []
    for spec in TASKS.values():
        if spec.id == "lidarr_request_retry" and not lidarr_mode:
            continue  # Lidarr mode only
        status_val, worker_last, can_cancel = runtime.get(spec.id, ("idle", None, False))
        latest_row = latest.get(spec.id)
        finished_row = finished.get(spec.id)
        db_running = bool(latest_row and latest_row.get("status") == "running")
        if db_running and status_val == "idle":
            status_val = "running"

        # The persisted history is the source; the worker's own timestamp only covers a task that has no recorded run.
        last_run = (
            _later_iso((finished_row or {}).get("finished_at"), (latest_row or {}).get("started_at"))
            if latest_row
            else worker_last
        )
        last_finished = (finished_row or {}).get("finished_at") if latest_row else worker_last
        effective = effective_interval_seconds(db, config, spec.id)
        unscheduled = schedule_disabled(config, spec.id)
        if unscheduled:
            effective = None
        if unscheduled:
            display = "Manual / On Demand"
        elif spec.kind == KIND_INTERVAL:
            display = format_interval(effective)
        elif spec.id == "download_queue_monitor":
            display = f"Every {int(getattr(acquisition_worker, 'poll_interval', 5.0))}s"
        else:
            display = _CONTINUOUS_DISPLAY.get(spec.id, "Manual / On Demand")

        description = spec.description
        if spec.id == "recycle_bin_cleanup":
            rb_days = int(db.get_media_management_settings().get("recycle_bin_cleanup_days") or 0)
            description = (
                f"Deletes recycle bin folders older than {rb_days} days." if rb_days > 0
                else "Automatic recycle bin cleanup is off (cleanup days is 0)."
            )

        tasks.append(
            ScheduledTaskItem(
                id=spec.id,
                name=spec.name,
                description=description,
                interval=display,
                status=status_val,
                last_run_at=last_run,
                can_trigger=spec.id in _MANUAL_RUNNABLE,
                can_cancel=can_cancel,
                schedule_kind="manual" if unscheduled else spec.kind,
                interval_seconds=effective,
                default_interval_seconds=None if unscheduled else default_interval_seconds(db, config, spec.id),
                interval_presets=[] if unscheduled else list(spec.presets),
                editable=spec.editable and not unscheduled,
                next_run_at=None
                if (status_val == "running" or unscheduled)
                else next_run_at(spec, effective, last_finished),
                last_run_status=(finished_row or {}).get("status"),
                last_duration_ms=(finished_row or {}).get("duration_ms"),
                current_run_started_at=(latest_row or {}).get("started_at") if db_running else None,
                progress=_task_progress(spec.id, status_val == "running"),
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


class TaskScheduleUpdate(BaseModel):
    """Body of ``PUT /tasks/{task_id}/schedule``. ``interval_seconds`` must be one of the task's presets; null resets."""

    model_config = ConfigDict(extra="forbid")
    interval_seconds: Optional[int] = None


@router.put(
    "/tasks/{task_id}/schedule",
    response_model=ScheduledTaskItem,
    summary="Set (or reset) a task's run interval from its presets",
)
def set_task_schedule(
    task_id: str,
    body: TaskScheduleUpdate,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    _admin: dict[str, Any] = Depends(require_admin),
) -> ScheduledTaskItem:
    """Stores an interval override for an editable task (admin required). Workers pick it up within a second."""
    spec = TASKS.get(task_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    if not spec.editable or schedule_disabled(config, task_id):
        raise HTTPException(status_code=400, detail=f"Task '{task_id}' does not have an editable schedule")
    if body.interval_seconds is not None and body.interval_seconds not in spec.presets:
        raise HTTPException(
            status_code=400,
            detail=f"interval_seconds must be one of {list(spec.presets)} for task '{task_id}'",
        )
    try:
        set_interval_override(db, task_id, body.interval_seconds)
    except sqlite3.Error as exc:
        logger.error("Could not store the schedule of '%s': %s", task_id, safe_exc(exc))
        raise HTTPException(status_code=500, detail="Could not store the schedule")
    try:
        db.record_event(
            "task_schedule_changed",
            f"Schedule of '{task_id}' " + ("reset to default" if body.interval_seconds is None else f"set to {body.interval_seconds}s"),
            source="TaskManager",
            severity="info",
            details={"task_id": task_id, "interval_seconds": body.interval_seconds},
        )
    except sqlite3.Error as ev_err:
        logger.warning("Failed to record task_schedule_changed event: %s", safe_exc(ev_err))
    return next(item for item in get_all_scheduled_tasks(db, config) if item.id == task_id)


@router.get(
    "/tasks/{task_id}/runs",
    response_model=list[TaskRunItem],
    summary="Run history of one task, newest first",
)
def get_task_runs(
    task_id: str,
    days: int = Query(default=7, ge=1, le=30),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Persisted runs (scheduled, manual, startup and event) of a task; history is kept for 7 days."""
    if task_id not in TASKS:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return db.list_task_runs(task_id, since, limit)


@router.get(
    "/activity",
    response_model=ActivityResponse,
    summary="Running tasks and the last finished runs (cheap; for the header activity indicator)",
)
def get_system_activity(
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Two indexed reads and in-memory progress; no external calls, so it is safe to poll every few seconds."""
    running = []
    for row in db.list_running_task_runs():
        spec = TASKS.get(str(row["task_id"]))
        running.append(
            {
                "task_id": row["task_id"],
                "name": spec.name if spec else str(row["task_id"]),
                "started_at": row["started_at"],
                "progress": _task_progress(str(row["task_id"]), True),
            }
        )
    recent = [
        {
            "task_id": row["task_id"],
            "name": TASKS[row["task_id"]].name if row["task_id"] in TASKS else str(row["task_id"]),
            "status": row["status"],
            "finished_at": row["finished_at"],
        }
        for row in db.list_recent_finished_task_runs(5)
    ]
    return {"running": running, "recent": recent}


@router.get(
    "/resources",
    response_model=ResourcesResponse,
    summary="CPU, memory, thread count and uptime of the TrackSeerr process",
)
def get_system_resources(_admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    """Process-wide numbers from /proc (null where unavailable). CPU is measured since the previous call."""
    return dict(process_stats.sample())


def _run_manual(db: Database, task_id: str, thread_name: str, body: Callable[[RunHandle], None]) -> None:
    """Runs ``body`` on a thread under the task run history (trigger ``manual``) and the in-memory running set."""

    def _thread() -> None:
        with _tasks_lock:
            _running_tasks.add(task_id)
        try:
            with record_task_run(db, task_id, TRIGGER_MANUAL) as run:
                body(run)
        except Exception as exc:  # the run is recorded as failed and logged by record_task_run; keep the thread quiet
            logger.debug("Manual run of '%s' ended with %s", task_id, safe_exc(exc), exc_info=True)
        finally:
            with _tasks_lock:
                _running_tasks.discard(task_id)

    threading.Thread(target=_thread, daemon=True, name=thread_name).start()


@router.post("/tasks/{task_id}/run", response_model=TaskActionResult, response_model_exclude_unset=True, summary="Trigger a scheduled task manually")
def run_scheduled_task(
    task_id: str,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[PlexClient] = Depends(get_plex_client),
    spotify_client: Optional[SpotifyClient] = Depends(get_spotify_client),
    deezer_client: Optional[DeezerClient] = Depends(get_deezer_client),
    lidarr_client: Optional[LidarrClient] = Depends(get_lidarr_client),
    media_server: Optional[MediaServer] = Depends(get_active_media_server),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Dispatches the specified background task asynchronously (admin required)."""
    if task_id not in VALID_TASK_IDS:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    if task_id not in _MANUAL_RUNNABLE:
        raise HTTPException(status_code=400, detail=f"Task '{task_id}' runs continuously and cannot be started manually")

    if task_id == "lidarr_auto_trickle" and get_library_mode(db) != MODE_LIDARR:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Library manager is set to TrackSeerr; the Lidarr trickle is disabled.",
        )

    if task_id == "lidarr_request_retry" and get_library_mode(db) != MODE_LIDARR:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Library manager is set to TrackSeerr; there are no Lidarr requests to retry.",
        )

    # The three tasks below record their own run (trigger manual) inside the function that starts them.
    if task_id == "seed_cleanup":
        if get_library_mode(db) == MODE_LIDARR:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Library manager is Lidarr; seed cleanup is disabled.")
        if not seed_cleanup.start_sweep_async(db):
            return {"success": True, "message": "Task 'seed_cleanup' is already running"}
    elif task_id == "recycle_bin_cleanup":
        if not recycle_bin.start_cleanup_async(db):
            return {"success": True, "message": "Task 'recycle_bin_cleanup' is already running"}
    elif task_id == "library_health":
        music_root = Path(db.get_media_management_settings().get("root_folder_path") or "/music")
        if not library_health.start_check_async(db, media_server, music_root=music_root):
            return {"success": True, "message": "Task 'library_health' is already running"}

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
                # The scanner records its own run (trigger manual) from the scan thread it starts.
                library_scanner.start_scan(db=db, plex_client=plex_client)
            finally:
                with _tasks_lock:
                    _running_tasks.discard("filesystem_scan")

        threading.Thread(target=_scan_thread, daemon=True, name="ManualScanTask").start()

    elif task_id == "playlist_sync":
        def _sync(run: RunHandle) -> None:
            run.apply_result(
                sync_state.execute_sync(
                    db=db,
                    config=config,
                    plex_client=plex_client,
                    spotify_client=spotify_client,
                    deezer_client=deezer_client,
                )
            )

        _run_manual(db, "playlist_sync", "ManualSyncTask", _sync)

    elif task_id == "wanted_backlog_sweep":
        _run_manual(db, task_id, "ManualBacklogTask", lambda run: run.apply_result(backlog_worker.poll_once(db=db)))

    elif task_id == "indexer_rss_sync":
        _run_manual(db, task_id, "ManualRSSTask", lambda run: run.apply_result(rss_worker.poll_once(db=db)))

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
                        # The trickle thread records its own run (trigger manual).
                        lidarr_worker.start_trickle(
                            items=items_to_push,
                            client=lidarr_client,
                            db=db,
                            delay_seconds=delay_sec,
                            auto_search=auto_srch,
                            batch_size=batch_size,
                            trigger=TRIGGER_MANUAL,
                        )
            finally:
                with _tasks_lock:
                    _running_tasks.discard("lidarr_auto_trickle")

        threading.Thread(target=_lidarr_thread, daemon=True, name="ManualLidarrTask").start()

    elif task_id == "lidarr_request_retry":
        def _retry(run: RunHandle) -> None:
            resent = library_manager.retry_stuck_lidarr_requests(db, config, ignore_schedule=True)
            run.message = f"resent={resent}"

        _run_manual(db, task_id, "ManualRequestRetryTask", _retry)

    elif task_id == "download_queue_monitor":
        def _acq(run: RunHandle) -> None:
            with track_job("download_queue_monitor", "Acquisition Worker") as job:
                job.message = run.message = summarize_result(acquisition_worker.poll_once(db=db, plex_client=plex_client))

        _run_manual(db, task_id, "ManualAcquisitionTask", _acq)

    elif task_id == "artist_metadata_refresh":
        _run_manual(db, task_id, "ManualArtistRefreshTask", lambda run: run.apply_result(artist_refresh_worker.refresh_once(db=db)))

    elif task_id == "art_thumbnail_backfill":
        with _tasks_lock:
            if "art_thumbnail_backfill" in _running_tasks:
                return {"success": True, "message": "Task 'art_thumbnail_backfill' is already running"}
            _running_tasks.add("art_thumbnail_backfill")

        def _art_backfill_thread():
            try:
                # Records its own run (trigger manual) and refuses to overlap another backfill.
                art_pipeline.run_backfill_task(db, TRIGGER_MANUAL)
            except Exception as exc:  # the run is recorded as failed; keep the thread from dying silently
                logger.error("Artwork thumbnail backfill failed: %s", safe_exc(exc))
            finally:
                with _tasks_lock:
                    _running_tasks.discard("art_thumbnail_backfill")

        threading.Thread(target=_art_backfill_thread, daemon=True, name="ArtBackfillTask").start()

    elif task_id == "scrobble_sync":
        def _scrobble(run: RunHandle) -> None:
            outcome = scrobble_worker.run_now(db, config)
            run.message = f"ingested={outcome.get('ingested', 0)}, retried={outcome.get('retried', 0)}"

        _run_manual(db, task_id, "ManualScrobbleSyncTask", _scrobble)

    elif task_id == "mix_generation":
        def _mix(run: RunHandle) -> None:
            outcome = mix_worker.run_now(db, config)
            run.message = f"due={outcome.get('due', 0)}, generated={outcome.get('generated', 0)}, errors={outcome.get('errors', 0)}"

        _run_manual(db, task_id, "ManualMixGenerationTask", _mix)

    elif task_id == "backup":
        from plex_playlist_sync.backup import create_backup, get_backup_retention, prune_scheduled

        def _backup(run: RunHandle) -> None:
            retention = get_backup_retention(db)
            path = create_backup(db, kind="manual")
            pruned = prune_scheduled(retention=retention, backup_dir=path.parent)
            run.message = f"created={path.name}, pruned={len(pruned)}"

        _run_manual(db, task_id, "ManualBackupTask", _backup)

    elif task_id == "update_check":
        def _update(run: RunHandle) -> None:
            outcome = run_update_check(db, config)
            if outcome.get("error"):
                run.failed = outcome["error"]
                run.message = f"error: {outcome['error']}"
            elif outcome.get("latest_version"):
                avail = " (update available)" if outcome.get("update_available") else ""
                run.message = f"latest={outcome['latest_version']}{avail}"
            else:
                run.message = "no releases found"

        _run_manual(db, task_id, "ManualUpdateCheckTask", _update)

    return {"success": True, "message": f"Task '{task_id}' dispatched successfully"}


@router.get(
    "/queue",
    response_model=JobQueueSnapshot, response_model_exclude_unset=True,
    summary="Running, queued and recently finished background jobs",
    dependencies=[Depends(require_core_tier)],
)
def get_job_queue(_admin: dict[str, Any] = Depends(require_admin)) -> dict[str, list[dict[str, Any]]]:
    """In-memory job list: what is running now, what is queued, and the last 50 finished runs (admin only)."""
    return job_tracker.snapshot()


@router.get(
    "/lidarr-health",
    response_model=LidarrHealthResponse, response_model_exclude_unset=True,
    summary="Lidarr reachability, version and health checks",
    dependencies=[Depends(require_core_tier)],
)
def get_lidarr_health(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Lidarr's own health checks. Native mode never contacts Lidarr. Never raises: an unreachable Lidarr is
    reported as ``reachable: false`` with the checks empty."""
    mode = get_library_mode(db)
    if mode != MODE_LIDARR:
        return {"mode": MODE_NATIVE, "reachable": None, "version": None, "health": []}
    unreachable: dict[str, Any] = {"mode": MODE_LIDARR, "reachable": False, "version": None, "health": []}
    try:
        client = build_lidarr_client(db, config)
        if client is None:
            return unreachable
        status_info = client.test_connection()
        if not status_info.get("online"):
            logger.info("Lidarr health: not reachable (%s)", redact_text(str(status_info.get("error", ""))))
            return unreachable
        return {
            "mode": MODE_LIDARR,
            "reachable": True,
            "version": status_info.get("version"),
            "health": client.get_health(),
        }
    except (LidarrApiError, sqlite3.Error) as exc:
        logger.warning("Lidarr health check failed: %s", redact_text(str(exc)))
        return unreachable


@router.post("/tasks/{task_id}/cancel", response_model=TaskActionResult, response_model_exclude_unset=True, summary="Cancel a running scheduled task")
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

    elif task_id == "backup":
        from plex_playlist_sync.backup import cancel_backup

        cancel_backup()
        try:
            db.record_event(
                "task_cancelled",
                "Scheduled task 'backup' cancelled by admin",
                source="TaskManager",
                severity="info",
                details={"task_id": "backup"},
            )
        except Exception as ev_err:
            logger.warning("Failed to record task_cancelled event: %s", ev_err)
        return {"success": True, "message": "Backup task cancelled"}

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Task '{task_id}' does not support cancellation",
        )


@router.get(
    "/update",
    response_model=SystemUpdateResponse,
    response_model_exclude_unset=True,
    summary="TrackSeerr update status and check configuration",
    dependencies=[Depends(require_core_tier)],
)
def get_system_update(
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Returns current and latest version, release URL, timestamps, and update status (admin only)."""
    return get_update_status(db, config)


@router.put(
    "/update",
    response_model=SystemUpdateResponse,
    response_model_exclude_unset=True,
    summary="Enable or disable software update check",
    dependencies=[Depends(require_core_tier)],
)
def put_system_update(
    payload: UpdateSettingsPayload,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Updates the update_check_enabled setting (admin only)."""
    db.set_update_check_enabled(payload.enabled)
    return get_update_status(db, config)


"""System diagnostics, hardware, and service status API routes."""


import logging
import os
import platform
import shutil
import sqlite3
import time
from typing import (
    Any,
    Optional,
)
from fastapi import (
    APIRouter,
    Depends,
)
from trackseerr.api.response_models import ApiModel
from trackseerr.api.schemas.system import (
    MediaServerStatus,
)
from trackseerr.api.dependencies import (
    get_config,
    get_db,
    get_plex_client,
    require_admin,
)
from trackseerr.redaction import (
    redact_text,
    safe_exc,
)
from trackseerr.clients.acquisition import (
    get_acquisition_driver,
    get_indexer_driver,
)
from trackseerr.clients.plex import PlexClient
from trackseerr.media_servers import as_media_server, build_jellyfin, build_subsonic
from trackseerr.config import MEDIA_SERVER_JELLYFIN, MEDIA_SERVER_SUBSONIC, Config
from trackseerr.media_server import media_server_status
from trackseerr.models import (
    DownloadClientConfig,
    IndexerConfig,
)
from trackseerr.security import is_safe_service_url
from trackseerr.storage import Database



logger = logging.getLogger(__name__)


router = APIRouter()


# Track application start timestamp at module import
_APP_START_TIME = time.time()


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
        from trackseerr.acquisition_worker import acquisition_worker

        acq_status = {"running": acquisition_worker.is_running()}
    except Exception as e:
        logger.warning("Failed to query acquisition worker: %s", redact_text(str(e)))
        acq_status = {"running": False, "error": redact_text(str(e))}

    # 2. Lidarr trickle worker status
    try:
        from trackseerr.lidarr_queue import lidarr_worker

        lidarr_status = lidarr_worker.get_status()
    except Exception as e:
        logger.warning("Failed to query lidarr worker: %s", redact_text(str(e)))
        lidarr_status = {"running": False, "error": redact_text(str(e))}

    # 3. Sync coordinator status
    try:
        from trackseerr.api.routes.sync import sync_state

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
        from trackseerr.backlog_worker import backlog_worker

        backlog_status = backlog_worker.get_status()
    except Exception as e:
        logger.warning("Failed to query backlog worker: %s", redact_text(str(e)))
        backlog_status = {"running": False, "error": redact_text(str(e))}

    # 5. RSS sync worker status
    try:
        from trackseerr.backlog_worker import rss_worker

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

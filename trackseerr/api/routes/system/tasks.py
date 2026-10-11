"""Scheduled background tasks registry and execution API routes."""


from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable, Optional
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    status,
)
from pydantic import BaseModel, ConfigDict
from trackseerr.api.response_models import ApiModel
from trackseerr.api.schemas.system import (
    ActivityResponse,
    JobQueueSnapshot,
    LidarrHealthResponse,
    ResourcesResponse,
    TaskActionResult,
    TaskProgress,
    TaskRunItem,
)
from trackseerr import art_pipeline, library_health, process_stats, recycle_bin, seed_cleanup
from trackseerr.acquisition_worker import acquisition_worker
from trackseerr.update_check import (
    run_update_check,
)
from trackseerr.artist_refresh_worker import artist_refresh_worker
from trackseerr.api.dependencies import (
    get_active_media_server,
    get_config,
    get_db,
    get_deezer_client,
    get_lidarr_client,
    get_plex_client,
    get_spotify_client,
    require_admin,
    require_core_tier,
)
from trackseerr.api.routes.sync import sync_state
from trackseerr.redaction import (
    redact_text,
    safe_exc,
)
from trackseerr.backlog_worker import backlog_worker, rss_worker
from trackseerr.clients.deezer import DeezerClient
from trackseerr.clients.lidarr import LidarrApiError, LidarrClient
from trackseerr.clients.plex import PlexClient
from trackseerr.media_servers.base import MediaServer
from trackseerr.clients.spotify import SpotifyClient
from trackseerr.config import (
    Config,
)
from trackseerr.job_tracker import job_tracker, summarize_result, track_job
from trackseerr import library_manager
from trackseerr.library_manager import MODE_LIDARR, MODE_NATIVE, build_lidarr_client, get_library_mode
from trackseerr.library_scanner import library_scanner
from trackseerr.lidarr_migration import lidarr_migration_job
from trackseerr.lidarr_queue import lidarr_worker
from trackseerr.mix_worker import mix_worker
from trackseerr.scrobble_worker import scrobble_worker
from trackseerr.storage import Database
from trackseerr.task_manager import (
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


router = APIRouter()


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
    if task_id == "lidarr_migration" and lidarr_migration_job.is_running():
        processed, total, mig_message = lidarr_migration_job.progress_snapshot()
        return TaskProgress(current=processed, total=total or None, message=mig_message)
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
    mig_stat = lidarr_migration_job.get_status()
    try:
        lh_run = db.get_last_library_health_run() or {}
    except sqlite3.Error as exc:
        logger.warning("Could not read the last library health run: %s", safe_exc(exc))
        lh_run = {}

    lidarr_mode = get_library_mode(db) == MODE_LIDARR

    # status, last run known to the worker itself (for runs from before the history existed or not recorded), cancel
    runtime: dict[str, tuple[str, Optional[str], bool]] = {
        "lidarr_migration": (
            "running"
            if lidarr_migration_job.is_running()
            else ("failed" if mig_stat.get("status") == "failed" else "idle"),
            mig_stat.get("completed_at") or mig_stat.get("started_at"),
            True,
        ),
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
        if spec.id == "lidarr_migration" and not lidarr_mode and status_val != "running" and spec.id not in latest:
            continue  # only relevant while importing from Lidarr or once it has run

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
def run_scheduled_task(  # noqa: C901, PLR0915
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
        from trackseerr.backup import create_backup, get_backup_retention, prune_scheduled

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
        from trackseerr.backup import cancel_backup

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

    elif task_id == "lidarr_migration":
        lidarr_migration_job.cancel()
        try:
            db.record_event(
                "task_cancelled",
                "Scheduled task 'lidarr_migration' cancelled by admin",
                source="TaskManager",
                severity="info",
                details={"task_id": "lidarr_migration"},
            )
        except Exception as ev_err:
            logger.warning("Failed to record task_cancelled event: %s", ev_err)
        return {"success": True, "message": "Lidarr import cancelled"}

    else:
        raise HTTPException(
            status_code=400,
            detail=f"Task '{task_id}' does not support cancellation",
        )

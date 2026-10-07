"""Seed cleanup: removes finished torrents the app has lost track of and surfaces the ones it must not touch.

``run_sweep`` is the scheduled/manual task (see ``docs/seed-cleanup.md``):

1. Tracked downloads held for seeding are re-evaluated with the shared ``evaluate_seed_cleanup`` function (same seed
   goal and ``deletion_safe`` gate as the import path). A failed removal is counted per download; after
   ``MAX_ATTEMPTS`` failures a ``cleanup_failed`` Needs-review finding replaces further silent retries.
2. Each torrent client's category is listed and every torrent matched by hash. A torrent whose download row is already
   terminal (or known only from import history) is evaluated like a tracked one. A torrent TrackSeerr never grabbed
   or imported becomes an ``orphan_torrent`` finding and is NEVER removed by the sweep; only an explicit user action
   (``remove_orphan``) removes it.
"""

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from plex_playlist_sync.acquisition_worker import (
    SeedOutcome,
    _client_path_mappings,
    _under_path,
    deletion_safe,
    effective_import_mode,
    evaluate_seed_cleanup,
    seed_action,
    translate_remote_path,
)
from plex_playlist_sync.clients.acquisition import get_acquisition_driver, is_torrent_driver_type
from plex_playlist_sync.job_tracker import track_job
from plex_playlist_sync.task_manager import (
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULED,
    record_task_run,
    wait_for_next_cycle,
)
from plex_playlist_sync.library_health import (
    CAUSE_CLEANUP_FAILED,
    CAUSE_ORPHAN_TORRENT,
    KIND_CLEANUP_FAILED,
    KIND_ORPHAN_TORRENT,
)
from plex_playlist_sync.models import DownloadStatus
from plex_playlist_sync.redaction import redact_text, safe_exc

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
WORKER_INTERVAL_SECONDS = 24 * 3600.0
WORKER_INITIAL_DELAY_SECONDS = 600.0

_LIVE_STATUSES = frozenset(
    {
        DownloadStatus.QUEUED.value,
        DownloadStatus.DOWNLOADING.value,
        DownloadStatus.IMPORTING.value,
        DownloadStatus.COMPLETED.value,
        DownloadStatus.WARNING.value,
    }
)
# Failures talking to a download client; anything else is a bug and must surface.
_CLIENT_ERRORS = (httpx.HTTPError, RuntimeError, ValueError, OSError)

_run_lock = threading.Lock()
_state_lock = threading.Lock()
_last_run: Optional[dict[str, Any]] = None


class SeedCleanupBusy(RuntimeError):
    """A sweep is already running."""


@dataclass
class _Stats:
    evaluated: int = 0
    removed: int = 0
    deleted_files: int = 0
    orphans: int = 0
    failures: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "evaluated": self.evaluated,
            "removed": self.removed,
            "deleted_files": self.deleted_files,
            "orphans": self.orphans,
            "failures": self.failures,
        }


def is_running() -> bool:
    return _run_lock.locked()


LAST_RUN_KV = "seed_cleanup_last_run"


def get_status(db: Any = None) -> dict[str, Any]:
    """``{running, last_run}`` for the status route. ``last_run`` is the newest finished sweep: in memory, else the
    copy persisted in ``kv_store`` (survives restarts); None when no sweep has ever finished."""
    with _state_lock:
        last = dict(_last_run) if _last_run else None
    if last is None and db is not None:
        try:
            raw = db.get_kv(LAST_RUN_KV)
            data = json.loads(raw) if raw else None
            last = data if isinstance(data, dict) else None
        except (ValueError, TypeError) as exc:
            logger.warning("Ignoring corrupt %s: %s", LAST_RUN_KV, type(exc).__name__)
    return {"running": is_running(), "last_run": last}


def _iso(now: datetime) -> str:
    return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).isoformat()


def _is_finished(state: str) -> bool:
    """A qBittorrent state that means the download is complete (seeding, paused or queued for upload)."""
    s = (state or "").lower()
    return s == "uploading" or s.endswith("up")


def _local_content_path(db: Any, client_id: Any, raw: str) -> Optional[Path]:
    mapped = translate_remote_path(raw, _client_path_mappings(db, client_id)) if raw else None
    return Path(mapped).resolve() if mapped else None


def content_in_music_root(db: Any, client_id: Any, raw_content_path: str) -> bool:
    """True when the content path is unknown, or overlaps the music root (either contains the other)."""
    local = _local_content_path(db, client_id, raw_content_path)
    if local is None:
        return True
    root_raw = str(db.get_media_management_settings().get("root_folder_path") or "").strip()
    if not root_raw:
        return True
    root = Path(root_raw).resolve()
    return _under_path(local, root) or _under_path(root, local)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def _cleanup_failed_findings(db: Any) -> list[dict[str, Any]]:
    return [f for f in db.list_library_health_findings(include_dismissed=True) if f["kind"] == KIND_CLEANUP_FAILED]


def _clear_failed_finding(db: Any, download_id: str) -> None:
    for f in _cleanup_failed_findings(db):
        if str((f.get("detail") or {}).get("download_id")) == str(download_id):
            db.delete_library_health_finding_by_path(f["path"], kind=KIND_CLEANUP_FAILED)


def _record_failure(db: Any, row: dict[str, Any], error: str, content_path: str, now: datetime) -> None:
    attempts = int(row.get("cleanup_attempts") or 0) + 1
    db.record_cleanup_result(row["id"], attempts, error)
    row["cleanup_attempts"] = attempts
    row["cleanup_error"] = error
    if attempts >= MAX_ATTEMPTS:
        path = str(row.get("title") or content_path or row["id"])
        db.upsert_library_health_findings(
            [
                {
                    "kind": KIND_CLEANUP_FAILED,
                    "server_kind": None,
                    "cause": CAUSE_CLEANUP_FAILED,
                    "group_key": f"client:{row.get('client_id') or ''}",
                    "path": path,
                    "detail": {"download_id": row["id"], "error": error, "attempts": attempts},
                }
            ],
            _iso(now),
        )
        logger.warning("Seed cleanup gave up on download %s after %d attempts: %s", row["id"], attempts, error)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _apply(
    db: Any,
    driver: Any,
    driver_type: str,
    row: dict[str, Any],
    lookup: str,
    status_dict: Optional[dict[str, Any]],
    media_settings: dict[str, Any],
    stats: _Stats,
    now: datetime,
) -> SeedOutcome:
    """Evaluates one download row against its seed goal and applies the configured action (counting failures)."""
    stats.evaluated += 1
    mode = effective_import_mode(driver_type, media_settings)
    outcome = evaluate_seed_cleanup(driver, lookup, media_settings, mode, status_dict, row, db)
    if outcome.error:
        stats.failures += 1
        _record_failure(db, row, outcome.error, str((status_dict or {}).get("content_path") or ""), now)
        return outcome
    if outcome.removed:
        stats.removed += 1
        if outcome.deleted_files:
            stats.deleted_files += 1
        if str(row.get("status") or "").lower() == DownloadStatus.COMPLETED.value:
            db.update_download_status(row["id"], status=DownloadStatus.IMPORTED.value)
        db.record_cleanup_result(row["id"], 0, None)
        _clear_failed_finding(db, row["id"])
        logger.info("Seed cleanup: %s (%s)", lookup, outcome.note)
    return outcome


def _client_driver(db: Any, client_id: Any) -> Optional[tuple[Any, dict[str, Any]]]:
    cfg = db.get_download_client(str(client_id)) if client_id else None
    if not cfg:
        return None
    return get_acquisition_driver(cfg), cfg


def _sweep_tracked(db: Any, media_settings: dict[str, Any], stats: _Stats, now: datetime) -> None:
    rows = db.list_active_downloads(statuses=[DownloadStatus.COMPLETED.value])
    for row in rows:
        if not is_torrent_driver_type(row.get("client_driver_type")) or not row.get("target_path"):
            continue  # only imported torrents held for seeding; downloads still importing belong to the worker
        if int(row.get("cleanup_attempts") or 0) >= MAX_ATTEMPTS:
            continue  # surfaced as a finding; the user must act
        lookup = row.get("download_hash") or row["id"]
        try:
            pair = _client_driver(db, row.get("client_id"))
            if pair is None:
                logger.warning("Seed cleanup: client for download %s no longer exists", row["id"])
                continue
            driver, cfg = pair
            status_dict = driver.get_status(lookup)
        except _CLIENT_ERRORS as exc:
            logger.warning("Seed cleanup: could not read status of %s: %s", lookup, redact_text(str(exc)))
            continue
        if not status_dict or "ratio" not in status_dict:
            continue  # status unknown: never act on a guess
        _apply(db, driver, str(cfg.get("driver_type") or ""), row, lookup, status_dict, media_settings, stats, now)


def _list_orphan_findings(db: Any) -> list[dict[str, Any]]:
    return [f for f in db.list_library_health_findings(include_dismissed=True) if f["kind"] == KIND_ORPHAN_TORRENT]


def _reconcile_client(
    db: Any, cfg: dict[str, Any], media_settings: dict[str, Any], stats: _Stats, now: datetime
) -> None:
    client_id = str(cfg["id"])
    driver_type = str(cfg.get("driver_type") or "")
    driver = get_acquisition_driver(cfg)
    try:
        torrents = driver.list_category()
    except _CLIENT_ERRORS as exc:
        logger.warning("Seed cleanup: could not list client %s: %s", cfg.get("name") or client_id, redact_text(str(exc)))
        stats.failures += 1
        return
    if torrents is None:
        return  # this driver cannot list its category
    orphan_paths: set[str] = set()
    orphan_hashes: set[str] = set()
    action = seed_action(media_settings)
    for tor in torrents:
        h = str(tor.get("hash") or "").lower()
        if not h:
            continue
        row = db.get_active_download_by_hash(h)
        known = row is not None or db.history_has_hash(h)
        if not known:
            path = str(tor.get("content_path") or tor.get("name") or h)
            orphan_paths.add(path)
            orphan_hashes.add(h)
            db.upsert_library_health_findings(
                [
                    {
                        "kind": KIND_ORPHAN_TORRENT,
                        "server_kind": None,
                        "cause": CAUSE_ORPHAN_TORRENT,
                        "group_key": f"client:{client_id}",
                        "path": path,
                        "detail": {
                            "client_id": client_id,
                            "hash": h,
                            "name": tor.get("name"),
                            "size": int(tor.get("size") or 0),
                            "ratio": float(tor.get("ratio") or 0.0),
                            "seeding_seconds": int(tor.get("seeding_time") or 0),
                        },
                    }
                ],
                _iso(now),
            )
            continue
        if row is not None and str(row.get("status") or "").lower() in _LIVE_STATUSES:
            continue  # tracked (or still being worked on) by the worker and part (a)
        if action == "keep" or not _is_finished(str(tor.get("state") or "")):
            continue
        if row is not None and int(row.get("cleanup_attempts") or 0) >= MAX_ATTEMPTS:
            continue
        status_dict = {
            "ratio": float(tor.get("ratio") or 0.0),
            "seeding_time_seconds": int(tor.get("seeding_time") or 0),
            "content_path": str(tor.get("content_path") or ""),
        }
        if row is None:  # known only from import history: no snapshot, no placed-files record, so files are always kept
            stats.evaluated += 1
            mode = effective_import_mode(driver_type, media_settings)
            outcome = evaluate_seed_cleanup(driver, h, media_settings, mode, status_dict, {"client_id": client_id}, db)
            if outcome.error:
                stats.failures += 1
            elif outcome.removed:
                stats.removed += 1
            continue
        _apply(db, driver, driver_type, row, h, status_dict, media_settings, stats, now)
    # Orphan findings of this client whose torrent is gone (removed by the user, or now matched) are dropped.
    for f in _list_orphan_findings(db):
        detail = f.get("detail") or {}
        if str(detail.get("client_id")) == client_id and str(detail.get("hash") or "").lower() not in orphan_hashes:
            db.delete_library_health_finding_by_path(f["path"], kind=KIND_ORPHAN_TORRENT)
    stats.orphans += len(orphan_hashes)


def _prune_failed_findings(db: Any) -> None:
    for f in _cleanup_failed_findings(db):
        did = (f.get("detail") or {}).get("download_id")
        row = db.get_active_download(str(did)) if did else None
        if row is None or int(row.get("cleanup_attempts") or 0) < MAX_ATTEMPTS:
            db.delete_library_health_finding_by_path(f["path"], kind=KIND_CLEANUP_FAILED)


def run_sweep(db: Any, *, now: Optional[datetime] = None, trigger: str = TRIGGER_MANUAL) -> dict[str, Any]:
    """One sweep, recorded in the job tracker and the task run history. Returns ``{evaluated, removed, deleted_files, orphans, failures}``.

    Raises ``SeedCleanupBusy`` when another sweep is running.
    """
    global _last_run
    if not _run_lock.acquire(blocking=False):
        raise SeedCleanupBusy("A seed cleanup is already running")
    when = now or datetime.now(timezone.utc)
    started = _iso(when)
    stats = _Stats()
    error: Optional[str] = None
    try:
        with record_task_run(db, "seed_cleanup", trigger) as run, track_job("seed_cleanup", "Seed cleanup") as handle:
            media_settings = db.get_media_management_settings()
            if media_settings.get("library_mode") == "lidarr":
                handle.message = "skipped: Lidarr manages the library"
            else:
                if seed_action(media_settings) != "keep":
                    _sweep_tracked(db, media_settings, stats, when)
                for cfg in db.list_download_clients(enabled_only=True):
                    if not is_torrent_driver_type(cfg.get("driver_type")):
                        continue
                    try:
                        _reconcile_client(db, cfg, media_settings, stats, when)
                    except _CLIENT_ERRORS as exc:
                        stats.failures += 1
                        logger.warning(
                            "Seed cleanup: client %s failed: %s", cfg.get("name") or cfg.get("id"), redact_text(str(exc))
                        )
                _prune_failed_findings(db)
                handle.message = (
                    f"evaluated={stats.evaluated}, removed={stats.removed}, orphans={stats.orphans}, failures={stats.failures}"
                )
            run.message = handle.message
        return stats.as_dict()
    except Exception as exc:
        error = safe_exc(exc)
        raise
    finally:
        record = {
            "started_at": started,
            "finished_at": _iso(datetime.now(timezone.utc)),
            "stats": stats.as_dict(),
            "error": error,
        }
        with _state_lock:
            _last_run = record
        try:
            db.set_kv(LAST_RUN_KV, json.dumps(record))
        except Exception as exc:  # noqa: BLE001 - persisting is advisory (sqlite errors and test doubles alike); never mask the sweep result
            logger.warning("Could not persist the seed cleanup last run: %s", safe_exc(exc))
        _run_lock.release()


def start_sweep_async(db: Any) -> bool:
    """Runs a sweep on a background thread. False (nothing started) when one is already running."""
    if _run_lock.locked():
        return False

    def _target() -> None:
        try:
            run_sweep(db)
        except SeedCleanupBusy:
            return
        except Exception as exc:  # noqa: BLE001 - a thread must not die silently; the cause is logged
            logger.error("seed cleanup crashed: %s", safe_exc(exc))
            logger.debug("seed cleanup traceback", exc_info=True)

    threading.Thread(target=_target, daemon=True, name="SeedCleanup").start()
    return True


# ---------------------------------------------------------------------------
# Explicit user actions
# ---------------------------------------------------------------------------


class OrphanActionError(RuntimeError):
    """A user-requested orphan action was refused; ``status`` is the HTTP status the route returns."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def remove_orphan(db: Any, finding: dict[str, Any], delete_files: bool) -> dict[str, Any]:
    """Removes one orphan torrent at the user's explicit request, then drops its finding.

    Re-checks against the live client first: the torrent must still be there and still unknown to TrackSeerr.
    ``delete_files`` is honoured, but refused (409) when the content path is unknown or overlaps the music root.
    """
    detail = finding.get("detail") or {}
    client_id, h = detail.get("client_id"), str(detail.get("hash") or "").lower()
    pair = _client_driver(db, client_id)
    if pair is None or not h:
        raise OrphanActionError("The download client for this torrent no longer exists", 404)
    driver, _cfg = pair
    try:
        torrents = driver.list_category()
    except _CLIENT_ERRORS as exc:
        raise OrphanActionError(f"Could not reach the download client: {redact_text(str(exc))}", 502) from exc
    live = next((t for t in torrents or [] if str(t.get("hash") or "").lower() == h), None)
    if live is None:
        db.delete_library_health_finding_by_path(finding["path"], kind=KIND_ORPHAN_TORRENT)
        raise OrphanActionError("The torrent is no longer in the client", 404)
    if db.get_active_download_by_hash(h) is not None or db.history_has_hash(h):
        db.delete_library_health_finding_by_path(finding["path"], kind=KIND_ORPHAN_TORRENT)
        raise OrphanActionError("TrackSeerr now tracks this torrent; it is no longer an orphan", 409)
    if delete_files and content_in_music_root(db, client_id, str(live.get("content_path") or "")):
        raise OrphanActionError(
            "Refusing to delete files: the torrent's content path is unknown or inside the music library", 409
        )
    try:
        done = driver.cleanup_completed(h, delete_files=bool(delete_files))
    except _CLIENT_ERRORS as exc:
        raise OrphanActionError(f"The download client failed the removal: {redact_text(str(exc))}", 502) from exc
    if done is False:
        raise OrphanActionError("The download client refused the removal", 502)
    db.delete_library_health_finding_by_path(finding["path"], kind=KIND_ORPHAN_TORRENT)
    logger.info("Orphan torrent %s removed by the user (delete_files=%s)", h, bool(delete_files))
    return {"removed": True, "delete_files": bool(delete_files)}


def retry_failed(db: Any, finding: dict[str, Any]) -> dict[str, Any]:
    """Resets a failed download's attempt counter and re-evaluates it now (user action on a ``cleanup_failed`` finding)."""
    download_id = str((finding.get("detail") or {}).get("download_id") or "")
    row = db.get_active_download(download_id) if download_id else None
    if row is None:
        db.delete_library_health_finding_by_path(finding["path"], kind=KIND_CLEANUP_FAILED)
        raise OrphanActionError("The download no longer exists", 404)
    db.record_cleanup_result(download_id, 0, None)
    row["cleanup_attempts"], row["cleanup_error"] = 0, None
    _clear_failed_finding(db, download_id)
    media_settings = db.get_media_management_settings()
    stats = _Stats()
    lookup = row.get("download_hash") or row["id"]
    try:
        pair = _client_driver(db, row.get("client_id"))
        if pair is None:
            raise OrphanActionError("The download client no longer exists", 404)
        driver, cfg = pair
        status_dict = driver.get_status(lookup)
    except _CLIENT_ERRORS as exc:
        raise OrphanActionError(f"Could not reach the download client: {redact_text(str(exc))}", 502) from exc
    outcome = _apply(
        db, driver, str(cfg.get("driver_type") or ""), row, lookup, status_dict, media_settings, stats, datetime.now(timezone.utc)
    )
    fresh = db.get_active_download(download_id) or row
    return {
        "retried": True,
        "removed": outcome.removed,
        "status": fresh.get("status"),
        "attempts": int(fresh.get("cleanup_attempts") or 0),
        "error": outcome.error,
    }


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


class SeedCleanupWorker:
    """Daemon thread: one sweep ~10 minutes after start, then one per interval (daily). Stop-event based."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running = False

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    def start(
        self,
        db: Any,
        config: Any = None,
        interval_seconds: float = WORKER_INTERVAL_SECONDS,
        initial_delay: float = WORKER_INITIAL_DELAY_SECONDS,
        interval_fn: Optional[Callable[[], float]] = None,
    ) -> bool:
        with self._lock:
            if self._is_running:
                return False
            self._stop_event.clear()
            self._is_running = True
        cycle_interval = interval_fn or (lambda: float(interval_seconds))

        def _loop() -> None:
            if not self._stop_event.wait(initial_delay):
                while not self._stop_event.is_set():
                    self.run_once(db)
                    if wait_for_next_cycle(self._stop_event, cycle_interval):
                        break
            with self._lock:
                self._is_running = False

        self._thread = threading.Thread(target=_loop, daemon=True, name="SeedCleanupWorkerThread")
        self._thread.start()
        return True

    def run_once(self, db: Any) -> bool:
        """One scheduled sweep. True when it ran; False when busy or failed (the cause is logged)."""
        try:
            run_sweep(db, trigger=TRIGGER_SCHEDULED)
            return True
        except SeedCleanupBusy:
            return False
        except Exception as exc:  # noqa: BLE001 - the schedule loop must survive any one failure; the cause is logged
            logger.error("SeedCleanupWorker: sweep failed: %s", safe_exc(exc))
            logger.debug("SeedCleanupWorker traceback", exc_info=True)
            return False

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=5.0)
        with self._lock:
            self._is_running = False


seed_cleanup_worker = SeedCleanupWorker()

__all__ = [
    "MAX_ATTEMPTS",
    "OrphanActionError",
    "SeedCleanupBusy",
    "SeedCleanupWorker",
    "deletion_safe",
    "get_status",
    "is_running",
    "remove_orphan",
    "retry_failed",
    "run_sweep",
    "seed_cleanup_worker",
    "start_sweep_async",
]

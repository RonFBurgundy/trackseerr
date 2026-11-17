"""Scheduled library self-heal task: runs safe catalog repairs and applies safe renames."""

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from trackseerr.api.routes.library.tagging import apply_renames, safe_rename_file_ids
from trackseerr.library_manager import get_library_mode
from trackseerr.media_servers import get_media_server
from trackseerr.redaction import redact_text, safe_exc
from trackseerr.task_manager import (
    TRIGGER_SCHEDULED,
    is_cancellable,
    is_paused,
    record_task_run,
    wait_for_next_cycle,
)

logger = logging.getLogger(__name__)

WORKER_INTERVAL_SECONDS = 7 * 86400
WORKER_INITIAL_DELAY_SECONDS = 30 * 60
WORKER_WAKE_SECONDS = 3600.0


def run_self_heal(db: Any, config: Any, run: Any) -> None:
    """Runs catalog repair + release matching, then applies safe library-wide renames (never retags)."""
    from trackseerr.artist_repair import run_repair_and_match

    run_repair_and_match(db, run)
    if run.should_stop():
        return

    if get_library_mode(db) == "lidarr":
        run.message = f"{run.message or ''}; renames: skipped (Lidarr mode)"
        return

    ids = safe_rename_file_ids(db)
    media_server = get_media_server(config) if config is not None else None
    res = apply_renames(db, ids, media_server)
    renamed = int(res.get("renamed_count", 0))
    blocked = int(res.get("blocked_count", 0))
    errors = list(res.get("errors") or [])
    run.message = (
        f"{run.message or ''}; renames: renamed={renamed} blocked={blocked} errors={len(errors)}"
    )
    for err in errors[:5]:
        logger.warning("LibrarySelfHeal rename error: %s", redact_text(str(err)))


def _task_running(db: Any, task_id: str) -> bool:
    """True when a manual, startup or scheduled run of ``task_id`` is currently active."""
    if is_cancellable(task_id):
        return True
    try:
        from trackseerr.api.routes.system.tasks import _running_tasks

        if task_id in _running_tasks:
            return True
    except ImportError as exc:
        logger.debug("Could not import _running_tasks: %s", safe_exc(exc))
    latest_fn = getattr(db, "latest_task_runs", None)
    if callable(latest_fn):
        try:
            row = latest_fn().get(task_id)
            if isinstance(row, dict) and not row.get("finished_at"):
                return True
        except sqlite3.Error as exc:
            logger.warning("Could not check running status of %s: %s", task_id, safe_exc(exc))
    return False


def _seconds_since(finished_raw: str) -> Optional[float]:
    """Seconds elapsed since an ISO timestamp, or None when empty or unparseable."""
    if not isinstance(finished_raw, str) or not finished_raw:
        return None
    try:
        finished = datetime.fromisoformat(finished_raw)
    except ValueError:
        logger.debug("LibrarySelfHealWorker: unparseable finished_at %r", finished_raw)
        return None
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - finished).total_seconds()


class LibrarySelfHealWorker:
    """Daemon thread: runs library self-heal on a configurable interval after an initial 30-minute delay."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running = False
        self._active_run = False

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    def has_active_run(self) -> bool:
        with self._lock:
            return self._active_run

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
                    self.run_if_due(db, config, cycle_interval())
                    if wait_for_next_cycle(
                        self._stop_event, lambda: min(WORKER_WAKE_SECONDS, float(cycle_interval()))
                    ):
                        break
            with self._lock:
                self._is_running = False

        self._thread = threading.Thread(target=_loop, daemon=True, name="LibrarySelfHealWorkerThread")
        self._thread.start()
        return True

    def run_if_due(
        self, db: Any, config: Any = None, interval_seconds: float = WORKER_INTERVAL_SECONDS
    ) -> bool:
        if is_paused(db, "library_self_heal"):
            logger.debug("LibrarySelfHealWorker: skipping run while library_self_heal is paused")
            return False
        try:
            row = db.latest_task_runs().get("library_self_heal")
        except sqlite3.Error as exc:
            logger.warning("Could not read latest library_self_heal run: %s", safe_exc(exc))
            row = None
        # An unfinished row is a run in flight; rows orphaned by a restart are closed at startup
        # (task_manager.startup_housekeeping), so an unfinished row never blocks forever.
        if isinstance(row, dict) and not row.get("finished_at"):
            logger.debug("LibrarySelfHealWorker: skipping run while a library_self_heal run is unfinished")
            return False
        finished_raw = row.get("finished_at") if isinstance(row, dict) else ""
        elapsed = _seconds_since(finished_raw if isinstance(finished_raw, str) else "")
        if elapsed is not None and elapsed < interval_seconds:
            logger.debug(
                "LibrarySelfHealWorker: skipping run (last finished %.0fs ago, interval %.0fs)",
                elapsed,
                interval_seconds,
            )
            return False
        return self.run_once(db, config)

    def run_once(self, db: Any, config: Any = None) -> bool:
        for other in ("library_self_heal", "mb_release_match"):
            if _task_running(db, other):
                logger.info("LibrarySelfHealWorker: skipping run while %s is active", other)
                return False
        with self._lock:
            if self._active_run:
                logger.info("LibrarySelfHealWorker: skipping run while another run is active")
                return False
            self._active_run = True
        try:
            with record_task_run(db, "library_self_heal", TRIGGER_SCHEDULED) as run:
                run_self_heal(db, config, run)
            return True
        except (sqlite3.Error, OSError) as exc:
            logger.warning("LibrarySelfHealWorker: self-heal failed: %s", safe_exc(exc))
            return False
        finally:
            with self._lock:
                self._active_run = False

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=5.0)
        with self._lock:
            self._is_running = False
            self._active_run = False


library_self_heal_worker = LibrarySelfHealWorker()

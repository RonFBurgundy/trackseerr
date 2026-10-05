"""Pending-release tick: grabs releases whose delay window has elapsed through the normal grab path."""

from __future__ import annotations

from datetime import datetime
import logging
import sqlite3
import threading
import time
from typing import Any, Optional

from plex_playlist_sync import delay_gate
from plex_playlist_sync.acquisition_coordinator import acquisition_coordinator
from plex_playlist_sync.library_manager import MODE_NATIVE, ModeChanged, work_guard
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)


def grab_pending(db: Database, row: dict[str, Any]) -> dict[str, Any]:
    """Grabs one pending row now (no delay gate). The row is removed only when the grab succeeded."""
    payload = row.get("payload") or {}
    candidate = delay_gate.candidate_from_dict(payload.get("candidate") or {})
    with work_guard(db, MODE_NATIVE):
        result = acquisition_coordinator.grab_candidate(
            db,
            candidate,
            parsed_quality=row.get("quality"),
            score=int(payload.get("score") or 0),
            artist=row.get("artist_name") or candidate.artist,
            album=row.get("album"),
            item_type=row.get("item_type") or "track",
            request_id=row.get("request_id"),
            track_id=row.get("track_id"),
            album_id=row.get("album_id"),
            upgrade=payload.get("upgrade_floor") is not None,
        )
    if result.get("success"):
        db.delete_pending_release(row["id"])
    return result


def release_due(db: Database, now: Optional[datetime] = None) -> dict[str, int]:
    """One tick: grab every pending release whose ``release_at`` has passed. Failures stay queued for the next tick."""
    now = now or delay_gate.utcnow()
    due = db.list_pending_releases(due_before=delay_gate._iso(now))
    released = failed = 0
    for row in due:
        try:
            result = grab_pending(db, row)
        except ModeChanged:
            logger.debug("Pending release tick: library manager is Lidarr; leaving %d item(s) queued", len(due))
            break
        except (sqlite3.Error, ValueError, OSError) as exc:
            logger.error("Pending release %s ('%s') failed: %s", row["id"], row["title"], exc)
            failed += 1
            continue
        if result.get("success"):
            released += 1
        else:
            failed += 1
            logger.warning(
                "Pending release %s ('%s') could not be grabbed yet: %s", row["id"], row["title"], result.get("message")
            )
    return {"due": len(due), "released": released, "failed": failed}


class PendingReleaseWorker:
    """Daemon thread that runs ``release_due`` on a short interval."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.released = 0
        self.errors = 0

    def start(self, db: Database, interval_seconds: int = 60) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()

            def _loop() -> None:
                while not self._stop_event.is_set():
                    try:
                        stats = release_due(db)
                        self.released += stats["released"]
                    except sqlite3.Error as exc:
                        self.errors += 1
                        logger.error("PendingReleaseWorker tick failed: %s", exc)
                    except Exception:  # keep the loop alive; logged with traceback
                        self.errors += 1
                        logger.exception("Unexpected error in PendingReleaseWorker tick")
                    slept = 0.0
                    while slept < interval_seconds and not self._stop_event.is_set():
                        time.sleep(min(1.0, interval_seconds - slept))
                        slept += 1.0

            self._thread = threading.Thread(target=_loop, daemon=True, name="PendingReleaseWorkerThread")
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)


pending_worker = PendingReleaseWorker()

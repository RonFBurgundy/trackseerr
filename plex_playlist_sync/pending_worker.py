"""Pending-release tick: grabs releases whose delay window has elapsed through the normal grab path."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import sqlite3
import threading
import time
from typing import Any, Optional

from plex_playlist_sync import delay_gate
from plex_playlist_sync.acquisition_coordinator import _extract_info_hash, acquisition_coordinator
from plex_playlist_sync.library_manager import MODE_NATIVE, ModeChanged, work_guard
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)


MAX_ATTEMPTS = 5
MAX_BACKOFF_MIN = 30
MIN_TTL = timedelta(hours=24)
_DEAD_REQUEST_STATUSES = frozenset({"rejected", "denied", "cancelled", "canceled", "available", "fulfilled"})
_LIVE_DOWNLOAD_STATUSES = ["queued", "downloading", "completed", "importing"]


def backoff_minutes(attempts: int) -> int:
    """1, 2, 4, ... minutes after the 1st, 2nd, 3rd failure, capped at ``MAX_BACKOFF_MIN``."""
    return min(2 ** max(0, attempts - 1), MAX_BACKOFF_MIN)


def _drop(db: Database, row: dict[str, Any], reason: str, *, record_event: bool = False) -> bool:
    """Claims then discards a row. Never logs ``payload_json`` (download URLs can embed indexer API keys)."""
    claimed = db.claim_pending_release(row["id"])
    if claimed is None:
        return False
    logger.info(
        "Dropped pending release %s ('%s' by '%s'): %s", row["id"], row.get("title"), row.get("artist_name"), reason
    )
    if record_event:
        try:
            db.record_event(
                "pending_release_dropped",
                f"Dropped delayed release '{row.get('title')}': {reason}",
                source="PendingReleaseWorker",
                severity="warning",
                details={
                    "pending_id": row["id"],
                    "title": row.get("title"),
                    "artist": row.get("artist_name"),
                    "request_id": row.get("request_id"),
                    "reason": reason,
                },
            )
        except sqlite3.Error as exc:
            logger.warning("Could not record the pending-release drop event: %s", type(exc).__name__)
    return True


def stale_reason(db: Database, row: dict[str, Any], now: datetime) -> Optional[str]:
    """Why a due row must be dropped instead of grabbed (None when it is still wanted)."""
    try:
        added = delay_gate.parse_ts(row["added_at"])
        release = delay_gate.parse_ts(row["release_at"])
        if now > release + max(MIN_TTL, 2 * (release - added)):
            return "expired (more than max(24h, 2x delay) past release time)"
    except (KeyError, TypeError, ValueError):
        pass  # unreadable timestamps: let the grab path decide

    payload = row.get("payload") or {}
    request_id = row.get("request_id")
    if request_id:
        req = db.get_request(str(request_id))
        if req is None:
            return "request no longer exists"
        if str(req.get("status") or "").lower() in _DEAD_REQUEST_STATUSES:
            return f"request is {req.get('status')}"

    upgrade = payload.get("upgrade_floor") is not None
    if not upgrade:
        item_title = payload.get("item_title") or ""
        if item_title and db.library_item_has_file(
            row.get("artist_name") or "", item_title, row.get("album"), row.get("item_type") or "track"
        ):
            return "item already has a file in the library"

    release_title = str(row.get("title") or "").strip().casefold()
    cand = payload.get("candidate") or {}
    for dl in db.list_active_downloads(_LIVE_DOWNLOAD_STATUSES):
        if (request_id and dl.get("request_id") == request_id) or (
            release_title and str(dl.get("title") or "").strip().casefold() == release_title
        ):
            return "item already has an active download"

    try:
        candidate = delay_gate.candidate_from_dict(cand)
        if db.is_blocklisted(
            release_title=candidate.title or None,
            release_guid=candidate.download_id or None,
            info_hash=_extract_info_hash(candidate),
        ):
            return "release is blocklisted"
    except (TypeError, ValueError) as exc:
        logger.warning("Could not check the blocklist for pending release %s: %s", row.get("id"), exc)
    return None


def grab_pending(db: Database, row: dict[str, Any], *, count_failure: bool = True) -> dict[str, Any]:
    """Claims one pending row (atomic delete) and grabs it now, skipping the delay gate.

    A row another path already claimed yields ``{"success": False, "claimed_elsewhere": True}``. When the grab fails
    the row is put back; with ``count_failure`` its attempt counter and backoff advance, and after ``MAX_ATTEMPTS``
    failures it is dropped for good (with a system event).
    """
    claimed = db.claim_pending_release(row["id"])
    if claimed is None:
        return {"success": False, "claimed_elsewhere": True, "message": "Pending release was already grabbed or removed"}
    payload = claimed.get("payload") or {}
    candidate = delay_gate.candidate_from_dict(payload.get("candidate") or {})
    failed = False
    result: dict[str, Any] = {}
    try:
        with work_guard(db, MODE_NATIVE):
            result = acquisition_coordinator.grab_candidate(
                db,
                candidate,
                parsed_quality=claimed.get("quality"),
                score=int(payload.get("score") or 0),
                artist=claimed.get("artist_name") or candidate.artist,
                album=claimed.get("album"),
                item_type=claimed.get("item_type") or "track",
                request_id=claimed.get("request_id"),
                track_id=claimed.get("track_id"),
                album_id=claimed.get("album_id"),
                upgrade=payload.get("upgrade_floor") is not None,
            )
        failed = not result.get("success")
        return result
    except ModeChanged:
        raise
    except (sqlite3.Error, ValueError, OSError):
        failed = True
        raise
    finally:
        if failed or not result.get("success"):
            _requeue_failed(db, claimed, count_failure and failed, result.get("message"))


def _requeue_failed(db: Database, claimed: dict[str, Any], count: bool, message: Any) -> None:
    attempts = int(claimed.get("attempts") or 0)
    if not count:
        db.restore_pending_release(claimed)
        return
    attempts += 1
    if attempts >= MAX_ATTEMPTS:
        logger.info(
            "Dropped pending release %s ('%s' by '%s'): failed %d times (last: %s)",
            claimed["id"], claimed.get("title"), claimed.get("artist_name"), attempts, message,
        )
        try:
            db.record_event(
                "pending_release_dropped",
                f"Dropped delayed release '{claimed.get('title')}' after {attempts} failed grabs",
                source="PendingReleaseWorker",
                severity="warning",
                details={
                    "pending_id": claimed["id"],
                    "title": claimed.get("title"),
                    "artist": claimed.get("artist_name"),
                    "request_id": claimed.get("request_id"),
                    "reason": f"repeated failure: {message}",
                    "attempts": attempts,
                },
            )
        except sqlite3.Error as exc:
            logger.warning("Could not record the pending-release drop event: %s", type(exc).__name__)
        return
    nxt = delay_gate._iso(delay_gate.utcnow() + timedelta(minutes=backoff_minutes(attempts)))
    db.restore_pending_release(claimed, attempts=attempts, next_attempt_at=nxt)


def release_due(db: Database, now: Optional[datetime] = None) -> dict[str, int]:
    """One tick: grab every due pending release after re-validating it. Failures back off; stale rows are dropped."""
    now = now or delay_gate.utcnow()
    due = db.list_pending_releases(due_before=delay_gate._iso(now))
    released = failed = dropped = 0
    for row in due:
        try:
            reason = stale_reason(db, row, now)
            if reason:
                if _drop(db, row, reason):
                    dropped += 1
                continue
            result = grab_pending(db, row)
        except ModeChanged:
            logger.debug("Pending release tick: library manager is Lidarr; leaving %d item(s) queued", len(due))
            break
        except (sqlite3.Error, ValueError, OSError) as exc:
            logger.error("Pending release %s ('%s') failed: %s", row["id"], row["title"], exc)
            failed += 1
            continue
        if result.get("claimed_elsewhere"):
            continue
        if result.get("success"):
            released += 1
        else:
            failed += 1
            logger.warning(
                "Pending release %s ('%s') could not be grabbed yet: %s", row["id"], row["title"], result.get("message")
            )
    return {"due": len(due), "released": released, "failed": failed, "dropped": dropped}


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
                        step = min(1.0, interval_seconds - slept)
                        self._stop_event.wait(step)
                        slept += step

            self._thread = threading.Thread(target=_loop, daemon=True, name="PendingReleaseWorkerThread")
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)


pending_worker = PendingReleaseWorker()

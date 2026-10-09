"""Background trickle worker and queue manager for Lidarr onboarding.

Provides paced, rate-limited onboarding to prevent overloading Lidarr
and the MusicBrainz metadata backend.
"""

import logging
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from trackseerr import lidarr_library
from trackseerr.clients.lidarr import LidarrClient
from trackseerr.job_tracker import track_job
from trackseerr.redaction import redact_text, safe_exc
from trackseerr.storage import Database
from trackseerr.task_manager import TRIGGER_EVENT, record_task_run

logger = logging.getLogger(__name__)

# Upper bound on items queued behind a running trickle; beyond it dispatch is refused and the request stays retryable.
MAX_PENDING_ITEMS = 1000


# Request outcomes worth showing the requester; anything else leaves the request as it was.
_REQUEST_OUTCOME_KINDS = ("not_in_metadata_profile", "monitor_failed", "albums_pending", "rate_limited")
# missing_tracks.lidarr_status values for a failed item (everything not listed is a plain "error"). "unavailable"
# is terminal-ish: the release is not in the user's metadata profile, so it is only re-checked weekly (see
# storage.lidarr_retry_delay); error and rate_limited back off 1h, 6h, 24h and then weekly.
_MISSING_TRACK_STATUS = {
    "not_found": "not_found",
    "not_in_metadata_profile": "unavailable",
    "rate_limited": "rate_limited",
}


class LidarrTrickleWorker:
    """Thread-safe background queue worker for trickling missing tracks to Lidarr."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._is_running: bool = False
        self._is_paused: bool = False
        self._run_trigger: str = TRIGGER_EVENT

        # Queue tracking stats
        self._total_items: int = 0
        self._processed_items: int = 0
        self._successful_items: int = 0
        self._failed_items: int = 0
        self._current_artist: Optional[str] = None
        self._current_album: Optional[str] = None
        self._delay_seconds: float = 3.0
        self._auto_search: bool = True
        self._started_at: Optional[str] = None
        self._last_processed_at: Optional[str] = None
        self._rate_limited_until: float = 0.0
        self._message: str = "Idle"
        # Items handed in while a trickle is running; drained by the worker before it exits.
        self._pending: list[dict[str, Any]] = []

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            cooldown_remaining = max(0, int(self._rate_limited_until - now)) if self._rate_limited_until > now else 0
            return {
                "is_running": self._is_running,
                "is_paused": self._is_paused,
                "total_items": self._total_items,
                "processed_items": self._processed_items,
                "remaining_items": max(0, self._total_items - self._processed_items),
                "successful_items": self._successful_items,
                "failed_items": self._failed_items,
                "current_artist": self._current_artist,
                "current_album": self._current_album,
                "delay_seconds": self._delay_seconds,
                "auto_search": self._auto_search,
                "started_at": self._started_at,
                "last_processed_at": self._last_processed_at,
                "is_rate_limited": cooldown_remaining > 0,
                "rate_limit_seconds_remaining": cooldown_remaining,
                "message": self._message,
            }

    def start_trickle(
        self,
        items: list[dict[str, Any]],
        client: LidarrClient,
        db: Database,
        delay_seconds: float = 3.0,
        auto_search: bool = True,
        batch_size: Optional[int] = None,
        queue_if_running: bool = False,
        trigger: str = TRIGGER_EVENT,
    ) -> dict[str, Any]:
        """Enqueues items and starts background trickle worker thread. ``trigger`` labels the run in the task
        history: ``scheduled`` (auto-trickle loop), ``manual`` (admin) or ``event`` (a request dispatch).

        Refuses unless the library manager is Lidarr, so nothing can reach Lidarr from native mode. With
        ``queue_if_running`` items arriving while a trickle is active are appended to the running worker's pending
        queue (status ``queued``) instead of being rejected; the worker drains it before it exits.
        """
        # Deferred: library_manager imports this module.
        from trackseerr.library_manager import MODE_LIDARR, ModeChanged, acquire_work, release_work

        # The guard is taken before (and never inside) the worker lock so the two locks cannot be ordered both ways.
        try:
            acquire_work(db, MODE_LIDARR)
        except ModeChanged:
            logger.info("Lidarr trickle refused: library manager is TrackSeerr (native mode)")
            return {
                "status": "refused",
                "message": "Library manager is set to TrackSeerr; switch to Lidarr to send items to Lidarr",
                "queued_count": 0,
            }
        owns_guard = True
        try:
            result, owns_guard = self._start_locked(
                items, client, db, delay_seconds, auto_search, batch_size, queue_if_running, trigger
            )
            return result
        finally:
            if owns_guard:
                release_work(MODE_LIDARR)

    def _start_locked(
        self,
        items: list[dict[str, Any]],
        client: LidarrClient,
        db: Database,
        delay_seconds: float,
        auto_search: bool,
        batch_size: Optional[int],
        queue_if_running: bool,
        trigger: str = TRIGGER_EVENT,
    ) -> tuple[dict[str, Any], bool]:
        """Does the start/queue work under the worker lock. Returns ``(result, caller_still_owns_guard)``: the guard
        passes to the worker thread only when one was actually started (it releases it when it exits)."""
        with self._lock:
            if self._is_running:
                if queue_if_running and items:
                    if len(self._pending) + len(items) > MAX_PENDING_ITEMS:
                        logger.warning(
                            "Lidarr trickle pending queue full (%d/%d); refusing %d item(s)",
                            len(self._pending),
                            MAX_PENDING_ITEMS,
                            len(items),
                        )
                        return {
                            "status": "queue_full",
                            "message": (
                                f"Lidarr queue is full ({MAX_PENDING_ITEMS} items waiting); "
                                "retry these requests once it drains"
                            ),
                            "queued_count": 0,
                        }, True
                    self._pending.extend(items)
                    self._total_items += len(items)
                    logger.info("Lidarr trickle running; queued %d more item(s) behind it", len(items))
                    return {
                        "status": "queued",
                        "message": f"Trickle already running; {len(items)} item(s) queued behind it",
                        "queued_count": len(items),
                    }, True
                return {
                    "status": "already_running",
                    "message": "Trickle worker is already running",
                    "queue": self.get_status(),
                }, True

            if batch_size and batch_size > 0:
                items = items[:batch_size]

            if not items:
                return {
                    "status": "empty",
                    "message": "No items to queue",
                    "queued_count": 0,
                }, True

            self._stop_event.clear()
            self._pause_event.clear()
            self._is_running = True
            self._is_paused = False
            self._total_items = len(items)
            self._processed_items = 0
            self._successful_items = 0
            self._failed_items = 0
            self._current_artist = None
            self._current_album = None
            self._delay_seconds = max(0.5, float(delay_seconds))
            self._auto_search = bool(auto_search)
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._last_processed_at = None
            self._rate_limited_until = 0.0
            self._message = f"Enqueued {len(items)} tracks. Starting artist-first trickle..."
            self._pending = []

            artist_groups = self._group_by_artist(items)

            self._run_trigger = trigger
            self._thread = threading.Thread(
                target=self._worker_loop,
                args=(artist_groups, client, db),
                name="LidarrTrickleWorkerThread",
                daemon=True,
            )
            try:
                self._thread.start()
            except RuntimeError:
                self._is_running = False
                logger.error("Could not start the Lidarr trickle worker thread")
                raise

            return {
                "status": "started",
                "message": f"Started Lidarr trickle worker for {len(items)} tracks ({len(artist_groups)} unique artists)",
                "queued_count": len(items),
                "artist_count": len(artist_groups),
                "delay_seconds": self._delay_seconds,
                "auto_search": self._auto_search,
            }, False

    @staticmethod
    def _group_by_artist(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Groups tracks by artist to consolidate API queries."""
        artist_groups: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            artist_key = (item.get("artist") or "").strip().lower()
            if not artist_key:
                continue
            artist_groups.setdefault(artist_key, []).append(item)
        return artist_groups

    def pause(self) -> dict[str, Any]:
        with self._lock:
            if not self._is_running:
                return {"status": "not_running", "message": "Worker is not currently active"}
            self._is_paused = True
            self._pause_event.set()
            self._message = "Paused"
            logger.info("Lidarr trickle worker paused by user")
            return {"status": "paused", "message": "Worker paused"}

    def resume(self) -> dict[str, Any]:
        with self._lock:
            if not self._is_running:
                return {"status": "not_running", "message": "Worker is not currently active"}
            self._is_paused = False
            self._pause_event.clear()
            self._message = "Resumed"
            logger.info("Lidarr trickle worker resumed by user")
            return {"status": "resumed", "message": "Worker resumed"}

    def cancel(self) -> dict[str, Any]:
        with self._lock:
            if not self._is_running:
                return {"status": "not_running", "message": "Worker is not currently active"}
            self._stop_event.set()
            self._is_paused = False
            self._pause_event.clear()
            self._message = "Canceled by user"
            logger.info("Lidarr trickle worker cancellation requested")
            return {"status": "canceling", "message": "Worker is stopping"}

    def _worker_loop(
        self,
        artist_groups: dict[str, list[dict[str, Any]]],
        client: LidarrClient,
        db: Database,
    ) -> None:
        logger.info(
            "Lidarr trickle worker started for %d artists (pacing: %.1fs, auto_search=%s)",
            len(artist_groups),
            self._delay_seconds,
            self._auto_search,
        )

        failure: Optional[str] = None
        try:
            with record_task_run(db, "lidarr_auto_trickle", self._run_trigger) as run, track_job(
                "lidarr_auto_trickle", "Lidarr Trickle Worker"
            ) as job:
                groups = artist_groups
                while True:
                    self._process_groups(groups, client, db)
                    with self._lock:
                        if self._stop_event.is_set() or not self._pending:
                            # Flip the flag in the same lock hold as the empty check so a concurrent
                            # start_trickle either sees a running worker (and its items get drained) or a stopped one.
                            self._is_running = False
                            break
                        more, self._pending = self._pending, []
                    groups = self._group_by_artist(more)
                job.cancelled = run.cancelled = self._stop_event.is_set()
                job.message = run.message = (
                    f"{self._successful_items} monitored, {self._failed_items} failed/missing "
                    f"of {self._total_items}"
                )
        except Exception as e:  # worker thread boundary: log the cause, surface it in the status, never crash the thread
            logger.error("Unexpected error in Lidarr trickle worker loop: %s", safe_exc(e))
            logger.debug("Lidarr trickle worker traceback", exc_info=True)
            failure = f"Error: {safe_exc(e)}"
            with self._lock:
                self._message = failure
        finally:
            stranded: list[str] = []
            with self._lock:
                self._is_running = False
                self._is_paused = False
                if self._pending:
                    stranded = [str(it.get("id")) for it in self._pending if it.get("is_request")]
                    logger.warning(
                        "Lidarr trickle ended with %d queued item(s) unsent (requests %s); retry them to resend",
                        len(self._pending),
                        ", ".join(stranded) or "none",
                    )
                    self._pending = []
                self._current_artist = None
                self._current_album = None
                if failure is not None:
                    self._message = failure
                elif self._stop_event.is_set():
                    self._message = f"Canceled ({self._processed_items}/{self._total_items} processed)"
                else:
                    self._message = f"Completed ({self._successful_items} monitored, {self._failed_items} failed/missing)"
                final_message = self._message
            logger.info("Lidarr trickle worker finished. %s", final_message)
            # Outside the worker lock: the DB write and the guard lock must never nest under it.
            if stranded:
                self._record_stranded(db, stranded)
            from trackseerr.library_manager import MODE_LIDARR, release_work  # deferred: circular import

            release_work(MODE_LIDARR)

    @staticmethod
    def _record_stranded(db: Database, request_ids: list[str]) -> None:
        """One event per batch of requests that were queued behind the trickle but never sent to Lidarr."""
        try:
            db.record_event(
                "lidarr_requests_unsent",
                f"{len(request_ids)} requests not sent to Lidarr; retry them from Requests",
                source="Lidarr Trickle",
                severity="warning",
                details={"request_ids": request_ids[:100], "count": len(request_ids)},
            )
        except (sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
            logger.warning("Could not record unsent-request event: %s", safe_exc(exc))

    def _process_groups(  # noqa: C901, PLR0915
        self,
        artist_groups: dict[str, list[dict[str, Any]]],
        client: LidarrClient,
        db: Database,
    ) -> None:
        for artist_key, group in artist_groups.items():
            if self._stop_event.is_set():
                logger.info("Lidarr trickle worker received stop signal")
                break

            # Handle pause
            while self._is_paused and not self._stop_event.is_set():
                time.sleep(0.5)

            if self._stop_event.is_set():
                break

            # Handle rate-limit cooldown
            now = time.time()
            if self._rate_limited_until > now:
                wait_time = self._rate_limited_until - now
                logger.warning("Lidarr trickle cooling down for %.1fs due to rate limits...", wait_time)
                with self._lock:
                    self._message = f"Rate limited: cooling down for {int(wait_time)}s"
                while time.time() < self._rate_limited_until and not self._stop_event.is_set():
                    time.sleep(1.0)
                if self._stop_event.is_set():
                    break

            first_item = group[0]
            artist_display = first_item.get("artist", "").strip()
            albums = list(dict.fromkeys(
                (it.get("album") or "").strip()
                for it in group
                if (it.get("album") or "").strip()
            ))

            with self._lock:
                self._current_artist = artist_display
                self._current_album = albums[0] if albums else None
                self._message = f"Processing artist: {artist_display} ({len(group)} track(s))"

            # One want per item: Lidarr's own settings decide how a new artist is added, and only the release each
            # item needs is monitored (an existing artist is never modified).
            wants = [
                {
                    "album": (it.get("album") or "").strip(),
                    "title": (it.get("title") or "").strip(),
                    "item_type": it.get("item_type") or "track",
                }
                for it in group
            ]
            res = client.add_artist_and_albums(
                artist_name=artist_display,
                album_names=albums,
                auto_search=self._auto_search,
                wants=wants,
            )

            # Check if rate-limited
            if res.get("status") == "rate_limited":
                retry_after = res.get("retry_after", 60)
                logger.warning(
                    "Rate limited by Lidarr/MusicBrainz for artist '%s'. Pausing for %ds",
                    artist_display,
                    retry_after,
                )
                with self._lock:
                    self._rate_limited_until = time.time() + retry_after
                    self._message = f"Rate limited on '{artist_display}' - cooling down for {retry_after}s"

                # Sleep through cooldown
                while time.time() < self._rate_limited_until and not self._stop_event.is_set():
                    time.sleep(1.0)

                if self._stop_event.is_set():
                    break

                # Retry once after cooldown
                res = client.add_artist_and_albums(
                    artist_name=artist_display,
                    album_names=albums,
                    auto_search=self._auto_search,
                    wants=wants,
                )

            lidarr_library.invalidate()  # Lidarr's artist/album lists changed (or may have): drop cached copies

            # Update database statuses for tracks / requests in this group, one outcome per item when Lidarr gave them
            outcomes = res.get("outcomes")
            if not (isinstance(outcomes, list) and len(outcomes) == len(group)):
                overall = str(res.get("status"))
                if overall == "success":
                    item_outcome = {"status": "monitored", "message": ""}
                elif overall == "not_found":
                    item_outcome = {"status": "not_found", "message": ""}
                elif overall in ("rate_limited", "not_in_metadata_profile", "albums_pending", "monitor_failed"):
                    item_outcome = {"status": overall, "message": str(res.get("message") or "")}
                else:
                    item_outcome = {"status": "error", "message": str(res.get("message") or "")}
                outcomes = [item_outcome for _ in group]

            monitored_count = 0
            failed_count = 0
            for it, outcome in zip(group, outcomes):
                item_id = it.get("id")
                kind = str(outcome.get("status"))
                is_request = bool(it.get("is_request"))
                ok = kind == "monitored"
                monitored_count += 1 if ok else 0
                failed_count += 0 if ok else 1
                if not item_id:
                    continue
                try:
                    if is_request:
                        if ok:
                            db.update_request_status(str(item_id), "processing")
                        elif kind in _REQUEST_OUTCOME_KINDS:
                            retry_after = res.get("retry_after") if kind == "rate_limited" else None
                            db.set_request_outcome(
                                str(item_id),
                                kind,
                                str(outcome.get("message") or "") or None,
                                retry_after=float(retry_after) if isinstance(retry_after, (int, float)) else None,
                            )
                    elif isinstance(item_id, int) or (isinstance(item_id, str) and item_id.isdigit()):
                        db.update_missing_tracks_lidarr_status_bulk(
                            [int(item_id)], "monitored" if ok else _MISSING_TRACK_STATUS.get(kind, "error")
                        )
                except sqlite3.Error as exc:
                    logger.warning("Could not record the Lidarr outcome for %s: %s", item_id, safe_exc(exc))
            with self._lock:
                self._successful_items += monitored_count
                self._failed_items += failed_count
            if res.get("status") == "success":
                logger.info("Monitored %d item(s) for artist '%s' in Lidarr", monitored_count, artist_display)
            elif res.get("status") == "not_found":
                logger.warning("Artist '%s' not found in Lidarr/MusicBrainz", artist_display)
            else:
                logger.error(
                    "Could not monitor %d item(s) for artist '%s': %s",
                    failed_count,
                    artist_display,
                    redact_text(str(res.get("message"))),
                )

            with self._lock:
                self._processed_items += len(group)
                self._last_processed_at = datetime.now(timezone.utc).isoformat()

            # Pacing delay with gentle jitter to prevent lockstep API hammering
            jitter = random.uniform(0.1, 0.4)
            sleep_duration = self._delay_seconds + jitter
            time.sleep(sleep_duration)


# Global singleton worker instance
lidarr_worker = LidarrTrickleWorker()

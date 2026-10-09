"""Autonomous Wanted Backlog Search & Indexer RSS Sync Workers.

- WantedBacklogWorker: Periodically sweeps unfulfilled requests and missing playlist
  tracks against all enabled indexers with pacing delays to protect API rate limits.
- RSSSyncWorker: Periodically polls indexer recent release feeds (RSS/Torznab) and
  snatches releases that fulfill monitored requests.
"""

from difflib import SequenceMatcher
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import sqlite3
import threading
import time
from typing import Any, Callable, Optional
import uuid

from trackseerr import delay_gate
from trackseerr.acquisition_coordinator import (
    acquisition_coordinator,
    candidate_rank,
    _to_quality_profile,
)
from trackseerr.clients.acquisition import (
    get_acquisition_driver,
    get_indexer_driver,
)
from trackseerr.item_history import (
    TRIGGER_ISSUE,
    TRIGGER_RSS,
    TRIGGER_UPGRADE,
    TRIGGER_WANTED,
    GrabTrigger,
)
from trackseerr.models import (
    AcquisitionSearchResult,
    ActiveDownload,
    DownloadStatus,
    NotificationEvent,
    QualityProfile,
    RequestStatus,
)
from trackseerr.notifications import notification_dispatcher
from trackseerr.playlist_policy import creator_may_auto_acquire
from trackseerr.decision_engine import prepare_profile, upgrade_floor
from trackseerr.quality import evaluate_release, parse_release_title
from trackseerr.redaction import redact_text
from trackseerr.job_tracker import tracked
from trackseerr.task_manager import TRIGGER_SCHEDULED, TRIGGER_STARTUP, record_task_run, wait_for_next_cycle
from trackseerr.library_manager import MODE_NATIVE, ModeChanged, work_guard
from trackseerr.seed_rules import apply_seed_rules_at_grab
from trackseerr.storage import Database

logger = logging.getLogger(__name__)


def effective_playlist_modes(db: Database) -> dict[str, str]:
    """Playlist id -> the monitor mode the backlog should honour.

    A playlist whose creator may not auto-request playlist tracks (a non-admin without ``AUTO_REQUEST_PLAYLISTS``) is
    list-only (``none``) here. The stored mode is untouched, so granting the permission again restores it.
    """
    modes: dict[str, str] = {}
    creators: dict[str, bool] = {}
    blocked = 0
    for p in db.list_playlists():
        mode = str(p.get("monitor_mode") or "track")
        if mode != "none" and not creator_may_auto_acquire(db, p, creators):
            mode = "none"
            blocked += 1
        modes[str(p["id"])] = mode
    if blocked:
        logger.info(
            "WantedBacklogWorker: %d playlist(s) are list-only because their creator may not auto-request playlist tracks",
            blocked,
        )
    return modes


def _cached_artist_tags(db: Database, cache: dict[str, list[str]], artist_name: Optional[str]) -> list[str]:
    """Artist tag labels by name, looked up once per artist per ``cache`` (one cache per sweep)."""
    key = (artist_name or "").strip().lower()
    if not key:
        return []
    if key not in cache:
        cache[key] = delay_gate.artist_tags(db, artist_name)
    return cache[key]


def _current_floor(
    db: Database,
    prof: QualityProfile,
    current_quality: str,
    *,
    request_id: Optional[str] = None,
    track_id: Optional[str] = None,
    album_id: Optional[str] = None,
    artist_tags: Optional[list[str]] = None,
) -> int:
    """Upgrade floor for the file currently held. The current file is scored from the release title it was imported
    from (download history) so its format score is real; when that title is unknown (or no longer matches the stored
    quality) only a better quality tier counts as an upgrade, so a same-tier candidate is never re-grabbed in a loop.
    ``artist_tags`` scope tag-restricted release profiles exactly as they do for the candidate being compared."""
    bare = parse_release_title(current_quality)
    if bare.quality == "Unknown":
        bare.quality = current_quality
    title = db.get_imported_release_title(request_id=request_id, track_id=track_id, album_id=album_id)
    if title:
        titled = parse_release_title(title)
        if titled.quality in {current_quality, bare.quality}:
            return upgrade_floor(evaluate_release(titled, prof, artist_tags=artist_tags), prof)
    return upgrade_floor(evaluate_release(bare, prof, artist_tags=artist_tags), prof, title_known=False)


def _matches_request(candidate: AcquisitionSearchResult, req: dict[str, Any]) -> bool:
    """Evaluates whether an indexer release matches a requested artist and title/album."""
    cand_raw = (candidate.title or "").lower()
    cand_artist = (candidate.artist or "").lower().strip()
    cand_album = (candidate.album or "").lower().strip()

    req_artist = (req.get("artist") or "").lower().strip()
    req_title = (req.get("title") or "").lower().strip()
    req_album = (req.get("album") or "").lower().strip()

    if not req_artist or not (req_title or req_album):
        return False

    # 1. Direct substring checks against raw candidate release title
    artist_match_raw = req_artist in cand_raw
    title_match_raw = (req_title in cand_raw) or (bool(req_album) and req_album in cand_raw)

    if artist_match_raw and title_match_raw:
        return True

    # 2. Parsed title matching and fuzzy matching via SequenceMatcher
    parsed = parse_release_title(candidate.title)
    p_artist = (parsed.artist or cand_artist).lower().strip()
    p_album = (parsed.album or cand_album).lower().strip()
    p_title = (parsed.title or candidate.title).lower().strip()

    artist_ok = False
    if p_artist and req_artist:
        if req_artist in p_artist or p_artist in req_artist:
            artist_ok = True
        elif SequenceMatcher(None, p_artist, req_artist).ratio() >= 0.8:
            artist_ok = True
    elif artist_match_raw:
        artist_ok = True

    if not artist_ok:
        return False

    title_ok = False
    for cand_text in (p_album, p_title, cand_album):
        if not cand_text:
            continue
        if req_title and (req_title in cand_text or cand_text in req_title):
            title_ok = True
            break
        if req_album and (req_album in cand_text or cand_text in req_album):
            title_ok = True
            break
        if req_title and SequenceMatcher(None, cand_text, req_title).ratio() >= 0.8:
            title_ok = True
            break
        if req_album and SequenceMatcher(None, cand_text, req_album).ratio() >= 0.8:
            title_ok = True
            break

    return artist_ok and (title_ok or title_match_raw)


# A track searched this recently is not searched again by a manual Wanted search.
RECENT_SEARCH_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True)
class ReplacementSpec:
    """An admin-driven replacement search for tracks that already have files (issue fix actions only).

    ``require_better`` keeps the "strictly better than the current file" rule (``audio_quality`` issues); otherwise a
    same-quality release is accepted (``corrupted_file`` / ``wrong_release``). ``issue_id`` is recorded on the grab so
    the import can comment on the issue.
    """

    issue_id: str
    require_better: bool = False


def _search_trigger(
    replacement: Optional[ReplacementSpec], min_score: Optional[int], actor_user_id: Optional[str] = None
) -> GrabTrigger:
    """Why a Wanted search grabs: an issue's replacement, a below-cutoff upgrade, or plain wanted."""
    if replacement is not None:
        return GrabTrigger(TRIGGER_ISSUE, ref=replacement.issue_id, label="Issue replacement", actor_user_id=actor_user_id)
    if min_score is not None:
        return GrabTrigger(TRIGGER_UPGRADE, label="Quality upgrade", actor_user_id=actor_user_id)
    return GrabTrigger(TRIGGER_WANTED, label="Wanted search", actor_user_id=actor_user_id)


def _searched_since(value: Any, cutoff: datetime) -> bool:
    """True when ``value`` (SQLite ``YYYY-MM-DD HH:MM:SS`` UTC or ISO-8601) is at or after ``cutoff``."""
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed >= cutoff


@dataclass
class _SweepCollect:
    """Encapsulates cross-phase state during backlog sweep collection."""

    db: Database
    stats: Optional[dict[str, int]] = None
    tag_cache: dict[str, list[str]] = field(default_factory=dict)
    active_req_ids: set[str] = field(default_factory=set)
    active_track_ids: set[str] = field(default_factory=set)
    active_artist_titles: set[tuple[str, str]] = field(default_factory=set)
    enable_upgrades: bool = True
    library_mode: str = "native"
    items_to_search: list[
        tuple[
            str,
            str,
            Optional[str],
            str,
            Optional[str],
            Optional[int],
            Optional[str],
            Optional[int],
            Optional[str],
            Optional[str],
        ]
    ] = field(default_factory=list)


class WantedBacklogWorker:
    """Autonomous worker periodically re-searching unfulfilled requests and missing tracks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running: bool = False
        self.interval_seconds: int = 3600
        self.pace_delay: float = 2.5
        self.last_run_at: Optional[str] = None
        self.items_checked: int = 0
        self.items_grabbed: int = 0
        self.errors: int = 0
        self.last_search_thread: Optional[threading.Thread] = None
        self._search_lock = threading.Lock()

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._is_running,
                "interval_seconds": self.interval_seconds,
                "pace_delay": self.pace_delay,
                "last_run_at": self.last_run_at,
                "items_checked": self.items_checked,
                "items_grabbed": self.items_grabbed,
                "errors": self.errors,
            }

    def start(
        self,
        db: Database,
        interval_seconds: int = 3600,
        pace_delay: float = 2.5,
        interval_fn: Optional[Callable[[], float]] = None,
    ) -> bool:
        """Starts daemon thread executing periodic backlog sweeps. ``interval_fn`` (the task manager's effective
        interval) is re-read every second while sleeping so a schedule edit applies without a restart."""
        with self._lock:
            if self._is_running:
                logger.warning("WantedBacklogWorker is already running")
                return False

            self.interval_seconds = interval_seconds
            self.pace_delay = pace_delay
            self._stop_event.clear()
            self._is_running = True

            def _worker_loop() -> None:
                logger.info(
                    "WantedBacklogWorker loop started (interval: %ds, pace: %.1fs)",
                    self.interval_seconds,
                    self.pace_delay,
                )
                cycle_interval = interval_fn or (lambda: float(self.interval_seconds))
                trigger = TRIGGER_STARTUP
                while not self._stop_event.is_set():
                    try:
                        with record_task_run(db, "wanted_backlog_sweep", trigger) as run:
                            run.apply_result(self.poll_once(db=db))
                    except Exception as e:
                        logger.exception("Unexpected error in WantedBacklogWorker poll cycle: %s", e)
                        with self._lock:
                            self.errors += 1
                    trigger = TRIGGER_SCHEDULED

                    # Responsive sleep; the interval is re-read every second (schedule edits apply at once)
                    if wait_for_next_cycle(self._stop_event, cycle_interval):
                        break
                    self.interval_seconds = int(cycle_interval())

                with self._lock:
                    self._is_running = False
                logger.info("WantedBacklogWorker loop stopped cleanly")

            self._thread = threading.Thread(
                target=_worker_loop, daemon=True, name="WantedBacklogWorkerThread"
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """Signals worker to stop and waits for completion."""
        with self._lock:
            if not self._is_running:
                return
            self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

        with self._lock:
            self._is_running = False

    @tracked("wanted_backlog_sweep", "Monitored Missing & Upgrade Search Sweep")
    def poll_once(self, db: Database) -> dict[str, int]:
        """Executes a single sweep over unfulfilled requests and missing tracks."""
        with self._lock:
            self.last_run_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        try:
            with work_guard(db, MODE_NATIVE):
                return self._sweep(db)
        except ModeChanged:
            logger.debug("WantedBacklogWorker: library manager is Lidarr; skipping native backlog sweep")
            return {"items_checked": 0, "items_grabbed": 0, "errors": 0, "skipped": "library manager is Lidarr"}

    def search_batch_running(self) -> bool:
        """True while a manual wanted-search batch thread is still working through its targets."""
        thread = self.last_search_thread
        return thread is not None and thread.is_alive()

    def queue_wanted_search(
        self,
        db: Database,
        targets: list[dict[str, Any]],
        replacement: Optional[ReplacementSpec] = None,
        actor_user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Queues indexer searches for Wanted rows; returns ``{"queued": n}`` plus a ``message`` when nothing/less ran.

        Only one manual batch runs at a time: while one is alive a new request queues nothing and says so. Tracks
        searched within ``RECENT_SEARCH_WINDOW`` (``last_searched_at``) are skipped, which also absorbs double clicks;
        a ``replacement`` (deliberate admin action) bypasses that guard for this call only.
        """
        with self._search_lock:
            if self.search_batch_running():
                return {"queued": 0, "message": "A search batch is already running"}
            cutoff = datetime.now(timezone.utc) - RECENT_SEARCH_WINDOW
            fresh: list[dict[str, Any]] = []
            skipped = 0
            for t in targets:
                if replacement is None and _searched_since(t.get("last_searched_at"), cutoff):
                    skipped += 1
                else:
                    fresh.append(t)
            queued = self.search_wanted_tracks(db, fresh, replacement=replacement, actor_user_id=actor_user_id)
        out: dict[str, Any] = {"queued": queued}
        if skipped and queued:
            out["message"] = f"Skipped {skipped} searched in the last 10 minutes"
        elif skipped:
            out["message"] = "All selected items were searched in the last 10 minutes"
        return out

    def search_wanted_tracks(
        self,
        db: Database,
        targets: list[dict[str, Any]],
        replacement: Optional[ReplacementSpec] = None,
        actor_user_id: Optional[str] = None,
    ) -> int:
        """Queues indexer searches for the given Wanted rows and returns how many were queued.

        The searches run on a daemon thread, paced like the periodic sweep; each one goes through
        ``acquisition_coordinator.search_and_grab`` (which takes the native work guard itself). Rows that already
        have a below-cutoff file are searched as upgrades, scored against the current quality. The caller is
        expected to hold the native ``work_guard`` while it decides to queue.

        With a ``replacement`` the tracks are searched even though their files meet the cutoff: same-quality
        releases are accepted unless ``require_better``, the delay profile is bypassed, and the grab is tagged with
        the issue id. The blocklist and quality profile still apply.
        """
        runnable = [t for t in targets if (t.get("artist") or "").strip() and (t.get("title") or "").strip()]
        if not runnable:
            return 0
        db.mark_tracks_searched([str(t["track_id"]) for t in runnable if t.get("track_id")])

        def _run() -> None:
            tag_cache: dict[str, list[str]] = {}
            for t in runnable:
                try:
                    min_score: Optional[int] = None
                    qp_id = t.get("quality_profile_id")
                    if t.get("current_quality") and (
                        (replacement.require_better if replacement is not None else t.get("cutoff_met") == 0)
                    ):
                        profile_dict = db.get_quality_profile(qp_id) if qp_id else db.get_default_quality_profile()
                        min_score = 0
                        if profile_dict:
                            min_score = _current_floor(
                                db,
                                _to_quality_profile(profile_dict),
                                t["current_quality"],
                                track_id=t.get("track_id"),
                                album_id=t.get("album_id"),
                                artist_tags=_cached_artist_tags(db, tag_cache, t.get("artist")),
                            )
                    res = acquisition_coordinator.search_and_grab(
                        artist=str(t["artist"]).strip(),
                        title=str(t["title"]).strip(),
                        album=(str(t["album"]).strip() if t.get("album") else None),
                        item_type="track",
                        db=db,
                        quality_profile_id=qp_id,
                        min_score=min_score,
                        track_id=t.get("track_id"),
                        album_id=t.get("album_id"),
                        bypass_delay=replacement is not None,
                        replacement_issue_id=replacement.issue_id if replacement is not None else None,
                        trigger=_search_trigger(replacement, min_score, actor_user_id),
                    )
                    if res.get("mode_changed"):
                        logger.info("Manual wanted search stopped: library manager switched to Lidarr")
                        return
                except Exception as e:  # one failing search must not abort the rest of the batch
                    logger.error(
                        "Manual wanted search failed for '%s - %s': %s", t.get("artist"), t.get("title"), redact_text(str(e))
                    )
                if self.pace_delay > 0:
                    time.sleep(min(float(self.pace_delay), 5.0))

        thread = threading.Thread(target=_run, daemon=True, name="WantedManualSearchThread")
        self.last_search_thread = thread
        thread.start()
        return len(runnable)

    def _collect_active_downloads(self, ctx: _SweepCollect) -> None:
        """Query active downloads to populate deduplication sets."""
        try:
            active_dls = ctx.db.list_active_downloads(statuses=["queued", "downloading", "importing", "warning"])
        except Exception as e:
            logger.error("WantedBacklogWorker error querying active downloads: %s", e)
            active_dls = []
            if ctx.stats is not None:
                ctx.stats["errors"] += 1

        ctx.active_req_ids = {d["request_id"] for d in active_dls if d.get("request_id")}
        ctx.active_track_ids = {d["track_id"] for d in active_dls if d.get("track_id")}
        ctx.active_artist_titles = {
            ((d.get("artist") or "").strip().lower(), (d.get("title") or "").strip().lower())
            for d in active_dls
        }

    def _collect_unfulfilled_requests(self, ctx: _SweepCollect) -> list[dict[str, Any]]:
        """Query unfulfilled requests and cutoff-unmet requests when upgrades enabled."""
        try:
            requests = ctx.db.list_requests()
        except Exception as e:
            logger.error("WantedBacklogWorker error querying requests: %s", e)
            requests = []
            if ctx.stats is not None:
                ctx.stats["errors"] += 1

        media_settings = ctx.db.get_media_management_settings()
        ctx.enable_upgrades = bool(media_settings.get("enable_quality_upgrades", True))
        ctx.library_mode = media_settings.get("library_mode", "native")

        unfulfilled_requests = [
            r
            for r in requests
            if r.get("status") in ("processing", "pending")
            and r.get("id") not in ctx.active_req_ids
            and (
                (r.get("artist") or "").strip().lower(),
                (r.get("title") or "").strip().lower(),
            )
            not in ctx.active_artist_titles
        ]

        if ctx.enable_upgrades:
            try:
                cutoff_unmet = ctx.db.get_cutoff_unmet_requests()
                for r in cutoff_unmet:
                    if (
                        r.get("id") not in ctx.active_req_ids
                        and (
                            (r.get("artist") or "").strip().lower(),
                            (r.get("title") or "").strip().lower(),
                        )
                        not in ctx.active_artist_titles
                    ):
                        unfulfilled_requests.append(r)
            except Exception as e:
                logger.error("WantedBacklogWorker error querying cutoff unmet requests: %s", e)
                if ctx.stats is not None:
                    ctx.stats["errors"] += 1

        return unfulfilled_requests

    def _collect_unfulfilled_missing(self, ctx: _SweepCollect) -> list[dict[str, Any]]:
        """Query unfulfilled missing playlist tracks respecting playlist monitor modes."""
        try:
            missing_tracks = ctx.db.get_missing_tracks()
        except Exception as e:
            logger.error("WantedBacklogWorker error querying missing tracks: %s", e)
            missing_tracks = []
            if ctx.stats is not None:
                ctx.stats["errors"] += 1

        # A playlist's monitor mode decides whether its missing tracks are searched one by one: "none" never,
        # "album"/"artist" only until the list mode has taken them over (then the monitored album/artist is searched).
        try:
            playlist_modes = effective_playlist_modes(ctx.db)
        except Exception as e:
            logger.error("WantedBacklogWorker error reading playlist monitor modes: %s", e)
            playlist_modes = {}
            if ctx.stats is not None:
                ctx.stats["errors"] += 1

        def _searchable_as_track(t: dict[str, Any]) -> bool:
            mode = playlist_modes.get(str(t.get("playlist_id")), "track")
            if mode == "none":
                return False
            return not (mode in ("album", "artist") and t.get("list_applied_at"))

        unfulfilled_missing = [
            t
            for t in missing_tracks
            if _searchable_as_track(t)
            and t.get("lidarr_status") != "monitored"
            and (
                (t.get("artist") or "").strip().lower(),
                (t.get("title") or "").strip().lower(),
            )
            not in ctx.active_artist_titles
        ]
        return unfulfilled_missing

    def _append_request_items(
        self, ctx: _SweepCollect, unfulfilled_requests: list[dict[str, Any]]
    ) -> None:
        """Convert unfulfilled requests into search tuples and append to items_to_search."""
        for r in unfulfilled_requests:
            is_upgrade = (r.get("status") == "available" or r.get("cutoff_met") == 0)
            min_score = None
            qp_id = r.get("quality_profile_id")
            if is_upgrade:
                profile_dict = ctx.db.get_quality_profile(qp_id) if qp_id else ctx.db.get_default_quality_profile()
                if profile_dict:
                    prof = _to_quality_profile(profile_dict)
                    cur_q = r.get("current_quality")
                    if cur_q:
                        min_score = _current_floor(
                            ctx.db,
                            prof,
                            cur_q,
                            request_id=r.get("id"),
                            artist_tags=_cached_artist_tags(ctx.db, ctx.tag_cache, r.get("artist")),
                        )
                    else:
                        min_score = 0
            ctx.items_to_search.append(
                (
                    r.get("artist", "").strip(),
                    r.get("title", "").strip(),
                    r.get("album", "").strip() if r.get("album") else None,
                    r.get("item_type", "track"),
                    r.get("id"),
                    None,
                    qp_id,
                    min_score,
                    None,
                    None,
                )
            )

    def _append_missing_items(
        self, ctx: _SweepCollect, unfulfilled_missing: list[dict[str, Any]]
    ) -> None:
        """Convert unfulfilled missing tracks into search tuples and append to items_to_search."""
        for t in unfulfilled_missing:
            ctx.items_to_search.append(
                (
                    t.get("artist", "").strip(),
                    t.get("title", "").strip(),
                    t.get("album", "").strip() if t.get("album") else None,
                    "track",
                    None,
                    int(t["id"]),
                    None,
                    None,
                    None,
                    None,
                )
            )

    def _append_native_missing(self, ctx: _SweepCollect) -> None:
        """Query monitored missing native catalog tracks and append to items_to_search."""
        try:
            missing_catalog = ctx.db.get_monitored_missing_catalog_tracks(limit=100)
            for t in missing_catalog:
                t_id = t["track_id"]
                t_pair = (
                    (t.get("artist_name") or "").strip().lower(),
                    (t.get("track_title") or "").strip().lower(),
                )
                if t_id in ctx.active_track_ids or t_pair in ctx.active_artist_titles:
                    continue
                ctx.items_to_search.append(
                    (
                        t.get("artist_name", "").strip(),
                        t.get("track_title", "").strip(),
                        t.get("album_title", "").strip() if t.get("album_title") else None,
                        "track",
                        None,
                        None,
                        t.get("quality_profile_id"),
                        None,
                        t_id,
                        t.get("album_id"),
                    )
                )
        except Exception as e:
            logger.error("WantedBacklogWorker error querying missing catalog tracks: %s", e)
            if ctx.stats is not None:
                ctx.stats["errors"] += 1

    def _append_native_cutoff_unmet(self, ctx: _SweepCollect) -> None:
        """Query cutoff-unmet native catalog tracks and append to items_to_search."""
        try:
            cutoff_unmet_catalog = ctx.db.get_cutoff_unmet_catalog_tracks(limit=100)
            for t in cutoff_unmet_catalog:
                t_id = t["track_id"]
                t_pair = (
                    (t.get("artist_name") or "").strip().lower(),
                    (t.get("track_title") or "").strip().lower(),
                )
                if t_id in ctx.active_track_ids or t_pair in ctx.active_artist_titles:
                    continue

                qp_id = t.get("quality_profile_id")
                profile_dict = ctx.db.get_quality_profile(qp_id) if qp_id else ctx.db.get_default_quality_profile()
                min_score = 0
                if profile_dict:
                    prof = _to_quality_profile(profile_dict)
                    cur_q = t.get("quality_name")
                    if cur_q:
                        min_score = _current_floor(
                            ctx.db,
                            prof,
                            cur_q,
                            track_id=t.get("track_id"),
                            album_id=t.get("album_id"),
                            artist_tags=_cached_artist_tags(ctx.db, ctx.tag_cache, t.get("artist_name")),
                        )

                ctx.items_to_search.append(
                    (
                        t.get("artist_name", "").strip(),
                        t.get("track_title", "").strip(),
                        t.get("album_title", "").strip() if t.get("album_title") else None,
                        "track",
                        None,
                        None,
                        qp_id,
                        min_score,
                        t_id,
                        t.get("album_id"),
                    )
                )
        except Exception as e:
            logger.error("WantedBacklogWorker error querying cutoff unmet catalog tracks: %s", e)
            if ctx.stats is not None:
                ctx.stats["errors"] += 1

    def _collect_sweep_items(
        self, db: Database, stats: Optional[dict[str, int]] = None
    ) -> Optional[list[tuple]]:
        """Collect backlog items to search across requests, missing tracks, and native catalog."""
        ctx = _SweepCollect(db=db, stats=stats)
        self._collect_active_downloads(ctx)
        unfulfilled_requests = self._collect_unfulfilled_requests(ctx)
        unfulfilled_missing = self._collect_unfulfilled_missing(ctx)
        self._append_request_items(ctx, unfulfilled_requests)
        self._append_missing_items(ctx, unfulfilled_missing)
        if ctx.library_mode == "native":
            self._append_native_missing(ctx)
            if ctx.enable_upgrades:
                self._append_native_cutoff_unmet(ctx)
        return ctx.items_to_search

    def _search_sweep_item(
        self, db: Database, item: tuple, stats: dict[str, int]
    ) -> None:
        """Search indexers and grab a release for a single backlog item."""
        (
            artist,
            title,
            album,
            item_type,
            req_id,
            missing_id,
            qp_id,
            min_score,
            track_id,
            album_id,
        ) = item

        stats["items_checked"] += 1
        try:
            res = acquisition_coordinator.search_and_grab(
                artist=artist,
                title=title,
                album=album,
                item_type=item_type,
                request_id=req_id,
                db=db,
                quality_profile_id=qp_id,
                min_score=min_score,
                track_id=track_id,
                album_id=album_id,
                trigger=_search_trigger(None, min_score),
            )
            if track_id:
                db.mark_tracks_searched([str(track_id)])
            if res.get("success"):
                stats["items_grabbed"] += 1
                logger.info(
                    "WantedBacklogWorker grabbed release for '%s - %s' (request_id=%s, missing_id=%s, track_id=%s)",
                    artist,
                    title,
                    req_id,
                    missing_id,
                    track_id,
                )
                if req_id:
                    db.update_request_status(req_id, RequestStatus.PROCESSING)
                if missing_id:
                    db.update_missing_track_lidarr_status(missing_id, "grabbed")
        except Exception as e:
            logger.error("Error during search_and_grab for '%s - %s': %s", artist, title, e)
            stats["errors"] += 1

        # Pacing delay between calls
        if self.pace_delay > 0 and not self._stop_event.is_set():
            slept = 0.0
            while slept < self.pace_delay and not self._stop_event.is_set():
                step = min(0.2, self.pace_delay - slept)
                self._stop_event.wait(step)
                slept += step

    def _sweep(self, db: Database) -> dict[str, int]:
        """Perform a backlog sweep across unfulfilled requests and missing tracks."""
        stats = {
            "items_checked": 0,
            "items_grabbed": 0,
            "errors": 0,
        }
        items = self._collect_sweep_items(db, stats)
        if items is None:
            with self._lock:
                self.items_checked += stats["items_checked"]
                self.items_grabbed += stats["items_grabbed"]
                self.errors += stats["errors"]
            return stats

        for item in items:
            if self._stop_event.is_set():
                logger.info("WantedBacklogWorker sweep interrupted by stop event")
                break

            artist, title = item[0], item[1]
            if not artist or not title:
                continue

            self._search_sweep_item(db, item, stats)

        with self._lock:
            self.items_checked += stats["items_checked"]
            self.items_grabbed += stats["items_grabbed"]
            self.errors += stats["errors"]

        try:
            db.record_event(
                "backlog_sweep",
                f"Backlog sweep completed: {stats['items_checked']} items checked, {stats['items_grabbed']} grabbed",
                source="BacklogWorker",
                severity="info",
            )
        except Exception as ev_err:
            logger.warning("Failed to record backlog_sweep event: %s", ev_err)

        return stats


class RSSSyncWorker:
    """Autonomous worker periodically polling indexer recent release feeds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running: bool = False
        self.interval_seconds: int = 900
        self.last_run_at: Optional[str] = None
        self.releases_scanned: int = 0
        self.grabs_triggered: int = 0
        self.errors: int = 0

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._is_running,
                "interval_seconds": self.interval_seconds,
                "last_run_at": self.last_run_at,
                "releases_scanned": self.releases_scanned,
                "grabs_triggered": self.grabs_triggered,
                "errors": self.errors,
            }

    def start(
        self, db: Database, interval_seconds: int = 900, interval_fn: Optional[Callable[[], float]] = None
    ) -> bool:
        """Starts daemon thread executing periodic RSS polling (``interval_fn``: see WantedBacklogWorker.start)."""
        with self._lock:
            if self._is_running:
                logger.warning("RSSSyncWorker is already running")
                return False

            self.interval_seconds = interval_seconds
            self._stop_event.clear()
            self._is_running = True

            def _worker_loop() -> None:
                logger.info("RSSSyncWorker loop started (interval: %ds)", self.interval_seconds)
                cycle_interval = interval_fn or (lambda: float(self.interval_seconds))
                trigger = TRIGGER_STARTUP
                while not self._stop_event.is_set():
                    try:
                        with record_task_run(db, "indexer_rss_sync", trigger) as run:
                            run.apply_result(self.poll_once(db=db))
                    except Exception as e:
                        logger.exception("Unexpected error in RSSSyncWorker poll cycle: %s", e)
                        with self._lock:
                            self.errors += 1
                    trigger = TRIGGER_SCHEDULED

                    # Responsive sleep; the interval is re-read every second (schedule edits apply at once)
                    if wait_for_next_cycle(self._stop_event, cycle_interval):
                        break
                    self.interval_seconds = int(cycle_interval())

                with self._lock:
                    self._is_running = False
                logger.info("RSSSyncWorker loop stopped cleanly")

            self._thread = threading.Thread(
                target=_worker_loop, daemon=True, name="RSSSyncWorkerThread"
            )
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """Signals worker to stop and waits for completion."""
        with self._lock:
            if not self._is_running:
                return
            self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

        with self._lock:
            self._is_running = False

    @tracked("indexer_rss_sync", "Torznab / Newznab RSS Sync")
    def poll_once(self, db: Database) -> dict[str, int]:
        """Polls indexer recent feeds and triggers grabs for matching requests."""
        with self._lock:
            self.last_run_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        try:
            with work_guard(db, MODE_NATIVE):
                return self._sync(db)
        except ModeChanged:
            logger.debug("RSSSyncWorker: library manager is Lidarr; skipping native RSS sync")
            return {"releases_scanned": 0, "grabs_triggered": 0, "errors": 0, "skipped": "library manager is Lidarr"}

    def _collect_wanted_requests(
        self, db: Database, stats: Optional[dict[str, int]] = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[Any]]:
        """Gather enabled indexers, unfulfilled requests, and active request IDs."""
        # 1. Retrieve enabled indexers
        try:
            indexers = db.list_indexers(enabled_only=True)
        except Exception as e:
            logger.error("RSSSyncWorker error listing enabled indexers: %s", e)
            indexers = []
            if stats is not None:
                stats["errors"] += 1

        # 2. Gather wanted requests without active transfers
        try:
            active_dls = db.list_active_downloads(statuses=["queued", "downloading", "importing", "warning"])
        except Exception as e:
            logger.error("RSSSyncWorker error querying active downloads: %s", e)
            active_dls = []
            if stats is not None:
                stats["errors"] += 1

        active_req_ids = {d["request_id"] for d in active_dls if d.get("request_id")}
        try:
            all_requests = db.list_requests()
        except Exception as e:
            logger.error("RSSSyncWorker error querying requests: %s", e)
            all_requests = []
            if stats is not None:
                stats["errors"] += 1

        media_settings = db.get_media_management_settings()
        enable_upgrades = bool(media_settings.get("enable_quality_upgrades", True))

        wanted_requests = [
            r
            for r in all_requests
            if r.get("status") in ("processing", "pending")
            and r.get("id") not in active_req_ids
        ]

        if enable_upgrades:
            try:
                cutoff_unmet = db.get_cutoff_unmet_requests()
                for r in cutoff_unmet:
                    if r.get("id") not in active_req_ids and r not in wanted_requests:
                        wanted_requests.append(r)
            except Exception as e:
                logger.error("RSSSyncWorker error querying cutoff unmet requests: %s", e)
                if stats is not None:
                    stats["errors"] += 1

        return indexers, wanted_requests, active_req_ids

    def _fetch_recent_releases(
        self, idx_cfg: dict[str, Any], stats: Optional[dict[str, int]] = None
    ) -> Optional[list[Any]]:
        """Fetch recent releases from an enabled indexer driver."""
        try:
            driver = get_indexer_driver(idx_cfg)
            if hasattr(driver, "fetch_recent"):
                return driver.fetch_recent(limit=100)
            logger.debug("Indexer driver '%s' does not implement fetch_recent", idx_cfg.get("name"))
            return None
        except Exception as e:
            logger.warning("Error fetching recent releases from indexer '%s': %s", idx_cfg.get("name"), e)
            if stats is not None:
                stats["errors"] += 1
            return None

    def _match_wanted_request(
        self,
        candidate: Any,
        wanted_requests: list[dict[str, Any]],
        active_req_ids: Optional[set[Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Find the first unfulfilled request matching the RSS candidate release."""
        active_ids = active_req_ids or set()
        for req in wanted_requests:
            if req.get("id") in active_ids:
                continue
            if _matches_request(candidate, req):
                return req
        return None

    def _evaluate_rss_candidate(
        self,
        db: Database,
        candidate: Any,
        matched_req: dict[str, Any],
        default_profile: Optional[Any],
    ) -> Optional[tuple[Optional[Any], Optional[Any], list[str], GrabTrigger]]:
        """Evaluate an RSS candidate against quality profile and cutoff upgrade thresholds."""
        eval_res = None
        req_profile = default_profile
        req_artist_tags: list[str] = []
        if matched_req.get("quality_profile_id"):
            try:
                p_dict = db.get_quality_profile(matched_req["quality_profile_id"])
                if p_dict:
                    req_profile = _to_quality_profile(p_dict)
            except Exception as e:
                logger.warning("Error fetching quality profile for request %s: %s", matched_req["id"], e)

        if req_profile:
            parsed = parse_release_title(candidate.title)
            req_artist_tags = delay_gate.artist_tags(db, matched_req.get("artist") or candidate.artist)
            eval_res = evaluate_release(
                release=parsed,
                profile=req_profile,
                size_bytes=candidate.size_bytes if candidate.size_bytes > 0 else None,
                artist_tags=req_artist_tags,
            )
            if not eval_res.is_acceptable:
                logger.debug(
                    "RSS candidate '%s' rejected by profile for request %s (%s)",
                    candidate.title,
                    matched_req["id"],
                    eval_res.rejection_reasons,
                )
                return None

            # If this is a cutoff-unmet request, verify candidate.score > current_score
            if matched_req.get("cutoff_met") == 0 or matched_req.get("status") == "available":
                current_quality = matched_req.get("current_quality")
                current_score = 0
                if current_quality:
                    current_score = _current_floor(
                        db,
                        req_profile,
                        current_quality,
                        request_id=matched_req.get("id"),
                        artist_tags=req_artist_tags,
                    )

                if eval_res.score <= current_score:
                    logger.debug(
                        "RSS candidate '%s' score %d does not exceed current score %d for upgrade request %s",
                        candidate.title,
                        eval_res.score,
                        current_score,
                        matched_req["id"],
                    )
                    return None

        indexer_name = str((candidate.extra or {}).get("indexer_name") or candidate.source or "") or None
        rss_trigger = GrabTrigger(TRIGGER_RSS, ref=indexer_name, label=indexer_name or "RSS")
        return eval_res, req_profile, req_artist_tags, rss_trigger

    def _delay_gate_rss_candidate(
        self,
        db: Database,
        candidate: Any,
        matched_req: dict[str, Any],
        eval_res: Optional[Any],
        req_profile: Optional[Any],
        req_artist_tags: list[str],
        rss_trigger: GrabTrigger,
    ) -> tuple[bool, Optional[Any]]:
        """Apply delay-gate rules to determine if the candidate can be grabbed immediately."""
        claimed_pending = None
        if eval_res is not None and req_profile is not None:
            delay_profile = delay_gate.resolve_delay_profile(
                db, matched_req.get("artist") or candidate.artist, tags=req_artist_tags
            )
            decision = delay_gate.apply_gate(
                db,
                profile=delay_profile,
                top_tier=delay_gate.highest_allowed_tier(prepare_profile(req_profile)),
                candidate=candidate,
                result=eval_res,
                rank=candidate_rank(candidate, eval_res, delay_profile.get("preferred_protocol")),
                artist=str(matched_req.get("artist") or candidate.artist or ""),
                item_title=str(matched_req.get("title") or ""),
                album=matched_req.get("album"),
                item_type=str(matched_req.get("item_type") or "track"),
                request_id=str(matched_req["id"]),
                album_id=None,
                track_id=None,
                quality_profile_id=matched_req.get("quality_profile_id"),
                trigger=rss_trigger,
            )
            if not decision.grab:
                logger.info(
                    "RSS held '%s' for request %s: %s", candidate.title, matched_req["id"], decision.reason
                )
                return False, None
            claimed_pending = decision.claimed

        return True, claimed_pending

    def _grab_rss_candidate(
        self,
        db: Database,
        candidate: Any,
        matched_req: dict[str, Any],
        eval_res: Optional[Any],
        rss_trigger: GrabTrigger,
        claimed_pending: Optional[Any],
        stats: dict[str, int],
        active_req_ids: Optional[set[Any]] = None,
    ) -> None:
        """Locate a client, dispatch download, and record active download and history in database."""
        # Find download client for protocol
        client = acquisition_coordinator.find_client_for_protocol(
            protocol=candidate.protocol, db=db
        )
        if not client:
            if claimed_pending:
                db.restore_pending_release(claimed_pending)
            logger.warning(
                "RSS matched '%s' for request %s but no client available for protocol %s",
                candidate.title,
                matched_req["id"],
                candidate.protocol,
            )
            return

        # Dispatch download to client
        try:
            client_driver = get_acquisition_driver(client)
            download_hash = client_driver.download(candidate)
        except Exception as e:
            logger.error(
                "Dispatch download failed on client '%s' for '%s': %s",
                client.get("name"),
                candidate.title,
                e,
            )
            stats["errors"] += 1
            if claimed_pending:
                db.restore_pending_release(claimed_pending)
            return

        # Record active download in database
        download_id = f"dl-{uuid.uuid4().hex[:12]}"
        active_dl = ActiveDownload(
            id=download_id,
            request_id=matched_req["id"],
            client_id=str(client["id"]),
            download_hash=download_hash,
            title=candidate.title,
            artist=matched_req.get("artist", candidate.artist),
            item_type=matched_req.get("item_type", "track"),
            status=DownloadStatus.QUEUED.value,
            progress=0.0,
            size_bytes=candidate.size_bytes,
            source_path=None,
            target_path=None,
        )
        try:
            db.create_active_download(active_dl)
            apply_seed_rules_at_grab(
                db, client_driver, download_id, download_hash, candidate.title, candidate.protocol,
                candidate.extra,
            )
            try:
                db.record_download_grab(
                    download_id,
                    indexer=str((candidate.extra or {}).get("indexer_name") or candidate.source or "") or None,
                    quality=eval_res.parsed_quality if eval_res else None,
                    protocol=candidate.protocol or None,
                    upgrade=matched_req.get("cutoff_met") == 0 or matched_req.get("status") == "available",
                    trigger=rss_trigger,
                )
            except sqlite3.Error as hist_err:
                logger.warning("Failed to record grab history for %s: %s", download_id, type(hist_err).__name__)
            db.clear_pending_for_item(str(matched_req["id"]))
            db.update_request_status(matched_req["id"], RequestStatus.PROCESSING)
            if active_req_ids is not None:
                active_req_ids.add(matched_req["id"])
            stats["grabs_triggered"] += 1
            try:
                notification_dispatcher.dispatch(
                    NotificationEvent.DOWNLOAD_STARTED,
                    data={
                        "artist": active_dl.artist,
                        "title": active_dl.title,
                        "release": candidate.title,
                        "client": client.get("name"),
                        "request_id": matched_req["id"],
                        "user_id": matched_req.get("user_id") or matched_req.get("requested_by"),
                        "download_id": download_id,
                        "size_bytes": candidate.size_bytes,
                    },
                    db=db,
                )
            except Exception as ex:
                logger.warning("Failed to dispatch RSS DOWNLOAD_STARTED notification: %s", ex)

            try:
                db.record_event(
                    "download_started",
                    f"Grabbed '{active_dl.title}' via {client.get('name')}",
                    source="AcquisitionWorker",
                    severity="info",
                    details={
                        "artist": active_dl.artist,
                        "title": active_dl.title,
                        "release": candidate.title,
                        "client": client.get("name"),
                        "request_id": matched_req["id"],
                        "download_id": download_id,
                        "size_bytes": candidate.size_bytes,
                    },
                )
            except Exception as ev_err:
                logger.warning("Failed to record RSS download_started event: %s", ev_err)

            logger.info(
                "RSSSyncWorker grabbed '%s' for request %s via %s (score=%s)",
                candidate.title,
                matched_req["id"],
                client.get("name"),
                eval_res.score if eval_res else "N/A",
            )
        except Exception as e:
            logger.error("Error creating active download for '%s': %s", candidate.title, e)
            stats["errors"] += 1

    def _process_rss_candidate(
        self,
        db: Database,
        idx_cfg: dict[str, Any],
        candidate: Any,
        wanted_requests: list[dict[str, Any]],
        default_profile: Optional[Any],
        stats: dict[str, int],
        active_req_ids: Optional[set[Any]] = None,
    ) -> None:
        """Process a single RSS candidate release against wanted requests."""
        matched_req = self._match_wanted_request(candidate, wanted_requests, active_req_ids)
        if not matched_req:
            return

        eval_data = self._evaluate_rss_candidate(db, candidate, matched_req, default_profile)
        if eval_data is None:
            return
        eval_res, req_profile, req_artist_tags, rss_trigger = eval_data

        can_grab, claimed_pending = self._delay_gate_rss_candidate(
            db, candidate, matched_req, eval_res, req_profile, req_artist_tags, rss_trigger
        )
        if not can_grab:
            return

        self._grab_rss_candidate(
            db,
            candidate,
            matched_req,
            eval_res,
            rss_trigger,
            claimed_pending,
            stats,
            active_req_ids,
        )

    def _sync(self, db: Database) -> dict[str, int]:
        """Poll enabled indexers for recent releases and process candidate matches."""
        stats = {
            "releases_scanned": 0,
            "grabs_triggered": 0,
            "errors": 0,
        }

        indexers, wanted_requests, active_req_ids = self._collect_wanted_requests(db, stats)

        if not indexers or not wanted_requests:
            with self._lock:
                self.releases_scanned += stats["releases_scanned"]
                self.grabs_triggered += stats["grabs_triggered"]
                self.errors += stats["errors"]
            return stats

        # 3. Retrieve default quality profile
        try:
            profile_dict = db.get_default_quality_profile()
            profile = _to_quality_profile(profile_dict)
        except Exception as e:
            logger.warning("RSSSyncWorker failed to load default quality profile: %s", e)
            profile = None

        # 4. Iterate over indexers and recent releases
        for idx_cfg in indexers:
            if self._stop_event.is_set():
                break

            recent_releases = self._fetch_recent_releases(idx_cfg, stats)
            if recent_releases is None:
                continue

            for candidate in recent_releases:
                if self._stop_event.is_set():
                    break

                stats["releases_scanned"] += 1
                self._process_rss_candidate(
                    db,
                    idx_cfg,
                    candidate,
                    wanted_requests,
                    profile,
                    stats,
                    active_req_ids,
                )

        with self._lock:
            self.releases_scanned += stats["releases_scanned"]
            self.grabs_triggered += stats["grabs_triggered"]
            self.errors += stats["errors"]

        try:
            db.record_event(
                "rss_synced",
                f"RSS sync completed: {stats['releases_scanned']} releases scanned, {stats['grabs_triggered']} grabbed",
                source="RssSyncWorker",
                severity="info",
            )
        except Exception as ev_err:
            logger.warning("Failed to record rss_synced event: %s", ev_err)

        return stats


# Singletons
backlog_worker = WantedBacklogWorker()
rss_worker = RSSSyncWorker()

__all__ = [
    "WantedBacklogWorker",
    "RSSSyncWorker",
    "backlog_worker",
    "rss_worker",
    "_matches_request",
]

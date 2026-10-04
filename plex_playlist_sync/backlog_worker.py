"""Autonomous Wanted Backlog Search & Indexer RSS Sync Workers.

- WantedBacklogWorker: Periodically sweeps unfulfilled requests and missing playlist
  tracks against all enabled indexers with pacing delays to protect API rate limits.
- RSSSyncWorker: Periodically polls indexer recent release feeds (RSS/Torznab) and
  snatches releases that fulfill monitored requests.
"""

from difflib import SequenceMatcher
import logging
import threading
import time
from typing import Any, Optional
import uuid

from plex_playlist_sync.acquisition_coordinator import (
    acquisition_coordinator,
    _to_quality_profile,
)
from plex_playlist_sync.clients.acquisition import (
    get_acquisition_driver,
    get_indexer_driver,
)
from plex_playlist_sync.models import (
    AcquisitionSearchResult,
    ActiveDownload,
    DownloadStatus,
    NotificationEvent,
    RequestStatus,
)
from plex_playlist_sync.notifications import notification_dispatcher
from plex_playlist_sync.quality import evaluate_release, parse_release_title
from plex_playlist_sync.job_tracker import tracked
from plex_playlist_sync.library_manager import MODE_NATIVE, ModeChanged, work_guard
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)


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
    ) -> bool:
        """Starts daemon thread executing periodic backlog sweeps."""
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
                while not self._stop_event.is_set():
                    try:
                        self.poll_once(db=db)
                    except Exception as e:
                        logger.exception("Unexpected error in WantedBacklogWorker poll cycle: %s", e)
                        with self._lock:
                            self.errors += 1

                    # Responsive sleep
                    slept = 0.0
                    while slept < float(self.interval_seconds) and not self._stop_event.is_set():
                        time.sleep(min(1.0, float(self.interval_seconds) - slept))
                        slept += 1.0

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

    def _sweep(self, db: Database) -> dict[str, int]:
        items_checked = 0
        items_grabbed = 0
        errors_count = 0

        # 1. Query active downloads to avoid duplicate searches
        try:
            active_dls = db.list_active_downloads(statuses=["queued", "downloading", "importing"])
        except Exception as e:
            logger.error("WantedBacklogWorker error querying active downloads: %s", e)
            active_dls = []
            errors_count += 1

        active_req_ids = {d["request_id"] for d in active_dls if d.get("request_id")}
        active_track_ids = {d["track_id"] for d in active_dls if d.get("track_id")}
        active_artist_titles = {
            ((d.get("artist") or "").strip().lower(), (d.get("title") or "").strip().lower())
            for d in active_dls
        }

        # 2. Query unfulfilled requests: status in ('processing', 'pending')
        try:
            requests = db.list_requests()
        except Exception as e:
            logger.error("WantedBacklogWorker error querying requests: %s", e)
            requests = []
            errors_count += 1

        media_settings = db.get_media_management_settings()
        enable_upgrades = bool(media_settings.get("enable_quality_upgrades", True))
        library_mode = media_settings.get("library_mode", "native")

        unfulfilled_requests = [
            r
            for r in requests
            if r.get("status") in ("processing", "pending")
            and r.get("id") not in active_req_ids
            and (
                (r.get("artist") or "").strip().lower(),
                (r.get("title") or "").strip().lower(),
            )
            not in active_artist_titles
        ]

        if enable_upgrades:
            try:
                cutoff_unmet = db.get_cutoff_unmet_requests()
                for r in cutoff_unmet:
                    if (
                        r.get("id") not in active_req_ids
                        and (
                            (r.get("artist") or "").strip().lower(),
                            (r.get("title") or "").strip().lower(),
                        )
                        not in active_artist_titles
                    ):
                        unfulfilled_requests.append(r)
            except Exception as e:
                logger.error("WantedBacklogWorker error querying cutoff unmet requests: %s", e)
                errors_count += 1

        # 3. Query unfulfilled missing tracks: lidarr_status != 'monitored'
        try:
            missing_tracks = db.get_missing_tracks()
        except Exception as e:
            logger.error("WantedBacklogWorker error querying missing tracks: %s", e)
            missing_tracks = []
            errors_count += 1

        unfulfilled_missing = [
            t
            for t in missing_tracks
            if t.get("lidarr_status") != "monitored"
            and (
                (t.get("artist") or "").strip().lower(),
                (t.get("title") or "").strip().lower(),
            )
            not in active_artist_titles
        ]

        # Items to search: (artist, title, album, item_type, request_id, missing_track_id, quality_profile_id, min_score, track_id, album_id)
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
        ] = []
        for r in unfulfilled_requests:
            is_upgrade = (r.get("status") == "available" or r.get("cutoff_met") == 0)
            min_score = None
            qp_id = r.get("quality_profile_id")
            if is_upgrade:
                profile_dict = db.get_quality_profile(qp_id) if qp_id else db.get_default_quality_profile()
                if profile_dict:
                    prof = _to_quality_profile(profile_dict)
                    cur_q = r.get("current_quality")
                    if cur_q:
                        cur_p = parse_release_title(cur_q)
                        if cur_p.quality == "Unknown":
                            cur_p.quality = cur_q
                        min_score = evaluate_release(cur_p, prof).score
                    else:
                        min_score = 0
            items_to_search.append(
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

        for t in unfulfilled_missing:
            items_to_search.append(
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

        # 4. Query native catalog missing and cutoff-unmet tracks if in native mode
        if library_mode == "native":
            try:
                missing_catalog = db.get_monitored_missing_catalog_tracks(limit=100)
                for t in missing_catalog:
                    t_id = t["track_id"]
                    t_pair = (
                        (t.get("artist_name") or "").strip().lower(),
                        (t.get("track_title") or "").strip().lower(),
                    )
                    if t_id in active_track_ids or t_pair in active_artist_titles:
                        continue
                    items_to_search.append(
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
                errors_count += 1

            if enable_upgrades:
                try:
                    cutoff_unmet_catalog = db.get_cutoff_unmet_catalog_tracks(limit=100)
                    for t in cutoff_unmet_catalog:
                        t_id = t["track_id"]
                        t_pair = (
                            (t.get("artist_name") or "").strip().lower(),
                            (t.get("track_title") or "").strip().lower(),
                        )
                        if t_id in active_track_ids or t_pair in active_artist_titles:
                            continue

                        qp_id = t.get("quality_profile_id")
                        profile_dict = db.get_quality_profile(qp_id) if qp_id else db.get_default_quality_profile()
                        min_score = 0
                        if profile_dict:
                            prof = _to_quality_profile(profile_dict)
                            cur_q = t.get("quality_name")
                            if cur_q:
                                cur_p = parse_release_title(cur_q)
                                if cur_p.quality == "Unknown":
                                    cur_p.quality = cur_q
                                min_score = evaluate_release(cur_p, prof).score

                        items_to_search.append(
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
                    errors_count += 1

        for (
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
        ) in items_to_search:
            if self._stop_event.is_set():
                logger.info("WantedBacklogWorker sweep interrupted by stop event")
                break

            if not artist or not title:
                continue

            items_checked += 1
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
                )
                if res.get("success"):
                    items_grabbed += 1
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
                errors_count += 1

            # Pacing delay between calls
            if self.pace_delay > 0 and not self._stop_event.is_set():
                slept = 0.0
                while slept < self.pace_delay and not self._stop_event.is_set():
                    time.sleep(min(0.2, self.pace_delay - slept))
                    slept += 0.2

        with self._lock:
            self.items_checked += items_checked
            self.items_grabbed += items_grabbed
            self.errors += errors_count

        try:
            db.record_event(
                "backlog_sweep",
                f"Backlog sweep completed: {items_checked} items checked, {items_grabbed} grabbed",
                source="BacklogWorker",
                severity="info",
            )
        except Exception as ev_err:
            logger.warning("Failed to record backlog_sweep event: %s", ev_err)

        return {
            "items_checked": items_checked,
            "items_grabbed": items_grabbed,
            "errors": errors_count,
        }


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

    def start(self, db: Database, interval_seconds: int = 900) -> bool:
        """Starts daemon thread executing periodic RSS polling."""
        with self._lock:
            if self._is_running:
                logger.warning("RSSSyncWorker is already running")
                return False

            self.interval_seconds = interval_seconds
            self._stop_event.clear()
            self._is_running = True

            def _worker_loop() -> None:
                logger.info("RSSSyncWorker loop started (interval: %ds)", self.interval_seconds)
                while not self._stop_event.is_set():
                    try:
                        self.poll_once(db=db)
                    except Exception as e:
                        logger.exception("Unexpected error in RSSSyncWorker poll cycle: %s", e)
                        with self._lock:
                            self.errors += 1

                    # Responsive sleep
                    slept = 0.0
                    while slept < float(self.interval_seconds) and not self._stop_event.is_set():
                        time.sleep(min(1.0, float(self.interval_seconds) - slept))
                        slept += 1.0

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

    def _sync(self, db: Database) -> dict[str, int]:
        releases_scanned = 0
        grabs_triggered = 0
        errors_count = 0

        # 1. Retrieve enabled indexers
        try:
            indexers = db.list_indexers(enabled_only=True)
        except Exception as e:
            logger.error("RSSSyncWorker error listing enabled indexers: %s", e)
            indexers = []
            errors_count += 1

        # 2. Gather wanted requests without active transfers
        try:
            active_dls = db.list_active_downloads(statuses=["queued", "downloading", "importing"])
        except Exception as e:
            logger.error("RSSSyncWorker error querying active downloads: %s", e)
            active_dls = []
            errors_count += 1

        active_req_ids = {d["request_id"] for d in active_dls if d.get("request_id")}
        try:
            all_requests = db.list_requests()
        except Exception as e:
            logger.error("RSSSyncWorker error querying requests: %s", e)
            all_requests = []
            errors_count += 1

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
                errors_count += 1

        if not indexers or not wanted_requests:
            with self._lock:
                self.releases_scanned += releases_scanned
                self.grabs_triggered += grabs_triggered
                self.errors += errors_count
            return {
                "releases_scanned": releases_scanned,
                "grabs_triggered": grabs_triggered,
                "errors": errors_count,
            }

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

            try:
                driver = get_indexer_driver(idx_cfg)
                if hasattr(driver, "fetch_recent"):
                    recent_releases = driver.fetch_recent(limit=100)
                else:
                    logger.debug("Indexer driver '%s' does not implement fetch_recent", idx_cfg.get("name"))
                    continue
            except Exception as e:
                logger.warning("Error fetching recent releases from indexer '%s': %s", idx_cfg.get("name"), e)
                errors_count += 1
                continue

            for candidate in recent_releases:
                if self._stop_event.is_set():
                    break

                releases_scanned += 1

                # Check if release matches any wanted request
                matched_req = None
                for req in wanted_requests:
                    if req["id"] in active_req_ids:
                        continue
                    if _matches_request(candidate, req):
                        matched_req = req
                        break

                if not matched_req:
                    continue

                # Evaluate candidate against quality profile
                eval_res = None
                req_profile = profile
                if matched_req.get("quality_profile_id"):
                    try:
                        p_dict = db.get_quality_profile(matched_req["quality_profile_id"])
                        if p_dict:
                            req_profile = _to_quality_profile(p_dict)
                    except Exception as e:
                        logger.warning("Error fetching quality profile for request %s: %s", matched_req["id"], e)

                if req_profile:
                    parsed = parse_release_title(candidate.title)
                    eval_res = evaluate_release(
                        release=parsed,
                        profile=req_profile,
                        size_bytes=candidate.size_bytes if candidate.size_bytes > 0 else None,
                    )
                    if not eval_res.is_acceptable:
                        logger.debug(
                            "RSS candidate '%s' rejected by profile for request %s (%s)",
                            candidate.title,
                            matched_req["id"],
                            eval_res.rejection_reasons,
                        )
                        continue

                    # If this is a cutoff-unmet request, verify candidate.score > current_score
                    if matched_req.get("cutoff_met") == 0 or matched_req.get("status") == "available":
                        current_quality = matched_req.get("current_quality")
                        current_score = 0
                        if current_quality:
                            cur_p = parse_release_title(current_quality)
                            if cur_p.quality == "Unknown":
                                cur_p.quality = current_quality
                            current_score = evaluate_release(cur_p, req_profile).score

                        if eval_res.score <= current_score:
                            logger.debug(
                                "RSS candidate '%s' score %d does not exceed current score %d for upgrade request %s",
                                candidate.title,
                                eval_res.score,
                                current_score,
                                matched_req["id"],
                            )
                            continue

                # Find download client for protocol
                client = acquisition_coordinator.find_client_for_protocol(
                    protocol=candidate.protocol, db=db
                )
                if not client:
                    logger.warning(
                        "RSS matched '%s' for request %s but no client available for protocol %s",
                        candidate.title,
                        matched_req["id"],
                        candidate.protocol,
                    )
                    continue

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
                    errors_count += 1
                    continue

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
                    db.update_request_status(matched_req["id"], RequestStatus.PROCESSING)
                    active_req_ids.add(matched_req["id"])
                    grabs_triggered += 1
                    try:
                        notification_dispatcher.dispatch(
                            NotificationEvent.DOWNLOAD_STARTED,
                            data={
                                "artist": active_dl.artist,
                                "title": active_dl.title,
                                "release": candidate.title,
                                "client": client.get("name"),
                                "request_id": matched_req["id"],
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
                    errors_count += 1

        with self._lock:
            self.releases_scanned += releases_scanned
            self.grabs_triggered += grabs_triggered
            self.errors += errors_count

        try:
            db.record_event(
                "rss_synced",
                f"RSS sync completed: {releases_scanned} releases scanned, {grabs_triggered} grabbed",
                source="RssSyncWorker",
                severity="info",
            )
        except Exception as ev_err:
            logger.warning("Failed to record rss_synced event: %s", ev_err)

        return {
            "releases_scanned": releases_scanned,
            "grabs_triggered": grabs_triggered,
            "errors": errors_count,
        }


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

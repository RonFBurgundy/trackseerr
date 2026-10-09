"""Arr-grade background worker for artist metadata & discography refresh, track hydration, and artwork caching.

Periodically sweeps monitored library artists with pacing delays to enrich metadata,
hydrate complete canonical tracklists via BrainzMash / MusicBrainz and Deezer,
cache artist poster/banner and album cover artwork locally, and reconcile media files.
"""

from datetime import datetime, timezone
import logging
import sqlite3
import threading
import time
from typing import Any, Callable, Optional

from plex_playlist_sync.clients.discovery import DiscoveryClient
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.mb_metadata_store import get_shared_discovery_client, get_shared_enricher
from plex_playlist_sync.job_tracker import tracked
from plex_playlist_sync.mb_metadata_store import MbMetadataStore
from plex_playlist_sync.task_manager import TRIGGER_SCHEDULED, record_task_run, wait_for_next_cycle
from plex_playlist_sync.system_paths import is_system_folder_name
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

# Per-artist "last refreshed" marker persisted in kv_store so a restart does not re-sweep the whole library.
_LAST_REFRESH_PREFIX = "artist_refresh:last:"
# First scheduled sweep waits this long after start so boot and the first requests are not competing with it.
DEFAULT_INITIAL_DELAY_SECONDS = 600


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class ArtistRefreshWorker:
    """Autonomous background worker executing periodic artist discography & track refreshes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running: bool = False
        self.interval_seconds: int = 86400  # Default 24 hours
        self.pace_delay: float = 1.5  # 1.5 seconds pacing between artists
        self.last_run_at: Optional[str] = None
        self.artists_checked: int = 0
        self.albums_added: int = 0
        self.tracks_added: int = 0
        self.errors: int = 0

    def is_running(self) -> bool:
        """Returns True if the background scheduler or a refresh cycle is running."""
        with self._lock:
            return self._is_running

    def get_status(self) -> dict[str, Any]:
        """Returns status snapshot for telemetry and scheduled task registries."""
        with self._lock:
            return {
                "running": self._is_running,
                "interval_seconds": self.interval_seconds,
                "pace_delay": self.pace_delay,
                "last_run_at": self.last_run_at,
                "artists_checked": self.artists_checked,
                "albums_added": self.albums_added,
                "tracks_added": self.tracks_added,
                "errors": self.errors,
            }

    def start(
        self,
        db: Database,
        discovery_client: Optional[DiscoveryClient] = None,
        enricher: Optional[MbidEnricherClient] = None,
        interval_seconds: int = 86400,
        pace_delay: float = 1.5,
        initial_delay: float = DEFAULT_INITIAL_DELAY_SECONDS,
        interval_fn: Optional[Callable[[], float]] = None,
    ) -> bool:
        """Starts background daemon thread for periodic scheduled refreshes. ``interval_fn`` (the task manager's
        effective interval) is re-read every second while sleeping so a schedule edit applies without a restart."""
        with self._lock:
            if self._is_running:
                logger.warning("ArtistRefreshWorker: Already running")
                return False

            self.interval_seconds = int(interval_seconds)
            self.pace_delay = float(pace_delay)
            self._stop_event.clear()
            self._is_running = True

            def _worker_loop() -> None:
                logger.info(
                    "ArtistRefreshWorker: Loop started (interval: %ds, pace: %.1fs)",
                    self.interval_seconds,
                    self.pace_delay,
                )
                # Delay the first cycle (responsive to stop) so a restart never starts a sweep at boot.
                if self._stop_event.wait(max(0.0, float(initial_delay))):
                    with self._lock:
                        self._is_running = False
                    return
                cycle_interval = interval_fn or (lambda: float(self.interval_seconds))
                while not self._stop_event.is_set():
                    try:
                        with record_task_run(db, "artist_metadata_refresh", TRIGGER_SCHEDULED) as run:
                            res = self.refresh_once(
                                db=db,
                                discovery_client=discovery_client,
                                enricher=enricher,
                                only_stale=True,
                            )
                            run.apply_result(res)
                            msg_parts = [
                                f"checked {res.get('artists_checked', 0)} artists",
                                f"{res.get('network_requests', 0)} MB requests",
                                f"{res.get('cache_hits', 0)} cache hits",
                            ]
                            if res.get("cache_pruned", 0) > 0:
                                msg_parts.append(f"{res['cache_pruned']} pruned")
                            if res.get("deezer_requests", 0) > 0 or res.get("deezer_cache_hits", 0) > 0:
                                msg_parts.append(f"{res.get('deezer_requests', 0)} Deezer reqs, {res.get('deezer_cache_hits', 0)} Deezer hits")
                            if res.get("aborted_source_unavailable"):
                                msg_parts.append(f"aborted due to source unavailable ({res.get('remaining_artists', 0)} remaining)")
                            run.message = f"Artist refresh: {', '.join(msg_parts)}"
                    except Exception as exc:
                        logger.exception("ArtistRefreshWorker: Error in refresh cycle: %s", exc)
                        with self._lock:
                            self.errors += 1

                    # Responsive sleep; the interval is re-read every second (schedule edits apply at once)
                    if wait_for_next_cycle(self._stop_event, cycle_interval):
                        break
                    self.interval_seconds = int(cycle_interval())

                with self._lock:
                    self._is_running = False
                logger.info("ArtistRefreshWorker: Loop terminated cleanly")

            self._thread = threading.Thread(
                target=_worker_loop, daemon=True, name="ArtistRefreshWorkerThread"
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        """Signals worker thread to terminate and waits for join."""
        self._stop_event.set()
        thread_to_join = None
        with self._lock:
            if self._thread is not None and self._thread != threading.current_thread():
                thread_to_join = self._thread
        if thread_to_join and thread_to_join.is_alive():
            thread_to_join.join(timeout=5.0)
        with self._lock:
            self._is_running = False

    def _filter_stale(self, db: Database, artists: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drops artists whose persisted last-refresh time is newer than the interval."""
        last = db.list_kv_prefix(_LAST_REFRESH_PREFIX)
        now = datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []
        for art in artists:
            raw = last.get(_LAST_REFRESH_PREFIX + str(art["id"]))
            ts = _parse_iso(raw) if raw else None
            if ts is not None and (now - ts).total_seconds() < self.interval_seconds:
                continue
            out.append(art)
        return out

    @tracked("artist_metadata_refresh", "Artist Metadata & Discography Refresh")
    def refresh_once(
        self,
        db: Database,
        discovery_client: Optional[DiscoveryClient] = None,
        enricher: Optional[MbidEnricherClient] = None,
        artist_ids: Optional[list[str]] = None,
        only_stale: bool = False,
    ) -> dict[str, Any]:
        """Synchronously executes a refresh cycle across target artists with pacing.

        ``only_stale`` (scheduled sweeps) skips artists refreshed within the last ``interval_seconds``.
        """
        from plex_playlist_sync.api.routes.library import refresh_single_artist

        with self._run_lock:
            dc = discovery_client or get_shared_discovery_client(db)
            enr = enricher or get_shared_enricher(db)

            enr_stats_before = enr.stats()
            dc_stats_before = dc.stats()

            if artist_ids:
                target_artists: list[dict[str, Any]] = []
                for aid in artist_ids:
                    art = db.get_library_artist(aid)
                    if art:
                        target_artists.append(art)
            else:
                target_artists = db.list_library_artists(monitored_only=True, limit=10000)
                if only_stale:
                    target_artists = self._filter_stale(db, target_artists)

            logger.info("ArtistRefreshWorker: Starting refresh cycle for %d artist(s)", len(target_artists))
            start_iso = datetime.now(timezone.utc).isoformat()
            checked = 0
            errs = 0
            aborted_source_unavailable = False
            remaining_artists = 0

            initial_stats = db.get_library_stats()
            prev_albums = initial_stats.get("album_count", 0)
            prev_tracks = initial_stats.get("track_count", 0)

            for idx, art in enumerate(target_artists):
                if self._stop_event.is_set():
                    logger.info("ArtistRefreshWorker: Stop event received, aborting cycle")
                    break

                if not enr.source_available():
                    remaining_artists = len(target_artists) - idx
                    aborted_source_unavailable = True
                    logger.warning(
                        "ArtistRefreshWorker: Enricher source unavailable before artist %s (%d artists remaining); aborting sweep early",
                        art.get("id"),
                        remaining_artists,
                    )
                    break

                art_id = str(art["id"])
                art_name = str(art.get("name") or "Unknown Artist")
                if is_system_folder_name(art_name):
                    logger.info("ArtistRefreshWorker: skipping %r (%s): OS/NAS system or trash folder name", art_name, art_id)
                    continue

                source_unavail_artist = False
                try:
                    res = refresh_single_artist(
                        artist_id=art_id,
                        db=db,
                        discovery_client=dc,
                        enricher=enr,
                        force=False,
                    )
                    if res.get("source_unavailable"):
                        source_unavail_artist = True
                    if not res.get("success"):
                        errs += 1
                        logger.warning("ArtistRefreshWorker: Refresh unsuccessful for %s: %s", art_name, res.get("message"))
                except Exception as exc:
                    errs += 1
                    logger.warning("ArtistRefreshWorker: Exception refreshing artist %s (%s): %s", art_name, art_id, exc)

                checked += 1
                if not source_unavail_artist:
                    try:
                        db.set_kv(_LAST_REFRESH_PREFIX + art_id, datetime.now(timezone.utc).isoformat())
                    except Exception as exc:
                        logger.warning("ArtistRefreshWorker: could not persist refresh time for %s: %s", art_id, exc)

                # Pacing delay between artists
                if idx < len(target_artists) - 1 and not self._stop_event.is_set():
                    p_delay = self.pace_delay
                    slept = 0.0
                    while slept < p_delay and not self._stop_event.is_set():
                        step = min(0.2, p_delay - slept)
                        self._stop_event.wait(step)
                        slept += step

            cache_pruned = 0
            try:
                cache_pruned = MbMetadataStore(db).prune_expired()
            except sqlite3.Error as p_exc:
                logger.warning("ArtistRefreshWorker: Failed pruning expired metadata cache: %s", p_exc)

            enr_stats_after = enr.stats()
            dc_stats_after = dc.stats()

            net_reqs = enr_stats_after.get("network_requests", 0) - enr_stats_before.get("network_requests", 0)
            c_hits = enr_stats_after.get("cache_hits", 0) - enr_stats_before.get("cache_hits", 0)
            dz_reqs = dc_stats_after.get("network_requests", 0) - dc_stats_before.get("network_requests", 0)
            dz_hits = dc_stats_after.get("cache_hits", 0) - dc_stats_before.get("cache_hits", 0)

            final_stats = db.get_library_stats()
            new_albums = max(0, final_stats.get("album_count", 0) - prev_albums)
            new_tracks = max(0, final_stats.get("track_count", 0) - prev_tracks)

            with self._lock:
                self.last_run_at = start_iso
                self.artists_checked += checked
                self.albums_added += new_albums
                self.tracks_added += new_tracks
                self.errors += errs

            logger.info(
                "ArtistRefreshWorker: Cycle complete. Checked %d artists, added %d albums, %d tracks, errors=%d, network_requests=%d, cache_hits=%d, cache_pruned=%d, deezer_requests=%d, deezer_cache_hits=%d",
                checked,
                new_albums,
                new_tracks,
                errs,
                net_reqs,
                c_hits,
                cache_pruned,
                dz_reqs,
                dz_hits,
            )

            result_dict: dict[str, Any] = {
                "success": True,
                "last_run_at": start_iso,
                "artists_checked": checked,
                "albums_added": new_albums,
                "tracks_added": new_tracks,
                "errors": errs,
                "network_requests": net_reqs,
                "cache_hits": c_hits,
                "cache_pruned": cache_pruned,
                "deezer_requests": dz_reqs,
                "deezer_cache_hits": dz_hits,
            }
            if aborted_source_unavailable:
                result_dict["aborted_source_unavailable"] = True
                result_dict["remaining_artists"] = remaining_artists

            return result_dict


# Singleton instance
artist_refresh_worker = ArtistRefreshWorker()

__all__ = ["ArtistRefreshWorker", "artist_refresh_worker"]

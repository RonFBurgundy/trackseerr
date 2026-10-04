"""Arr-grade background worker for artist metadata & discography refresh, track hydration, and artwork caching.

Periodically sweeps monitored library artists with pacing delays to enrich metadata,
hydrate complete canonical tracklists via BrainzMash / MusicBrainz and Deezer,
cache artist poster/banner and album cover artwork locally, and reconcile media files.
"""

from datetime import datetime, timezone
import logging
import threading
import time
from typing import Any, Optional

from plex_playlist_sync.clients.discovery import DiscoveryClient
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.job_tracker import tracked
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)


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
    ) -> bool:
        """Starts background daemon thread for periodic scheduled refreshes."""
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
                while not self._stop_event.is_set():
                    try:
                        self.refresh_once(
                            db=db,
                            discovery_client=discovery_client,
                            enricher=enricher,
                        )
                    except Exception as exc:
                        logger.exception("ArtistRefreshWorker: Error in refresh cycle: %s", exc)
                        with self._lock:
                            self.errors += 1

                    # Responsive sleep
                    slept = 0.0
                    while slept < float(self.interval_seconds) and not self._stop_event.is_set():
                        time.sleep(min(1.0, float(self.interval_seconds) - slept))
                        slept += 1.0

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

    @tracked("artist_metadata_refresh", "Artist Metadata & Discography Refresh")
    def refresh_once(
        self,
        db: Database,
        discovery_client: Optional[DiscoveryClient] = None,
        enricher: Optional[MbidEnricherClient] = None,
        artist_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Synchronously executes a refresh cycle across target artists with pacing."""
        from plex_playlist_sync.api.routes.library import refresh_single_artist

        with self._run_lock:
            dc = discovery_client or DiscoveryClient()
            enr = enricher or MbidEnricherClient()

            if artist_ids:
                target_artists: list[dict[str, Any]] = []
                for aid in artist_ids:
                    art = db.get_library_artist(aid)
                    if art:
                        target_artists.append(art)
            else:
                target_artists = db.list_library_artists(monitored_only=True, limit=10000)

            logger.info("ArtistRefreshWorker: Starting refresh cycle for %d artist(s)", len(target_artists))
            start_iso = datetime.now(timezone.utc).isoformat()
            checked = 0
            errs = 0

            initial_stats = db.get_library_stats()
            prev_albums = initial_stats.get("album_count", 0)
            prev_tracks = initial_stats.get("track_count", 0)

            for idx, art in enumerate(target_artists):
                if self._stop_event.is_set():
                    logger.info("ArtistRefreshWorker: Stop event received, aborting cycle")
                    break

                art_id = str(art["id"])
                art_name = str(art.get("name") or "Unknown Artist")
                try:
                    res = refresh_single_artist(
                        artist_id=art_id,
                        db=db,
                        discovery_client=dc,
                        enricher=enr,
                    )
                    if not res.get("success"):
                        errs += 1
                        logger.warning("ArtistRefreshWorker: Refresh unsuccessful for %s: %s", art_name, res.get("message"))
                except Exception as exc:
                    errs += 1
                    logger.warning("ArtistRefreshWorker: Exception refreshing artist %s (%s): %s", art_name, art_id, exc)

                checked += 1

                # Pacing delay between artists
                if idx < len(target_artists) - 1 and not self._stop_event.is_set():
                    p_delay = self.pace_delay
                    slept = 0.0
                    while slept < p_delay and not self._stop_event.is_set():
                        time.sleep(min(0.2, p_delay - slept))
                        slept += 0.2

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
                "ArtistRefreshWorker: Cycle complete. Checked %d artists, added %d albums, %d tracks, errors=%d",
                checked,
                new_albums,
                new_tracks,
                errs,
            )

            return {
                "success": True,
                "last_run_at": start_iso,
                "artists_checked": checked,
                "albums_added": new_albums,
                "tracks_added": new_tracks,
                "errors": errs,
            }


# Singleton instance
artist_refresh_worker = ArtistRefreshWorker()

__all__ = ["ArtistRefreshWorker", "artist_refresh_worker"]

"""Thread-safe Lidarr migration engine.

Imports artists, albums, tracks, and physical audio files from Lidarr REST API
into TrackSeerr native library catalog tables (library_artists, library_albums,
library_tracks, library_files) and updates media management library mode.
"""

import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from trackseerr.clients.lidarr import LidarrClient
from trackseerr.models import (
    LibraryAlbum,
    LibraryArtist,
    LibraryFile,
    LibraryTrack,
)
from trackseerr.library_manager import MODE_NATIVE, SwitchRefused, switch_mode
from trackseerr.redaction import safe_exc
from trackseerr.storage import Database

logger = logging.getLogger(__name__)


class LidarrMigrationJob:
    """Thread-safe background runner for migrating Lidarr catalog into TrackSeerr."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._status: dict[str, Any] = self._default_status()

    @staticmethod
    def _default_status() -> dict[str, Any]:
        return {
            "is_migrating": False,
            "artists_migrated": 0,
            "albums_migrated": 0,
            "tracks_migrated": 0,
            "files_migrated": 0,
            "status": "idle",  # "idle" | "running" | "completed" | "cancelled" | "failed"
            "error": None,
            "started_at": None,
            "completed_at": None,
        }

    def is_running(self) -> bool:
        """Returns True if a migration job is currently executing."""
        with self._lock:
            return bool(self._status.get("is_migrating", False))

    def get_status(self) -> dict[str, Any]:
        """Returns a copy of the current migration status dictionary."""
        with self._lock:
            return dict(self._status)

    def cancel(self) -> dict[str, Any]:
        """Signals active migration to stop and returns current status."""
        with self._lock:
            self._stop_event.set()
            if not self._status.get("is_migrating", False) and self._status.get("status") == "idle":
                self._status["status"] = "cancelled"
            return dict(self._status)

    def start_migration(
        self,
        db: Database,
        lidarr_client: LidarrClient,
        auto_switch_mode: bool = True,
    ) -> bool:
        """Spawns background daemon thread running run_migration."""
        with self._lock:
            if self._status.get("is_migrating", False):
                logger.warning("LidarrMigrationJob: Migration already running.")
                return False

            self._stop_event.clear()
            self._status = self._default_status()
            self._status["is_migrating"] = True
            self._status["status"] = "running"
            self._status["started_at"] = datetime.now(timezone.utc).isoformat()

            self._thread = threading.Thread(
                target=self.run_migration,
                args=(db, lidarr_client, auto_switch_mode),
                daemon=True,
                name="LidarrMigrationThread",
            )
            self._thread.start()
            return True

    def run_migration(
        self,
        db: Database,
        lidarr_client: LidarrClient,
        auto_switch_mode: bool = True,
    ) -> dict[str, Any]:
        """Synchronously executes the Lidarr library migration loop."""
        with self._lock:
            if self._stop_event.is_set():
                self._status["status"] = "cancelled"
                self._status["is_migrating"] = False
                self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                return dict(self._status)

            if self._status.get("is_migrating", False) and self._thread != threading.current_thread():
                logger.warning("LidarrMigrationJob: Migration is already running in another thread.")
                return dict(self._status)

            self._status["is_migrating"] = True
            self._status["status"] = "running"
            self._status["artists_migrated"] = 0
            self._status["albums_migrated"] = 0
            self._status["tracks_migrated"] = 0
            self._status["files_migrated"] = 0
            self._status["error"] = None
            if not self._status.get("started_at"):
                self._status["started_at"] = datetime.now(timezone.utc).isoformat()
            self._status["completed_at"] = None

        lidarr_artist_map: dict[Any, str] = {}
        lidarr_album_map: dict[Any, str] = {}
        lidarr_track_map: dict[Any, str] = {}

        try:
            # -------------------------------------------------------------
            # 1. Fetch & ingest artists
            # -------------------------------------------------------------
            logger.info("LidarrMigration: Fetching artists from Lidarr...")
            raw_artists = lidarr_client.get_all_artists()

            for artist in raw_artists:
                if self._stop_event.is_set():
                    logger.info("LidarrMigration: Cancellation requested during artist ingestion.")
                    with self._lock:
                        self._status["status"] = "cancelled"
                        self._status["is_migrating"] = False
                        self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                    return dict(self._status)

                artist_name = (artist.get("artistName") or "Unknown Artist").strip()
                lidarr_art_id = artist.get("id")
                foreign_artist_id = (
                    str(artist["foreignArtistId"]) if artist.get("foreignArtistId") is not None else None
                )
                path = artist.get("path")
                monitored = bool(artist.get("monitored", True))

                existing = db.get_library_artist_by_name(artist_name)
                artist_id = str(existing["id"]) if existing else str(uuid.uuid4())

                image_url: Optional[str] = None
                banner_url: Optional[str] = None
                fanart_url: Optional[str] = None
                for img in (artist.get("images") or []):
                    if not isinstance(img, dict):
                        continue
                    ctype = str(img.get("coverType") or "").strip().lower()
                    url = img.get("url")
                    if not url:
                        continue
                    url_str = str(url)
                    if ctype == "poster":
                        image_url = url_str
                    elif ctype == "banner":
                        banner_url = url_str
                    elif ctype == "fanart":
                        fanart_url = url_str

                if not banner_url and fanart_url:
                    banner_url = fanart_url
                if not image_url and fanart_url:
                    image_url = fanart_url

                artist_model = LibraryArtist(
                    id=artist_id,
                    name=artist_name,
                    foreign_artist_id=foreign_artist_id,
                    path=path,
                    monitored=monitored,
                    # Lidarr's per-album/track flags are copied as-is, so never reinterpret them under "existing".
                    monitor_option="all",
                    image_url=image_url,
                    banner_url=banner_url,
                )
                upserted = db.upsert_library_artist(artist_model)
                new_artist_id = str(upserted["id"])

                if lidarr_art_id is not None:
                    lidarr_artist_map[lidarr_art_id] = new_artist_id

                with self._lock:
                    self._status["artists_migrated"] += 1

            if self._stop_event.is_set():
                with self._lock:
                    self._status["status"] = "cancelled"
                    self._status["is_migrating"] = False
                    self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                return dict(self._status)

            # -------------------------------------------------------------
            # 2. Fetch & ingest albums
            # -------------------------------------------------------------
            logger.info("LidarrMigration: Fetching albums from Lidarr...")
            raw_albums = lidarr_client.get_all_albums()

            for album in raw_albums:
                if self._stop_event.is_set():
                    logger.info("LidarrMigration: Cancellation requested during album ingestion.")
                    with self._lock:
                        self._status["status"] = "cancelled"
                        self._status["is_migrating"] = False
                        self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                    return dict(self._status)

                lidarr_album_id = album.get("id")
                lidarr_art_id = album.get("artistId")
                mapped_artist_id = lidarr_artist_map.get(lidarr_art_id)

                if not mapped_artist_id:
                    artist_name = album.get("artistName")
                    if artist_name:
                        existing_art = db.get_library_artist_by_name(artist_name)
                        if existing_art:
                            mapped_artist_id = str(existing_art["id"])

                if not mapped_artist_id:
                    logger.warning(
                        "LidarrMigration: Skipping album '%s', artistId %s could not be resolved.",
                        album.get("title"),
                        lidarr_art_id,
                    )
                    continue

                album_title = (album.get("title") or "Unknown Album").strip()
                foreign_album_id = (
                    str(album["foreignAlbumId"]) if album.get("foreignAlbumId") is not None else None
                )
                release_date = album.get("releaseDate")
                year: Optional[int] = None
                if album.get("year") is not None:
                    try:
                        year = int(album["year"])
                    except (ValueError, TypeError):
                        pass
                if year is None and release_date:
                    try:
                        prefix = str(release_date)[:4]
                        if prefix.isdigit():
                            year = int(prefix)
                    except (ValueError, TypeError):
                        pass

                monitored = bool(album.get("monitored", True))
                album_path = album.get("path")

                existing_album = db.get_library_album_by_title(mapped_artist_id, album_title)
                album_id = str(existing_album["id"]) if existing_album else str(uuid.uuid4())

                cover_url: Optional[str] = None
                for img in (album.get("images") or []):
                    if not isinstance(img, dict):
                        continue
                    ctype = str(img.get("coverType") or "").strip().lower()
                    url = img.get("url")
                    if url and ctype == "cover":
                        cover_url = str(url)
                        break

                album_model = LibraryAlbum(
                    id=album_id,
                    artist_id=mapped_artist_id,
                    title=album_title,
                    foreign_album_id=foreign_album_id,
                    release_date=release_date,
                    year=year,
                    monitored=monitored,
                    path=album_path,
                    cover_url=cover_url,
                )
                upserted_album = db.upsert_library_album(album_model)
                new_album_id = str(upserted_album["id"])

                if lidarr_album_id is not None:
                    lidarr_album_map[lidarr_album_id] = new_album_id

                with self._lock:
                    self._status["albums_migrated"] += 1

            if self._stop_event.is_set():
                with self._lock:
                    self._status["status"] = "cancelled"
                    self._status["is_migrating"] = False
                    self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                return dict(self._status)

            # -------------------------------------------------------------
            # 3. Fetch & ingest tracks
            # -------------------------------------------------------------
            logger.info("LidarrMigration: Fetching tracks from Lidarr...")
            raw_tracks = lidarr_client.get_all_tracks()

            for track in raw_tracks:
                if self._stop_event.is_set():
                    logger.info("LidarrMigration: Cancellation requested during track ingestion.")
                    with self._lock:
                        self._status["status"] = "cancelled"
                        self._status["is_migrating"] = False
                        self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                    return dict(self._status)

                lidarr_track_id = track.get("id")
                lidarr_alb_id = track.get("albumId")
                lidarr_art_id = track.get("artistId")

                mapped_album_id = lidarr_album_map.get(lidarr_alb_id)
                mapped_artist_id = lidarr_artist_map.get(lidarr_art_id)

                if not mapped_artist_id and mapped_album_id:
                    alb_row = db.get_library_album(mapped_album_id)
                    if alb_row:
                        mapped_artist_id = str(alb_row.get("artist_id"))

                if not mapped_album_id or not mapped_artist_id:
                    logger.warning(
                        "LidarrMigration: Skipping track '%s', unresolved albumId %s or artistId %s.",
                        track.get("title"),
                        lidarr_alb_id,
                        lidarr_art_id,
                    )
                    continue

                track_title = (track.get("title") or "Unknown Track").strip()
                try:
                    track_number = int(track.get("trackNumber", 1))
                except (ValueError, TypeError):
                    track_number = 1
                try:
                    disc_number = int(track.get("discNumber", 1))
                except (ValueError, TypeError):
                    disc_number = 1

                duration_ms = track.get("duration")
                duration_seconds: Optional[float] = None
                if duration_ms is not None:
                    try:
                        duration_seconds = float(duration_ms) / 1000.0
                    except (ValueError, TypeError):
                        pass

                monitored = bool(track.get("monitored", True))
                foreign_track_id = (
                    str(track["foreignTrackId"]) if track.get("foreignTrackId") is not None else None
                )

                existing_track = db.get_library_track_by_title(
                    mapped_album_id, track_title, track_number
                )
                track_id = str(existing_track["id"]) if existing_track else str(uuid.uuid4())

                track_model = LibraryTrack(
                    id=track_id,
                    album_id=mapped_album_id,
                    artist_id=mapped_artist_id,
                    title=track_title,
                    track_number=track_number,
                    disc_number=disc_number,
                    duration_seconds=duration_seconds,
                    monitored=monitored,
                    foreign_track_id=foreign_track_id,
                )
                upserted_track = db.upsert_library_track(track_model)
                new_track_id = str(upserted_track["id"])

                if lidarr_track_id is not None:
                    lidarr_track_map[lidarr_track_id] = new_track_id

                with self._lock:
                    self._status["tracks_migrated"] += 1

            if self._stop_event.is_set():
                with self._lock:
                    self._status["status"] = "cancelled"
                    self._status["is_migrating"] = False
                    self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                return dict(self._status)

            # -------------------------------------------------------------
            # 4. Fetch & ingest physical track files
            # -------------------------------------------------------------
            logger.info("LidarrMigration: Fetching track files from Lidarr...")
            raw_track_files = lidarr_client.get_all_track_files()

            for tf in raw_track_files:
                if self._stop_event.is_set():
                    logger.info("LidarrMigration: Cancellation requested during track file ingestion.")
                    with self._lock:
                        self._status["status"] = "cancelled"
                        self._status["is_migrating"] = False
                        self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                    return dict(self._status)

                lidarr_track_id_raw = tf.get("trackId")
                if lidarr_track_id_raw is None and tf.get("trackIds"):
                    lidarr_track_id_raw = tf["trackIds"][0]

                mapped_track_id = lidarr_track_map.get(lidarr_track_id_raw)
                if not mapped_track_id:
                    logger.debug(
                        "LidarrMigration: Track file '%s' has unresolved trackId %s, skipping.",
                        tf.get("path"),
                        lidarr_track_id_raw,
                    )
                    continue

                file_path = tf.get("path")
                if not file_path:
                    continue

                relative_path = tf.get("relativePath") or Path(file_path).name
                media_info = tf.get("mediaInfo") or {}
                codec = str(media_info.get("audioCodec") or "UNKNOWN")

                bitrate: Optional[int] = None
                if media_info.get("audioBitrate") is not None:
                    try:
                        bitrate = int(media_info["audioBitrate"])
                    except (ValueError, TypeError):
                        pass

                sample_rate: Optional[int] = None
                if media_info.get("audioSampleRate") is not None:
                    try:
                        sample_rate = int(media_info["audioSampleRate"])
                    except (ValueError, TypeError):
                        pass

                bits_per_sample: Optional[int] = None
                if media_info.get("audioBitsPerSample") is not None:
                    try:
                        bits_per_sample = int(media_info["audioBitsPerSample"])
                    except (ValueError, TypeError):
                        pass

                q_dict = tf.get("quality") or {}
                q_inner = q_dict.get("quality") or {}
                quality_name = str(q_inner.get("name") or q_dict.get("name") or "Unknown")

                try:
                    size_bytes = int(tf.get("size", 0))
                except (ValueError, TypeError):
                    size_bytes = 0

                existing_file = db.get_library_file_by_path(file_path)
                file_id = str(existing_file["id"]) if existing_file else str(uuid.uuid4())

                file_model = LibraryFile(
                    id=file_id,
                    track_id=mapped_track_id,
                    file_path=file_path,
                    relative_path=relative_path,
                    codec=codec,
                    bitrate=bitrate,
                    sample_rate=sample_rate,
                    bits_per_sample=bits_per_sample,
                    quality_name=quality_name,
                    size_bytes=size_bytes,
                    cutoff_met=True,
                )
                db.upsert_library_file(file_model)

                with self._lock:
                    self._status["files_migrated"] += 1

            # -------------------------------------------------------------
            # 5. Finalize migration & optional mode switch
            # -------------------------------------------------------------
            if self._stop_event.is_set():
                with self._lock:
                    self._status["status"] = "cancelled"
                    self._status["is_migrating"] = False
                    self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                return dict(self._status)

            if auto_switch_mode:
                logger.info("LidarrMigration: Automatically switching library_mode to 'native'.")
                self._switch_to_native(db)

            with self._lock:
                self._status["status"] = "completed"
                self._status["is_migrating"] = False
                self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
            # Imported artists and albums have no thumbnails yet; generate them now instead of at the next daily pass.
            from trackseerr import art_pipeline

            art_pipeline.request_backfill_after_event(db, "Lidarr library import")
            return dict(self._status)

        except Exception as exc:
            logger.exception("LidarrMigration: Unhandled exception during migration: %s", exc)
            with self._lock:
                self._status["status"] = "failed"
                self._status["error"] = str(exc)
                self._status["is_migrating"] = False
                self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
            return dict(self._status)


    @staticmethod
    def _switch_to_native(db: Database) -> None:
        """Goes through the shared switch guard; a refusal leaves the import successful and tells the admin."""
        try:
            switch_mode(db, MODE_NATIVE, source="lidarr_import")
        except SwitchRefused as refused:
            logger.warning(
                "LidarrMigration: import finished but the library manager was not switched to TrackSeerr: %s",
                refused.reason,
            )
            try:
                db.record_event(
                    "library_manager_switch_skipped",
                    "Lidarr import finished, but the library manager could not be switched to TrackSeerr "
                    f"({refused.reason}) Switch it manually in Settings -> General when the work has finished.",
                    source="lidarr_import",
                    severity="warning",
                    details={"reason": refused.reason},
                )
            except sqlite3.Error as exc:
                logger.warning("LidarrMigration: could not record switch-skipped event: %s", safe_exc(exc))
        except sqlite3.Error as exc:
            logger.error("LidarrMigration: could not switch the library manager: %s", safe_exc(exc))


lidarr_migration_job = LidarrMigrationJob()

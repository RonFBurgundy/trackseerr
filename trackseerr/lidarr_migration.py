"""Thread-safe Lidarr migration engine.

Imports artists, albums, tracks, and physical audio files from Lidarr REST API
into TrackSeerr native library catalog tables (library_artists, library_albums,
library_tracks, library_files) and updates media management library mode.
"""

import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from trackseerr.album_years import plausible_year
from trackseerr.clients.lidarr import LidarrApiError, LidarrClient
from trackseerr.models import (
    LibraryAlbum,
    LibraryArtist,
    LibraryFile,
    LibraryTrack,
)
from trackseerr.job_tracker import track_job
from trackseerr.lidarr_import_report import ImportReport, write_report
from trackseerr.library_manager import MODE_NATIVE, SwitchRefused, switch_mode
from trackseerr.redaction import safe_exc
from trackseerr.storage import Database, clean_library_name
from trackseerr.task_manager import TRIGGER_MANUAL, record_task_run

logger = logging.getLogger(__name__)


def _lidarr_image_url(img: dict[str, Any]) -> Optional[str]:
    """Return a fetchable absolute art URL: Lidarr's ``remoteUrl``, else an absolute ``url``; never a local path."""
    for key in ("remoteUrl", "url"):
        value = img.get(key)
        if isinstance(value, str) and value.strip().lower().startswith(("http://", "https://")):
            return value.strip()
    return None


class _MigrationCancelled(Exception):
    """Raised inside the per-artist loop when the cancel event is set."""


@dataclass
class _MigrationRun:
    """Encapsulates cross-phase state during Lidarr library migration."""

    db: Database
    lidarr_client: LidarrClient
    lidarr_artist_map: dict[Any, str] = field(default_factory=dict)
    lidarr_album_map: dict[Any, str] = field(default_factory=dict)
    lidarr_track_map: dict[Any, str] = field(default_factory=dict)
    lidarr_trackfile_map: dict[Any, str] = field(default_factory=dict)
    lidarr_artist_monitored: dict[Any, bool] = field(default_factory=dict)
    lidarr_album_monitored: dict[Any, bool] = field(default_factory=dict)
    report: ImportReport = field(default_factory=ImportReport)


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
            "artists_total": 0,
            "artists_processed": 0,
            "artists_migrated": 0,
            "albums_migrated": 0,
            "tracks_migrated": 0,
            "files_migrated": 0,
            "status": "idle",  # "idle" | "running" | "completed" | "cancelled" | "failed"
            "error": None,
            "started_at": None,
            "completed_at": None,
            "report_name": None,
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
        config_dir: Optional[str] = None,
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
                target=self._run_recorded,
                args=(db, lidarr_client, auto_switch_mode, config_dir),
                daemon=True,
                name="LidarrMigrationThread",
            )
            self._thread.start()
            return True

    def _run_recorded(
        self, db: Database, lidarr_client: LidarrClient, auto_switch_mode: bool, config_dir: Optional[str] = None
    ) -> None:
        """Thread body: runs the migration inside a task-run record and a live job entry."""
        with record_task_run(db, "lidarr_migration", TRIGGER_MANUAL) as run, track_job(
            "lidarr_migration", "Lidarr library import"
        ) as job:
            result = self.run_migration(db, lidarr_client, auto_switch_mode, config_dir)
            summary = (
                f"{result.get('artists_migrated', 0)} artists, {result.get('albums_migrated', 0)} albums, "
                f"{result.get('tracks_migrated', 0)} tracks, {result.get('files_migrated', 0)} files"
            )
            status = result.get("status")
            if status == "failed":
                run.failed = job.failed = str(result.get("error") or "Lidarr import failed")
            elif status == "cancelled":
                run.cancelled = job.cancelled = True
                run.message = summary
            else:
                run.message = summary
                job.update_message(summary)

    def progress_snapshot(self) -> tuple[int, int, str]:
        """Returns (artists_processed, artists_total, message) for the task list."""
        with self._lock:
            st = self._status
            message = (
                f"{st['artists_migrated']} artists · {st['albums_migrated']} albums · "
                f"{st['tracks_migrated']} tracks · {st['files_migrated']} files"
            )
            return int(st["artists_processed"]), int(st["artists_total"]), message

    def _migrate_one_artist(self, run: _MigrationRun, artist: dict[str, Any]) -> None:
        """Ingests a single raw Lidarr artist dictionary."""
        artist_name = (artist.get("artistName") or "Unknown Artist").strip()
        lidarr_art_id = artist.get("id")
        foreign_artist_id = (
            str(artist["foreignArtistId"]) if artist.get("foreignArtistId") is not None else None
        )
        path = artist.get("path")
        monitored = bool(artist.get("monitored", True))

        existing = run.db.get_library_artist_by_name(artist_name)
        artist_id = str(existing["id"]) if existing else str(uuid.uuid4())

        image_url: Optional[str] = None
        banner_url: Optional[str] = None
        fanart_url: Optional[str] = None
        for img in (artist.get("images") or []):
            if not isinstance(img, dict):
                continue
            ctype = str(img.get("coverType") or "").strip().lower()
            url_str = _lidarr_image_url(img)
            if not url_str:
                continue
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
            # Per-album/track flags are copied as-is; "future" keeps the later artist refresh from monitoring the
            # back catalog, while Lidarr's own "none" is honoured.
            monitor_option="none" if artist.get("monitorNewItems") == "none" else "future",
            image_url=image_url,
            banner_url=banner_url,
        )
        upserted = run.db.upsert_library_artist(artist_model)
        new_artist_id = str(upserted["id"])
        run.report.add_artist(artist, new_artist_id)

        if lidarr_art_id is not None:
            run.lidarr_artist_map[lidarr_art_id] = new_artist_id
            run.lidarr_artist_monitored[lidarr_art_id] = monitored

        with self._lock:
            self._status["artists_migrated"] += 1

    def _migrate_artists(self, run: _MigrationRun, raw_artists: list[dict[str, Any]]) -> bool:
        """Ingests raw Lidarr artists into the native catalog."""
        with self._lock:
            self._status["artists_total"] = len(raw_artists)
        for artist in raw_artists:
            if self._stop_event.is_set():
                logger.info("LidarrMigration: Cancellation requested during artist ingestion.")
                with self._lock:
                    self._status["status"] = "cancelled"
                    self._status["is_migrating"] = False
                    self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                return False

            self._migrate_one_artist(run, artist)

        return True

    def _migrate_one_album(self, run: _MigrationRun, album: dict[str, Any]) -> None:
        """Ingests a single raw Lidarr album dictionary."""
        lidarr_album_id = album.get("id")
        lidarr_art_id = album.get("artistId")
        mapped_artist_id = run.lidarr_artist_map.get(lidarr_art_id)

        if not mapped_artist_id:
            artist_name = album.get("artistName")
            if artist_name:
                existing_art = run.db.get_library_artist_by_name(artist_name)
                if existing_art:
                    mapped_artist_id = str(existing_art["id"])

        if not mapped_artist_id:
            logger.warning(
                "LidarrMigration: Skipping album '%s', artistId %s could not be resolved.",
                album.get("title"),
                lidarr_art_id,
            )
            run.report.skip(
                "album",
                "artist_unresolved",
                {"title": album.get("title"), "lidarr_album_id": lidarr_album_id, "lidarr_artist_id": lidarr_art_id},
            )
            return

        album_title = (album.get("title") or "Unknown Album").strip()
        foreign_album_id = (
            str(album["foreignAlbumId"]) if album.get("foreignAlbumId") is not None else None
        )
        release_date = album.get("releaseDate")
        year = plausible_year(album.get("year"))
        if year is None:
            year = plausible_year(release_date)

        monitored = bool(album.get("monitored", True))
        album_path = album.get("path")

        existing_album = (
            run.db.get_library_album_by_release_group_id(foreign_album_id) if foreign_album_id else None
        ) or run.db.get_library_album_by_title(
            mapped_artist_id, album_title, release_group_id=foreign_album_id
        )
        album_id = str(existing_album["id"]) if existing_album else str(uuid.uuid4())

        cover_url: Optional[str] = None
        for img in (album.get("images") or []):
            if not isinstance(img, dict):
                continue
            ctype = str(img.get("coverType") or "").strip().lower()
            url_str = _lidarr_image_url(img)
            if url_str and ctype == "cover":
                cover_url = url_str
                break

        album_type = str(album.get("albumType") or "").strip().lower() or "album"
        secondary_raw = album.get("secondaryTypes")
        secondary_types: Optional[list[str]] = None
        if isinstance(secondary_raw, list):
            secondary_types = [
                str(t.get("name") if isinstance(t, dict) else t).strip()
                for t in secondary_raw
                if (t.get("name") if isinstance(t, dict) else t)
            ]

        album_model = LibraryAlbum(
            id=album_id,
            artist_id=mapped_artist_id,
            title=album_title,
            album_type=album_type,
            secondary_types=secondary_types,
            foreign_album_id=foreign_album_id,
            mb_release_group_id=foreign_album_id,
            release_date=release_date,
            year=year,
            monitored=monitored,
            path=album_path,
            cover_url=cover_url,
        )
        upserted_album = run.db.upsert_library_album(album_model)
        new_album_id = str(upserted_album["id"])

        if lidarr_album_id is not None:
            run.lidarr_album_map[lidarr_album_id] = new_album_id
            run.lidarr_album_monitored[lidarr_album_id] = monitored
        run.report.album(lidarr_art_id, monitored)

        with self._lock:
            self._status["albums_migrated"] += 1

    def _migrate_one_track(self, run: _MigrationRun, track: dict[str, Any]) -> None:
        """Ingests a single raw Lidarr track dictionary."""
        lidarr_track_id = track.get("id")
        lidarr_alb_id = track.get("albumId")
        lidarr_art_id = track.get("artistId")

        mapped_album_id = run.lidarr_album_map.get(lidarr_alb_id)
        mapped_artist_id = run.lidarr_artist_map.get(lidarr_art_id)

        if not mapped_artist_id and mapped_album_id:
            alb_row = run.db.get_library_album(mapped_album_id)
            if alb_row:
                mapped_artist_id = str(alb_row.get("artist_id"))

        if not mapped_album_id or not mapped_artist_id:
            logger.warning(
                "LidarrMigration: Skipping track '%s', unresolved albumId %s or artistId %s.",
                track.get("title"),
                lidarr_alb_id,
                lidarr_art_id,
            )
            run.report.skip(
                "track",
                "album_or_artist_unresolved",
                {
                    "title": track.get("title"),
                    "lidarr_track_id": lidarr_track_id,
                    "lidarr_album_id": lidarr_alb_id,
                    "lidarr_artist_id": lidarr_art_id,
                },
            )
            return

        track_title = (track.get("title") or "Unknown Track").strip()
        track_number = 1
        for raw_number in (track.get("trackNumber"), track.get("absoluteTrackNumber")):
            try:
                track_number = int(raw_number)
                break
            except (ValueError, TypeError):
                continue
        try:
            disc_number = int(track.get("mediumNumber") or track.get("discNumber") or 1)
        except (ValueError, TypeError):
            disc_number = 1

        duration_ms = track.get("duration")
        duration_seconds: Optional[float] = None
        if duration_ms is not None:
            try:
                duration_seconds = float(duration_ms) / 1000.0
            except (ValueError, TypeError):
                pass

        # Lidarr tracks carry no monitored flag; they follow album and artist.
        monitored = run.lidarr_album_monitored.get(lidarr_alb_id, False) and run.lidarr_artist_monitored.get(
            lidarr_art_id, True
        )
        foreign_track_id = (
            str(track["foreignTrackId"]) if track.get("foreignTrackId") is not None else None
        )

        # Lidarr's data is authoritative and complete: identity is the foreign track id, never a title match.
        existing_track = self._find_existing_track(
            run.db, mapped_album_id, foreign_track_id, track_title, track_number, disc_number
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
            mb_recording_id=(
                str(track["foreignRecordingId"]) if track.get("foreignRecordingId") is not None else None
            ),
        )
        upserted_track = run.db.upsert_library_track(track_model)
        new_track_id = str(upserted_track["id"])

        if lidarr_track_id is not None:
            run.lidarr_track_map[lidarr_track_id] = new_track_id

        lidarr_trackfile_id = track.get("trackFileId")
        if isinstance(lidarr_trackfile_id, int) and lidarr_trackfile_id:
            run.lidarr_trackfile_map[lidarr_trackfile_id] = new_track_id
        run.report.track(lidarr_art_id, monitored, lidarr_trackfile_id)

        with self._lock:
            self._status["tracks_migrated"] += 1

    @staticmethod
    def _find_existing_track(
        db: Database,
        album_id: str,
        foreign_track_id: Optional[str],
        title: str,
        track_number: int,
        disc_number: int,
    ) -> Optional[dict[str, Any]]:
        """Lidarr's identity for a track is its foreign id; without one only an exact disc + number + title matches."""
        if foreign_track_id:
            return db.get_library_track_by_foreign_id(foreign_track_id, album_id=album_id)
        row = db.get_library_track_by_title(album_id, title, track_number, disc_number=disc_number)
        if row and (
            int(row["disc_number"]) == disc_number
            and int(row["track_number"]) == track_number
            and clean_library_name(row["title"]) == clean_library_name(title)
        ):
            return row
        return None

    def _migrate_one_track_file(self, run: _MigrationRun, tf: dict[str, Any]) -> None:
        """Ingests a single raw Lidarr track file dictionary."""
        mapped_track_id = run.lidarr_trackfile_map.get(tf.get("id"))
        if not mapped_track_id:
            logger.debug(
                "LidarrMigration: Track file %s ('%s') matches no imported track, skipping.",
                tf.get("id"),
                tf.get("path"),
            )
            run.report.skip(
                "track_file",
                "no_matching_track",
                {"lidarr_trackfile_id": tf.get("id"), "path": tf.get("path")},
            )
            return

        file_path = tf.get("path")
        if not file_path:
            run.report.skip("track_file", "no_path", {"lidarr_trackfile_id": tf.get("id")})
            return

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

        existing_file = run.db.get_library_file_by_path(file_path)
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
        run.db.upsert_library_file(file_model)
        run.report.file_linked(tf.get("id"))

        with self._lock:
            self._status["files_migrated"] += 1

    def run_migration(
        self,
        db: Database,
        lidarr_client: LidarrClient,
        auto_switch_mode: bool = True,
        config_dir: Optional[str] = None,
    ) -> dict[str, Any]:
        """Synchronously executes the Lidarr library migration loop and writes its reconciliation report."""
        report = ImportReport()
        result = self._execute_migration(db, lidarr_client, auto_switch_mode, report)
        if not report.active:
            return result
        return self._finish_report(db, report, result, auto_switch_mode, config_dir)

    def _finish_report(
        self,
        db: Database,
        report: ImportReport,
        result: dict[str, Any],
        auto_switch_mode: bool,
        config_dir: Optional[str],
    ) -> dict[str, Any]:
        """Builds and writes the report for a finished run; a report failure never fails the import."""
        if not config_dir:
            return result
        try:
            data = report.finalize(db, result.get("status", ""), result.get("error"), result.get("started_at"), auto_switch_mode)
        except sqlite3.Error as exc:
            logger.warning("LidarrMigration: could not build the import report: %s", safe_exc(exc))
            return result
        path = write_report(data, config_dir)
        if path is None:
            return result
        with self._lock:
            self._status["report_name"] = path.name
            return dict(self._status)

    def _execute_migration(
        self,
        db: Database,
        lidarr_client: LidarrClient,
        auto_switch_mode: bool,
        report: ImportReport,
    ) -> dict[str, Any]:
        """The migration loop itself; marks ``report.active`` once the run really starts."""
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
            self._status["artists_processed"] = 0
            self._status["albums_migrated"] = 0
            self._status["tracks_migrated"] = 0
            self._status["files_migrated"] = 0
            self._status["error"] = None
            if not self._status.get("started_at"):
                self._status["started_at"] = datetime.now(timezone.utc).isoformat()
            self._status["completed_at"] = None

        report.active = True
        run = _MigrationRun(
            db=db,
            lidarr_client=lidarr_client,
            report=report,
        )

        try:
            logger.info("LidarrMigration: Fetching artists from Lidarr...")
            raw_artists = lidarr_client.get_all_artists()
            if not raw_artists:
                # get_all_artists() swallows failures into [], so confirm with a strict call: an outage must fail the run.
                raw_artists = lidarr_client._get_list("artist", {}, 60.0)
            if not self._migrate_artists(run, raw_artists):
                return dict(self._status)

            for lidarr_artist in raw_artists:
                if self._stop_event.is_set():
                    return self._mark("cancelled")
                self._migrate_artist_catalog(run, lidarr_artist)
                with self._lock:
                    self._status["artists_processed"] += 1

            if self._stop_event.is_set():
                return self._mark("cancelled")

            if auto_switch_mode:
                logger.info("LidarrMigration: Automatically switching library_mode to 'native'.")
                self._switch_to_native(db)

            self._mark("completed")
            # Imported artists and albums have no thumbnails yet; generate them now instead of at the next daily pass.
            from trackseerr import art_pipeline

            art_pipeline.request_backfill_after_event(db, "Lidarr library import")
            return dict(self._status)

        except _MigrationCancelled:
            return self._mark("cancelled")
        except LidarrApiError as exc:
            logger.error("LidarrMigration: aborted, library manager left unchanged: %s", safe_exc(exc))
            return self._mark("failed", str(exc))
        except Exception as exc:
            logger.exception("LidarrMigration: Unhandled exception during migration: %s", exc)
            return self._mark("failed", str(exc))

    def _mark(self, status: str, error: Optional[str] = None) -> dict[str, Any]:
        """Records a terminal status (completed, cancelled or failed) and returns a copy of the status dict."""
        with self._lock:
            self._status["status"] = status
            if error is not None:
                self._status["error"] = error
            self._status["is_migrating"] = False
            self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
            return dict(self._status)

    @staticmethod
    def _unmonitor_albums_not_in_lidarr(
        run: _MigrationRun, lidarr_artist_id: Any, lidarr_albums: list[dict[str, Any]]
    ) -> None:
        """Unmonitors the artist's monitored albums that Lidarr does not have (never deletes)."""
        mapped_artist_id = run.lidarr_artist_map.get(lidarr_artist_id)
        if not mapped_artist_id:
            return
        imported = {
            run.lidarr_album_map[a.get("id")] for a in lidarr_albums if a.get("id") in run.lidarr_album_map
        }
        extra = [
            str(row["id"])
            for row in run.db.list_library_albums(artist_id=mapped_artist_id, monitored_only=True, limit=100000)
            if str(row["id"]) not in imported
        ]
        if extra:
            run.db.bulk_set_albums_monitored(extra, False, cascade_tracks=True)

    def _migrate_artist_catalog(self, run: _MigrationRun, lidarr_artist: dict[str, Any]) -> None:
        """Fetches and ingests one artist's albums, their tracks and the artist's files; LidarrApiError on a failed fetch."""
        artist_id = lidarr_artist.get("id")
        if artist_id is None:
            return
        artist_name = lidarr_artist.get("artistName") or "Unknown Artist"
        client = run.lidarr_client
        try:
            albums = client.fetch_artist_albums(artist_id, timeout=60.0)
            for album in albums:
                if self._stop_event.is_set():
                    raise _MigrationCancelled()
                self._migrate_one_album(run, album)
            self._unmonitor_albums_not_in_lidarr(run, artist_id, albums)
            if self._stop_event.is_set():
                raise _MigrationCancelled()
            # One call returns every track of the artist (each row carries its albumId) instead of one per album.
            for index, track in enumerate(client.fetch_artist_tracks_strict(artist_id, timeout=60.0)):
                if index % 200 == 0 and self._stop_event.is_set():
                    raise _MigrationCancelled()
                self._migrate_one_track(run, track)
            if self._stop_event.is_set():
                raise _MigrationCancelled()
            for track_file in client.fetch_artist_track_files(artist_id):
                self._migrate_one_track_file(run, track_file)
        except _MigrationCancelled:
            raise
        except LidarrApiError as exc:
            if f"artist '{artist_name}'" in str(exc):
                raise
            raise LidarrApiError(f"{exc} (artist '{artist_name}')") from exc


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

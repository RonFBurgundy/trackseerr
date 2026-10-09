"""Thread-safe recursive filesystem scanner and library ingestion engine.

Scans local audio storage non-destructively, extracts Mutagen metadata,
normalizes artists, albums, tracks, and files into SQLite catalog tables,
and evaluates quality profile cutoff compliance.
"""

import contextvars
import concurrent.futures
from dataclasses import dataclass, field
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from trackseerr import art_pipeline, delay_gate
from trackseerr.acquisition_coordinator import _to_quality_profile
from trackseerr.library import (
    AUDIO_EXTENSIONS,
    find_folder_art,
    inspect_audio_file,
    parse_filename_track,
    primary_artist,
    resolve_album_artist,
)
from trackseerr.library_monitoring import album_monitored_for_option, normalize_album_type
from trackseerr.models import (
    LibraryAlbum,
    LibraryArtist,
    LibraryFile,
    LibraryTrack,
)
from trackseerr.quality import evaluate_release, parse_release_title
from trackseerr.job_tracker import track_job
from trackseerr.task_manager import TRIGGER_MANUAL, record_task_run
from trackseerr.library_manager import ModeChanged, run_guarded
from trackseerr.media_servers import as_media_server
from trackseerr.item_history import TRIGGER_SCAN, GrabTrigger, emit, provenance
from trackseerr.recycle_bin import is_excluded_entry, library_excluded_paths, prune_excluded_dirs
from trackseerr.redaction import redact_text, safe_exc
from trackseerr.storage import Database

logger = logging.getLogger(__name__)


def _inspect_audio_file_worker(file_path: Path, root: Path) -> tuple[Path, dict[str, Any]]:
    """Mutagen worker function for concurrent thread pool inspection."""
    parent = file_path.parent
    fallback_album = parent.name if parent != root else "Unknown Album"
    grandparent = parent.parent
    fallback_artist = (
        grandparent.name
        if (parent != root and grandparent != root and grandparent != parent)
        else "Unknown Artist"
    )
    fallback_title, fallback_track_number = parse_filename_track(file_path.stem)

    try:
        metadata = inspect_audio_file(file_path)
    except Exception as exc:
        logger.warning(
            "LibraryScanner: Mutagen extraction failed for %s: %s. Using path fallbacks.",
            file_path,
            exc,
        )
        metadata = {
            "title": fallback_title,
            "artist": fallback_artist,
            "album": fallback_album,
            "track_number": fallback_track_number,
            "disc_number": 1,
            "codec": file_path.suffix.lstrip(".").upper() or "UNKNOWN",
            "bitrate": None,
            "sample_rate": None,
            "bits_per_sample": None,
            "duration": 0.0,
            "quality_full": file_path.suffix.lstrip(".").upper() or "UNKNOWN",
            "file_path": str(file_path),
            "musicbrainz_artistid": None,
            "musicbrainz_albumartistid": None,
            "artists": [],
            "musicbrainz_albumid": None,
            "musicbrainz_releasegroupid": None,
            "musicbrainz_trackid": None,
            "isrc": None,
        }
    return file_path, metadata


def extract_embedded_cover_art(audio_file_path: Path, output_dir: Path) -> Optional[Path]:
    """Extracts embedded cover artwork from FLAC, MP3, or MP4/M4A audio files to cover.jpg."""
    try:
        import mutagen

        audio = mutagen.File(str(audio_file_path))
        if audio is None:
            return None

        picture_data: Optional[bytes] = None
        # 1. FLAC / OGG pictures
        if hasattr(audio, "pictures") and audio.pictures:
            picture_data = getattr(audio.pictures[0], "data", None)

        # 2. MP3 APIC tags
        if not picture_data and hasattr(audio, "tags") and audio.tags:
            getall_fn = getattr(audio.tags, "getall", None)
            if callable(getall_fn):
                apics = getall_fn("APIC")
                if apics and hasattr(apics[0], "data"):
                    picture_data = apics[0].data

        # 3. MP4 / M4A covr atom
        if not picture_data and hasattr(audio, "tags") and audio.tags:
            covr = audio.tags.get("covr")
            if covr and len(covr) > 0:
                picture_data = bytes(covr[0])

        if picture_data:
            out_file = output_dir / "cover.jpg"
            out_file.write_bytes(picture_data)
            return out_file
    except Exception as exc:
        logger.debug("LibraryScanner: extract_embedded_cover_art failed for %s: %s", audio_file_path, exc)
    return None


@dataclass
class _ScanRun:
    """Encapsulates cross-phase state during media library filesystem scanning."""

    db: Database
    root: Path
    media_settings: dict[str, Any]
    scan_monitor_option: str
    prune_missing: bool
    plex_client: Optional[Any]
    audio_files: list[Path] = field(default_factory=list)
    existing_files_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    artist_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    album_cache: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    track_cache: dict[tuple[str, str, Optional[int]], dict[str, Any]] = field(default_factory=dict)
    track_id_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    artist_tag_cache: dict[str, list[str]] = field(default_factory=dict)
    quality_profile_cache: dict[Optional[str], Any] = field(default_factory=dict)
    art_registered_albums: set[str] = field(default_factory=set)
    art_registered_artists: set[str] = field(default_factory=set)
    newly_created_artist_ids: list[str] = field(default_factory=list)


class LibraryScanner:
    """Thread-safe media library filesystem scanner and ingestion engine."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._status: dict[str, Any] = self._default_status()

    @staticmethod
    def _default_status() -> dict[str, Any]:
        return {
            "is_scanning": False,
            "total_files_found": 0,
            "processed_files": 0,
            "artists_created": 0,
            "albums_created": 0,
            "tracks_created": 0,
            "files_indexed": 0,
            "files_pruned": 0,
            "current_file": None,
            "status": "idle",  # "idle" | "scanning" | "completed" | "cancelled" | "failed" | "skipped"
            "error": None,
            "started_at": None,
            "completed_at": None,
        }

    def is_running(self) -> bool:
        """Returns True if a library scan is currently executing."""
        with self._lock:
            return bool(self._status.get("is_scanning", False))

    def get_status(self) -> dict[str, Any]:
        """Returns a snapshot copy of the current scanner status."""
        with self._lock:
            return dict(self._status)

    def cancel_scan(self) -> dict[str, Any]:
        """Signals the active scan to stop and returns the current status."""
        with self._lock:
            self._stop_event.set()
            if not self._status.get("is_scanning", False) and self._status.get("status") == "idle":
                self._status["status"] = "cancelled"
            return dict(self._status)

    @staticmethod
    def _register_art(db: Database, kind: str, row: dict[str, Any], settings: dict[str, Any]) -> None:
        """Versions the art the routes will serve for ``row`` and queues its thumbnails; art bookkeeping must never
        fail a scan."""
        try:
            art_pipeline.register_served_art(db, kind, row, settings)
        except Exception as exc:
            logger.warning("Could not register %s art for %s: %s", kind, row.get("id"), exc)

    def start_scan(
        self,
        db: Database,
        root_folder: Optional[str] = None,
        prune_missing: bool = False,
        plex_client: Optional[Any] = None,
    ) -> bool:
        """Starts a background scanning daemon thread if not already running."""
        with self._lock:
            if self._status.get("is_scanning", False):
                logger.warning("LibraryScanner: Scan already running, cannot start another.")
                return False

            self._stop_event.clear()
            self._status = self._default_status()
            self._status["is_scanning"] = True
            self._status["status"] = "scanning"
            self._status["started_at"] = datetime.now(timezone.utc).isoformat()

            self._thread = threading.Thread(
                target=self._run_background_scan,
                args=(db, root_folder, prune_missing, plex_client),
                daemon=True,
                name="LibraryScannerThread",
            )
            self._thread.start()
            return True

    def _run_background_scan(
        self,
        db: Database,
        root_folder: Optional[str],
        prune_missing: bool,
        plex_client: Optional[Any],
    ) -> None:
        """Entrypoint for background scanning thread."""
        try:
            with record_task_run(db, "filesystem_scan", TRIGGER_MANUAL) as run, track_job(
                "filesystem_scan", "Media Library Disk Scanner"
            ) as job:
                result = self.scan(
                    db=db,
                    root_folder=root_folder,
                    prune_missing=prune_missing,
                    plex_client=plex_client,
                    _is_background=True,
                )
                outcome = str(result.get("status") or "")
                job.message = run.message = outcome or None
                if outcome == "failed":
                    err = redact_text(str(result.get("error") or ""))
                    job.failed = run.failed = f"Scan failed: {err}" if err else "Scan failed"
                elif outcome == "cancelled":
                    job.cancelled = run.cancelled = True
            if outcome == "completed":
                # A scan can add artists and albums; give them thumbnails without waiting for the daily pass.
                art_pipeline.request_backfill_after_event(db, "library scan")
        except Exception as exc:
            logger.error("LibraryScanner: Unhandled exception in background scan thread: %s", safe_exc(exc))
            logger.debug("LibraryScanner background scan traceback", exc_info=True)
            with self._lock:
                self._status["is_scanning"] = False
                self._status["status"] = "failed"
                self._status["error"] = safe_exc(exc)
                self._status["completed_at"] = datetime.now(timezone.utc).isoformat()

    def scan(
        self,
        db: Database,
        root_folder: Optional[str] = None,
        prune_missing: bool = False,
        plex_client: Optional[Any] = None,
        _is_background: bool = False,
    ) -> dict[str, Any]:
        """Synchronously scans media root folder, indexes audio files, and optionally prunes missing files.

        Runs under the library-manager guard, so the mode cannot be switched while a scan is in progress.
        """
        try:
            with provenance(GrabTrigger(TRIGGER_SCAN, label="Library scan")):
                return run_guarded(
                    db, lambda: self._scan(db, root_folder, prune_missing, plex_client, _is_background)
                )
        except ModeChanged:
            logger.warning("LibraryScanner: library manager changed repeatedly; scan skipped")
            with self._lock:
                self._status["status"] = "skipped"
                self._status["is_scanning"] = False
                self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                return dict(self._status)

    def _prepare_scan(
        self,
        db: Database,
        root_folder: Optional[str],
        prune_missing: bool,
        plex_client: Optional[Any],
    ) -> Optional[_ScanRun]:
        """Validates scan prerequisites, collects audio files, and initializes run state."""
        # Check immediate cancellation
        if self._stop_event.is_set():
            logger.info("LibraryScanner: Stop flag set before start.")
            with self._lock:
                self._status["status"] = "cancelled"
                self._status["is_scanning"] = False
                self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                return None

        # Step a: Check library mode safeguard
        media_settings = db.get_media_management_settings()
        if media_settings.get("library_mode") == "lidarr":
            logger.debug("LibraryScanner: library manager is Lidarr; skipping filesystem scan")
            with self._lock:
                self._status["status"] = "skipped"
                self._status["is_scanning"] = False
                self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                return None

        scan_monitor_option = str(media_settings.get("scan_monitor_option") or "existing")

        # Step b: Determine and validate root folder
        root_path_str = root_folder or media_settings.get("root_folder_path") or "/music"
        root = Path(root_path_str).resolve()
        if not root.exists():
            logger.error("LibraryScanner: Root folder does not exist: %s", root)
            with self._lock:
                self._status["status"] = "failed"
                self._status["error"] = "Root folder does not exist"
                self._status["is_scanning"] = False
                self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                return None

        try:
            db.record_event(
                "scan_started",
                f"Filesystem scan started on {root}",
                source="LibraryScanner",
                severity="info",
            )
        except Exception as e:
            logger.warning("LibraryScanner: Failed to record scan_started event: %s", e)

        # Step c: Collect audio files
        audio_files: list[Path] = []
        excluded = library_excluded_paths(media_settings)
        try:
            # os.walk with in-place pruning: recycle/quarantine folders are never descended into, so a large
            # bin adds no scan time. Symlinked directories are listed but not followed (as rglob did).
            for walk_root, dirnames, filenames in os.walk(root, followlinks=False):
                if self._stop_event.is_set():
                    break
                dirnames[:] = prune_excluded_dirs(walk_root, dirnames, excluded)
                for fname in filenames:
                    entry = Path(walk_root, fname)
                    try:
                        if is_excluded_entry(entry, root, excluded):
                            continue  # recycled/quarantined files (new and legacy folders) are not library content
                        if entry.is_file() and entry.suffix.lower() in AUDIO_EXTENSIONS:
                            audio_files.append(entry.resolve())
                    except OSError as oe:
                        logger.warning("LibraryScanner: Cannot access entry %s: %s", entry, oe)
        except OSError as oe:
            logger.warning("LibraryScanner: Directory walk error in %s: %s", root, oe)

        audio_files.sort()
        with self._lock:
            self._status["total_files_found"] = len(audio_files)

        if self._stop_event.is_set():
            with self._lock:
                self._status["status"] = "cancelled"
                self._status["is_scanning"] = False
                self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                return None

        existing_files_map = {
            f["file_path"]: f for f in db.list_library_files(limit=100000)
        }
        return _ScanRun(
            db=db,
            root=root,
            media_settings=media_settings,
            scan_monitor_option=scan_monitor_option,
            prune_missing=prune_missing,
            plex_client=plex_client,
            audio_files=audio_files,
            existing_files_map=existing_files_map,
        )

    def _inspect_chunk(
        self, run: _ScanRun, chunk_files: list[Path]
    ) -> tuple[dict[Path, dict[str, Any]], dict[Path, dict[str, Any]]]:
        """Separates cached metadata items and inspects uncached files concurrently."""
        cached_metadata_items: dict[Path, dict[str, Any]] = {}
        files_to_inspect: list[Path] = []

        for fpath in chunk_files:
            fpath_str = str(fpath)
            existing = run.existing_files_map.get(fpath_str)
            f_size = 0
            try:
                f_size = fpath.stat().st_size
            except OSError:
                pass

            if existing and f_size > 0 and existing.get("size_bytes") == f_size:
                cached_metadata_items[fpath] = {
                    "file_path": fpath_str,
                    "codec": existing.get("codec") or fpath.suffix.lstrip(".").upper() or "UNKNOWN",
                    "bitrate": existing.get("bitrate"),
                    "sample_rate": existing.get("sample_rate"),
                    "bits_per_sample": existing.get("bits_per_sample"),
                    "quality_full": existing.get("quality_name"),
                    "size_bytes": f_size,
                    "cached_track_id": existing.get("track_id"),
                    "cached_file_id": existing.get("id"),
                    "cutoff_met": existing.get("cutoff_met", True),
                    "quality_name": existing.get("quality_name", "Unknown"),
                    "from_cache": True,
                }
            else:
                files_to_inspect.append(fpath)

        # Multi-threaded Mutagen worker pool for files needing inspection
        inspected_metadata_items: dict[Path, dict[str, Any]] = {}
        if files_to_inspect:
            max_workers = min(8, os.cpu_count() or 4)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_file = {
                    executor.submit(_inspect_audio_file_worker, fp, run.root): fp
                    for fp in files_to_inspect
                }
                for fut in concurrent.futures.as_completed(future_to_file):
                    if self._stop_event.is_set():
                        for f in future_to_file:
                            f.cancel()
                        break
                    try:
                        fpath_res, meta_res = fut.result()
                        inspected_metadata_items[fpath_res] = meta_res
                    except Exception as exc:
                        fp = future_to_file[fut]
                        logger.warning("LibraryScanner: Worker error inspecting %s: %s", fp, exc)

        return cached_metadata_items, inspected_metadata_items

    def _resolve_scan_artist(
        self,
        run: _ScanRun,
        artist_name: str,
        metadata: dict[str, Any],
        parent: Path,
        grandparent: Path,
    ) -> tuple[str, dict[str, Any]]:
        """Finds or creates the library artist record from audio tags or folder paths."""
        mb_artist_id = metadata.get("musicbrainz_artistid")
        artist_row = run.artist_cache.get(artist_name) or run.db.get_library_artist_by_name(artist_name)
        if not artist_row:
            artist_id = str(uuid.uuid4())
            artist_path = (
                str(parent.parent)
                if (parent != run.root and grandparent != run.root and grandparent != parent)
                else str(parent)
            )
            foreign_artist_id = f"musicbrainz:artist:{mb_artist_id}" if mb_artist_id else None
            art_img_url: Optional[str] = None
            if artist_path:
                for art_cand in ("artist.jpg", "artist.png", "folder.jpg"):
                    if (Path(artist_path) / art_cand).is_file():
                        art_img_url = f"/api/library/artists/{artist_id}/image"
                        break
            artist_row = run.db.upsert_library_artist(
                LibraryArtist(
                    id=artist_id,
                    name=artist_name,
                    path=artist_path,
                    monitored=True,
                    monitor_option=run.scan_monitor_option,
                    mbid=mb_artist_id,
                    foreign_artist_id=foreign_artist_id,
                    image_url=art_img_url,
                )
            )
            run.newly_created_artist_ids.append(artist_id)
            with self._lock:
                self._status["artists_created"] += 1
        else:
            need_artist_update = False
            new_mbid = artist_row.get("mbid")
            new_foreign = artist_row.get("foreign_artist_id")
            new_img = artist_row.get("image_url")
            if not new_mbid and mb_artist_id:
                new_mbid = mb_artist_id
                need_artist_update = True
            if not new_foreign and mb_artist_id:
                new_foreign = f"musicbrainz:artist:{mb_artist_id}"
                need_artist_update = True
            if not new_img and artist_row.get("path"):
                for art_cand in ("artist.jpg", "artist.png", "folder.jpg"):
                    if (Path(artist_row["path"]) / art_cand).is_file():
                        new_img = f"/api/library/artists/{artist_row['id']}/image"
                        need_artist_update = True
                        break
            if need_artist_update:
                artist_row = run.db.upsert_library_artist({
                    **artist_row,
                    "mbid": new_mbid,
                    "foreign_artist_id": new_foreign,
                    "image_url": new_img,
                }, preserve_monitoring=True)
        run.artist_cache[artist_name] = artist_row
        artist_id = str(artist_row["id"])
        return artist_id, artist_row

    def _resolve_scan_album(
        self,
        run: _ScanRun,
        artist_id: str,
        artist_row: dict[str, Any],
        album_title: str,
        metadata: dict[str, Any],
        file_path: Path,
        parent: Path,
        year: Optional[int],
        total_tracks: Optional[int],
    ) -> tuple[str, dict[str, Any]]:
        """Finds or creates the library album record and registers local cover art."""
        mb_rg_id = metadata.get("musicbrainz_releasegroupid")
        mb_rel_id = metadata.get("musicbrainz_albumid")
        has_local_cover = False
        if find_folder_art(parent) is not None:
            has_local_cover = True

        if not has_local_cover:
            extracted = extract_embedded_cover_art(file_path, parent)
            if extracted is not None and extracted.is_file():
                has_local_cover = True

        album_key = (artist_id, album_title)
        album_row = run.album_cache.get(album_key) or run.db.get_library_album_by_title(artist_id, album_title)
        if not album_row:
            album_id = str(uuid.uuid4())
            new_album_type = normalize_album_type(
                metadata.get("album_type") or ("single" if total_tracks == 1 else "album")
            )
            new_album_monitored = album_monitored_for_option(
                str(artist_row.get("monitor_option") or run.scan_monitor_option),
                artist_monitored=bool(artist_row.get("monitored", True)),
                album_type=new_album_type,
                has_files=True,
                release_date=metadata.get("release_date"),
                year=year,
                artist_added_at=artist_row.get("created_at"),
            )
            album_path = str(parent)
            album_cover = f"/api/library/albums/{album_id}/cover" if has_local_cover else None
            album_row = run.db.upsert_library_album(
                LibraryAlbum(
                    id=album_id,
                    artist_id=artist_id,
                    title=album_title,
                    year=year,
                    path=album_path,
                    cover_url=album_cover,
                    total_tracks=total_tracks,
                    album_type=new_album_type,
                    release_date=metadata.get("release_date"),
                    monitored=new_album_monitored,
                    mb_release_group_id=mb_rg_id,
                    mb_release_id=mb_rel_id,
                )
            )
            with self._lock:
                self._status["albums_created"] += 1
        else:
            album_id = str(album_row["id"])
            need_album_update = False
            new_rg = album_row.get("mb_release_group_id")
            new_rel = album_row.get("mb_release_id")
            new_cov = album_row.get("cover_url")
            if not new_rg and mb_rg_id:
                new_rg = mb_rg_id
                need_album_update = True
            if not new_rel and mb_rel_id:
                new_rel = mb_rel_id
                need_album_update = True
            if not new_cov and has_local_cover:
                new_cov = f"/api/library/albums/{album_id}/cover"
                need_album_update = True
            if need_album_update:
                album_row = run.db.upsert_library_album({
                    **album_row,
                    "mb_release_group_id": new_rg,
                    "mb_release_id": new_rel,
                    "cover_url": new_cov,
                }, preserve_monitoring=True)
        run.album_cache[album_key] = album_row
        album_id = str(album_row["id"])

        # Folder art found: version it and pre-generate its 250/500 derivatives (once per album per scan).
        if has_local_cover and album_id not in run.art_registered_albums:
            run.art_registered_albums.add(album_id)
            self._register_art(run.db, "album", album_row, run.media_settings)
        if artist_id not in run.art_registered_artists:
            run.art_registered_artists.add(artist_id)
            self._register_art(run.db, "artist", artist_row, run.media_settings)

        return album_id, album_row

    def _resolve_scan_track(
        self,
        run: _ScanRun,
        artist_id: str,
        album_id: str,
        album_row: dict[str, Any],
        track_title: str,
        tagged_track_number: Optional[int],
        track_number: int,
        disc_number: int,
        duration_seconds: Optional[float],
        metadata: dict[str, Any],
    ) -> str:
        """Finds or creates the library track record from audio metadata."""
        mb_rec_id = metadata.get("musicbrainz_trackid")
        track_isrc = metadata.get("isrc")
        track_key = (album_id, track_title, tagged_track_number)
        track_row = run.track_cache.get(track_key) or run.db.get_library_track_by_title(
            album_id, track_title, tagged_track_number
        )
        if not track_row:
            track_id = str(uuid.uuid4())
            track_row = run.db.upsert_library_track(
                LibraryTrack(
                    id=track_id,
                    album_id=album_id,
                    artist_id=artist_id,
                    title=track_title,
                    track_number=int(track_number),
                    disc_number=int(disc_number),
                    duration_seconds=duration_seconds,
                    monitored=bool(album_row.get("monitored", True)),
                    mb_recording_id=mb_rec_id,
                    isrc=track_isrc,
                )
            )
            with self._lock:
                self._status["tracks_created"] += 1
        else:
            need_track_update = False
            new_rec = track_row.get("mb_recording_id")
            new_isrc = track_row.get("isrc")
            if not new_rec and mb_rec_id:
                new_rec = mb_rec_id
                need_track_update = True
            if not new_isrc and track_isrc:
                new_isrc = track_isrc
                need_track_update = True
            if need_track_update:
                track_row = run.db.upsert_library_track({
                    **track_row,
                    "mb_recording_id": new_rec,
                    "isrc": new_isrc,
                }, preserve_monitoring=True)
        run.track_cache[track_key] = track_row
        run.track_id_cache[str(track_row["id"])] = track_row
        return str(track_row["id"])

    def _evaluate_scan_quality(
        self,
        run: _ScanRun,
        artist_id: str,
        artist_row: dict[str, Any],
        file_path: Path,
        file_size: int,
        metadata: dict[str, Any],
    ) -> tuple[bool, str]:
        """Evaluates release quality and cutoff compliance against artist quality profile."""
        cutoff_met = True
        quality_name = str(metadata.get("quality_full") or metadata.get("codec") or "Unknown")
        try:
            qp_id = artist_row.get("quality_profile_id")
            profile_dict = run.quality_profile_cache.get(qp_id)
            if profile_dict is None:
                profile_dict = run.db.get_quality_profile(qp_id) if qp_id else None
                if not profile_dict:
                    profile_dict = run.db.get_default_quality_profile()
                run.quality_profile_cache[qp_id] = profile_dict
            if profile_dict:
                qp = _to_quality_profile(profile_dict)
                quality_input = (
                    metadata.get("quality_full")
                    or metadata.get("codec")
                    or file_path.suffix.lstrip(".").upper()
                )
                parsed = parse_release_title(str(quality_input))
                if parsed.quality == "Unknown" and quality_input:
                    parsed.quality = str(quality_input)
                if artist_id not in run.artist_tag_cache:
                    run.artist_tag_cache[artist_id] = delay_gate.artist_tags(
                        run.db, artist_row.get("name"), artist_id
                    )
                eval_result = evaluate_release(
                    parsed, qp, size_bytes=file_size, artist_tags=run.artist_tag_cache[artist_id]
                )
                # A library file is scored from a bare quality string (its release title is unknown), so
                # its format score is 0 and could never reach ``cutoff_format_score``: judge the quality
                # tier only, i.e. treat the format-score cutoff as met.
                bd = eval_result.breakdown
                cutoff_met = bool(
                    bd.quality_cutoff_met if bd is not None else eval_result.meets_cutoff
                )
                quality_name = eval_result.parsed_quality or str(quality_input)
        except Exception as exc:
            logger.warning(
                "LibraryScanner: Cutoff evaluation error for %s: %s. Defaulting cutoff_met=True.",
                file_path,
                exc,
            )
            cutoff_met = True

        return cutoff_met, quality_name

    def _scan_one_file(
        self,
        run: _ScanRun,
        file_path: Path,
        metadata: dict[str, Any],
        pending_files_to_batch: list[LibraryFile],
    ) -> None:
        """Resolves artist, album, track, quality cutoff, and queues file record for batch insertion."""
        fpath_str = str(file_path)
        file_size = 0
        try:
            file_size = file_path.stat().st_size
        except OSError:
            pass

        parent = file_path.parent
        fallback_album = parent.name if parent != run.root else "Unknown Album"
        grandparent = parent.parent
        fallback_artist = (
            grandparent.name
            if (parent != run.root and grandparent != run.root and grandparent != parent)
            else "Unknown Artist"
        )
        fallback_title, fallback_track_number = parse_filename_track(file_path.stem)

        if metadata.get("from_cache"):
            cached_track_id = metadata.get("cached_track_id")
            track_row = None
            if cached_track_id:
                track_row = run.track_id_cache.get(cached_track_id) or run.db.get_library_track(cached_track_id)
                if track_row:
                    run.track_id_cache[cached_track_id] = track_row

            if track_row:
                track_id = str(track_row["id"])
                cutoff_met = bool(metadata.get("cutoff_met", True))
                quality_name = str(metadata.get("quality_name") or "Unknown")
            else:
                metadata["from_cache"] = False
                if not metadata.get("title"):
                    metadata["title"] = fallback_title
                    metadata["artist"] = fallback_artist
                    metadata["album"] = fallback_album

        if not metadata.get("from_cache"):
            def known_artist(name: str) -> bool:
                return name in run.artist_cache or run.db.get_library_artist_by_name(name) is not None

            artist_name = (
                resolve_album_artist(metadata, known_artist=known_artist)
                or primary_artist(fallback_artist, known_artist)
                or fallback_artist
            )
            album_title = (metadata.get("album") or "").strip() or fallback_album
            track_title = (metadata.get("title") or "").strip() or fallback_title
            # None means "untagged": it is stored as 1 (NOT NULL column) but never used to match tracks.
            tagged_track_number = metadata.get("track_number") or fallback_track_number
            track_number = tagged_track_number or 1
            disc_number = metadata.get("disc_number") or 1
            duration_seconds = metadata.get("duration")
            year = metadata.get("year")
            total_tracks = metadata.get("total_tracks")

            artist_id, artist_row = self._resolve_scan_artist(run, artist_name, metadata, parent, grandparent)
            album_id, album_row = self._resolve_scan_album(
                run, artist_id, artist_row, album_title, metadata, file_path, parent, year, total_tracks
            )
            track_id = self._resolve_scan_track(
                run,
                artist_id,
                album_id,
                album_row,
                track_title,
                tagged_track_number,
                track_number,
                disc_number,
                duration_seconds,
                metadata,
            )
            cutoff_met, quality_name = self._evaluate_scan_quality(
                run, artist_id, artist_row, file_path, file_size, metadata
            )

        # Upsert File
        rel_path = str(file_path.relative_to(run.root))
        file_id = str(metadata.get("cached_file_id") or "")
        if not file_id:
            existing_file = run.existing_files_map.get(fpath_str) or run.db.get_library_file_by_path(fpath_str)
            file_id = str(existing_file["id"]) if existing_file else str(uuid.uuid4())

        lib_file = LibraryFile(
            id=file_id,
            track_id=track_id,
            file_path=fpath_str,
            relative_path=rel_path,
            codec=str(metadata.get("codec") or file_path.suffix.lstrip(".").upper() or "UNKNOWN"),
            bitrate=metadata.get("bitrate"),
            sample_rate=metadata.get("sample_rate"),
            bits_per_sample=metadata.get("bits_per_sample"),
            quality_name=quality_name,
            size_bytes=file_size,
            cutoff_met=cutoff_met,
        )
        pending_files_to_batch.append(lib_file)
        with self._lock:
            self._status["files_indexed"] += 1
            self._status["processed_files"] += 1

    def _process_chunk(self, run: _ScanRun, chunk_files: list[Path]) -> None:
        """Inspects audio metadata and indexes a chunk of files into the library database."""
        cached_metadata_items, inspected_metadata_items = self._inspect_chunk(run, chunk_files)
        if self._stop_event.is_set():
            return

        # Process chunk items and prepare batched records
        pending_files_to_batch: list[LibraryFile] = []
        for file_path in chunk_files:
            if self._stop_event.is_set():
                break

            with self._lock:
                self._status["current_file"] = str(file_path)

            metadata = cached_metadata_items.get(file_path) or inspected_metadata_items.get(file_path)
            if not metadata:
                continue

            self._scan_one_file(run, file_path, metadata, pending_files_to_batch)

        # Batched database write for this chunk
        if pending_files_to_batch:
            run.db.upsert_library_files_batch(pending_files_to_batch)

        logger.info(
            "LibraryScanner: Processed %d/%d files...",
            self._status["processed_files"],
            len(run.audio_files),
        )

    def _finish_scan(self, run: _ScanRun) -> None:
        """Finalizes scan by pruning missing files, notifying media servers, and recording completion."""
        if self._stop_event.is_set():
            with self._lock:
                self._status["status"] = "cancelled"
                self._status["is_scanning"] = False
                self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                return

        # Step e: Prune missing files if enabled and scan was not cancelled
        if run.prune_missing and not self._stop_event.is_set():
            all_files = run.db.list_library_files(limit=10000)
            for row in all_files:
                if self._stop_event.is_set():
                    break
                fpath = row.get("file_path")
                if fpath and not Path(fpath).exists():
                    if row.get("track_id"):
                        emit(
                            run.db, "file_missing", track_id=str(row["track_id"]), dedupe_last=True,
                            message="Scanner found the file gone from disk",
                            details={"path": str(fpath), "quality": row.get("quality_name"), "codec": row.get("codec")},
                            trigger=TRIGGER_SCAN, trigger_label="Library scan",
                        )
                    run.db.delete_library_file(str(row["id"]))
                    with self._lock:
                        self._status["files_pruned"] += 1

        # Step f: Notify Plex client if available and scan was not cancelled
        if run.plex_client is not None and not self._stop_event.is_set():
            try:
                as_media_server(run.plex_client).refresh_library()
            except Exception as exc:
                logger.warning(
                    "LibraryScanner: Error invoking media-server library refresh: %s",
                    exc,
                )

        # Step g: Conclude scan
        if not self._stop_event.is_set():
            try:
                run.db.record_event(
                    "scan_completed",
                    f"Scan completed: {self._status['files_indexed']} files indexed ({self._status['artists_created']} artists, {self._status['albums_created']} albums)",
                    source="LibraryScanner",
                    severity="info",
                )
            except Exception as e:
                logger.warning("LibraryScanner: Failed to record scan_completed event: %s", e)

        # Auto-hydrate newly created artists in the background
        if run.newly_created_artist_ids and not self._stop_event.is_set():
            self._launch_auto_hydration(run.db, run.newly_created_artist_ids)

        with self._lock:
            self._status["current_file"] = None
            self._status["is_scanning"] = False
            self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
            if self._stop_event.is_set():
                self._status["status"] = "cancelled"
            else:
                self._status["status"] = "completed"

    def _scan(
        self,
        db: Database,
        root_folder: Optional[str],
        prune_missing: bool,
        plex_client: Optional[Any],
        _is_background: bool,
    ) -> dict[str, Any]:
        """Executes a full library scan across filesystem discovery, inspection, and cataloging."""
        if not _is_background:
            with self._lock:
                if self._status.get("is_scanning", False):
                    logger.warning("LibraryScanner: Scan already running, returning active status.")
                    return dict(self._status)
                if self._status.get("status") in ("completed", "failed", "skipped"):
                    self._stop_event.clear()
                self._status = self._default_status()
                self._status["is_scanning"] = True
                self._status["status"] = "scanning"
                self._status["started_at"] = datetime.now(timezone.utc).isoformat()

        try:
            run = self._prepare_scan(db, root_folder, prune_missing, plex_client)
            if run is None:
                with self._lock:
                    return dict(self._status)

            CHUNK_SIZE = 100
            for i in range(0, len(run.audio_files), CHUNK_SIZE):
                if self._stop_event.is_set():
                    logger.info("LibraryScanner: Cancellation requested during indexing.")
                    break

                chunk_files = run.audio_files[i:i + CHUNK_SIZE]
                self._process_chunk(run, chunk_files)
                if self._stop_event.is_set():
                    break

            self._finish_scan(run)
            with self._lock:
                return dict(self._status)

        except Exception as exc:
            logger.error("LibraryScanner: Fatal error during scan execution: %s", safe_exc(exc))
            logger.debug("LibraryScanner scan traceback", exc_info=True)
            with self._lock:
                self._status["status"] = "failed"
                self._status["error"] = safe_exc(exc)
                self._status["is_scanning"] = False
                self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                return dict(self._status)
        finally:
            with self._lock:
                self._status["is_scanning"] = False
                self._status["current_file"] = None
                if self._status.get("completed_at") is None:
                    self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                if self._stop_event.is_set() and self._status.get("status") not in ("failed", "skipped"):
                    self._status["status"] = "cancelled"


    def _launch_auto_hydration(self, db: Database, artist_ids: Any) -> None:
        try:
            from trackseerr.artist_refresh_worker import artist_refresh_worker
            refresh_ids = list(artist_ids)
            # Threads start with an empty context: carry the scan provenance so library writes stay labelled.
            hydration_context = contextvars.copy_context()
            threading.Thread(
                target=lambda: hydration_context.run(
                    lambda: artist_refresh_worker.refresh_once(db=db, artist_ids=refresh_ids)
                ),
                daemon=True,
                name="AutoArtistHydrationThread",
            ).start()
        except Exception as exc:
            logger.warning("LibraryScanner: Failed to launch background artist auto-hydration: %s", exc)


# Expose module singleton instance
library_scanner = LibraryScanner()

__all__ = ["LibraryScanner", "library_scanner"]

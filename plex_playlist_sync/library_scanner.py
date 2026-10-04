"""Thread-safe recursive filesystem scanner and library ingestion engine.

Scans local audio storage non-destructively, extracts Mutagen metadata,
normalizes artists, albums, tracks, and files into SQLite catalog tables,
and evaluates quality profile cutoff compliance.
"""

import concurrent.futures
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from plex_playlist_sync.acquisition_coordinator import _to_quality_profile
from plex_playlist_sync.library import AUDIO_EXTENSIONS, inspect_audio_file
from plex_playlist_sync.library_monitoring import album_monitored_for_option, normalize_album_type
from plex_playlist_sync.models import (
    LibraryAlbum,
    LibraryArtist,
    LibraryFile,
    LibraryTrack,
)
from plex_playlist_sync.quality import evaluate_release, parse_release_title
from plex_playlist_sync.job_tracker import track_job
from plex_playlist_sync.library_manager import ModeChanged, run_guarded
from plex_playlist_sync.redaction import redact_text, safe_exc
from plex_playlist_sync.storage import Database

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
    fallback_title = file_path.stem
    fallback_track_number = 1

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
            with track_job("filesystem_scan", "Media Library Disk Scanner") as job:
                result = self.scan(
                    db=db,
                    root_folder=root_folder,
                    prune_missing=prune_missing,
                    plex_client=plex_client,
                    _is_background=True,
                )
                outcome = str(result.get("status") or "")
                job.message = outcome or None
                if outcome == "failed":
                    err = redact_text(str(result.get("error") or ""))
                    job.failed = f"Scan failed: {err}" if err else "Scan failed"
                elif outcome == "cancelled":
                    job.cancelled = True
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

    def _scan(
        self,
        db: Database,
        root_folder: Optional[str],
        prune_missing: bool,
        plex_client: Optional[Any],
        _is_background: bool,
    ) -> dict[str, Any]:
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
            # Check immediate cancellation
            if self._stop_event.is_set():
                logger.info("LibraryScanner: Stop flag set before start.")
                with self._lock:
                    self._status["status"] = "cancelled"
                    self._status["is_scanning"] = False
                    self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                    return dict(self._status)

            # Step a: Check library mode safeguard
            media_settings = db.get_media_management_settings()
            if media_settings.get("library_mode") == "lidarr":
                logger.debug("LibraryScanner: library manager is Lidarr; skipping filesystem scan")
                with self._lock:
                    self._status["status"] = "skipped"
                    self._status["is_scanning"] = False
                    self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                    return dict(self._status)

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
                    return dict(self._status)

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
            try:
                for entry in root.rglob("*"):
                    if self._stop_event.is_set():
                        break
                    try:
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
                    return dict(self._status)

            # Step d: Process audio files with caching, concurrency, and batched writes
            existing_files_map = {
                f["file_path"]: f for f in db.list_library_files(limit=100000)
            }
            newly_created_artist_ids: list[str] = []
            artist_cache: dict[str, dict[str, Any]] = {}
            album_cache: dict[tuple[str, str], dict[str, Any]] = {}
            track_cache: dict[tuple[str, str, int], dict[str, Any]] = {}
            track_id_cache: dict[str, dict[str, Any]] = {}
            quality_profile_cache: dict[Optional[str], Any] = {}

            CHUNK_SIZE = 100
            for i in range(0, len(audio_files), CHUNK_SIZE):
                if self._stop_event.is_set():
                    logger.info("LibraryScanner: Cancellation requested during indexing.")
                    break

                chunk_files = audio_files[i:i + CHUNK_SIZE]
                cached_metadata_items: dict[Path, dict[str, Any]] = {}
                files_to_inspect: list[Path] = []

                for fpath in chunk_files:
                    fpath_str = str(fpath)
                    existing = existing_files_map.get(fpath_str)
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
                            executor.submit(_inspect_audio_file_worker, fp, root): fp
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

                if self._stop_event.is_set():
                    break

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

                    fpath_str = str(file_path)
                    file_size = 0
                    try:
                        file_size = file_path.stat().st_size
                    except OSError:
                        pass

                    parent = file_path.parent
                    fallback_album = parent.name if parent != root else "Unknown Album"
                    grandparent = parent.parent
                    fallback_artist = (
                        grandparent.name
                        if (parent != root and grandparent != root and grandparent != parent)
                        else "Unknown Artist"
                    )
                    fallback_title = file_path.stem
                    fallback_track_number = 1

                    if metadata.get("from_cache"):
                        cached_track_id = metadata.get("cached_track_id")
                        track_row = None
                        if cached_track_id:
                            track_row = track_id_cache.get(cached_track_id) or db.get_library_track(cached_track_id)
                            if track_row:
                                track_id_cache[cached_track_id] = track_row

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
                        artist_name = (
                            metadata.get("artist") or metadata.get("album_artist") or ""
                        ).strip() or fallback_artist
                        album_title = (metadata.get("album") or "").strip() or fallback_album
                        track_title = (metadata.get("title") or "").strip() or fallback_title
                        track_number = metadata.get("track_number") or fallback_track_number
                        disc_number = metadata.get("disc_number") or 1
                        duration_seconds = metadata.get("duration")
                        year = metadata.get("year")
                        total_tracks = metadata.get("total_tracks")

                        # Resolve/Upsert Artist
                        mb_artist_id = metadata.get("musicbrainz_artistid")
                        artist_row = artist_cache.get(artist_name) or db.get_library_artist_by_name(artist_name)
                        if not artist_row:
                            artist_id = str(uuid.uuid4())
                            artist_path = (
                                str(parent.parent)
                                if (parent != root and grandparent != root and grandparent != parent)
                                else str(parent)
                            )
                            foreign_artist_id = f"musicbrainz:artist:{mb_artist_id}" if mb_artist_id else None
                            art_img_url: Optional[str] = None
                            if artist_path:
                                for art_cand in ("artist.jpg", "artist.png", "folder.jpg"):
                                    if (Path(artist_path) / art_cand).is_file():
                                        art_img_url = f"/api/library/artists/{artist_id}/image"
                                        break
                            artist_row = db.upsert_library_artist(
                                LibraryArtist(
                                    id=artist_id,
                                    name=artist_name,
                                    path=artist_path,
                                    monitored=True,
                                    monitor_option=scan_monitor_option,
                                    mbid=mb_artist_id,
                                    foreign_artist_id=foreign_artist_id,
                                    image_url=art_img_url,
                                )
                            )
                            newly_created_artist_ids.append(artist_id)
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
                                artist_row = db.upsert_library_artist({
                                    **artist_row,
                                    "mbid": new_mbid,
                                    "foreign_artist_id": new_foreign,
                                    "image_url": new_img,
                                }, preserve_monitoring=True)
                        artist_cache[artist_name] = artist_row
                        artist_id = str(artist_row["id"])

                        # Resolve/Upsert Album
                        mb_rg_id = metadata.get("musicbrainz_releasegroupid")
                        mb_rel_id = metadata.get("musicbrainz_albumid")
                        has_local_cover = False
                        for cover_name in ("cover.jpg", "cover.png", "folder.jpg", "folder.png"):
                            candidate = parent / cover_name
                            if candidate.is_file():
                                has_local_cover = True
                                break

                        if not has_local_cover:
                            extracted = extract_embedded_cover_art(file_path, parent)
                            if extracted is not None and extracted.is_file():
                                has_local_cover = True

                        album_key = (artist_id, album_title)
                        album_row = album_cache.get(album_key) or db.get_library_album_by_title(artist_id, album_title)
                        if not album_row:
                            album_id = str(uuid.uuid4())
                            new_album_type = normalize_album_type(
                                metadata.get("album_type") or ("single" if total_tracks == 1 else "album")
                            )
                            new_album_monitored = album_monitored_for_option(
                                str(artist_row.get("monitor_option") or scan_monitor_option),
                                artist_monitored=bool(artist_row.get("monitored", True)),
                                album_type=new_album_type,
                                has_files=True,
                                release_date=metadata.get("release_date"),
                                year=year,
                                artist_added_at=artist_row.get("created_at"),
                            )
                            album_path = str(parent)
                            album_cover = f"/api/library/albums/{album_id}/cover" if has_local_cover else None
                            album_row = db.upsert_library_album(
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
                                album_row = db.upsert_library_album({
                                    **album_row,
                                    "mb_release_group_id": new_rg,
                                    "mb_release_id": new_rel,
                                    "cover_url": new_cov,
                                }, preserve_monitoring=True)
                        album_cache[album_key] = album_row
                        album_id = str(album_row["id"])

                        # Resolve/Upsert Track
                        mb_rec_id = metadata.get("musicbrainz_trackid")
                        track_isrc = metadata.get("isrc")
                        track_key = (album_id, track_title, int(track_number))
                        track_row = track_cache.get(track_key) or db.get_library_track_by_title(album_id, track_title, track_number)
                        if not track_row:
                            track_id = str(uuid.uuid4())
                            track_row = db.upsert_library_track(
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
                                track_row = db.upsert_library_track({
                                    **track_row,
                                    "mb_recording_id": new_rec,
                                    "isrc": new_isrc,
                                }, preserve_monitoring=True)
                        track_cache[track_key] = track_row
                        track_id_cache[str(track_row["id"])] = track_row
                        track_id = str(track_row["id"])

                        # Quality profile & Cutoff evaluation
                        cutoff_met = True
                        quality_name = str(metadata.get("quality_full") or metadata.get("codec") or "Unknown")
                        try:
                            qp_id = artist_row.get("quality_profile_id")
                            profile_dict = quality_profile_cache.get(qp_id)
                            if profile_dict is None:
                                profile_dict = db.get_quality_profile(qp_id) if qp_id else None
                                if not profile_dict:
                                    profile_dict = db.get_default_quality_profile()
                                quality_profile_cache[qp_id] = profile_dict
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
                                eval_result = evaluate_release(parsed, qp, size_bytes=file_size)
                                cutoff_met = bool(eval_result.meets_cutoff)
                                quality_name = eval_result.parsed_quality or str(quality_input)
                        except Exception as exc:
                            logger.warning(
                                "LibraryScanner: Cutoff evaluation error for %s: %s. Defaulting cutoff_met=True.",
                                file_path,
                                exc,
                            )
                            cutoff_met = True

                    # Upsert File
                    rel_path = str(file_path.relative_to(root))
                    file_id = str(metadata.get("cached_file_id") or "")
                    if not file_id:
                        existing_file = existing_files_map.get(fpath_str) or db.get_library_file_by_path(fpath_str)
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

                # Batched database write for this chunk
                if pending_files_to_batch:
                    db.upsert_library_files_batch(pending_files_to_batch)

                logger.info(
                    "LibraryScanner: Processed %d/%d files...",
                    self._status["processed_files"],
                    len(audio_files),
                )

            if self._stop_event.is_set():
                with self._lock:
                    self._status["status"] = "cancelled"
                    self._status["is_scanning"] = False
                    self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                    return dict(self._status)

            # Step e: Prune missing files if enabled and scan was not cancelled
            if prune_missing and not self._stop_event.is_set():
                all_files = db.list_library_files(limit=10000)
                for row in all_files:
                    if self._stop_event.is_set():
                        break
                    fpath = row.get("file_path")
                    if fpath and not Path(fpath).exists():
                        db.delete_library_file(str(row["id"]))
                        with self._lock:
                            self._status["files_pruned"] += 1

            # Step f: Notify Plex client if available and scan was not cancelled
            if plex_client is not None and not self._stop_event.is_set():
                try:
                    if hasattr(plex_client, "refresh_music_library"):
                        plex_client.refresh_music_library()
                except Exception as exc:
                    logger.warning(
                        "LibraryScanner: Error invoking plex_client.refresh_music_library(): %s",
                        exc,
                    )

            # Step g: Conclude scan
            if not self._stop_event.is_set():
                try:
                    db.record_event(
                        "scan_completed",
                        f"Scan completed: {self._status['files_indexed']} files indexed ({self._status['artists_created']} artists, {self._status['albums_created']} albums)",
                        source="LibraryScanner",
                        severity="info",
                    )
                except Exception as e:
                    logger.warning("LibraryScanner: Failed to record scan_completed event: %s", e)

            # Auto-hydrate newly created artists in the background
            if newly_created_artist_ids and not self._stop_event.is_set():
                try:
                    from plex_playlist_sync.artist_refresh_worker import artist_refresh_worker
                    refresh_ids = list(newly_created_artist_ids)
                    threading.Thread(
                        target=lambda: artist_refresh_worker.refresh_once(db=db, artist_ids=refresh_ids),
                        daemon=True,
                        name="AutoArtistHydrationThread",
                    ).start()
                except Exception as exc:
                    logger.warning("LibraryScanner: Failed to launch background artist auto-hydration: %s", exc)

            with self._lock:
                self._status["current_file"] = None
                self._status["is_scanning"] = False
                self._status["completed_at"] = datetime.now(timezone.utc).isoformat()
                if self._stop_event.is_set():
                    self._status["status"] = "cancelled"
                else:
                    self._status["status"] = "completed"
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


# Expose module singleton instance
library_scanner = LibraryScanner()

__all__ = ["LibraryScanner", "library_scanner"]

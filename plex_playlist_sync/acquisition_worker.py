"""Download Monitor & Automated Library Organizer Worker.

Periodically inspects active downloads across configured download clients,
detects completed transfers, inspects audio tags, calculates destination
paths via the token template engine, performs atomic file moves with
collision resolution into /music, and triggers Plex library update pings.
"""

import difflib
import json
import logging
import os
import shutil
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from plex_playlist_sync.acquisition_coordinator import _to_quality_profile
from plex_playlist_sync.clients.acquisition import get_acquisition_driver
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.clients.plex import PlexClient
from plex_playlist_sync.library import (
    AUDIO_EXTENSIONS,
    embed_album_artwork,
    extract_archive,
    inspect_audio_file,
    is_archive_file,
    resolve_collision,
    write_audio_tags,
)
from plex_playlist_sync.models import (
    DownloadStatus,
    LibraryAlbum,
    LibraryArtist,
    LibraryFile,
    LibraryTrack,
    NotificationEvent,
    RequestStatus,
)
from plex_playlist_sync.naming import build_track_path
from plex_playlist_sync.notifications import notification_dispatcher
from plex_playlist_sync.redaction import safe_exc
from plex_playlist_sync.quality import evaluate_release, parse_release_title
from plex_playlist_sync.security import is_safe_service_url
from plex_playlist_sync.storage import Database, clean_library_name

logger = logging.getLogger(__name__)


def _is_safe_cover_url(url: Optional[str]) -> bool:
    """Validates that a cover artwork URL is safe against SSRF attacks."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = (parsed.hostname or "").lower()
        whitelisted_domains = (
            "mzstatic.com",
            "deezer.com",
            "dzcdn.net",
            "spotify.com",
            "scdn.co",
            "last.fm",
            "musicbrainz.org",
            "discogs.com",
            "coverartarchive.org",
            "archive.org",
        )
        if any(hostname == d or hostname.endswith("." + d) for d in whitelisted_domains):
            return True
        return is_safe_service_url(url, allow_lan=False)
    except Exception:
        return False


def safe_atomic_move(source_file: Path | str, target_file: Path | str) -> Path:
    """Atomically places source_file at target_file, safely handling cross-device mounts.

    If source and destination reside on the same filesystem, os.replace is used directly.
    Across different filesystems, writes to a temporary hidden file in the destination
    folder first, then atomically replaces to ensure Plex never indexes incomplete files.
    """
    src = Path(source_file).resolve()
    dst = Path(target_file).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    try:
        os.replace(str(src), str(dst))
        return dst
    except OSError:
        # Cross-device link: write temporary file in destination folder, then os.replace
        tmp_dst = dst.parent / f".tmp_{dst.name}_{os.getpid()}_{time.time_ns()}"
        shutil.copy2(str(src), str(tmp_dst))
        os.replace(str(tmp_dst), str(dst))
        try:
            src.unlink(missing_ok=True)
        except OSError:
            pass
        return dst


def place_audio_file(
    source_file: Path | str, target_file: Path | str, mode: str = "move"
) -> Path:
    """Places source_file at target_file using either atomic move or hardlink.

    - mode="hardlink": Target parent directories created, calls os.link(src, dst).
      If successful, returns dst (original src preserved untouched for seeding).
      If os.link fails (e.g. cross-device EXDEV), falls back to shutil.copy2 without unlinking src.
    - mode="move": Calls safe_atomic_move(source_file, target_file) (atomic replace, unlink source).
    """
    src = Path(source_file).resolve()
    dst = Path(target_file).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    if mode == "hardlink":
        try:
            os.link(str(src), str(dst))
            logger.info("Successfully hardlinked '%s' -> '%s'", src, dst)
            return dst
        except OSError as e:
            logger.warning(
                "os.link failed (%s); falling back to shutil.copy2 for '%s' -> '%s'",
                e,
                src,
                dst,
            )
            shutil.copy2(str(src), str(dst))
            return dst
    else:
        return safe_atomic_move(source_file, target_file)


def reconcile_audio_file_to_track(
    meta: dict[str, Any],
    candidate_tracks: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Reconciles an audio file's metadata against a list of expected library tracks.

    Matching hierarchy:
    1. Exact match on disc_number and track_number (if mutagen extracted valid track number).
    2. Clean title similarity match (clean_library_name(t["title"]) == clean_library_name(meta["title"]) or ratio >= 0.85).
    3. Duration tolerance match (within 5 seconds) if multiple candidates match title.
    """
    if not candidate_tracks:
        return None

    file_track = meta.get("track_number")
    file_disc = meta.get("disc_number") or 1
    has_valid_track_num = isinstance(file_track, int) and file_track > 0

    # 1. Exact match on disc_number and track_number (if mutagen extracted valid track number)
    if has_valid_track_num:
        num_matches = [
            t
            for t in candidate_tracks
            if int(t.get("track_number") or 1) == file_track
            and int(t.get("disc_number") or 1) == int(file_disc)
        ]
        if len(num_matches) == 1:
            return num_matches[0]
        elif len(num_matches) > 1:
            clean_title = clean_library_name(meta.get("title") or "")
            title_matches = [
                t
                for t in num_matches
                if clean_library_name(t.get("title") or "") == clean_title
            ]
            if len(title_matches) == 1:
                return title_matches[0]
            file_dur = meta.get("duration") or meta.get("duration_seconds")
            if file_dur is not None:
                dur_matches = [
                    t
                    for t in num_matches
                    if t.get("duration_seconds") is not None
                    and abs(float(t["duration_seconds"]) - float(file_dur)) <= 5.0
                ]
                if dur_matches:
                    return min(
                        dur_matches,
                        key=lambda t: abs(float(t["duration_seconds"]) - float(file_dur)),
                    )
            return num_matches[0]

    # 2. Clean title similarity match
    meta_title = meta.get("title") or ""
    clean_meta = clean_library_name(meta_title)
    if not clean_meta and meta.get("file_path"):
        clean_meta = clean_library_name(Path(meta["file_path"]).stem)

    if clean_meta:
        # Exact clean title match
        exact_title_matches = [
            t
            for t in candidate_tracks
            if clean_library_name(t.get("title") or "") == clean_meta
        ]
        if len(exact_title_matches) == 1:
            return exact_title_matches[0]
        elif len(exact_title_matches) > 1:
            # 3. Duration tolerance match (within 5 seconds) if multiple candidates match title
            file_dur = meta.get("duration") or meta.get("duration_seconds")
            if file_dur is not None:
                dur_matches = [
                    t
                    for t in exact_title_matches
                    if t.get("duration_seconds") is not None
                    and abs(float(t["duration_seconds"]) - float(file_dur)) <= 5.0
                ]
                if len(dur_matches) == 1:
                    return dur_matches[0]
                elif dur_matches:
                    return min(
                        dur_matches,
                        key=lambda t: abs(float(t["duration_seconds"]) - float(file_dur)),
                    )
            return exact_title_matches[0]

        # Fuzzy title match with ratio >= 0.85
        fuzzy_candidates: list[tuple[float, dict[str, Any]]] = []
        for t in candidate_tracks:
            clean_t = clean_library_name(t.get("title") or "")
            if not clean_t:
                continue
            ratio = difflib.SequenceMatcher(None, clean_t, clean_meta).ratio()
            if ratio >= 0.85:
                fuzzy_candidates.append((ratio, t))

        if fuzzy_candidates:
            fuzzy_candidates.sort(key=lambda x: x[0], reverse=True)
            top_ratio = fuzzy_candidates[0][0]
            top_matches = [t for r, t in fuzzy_candidates if abs(r - top_ratio) < 0.001]
            if len(top_matches) == 1:
                return top_matches[0]

            # 3. Duration tolerance match if multiple fuzzy candidates
            file_dur = meta.get("duration") or meta.get("duration_seconds")
            if file_dur is not None:
                dur_matches = [
                    t
                    for t in top_matches
                    if t.get("duration_seconds") is not None
                    and abs(float(t["duration_seconds"]) - float(file_dur)) <= 5.0
                ]
                if len(dur_matches) == 1:
                    return dur_matches[0]
                elif dur_matches:
                    return min(
                        dur_matches,
                        key=lambda t: abs(float(t["duration_seconds"]) - float(file_dur)),
                    )
            return top_matches[0]

    return None


def translate_remote_path(
    remote_path: Optional[str], mappings: list[dict[str, str]]
) -> Optional[str]:
    """Translates remote download client file paths to local mount paths.

    If remote_path starts with a mapping's remote_path, replaces that prefix with local_path.
    Guards against directory traversal attacks.
    """
    if remote_path is None:
        return None

    # Defense against directory traversal attempts in remote path input
    parts = remote_path.replace("\\", "/").split("/")
    if ".." in parts:
        logger.warning("Path traversal attempt rejected in remote_path: %s", remote_path)
        return None

    if not mappings:
        return remote_path

    resolved = remote_path
    for m in mappings:
        if not isinstance(m, dict):
            continue
        r = m.get("remote_path")
        l = m.get("local_path")
        if not r or not l:
            continue
        r_clean = r.rstrip("/")
        l_clean = l.rstrip("/")
        if resolved == r_clean:
            resolved = l_clean
            break
        elif resolved.startswith(r_clean + "/"):
            resolved = l_clean + resolved[len(r_clean):]
            break
        elif resolved.startswith(r_clean + "\\"):
            resolved = l_clean + "/" + resolved[len(r_clean) + 1:].replace("\\", "/")
            break

    norm = os.path.normpath(resolved)
    if ".." in norm.replace("\\", "/").split("/"):
        logger.warning("Directory traversal detected in remote path mapping: %s", resolved)
        return None

    return norm


class AcquisitionWorker:
    """Thread-safe background runner monitoring active downloads and organizing media."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running: bool = False
        self.poll_interval: float = 5.0
        self.staging_dir: str = "/downloads"

    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    def start(
        self,
        db: Database,
        plex_client: Optional[PlexClient] = None,
        poll_interval: float = 5.0,
        staging_dir: Optional[str] = None,
        plex_client_provider: Optional[Callable[[], Optional[PlexClient]]] = None,
    ) -> bool:
        """Starts background download monitor worker thread.

        ``plex_client_provider`` is resolved on every poll cycle, so a Plex that was down at boot and
        connects later is picked up; it takes precedence over the by-value ``plex_client``.
        """
        with self._lock:
            if self._is_running:
                logger.warning("AcquisitionWorker is already running")
                return False

            self.poll_interval = poll_interval
            if staging_dir:
                self.staging_dir = staging_dir

            self._stop_event.clear()
            self._is_running = True

            def _worker_loop() -> None:
                logger.info("AcquisitionWorker loop started (poll interval: %.1fs)", self.poll_interval)
                while not self._stop_event.is_set():
                    try:
                        current_plex = plex_client
                        if plex_client_provider is not None:
                            try:
                                current_plex = plex_client_provider()
                            except Exception as e:  # a failing provider must not stop imports; Plex refresh is optional
                                logger.warning("Plex client provider failed: %s", safe_exc(e))
                                current_plex = None
                        self.poll_once(db=db, plex_client=current_plex, staging_dir=self.staging_dir)
                    except Exception as e:
                        logger.error("Unexpected error in AcquisitionWorker poll cycle: %s", e)

                    # Sleep with responsive stop checking
                    slept = 0.0
                    while slept < self.poll_interval and not self._stop_event.is_set():
                        time.sleep(min(0.5, self.poll_interval - slept))
                        slept += 0.5

                with self._lock:
                    self._is_running = False
                logger.info("AcquisitionWorker loop stopped cleanly")

            self._thread = threading.Thread(target=_worker_loop, daemon=True, name="AcquisitionWorkerThread")
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """Signals background worker to stop and waits for completion."""
        with self._lock:
            if not self._is_running:
                return
            self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

        with self._lock:
            self._is_running = False

    def _find_audio_files(self, candidate_path: Optional[str | Path], search_term: str) -> list[Path]:
        """Locates downloaded audio files from source path or staging directory.

        Automatically extracts archives (.zip, .tar, etc.) encountered in candidate_path
        or staging into a temporary subfolder in staging and discovers extracted audio files.
        """
        found: list[Path] = []
        staging_path = Path(self.staging_dir).resolve()

        if candidate_path:
            src_path = Path(candidate_path).resolve()
            if not src_path.is_relative_to(staging_path):
                logger.warning("Rejecting source path outside staging directory: %s", candidate_path)
            else:
                if src_path.is_file():
                    if src_path.suffix.lower() in AUDIO_EXTENSIONS:
                        return [src_path]
                    elif is_archive_file(src_path):
                        extract_dir = staging_path / f"_extracted_{src_path.stem}_{os.getpid()}_{time.time_ns()}"
                        extract_dir.mkdir(parents=True, exist_ok=True)
                        try:
                            extracted = extract_archive(src_path, extract_dir)
                            if extracted:
                                return sorted(extracted)
                        except Exception as e:
                            logger.warning("Failed to extract candidate archive %s: %s", src_path, e)
                elif src_path.is_dir():
                    archives_in_src: list[Path] = []
                    for root, _, files in os.walk(str(src_path)):
                        for f in files:
                            f_path = (Path(root) / f).resolve()
                            if f_path.is_relative_to(staging_path):
                                if f_path.suffix.lower() in AUDIO_EXTENSIONS:
                                    found.append(f_path)
                                elif is_archive_file(f_path):
                                    archives_in_src.append(f_path)
                    if found:
                        return sorted(found)
                    for arc_path in archives_in_src:
                        extract_dir = staging_path / f"_extracted_{arc_path.stem}_{os.getpid()}_{time.time_ns()}"
                        extract_dir.mkdir(parents=True, exist_ok=True)
                        try:
                            extracted = extract_archive(arc_path, extract_dir)
                            found.extend(extracted)
                        except Exception as e:
                            logger.warning("Failed to extract archive %s in candidate dir: %s", arc_path, e)
                    if found:
                        return sorted(found)

        # Fallback: scan staging directory for files matching search term
        if staging_path.exists():
            clean_term = search_term.lower()
            staging_archives: list[Path] = []
            for root, _, files in os.walk(str(staging_path)):
                for f in files:
                    f_path = (Path(root) / f).resolve()
                    if f_path.is_relative_to(staging_path):
                        if clean_term in f.lower() or clean_term in root.lower():
                            if f_path.suffix.lower() in AUDIO_EXTENSIONS:
                                found.append(f_path)
                            elif is_archive_file(f_path):
                                staging_archives.append(f_path)

            if not found and staging_archives:
                for arc_path in staging_archives:
                    extract_dir = staging_path / f"_extracted_{arc_path.stem}_{os.getpid()}_{time.time_ns()}"
                    extract_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        extracted = extract_archive(arc_path, extract_dir)
                        found.extend(extracted)
                    except Exception as e:
                        logger.warning("Failed to extract fallback archive %s: %s", arc_path, e)

        return sorted(found)

    def poll_once(
        self,
        db: Database,
        plex_client: Optional[PlexClient] = None,
        staging_dir: Optional[str] = None,
    ) -> dict[str, int]:
        media_settings = db.get_media_management_settings()
        import_mode = media_settings.get("import_mode", "move")
        write_tags = bool(media_settings.get("write_audio_tags", True))
        embed_art = bool(media_settings.get("embed_artwork", True))
        save_cover = bool(media_settings.get("save_cover_art_file", True))
        if staging_dir:
            self.staging_dir = staging_dir
        else:
            self.staging_dir = media_settings.get("staging_folder_path", self.staging_dir)

        stats = {"polled": 0, "completed": 0, "failed": 0, "imported": 0}
        active_items = db.list_active_downloads(
            statuses=[
                DownloadStatus.QUEUED.value,
                DownloadStatus.DOWNLOADING.value,
                DownloadStatus.IMPORTING.value,
                DownloadStatus.COMPLETED.value,
            ]
        )

        if not active_items:
            return stats

        for item in active_items:
            download_id = item["id"]
            client_id = item.get("client_id")
            stats["polled"] += 1

            def _notify_failed(err_text: str) -> None:
                try:
                    db.record_event(
                        "download_failed",
                        f"Download failed for '{item.get('title', '')}': {err_text}",
                        source="AcquisitionWorker",
                        severity="error",
                    )
                except Exception as ev_err:
                    logger.warning("Failed to record download_failed event: %s", ev_err)
                try:
                    notification_dispatcher.dispatch(
                        NotificationEvent.DOWNLOAD_FAILED,
                        data={
                            "artist": item.get("artist"),
                            "title": item.get("title"),
                            "download_id": download_id,
                            "request_id": item.get("request_id"),
                            "error_message": err_text,
                        },
                        db=db,
                    )
                except Exception as ex:
                    logger.warning("Failed to dispatch DOWNLOAD_FAILED notification: %s", ex)

            client_config = db.get_download_client(client_id) if client_id else None
            if not client_config:
                logger.warning("Download client %s not found for active download %s", client_id, download_id)
                err_msg = f"Client '{client_id}' not found"
                db.update_download_status(
                    download_id,
                    status=DownloadStatus.FAILED.value,
                    error_message=err_msg,
                )
                _notify_failed(err_msg)
                stats["failed"] += 1
                continue

            try:
                driver = get_acquisition_driver(client_config)
            except Exception as e:
                logger.error("Could not instantiate driver for client %s: %s", client_id, e)
                err_msg = f"Driver error: {str(e)}"
                db.update_download_status(
                    download_id,
                    status=DownloadStatus.FAILED.value,
                    error_message=err_msg,
                )
                _notify_failed(err_msg)
                stats["failed"] += 1
                continue

            # Poll driver status
            target_lookup = item.get("download_hash") or download_id
            try:
                status_dict = driver.get_status(target_lookup)
            except Exception as e:
                logger.warning("Exception querying driver status for %s: %s", target_lookup, e)
                continue

            cur_status = status_dict.get("status", DownloadStatus.DOWNLOADING.value).lower()
            progress = float(status_dict.get("progress") or 0.0)
            size_bytes = status_dict.get("size_bytes")
            db.update_download_progress(download_id, progress, size_bytes)

            if item.get("status") == DownloadStatus.QUEUED.value and cur_status == DownloadStatus.DOWNLOADING.value:
                client_name = client_config.get("name", "Client") if client_config else "Client"
                try:
                    db.record_event(
                        "download_started",
                        f"Grabbed '{item.get('title', '')}' via {client_name}",
                        source="AcquisitionWorker",
                        severity="info",
                        details={
                            "artist": item.get("artist"),
                            "title": item.get("title"),
                            "client": client_name,
                            "download_id": download_id,
                            "size_bytes": size_bytes,
                        },
                    )
                except Exception as ev_err:
                    logger.warning("Failed to record download_started event: %s", ev_err)

            # If failed
            if cur_status == DownloadStatus.FAILED.value:
                err_msg = status_dict.get("error_message") or "Download failed"
                db.update_download_status(download_id, status=DownloadStatus.FAILED.value, error_message=err_msg)
                _notify_failed(err_msg)
                try:
                    db.add_to_blocklist(
                        source_title=item.get("title", ""),
                        artist=item.get("artist"),
                        release_guid=item.get("id"),
                        info_hash=item.get("download_hash"),
                        reason=err_msg,
                    )
                except Exception as bl_err:
                    logger.warning("Failed to add failed download to blocklist: %s", bl_err)
                stats["failed"] += 1
                continue

            # If completed or ready to import
            is_ready = cur_status == DownloadStatus.COMPLETED.value or item.get("status") == DownloadStatus.COMPLETED.value
            already_imported = bool(item.get("target_path"))

            if already_imported and is_ready:
                # Torrent already imported, currently seeding under governance
                if media_settings.get("delete_completed_transfers"):
                    seed_ratio_limit = media_settings.get("seed_ratio_limit")
                    seed_time_limit_minutes = media_settings.get("seed_time_limit_minutes")
                    if import_mode == "hardlink" and (seed_ratio_limit is not None or seed_time_limit_minutes is not None):
                        cur_ratio = float(status_dict.get("ratio") or 0.0)
                        cur_seeding_sec = int(status_dict.get("seeding_time_seconds") or 0)
                        ratio_met = seed_ratio_limit is not None and cur_ratio >= float(seed_ratio_limit)
                        time_met = seed_time_limit_minutes is not None and cur_seeding_sec >= int(seed_time_limit_minutes) * 60
                        limit_reached = ratio_met or time_met
                        if limit_reached:
                            try:
                                driver.cleanup_completed(target_lookup, delete_files=False)
                            except Exception as ex:
                                logger.warning("Error during cleanup_completed for %s: %s", target_lookup, ex)
                            db.update_download_status(download_id, status=DownloadStatus.IMPORTED.value)
                        else:
                            db.update_download_status(download_id, status=DownloadStatus.COMPLETED.value)
                    else:
                        try:
                            driver.cleanup_completed(target_lookup, delete_files=False)
                        except Exception as ex:
                            logger.warning("Error during cleanup_completed for %s: %s", target_lookup, ex)
                        db.update_download_status(download_id, status=DownloadStatus.IMPORTED.value)
                continue

            if is_ready:
                stats["completed"] += 1
                db.update_download_status(download_id, status=DownloadStatus.IMPORTING.value)

                # Special case: Lidarr performs native file organization
                driver_type = str(client_config.get("driver_type", "")).lower()
                if driver_type == "lidarr":
                    db.update_download_status(download_id, status=DownloadStatus.IMPORTED.value)
                    req_row = None
                    if item.get("request_id"):
                        db.update_request_status(item["request_id"], RequestStatus.AVAILABLE.value)
                        req_row = db.get_request(item["request_id"])
                    stats["imported"] += 1
                    try:
                        db.record_event(
                            "item_available",
                            f"Imported '{item.get('title', '')}' to library",
                            source="AcquisitionWorker",
                            severity="info",
                        )
                    except Exception as ev_err:
                        logger.warning("Failed to record Lidarr item_available event: %s", ev_err)
                    try:
                        notification_dispatcher.dispatch(
                            NotificationEvent.ITEM_AVAILABLE,
                            data={
                                "artist": item.get("artist"),
                                "title": item.get("title"),
                                "album": item.get("title") if item.get("item_type") == "album" else None,
                                "request_id": item.get("request_id"),
                                "download_id": download_id,
                                "cover_url": req_row.get("cover_url") if req_row else None,
                                "username": req_row.get("username") if req_row else None,
                            },
                            db=db,
                        )
                    except Exception as ex:
                        logger.warning("Failed to dispatch ITEM_AVAILABLE notification for Lidarr import: %s", ex)

                    if plex_client:
                        try:
                            plex_client.refresh_music_library()
                        except Exception as e:
                            logger.warning("Error refreshing Plex after Lidarr import: %s", e)
                    continue

                # Locate downloaded audio files with remote path translation
                mappings: list[dict[str, str]] = []
                extra_json = client_config.get("extra_settings_json")
                if extra_json:
                    try:
                        extra_data = json.loads(extra_json) if isinstance(extra_json, str) else extra_json
                        if isinstance(extra_data, dict):
                            mappings = extra_data.get("remote_path_mappings", [])
                    except (json.JSONDecodeError, TypeError):
                        mappings = []

                raw_src = status_dict.get("source_path") or item.get("source_path")
                candidate_src = translate_remote_path(raw_src, mappings) if raw_src else None
                if candidate_src:
                    src_path = Path(candidate_src).resolve()
                    staging_path = Path(self.staging_dir).resolve()
                    if not src_path.is_relative_to(staging_path):
                        logger.warning("Rejecting source path outside staging directory: %s", candidate_src)
                        candidate_src = None

                search_term = item.get("title") or item.get("artist") or ""
                audio_files = self._find_audio_files(candidate_src, search_term)

                if not audio_files:
                    logger.warning(
                        "Download %s marked completed but no audio files found at %s or staging %s",
                        download_id,
                        candidate_src,
                        self.staging_dir,
                    )
                    err_msg = "No audio files found for import in download staging"
                    db.update_download_status(
                        download_id,
                        status=DownloadStatus.FAILED.value,
                        error_message=err_msg,
                    )
                    _notify_failed(err_msg)
                    try:
                        db.add_to_blocklist(
                            source_title=item.get("title", ""),
                            artist=item.get("artist"),
                            release_guid=item.get("id"),
                            info_hash=item.get("download_hash"),
                            reason=err_msg,
                        )
                    except Exception as bl_err:
                        logger.warning("Failed to add missing-audio download to blocklist: %s", bl_err)
                    stats["failed"] += 1
                    continue

                # Organize and move each audio file
                imported_paths: list[str] = []
                root_folder = media_settings.get("root_folder_path") or "/music"
                root_path = Path(root_folder).resolve()

                # Fetch associated request and album cover art if available
                req = db.get_request(item["request_id"]) if item.get("request_id") else None
                cover_bytes: bytes | None = None
                if req and (embed_art or save_cover):
                    cover_url = req.get("cover_url")
                    if cover_url and _is_safe_cover_url(cover_url):
                        try:
                            resp = httpx.get(cover_url, timeout=10.0, follow_redirects=True)
                            if resp.status_code == 200 and resp.content:
                                cover_bytes = resp.content
                        except httpx.HTTPError as e:
                            logger.warning("HTTP error fetching cover art from %s: %s", cover_url, e)
                        except Exception as e:
                            logger.warning("Error fetching cover art from %s: %s", cover_url, e)
                    elif cover_url:
                        logger.warning("Cover art URL rejected by SSRF protection: %s", cover_url)

                last_metadata: dict[str, Any] = {}

                # Check if item has album_id or matches an existing album in catalog
                target_album = None
                if item.get("album_id"):
                    target_album = db.get_library_album(item["album_id"])
                elif item.get("track_id"):
                    req_track = db.get_library_track(item["track_id"])
                    if req_track:
                        target_album = db.get_library_album(req_track["album_id"])

                if not target_album:
                    art_name_cand = item.get("artist") or (req.get("artist") if req else None)
                    alb_title_cand = (
                        (item.get("title") if item.get("item_type") == "album" else None)
                        or (req.get("album") or req.get("title") if req else None)
                        or item.get("title")
                    )
                    if art_name_cand and alb_title_cand:
                        art_cand = db.get_library_artist_by_name(art_name_cand)
                        if art_cand:
                            target_album = db.get_library_album_by_title(art_cand["id"], alb_title_cand)

                expected_tracks: list[dict[str, Any]] = []
                if target_album:
                    expected_tracks = db.list_library_tracks(album_id=target_album["id"], limit=1000)

                remaining_expected_tracks = list(expected_tracks)
                placed_to_track: dict[str, dict[str, Any]] = {}

                for af in audio_files:
                    try:
                        metadata = inspect_audio_file(af)
                    except Exception as e:
                        logger.warning("Mutagen inspection failed for %s: %s; using item defaults", af, e)
                        metadata = {
                            "artist": item.get("artist", "Unknown Artist"),
                            "title": item.get("title", af.stem),
                            "album": item.get("title") if item.get("item_type") == "album" else "Unknown Album",
                            "file_path": str(af),
                            "extension": af.suffix.lower(),
                            "track_number": None,
                            "disc_number": 1,
                            "total_discs": 1,
                        }

                    # Fallbacks for empty tags
                    if not metadata.get("artist"):
                        metadata["artist"] = item.get("artist") or "Unknown Artist"
                    if not metadata.get("title"):
                        metadata["title"] = item.get("title") or af.stem

                    # Reconcile against expected catalog tracks if present
                    matched_expected_track = None
                    if remaining_expected_tracks:
                        matched_expected_track = reconcile_audio_file_to_track(metadata, remaining_expected_tracks)
                        if matched_expected_track:
                            remaining_expected_tracks.remove(matched_expected_track)
                            metadata["title"] = matched_expected_track["title"]
                            metadata["track_number"] = int(matched_expected_track.get("track_number") or 1)
                            metadata["disc_number"] = int(matched_expected_track.get("disc_number") or 1)
                            if target_album:
                                metadata["album"] = target_album["title"]
                                art_cand = db.get_library_artist(target_album["artist_id"])
                                if art_cand:
                                    metadata["artist"] = art_cand["name"]

                    # Disc 1 of a multi-disc release must use the multi-disc format too.
                    known_discs = [int(metadata.get("total_discs") or 1)]
                    known_discs += [int(t.get("disc_number") or 1) for t in expected_tracks]
                    metadata["total_discs"] = max(known_discs)

                    last_metadata = metadata

                    target_str = build_track_path(metadata, media_settings)
                    final_target = resolve_collision(target_str)
                    target_path = Path(final_target).resolve()
                    if not target_path.is_relative_to(root_path):
                        logger.error("Destination %s escapes music root %s", target_path, root_path)
                        continue

                    placed_path = place_audio_file(af, target_path, mode=import_mode)
                    imported_paths.append(str(placed_path))
                    if matched_expected_track:
                        placed_to_track[str(placed_path)] = matched_expected_track
                    logger.info("Successfully imported '%s' -> '%s'", af.name, placed_path)
                    try:
                        db.record_event(
                            "item_available",
                            f"Imported '{af.name}' to library",
                            source="AcquisitionWorker",
                            severity="info",
                        )
                    except Exception as ev_err:
                        logger.warning("Failed to record item_available event: %s", ev_err)

                    # Tag writing and artwork embedding
                    tags_to_write: dict[str, Any] = {
                        "artist": (req.get("artist") if req else None) or metadata.get("artist"),
                        "album": (req.get("album") or req.get("title") if req else None) or metadata.get("album"),
                        "title": metadata.get("title") if len(audio_files) > 1 else ((req.get("title") if req else None) or metadata.get("title")),
                        "date": (req.get("release_date") if req else None) or metadata.get("year"),
                        "tracknumber": metadata.get("track_number"),
                        "totaltracks": metadata.get("total_tracks"),
                        "discnumber": metadata.get("disc_number"),
                        "totaldiscs": metadata.get("total_discs"),
                    }

                    # Asynchronously enrich with MBIDs if enabled
                    if media_settings.get("enrich_mbids", True):
                        try:
                            enricher = MbidEnricherClient(
                                base_url=media_settings.get("mb_mirror_url", "https://api.brainzmash.cc")
                            )
                            artist_query = str(tags_to_write.get("artist") or "")
                            album_query = str(tags_to_write.get("album") or "")
                            title_query = str(tags_to_write.get("title") or "")
                            track_isrc = metadata.get("isrc")
                            resolved_mbids = enricher.lookup_track_mbids(
                                artist_query, album_query, title_query, isrc=track_isrc
                            )
                            if resolved_mbids:
                                for mb_key in (
                                    "musicbrainz_artistid",
                                    "musicbrainz_albumid",
                                    "musicbrainz_releasegroupid",
                                    "musicbrainz_trackid",
                                ):
                                    if resolved_mbids.get(mb_key):
                                        tags_to_write[mb_key] = resolved_mbids[mb_key]

                            if not cover_bytes and (embed_art or save_cover):
                                rg_id = resolved_mbids.get("musicbrainz_releasegroupid") if resolved_mbids else None
                                rel_id = resolved_mbids.get("musicbrainz_albumid") if resolved_mbids else None
                                resolved_cover_url = enricher.get_cover_art_url(release_group_id=rg_id, release_id=rel_id)
                                if resolved_cover_url and _is_safe_cover_url(resolved_cover_url):
                                    try:
                                        resp = httpx.get(resolved_cover_url, timeout=5.0, follow_redirects=True)
                                        if resp.status_code == 200 and resp.content:
                                            cover_bytes = resp.content
                                    except Exception as exc:
                                        logger.debug("Failed fetching cover art from Cover Art Archive %s: %s", resolved_cover_url, exc)
                        except Exception as e:
                            logger.warning("AcquisitionWorker: MBID enrichment error: %s", e)

                    if write_tags:
                        art_to_embed = cover_bytes if embed_art else None
                        try:
                            write_audio_tags(placed_path, tags=tags_to_write, cover_art_bytes=art_to_embed)
                        except Exception as e:
                            logger.warning("Error writing audio tags to %s: %s", placed_path, e)
                    elif embed_art and cover_bytes:
                        try:
                            embed_album_artwork(placed_path, cover_bytes)
                        except Exception as e:
                            logger.warning("Error embedding artwork into %s: %s", placed_path, e)

                    if save_cover and cover_bytes:
                        cover_file = placed_path.parent / "cover.jpg"
                        if not cover_file.exists():
                            try:
                                cover_file.write_bytes(cover_bytes)
                                logger.info("Saved album cover to %s", cover_file)
                            except OSError as e:
                                logger.warning("Failed to save cover.jpg at %s: %s", cover_file, e)

                if not imported_paths:
                    logger.error("No audio files were successfully imported for download %s", download_id)
                    err_msg = "Destination escaped music root or placement failed"
                    db.update_download_status(
                        download_id,
                        status=DownloadStatus.FAILED.value,
                        error_message=err_msg,
                    )
                    _notify_failed(err_msg)
                    try:
                        db.add_to_blocklist(
                            source_title=item.get("title", ""),
                            artist=item.get("artist"),
                            release_guid=item.get("id"),
                            info_hash=item.get("download_hash"),
                            reason=err_msg,
                        )
                    except Exception as bl_err:
                        logger.warning("Failed to add unplaced download to blocklist: %s", bl_err)
                    stats["failed"] += 1
                    continue

                # Update database records
                target_summary = imported_paths[0] if imported_paths else None
                db.update_download_status(
                    download_id,
                    status=DownloadStatus.IMPORTING.value,
                    target_path=target_summary,
                )

                # Native catalog upsert (when library_mode != "lidarr")
                if media_settings.get("library_mode") != "lidarr":
                    for placed_str in imported_paths:
                        try:
                            placed_p = Path(placed_str).resolve()
                            try:
                                f_meta = inspect_audio_file(placed_p)
                            except Exception as insp_err:
                                logger.warning(
                                    "Could not inspect placed audio file %s: %s; using fallback metadata",
                                    placed_p,
                                    insp_err,
                                )
                                f_meta = {}

                            matched_track = placed_to_track.get(placed_str)
                            if matched_track and target_album:
                                artist_id = target_album["artist_id"]
                                artist_row = db.get_library_artist(artist_id) or {
                                    "id": artist_id,
                                    "name": item.get("artist") or "Unknown Artist",
                                    "quality_profile_id": None,
                                }
                                album_id = target_album["id"]
                                album_row = target_album
                                track_id = matched_track["id"]
                                track_row = matched_track
                                db.set_track_monitored(track_id, True)
                            else:
                                artist_name = (
                                    f_meta.get("artist")
                                    or item.get("artist")
                                    or (req.get("artist") if req else None)
                                    or "Unknown Artist"
                                ).strip()

                                # 1. Resolve / upsert LibraryArtist
                                artist_row = None
                                existing_track = None
                                if item.get("track_id"):
                                    existing_track = db.get_library_track(item["track_id"])
                                    if existing_track:
                                        artist_row = db.get_library_artist(existing_track["artist_id"])
                                if not artist_row:
                                    artist_row = db.get_library_artist_by_name(artist_name)
                                if not artist_row:
                                    artist_id = str(uuid.uuid4())
                                    artist_folder = (
                                        str(placed_p.parent.parent)
                                        if placed_p.parent != root_path
                                        else str(placed_p.parent)
                                    )
                                    artist_row = db.upsert_library_artist(
                                        LibraryArtist(
                                            id=artist_id,
                                            name=artist_name,
                                            path=artist_folder,
                                        )
                                    )
                                artist_id = artist_row["id"]

                                # 2. Resolve / upsert LibraryAlbum
                                album_title = (
                                    f_meta.get("album")
                                    or (item.get("title") if item.get("item_type") == "album" else None)
                                    or (req.get("album") or req.get("title") if req else None)
                                    or "Unknown Album"
                                ).strip()
                                year_val = f_meta.get("year")
                                if year_val is None and req and req.get("release_date"):
                                    rdate = str(req["release_date"]).strip()
                                    if len(rdate) >= 4 and rdate[:4].isdigit():
                                        year_val = int(rdate[:4])

                                album_row = None
                                if item.get("album_id"):
                                    album_row = db.get_library_album(item["album_id"])
                                elif item.get("track_id") and existing_track:
                                    album_row = db.get_library_album(existing_track["album_id"])
                                if not album_row:
                                    album_row = db.get_library_album_by_title(artist_id, album_title)
                                if not album_row:
                                    album_id = str(uuid.uuid4())
                                    album_row = db.upsert_library_album(
                                        LibraryAlbum(
                                            id=album_id,
                                            artist_id=artist_id,
                                            title=album_title,
                                            year=year_val,
                                            path=str(placed_p.parent),
                                        )
                                    )
                                album_id = album_row["id"]

                                # 3. Resolve / upsert LibraryTrack
                                track_row = None
                                if item.get("track_id"):
                                    track_row = existing_track or db.get_library_track(item["track_id"])
                                if not track_row:
                                    track_title = (
                                        f_meta.get("title")
                                        or item.get("title")
                                        or (req.get("title") if req else None)
                                        or placed_p.stem
                                    ).strip()
                                    track_num = int(f_meta.get("track_number") or 1)
                                    track_row = db.get_library_track_by_title(
                                        album_id, track_title, track_number=track_num
                                    )
                                    if not track_row:
                                        track_id = str(uuid.uuid4())
                                        track_row = db.upsert_library_track(
                                            LibraryTrack(
                                                id=track_id,
                                                album_id=album_id,
                                                artist_id=artist_id,
                                                title=track_title,
                                                track_number=track_num,
                                                disc_number=int(f_meta.get("disc_number") or 1),
                                                duration_seconds=(
                                                    float(f_meta["duration"])
                                                    if f_meta.get("duration") is not None
                                                    else None
                                                ),
                                            )
                                        )
                                track_id = track_row["id"]

                            # 4. Evaluate cutoff against artist's quality profile (or default)
                            qp_id = artist_row.get("quality_profile_id") or (
                                req.get("quality_profile_id") if req else None
                            )
                            prof_dict = db.get_quality_profile(qp_id) if qp_id else None
                            if not prof_dict:
                                prof_dict = db.get_default_quality_profile()

                            parsed = parse_release_title(item.get("title") or placed_p.name)
                            if parsed.quality == "Unknown":
                                codec_name = str(f_meta.get("codec", "")).upper()
                                if codec_name == "FLAC":
                                    parsed.quality = (
                                        "FLAC 24bit"
                                        if (f_meta.get("bits_per_sample") or 16) > 16
                                        else "FLAC 16bit"
                                    )
                                elif codec_name == "MP3":
                                    br = f_meta.get("bitrate") or 320
                                    parsed.quality = (
                                        "MP3 320"
                                        if br >= 310 or br >= 300000
                                        else "MP3 192"
                                    )
                                elif codec_name in ("AAC", "M4A"):
                                    parsed.quality = "AAC 256"

                            file_size = (
                                placed_p.stat().st_size
                                if placed_p.exists()
                                else int(item.get("size_bytes") or 0)
                            )
                            cutoff_met = True
                            quality_str = parsed.quality
                            if prof_dict:
                                profile_obj = _to_quality_profile(prof_dict)
                                eval_res = evaluate_release(
                                    release=parsed, profile=profile_obj, size_bytes=file_size
                                )
                                quality_str = eval_res.parsed_quality
                                cutoff_met = eval_res.meets_cutoff

                            # 5. Upsert LibraryFile
                            rel_path = (
                                str(placed_p.relative_to(root_path))
                                if placed_p.is_relative_to(root_path)
                                else str(placed_p)
                            )
                            file_id = f"fil-{uuid.uuid4().hex[:12]}"
                            db.upsert_library_file(
                                LibraryFile(
                                    id=file_id,
                                    track_id=track_id,
                                    file_path=str(placed_p),
                                    relative_path=rel_path,
                                    codec=f_meta.get("codec") or placed_p.suffix.lstrip(".").upper(),
                                    bitrate=int(f_meta["bitrate"]) if f_meta.get("bitrate") is not None else None,
                                    sample_rate=int(f_meta["sample_rate"]) if f_meta.get("sample_rate") is not None else None,
                                    bits_per_sample=int(f_meta["bits_per_sample"]) if f_meta.get("bits_per_sample") is not None else None,
                                    quality_name=quality_str,
                                    size_bytes=file_size,
                                    cutoff_met=cutoff_met,
                                )
                            )
                            logger.info(
                                "Native catalog upserted file %s for track %s (cutoff_met=%s)",
                                file_id,
                                track_id,
                                cutoff_met,
                            )
                        except Exception as upsert_err:
                            logger.exception(
                                "Error upserting native library records for %s: %s",
                                placed_str,
                                upsert_err,
                            )
                if item.get("request_id"):
                    db.update_request_status(item["request_id"], RequestStatus.AVAILABLE.value)
                    try:
                        parsed = parse_release_title(item.get("title") or "")
                        if parsed.quality == "Unknown" and last_metadata:
                            codec = str(last_metadata.get("codec", "")).upper()
                            if codec == "FLAC":
                                parsed.quality = "FLAC 24bit" if (last_metadata.get("bits_per_sample") or 16) > 16 else "FLAC 16bit"
                            elif codec == "MP3":
                                br = last_metadata.get("bitrate") or 320
                                parsed.quality = "MP3 320" if br >= 310 or br >= 300000 else "MP3 192"
                            elif codec in ("AAC", "M4A"):
                                parsed.quality = "AAC 256"

                        profile_dict = None
                        if req and req.get("quality_profile_id"):
                            profile_dict = db.get_quality_profile(req["quality_profile_id"])
                        if not profile_dict:
                            profile_dict = db.get_default_quality_profile()

                        if profile_dict:
                            profile = _to_quality_profile(profile_dict)
                            eval_res = evaluate_release(
                                release=parsed,
                                profile=profile,
                                size_bytes=item.get("size_bytes"),
                            )
                            current_q = eval_res.parsed_quality
                            cutoff_met_val = 1 if eval_res.meets_cutoff else 0
                            db.update_request_quality(
                                item["request_id"],
                                current_quality=current_q,
                                cutoff_met=cutoff_met_val,
                            )
                    except Exception as ex:
                        logger.warning("Error evaluating release quality for request %s: %s", item.get("request_id"), ex)

                should_keep_seeding = False
                if media_settings.get("delete_completed_transfers"):
                    seed_ratio_limit = media_settings.get("seed_ratio_limit")
                    seed_time_limit_minutes = media_settings.get("seed_time_limit_minutes")
                    if import_mode == "hardlink" and (seed_ratio_limit is not None or seed_time_limit_minutes is not None):
                        cur_ratio = float(status_dict.get("ratio") or 0.0)
                        cur_seeding_sec = int(status_dict.get("seeding_time_seconds") or 0)
                        ratio_met = seed_ratio_limit is not None and cur_ratio >= float(seed_ratio_limit)
                        time_met = seed_time_limit_minutes is not None and cur_seeding_sec >= int(seed_time_limit_minutes) * 60
                        limit_reached = ratio_met or time_met
                        if limit_reached:
                            try:
                                driver.cleanup_completed(target_lookup, delete_files=False)
                            except Exception as ex:
                                logger.warning("Error during cleanup_completed for %s: %s", target_lookup, ex)
                        else:
                            should_keep_seeding = True
                    else:
                        try:
                            driver.cleanup_completed(target_lookup, delete_files=False)
                        except Exception as ex:
                            logger.warning("Error during cleanup_completed for %s: %s", target_lookup, ex)

                if should_keep_seeding:
                    db.update_download_status(
                        download_id,
                        status=DownloadStatus.COMPLETED.value,
                        target_path=target_summary,
                    )
                else:
                    db.update_download_status(
                        download_id,
                        status=DownloadStatus.IMPORTED.value,
                        target_path=target_summary,
                    )

                stats["imported"] += 1

                try:
                    notification_dispatcher.dispatch(
                        NotificationEvent.ITEM_AVAILABLE,
                        data={
                            "artist": item.get("artist"),
                            "title": item.get("title"),
                            "album": item.get("title") if item.get("item_type") == "album" else None,
                            "request_id": item.get("request_id"),
                            "download_id": download_id,
                            "target_path": target_summary,
                            "cover_url": req.get("cover_url") if req else None,
                            "username": req.get("username") if req else None,
                        },
                        db=db,
                    )
                except Exception as ex:
                    logger.warning("Failed to dispatch ITEM_AVAILABLE notification for native import: %s", ex)

                # Trigger Plex library refresh ping
                if plex_client:
                    try:
                        plex_client.refresh_music_library()
                    except Exception as e:
                        logger.warning("Error triggering Plex library refresh: %s", e)

            else:
                # Update progress and active status
                db.update_download_status(download_id, status=cur_status)

        return stats


# Global acquisition worker instance
acquisition_worker = AcquisitionWorker()

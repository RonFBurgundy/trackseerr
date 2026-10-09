"""Download Monitor & Automated Library Organizer Worker.

Periodically inspects active downloads across configured download clients,
detects completed transfers, inspects audio tags, calculates destination
paths via the token template engine, performs atomic file moves with
collision resolution into /music, and triggers Plex library update pings.
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from trackseerr import delay_gate
from trackseerr.acquisition_coordinator import _to_quality_profile
from trackseerr.clients.acquisition import get_acquisition_driver, is_torrent_driver_type
from trackseerr.mb_metadata_store import get_shared_enricher
from trackseerr.clients.plex import PlexClient
from trackseerr.media_servers import as_media_server
from trackseerr.job_tracker import job_tracker, summarize_result
from trackseerr.task_manager import TRIGGER_SCHEDULED, record_finished_run
from trackseerr.download_roots import AllowedRoots, allowed_roots_for_all_clients, allowed_roots_for_client
from trackseerr.import_security import (
    clear_exec_bits,
    quarantine_files,
    verify_files,
)
from trackseerr.recycle_bin import (
    is_system_dirname,
    is_system_filename,
    EXCLUDED_DIRNAMES,
    DisposeResult,
    dispose_for_settings,
    effective_quarantine_path,
    library_excluded_paths,
    log_recycled,
    recycle_in_place_target,
    recycle_replaced_files,
    restore_recycled,
)
from trackseerr.import_quality_check import CHECK_OFF, check_files, normalize_check_mode
from trackseerr.item_history import download_trigger_kwargs, emit
from trackseerr.library_health import record_weak_match
from trackseerr.library_monitoring import NATIVE_MONITOR_OPTIONS
from trackseerr.library_manager import ModeChanged, run_guarded
from trackseerr.library import (
    AUDIO_EXTENSIONS,
    build_tags_to_write,
    embed_album_artwork,
    ArchiveLimitError,
    extract_archive,
    inspect_audio_file,
    is_archive_file,
    resolve_collision,
    write_audio_tags,
)
from trackseerr.models import (
    DownloadStatus,
    LibraryAlbum,
    LibraryArtist,
    LibraryFile,
    LibraryTrack,
    NotificationEvent,
    RequestStatus,
)
from trackseerr.naming import build_track_path
from trackseerr.notifications import notification_dispatcher
from trackseerr.redaction import safe_exc
from trackseerr.quality import evaluate_release, parse_release_title
from trackseerr.storage import Database

from trackseerr.import_files import (
    _is_safe_cover_url,
    effective_import_mode,
    place_audio_file,
    prepare_file_for_tagging,
    translate_remote_path,
)
from trackseerr.seed_safety import (
    _under_path,
    seed_action,
    settle_transfer_after_import,
)
from trackseerr.track_matching import (
    MATCH_STRONG,
    _fingerprint_fallback_match,
    reconcile_audio_file_to_track_scored,
    resolve_download_expected_tracks,
)

logger = logging.getLogger(__name__)


def _quality_from_codec(meta: dict[str, Any]) -> Optional[str]:
    """Quality id for a file whose title said nothing, from its mutagen codec/bitrate (None when unrecognised)."""
    codec = str(meta.get("codec", "")).upper()
    bits = meta.get("bits_per_sample") or 16
    raw_br = meta.get("bitrate") or 320
    kbps = raw_br / 1000 if raw_br > 1000 else raw_br
    if codec == "FLAC":
        return "FLAC 24bit" if bits > 16 else "FLAC 16bit"
    if codec == "ALAC":
        return "ALAC"
    if codec in ("WAV", "AIFF"):
        return "WAV/AIFF"
    if codec == "OPUS":
        return "Opus"
    if codec in ("VORBIS", "OGG"):
        return "OGG Vorbis"
    if codec == "MP3":
        return "MP3 320" if kbps >= 310 else "MP3 192"
    if codec in ("AAC", "M4A"):
        return "AAC 256" if kbps >= 240 or not meta.get("bitrate") else "AAC (other)"
    return None


def _scan_monitor_option(media_settings: dict[str, Any]) -> str:
    """Monitor option for artists created by an import: the configured scan default, never the model default ``all``."""
    option = str(media_settings.get("scan_monitor_option") or "existing")
    return option if option in NATIVE_MONITOR_OPTIONS else "existing"


def record_import_events(
    db: Any,
    item: dict[str, Any],
    track_id: str,
    placed: Path,
    quality: Optional[str],
    meta: dict[str, Any],
    replaced_rows: list[dict[str, Any]],
) -> None:
    """``imported`` for a freshly linked file, plus ``upgraded`` (old -> new quality) when it replaced other files."""
    download_id = str(item.get("id") or "") or None
    provenance = download_trigger_kwargs(db, download_id)
    codec = meta.get("codec") or placed.suffix.lstrip(".").upper()
    common: dict[str, Any] = {
        "track_id": track_id, "request_id": item.get("request_id"), "download_id": download_id, **provenance,
    }
    emit(
        db, "imported", message=f"Imported {placed.name}",
        details={"path": str(placed), "quality": quality, "codec": codec, "release": item.get("title")}, **common,
    )
    if replaced_rows:
        old_quality = ", ".join(dict.fromkeys(str(r.get("quality_name") or "Unknown") for r in replaced_rows))
        emit(
            db, "upgraded", message=f"{old_quality} -> {quality}",
            details={"from_quality": old_quality, "to_quality": quality, "release": item.get("title")}, **common,
        )


@dataclass(frozen=True)
class _PollContext:
    """Settings and clients snapshot for a single acquisition poll pass."""

    media_settings: dict[str, Any]
    write_tags: bool
    embed_art: bool
    save_cover: bool
    plex_client: Optional[PlexClient]
    lidarr_mode: bool


@dataclass
class _ImportJob:
    """State across import pipeline phases for a single completed download."""

    item: dict[str, Any]
    download_id: Any
    client_id: Any
    client_config: dict[str, Any]
    driver: Any
    driver_type: str
    import_mode: str
    status_dict: dict[str, Any]
    target_lookup: Any
    audio_files: list[Path] = field(default_factory=list)
    probes: dict[str, Any] = field(default_factory=dict)
    req: Optional[dict[str, Any]] = None
    cover_bytes: Optional[bytes] = None
    target_album: Optional[dict[str, Any]] = None
    root_path: Optional[Path] = None
    expected_tracks: list[dict[str, Any]] = field(default_factory=list)
    remaining_expected_tracks: list[dict[str, Any]] = field(default_factory=list)
    imported_paths: list[str] = field(default_factory=list)
    held_files: list[str] = field(default_factory=list)
    held_msg: Optional[str] = None
    placed_to_track: dict[str, dict[str, Any]] = field(default_factory=dict)
    recycled_in_place: dict[str, tuple[DisposeResult, dict[str, Any]]] = field(default_factory=dict)
    last_metadata: dict[str, Any] = field(default_factory=dict)
    replaced_kept: list[str] = field(default_factory=list)
    replaced_retired: list[str] = field(default_factory=list)
    replacement_issue_id: Optional[str] = None
    target_summary: Optional[str] = None
    should_keep_seeding: bool = False


class AcquisitionWorker:
    """Thread-safe background runner monitoring active downloads and organizing media."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running: bool = False
        self.poll_interval: float = 5.0
        self.staging_dir: str = ""
        self.allowed_roots: Optional[AllowedRoots] = None
        self._archive_errors: list[str] = []
        self._excluded_paths: list[Path] = []  # effective recycle + quarantine folders, skipped by download-root walks

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
                        tick_started = time.monotonic()
                        stats = self.poll_once(db=db, plex_client=current_plex, staging_dir=self.staging_dir)
                        if isinstance(stats, dict) and stats.get("polled"):  # idle 5s ticks would flood the job list; only record real work
                            job_tracker.record_completed(
                                "download_queue_monitor",
                                "Acquisition Worker",
                                int((time.monotonic() - tick_started) * 1000),
                                summarize_result(stats),
                            )
                            record_finished_run(
                                db, "download_queue_monitor", TRIGGER_SCHEDULED, tick_started, summarize_result(stats)
                            )
                    except Exception as e:
                        logger.error("Unexpected error in AcquisitionWorker poll cycle: %s", e)

                    # Sleep with responsive stop checking
                    slept = 0.0
                    while slept < self.poll_interval and not self._stop_event.is_set():
                        step = min(0.5, self.poll_interval - slept)
                        self._stop_event.wait(step)
                        slept += step

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

    def _note_archive_failure(self, archive: Path, exc: Exception) -> None:
        """Logs an extraction failure; limit violations are also remembered so the poll can record a system event."""
        logger.warning("Failed to extract archive %s: %s: %s", archive, type(exc).__name__, exc)
        if isinstance(exc, ArchiveLimitError):
            self._archive_errors.append(f"{archive.name}: {exc}")

    def _is_excluded_dir_entry(self, f_path: Path, root: Path) -> bool:
        """True for a file inside a quarantine/recycle folder (legacy, default or configured) under ``root``."""
        try:
            parts = f_path.relative_to(root).parts
        except ValueError:
            parts = ()
        return any(p in EXCLUDED_DIRNAMES for p in parts) or any(_under_path(f_path, ex) for ex in self._excluded_paths)

    def _effective_roots(self) -> AllowedRoots:
        """Roots for the current item (set per client in the poll loop); bare staging dir when none is set."""
        if self.allowed_roots is not None:
            return self.allowed_roots
        return AllowedRoots(roots=[Path(self.staging_dir).resolve()] if self.staging_dir else [])

    def _recycle_roots(self, db: Database, media_settings: dict[str, Any]) -> list[Path]:
        """Every download-client root (all clients, not only the current one) plus the current and staging roots."""
        roots: list[Path] = list(self._effective_roots().roots)
        if self.staging_dir:
            roots.append(Path(self.staging_dir).resolve())
        try:
            roots.extend(allowed_roots_for_all_clients(db, media_settings).roots)
        except (sqlite3.Error, OSError, ValueError) as exc:
            logger.warning("Could not read every download client's folders for the recycle check: %s", safe_exc(exc))
        return list(dict.fromkeys(roots))

    def _dispose(
        self, db: Database, media_settings: dict[str, Any], library_root: Path, old_p: Path
    ) -> DisposeResult:
        return dispose_for_settings(media_settings, library_root, old_p, self._recycle_roots(db, media_settings))

    def _recycle_in_place_target(
        self,
        db: Database,
        media_settings: dict[str, Any],
        library_root: Path,
        desired: Path,
        matched_track: Optional[dict[str, Any]],
    ) -> Optional[tuple[DisposeResult, dict[str, Any]]]:
        """See ``recycle_bin.recycle_in_place_target`` (shared with manual import)."""
        track_id = str(matched_track.get("id") or "") if matched_track else None
        return recycle_in_place_target(
            db, media_settings, library_root, desired, track_id, self._recycle_roots(db, media_settings)
        )

    def _log_recycled(
        self,
        db: Database,
        item: dict[str, Any],
        result: DisposeResult,
        new_path: Path,
        old_row: dict[str, Any],
        new_quality: str,
        media_settings: dict[str, Any],
        issue_id: Optional[str],
        retired: list[str],
    ) -> None:
        """Logs and records the history event for a replaced file, and feeds the issue's admin comment."""
        log_recycled(
            db, result, new_path, old_row, new_quality,
            title=str(item.get("title", "")), download_id=item.get("id"), issue_id=issue_id, retired=retired,
        )

    def _recycle_replaced_files(
        self,
        db: Database,
        item: dict[str, Any],
        issue_id: Optional[str],
        old_rows: list[dict[str, Any]],
        new_path: Path,
        library_root: Path,
        media_settings: dict[str, Any],
        new_quality: str,
        retired: list[str],
        kept: list[str],
    ) -> None:
        """Recycles a track's previous library files after an import replaced them (see ``recycle_replaced_files``)."""
        recycle_replaced_files(
            db, old_rows, new_path, library_root, media_settings, self._recycle_roots(db, media_settings),
            new_quality, retired, kept,
            title=str(item.get("title", "")), download_id=item.get("id"), issue_id=issue_id,
        )

    def _extract_into(self, archive: Path, root: Path) -> list[Path]:
        """Extracts ``archive`` into a fresh ``_extracted_*`` folder under ``root``; failures are noted, not raised."""
        extract_dir = root / f"_extracted_{archive.stem}_{os.getpid()}_{time.time_ns()}"
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            return list(extract_archive(archive, extract_dir))
        except Exception as e:
            self._note_archive_failure(archive, e)
            return []

    def _find_audio_files(self, candidate_path: Optional[str | Path], search_term: str) -> list[Path]:
        """Locates downloaded audio files from the source path or, failing that, the allowed download roots.

        A candidate is used only if it passes ``AllowedRoots.check`` (under a client-reported or legacy staging root,
        never overlapping the library or config dir). Archives are extracted into a subfolder of the root that holds
        them. The search-term fallback scans every allowed root, skipping the library and quarantine folders.
        """
        found: list[Path] = []
        allowed = self._effective_roots()

        if candidate_path:
            ok, reason = allowed.check(candidate_path)
            src_path = Path(candidate_path).resolve()
            root = allowed.matching_root(src_path) if ok else None
            if not ok or root is None:
                logger.warning("Rejecting source path %s: %s", candidate_path, reason)
            elif src_path.is_file():
                if src_path.suffix.lower() in AUDIO_EXTENSIONS:
                    return [src_path]
                if is_archive_file(src_path):
                    extracted = self._extract_into(src_path, root)
                    if extracted:
                        return sorted(extracted)
            elif src_path.is_dir():
                archives_in_src: list[Path] = []
                for walk_root, walk_dirs, files in os.walk(str(src_path)):
                    walk_dirs[:] = [d for d in walk_dirs if not is_system_dirname(d)]
                    for f in files:
                        if is_system_filename(f):
                            continue
                        f_path = (Path(walk_root) / f).resolve()
                        if not allowed.is_allowed(f_path) or self._is_excluded_dir_entry(f_path, root):
                            continue
                        if f_path.suffix.lower() in AUDIO_EXTENSIONS:
                            found.append(f_path)
                        elif is_archive_file(f_path):
                            archives_in_src.append(f_path)
                if found:
                    return sorted(found)
                for arc_path in archives_in_src:
                    found.extend(self._extract_into(arc_path, root))
                if found:
                    return sorted(found)

        # Fallback: scan the allowed roots for files matching the search term
        clean_term = search_term.lower()
        if not clean_term:
            return sorted(found)
        scan_archives: list[tuple[Path, Path]] = []
        for scan_root in allowed.usable_roots():
            if not scan_root.exists():
                continue
            for walk_root, dirs, files in os.walk(str(scan_root)):
                # Prune the library and the quarantine so a broad legacy root (e.g. /data) never sweeps them in.
                dirs[:] = [
                    d for d in dirs
                    if d not in EXCLUDED_DIRNAMES
                    and not is_system_dirname(d)
                    and not any(_under_path(Path(walk_root, d).resolve(), ex) for ex in self._excluded_paths)
                    and not (allowed.library_root is not None and _under_path(Path(walk_root, d).resolve(), allowed.library_root))
                ]
                for f in files:
                    if is_system_filename(f):
                        continue
                    f_path = (Path(walk_root) / f).resolve()
                    if not allowed.is_allowed(f_path):
                        continue
                    if clean_term in f.lower() or clean_term in walk_root.lower():
                        if f_path.suffix.lower() in AUDIO_EXTENSIONS:
                            found.append(f_path)
                        elif is_archive_file(f_path):
                            scan_archives.append((f_path, scan_root))

        if not found:
            for arc_path, scan_root in scan_archives:
                found.extend(self._extract_into(arc_path, scan_root))

        return sorted(dict.fromkeys(found))

    def poll_once(
        self,
        db: Database,
        plex_client: Optional[PlexClient] = None,
        staging_dir: Optional[str] = None,
    ) -> dict[str, int]:
        """One poll cycle, run under the library-manager guard so the mode cannot flip mid-cycle."""
        try:
            return run_guarded(db, lambda: self._poll_once(db, plex_client, staging_dir))
        except ModeChanged:
            logger.warning("AcquisitionWorker: library manager changed repeatedly; skipping this poll cycle")
            return {"polled": 0, "completed": 0, "failed": 0, "imported": 0}

    def _poll_once(
        self,
        db: Database,
        plex_client: Optional[PlexClient] = None,
        staging_dir: Optional[str] = None,
    ) -> dict[str, int]:
        media_settings = db.get_media_management_settings()
        write_tags = bool(media_settings.get("write_audio_tags", True))
        embed_art = bool(media_settings.get("embed_artwork", True))
        save_cover = bool(media_settings.get("save_cover_art_file", True))
        if staging_dir:
            self.staging_dir = staging_dir
        else:
            self.staging_dir = str(media_settings.get("staging_folder_path") or "").strip()
        if staging_dir:
            media_settings = dict(media_settings, staging_folder_path=staging_dir)
        self._excluded_paths = library_excluded_paths(media_settings)

        stats = {"polled": 0, "completed": 0, "failed": 0, "imported": 0}
        lidarr_mode = media_settings.get("library_mode") == "lidarr"
        active_items = db.list_active_downloads(
            statuses=[
                DownloadStatus.QUEUED.value,
                DownloadStatus.DOWNLOADING.value,
                DownloadStatus.IMPORTING.value,
                DownloadStatus.COMPLETED.value,
            ]
        )

        # Exactly one side owns each item: in lidarr mode only Lidarr-driver items are followed (nothing is done
        # natively), in native mode Lidarr-driver items are left alone (Lidarr must not be contacted).
        active_items = [
            i for i in active_items if (str(i.get("client_driver_type") or "").lower() == "lidarr") == lidarr_mode
        ]
        if lidarr_mode and not active_items:
            logger.debug("AcquisitionWorker: library manager is Lidarr; no native download polling")

        if not active_items:
            return stats


        ctx = _PollContext(
            media_settings=media_settings,
            write_tags=write_tags,
            embed_art=embed_art,
            save_cover=save_cover,
            plex_client=plex_client,
            lidarr_mode=lidarr_mode,
        )

        for item in active_items:
            self._process_item(db, ctx, item, stats)

        return stats

    def _notify_download_failed(
        self,
        db: Database,
        item: dict[str, Any],
        err_text: str,
    ) -> None:
        """Record a download failure event and dispatch a failure notification."""
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
            req_info = db.get_request(item["request_id"]) if item.get("request_id") else None
            notification_dispatcher.dispatch(
                NotificationEvent.DOWNLOAD_FAILED,
                data={
                    "artist": item.get("artist"),
                    "title": item.get("title"),
                    "download_id": item["id"],
                    "request_id": item.get("request_id"),
                    "user_id": req_info.get("user_id") if req_info else None,
                    "error_message": err_text,
                },
                db=db,
            )
        except Exception as ex:
            logger.warning("Failed to dispatch DOWNLOAD_FAILED notification: %s", ex)

    def _process_item(
        self,
        db: Database,
        ctx: _PollContext,
        item: dict[str, Any],
        stats: dict[str, int],
    ) -> None:
        """Poll driver status for a single download item and trigger import if ready."""
        self.allowed_roots = None
        download_id = item["id"]
        client_id = item.get("client_id")
        media_settings = ctx.media_settings
        stats["polled"] += 1
        client_config = db.get_download_client(client_id) if client_id else None
        if not client_config:
            logger.warning("Download client %s not found for active download %s", client_id, download_id)
            err_msg = f"Client '{client_id}' not found"
            db.update_download_status(
                download_id,
                status=DownloadStatus.FAILED.value,
                error_message=err_msg,
            )
            self._notify_download_failed(db, item, err_msg)
            stats["failed"] += 1
            return

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
            self._notify_download_failed(db, item, err_msg)
            stats["failed"] += 1
            return

        # Poll driver status
        target_lookup = item.get("download_hash") or download_id
        try:
            status_dict = driver.get_status(target_lookup)
        except Exception as e:
            logger.warning("Exception querying driver status for %s: %s", target_lookup, e)
            return

        cur_status = status_dict.get("status", DownloadStatus.DOWNLOADING.value).lower()
        progress = float(status_dict.get("progress") or 0.0)
        size_bytes = status_dict.get("size_bytes")
        db.update_download_progress(download_id, progress, size_bytes)
        if "ratio" in status_dict:  # torrent clients report seeding progress; the queue shows it for held downloads
            try:
                db.record_seed_progress(
                    download_id,
                    float(status_dict.get("ratio") or 0.0),
                    int(status_dict.get("seeding_time_seconds") or 0),
                )
            except (sqlite3.Error, TypeError, ValueError) as seed_err:
                logger.warning("Could not record seed progress for %s: %s", download_id, seed_err)

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
            self._notify_download_failed(db, item, err_msg)
            try:
                db.add_to_blocklist(
                    source_title=item.get("title", ""),
                    artist=item.get("artist"),
                    release_guid=item.get("id"),
                    info_hash=item.get("download_hash"),
                    reason=err_msg,
                    download_id=download_id,
                )
            except Exception as bl_err:
                logger.warning("Failed to add failed download to blocklist: %s", bl_err)
            stats["failed"] += 1
            return

        # If completed or ready to import
        is_ready = cur_status == DownloadStatus.COMPLETED.value or item.get("status") == DownloadStatus.COMPLETED.value
        already_imported = bool(item.get("target_path"))

        if already_imported and is_ready:
            # Torrent already imported, currently seeding under governance
            if seed_action(media_settings) != "keep" and int(item.get("cleanup_attempts") or 0) < 3:
                db.update_download_status(
                    download_id,
                    status=settle_transfer_after_import(
                        driver,
                        target_lookup,
                        media_settings,
                        effective_import_mode(client_config.get("driver_type"), media_settings),
                        status_dict,
                        item,
                        db,
                    ),
                )
            return


        if is_ready:
            self._import_ready_item(
                db,
                ctx,
                item,
                client_config,
                driver,
                status_dict,
                stats,
            )
        else:
            # Update progress and active status
            db.update_download_status(download_id, status=cur_status)

    def _finalize_lidarr_item(
        self,
        db: Database,
        ctx: _PollContext,
        item: dict[str, Any],
        stats: dict[str, int],
    ) -> None:
        """Finalize and record availability for a Lidarr-managed download."""
        download_id = item["id"]
        plex_client = ctx.plex_client
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
                    "user_id": req_row.get("user_id") if req_row else None,
                },
                db=db,
            )
        except Exception as ex:
            logger.warning("Failed to dispatch ITEM_AVAILABLE notification for Lidarr import: %s", ex)

        if plex_client:
            try:
                as_media_server(plex_client).refresh_library()
            except Exception as e:
                logger.warning("Error refreshing Plex after Lidarr import: %s", e)

    def _import_ready_item(
        self,
        db: Database,
        ctx: _PollContext,
        item: dict[str, Any],
        client_config: dict[str, Any],
        driver: Any,
        status_dict: dict[str, Any],
        stats: dict[str, int],
    ) -> None:
        """Import audio files for a completed download into the music library."""
        download_id = item["id"]
        client_id = item.get("client_id")
        target_lookup = item.get("download_hash") or download_id
        media_settings = ctx.media_settings

        stats["completed"] += 1
        db.update_download_status(download_id, status=DownloadStatus.IMPORTING.value)

        # Special case: Lidarr performs native file organization
        driver_type = str(client_config.get("driver_type", "")).lower()
        import_mode = effective_import_mode(driver_type, media_settings)
        if driver_type == "lidarr":
            self._finalize_lidarr_item(db, ctx, item, stats)
            return

        job = _ImportJob(
            item=item,
            download_id=download_id,
            client_id=client_id,
            client_config=client_config,
            driver=driver,
            driver_type=driver_type,
            import_mode=import_mode,
            status_dict=status_dict,
            target_lookup=target_lookup,
        )

        if not self._locate_audio_files(db, ctx, job, stats):
            return
        if not self._security_gate(db, ctx, job, stats):
            return
        if not self._bitrate_gate(db, ctx, job, stats):
            return
        self._prepare_placement(db, ctx, job)
        self._place_audio_files(db, ctx, job)
        if not self._check_placement(db, ctx, job, stats):
            return
        self._record_imported_files(db, ctx, job)
        self._upsert_native_catalog(db, ctx, job)
        self._complete_request(db, ctx, job)
        self._settle_download(db, ctx, job, stats)

    def _locate_audio_files(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        stats: dict[str, int],
    ) -> bool:
        """Locate downloaded audio files with remote path translation."""
        mappings: list[dict[str, str]] = []
        extra_json = job.client_config.get("extra_settings_json")
        if extra_json:
            try:
                extra_data = json.loads(extra_json) if isinstance(extra_json, str) else extra_json
                if isinstance(extra_data, dict):
                    mappings = extra_data.get("remote_path_mappings", [])
            except (json.JSONDecodeError, TypeError):
                mappings = []

        raw_src = job.status_dict.get("source_path") or job.item.get("source_path")
        candidate_src = translate_remote_path(raw_src, mappings) if raw_src else None
        client_label = str(job.client_config.get("name") or job.client_id or "download client")
        self.allowed_roots = allowed_roots_for_client(db, ctx.media_settings, job.client_config, driver=job.driver)
        if not self.allowed_roots.roots:
            reason = "; ".join(self.allowed_roots.errors) or f"Could not read download folder from {client_label}"
            err_msg = f"{reason}; check client connection"
            logger.warning("Download %s cannot be imported yet: %s", job.download_id, err_msg)
            db.update_download_status(job.download_id, status=DownloadStatus.COMPLETED.value, error_message=err_msg)
            self.allowed_roots = None
            return False
        if candidate_src:
            ok, reject_reason = self.allowed_roots.check(candidate_src)
            if not ok:
                logger.warning("Rejecting source path %s from %s: %s", candidate_src, client_label, reject_reason)
                candidate_src = None

        search_term = job.item.get("title") or job.item.get("artist") or ""
        self._archive_errors = []
        job.audio_files = self._find_audio_files(candidate_src, search_term)

        if self._archive_errors and not job.audio_files:
            err_msg = "Archive rejected: " + "; ".join(self._archive_errors)
            try:
                db.record_event(
                    "import_security",
                    f"Archive limits exceeded for '{job.item.get('title', '')}': {err_msg}",
                    source="AcquisitionWorker",
                    severity="error",
                    details={"download_id": job.download_id, "archives": list(self._archive_errors)},
                )
            except sqlite3.Error as ev_err:
                logger.warning("Failed to record import_security event: %s", ev_err)
            db.update_download_status(job.download_id, status=DownloadStatus.FAILED.value, error_message=err_msg)
            self._notify_download_failed(db, job.item, err_msg)
            try:
                db.add_to_blocklist(
                    source_title=job.item.get("title", ""),
                    artist=job.item.get("artist"),
                    release_guid=job.item.get("id"),
                    info_hash=job.item.get("download_hash"),
                    reason=err_msg,
                    download_id=job.download_id,
                )
            except Exception as bl_err:
                logger.warning("Failed to add archive-rejected download to blocklist: %s", bl_err)
            stats["failed"] += 1
            return False

        if not job.audio_files:
            logger.warning(
                "Download %s marked completed but no audio files found at %s or download roots %s",
                job.download_id,
                candidate_src,
                ", ".join(str(r) for r in self._effective_roots().roots),
            )
            err_msg = "No audio files found for import in download staging"
            db.update_download_status(
                job.download_id,
                status=DownloadStatus.FAILED.value,
                error_message=err_msg,
            )
            self._notify_download_failed(db, job.item, err_msg)
            try:
                db.add_to_blocklist(
                    source_title=job.item.get("title", ""),
                    artist=job.item.get("artist"),
                    release_guid=job.item.get("id"),
                    info_hash=job.item.get("download_hash"),
                    reason=err_msg,
                    download_id=job.download_id,
                )
            except Exception as bl_err:
                logger.warning("Failed to add missing-audio download to blocklist: %s", bl_err)
            stats["failed"] += 1
            return False
        return True

    def _security_gate(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        stats: dict[str, int],
    ) -> bool:
        """Verify audio files against security checks, quarantining any offenders."""
        # Security gate (always on, any import_bitrate_check mode): magic bytes + header parse. One bad file
        # rejects the whole release; offenders are quarantined, never imported.
        job.probes = {}
        security = verify_files(job.audio_files, job.probes)
        if security.failed:
            err_msg = f"Security check failed: {security.reason()}"
            # Torrent sources keep seeding from their download folder, so they are copied, never moved. Usenet,
            # Soulseek and staging sources have nothing seeding and are moved out of the download folder.
            q_root = effective_quarantine_path(ctx.media_settings)
            keep_sources = is_torrent_driver_type(job.driver_type)
            if q_root is None:
                # No quarantine or library root is configured: never fall back to the process cwd.
                logger.warning(
                    "No quarantine folder or library root configured; leaving rejected files of download "
                    "%s in place (release is still refused).", job.download_id,
                )
                moved = []
            else:
                moved = quarantine_files(
                    [p for p, _ in security.failures], q_root, str(job.download_id), copy=keep_sources
                )
            try:
                db.record_event(
                    "import_security",
                    f"Security check failed for '{job.item.get('title', '')}': {security.reason()}",
                    source="AcquisitionWorker",
                    severity="error",
                    details={
                        "download_id": job.download_id,
                        "files": [{"file": p, "reason": r} for p, r in security.failures],
                        "quarantined_to": [str(m) for m in moved],
                        "sources_kept_for_seeding": keep_sources,
                    },
                )
            except sqlite3.Error as ev_err:
                logger.warning("Failed to record import_security event: %s", ev_err)
            db.record_download_item_event(
                "quarantined", str(job.download_id), message=f"Import security: {security.reason()}",
                details={
                    "release": job.item.get("title"),
                    "files": [{"file": p, "reason": r} for p, r in security.failures],
                    "quarantined_to": [str(m) for m in moved], "copied": keep_sources,
                },
            )
            logger.error("Import security failure for download %s: %s", job.download_id, err_msg)
            db.update_download_status(job.download_id, status=DownloadStatus.FAILED.value, error_message=err_msg)
            self._notify_download_failed(db, job.item, err_msg)
            try:
                db.add_to_blocklist(
                    source_title=job.item.get("title", ""),
                    artist=job.item.get("artist"),
                    release_guid=job.item.get("id"),
                    info_hash=job.item.get("download_hash"),
                    reason=err_msg,
                    download_id=job.download_id,
                )
            except Exception as bl_err:
                logger.warning("Failed to add security-rejected download to blocklist: %s", bl_err)
            stats["failed"] += 1
            return False
        return True

    def _bitrate_gate(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        stats: dict[str, int],
    ) -> bool:
        """Validate audio file bitrates according to configured check mode."""
        # Per-track bitrate check (media management: import_bitrate_check = off | warn | reject).
        check_mode = normalize_check_mode(ctx.media_settings.get("import_bitrate_check"))
        if check_mode != CHECK_OFF:
            try:
                definitions = {str(d["quality"]): d for d in db.list_quality_definitions()}
                check = check_files(job.audio_files, check_mode, definitions, probes=job.probes)
            except Exception as chk_err:  # noqa: BLE001 - the check is advisory; it must never crash the worker loop
                logger.warning(
                    "Import bitrate check failed for download %s: %s: %s",
                    job.download_id,
                    type(chk_err).__name__,
                    chk_err,
                )
                check = None
            if check is not None and (check.out_of_range or check.skipped):
                summary = check.reason()
                try:
                    db.record_event(
                        "import_bitrate_check",
                        f"Bitrate check ({check_mode}) for '{job.item.get('title', '')}': {summary}",
                        source="AcquisitionWorker",
                        severity="error" if check.failed else "warning",
                        details={
                            "download_id": job.download_id,
                            "mode": check_mode,
                            "checked": check.checked,
                            "out_of_range": [
                                {
                                    "file": f.path,
                                    "quality": f.quality,
                                    "kbps": round(f.kbps, 1),
                                    "min_kbps": f.min_kbps,
                                    "max_kbps": f.max_kbps,
                                    "severity": f.severity,
                                    "detail": f.detail,
                                }
                                for f in check.out_of_range
                            ],
                            "skipped": [{"file": p, "reason": r} for p, r in check.skipped],
                        },
                    )
                except sqlite3.Error as ev_err:
                    logger.warning("Failed to record import_bitrate_check event: %s", ev_err)
                logger.warning("Import bitrate check (%s) for download %s: %s", check_mode, job.download_id, summary)
            if check is not None and check.failed:
                err_msg = f"Bitrate check failed: {check.reason()}"
                db.update_download_status(
                    job.download_id,
                    status=DownloadStatus.FAILED.value,
                    error_message=err_msg,
                )
                self._notify_download_failed(db, job.item, err_msg)
                try:
                    db.add_to_blocklist(
                        source_title=job.item.get("title", ""),
                        artist=job.item.get("artist"),
                        release_guid=job.item.get("id"),
                        info_hash=job.item.get("download_hash"),
                        reason=err_msg,
                        download_id=job.download_id,
                    )
                except Exception as bl_err:
                    logger.warning("Failed to add bitrate-rejected download to blocklist: %s", bl_err)
                stats["failed"] += 1
                return False
        return True

    def _prepare_placement(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
    ) -> None:
        """Prepare target directory, metadata lookup, and cover art before placement."""
        # Organize and move each audio file
        job.imported_paths = []
        root_folder = ctx.media_settings.get("root_folder_path") or "/music"
        job.root_path = Path(root_folder).resolve()

        # Fetch associated request and album cover art if available
        job.req = db.get_request(job.item["request_id"]) if job.item.get("request_id") else None
        job.cover_bytes = None
        if job.req and (ctx.embed_art or ctx.save_cover):
            cover_url = job.req.get("cover_url")
            if cover_url and _is_safe_cover_url(cover_url):
                try:
                    resp = httpx.get(cover_url, timeout=10.0, follow_redirects=True)
                    if resp.status_code == 200 and resp.content:
                        job.cover_bytes = resp.content
                except httpx.HTTPError as e:
                    logger.warning("HTTP error fetching cover art from %s: %s", cover_url, e)
                except Exception as e:
                    logger.warning("Error fetching cover art from %s: %s", cover_url, e)
            elif cover_url:
                logger.warning("Cover art URL rejected by SSRF protection: %s", cover_url)

        job.last_metadata = {}

        # Check if item has album_id or matches an existing album in catalog
        job.target_album, job.expected_tracks = resolve_download_expected_tracks(db, job.item, job.req)

        job.remaining_expected_tracks = list(job.expected_tracks)
        job.placed_to_track = {}
        # placed path -> (recycle result, old file row) for old files recycled right before an in-place replace
        job.recycled_in_place = {}
        # Files with no catalog match when the release has expected tracks: left on disk for manual import.
        job.held_files = []

    def _place_one_file(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        af: Path,
    ) -> None:
        """Place, tag, and enrich a single audio file."""
        assert job.root_path is not None
        try:
            metadata = inspect_audio_file(af)
        except Exception as e:
            logger.warning("Mutagen inspection failed for %s: %s; using item defaults", af, e)
            metadata = {
                "artist": job.item.get("artist", "Unknown Artist"),
                "title": job.item.get("title", af.stem),
                "album": job.item.get("title") if job.item.get("item_type") == "album" else "Unknown Album",
                "file_path": str(af),
                "extension": af.suffix.lower(),
                "track_number": None,
                "disc_number": 1,
                "total_discs": 1,
            }

        # Fallbacks for empty tags
        if not metadata.get("artist"):
            metadata["artist"] = job.item.get("artist") or "Unknown Artist"
        if not metadata.get("title"):
            metadata["title"] = job.item.get("title") or af.stem

        # Reconcile against expected catalog tracks if present
        matched_expected_track = None
        file_weak_strength: Optional[str] = None
        if job.remaining_expected_tracks:
            matched_expected_track, match_strength = reconcile_audio_file_to_track_scored(
                metadata, job.remaining_expected_tracks
            )
            tag_track = matched_expected_track
            matched_expected_track = _fingerprint_fallback_match(
                af, ctx.media_settings, job.remaining_expected_tracks, matched_expected_track, match_strength
            )
            if matched_expected_track is not None and matched_expected_track is tag_track and match_strength != MATCH_STRONG:
                file_weak_strength = match_strength
            if matched_expected_track:
                job.remaining_expected_tracks.remove(matched_expected_track)
                metadata["title"] = matched_expected_track["title"]
                metadata["track_number"] = int(matched_expected_track.get("track_number") or 1)
                metadata["disc_number"] = int(matched_expected_track.get("disc_number") or 1)
                if job.target_album:
                    metadata["album"] = job.target_album["title"]
                    art_cand = db.get_library_artist(job.target_album["artist_id"])
                    if art_cand:
                        metadata["artist"] = art_cand["name"]

        if job.expected_tracks and matched_expected_track is None:
            logger.warning(
                "Holding unmatched file %s for download %s (manual import required)", af, job.download_id
            )
            job.held_files.append(str(af))
            return

        # Disc 1 of a multi-disc release must use the multi-disc format too.
        known_discs = [int(metadata.get("total_discs") or 1)]
        known_discs += [int(t.get("disc_number") or 1) for t in job.expected_tracks]
        metadata["total_discs"] = max(known_discs)

        job.last_metadata = metadata

        target_str = build_track_path(metadata, ctx.media_settings)
        desired_path = Path(target_str).resolve()
        pre_recycled = self._recycle_in_place_target(
            db, ctx.media_settings, job.root_path, desired_path, matched_expected_track
        )
        final_target = desired_path if pre_recycled is not None else resolve_collision(target_str)
        target_path = Path(final_target).resolve()
        if not target_path.is_relative_to(job.root_path):
            logger.error("Destination %s escapes music root %s", target_path, job.root_path)
            if pre_recycled is not None:
                restore_recycled(pre_recycled[0])
            return

        try:
            placed_path = place_audio_file(af, target_path, mode=job.import_mode)
        except Exception:
            if pre_recycled is not None:
                restore_recycled(pre_recycled[0])  # the replacement never landed: put the old bytes back
            raise
        if pre_recycled is not None:
            # The old file's bytes now live in the recycle bin; its row would point at the new file.
            job.recycled_in_place[str(placed_path)] = pre_recycled
        job.imported_paths.append(str(placed_path))
        if matched_expected_track:
            job.placed_to_track[str(placed_path)] = matched_expected_track
            if file_weak_strength is not None:
                record_weak_match(
                    db,
                    str(placed_path),
                    track_id=str(matched_expected_track.get("id") or ""),
                    title=str(matched_expected_track.get("title") or ""),
                    source_name=str(job.item.get("title") or ""),
                    strength=file_weak_strength,
                )
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
        tags_to_write: dict[str, Any] = build_tags_to_write(
            metadata,
            req=job.req,
            audio_files_count=len(job.audio_files),
        )

        # Asynchronously enrich with MBIDs if enabled
        if ctx.media_settings.get("enrich_mbids", True):
            try:
                enricher = get_shared_enricher(db)
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

                if not job.cover_bytes and (ctx.embed_art or ctx.save_cover):
                    rg_id = resolved_mbids.get("musicbrainz_releasegroupid") if resolved_mbids else None
                    rel_id = resolved_mbids.get("musicbrainz_albumid") if resolved_mbids else None
                    resolved_cover_url = enricher.get_cover_art_url(release_group_id=rg_id, release_id=rel_id)
                    if resolved_cover_url and _is_safe_cover_url(resolved_cover_url):
                        try:
                            resp = httpx.get(resolved_cover_url, timeout=5.0, follow_redirects=True)
                            if resp.status_code == 200 and resp.content:
                                job.cover_bytes = resp.content
                        except Exception as exc:
                            logger.debug("Failed fetching cover art from Cover Art Archive %s: %s", resolved_cover_url, exc)
            except Exception as e:
                logger.warning("AcquisitionWorker: MBID enrichment error: %s", e)

        file_write_tags, file_embed_art = ctx.write_tags, ctx.embed_art
        if (file_write_tags or (file_embed_art and job.cover_bytes)) and not prepare_file_for_tagging(placed_path, ctx.media_settings):
            file_write_tags = file_embed_art = False
        if file_write_tags:
            art_to_embed = job.cover_bytes if file_embed_art else None
            try:
                write_audio_tags(placed_path, tags=tags_to_write, cover_art_bytes=art_to_embed)
            except Exception as e:
                logger.warning("Error writing audio tags to %s: %s", placed_path, e)
        elif file_embed_art and job.cover_bytes:
            try:
                embed_album_artwork(placed_path, job.cover_bytes)
            except Exception as e:
                logger.warning("Error embedding artwork into %s: %s", placed_path, e)

        if ctx.save_cover and job.cover_bytes:
            cover_file = placed_path.parent / "cover.jpg"
            if not cover_file.exists():
                try:
                    cover_file.write_bytes(job.cover_bytes)
                    clear_exec_bits(cover_file)
                    logger.info("Saved album cover to %s", cover_file)
                except OSError as e:
                    logger.warning("Failed to save cover.jpg at %s: %s", cover_file, e)

    def _place_audio_files(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
    ) -> None:
        """Place each audio file into the destination library folder."""
        for af in job.audio_files:
            self._place_one_file(db, ctx, job, af)

    def _check_placement(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        stats: dict[str, int],
    ) -> bool:
        """Check placed files against held/error conditions after placement pass."""
        if job.held_files:
            db.set_download_unmatched_files(job.download_id, job.held_files)
            job.held_msg = f"{len(job.held_files)} file(s) couldn't be matched — manual import required"
            if not job.imported_paths:
                # Nothing placed: park the download for manual import (not a failure, no blocklisting).
                db.update_download_status(
                    job.download_id, status=DownloadStatus.WARNING.value, error_message=job.held_msg
                )
                return False

        if not job.imported_paths:
            logger.error("No audio files were successfully imported for download %s", job.download_id)
            err_msg = "Destination escaped music root or placement failed"
            db.update_download_status(
                job.download_id,
                status=DownloadStatus.FAILED.value,
                error_message=err_msg,
            )
            self._notify_download_failed(db, job.item, err_msg)
            try:
                db.add_to_blocklist(
                    source_title=job.item.get("title", ""),
                    artist=job.item.get("artist"),
                    release_guid=job.item.get("id"),
                    info_hash=job.item.get("download_hash"),
                    reason=err_msg,
                    download_id=job.download_id,
                )
            except Exception as bl_err:
                logger.warning("Failed to add unplaced download to blocklist: %s", bl_err)
            stats["failed"] += 1
            return False
        return True

    def _record_imported_files(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
    ) -> None:
        """Update download status and look up replacement issue."""
        job.target_summary = job.imported_paths[0] if job.imported_paths else None
        db.update_download_status(
            job.download_id,
            status=DownloadStatus.IMPORTING.value,
            target_path=job.target_summary,
        )

        # An issue-driven replacement retires the track's previous file(s) once the new row is in.
        job.replacement_issue_id = None
        job.replaced_retired = []
        job.replaced_kept = []
        try:
            job.replacement_issue_id = db.get_download_replacement_issue(str(job.download_id))
        except sqlite3.Error as ri_err:
            logger.warning("Could not look up replacement issue for %s: %s", job.download_id, safe_exc(ri_err))

    def _resolve_catalog_rows(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        placed_p: Path,
        f_meta: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Resolve or upsert artist, album, and track records for an unmatched placed file."""
        assert job.root_path is not None
        artist_name = (
            f_meta.get("artist")
            or job.item.get("artist")
            or (job.req.get("artist") if job.req else None)
            or "Unknown Artist"
        ).strip()

        # 1. Resolve / upsert LibraryArtist
        artist_row = None
        existing_track = None
        if job.item.get("track_id"):
            existing_track = db.get_library_track(job.item["track_id"])
            if existing_track:
                artist_row = db.get_library_artist(existing_track["artist_id"])
        if not artist_row:
            artist_row = db.get_library_artist_by_name(artist_name)
        if not artist_row:
            artist_id = str(uuid.uuid4())
            artist_folder = (
                str(placed_p.parent.parent)
                if placed_p.parent != job.root_path
                else str(placed_p.parent)
            )
            artist_row = db.upsert_library_artist(
                LibraryArtist(
                    id=artist_id,
                    name=artist_name,
                    path=artist_folder,
                    monitor_option=_scan_monitor_option(ctx.media_settings),
                ),
                preserve_monitoring=True,
            )
        artist_id = artist_row["id"]

        # 2. Resolve / upsert LibraryAlbum
        album_title = (
            f_meta.get("album")
            or (job.item.get("title") if job.item.get("item_type") == "album" else None)
            or (job.req.get("album") or job.req.get("title") if job.req else None)
            or "Unknown Album"
        ).strip()
        year_val = f_meta.get("year")
        if year_val is None and job.req and job.req.get("release_date"):
            rdate = str(job.req["release_date"]).strip()
            if len(rdate) >= 4 and rdate[:4].isdigit():
                year_val = int(rdate[:4])

        album_row = None
        if job.item.get("album_id"):
            album_row = db.get_library_album(job.item["album_id"])
        elif job.item.get("track_id") and existing_track:
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
        if job.item.get("track_id"):
            track_row = existing_track or db.get_library_track(job.item["track_id"])
        if not track_row:
            track_title = (
                f_meta.get("title")
                or job.item.get("title")
                or (job.req.get("title") if job.req else None)
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
        return artist_row, album_row, track_row

    def _upsert_catalog_for_file(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        placed_str: str,
        import_tag_cache: dict[str, list[str]],
    ) -> None:
        """Upsert library artist, album, track, and file records for a placed audio file."""
        assert job.root_path is not None
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

            matched_track = job.placed_to_track.get(placed_str)
            if matched_track and job.target_album:
                artist_id = job.target_album["artist_id"]
                artist_row = db.get_library_artist(artist_id) or {
                    "id": artist_id,
                    "name": job.item.get("artist") or "Unknown Artist",
                    "quality_profile_id": None,
                }
                album_id = job.target_album["id"]
                album_row = job.target_album
                track_id = matched_track["id"]
                track_row = matched_track
                db.set_track_monitored(track_id, True)
            else:
                artist_row, album_row, track_row = self._resolve_catalog_rows(db, ctx, job, placed_p, f_meta)
                artist_id = artist_row["id"]
                album_id = album_row["id"]
                track_id = track_row["id"]

            # 4. Evaluate cutoff against artist's quality profile (or default)
            qp_id = artist_row.get("quality_profile_id") or (
                job.req.get("quality_profile_id") if job.req else None
            )
            prof_dict = db.get_quality_profile(qp_id) if qp_id else None
            if not prof_dict:
                prof_dict = db.get_default_quality_profile()

            parsed = parse_release_title(job.item.get("title") or placed_p.name)
            if parsed.quality == "Unknown":
                parsed.quality = _quality_from_codec(f_meta) or parsed.quality

            file_size = (
                placed_p.stat().st_size
                if placed_p.exists()
                else int(job.item.get("size_bytes") or 0)
            )
            cutoff_met = True
            quality_str = parsed.quality
            if prof_dict:
                profile_obj = _to_quality_profile(prof_dict)
                if artist_id not in import_tag_cache:
                    import_tag_cache[artist_id] = delay_gate.artist_tags(
                        db, artist_row.get("name"), artist_id
                    )
                eval_res = evaluate_release(
                    release=parsed,
                    profile=profile_obj,
                    size_bytes=file_size,
                    artist_tags=import_tag_cache[artist_id],
                )
                quality_str = eval_res.parsed_quality
                cutoff_met = eval_res.meets_cutoff

            # 5. Upsert LibraryFile
            rel_path = (
                str(placed_p.relative_to(job.root_path))
                if placed_p.is_relative_to(job.root_path)
                else str(placed_p)
            )
            file_id = f"fil-{uuid.uuid4().hex[:12]}"
            # Every other file the track has is superseded by this import (upgrade or issue
            # replacement), except files this same download placed (a multi-file release).
            sibling_paths = {str(Path(p).resolve()) for p in job.imported_paths} | {str(placed_p)}
            old_file_rows = [
                r for r in db.list_library_files_for_track(track_id)
                if str(r.get("file_path") or "") not in sibling_paths
            ]
            in_place = job.recycled_in_place.pop(str(placed_p), None)
            if in_place is not None:
                old_file_rows = [r for r in old_file_rows if str(r["id"]) != str(in_place[1]["id"])]
                self._log_recycled(db, job.item, in_place[0], placed_p, in_place[1], quality_str, ctx.media_settings,
                                   job.replacement_issue_id, job.replaced_retired)
                try:
                    db.delete_library_file(str(in_place[1]["id"]))
                except sqlite3.Error as del_err:
                    logger.warning("Could not remove stale library file row %s: %s", in_place[1].get("id"), safe_exc(del_err))
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
            record_import_events(
                db, job.item, str(track_id), placed_p, quality_str, f_meta,
                ([in_place[1]] if in_place is not None else []) + old_file_rows,
            )
            if old_file_rows:
                self._recycle_replaced_files(
                    db, job.item, job.replacement_issue_id, old_file_rows, placed_p, job.root_path,
                    ctx.media_settings, quality_str, job.replaced_retired, job.replaced_kept,
                )
        except Exception as upsert_err:
            logger.exception(
                "Error upserting native library records for %s: %s",
                placed_str,
                upsert_err,
            )

    def _upsert_native_catalog(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
    ) -> None:
        """Upsert library catalog records for all placed files if library mode is not lidarr."""
        # Native catalog upsert (when library_mode != "lidarr")
        if ctx.media_settings.get("library_mode") != "lidarr":
            # Artist tag labels by artist id, looked up once per artist across the placed files.
            import_tag_cache: dict[str, list[str]] = {}
            for placed_str in job.imported_paths:
                self._upsert_catalog_for_file(db, ctx, job, placed_str, import_tag_cache)

    def _complete_request(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
    ) -> None:
        """Update request status and evaluate quality cutoff for completed request."""
        if job.item.get("request_id"):
            db.update_request_status(job.item["request_id"], RequestStatus.AVAILABLE.value)
            try:
                parsed = parse_release_title(job.item.get("title") or "")
                if parsed.quality == "Unknown" and job.last_metadata:
                    parsed.quality = _quality_from_codec(job.last_metadata) or parsed.quality

                profile_dict = None
                if job.req and job.req.get("quality_profile_id"):
                    profile_dict = db.get_quality_profile(job.req["quality_profile_id"])
                if not profile_dict:
                    profile_dict = db.get_default_quality_profile()

                if profile_dict:
                    profile = _to_quality_profile(profile_dict)
                    eval_res = evaluate_release(
                        release=parsed,
                        profile=profile,
                        size_bytes=job.item.get("size_bytes"),
                        artist_tags=delay_gate.artist_tags(
                            db, (job.req.get("artist") if job.req else None) or job.item.get("artist")
                        ),
                    )
                    current_q = eval_res.parsed_quality
                    cutoff_met_val = 1 if eval_res.meets_cutoff else 0
                    db.update_request_quality(
                        job.item["request_id"],
                        current_quality=current_q,
                        cutoff_met=cutoff_met_val,
                    )
            except Exception as ex:
                logger.warning("Error evaluating release quality for request %s: %s", job.item.get("request_id"), ex)

    def _settle_download(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        stats: dict[str, int],
    ) -> None:
        """Decide download settling, seeding retention, issue comments, notifications, and Plex refresh."""
        job.should_keep_seeding = False
        # Held files still live in the client's download folder: never remove the transfer while they wait.
        if job.held_files:
            logger.info(
                "Download %s keeps %d unmatched file(s); skipping download-client cleanup",
                job.download_id,
                len(job.held_files),
            )
        else:
            try:
                db.set_download_placed_files(job.download_id, job.imported_paths, job.import_mode)
            except sqlite3.Error as placed_err:
                logger.warning("Could not record placed files for %s: %s", job.download_id, safe_exc(placed_err))
            job.should_keep_seeding = (
                settle_transfer_after_import(
                    job.driver,
                    job.target_lookup,
                    ctx.media_settings,
                    job.import_mode,
                    job.status_dict,
                    db.get_active_download(job.download_id) or job.item,
                    db,
                )
                == DownloadStatus.COMPLETED.value
            )

        if job.held_files:
            db.update_download_status(
                job.download_id,
                status=DownloadStatus.WARNING.value,
                error_message=job.held_msg,
                target_path=job.target_summary,
            )
        elif job.should_keep_seeding:
            db.update_download_status(
                job.download_id,
                status=DownloadStatus.COMPLETED.value,
                target_path=job.target_summary,
            )
        else:
            db.update_download_status(
                job.download_id,
                status=DownloadStatus.IMPORTED.value,
                target_path=job.target_summary,
            )

        stats["imported"] += 1

        try:  # a grab made by an issue's "Search again" tells that issue; the admin decides the status
            issue_id = job.replacement_issue_id
            if issue_id and db.get_issue(issue_id):
                body = "Replacement imported"
                for line in job.replaced_retired:
                    body += f"\nRetired old file: {line}"
                for line in job.replaced_kept:
                    body += f"\nOld file kept at {line}"
                db.add_issue_comment(issue_id, None, body, is_admin=True, is_system=True, staff=True)
        except sqlite3.Error as issue_err:
            logger.warning("Could not comment on the issue for download %s: %s", job.download_id, safe_exc(issue_err))

        try:
            notification_dispatcher.dispatch(
                NotificationEvent.ITEM_AVAILABLE,
                data={
                    "artist": job.item.get("artist"),
                    "title": job.item.get("title"),
                    "album": job.item.get("title") if job.item.get("item_type") == "album" else None,
                    "request_id": job.item.get("request_id"),
                    "download_id": job.download_id,
                    "target_path": job.target_summary,
                    "cover_url": job.req.get("cover_url") if job.req else None,
                    "username": job.req.get("username") if job.req else None,
                    "user_id": job.req.get("user_id") if job.req else None,
                },
                db=db,
            )
        except Exception as ex:
            logger.warning("Failed to dispatch ITEM_AVAILABLE notification for native import: %s", ex)

        # Trigger Plex library refresh ping
        if ctx.plex_client:
            try:
                as_media_server(ctx.plex_client).refresh_library()
            except Exception as e:
                logger.warning("Error triggering Plex library refresh: %s", e)


# Global acquisition worker instance
acquisition_worker = AcquisitionWorker()

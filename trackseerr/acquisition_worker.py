"""Download Monitor & Automated Library Organizer Worker.

Periodically inspects active downloads across configured download clients,
detects completed transfers, inspects audio tags, calculates destination
paths via the token template engine, performs atomic file moves with
collision resolution into /music, and triggers Plex library update pings.
"""

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional


from trackseerr.clients.acquisition import get_acquisition_driver
from trackseerr.clients.plex import PlexClient
from trackseerr.job_tracker import job_tracker, summarize_result
from trackseerr.task_manager import TRIGGER_SCHEDULED, record_finished_run
from trackseerr.download_roots import AllowedRoots, allowed_roots_for_all_clients
from trackseerr.recycle_bin import (
    is_system_dirname,
    is_system_filename,
    EXCLUDED_DIRNAMES,
    DisposeResult,
    dispose_for_settings,
    library_excluded_paths,
    log_recycled,
    recycle_in_place_target,
    recycle_replaced_files,
)
from trackseerr.library_manager import ModeChanged, run_guarded
from trackseerr.library import (
    AUDIO_EXTENSIONS,
    ArchiveLimitError,
    extract_archive,
    is_archive_file,
)
from trackseerr.models import (
    DownloadStatus,
    NotificationEvent,
)
from trackseerr.notifications import notification_dispatcher
from trackseerr.redaction import safe_exc
from trackseerr.storage import Database

from trackseerr.import_files import (
    effective_import_mode,
)
from trackseerr.seed_safety import (
    _under_path,
    seed_action,
    settle_transfer_after_import,
)

logger = logging.getLogger(__name__)
from trackseerr.acquisition_import import (
    ImportPipelineMixin,
    _PollContext,
)


class AcquisitionWorker(ImportPipelineMixin):
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


# Global acquisition worker instance
acquisition_worker = AcquisitionWorker()

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx


from trackseerr.clients.acquisition import get_acquisition_driver, is_torrent_driver_type
from trackseerr.clients.plex import PlexClient
from trackseerr.job_tracker import job_tracker, summarize_result
from trackseerr.task_manager import TRIGGER_SCHEDULED, record_finished_run
from trackseerr.download_folder_cleanup import prune_empty_parents, sweep_empty_dirs
from trackseerr.download_roots import (
    AllowedRoots,
    allowed_roots_for_all_clients,
    allowed_roots_for_client,
    build_allowed_roots,
    client_mappings,
    fetch_client_roots,
)
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
    inspect_audio_file,
    is_archive_file,
)
from trackseerr.models import (
    DownloadStatus,
    NotificationEvent,
    RequestStatus,
)
from trackseerr.notifications import notification_dispatcher
from trackseerr.redaction import safe_exc
from trackseerr.storage import Database
from trackseerr.track_matching import (
    candidate_artist_ids,
    find_fileless_equivalent,
    find_owned_for_item,
    wanted_song,
)

from trackseerr.import_files import (
    translate_remote_path,
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
    _download_artist_id,
)


def _env_minutes(name: str, default: int) -> int:
    """Read a positive-minutes env knob at call time; fall back to ``default`` when missing or invalid."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if value < 1:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default
    return value


def _minutes_since(stamp: Any, now: datetime) -> Optional[float]:
    """Minutes between a SQLite UTC timestamp ("YYYY-MM-DD HH:MM:SS") and ``now``; None if unparsable."""
    if not stamp:
        return None
    try:
        then = datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - then).total_seconds() / 60.0


def _slskd_stall_reason(
    item: dict[str, Any], status_dict: dict[str, Any], now: datetime, limits: dict[str, int]
) -> Optional[str]:
    """Return why a Soulseek transfer should be failed as stuck, or None.

    ``limits`` (minutes) comes from ``_slskd_limits``: ``queue`` is queued/requested with no progress,
    ``stall`` is in progress with no byte movement, ``missing`` is the transfer absent from slskd.
    """
    created_age = _minutes_since(item.get("created_at"), now)
    moved_age = _minutes_since(item.get("progress_updated_at") or item.get("created_at"), now)
    status = str(status_dict.get("status", "")).lower()
    progress = float(status_dict.get("progress") or 0.0)

    if status_dict.get("missing"):
        limit = limits["missing"]
        if created_age is not None and created_age > limit:
            return "Transfer disappeared from slskd"
        return None
    if status == DownloadStatus.QUEUED.value and progress == 0.0:
        limit = limits["queue"]
        if moved_age is not None and moved_age > limit:
            remote = status_dict.get("remote_state") or "queued"
            return f"Peer never started the transfer ({remote}) after {limit} min"
        return None
    if status == DownloadStatus.DOWNLOADING.value and progress == float(item.get("progress") or 0.0):
        limit = limits["stall"]
        if moved_age is not None and moved_age > limit:
            return f"Transfer stalled at {progress:.0f}% for {limit} min"
    return None


def _slskd_limits(client_config: dict[str, Any]) -> dict[str, int]:
    """Stall limits in minutes: the client's ``extra_settings_json`` overrides, else the env knobs, else defaults.

    Client keys: ``queue_timeout_minutes``, ``stall_minutes``, ``missing_minutes`` (positive ints).
    Env: SLSKD_QUEUE_TIMEOUT_MINUTES (15), SLSKD_STALL_MINUTES (15), SLSKD_MISSING_MINUTES (10).
    """
    limits = {
        "queue": _env_minutes("SLSKD_QUEUE_TIMEOUT_MINUTES", 15),
        "stall": _env_minutes("SLSKD_STALL_MINUTES", 15),
        "missing": _env_minutes("SLSKD_MISSING_MINUTES", 10),
    }
    raw = client_config.get("extra_settings_json")
    if not raw:
        return limits
    try:
        extra = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError) as exc:
        logger.warning("Invalid extra_settings_json on slskd client %s; using default timeouts: %s", client_config.get("name"), exc)
        return limits
    if not isinstance(extra, dict):
        return limits
    for key, name in (("queue_timeout_minutes", "queue"), ("stall_minutes", "stall"), ("missing_minutes", "missing")):
        value = extra.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            limits[name] = value
        elif value is not None:
            logger.warning("Ignoring invalid %s=%r on slskd client %s", key, value, client_config.get("name"))
    return limits


def _fail_if_slskd_stalled(
    driver: Any, item: dict[str, Any], status_dict: dict[str, Any], lookup: str, client_config: dict[str, Any]
) -> dict[str, Any]:
    """Cancel a stuck Soulseek transfer and return a FAILED status dict; otherwise return ``status_dict`` unchanged."""
    reason = _slskd_stall_reason(item, status_dict, datetime.now(timezone.utc), _slskd_limits(client_config))
    if not reason:
        return status_dict
    logger.info("slskd download %s stalled: %s", item["id"], reason)
    if not driver.cancel(lookup):
        logger.warning("Could not cancel stalled slskd transfer %s", lookup)
    return {**status_dict, "status": DownloadStatus.FAILED.value, "error_message": reason, "cancelled": True}


def _check_slskd_status(
    db: Database, driver: Any, item: dict[str, Any], status_dict: dict[str, Any], lookup: str,
    client_config: dict[str, Any],
) -> dict[str, Any]:
    """Applies stall detection to a Soulseek status and stores the peer's remote transfer state."""
    status_dict = _fail_if_slskd_stalled(driver, item, status_dict, lookup, client_config)
    try:
        db.set_download_remote_state(str(item["id"]), status_dict.get("remote_state"))
    except sqlite3.Error as exc:
        logger.warning("Could not store remote state for %s: %s", item["id"], exc)
    return status_dict


def _torrent_dead_minutes(client_config: dict[str, Any]) -> int:
    """Minutes a torrent may sit at zero progress before it is failed: client ``dead_torrent_minutes`` > env > 120."""
    default = _env_minutes("TORRENT_DEAD_MINUTES", 120)
    raw = client_config.get("extra_settings_json")
    if not raw:
        return default
    try:
        extra = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError) as exc:
        logger.warning("Invalid extra_settings_json on torrent client %s; using default dead limit: %s", client_config.get("name"), exc)
        return default
    if not isinstance(extra, dict):
        return default
    value = extra.get("dead_torrent_minutes")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if value is not None:
        logger.warning("Ignoring invalid dead_torrent_minutes=%r on torrent client %s", value, client_config.get("name"))
    return default


def _torrent_dead_reason(item: dict[str, Any], status_dict: dict[str, Any], now: datetime, limit: int) -> Optional[str]:
    """Return why a torrent should be failed as dead, or None.

    Seeding safety: only a torrent that has downloaded and uploaded NOTHING qualifies. Any progress (row or
    client), any ratio or seeding time, or any status other than queued/downloading returns None.
    """
    status = str(status_dict.get("status", "")).lower()
    if status not in (DownloadStatus.QUEUED.value, DownloadStatus.DOWNLOADING.value):
        return None
    try:
        untouched = (
            float(status_dict.get("progress") or 0) == 0.0
            and float(item.get("progress") or 0) == 0.0
            and float(status_dict.get("ratio") or 0) == 0.0
            and int(status_dict.get("seeding_time_seconds") or 0) == 0
        )
    except (TypeError, ValueError):
        return None  # unreadable numbers: never risk cancelling
    if not untouched:
        return None
    age = _minutes_since(item.get("progress_updated_at") or item.get("created_at"), now)
    if age is None or age <= limit:
        return None
    return f"Torrent never started downloading after {limit} min (no seeders?)"


def _fail_if_torrent_dead(
    driver: Any, item: dict[str, Any], status_dict: dict[str, Any], lookup: str, client_config: dict[str, Any]
) -> dict[str, Any]:
    """Cancel a torrent that never started (zero progress) and return a FAILED dict; otherwise ``status_dict``."""
    reason = _torrent_dead_reason(item, status_dict, datetime.now(timezone.utc), _torrent_dead_minutes(client_config))
    if not reason:
        return status_dict
    logger.info("Torrent download %s is dead: %s", item["id"], reason)
    if not driver.cancel(lookup):
        logger.warning("Could not cancel dead torrent %s", lookup)
    return {**status_dict, "status": DownloadStatus.FAILED.value, "error_message": reason, "cancelled": True}


def _apply_stall_checks(
    db: Database, driver: Any, item: dict[str, Any], status_dict: dict[str, Any], lookup: str,
    client_config: dict[str, Any], is_slskd: bool,
) -> dict[str, Any]:
    """Soulseek stall detection or dead-torrent detection, whichever the client type calls for."""
    if is_slskd:
        return _check_slskd_status(db, driver, item, status_dict, lookup, client_config)
    if is_torrent_driver_type(client_config.get("driver_type")):
        return _fail_if_torrent_dead(driver, item, status_dict, lookup, client_config)
    return status_dict


def _research_after_failure(db: Database, item: dict[str, Any], reason: str) -> None:
    """Queue an immediate wanted-search for the track (or album's tracks) of a failed download."""
    from trackseerr.backlog_worker import backlog_worker  # lazy: backlog_worker imports this module's neighbours

    try:
        if item.get("track_id"):
            track_ids = [str(item["track_id"])]
        elif item.get("album_id"):
            track_ids = [str(t["id"]) for t in db.list_library_tracks(album_id=item["album_id"], limit=1000)]
        else:
            return
        targets = db.list_wanted_search_targets(track_ids=track_ids) if track_ids else []
        if targets:
            res = backlog_worker.queue_wanted_search(db, targets)
            logger.info("Re-searching %d track(s) after failed download %s (%s): %s", len(track_ids), item.get("id"), reason, res)
    except sqlite3.Error as exc:
        logger.warning("Could not queue re-search after failed download %s: %s", item.get("id"), exc)


def _research_if_failed(db: Database, item: dict[str, Any], err_msg: str) -> None:
    """Re-search after any failed download, whatever the driver (kept out of ``_process_item``)."""
    _research_after_failure(db, item, err_msg)


def _clear_failed_slskd_transfer(driver: Any, target_lookup: str, status_dict: dict[str, Any]) -> None:
    """Cancels a failed slskd download's live transfers, or removes its already-cancelled entries from slskd."""
    if status_dict.get("cancelled"):
        if not driver.cleanup_completed(target_lookup):
            logger.warning("Could not remove cancelled slskd transfers for failed download %s", target_lookup)
    elif not driver.cancel(target_lookup):
        logger.warning("Could not cancel remaining slskd transfers for failed download %s", target_lookup)


#: Minimum gap between empty-folder sweeps of slskd's download/incomplete folders.
SLSKD_SWEEP_INTERVAL_SECONDS = 3600
#: A folder must have been untouched this long before the sweep may remove it (slskd may just have created it).
SLSKD_SWEEP_MIN_AGE_SECONDS = 3600


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
        self._last_slskd_sweep: Optional[float] = None  # time.monotonic() of the last sweep; None = never ran
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

    def _find_audio_files(self, candidate_path: Optional[str | Path], search_term: str) -> list[Path]:  # noqa: C901
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

        ctx = _PollContext(
            media_settings=media_settings,
            write_tags=write_tags,
            embed_art=embed_art,
            save_cover=save_cover,
            plex_client=plex_client,
            lidarr_mode=lidarr_mode,
        )

        if not lidarr_mode:
            self._resolve_parked_duplicates(db, ctx)

        for item in active_items:
            self._process_item(db, ctx, item, stats)

        self._sweep_slskd_folders(db, media_settings)
        return stats

    def _sweep_slskd_folders(self, db: Database, media_settings: dict[str, Any]) -> None:
        """Hourly: removes old empty folders slskd leaves in its incomplete and completed-downloads folders."""
        now = time.monotonic()
        last = self._last_slskd_sweep
        if last is not None and now - last < SLSKD_SWEEP_INTERVAL_SECONDS:
            return
        self._last_slskd_sweep = now
        try:
            clients = db.list_download_clients(enabled_only=True)
        except sqlite3.Error as exc:
            logger.warning("Could not list download clients for slskd folder sweep: %s", safe_exc(exc))
            return
        for cfg in clients:
            if str(cfg.get("driver_type") or "").lower() != "slskd":
                continue
            try:
                self._sweep_slskd_client(cfg, media_settings)
            except (sqlite3.Error, httpx.HTTPError, OSError, ValueError, TypeError, AttributeError) as exc:
                logger.warning("slskd folder sweep failed for client %s: %s", cfg.get("name") or cfg.get("id"), safe_exc(exc))

    @staticmethod
    def _sweep_slskd_client(cfg: dict[str, Any], media_settings: dict[str, Any]) -> None:
        driver = get_acquisition_driver(cfg)
        local_roots, _error = fetch_client_roots(cfg)
        incomplete = driver.get_incomplete_root()
        if isinstance(incomplete, str) and incomplete:
            translated = translate_remote_path(incomplete, client_mappings(cfg))
            if translated and translated not in local_roots:
                local_roots.append(translated)
        if not local_roots:
            return
        guard = build_allowed_roots(media_settings, [], extra_roots=local_roots)
        for raw in local_roots:
            root = Path(raw)
            if not root.is_dir():
                continue
            resolved = root.resolve()
            overlaps_config = any(c == resolved or c in resolved.parents or resolved in c.parents for c in guard.config_dirs)
            if overlaps_config or not guard.check(resolved)[0]:
                logger.debug("Skipping slskd folder sweep of %s: overlaps the library or config directory", resolved)
                continue
            sweep_empty_dirs(resolved, min_age_seconds=SLSKD_SWEEP_MIN_AGE_SECONDS)

    def _classify_held_path(
        self,
        db: Database,
        item: dict[str, Any],
        path_str: str,
    ) -> str:
        """Classify a held path as 'duplicate', 'gone_resolved', 'gone_missing', or 'still_held'."""
        path = Path(path_str)
        if not path.exists():
            track_id = item.get("track_id")
            if track_id and db.get_library_file_for_track(str(track_id)) is not None:
                return "gone_resolved"
            artist_id = _download_artist_id(db, item, None)
            w_title, w_dur = wanted_song(db, item)
            if w_title:
                owned = find_owned_for_item(
                    db,
                    item,
                    w_title,
                    w_dur,
                    exclude_track_id=track_id,
                    artist_id=artist_id,
                )
                if owned and owned.get("cutoff_met"):
                    return "gone_resolved"
            return "gone_missing"

        try:
            metadata = inspect_audio_file(path)
        except Exception as e:
            logger.warning("Inspect failed for held file %s: %s", path, e)
            return "still_held"

        artist_id = _download_artist_id(db, item, None)
        if not artist_id and metadata.get("artist"):
            art = db.get_library_artist_by_name(str(metadata["artist"]))
            if art and art.get("id"):
                artist_id = str(art["id"])
        title = metadata.get("title") or ""
        duration = metadata.get("duration") or metadata.get("duration_seconds")
        owned = find_owned_for_item(
            db,
            item,
            title,
            duration,
            tag_artist=metadata.get("artist"),
            artist_id=artist_id,
        )
        if owned and owned.get("cutoff_met"):
            return "duplicate"
        if find_fileless_equivalent(
            db,
            candidate_artist_ids(db, item, metadata.get("artist")),
            title,
            duration,
        ):
            return "retry_import"
        return "still_held"

    @staticmethod
    def _find_duplicate_owned_title(
        db: Database, item: dict[str, Any], duplicate_paths: list[str]
    ) -> str:
        for p in duplicate_paths:
            try:
                path_obj = Path(p)
                if path_obj.exists():
                    metadata = inspect_audio_file(path_obj)
                    artist_id = _download_artist_id(db, item, None)
                    if not artist_id and metadata.get("artist"):
                        art = db.get_library_artist_by_name(str(metadata["artist"]))
                        if art and art.get("id"):
                            artist_id = str(art["id"])
                    title = metadata.get("title") or ""
                    duration = metadata.get("duration") or metadata.get("duration_seconds")
                    owned = find_owned_for_item(
                        db,
                        item,
                        title,
                        duration,
                        tag_artist=metadata.get("artist"),
                        artist_id=artist_id,
                    )
                    if owned and owned.get("title"):
                        return str(owned["title"])
            except (OSError, ValueError, TypeError) as e:
                logger.debug("Failed inspecting duplicate file %s: %s", p, e)
        return "another track"

    @staticmethod
    def _unmonitor_satisfied_track(db: Database, item: dict[str, Any], track_id: str) -> None:
        try:
            db.set_track_monitored(str(track_id), False)
            artist_id = _download_artist_id(db, item, None)
            trk = db.get_library_track(str(track_id))
            trk_title = trk.get("title") if trk else ""
            trk_dur = trk.get("duration_seconds") if trk else None
            owned = (
                find_owned_for_item(
                    db,
                    item,
                    trk_title,
                    trk_dur,
                    exclude_track_id=track_id,
                    artist_id=artist_id,
                )
                if trk_title
                else None
            )
            owned_title = owned.get("title") if owned else "equivalent track"
            logger.info("Unmonitored %s: already owned as %s", track_id, owned_title)
        except sqlite3.Error as exc:
            logger.warning("Failed to unmonitor duplicate track %s: %s", track_id, exc)


    def _clear_parked_download(
        self,
        db: Database,
        item: dict[str, Any],
        duplicate_paths: list[str],
        driver_type: str,
        import_mode: str,
        satisfied: bool = True,
    ) -> None:
        """Clears a parked download whose held files have all been resolved."""
        owned_title = "another track"
        if not satisfied:
            owned_title = self._find_duplicate_owned_title(db, item, duplicate_paths)

        if driver_type == "slskd" or import_mode == "move":
            for p in duplicate_paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("Failed to delete duplicate file %s: %s", p, e)

        client_id = item.get("client_id")
        client_config = db.get_download_client(client_id) if client_id else None
        if (driver_type == "slskd" or import_mode == "move") and client_config:
            self._prune_client_dirs(db, db.get_media_management_settings(), client_config, duplicate_paths)
        target_lookup = item.get("download_hash") or item["id"]
        if client_config:
            try:
                driver = get_acquisition_driver(client_config)
                driver.cleanup_completed(target_lookup)
            except Exception as e:
                logger.warning("Driver cleanup_completed failed for %s: %s", item["id"], e)

        db.set_download_unmatched_files(item["id"], [])
        msg = (
            "Already in library — duplicate discarded"
            if satisfied
            else "Discarded a file that is a different song you already own"
        )
        db.update_download_status(
            item["id"],
            status=DownloadStatus.IMPORTED.value,
            error_message=msg,
        )
        if satisfied and item.get("request_id"):
            db.update_request_status(item["request_id"], RequestStatus.AVAILABLE.value)

        if not satisfied:
            try:
                db.add_to_blocklist(
                    source_title=item["title"],
                    artist=item.get("artist"),
                    release_guid=(item.get("download_hash") or item["id"]),
                    info_hash=item.get("download_hash"),
                    reason=f"Already own this file's song as {owned_title}",
                    download_id=item["id"],
                    track_id=item.get("track_id"),
                )
            except sqlite3.Error as exc:
                logger.warning("Failed to blocklist duplicate download %s: %s", item["id"], exc)

        track_id = item.get("track_id")
        if satisfied and track_id and db.get_library_file_for_track(str(track_id)) is None:
            self._unmonitor_satisfied_track(db, item, str(track_id))

    def _is_download_satisfied(self, db: Database, item: dict[str, Any]) -> bool:
        """Checks whether the wanted song of a download is already satisfied in the library."""
        track_id = item.get("track_id")
        if track_id and db.get_library_file_for_track(str(track_id)) is not None:
            return True
        title, dur = wanted_song(db, item)
        if not title:
            return False
        artist_id = _download_artist_id(db, item, None)
        owned = find_owned_for_item(
            db,
            item,
            title,
            dur,
            exclude_track_id=track_id,
            artist_id=artist_id,
        )
        return bool(owned and owned.get("cutoff_met"))

    @staticmethod
    def _prune_client_dirs(
        db: Database, media_settings: dict[str, Any], client_config: dict[str, Any], paths: list[str]
    ) -> None:
        """Removes folders emptied by deleting ``paths``, bounded to the client's allowed download roots."""
        if not paths:
            return
        try:
            roots = allowed_roots_for_client(db, media_settings, client_config)
            grouped: dict[Path, list[str]] = {}
            for p in paths:
                root = roots.matching_root(Path(p).resolve())
                if root is not None:
                    grouped.setdefault(root, []).append(p)
        except (sqlite3.Error, OSError, RuntimeError, ValueError) as exc:
            logger.debug("Skipping empty-folder pruning: %s", safe_exc(exc))
            return
        for root, group in grouped.items():
            prune_empty_parents(group, root)

    def _unlink_duplicate_files(
        self,
        paths: list[str],
        driver_type: str,
        import_mode: str,
        db: Optional[Database] = None,
        media_settings: Optional[dict[str, Any]] = None,
        client_config: Optional[dict[str, Any]] = None,
    ) -> None:
        """Removes local files for identified duplicates when under slskd or move import mode."""
        if driver_type == "slskd" or import_mode == "move":
            for p in paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("Failed to delete duplicate file %s: %s", p, e)
            if db is not None and client_config and media_settings is not None:
                self._prune_client_dirs(db, media_settings, client_config, paths)

    def _resolve_parked_item(
        self,
        db: Database,
        ctx: _PollContext,
        item: dict[str, Any],
        requeued_paths: Optional[set[str]] = None,
    ) -> None:
        """Processes a single parked download item during the duplicate sweep."""
        held_paths = item.get("unmatched_files") or []
        if not held_paths:
            return

        client_id = item.get("client_id")
        client_config = db.get_download_client(client_id) if client_id else None
        driver_type = str(client_config.get("driver_type") if client_config else item.get("client_driver_type") or "").lower()
        import_mode = effective_import_mode(driver_type, ctx.media_settings)

        duplicate_paths: list[str] = []
        remaining_paths: list[str] = []
        retry_import_paths: list[str] = []
        gone_missing_count = 0

        for path_str in held_paths:
            cls = self._classify_held_path(db, item, path_str)
            if cls == "duplicate":
                duplicate_paths.append(path_str)
            elif cls == "gone_resolved":
                pass
            elif cls == "gone_missing":
                gone_missing_count += 1
            elif cls == "retry_import":
                retry_import_paths.append(path_str)
            else:
                remaining_paths.append(path_str)

        can_requeue = (
            retry_import_paths
            and not duplicate_paths
            and gone_missing_count == 0
            and (requeued_paths is None or not any(p in requeued_paths for p in retry_import_paths))
            and not str(item.get("error_message") or "").startswith("Retried import")
        )
        if can_requeue:
            if requeued_paths is not None:
                requeued_paths.update(retry_import_paths)
            logger.info("Requeuing download %s for import retry onto matching track", item["id"])
            db.update_download_status(
                item["id"],
                status=DownloadStatus.COMPLETED.value,
                error_message="Retried import onto a matching track",
            )
            return

        remaining_paths.extend(retry_import_paths)

        if not remaining_paths:
            satisfied = self._is_download_satisfied(db, item)
            if gone_missing_count == len(held_paths) and not satisfied:
                db.set_download_unmatched_files(item["id"], [])
                db.update_download_status(
                    item["id"],
                    status=DownloadStatus.FAILED.value,
                    error_message="The downloaded file is no longer on disk",
                )
            else:
                self._clear_parked_download(db, item, duplicate_paths, driver_type, import_mode, satisfied=satisfied)
        elif len(remaining_paths) < len(held_paths):
            self._unlink_duplicate_files(
                duplicate_paths, driver_type, import_mode, db, ctx.media_settings, client_config
            )
            db.set_download_unmatched_files(item["id"], remaining_paths)
            msg = f"{len(remaining_paths)} file(s) couldn't be matched — manual import required"
            db.update_download_status(item["id"], status=DownloadStatus.WARNING.value, error_message=msg)

    def _resolve_parked_duplicates(self, db: Database, ctx: _PollContext) -> None:
        """Sweeps parked WARNING downloads to auto-clear files already owned in library."""
        warning_items = db.list_active_downloads(statuses=[DownloadStatus.WARNING.value])
        requeued_paths: set[str] = set()
        for item in warning_items:
            self._resolve_parked_item(db, ctx, item, requeued_paths=requeued_paths)

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

        is_slskd = str(client_config.get("driver_type")).lower() == "slskd"
        status_dict = _apply_stall_checks(db, driver, item, status_dict, target_lookup, client_config, is_slskd)

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
            if is_slskd:
                _clear_failed_slskd_transfer(driver, target_lookup, status_dict)
            try:
                db.add_to_blocklist(
                    source_title=item.get("title", ""),
                    artist=item.get("artist"),
                    release_guid=(item.get("download_hash") or item.get("id")) if is_slskd else item.get("id"),
                    info_hash=item.get("download_hash"),
                    reason=err_msg,
                    download_id=download_id,
                )
            except Exception as bl_err:
                logger.warning("Failed to add failed download to blocklist: %s", bl_err)
            _research_if_failed(db, item, err_msg)
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

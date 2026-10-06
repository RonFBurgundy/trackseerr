"""Download Monitor & Automated Library Organizer Worker.

Periodically inspects active downloads across configured download clients,
detects completed transfers, inspects audio tags, calculates destination
paths via the token template engine, performs atomic file moves with
collision resolution into /music, and triggers Plex library update pings.
"""

import difflib
import errno
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from plex_playlist_sync import delay_gate
from plex_playlist_sync.acquisition_coordinator import _to_quality_profile
from plex_playlist_sync.clients.acquisition import get_acquisition_driver, is_torrent_driver_type
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.clients.plex import PlexClient
from plex_playlist_sync.media_servers import as_media_server
from plex_playlist_sync.job_tracker import job_tracker, summarize_result
from plex_playlist_sync.download_roots import AllowedRoots, allowed_roots_for_all_clients, allowed_roots_for_client
from plex_playlist_sync.import_security import (
    clear_exec_bits,
    quarantine_files,
    verify_files,
)
from plex_playlist_sync.recycle_bin import (
    is_system_dirname,
    is_system_filename,
    EXCLUDED_DIRNAMES,
    DisposeResult,
    dispose_for_settings,
    effective_quarantine_path,
    effective_recycle_path,
    library_excluded_paths,
    log_recycled,
    recycle_in_place_target,
    recycle_replaced_files,
    restore_recycled,
)
from plex_playlist_sync.import_quality_check import CHECK_OFF, check_files, normalize_check_mode
from plex_playlist_sync.item_history import TRIGGER_SEED_CLEANUP, download_trigger_kwargs, emit
from plex_playlist_sync.library_health import record_weak_match
from plex_playlist_sync.library_monitoring import NATIVE_MONITOR_OPTIONS
from plex_playlist_sync.library_manager import ModeChanged, run_guarded
from plex_playlist_sync.library import (
    AUDIO_EXTENSIONS,
    embed_album_artwork,
    ArchiveLimitError,
    extract_archive,
    fingerprint_audio_file,
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
from plex_playlist_sync.redaction import redact_text, safe_exc
from plex_playlist_sync.quality import evaluate_release, parse_release_title
from plex_playlist_sync.security import is_safe_service_url
from plex_playlist_sync.storage import Database, clean_library_name

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


IMPORT_MODES = ("move", "hardlink", "copy")


def preserves_source(mode: str | None) -> bool:
    """True when an import mode leaves the source file in place, so a torrent can keep seeding."""
    return mode in ("hardlink", "copy")


def _copy_atomic(src: Path, dst: Path) -> Path:
    """Copies src to a hidden temp file beside dst, then os.replace: readers never see a partial file."""
    tmp_dst = dst.parent / f".tmp_{dst.name}_{os.getpid()}_{time.time_ns()}"
    try:
        shutil.copy2(str(src), str(tmp_dst))
        os.replace(str(tmp_dst), str(dst))
    except BaseException:
        try:
            tmp_dst.unlink(missing_ok=True)
        except OSError as cleanup_err:
            logger.warning("Could not remove temp file '%s': %s", tmp_dst, cleanup_err)
        raise
    return dst


def safe_atomic_move(source_file: Path | str, target_file: Path | str) -> Path:
    """Atomically places source_file at target_file, safely handling cross-device mounts.

    If source and destination reside on the same filesystem, os.replace is used directly.
    On EXDEV (different filesystems), writes to a temporary hidden file in the destination
    folder first, then atomically replaces to ensure Plex never indexes incomplete files.
    Any other OSError is logged and re-raised.
    """
    src = Path(source_file).resolve()
    dst = Path(target_file).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    try:
        os.replace(str(src), str(dst))
        return dst
    except OSError as e:
        if e.errno != errno.EXDEV:
            logger.error("Move '%s' -> '%s' failed: %s", src, dst, e)
            raise
    _copy_atomic(src, dst)
    try:
        src.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Copied '%s' -> '%s' across devices but could not remove the source: %s", src, dst, e)
    return dst


def place_audio_file(
    source_file: Path | str, target_file: Path | str, mode: str = "move"
) -> Path:
    """Places source_file at target_file according to mode.

    - "hardlink": os.link(src, dst); on OSError (e.g. EXDEV) falls back to an atomic copy.
      The source is always left untouched.
    - "copy": atomic copy (hidden temp + os.replace); the source is left untouched.
    - "move": safe_atomic_move (atomic replace, source removed).
    Any other mode raises ValueError.
    """
    if mode not in IMPORT_MODES:
        raise ValueError(f"Unknown import mode {mode!r}; expected one of {', '.join(IMPORT_MODES)}")
    src = Path(source_file).resolve()
    dst = Path(target_file).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    if mode == "hardlink":
        try:
            os.link(str(src), str(dst))
            logger.info("Successfully hardlinked '%s' -> '%s'", src, dst)
            clear_exec_bits(dst)
            return dst
        except OSError as e:
            logger.warning("os.link failed (%s); falling back to atomic copy for '%s' -> '%s'", e, src, dst)
            _copy_atomic(src, dst)
            clear_exec_bits(dst)
            return dst
    if mode == "copy":
        _copy_atomic(src, dst)
        clear_exec_bits(dst)
        return dst
    placed = safe_atomic_move(source_file, target_file)
    clear_exec_bits(placed)
    return placed


def ensure_private_copy(path: Path | str) -> bool:
    """Makes ``path`` safe to rewrite in place: True when no other link shares its inode afterwards.

    A hardlink-imported library file shares its inode with the torrent's seeding file, so tagging it would corrupt the
    torrent's data. When ``st_nlink > 1`` the file is copied to a hidden temp beside it and ``os.replace``d over it
    (atomic; the other link keeps the original inode and bytes). Returns False when the copy failed: the caller must
    then skip every tag/artwork write for that file.
    """
    p = Path(path)
    try:
        if p.stat().st_nlink <= 1:
            return True
    except OSError as e:
        logger.warning("Cannot stat '%s' before tagging; skipping tag writes: %s", p, e)
        return False
    try:
        _copy_atomic(p, p)
        clear_exec_bits(p)
    except OSError as e:
        logger.warning("Could not break hardlink for tagging '%s'; skipping tag writes: %s", p, e)
        return False
    logger.info("Broke hardlink for tagging: %s", p)
    return True


TORRENT_HARDLINK_TAG_MODES = ("copy_and_tag", "keep_hardlink")


def effective_import_mode(client_type: str | None, media_settings: dict[str, Any]) -> str:
    """Import mode for a download: the configured mode for torrent clients, always "move" for everything else.

    Usenet and Soulseek files do not seed from their source, so there is nothing to preserve.
    """
    if not is_torrent_driver_type(client_type):
        return "move"
    mode = str(media_settings.get("import_mode") or "move")
    return mode if mode in IMPORT_MODES else "move"


def prepare_file_for_tagging(path: Path | str, media_settings: dict[str, Any]) -> bool:
    """True when ``path`` may be rewritten with tags/artwork; False when tag and art writes must be skipped.

    A hardlinked file (shared inode with a seeding torrent) is either kept untouched (``keep_hardlink``) or split into
    a private copy first (``copy_and_tag``, the default, via ``ensure_private_copy``).
    """
    p = Path(path)
    if str(media_settings.get("torrent_hardlink_tags") or "copy_and_tag") == "keep_hardlink":
        try:
            shared = p.stat().st_nlink > 1
        except OSError as e:
            logger.warning("Cannot stat '%s' before tagging; skipping tag writes: %s", p, e)
            return False
        if shared:
            logger.info("Kept hardlink; skipped tag writing for %s", p)
            return False
        return True
    return ensure_private_copy(p)


SEED_ACTIONS = ("keep", "remove", "remove_and_delete")


def seed_action(media_settings: dict[str, Any]) -> str:
    """The configured "When seeding is done" action. A missing or unknown value reads as ``keep`` (never touch the client)."""
    action = str(media_settings.get("seed_complete_action") or "keep")
    return action if action in SEED_ACTIONS else "keep"


def _under_path(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _client_path_mappings(db: Any, client_id: Any) -> list[dict[str, str]]:
    """Remote-to-local path mappings configured on a download client (empty when none or unreadable)."""
    if not client_id:
        return []
    try:
        cfg = db.get_download_client(str(client_id))
    except sqlite3.Error as exc:
        logger.warning("Could not read download client %s for path mappings: %s", client_id, safe_exc(exc))
        return []
    extra = (cfg or {}).get("extra_settings_json")
    if not extra:
        return []
    try:
        data = json.loads(extra) if isinstance(extra, str) else extra
    except (json.JSONDecodeError, TypeError):
        return []
    maps = data.get("remote_path_mappings", []) if isinstance(data, dict) else []
    return [m for m in maps if isinstance(m, dict)]


def _inodes_under(root: Path) -> set[tuple[int, int]]:
    """(device, inode) of every regular file at or under ``root``."""
    found: set[tuple[int, int]] = set()
    if root.is_file():
        st = root.stat()
        return {(st.st_dev, st.st_ino)}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            found.add((st.st_dev, st.st_ino))
    return found


def deletion_safe(
    download: dict[str, Any],
    effective_mode: str | None,
    db: Any,
    status_dict: Optional[dict[str, Any]] = None,
) -> tuple[bool, str]:
    """Strict gate for ``remove_and_delete``: may the client delete the torrent's files? Returns (ok, reason).

    Every condition must hold, otherwise the caller falls back to a plain removal:
    - the effective import mode, and the mode recorded when the files were placed, are ``hardlink`` or ``copy``;
    - the download has a placed-files record, and every file in it exists and is not (or inside) the torrent's content;
      for hardlinks no placed file shares an inode with any file under the torrent content;
    - no unmatched files are still held for manual import;
    - the torrent's content path is known and is not at or inside the music root (all paths resolved).
    """
    if effective_mode not in ("hardlink", "copy"):
        return False, f"import mode is {effective_mode or 'unknown'}; files are only deleted for hardlink or copy"
    placed_mode = download.get("placed_mode")
    if placed_mode not in ("hardlink", "copy"):
        return False, f"import mode when the files were placed is {placed_mode or 'unknown'}"
    if download.get("unmatched_files"):
        return False, "unmatched files are still held for manual import"
    placed = [str(p) for p in (download.get("placed_files") or []) if p]
    if not placed:
        return False, "no record of the library files placed for this download"
    raw_content = str((status_dict or {}).get("content_path") or "").strip()
    if not raw_content:
        return False, "the torrent's content path is unknown"
    if db is None:
        return False, "no database to check the music root"
    try:
        music_root_raw = db.get_media_management_settings().get("root_folder_path") or ""
    except sqlite3.Error as exc:
        return False, f"could not read the music root ({safe_exc(exc)})"
    if not str(music_root_raw).strip():
        return False, "the music root is not configured"
    mapped = translate_remote_path(raw_content, _client_path_mappings(db, download.get("client_id")))
    if not mapped:
        return False, "the torrent's content path was rejected"
    content = Path(mapped).resolve()
    music_root = Path(str(music_root_raw)).resolve()
    if _under_path(content, music_root) or _under_path(music_root, content):
        return False, "the torrent's content path overlaps the music root"
    check_inodes = "hardlink" in (effective_mode, placed_mode)
    torrent_inodes: set[tuple[int, int]] = set()
    if check_inodes:
        if not content.exists():
            return False, "the torrent's content is not visible to TrackSeerr, so shared files cannot be ruled out"
        try:
            torrent_inodes = _inodes_under(content)
        except OSError as exc:
            return False, f"could not inspect the torrent's content ({safe_exc(exc)})"
    for raw in placed:
        lib = Path(raw)
        try:
            resolved = lib.resolve()
            st = resolved.stat()
        except OSError:
            return False, f"library file is missing: {raw}"
        if not resolved.is_file():
            return False, f"library file is not a regular file: {raw}"
        if _under_path(resolved, content):
            return False, f"library file is the torrent's own file: {raw}"
        if check_inodes and (st.st_dev, st.st_ino) in torrent_inodes:
            return False, f"library file shares an inode with the torrent: {raw}"
    return True, "ok"


@dataclass
class SeedOutcome:
    """Result of one seed-goal evaluation. ``status`` is the DownloadStatus value to record."""

    status: str
    removed: bool = False
    deleted_files: bool = False
    error: Optional[str] = None
    note: str = ""


def evaluate_seed_cleanup(
    driver: Any,
    target_lookup: str,
    media_settings: dict[str, Any],
    import_mode: str | None,
    status_dict: Optional[dict[str, Any]],
    download: Optional[dict[str, Any]] = None,
    db: Any = None,
) -> SeedOutcome:
    """Seed-goal evaluation and the configured action; the one implementation behind the worker, manual import and sweep.

    The goal is the download's snapshotted target (indexer rule or global), else the global limits. An unmet indexer
    rule always blocks. ``remove_and_delete`` deletes files only when ``deletion_safe`` passes, otherwise it removes
    the torrent alone and says why in ``note``. A failed client call sets ``error`` and keeps the COMPLETED status.
    """
    action = seed_action(media_settings)
    if action == "keep":
        return SeedOutcome(DownloadStatus.IMPORTED.value, note="keep")
    seed_ratio_limit = media_settings.get("seed_ratio_limit")
    seed_time_limit_minutes = media_settings.get("seed_time_limit_minutes")
    rule_source = (download or {}).get("seed_rule_source")
    if rule_source in ("indexer", "global"):
        seed_ratio_limit = download.get("seed_ratio_target")  # type: ignore[union-attr]
        seed_time_limit_minutes = download.get("seed_time_target_minutes")  # type: ignore[union-attr]
    keep = SeedOutcome(DownloadStatus.COMPLETED.value, note="seed goal not met")
    if rule_source == "indexer":
        ratio_t = float(seed_ratio_limit or 0.0)
        time_t = int(seed_time_limit_minutes or 0)
        if ratio_t > 0 or time_t > 0:  # 0/None never counts as met on its own, nor as a requirement
            if status_dict is None:
                logger.info("Keeping transfer %s: seeding status unavailable (indexer seed rule)", target_lookup)
                return keep
            cur_ratio = float(status_dict.get("ratio") or 0.0)
            cur_seeding_sec = int(status_dict.get("seeding_time_seconds") or 0)
            ratio_met = ratio_t > 0 and cur_ratio >= ratio_t
            time_met = time_t > 0 and cur_seeding_sec >= time_t * 60
            if not (ratio_met or time_met):
                return keep
    elif preserves_source(import_mode) and (seed_ratio_limit is not None or seed_time_limit_minutes is not None):
        if status_dict is None:
            logger.info("Keeping transfer %s: seeding status unavailable", target_lookup)
            return keep
        cur_ratio = float(status_dict.get("ratio") or 0.0)
        cur_seeding_sec = int(status_dict.get("seeding_time_seconds") or 0)
        ratio_met = seed_ratio_limit is not None and cur_ratio >= float(seed_ratio_limit)
        time_met = seed_time_limit_minutes is not None and cur_seeding_sec >= int(seed_time_limit_minutes) * 60
        if not (ratio_met or time_met):
            return keep

    delete_files = False
    note = "removed"
    if action == "remove_and_delete":
        ok, reason = deletion_safe(download or {}, import_mode, db, status_dict)
        if ok:
            delete_files = True
            note = "removed with files"
        else:
            note = f"removed, files kept: {reason}"
            logger.info("Not deleting files for %s: %s", target_lookup, reason)
    try:
        done = driver.cleanup_completed(target_lookup, delete_files=delete_files)
    except Exception as ex:  # noqa: BLE001 - driver errors span HTTP, auth and parsing; the cause is logged and surfaced
        logger.warning("Error during cleanup_completed for %s: %s", target_lookup, redact_text(str(ex)))
        return SeedOutcome(DownloadStatus.COMPLETED.value, error=redact_text(str(ex))[:300] or type(ex).__name__)
    if done is False and getattr(driver, "is_torrent", False):
        logger.warning("Download client did not remove %s", target_lookup)
        return SeedOutcome(DownloadStatus.COMPLETED.value, error="The download client refused or failed the removal")
    if delete_files and db is not None and (download or {}).get("id"):
        db.record_download_item_event(
            "file_deleted", str(download["id"]),  # type: ignore[index]
            message="Seeding copy deleted after the seed goal was met",
            details={"release": (download or {}).get("title"), "note": note, "scope": "download client copy"},
            trigger=TRIGGER_SEED_CLEANUP, trigger_ref="", trigger_label="Seed cleanup",
        )
    return SeedOutcome(DownloadStatus.IMPORTED.value, removed=True, deleted_files=delete_files, note=note)


def settle_transfer_after_import(
    driver: Any,
    target_lookup: str,
    media_settings: dict[str, Any],
    import_mode: str | None,
    status_dict: Optional[dict[str, Any]],
    download: Optional[dict[str, Any]] = None,
    db: Any = None,
) -> str:
    """Shared post-import download-client governance (worker and manual import).

    Returns the DownloadStatus value to record: COMPLETED when the transfer is kept (seeding
    continues; the worker's already-imported branch removes it once the goal is met), else IMPORTED.
    Driven by ``seed_complete_action`` (see ``evaluate_seed_cleanup``):
    - keep: no client call, IMPORTED.
    - remove: cleanup_completed(delete_files=False) once the seed goal is met.
    - remove_and_delete: as remove, but with delete_files=True only when ``deletion_safe`` passes (needs ``db``).
    A failed removal here is logged and recorded as IMPORTED: the seed-cleanup sweep finds the leftover torrent
    and retries it with attempt counting.
    """
    outcome = evaluate_seed_cleanup(driver, target_lookup, media_settings, import_mode, status_dict, download, db)
    if outcome.error:
        return DownloadStatus.IMPORTED.value
    return outcome.status


def reconcile_audio_file_to_track(
    meta: dict[str, Any],
    candidate_tracks: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Reconciles an audio file's metadata against expected library tracks; returns only the track.

    Thin wrapper over reconcile_audio_file_to_track_scored (see it for the matching hierarchy).
    """
    return reconcile_audio_file_to_track_scored(meta, candidate_tracks)[0]


MATCH_STRONG = "strong"
MATCH_WEAK = "weak"
MATCH_NONE = "none"


def reconcile_audio_file_to_track_scored(
    meta: dict[str, Any],
    candidate_tracks: list[dict[str, Any]],
) -> tuple[Optional[dict[str, Any]], str]:
    """Reconciles an audio file's metadata against a list of expected library tracks.

    Matching hierarchy:
    1. Exact match on disc_number and track_number (if mutagen extracted valid track number).
    2. Clean title similarity match (clean_library_name(t["title"]) == clean_library_name(meta["title"]) or ratio >= 0.85).
    3. Duration tolerance match (within 5 seconds) if multiple candidates match title.

    Returns (track, strength). Strength is "strong" for a unique disc+track number match, a number match
    disambiguated to one by exact title, or a unique exact clean-title match; "weak" for every fallback pick
    (ambiguous picks, duration tie-breaks, fuzzy matches); "none" when nothing matched.
    """
    if not candidate_tracks:
        return None, MATCH_NONE

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
            return num_matches[0], MATCH_STRONG
        elif len(num_matches) > 1:
            clean_title = clean_library_name(meta.get("title") or "")
            title_matches = [
                t
                for t in num_matches
                if clean_library_name(t.get("title") or "") == clean_title
            ]
            if len(title_matches) == 1:
                return title_matches[0], MATCH_STRONG
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
                    ), MATCH_WEAK
            return num_matches[0], MATCH_WEAK

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
            return exact_title_matches[0], MATCH_STRONG
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
                    return dur_matches[0], MATCH_WEAK
                elif dur_matches:
                    return min(
                        dur_matches,
                        key=lambda t: abs(float(t["duration_seconds"]) - float(file_dur)),
                    ), MATCH_WEAK
            return exact_title_matches[0], MATCH_WEAK

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
                return top_matches[0], MATCH_WEAK

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
                    return dur_matches[0], MATCH_WEAK
                elif dur_matches:
                    return min(
                        dur_matches,
                        key=lambda t: abs(float(t["duration_seconds"]) - float(file_dur)),
                    ), MATCH_WEAK
            return top_matches[0], MATCH_WEAK

    return None, MATCH_NONE


FINGERPRINT_MIN_SCORE = 0.80


def _fingerprint_fallback_match(
    file_path: Path,
    media_settings: dict[str, Any],
    remaining_tracks: list[dict[str, Any]],
    tag_track: Optional[dict[str, Any]],
    strength: str,
) -> Optional[dict[str, Any]]:
    """Resolves a weak or missing tag match via AcoustID fingerprinting; returns the tag result when it cannot improve.

    Only runs when the tag match is not strong, fingerprint_on_weak_match is enabled and an AcoustID key is set.
    fingerprint_audio_file never raises, so a lookup failure leaves the tag result untouched.
    """
    if strength == MATCH_STRONG:
        logger.info("Import match for %s decided by tag-strong", file_path.name)
        return tag_track
    api_key = media_settings.get("acoustid_api_key")
    if not (media_settings.get("fingerprint_on_weak_match") and api_key):
        logger.info("Import match for %s decided by tag-weak-kept (fingerprint fallback disabled)", file_path.name)
        return tag_track

    fp = fingerprint_audio_file(file_path, api_key)
    if fp and float(fp.get("score") or 0.0) >= FINGERPRINT_MIN_SCORE:
        rec_id = fp.get("recording_id")
        if rec_id:
            rec_hits = [t for t in remaining_tracks if t.get("mb_recording_id") == rec_id]
            if rec_hits:
                logger.info("Import match for %s decided by fingerprint-recording (%s)", file_path.name, rec_id)
                return rec_hits[0]
        fp_title = clean_library_name(fp.get("title") or "")
        if fp_title:
            title_hits = [t for t in remaining_tracks if clean_library_name(t.get("title") or "") == fp_title]
            if len(title_hits) == 1:
                logger.info("Import match for %s decided by fingerprint-title (%s)", file_path.name, fp_title)
                return title_hits[0]
    logger.info("Import match for %s decided by tag-weak-kept (strength=%s)", file_path.name, strength)
    return tag_track


def resolve_download_expected_tracks(
    db: Database,
    item: dict[str, Any],
    req: Optional[dict[str, Any]],
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """The catalog album a native download targets and that album's tracks (the tracks the import expects).

    Resolution order: the download's album_id, its track's album, then artist name + album title.
    Returns (None, []) when the download cannot be tied to a catalog album.
    """
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
    return target_album, expected_tracks


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


def _path_under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


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

        for item in active_items:
            self.allowed_roots = None
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
                _notify_failed(err_msg)
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
                continue

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
                continue

            if is_ready:
                stats["completed"] += 1
                db.update_download_status(download_id, status=DownloadStatus.IMPORTING.value)

                # Special case: Lidarr performs native file organization
                driver_type = str(client_config.get("driver_type", "")).lower()
                import_mode = effective_import_mode(driver_type, media_settings)
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
                            as_media_server(plex_client).refresh_library()
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
                client_label = str(client_config.get("name") or client_id or "download client")
                self.allowed_roots = allowed_roots_for_client(db, media_settings, client_config, driver=driver)
                if not self.allowed_roots.roots:
                    reason = "; ".join(self.allowed_roots.errors) or f"Could not read download folder from {client_label}"
                    err_msg = f"{reason}; check client connection"
                    logger.warning("Download %s cannot be imported yet: %s", download_id, err_msg)
                    db.update_download_status(download_id, status=DownloadStatus.COMPLETED.value, error_message=err_msg)
                    self.allowed_roots = None
                    continue
                if candidate_src:
                    ok, reject_reason = self.allowed_roots.check(candidate_src)
                    if not ok:
                        logger.warning("Rejecting source path %s from %s: %s", candidate_src, client_label, reject_reason)
                        candidate_src = None

                search_term = item.get("title") or item.get("artist") or ""
                self._archive_errors = []
                audio_files = self._find_audio_files(candidate_src, search_term)

                if self._archive_errors and not audio_files:
                    err_msg = "Archive rejected: " + "; ".join(self._archive_errors)
                    try:
                        db.record_event(
                            "import_security",
                            f"Archive limits exceeded for '{item.get('title', '')}': {err_msg}",
                            source="AcquisitionWorker",
                            severity="error",
                            details={"download_id": download_id, "archives": list(self._archive_errors)},
                        )
                    except sqlite3.Error as ev_err:
                        logger.warning("Failed to record import_security event: %s", ev_err)
                    db.update_download_status(download_id, status=DownloadStatus.FAILED.value, error_message=err_msg)
                    _notify_failed(err_msg)
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
                        logger.warning("Failed to add archive-rejected download to blocklist: %s", bl_err)
                    stats["failed"] += 1
                    continue

                if not audio_files:
                    logger.warning(
                        "Download %s marked completed but no audio files found at %s or download roots %s",
                        download_id,
                        candidate_src,
                        ", ".join(str(r) for r in self._effective_roots().roots),
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
                            download_id=download_id,
                        )
                    except Exception as bl_err:
                        logger.warning("Failed to add missing-audio download to blocklist: %s", bl_err)
                    stats["failed"] += 1
                    continue

                # Security gate (always on, any import_bitrate_check mode): magic bytes + header parse. One bad file
                # rejects the whole release; offenders are quarantined, never imported.
                probes: dict[str, Any] = {}
                security = verify_files(audio_files, probes)
                if security.failed:
                    err_msg = f"Security check failed: {security.reason()}"
                    # Torrent sources keep seeding from their download folder, so they are copied, never moved. Usenet,
                    # Soulseek and staging sources have nothing seeding and are moved out of the download folder.
                    q_root = effective_quarantine_path(media_settings)
                    keep_sources = is_torrent_driver_type(driver_type)
                    if q_root is None:
                        # No quarantine or library root is configured: never fall back to the process cwd.
                        logger.warning(
                            "No quarantine folder or library root configured; leaving rejected files of download "
                            "%s in place (release is still refused).", download_id,
                        )
                        moved = []
                    else:
                        moved = quarantine_files(
                            [p for p, _ in security.failures], q_root, str(download_id), copy=keep_sources
                        )
                    try:
                        db.record_event(
                            "import_security",
                            f"Security check failed for '{item.get('title', '')}': {security.reason()}",
                            source="AcquisitionWorker",
                            severity="error",
                            details={
                                "download_id": download_id,
                                "files": [{"file": p, "reason": r} for p, r in security.failures],
                                "quarantined_to": [str(m) for m in moved],
                                "sources_kept_for_seeding": keep_sources,
                            },
                        )
                    except sqlite3.Error as ev_err:
                        logger.warning("Failed to record import_security event: %s", ev_err)
                    db.record_download_item_event(
                        "quarantined", str(download_id), message=f"Import security: {security.reason()}",
                        details={
                            "release": item.get("title"),
                            "files": [{"file": p, "reason": r} for p, r in security.failures],
                            "quarantined_to": [str(m) for m in moved], "copied": keep_sources,
                        },
                    )
                    logger.error("Import security failure for download %s: %s", download_id, err_msg)
                    db.update_download_status(download_id, status=DownloadStatus.FAILED.value, error_message=err_msg)
                    _notify_failed(err_msg)
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
                        logger.warning("Failed to add security-rejected download to blocklist: %s", bl_err)
                    stats["failed"] += 1
                    continue

                # Per-track bitrate check (media management: import_bitrate_check = off | warn | reject).
                check_mode = normalize_check_mode(media_settings.get("import_bitrate_check"))
                if check_mode != CHECK_OFF:
                    try:
                        definitions = {str(d["quality"]): d for d in db.list_quality_definitions()}
                        check = check_files(audio_files, check_mode, definitions, probes=probes)
                    except Exception as chk_err:  # noqa: BLE001 - the check is advisory; it must never crash the worker loop
                        logger.warning(
                            "Import bitrate check failed for download %s: %s: %s",
                            download_id,
                            type(chk_err).__name__,
                            chk_err,
                        )
                        check = None
                    if check is not None and (check.out_of_range or check.skipped):
                        summary = check.reason()
                        try:
                            db.record_event(
                                "import_bitrate_check",
                                f"Bitrate check ({check_mode}) for '{item.get('title', '')}': {summary}",
                                source="AcquisitionWorker",
                                severity="error" if check.failed else "warning",
                                details={
                                    "download_id": download_id,
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
                        logger.warning("Import bitrate check (%s) for download %s: %s", check_mode, download_id, summary)
                    if check is not None and check.failed:
                        err_msg = f"Bitrate check failed: {check.reason()}"
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
                                download_id=download_id,
                            )
                        except Exception as bl_err:
                            logger.warning("Failed to add bitrate-rejected download to blocklist: %s", bl_err)
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
                target_album, expected_tracks = resolve_download_expected_tracks(db, item, req)

                remaining_expected_tracks = list(expected_tracks)
                placed_to_track: dict[str, dict[str, Any]] = {}
                # placed path -> (recycle result, old file row) for old files recycled right before an in-place replace
                recycled_in_place: dict[str, tuple[DisposeResult, dict[str, Any]]] = {}
                # Files with no catalog match when the release has expected tracks: left on disk for manual import.
                held_files: list[str] = []

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
                    file_weak_strength: Optional[str] = None
                    if remaining_expected_tracks:
                        matched_expected_track, match_strength = reconcile_audio_file_to_track_scored(
                            metadata, remaining_expected_tracks
                        )
                        tag_track = matched_expected_track
                        matched_expected_track = _fingerprint_fallback_match(
                            af, media_settings, remaining_expected_tracks, matched_expected_track, match_strength
                        )
                        if matched_expected_track is not None and matched_expected_track is tag_track and match_strength != MATCH_STRONG:
                            file_weak_strength = match_strength
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

                    if expected_tracks and matched_expected_track is None:
                        logger.warning(
                            "Holding unmatched file %s for download %s (manual import required)", af, download_id
                        )
                        held_files.append(str(af))
                        continue

                    # Disc 1 of a multi-disc release must use the multi-disc format too.
                    known_discs = [int(metadata.get("total_discs") or 1)]
                    known_discs += [int(t.get("disc_number") or 1) for t in expected_tracks]
                    metadata["total_discs"] = max(known_discs)

                    last_metadata = metadata

                    target_str = build_track_path(metadata, media_settings)
                    desired_path = Path(target_str).resolve()
                    pre_recycled = self._recycle_in_place_target(
                        db, media_settings, root_path, desired_path, matched_expected_track
                    )
                    final_target = desired_path if pre_recycled is not None else resolve_collision(target_str)
                    target_path = Path(final_target).resolve()
                    if not target_path.is_relative_to(root_path):
                        logger.error("Destination %s escapes music root %s", target_path, root_path)
                        if pre_recycled is not None:
                            restore_recycled(pre_recycled[0])
                        continue

                    try:
                        placed_path = place_audio_file(af, target_path, mode=import_mode)
                    except Exception:
                        if pre_recycled is not None:
                            restore_recycled(pre_recycled[0])  # the replacement never landed: put the old bytes back
                        raise
                    if pre_recycled is not None:
                        # The old file's bytes now live in the recycle bin; its row would point at the new file.
                        recycled_in_place[str(placed_path)] = pre_recycled
                    imported_paths.append(str(placed_path))
                    if matched_expected_track:
                        placed_to_track[str(placed_path)] = matched_expected_track
                        if file_weak_strength is not None:
                            record_weak_match(
                                db,
                                str(placed_path),
                                track_id=str(matched_expected_track.get("id") or ""),
                                title=str(matched_expected_track.get("title") or ""),
                                source_name=str(item.get("title") or ""),
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

                    file_write_tags, file_embed_art = write_tags, embed_art
                    if (file_write_tags or (file_embed_art and cover_bytes)) and not prepare_file_for_tagging(placed_path, media_settings):
                        file_write_tags = file_embed_art = False
                    if file_write_tags:
                        art_to_embed = cover_bytes if file_embed_art else None
                        try:
                            write_audio_tags(placed_path, tags=tags_to_write, cover_art_bytes=art_to_embed)
                        except Exception as e:
                            logger.warning("Error writing audio tags to %s: %s", placed_path, e)
                    elif file_embed_art and cover_bytes:
                        try:
                            embed_album_artwork(placed_path, cover_bytes)
                        except Exception as e:
                            logger.warning("Error embedding artwork into %s: %s", placed_path, e)

                    if save_cover and cover_bytes:
                        cover_file = placed_path.parent / "cover.jpg"
                        if not cover_file.exists():
                            try:
                                cover_file.write_bytes(cover_bytes)
                                clear_exec_bits(cover_file)
                                logger.info("Saved album cover to %s", cover_file)
                            except OSError as e:
                                logger.warning("Failed to save cover.jpg at %s: %s", cover_file, e)

                if held_files:
                    db.set_download_unmatched_files(download_id, held_files)
                    held_msg = f"{len(held_files)} file(s) couldn't be matched — manual import required"
                    if not imported_paths:
                        # Nothing placed: park the download for manual import (not a failure, no blocklisting).
                        db.update_download_status(
                            download_id, status=DownloadStatus.WARNING.value, error_message=held_msg
                        )
                        continue

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
                            download_id=download_id,
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

                # An issue-driven replacement retires the track's previous file(s) once the new row is in.
                replacement_issue_id: Optional[str] = None
                replaced_retired: list[str] = []
                replaced_kept: list[str] = []
                try:
                    replacement_issue_id = db.get_download_replacement_issue(str(download_id))
                except sqlite3.Error as ri_err:
                    logger.warning("Could not look up replacement issue for %s: %s", download_id, safe_exc(ri_err))

                # Native catalog upsert (when library_mode != "lidarr")
                if media_settings.get("library_mode") != "lidarr":
                    # Artist tag labels by artist id, looked up once per artist across the placed files.
                    import_tag_cache: dict[str, list[str]] = {}
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
                                            monitor_option=_scan_monitor_option(media_settings),
                                        ),
                                        preserve_monitoring=True,
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
                                parsed.quality = _quality_from_codec(f_meta) or parsed.quality

                            file_size = (
                                placed_p.stat().st_size
                                if placed_p.exists()
                                else int(item.get("size_bytes") or 0)
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
                                str(placed_p.relative_to(root_path))
                                if placed_p.is_relative_to(root_path)
                                else str(placed_p)
                            )
                            file_id = f"fil-{uuid.uuid4().hex[:12]}"
                            # Every other file the track has is superseded by this import (upgrade or issue
                            # replacement), except files this same download placed (a multi-file release).
                            sibling_paths = {str(Path(p).resolve()) for p in imported_paths} | {str(placed_p)}
                            old_file_rows = [
                                r for r in db.list_library_files_for_track(track_id)
                                if str(r.get("file_path") or "") not in sibling_paths
                            ]
                            in_place = recycled_in_place.pop(str(placed_p), None)
                            if in_place is not None:
                                old_file_rows = [r for r in old_file_rows if str(r["id"]) != str(in_place[1]["id"])]
                                self._log_recycled(db, item, in_place[0], placed_p, in_place[1], quality_str, media_settings,
                                                   replacement_issue_id, replaced_retired)
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
                                db, item, str(track_id), placed_p, quality_str, f_meta,
                                ([in_place[1]] if in_place is not None else []) + old_file_rows,
                            )
                            if old_file_rows:
                                self._recycle_replaced_files(
                                    db, item, replacement_issue_id, old_file_rows, placed_p, root_path,
                                    media_settings, quality_str, replaced_retired, replaced_kept,
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
                            parsed.quality = _quality_from_codec(last_metadata) or parsed.quality

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
                                artist_tags=delay_gate.artist_tags(
                                    db, (req.get("artist") if req else None) or item.get("artist")
                                ),
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
                # Held files still live in the client's download folder: never remove the transfer while they wait.
                if held_files:
                    logger.info(
                        "Download %s keeps %d unmatched file(s); skipping download-client cleanup",
                        download_id,
                        len(held_files),
                    )
                else:
                    try:
                        db.set_download_placed_files(download_id, imported_paths, import_mode)
                    except sqlite3.Error as placed_err:
                        logger.warning("Could not record placed files for %s: %s", download_id, safe_exc(placed_err))
                    should_keep_seeding = (
                        settle_transfer_after_import(
                            driver,
                            target_lookup,
                            media_settings,
                            import_mode,
                            status_dict,
                            db.get_active_download(download_id) or item,
                            db,
                        )
                        == DownloadStatus.COMPLETED.value
                    )

                if held_files:
                    db.update_download_status(
                        download_id,
                        status=DownloadStatus.WARNING.value,
                        error_message=held_msg,
                        target_path=target_summary,
                    )
                elif should_keep_seeding:
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

                try:  # a grab made by an issue's "Search again" tells that issue; the admin decides the status
                    issue_id = replacement_issue_id
                    if issue_id and db.get_issue(issue_id):
                        body = "Replacement imported"
                        for line in replaced_retired:
                            body += f"\nRetired old file: {line}"
                        for line in replaced_kept:
                            body += f"\nOld file kept at {line}"
                        db.add_issue_comment(issue_id, None, body, is_admin=True, is_system=True, staff=True)
                except sqlite3.Error as issue_err:
                    logger.warning("Could not comment on the issue for download %s: %s", download_id, safe_exc(issue_err))

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
                        as_media_server(plex_client).refresh_library()
                    except Exception as e:
                        logger.warning("Error triggering Plex library refresh: %s", e)

            else:
                # Update progress and active status
                db.update_download_status(download_id, status=cur_status)

        return stats


# Global acquisition worker instance
acquisition_worker = AcquisitionWorker()

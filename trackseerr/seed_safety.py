"""Seed cleanup evaluation and deletion safety checks for Trackseerr."""

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


from trackseerr.item_history import TRIGGER_SEED_CLEANUP
from trackseerr.models import (
    DownloadStatus,
)
from trackseerr.redaction import redact_text, safe_exc


from trackseerr.import_files import preserves_source, translate_remote_path

logger = logging.getLogger(__name__)


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


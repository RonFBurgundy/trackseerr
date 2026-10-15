"""Relinks a library file to the catalog track its audio really is (the fix for an AcoustID rename-preview mismatch).

No FastAPI imports: the route maps ``LookupError`` / ``ValueError`` / ``RelinkConflict`` / ``OSError`` to HTTP.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from trackseerr.item_history import emit
from trackseerr.recycle_bin import _client_roots, _library_root, dispose_for_settings, log_recycled

logger = logging.getLogger(__name__)

LOSSLESS_CODECS = frozenset({"FLAC", "ALAC", "WAV", "AIFF", "APE", "WV"})
CONFLICT_CHOICES = frozenset({"keep_incoming", "keep_existing", "prefer_quality"})
SOURCE = "Relink"


class RelinkConflict(Exception):
    """The target track already has a file and the caller has not said which one to keep."""

    def __init__(self, existing: dict[str, Any], incoming: dict[str, Any]) -> None:
        super().__init__("The right track already has a file")
        self.existing = existing
        self.incoming = incoming


def quality_key(f: dict[str, Any]) -> tuple[int, int, int, int, int]:
    """Sort key for a file's quality: lossless first, then bit depth, sample rate, bitrate, size (None counts as 0)."""
    lossless = 1 if str(f.get("codec") or "").upper() in LOSSLESS_CODECS else 0
    return (
        lossless,
        int(f.get("bits_per_sample") or 0),
        int(f.get("sample_rate") or 0),
        int(f.get("bitrate") or 0),
        int(f.get("size_bytes") or 0),
    )


def _dispose(db: Any, row: dict[str, Any], kept: dict[str, Any], title: str, settings: tuple[Any, ...]) -> None:
    """Recycles (or deletes, per settings) a file and drops its row. A file that cannot be moved stays on disk with
    its row in place and ``OSError`` is raised."""
    mm, library_root, client_roots = settings
    path = Path(str(row["file_path"]))
    if library_root is None:
        raise OSError("No library root folder is configured, so the file cannot be moved to the recycle bin")
    result = dispose_for_settings(mm, library_root, path, client_roots)
    if result.status == "kept":
        raise OSError(f"{path.name} could not be moved to the recycle bin: {result.reason}")
    if result.status in ("recycled", "deleted"):
        log_recycled(
            db, result, Path(str(kept["file_path"])), row, str(kept.get("quality_name") or ""),
            title=title, download_id=None, issue_id=None, retired=[], source=SOURCE, log_prefix="Relink",
        )
    db.delete_library_file(str(row["id"]))


def _resolve_conflict(
    conflict: Optional[str], existing: list[dict[str, Any]], incoming: dict[str, Any]
) -> str:
    """The concrete choice (keep_incoming / keep_existing) for a conflict; raises ``RelinkConflict`` when undecided."""
    if conflict is None:
        raise RelinkConflict(existing[0], incoming)
    if conflict not in CONFLICT_CHOICES:
        raise ValueError(f"Unknown conflict choice: {conflict!r}")
    if conflict != "prefer_quality":
        return conflict
    best = max(existing, key=quality_key)
    if quality_key(incoming) == quality_key(best):
        raise RelinkConflict(best, incoming)
    return "keep_incoming" if quality_key(incoming) > quality_key(best) else "keep_existing"


def _record_relink(db: Any, file_row: dict[str, Any], old: dict[str, Any], target: dict[str, Any]) -> None:
    name = Path(str(file_row["file_path"])).name
    emit(
        db, "relinked", track_id=target["id"],
        message=f"{name}: was linked to '{old.get('title')}'",
        details={"file_id": file_row["id"], "from_track_id": old["id"], "to_track_id": target["id"]},
    )
    emit(
        db, "relinked", track_id=old["id"],
        message=f"File moved to '{target.get('title')}' (AcoustID)",
        details={"file_id": file_row["id"], "from_track_id": old["id"], "to_track_id": target["id"]},
    )


def relink_file(db: Any, file_id: str, target_track_id: str, conflict: Optional[str]) -> dict[str, Any]:
    """Points ``file_id`` at ``target_track_id``. When the target already has a file, ``conflict`` picks the winner
    (``keep_incoming``, ``keep_existing`` or ``prefer_quality``); without it ``RelinkConflict`` is raised."""
    file_row = db.get_library_file(file_id)
    if not file_row:
        raise LookupError("File not found")
    old = db.get_library_track(str(file_row["track_id"]))
    if not old:
        raise LookupError("The track this file is linked to was not found")
    target = db.get_library_track(target_track_id)
    if not target:
        raise LookupError("Target track not found")
    if str(target["id"]) == str(old["id"]):
        raise ValueError("The file is already linked to that track")
    if conflict is not None and conflict not in CONFLICT_CHOICES:
        raise ValueError(f"Unknown conflict choice: {conflict!r}")

    result: dict[str, Any] = {
        "action": "relinked", "kept_file_id": file_id, "recycled_file_id": None,
        "old_track_id": str(old["id"]), "target_track_id": str(target["id"]),
    }
    existing = db.list_library_files_for_track(target_track_id)
    choice = _resolve_conflict(conflict, existing, file_row) if existing else None
    mm = db.get_media_management_settings()
    settings = (mm, _library_root(mm), _client_roots(db, mm))
    title = str(target.get("title") or "")

    if choice == "keep_existing":
        _dispose(db, file_row, existing[0], title, settings)
        return {**result, "action": "kept_existing", "kept_file_id": str(existing[0]["id"]), "recycled_file_id": file_id}
    for row in existing:
        _dispose(db, row, file_row, title, settings)
    if existing:
        result["recycled_file_id"] = str(existing[0]["id"])
    if not db.relink_library_file(file_id, target_track_id):
        raise LookupError("File not found")
    logger.info("Relinked file %s from track %s to %s", file_id, old["id"], target["id"])
    _record_relink(db, file_row, old, target)
    return result


def relink_files(
    db: Any,
    items: list[Any],
    conflict: Optional[str] = "skip",
) -> dict[str, Any]:
    """Relinks multiple files in sequence.

    When ``conflict == 'skip'``, items that encounter a ``RelinkConflict`` are caught and marked as ``conflict_skipped``.
    Otherwise, ``conflict`` is passed through to ``relink_file``.
    """
    results: list[dict[str, Any]] = []
    relinked_count = 0
    conflicts_skipped_count = 0
    errors_count = 0

    per_file_conflict = None if conflict == "skip" else conflict

    for item in items:
        file_id = item.file_id if hasattr(item, "file_id") else item["file_id"]
        target_track_id = item.target_track_id if hasattr(item, "target_track_id") else item["target_track_id"]
        res_entry: dict[str, Any] = {"file_id": file_id}
        try:
            res = relink_file(db, file_id, target_track_id, per_file_conflict)
            res_entry["status"] = res.get("action", "relinked")
            res_entry["kept_file_id"] = res.get("kept_file_id")
            res_entry["recycled_file_id"] = res.get("recycled_file_id")
            res_entry["old_track_id"] = res.get("old_track_id")
            res_entry["target_track_id"] = res.get("target_track_id")
            relinked_count += 1
        except RelinkConflict:
            res_entry["status"] = "conflict_skipped"
            conflicts_skipped_count += 1
        except (LookupError, ValueError) as exc:
            res_entry["status"] = "error"
            res_entry["message"] = str(exc)
            errors_count += 1

        results.append(res_entry)

    return {
        "results": results,
        "relinked": relinked_count,
        "conflicts_skipped": conflicts_skipped_count,
        "errors": errors_count,
    }


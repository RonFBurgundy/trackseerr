"""Album merges that Library Self-Heal could not finish, and the admin's choice of which copy to keep.

No FastAPI imports: the route maps ``LookupError`` / ``ValueError`` / ``DisposalError`` to HTTP.
"""

from __future__ import annotations

import logging
from typing import Any

from trackseerr.artist_repair import merge_conflict_pairs, merge_single_album_into
from trackseerr.file_relink import RelinkConflict, relink_file

logger = logging.getLogger(__name__)

CHOICES = frozenset({"keep_existing", "keep_incoming", "prefer_quality", "keep_both"})


class DisposalError(Exception):
    """A losing file could not be moved to the recycle bin; nothing further was changed."""


def list_choices(db: Any) -> list[dict[str, Any]]:
    """Every open blocked merge with both albums and the file pairs that collide."""
    items: list[dict[str, Any]] = []
    for conflict in db.list_merge_conflicts(include_dismissed=False):
        pairs = merge_conflict_pairs(db, conflict["source_album_id"], conflict["holder_album_id"])
        items.append({**conflict, "pairs": pairs})
    return items


def _relink_pair(db: Any, pair: dict[str, Any], choice: str) -> dict[str, Any]:
    source_file_id = str(pair["source_file"]["id"])
    holder_track_id = str(pair["holder_track"]["id"])
    try:
        return relink_file(db, source_file_id, holder_track_id, choice)
    except RelinkConflict:
        # Same quality on both sides: keep the copy that is already in the holder album.
        logger.info("Duplicate choice: quality tie for file %s, keeping the existing copy", source_file_id)
        return relink_file(db, source_file_id, holder_track_id, "keep_existing")


def resolve_choice(db: Any, conflict_id: int, choice: str) -> dict[str, Any]:
    """Applies the admin's choice to a recorded conflict; returns ``{merged, recycled_file_ids, kept_file_ids}``."""
    if choice not in CHOICES:
        raise ValueError(f"Unknown choice: {choice!r}")
    conflict = db.get_merge_conflict(conflict_id)
    if conflict is None:
        raise LookupError("Duplicate not found")
    source_id, holder_id = conflict["source_album_id"], conflict["holder_album_id"]
    if choice == "keep_both":
        db.dismiss_merge_conflict(conflict_id)
        logger.info(
            "Duplicate choice: keeping both '%s' (%s) and '%s' (%s)",
            conflict["source_title"], source_id, conflict["holder_title"], holder_id,
        )
        return {"merged": False, "recycled_file_ids": [], "kept_file_ids": []}

    recycled: list[str] = []
    kept: list[str] = []
    for pair in merge_conflict_pairs(db, source_id, holder_id):
        try:
            result = _relink_pair(db, pair, choice)
        except OSError as exc:
            logger.error("Duplicate choice %s on conflict %s stopped: %s", choice, conflict_id, exc)
            raise DisposalError(str(exc)) from exc
        if result.get("recycled_file_id"):
            recycled.append(str(result["recycled_file_id"]))
        kept.append(str(result["kept_file_id"]))
        logger.info(
            "Duplicate choice %s: kept file %s, recycled %s", choice, result["kept_file_id"], result.get("recycled_file_id")
        )

    outcome = merge_single_album_into(db, source_id, holder_id)
    merged = outcome == "merged"
    if merged:
        db.clear_merge_conflicts_for_album(source_id)
    logger.info(
        "Duplicate choice %s: '%s' into '%s' -> %s", choice, conflict["source_title"], conflict["holder_title"], outcome
    )
    return {"merged": merged, "recycled_file_ids": recycled, "kept_file_ids": kept}

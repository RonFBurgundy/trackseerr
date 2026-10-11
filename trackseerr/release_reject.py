"""Rejecting the release behind an item's current files: blocklist it, then search for a replacement.

Shared by the issue fix action ``blocklist_and_research`` and ``POST /api/library/{entity}/{id}/reject-current``.
The current files are never deleted here; the replacement's import swaps them (and records ``upgraded``).
"""

import logging
from typing import Any, Literal, Optional

from trackseerr.backlog_worker import ReplacementSpec, backlog_worker
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

REJECT_REASON = "Rejected from the library"


def blocklist_current_release(
    db: Database, album_id: str, track_ids: list[str], request_id: Optional[str], reason: str
) -> Optional[dict[str, Any]]:
    """Blocklists the release whose import produced the album's/tracks' files.

    Returns ``{"blocklist_id", "release"}``, or None when the import history does not name a release.
    """
    source = db.get_imported_release_source(album_id, track_ids, request_id)
    if source is None:
        return None
    item = db.add_to_blocklist(
        source_title=str(source["release_title"]),
        artist=source.get("artist"),
        album=source.get("album"),
        release_guid=source.get("release_guid"),
        info_hash=source.get("info_hash"),
        protocol=source.get("protocol"),
        indexer=source.get("indexer"),
        reason=reason,
    )
    return {"blocklist_id": item["id"], "release": item["source_title"]}


def reject_current_release(
    db: Database, entity: Literal["album", "track"], entity_id: str, actor_user_id: Optional[str]
) -> dict[str, Any]:
    """Blocklists the release behind an album's or track's files and queues a replacement search.

    Raises ``LookupError`` when the entity does not exist. When no source release is on record nothing is
    blocklisted, but the replacement search still runs.
    """
    if entity == "album":
        album_id: Optional[str] = str(entity_id)
        if db.get_library_album(album_id) is None:
            raise LookupError("Album not found")
        track_ids = [str(t["id"]) for t in db.list_library_tracks(album_id=album_id, limit=1000)]
    else:
        track = db.get_library_track(str(entity_id))
        if track is None:
            raise LookupError("Track not found")
        album_id = str(track["album_id"]) if track.get("album_id") else None
        track_ids = [str(entity_id)]

    blocklisted = blocklist_current_release(db, album_id, track_ids, None, REJECT_REASON) if album_id else None
    targets = db.list_wanted_search_targets(track_ids=track_ids)
    search = backlog_worker.queue_wanted_search(
        db, targets, replacement=ReplacementSpec(issue_id=None, require_better=False), actor_user_id=actor_user_id
    )
    queued = int(search.get("queued") or 0)
    if blocklisted is None:
        logger.info("Reject-current on %s %s: no imported release on record, nothing blocklisted", entity, entity_id)
        message = "The release that produced these files could not be identified, so nothing was blocklisted."
    else:
        message = f"Blocklisted {blocklisted['release']}."
    if queued:
        message += f" Searching for a replacement ({queued} queued)."
    else:
        message += f" {search.get('message') or 'No replacement search was queued.'}"
    return {
        "blocklisted": blocklisted is not None,
        "release": blocklisted["release"] if blocklisted else None,
        "queued": queued,
        "message": message,
    }

"""Library-wide AcoustID verification ("Wrong files") service."""

from __future__ import annotations

from typing import Any, Callable, Optional

from trackseerr.rename_verification import MISMATCH, start_verification, verdict_for

_NO_KEY_MESSAGE = "Add an AcoustID API key in Settings → Media Management first"


def start_wrong_file_check(db: Any) -> bool:
    """Starts a background AcoustID check across every file in the library."""
    key = str(db.get_media_management_settings().get("acoustid_api_key") or "").strip()
    if not key:
        raise ValueError(_NO_KEY_MESSAGE)
    return start_verification(db, db.list_library_file_ids(), key)


def _cached_lookup(
    cache: dict[str, Optional[dict[str, Any]]],
    fetcher: Callable[[str], Optional[dict[str, Any]]],
    entity_id: Optional[str],
) -> Optional[dict[str, Any]]:
    if not entity_id:
        return None
    if entity_id not in cache:
        cache[entity_id] = fetcher(entity_id)
    return cache[entity_id]


def _actual_library_fields(
    db: Any,
    actual_track_id: Optional[str],
    albums: dict[str, Optional[dict[str, Any]]],
    artists: dict[str, Optional[dict[str, Any]]],
) -> dict[str, Optional[str]]:
    if not actual_track_id:
        return {"actual_track_title": None, "actual_album_title": None, "actual_artist_name": None}
    actual_track = db.get_library_track(actual_track_id)
    actual_album = _cached_lookup(albums, db.get_library_album, (actual_track or {}).get("album_id"))
    actual_artist_id = (actual_track or {}).get("artist_id") or (actual_album or {}).get("artist_id")
    actual_artist = _cached_lookup(artists, db.get_library_artist, actual_artist_id)
    return {
        "actual_track_title": (actual_track or {}).get("title"),
        "actual_album_title": (actual_album or {}).get("title"),
        "actual_artist_name": (actual_artist or {}).get("name"),
    }


def _build_item(
    db: Any,
    row: dict[str, Any],
    track: dict[str, Any],
    verdict: dict[str, Any],
    albums: dict[str, Optional[dict[str, Any]]],
    artists: dict[str, Optional[dict[str, Any]]],
) -> dict[str, Any]:
    album = _cached_lookup(albums, db.get_library_album, track.get("album_id"))
    artist_id = track.get("artist_id") or (album or {}).get("artist_id")
    artist = _cached_lookup(artists, db.get_library_artist, artist_id)
    actual_fields = _actual_library_fields(db, verdict.get("actual_track_id"), albums, artists)
    return {
        "file_id": row["file_id"],
        "relative_path": row.get("relative_path"),
        "track_id": row["track_id"],
        "track_title": str(track.get("title") or ""),
        "album_id": track.get("album_id"),
        "album_title": str((album or {}).get("title") or ""),
        "artist_name": str((artist or {}).get("name") or ""),
        "actual_title": verdict.get("actual_title"),
        "actual_artist": verdict.get("actual_artist"),
        "actual_track_id": verdict.get("actual_track_id"),
        "actual_album_id": verdict.get("actual_album_id"),
        "score": verdict.get("score"),
        **actual_fields,
    }


def list_wrong_files(db: Any) -> dict[str, Any]:
    """Lists fingerprinted library files whose audio mismatches their linked track."""
    albums: dict[str, Optional[dict[str, Any]]] = {}
    artists: dict[str, Optional[dict[str, Any]]] = {}
    items: list[dict[str, Any]] = []
    checked = 0

    for row in db.list_fingerprinted_files():
        track = db.get_library_track(row["track_id"])
        if not track:
            continue
        verdict = verdict_for(db, row, track, row["current_path"])
        if verdict is None:
            continue
        checked += 1
        if verdict["status"] != MISMATCH:
            continue
        items.append(_build_item(db, row, track, verdict, albums, artists))

    items.sort(
        key=lambda item: (
            item["artist_name"].casefold(),
            item["album_title"].casefold(),
            item["track_title"].casefold(),
        )
    )
    return {
        "items": items,
        "total": len(items),
        "checked": checked,
        "library_files": len(db.list_library_file_ids()),
    }

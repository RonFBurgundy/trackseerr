"""Add a missing wanted song from MusicBrainz so held downloads can import cleanly.

When a single-track request downloads a file whose title matches the wanted song
but the library album lacks that track (e.g. a reissue bonus track), this module
resolves a confident MusicBrainz recording and creates a file-less track row.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from typing import Any, Optional

import requests

from trackseerr import (
    compilation_filing,
    lidarr_release,
    mb_metadata_store,
    recording_search,
    release_matching,
    track_matching,
)
from trackseerr.models import LibraryTrack
from trackseerr.redaction import safe_exc

logger = logging.getLogger(__name__)


def _resolve_artist(db: Any, item: dict[str, Any], metadata: dict[str, Any]) -> Optional[dict[str, Any]]:
    artist_id = track_matching.download_artist_id(db, item, None)
    if not artist_id and metadata.get("artist"):
        art = db.get_library_artist_by_name(str(metadata["artist"]))
        if art and art.get("id"):
            artist_id = str(art["id"])
    if not artist_id:
        return None
    artist = db.get_library_artist(str(artist_id))
    if not artist or not str(artist.get("name") or "").strip():
        return None
    return artist


def _search_recording(
    db: Any, artist_name: str, wanted_title: str, duration: Optional[float]
) -> Optional[dict[str, Any]]:
    enricher = mb_metadata_store.get_shared_enricher(db)
    query_title = recording_search.search_title(wanted_title, artist_name)
    candidates = enricher.search_recordings(artist_name, query_title)
    if not candidates:
        return None
    chosen = recording_search.pick_search_recording(
        candidates, wanted_title, artist_name, duration, set()
    )
    if not chosen:
        return None
    if not lidarr_release.titles_match(chosen.get("title") or "", wanted_title):
        return None
    return chosen


def _unowned_recording_track(
    db: Any, rec_id: str, album_id: Optional[str] = None
) -> tuple[bool, Optional[dict[str, Any]]]:
    """Returns (found_existing, track_if_fileless)."""
    existing = db.get_library_track_by_mb_recording_id(rec_id, album_id=album_id)
    if not existing:
        return False, None
    if db.get_library_file_for_track(str(existing["id"])) is None:
        return True, existing
    return True, None


def _create_album_track(
    db: Any,
    album: dict[str, Any],
    artist: dict[str, Any],
    chosen: dict[str, Any],
    rec_id: str,
    duration: Optional[float],
    item_id: Any,
) -> dict[str, Any]:
    album_id = str(album["id"])
    disc1_nums = [
        int(t.get("track_number") or 0)
        for t in db.list_library_tracks(album_id=album_id, limit=1000)
        if int(t.get("disc_number") or 1) == 1
    ]
    next_track_num = (max(disc1_nums) if disc1_nums else 0) + 1
    length_ms = chosen.get("length_ms")
    track_duration = float(length_ms) / 1000.0 if length_ms else duration
    created = db.upsert_library_track(
        LibraryTrack(
            id=str(uuid.uuid4()),
            album_id=album_id,
            artist_id=str(album["artist_id"]),
            title=str(chosen["title"]),
            track_number=next_track_num,
            disc_number=1,
            duration_seconds=track_duration,
            monitored=True,
            mb_recording_id=rec_id,
        )
    )
    if str(album["artist_id"]) != str(artist["id"]):
        compilation_filing.ensure_primary_credit(
            db, str(created["id"]), str(artist["name"]), artist.get("mbid")
        )
    logger.info(
        "Added %r to %s from MusicBrainz (recording %s) for download %s",
        created["title"],
        album.get("title"),
        rec_id,
        item_id,
    )
    return created


def add_wanted_song_from_musicbrainz(
    db: Any, item: dict[str, Any], metadata: dict[str, Any], duration: Optional[float]
) -> Optional[dict[str, Any]]:
    """Return a file-less library track for a wanted song from MusicBrainz, or None."""
    if str(item.get("item_type") or "track") != "track":
        return None
    try:
        wanted_title, _ = track_matching.wanted_song(db, item)
        if not wanted_title or not str(wanted_title).strip():
            return None
        if not lidarr_release.titles_match(metadata.get("title") or "", wanted_title):
            return None

        artist = _resolve_artist(db, item, metadata)
        if not artist:
            return None
        artist_id = str(artist["id"])
        artist_name = str(artist["name"]).strip()

        chosen = _search_recording(db, artist_name, wanted_title, duration)
        if not chosen or not chosen.get("id"):
            return None
        rec_id = str(chosen["id"])

        found_global, fileless_global = _unowned_recording_track(db, rec_id)
        if found_global:
            return fileless_global

        album_title = metadata.get("album") or item.get("album")
        album = release_matching.album_for_recording(
            db, artist_id, rec_id, album_title=album_title, track_title=wanted_title
        )
        if not album:
            return None

        found_album, fileless_album = _unowned_recording_track(
            db, rec_id, album_id=str(album["id"])
        )
        if found_album:
            return fileless_album

        return _create_album_track(
            db, album, artist, chosen, rec_id, duration, item.get("id")
        )
    except (requests.RequestException, sqlite3.Error) as exc:
        logger.warning("MusicBrainz held-song catalog add failed: %s", safe_exc(exc))
        return None

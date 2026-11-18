"""Resolve playlist tracks to local library files by ISRC or MusicBrainz identity."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

import requests

from .models import Track
from .recording_search import pick_search_recording
from .redaction import safe_exc

logger = logging.getLogger(__name__)

# MusicBrainz allows ~1 request/s, so capping at 40 keeps playlist sync under ~1 minute.
DEFAULT_SYNC_MB_LOOKUPS = 40


@dataclass(frozen=True)
class LibraryHit:
    """Resolved library file for a playlist track."""

    track_id: str
    title: str
    artist: str
    album: str
    file_path: str
    via: str


class MusicBrainzBudget:
    """Simple per-run counter capping MusicBrainz network lookups."""

    def __init__(self, limit: int = DEFAULT_SYNC_MB_LOOKUPS) -> None:
        self.limit = max(0, int(limit))
        self.used = 0

    def take(self) -> bool:
        """Consume one lookup slot, returning False once the limit is reached."""
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def _hit_from_track(db: Any, track_row: dict[str, Any], via: str) -> LibraryHit | None:
    track_id = str(track_row.get("id") or "")
    if not track_id:
        return None
    files = db.list_library_files_for_track(track_id)
    if not files:
        return None
    file_path = str(files[0].get("file_path") or "")
    if not file_path:
        return None
    artist_id = track_row.get("artist_id")
    album_id = track_row.get("album_id")
    artist_row = db.get_library_artist(str(artist_id)) if artist_id else None
    album_row = db.get_library_album(str(album_id)) if album_id else None
    return LibraryHit(
        track_id=track_id,
        title=str(track_row.get("title") or ""),
        artist=str(artist_row.get("name") or "") if artist_row else "",
        album=str(album_row.get("title") or "") if album_row else "",
        file_path=file_path,
        via=via,
    )


def _hit_by_recording_id(db: Any, recording_id: str, via: str) -> LibraryHit | None:
    track_row = db.get_library_track_by_mb_recording_id(recording_id)
    if not track_row:
        return None
    return _hit_from_track(db, track_row, via=via)


def find_by_isrc(db: Any, isrc: str) -> LibraryHit | None:
    """Find a filed library track in the local DB matching ``isrc`` (case-insensitive)."""
    clean_isrc = (isrc or "").strip().upper()
    if not clean_isrc:
        return None
    track_row = db.find_library_track_by_isrc(clean_isrc)
    if not track_row:
        return None
    return _hit_from_track(db, track_row, via="isrc")


def resolve_with_musicbrainz(
    db: Any,
    enricher: Any,
    track: Track,
    budget: MusicBrainzBudget | None = None,
) -> LibraryHit | None:
    """Resolve a playlist track to a filed library track via MusicBrainz ISRC or recording search."""
    if budget is not None and not budget.take():
        return None

    try:
        if track.isrc:
            mbids = enricher.lookup_track_mbids(track.artist, track.album, track.title, isrc=track.isrc)
            rec_id = mbids.get("musicbrainz_trackid") if isinstance(mbids, dict) else None
            if rec_id:
                hit = _hit_by_recording_id(db, str(rec_id), via="isrc_musicbrainz")
                if hit is not None:
                    return hit

        candidates = enricher.search_recordings(track.artist, track.title)
        if candidates is None:
            return None
        picked = pick_search_recording(candidates, track.title, track.artist, track.duration_seconds, set())
        if picked and picked.get("id"):
            return _hit_by_recording_id(db, str(picked["id"]), via="musicbrainz_search")
    except (requests.RequestException, sqlite3.Error) as exc:
        logger.debug("MusicBrainz playlist identity resolution failed for '%s - %s': %s", track.artist, track.title, safe_exc(exc))
        return None

    return None

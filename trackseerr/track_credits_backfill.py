"""Backfill of per-track artist credits for tracks that have none (MusicBrainz first, then file tags)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from mutagen import MutagenError

from trackseerr.library import inspect_audio_file
from trackseerr.storage_common import clean_library_name
from trackseerr.track_credits import build_track_credits, store_track_credits

logger = logging.getLogger(__name__)

_BATCH = 200


def _match_mb_track(track: dict[str, Any], mb_tracks: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The MusicBrainz track for a library track: by recording id, else by position AND title (never position alone)."""
    rec = str(track.get("mb_recording_id") or "").strip()
    if rec:
        for mb in mb_tracks:
            if str(mb.get("mb_recording_id") or "").strip() == rec:
                return mb
    title = clean_library_name(str(track.get("title") or ""))
    if not title:
        return None
    position = (int(track.get("disc_number") or 1), int(track.get("track_number") or 1))
    for mb in mb_tracks:
        if (
            (int(mb.get("disc_number") or 1), int(mb.get("track_number") or 1)) == position
            and clean_library_name(str(mb.get("title") or "")) == title
        ):
            return mb
    return None


def _credits_from_musicbrainz(db: Any, enricher: Any, album: dict[str, Any], tracks: list[dict[str, Any]]) -> int:
    """Stores MusicBrainz credits for the tracks that match; returns how many tracks gained credits."""
    rg = album.get("mb_release_group_id")
    if not rg:
        return 0
    mb_tracks = enricher.get_release_group_tracks(rg) or []
    stored = 0
    for track in tracks:
        mb = _match_mb_track(track, mb_tracks)
        if mb is None or not mb.get("artist_credit"):
            continue
        store_track_credits(db, track["id"], mb)
        if db.list_track_artists(track["id"]):
            track["has_credits"] = True
            stored += 1
    return stored


def _credits_from_tags(db: Any, track: dict[str, Any]) -> bool:
    """Stores a single primary credit from the file's artist tag; False when there is no usable tag."""
    path = str(track.get("file_path") or "")
    if not path or not Path(path).exists():
        return False
    try:
        tags = inspect_audio_file(path)
    except (OSError, ValueError, MutagenError) as exc:
        logger.warning("Track credits backfill: cannot read tags of %s: %s", path, exc)
        return False
    credits = build_track_credits(
        None,
        str(track.get("title") or ""),
        tag_artist=tags.get("artist"),
        tag_artist_mbid=tags.get("musicbrainz_artistid"),
    )
    if not credits:
        return False
    db.replace_track_artists(track["id"], credits)
    return True


def _backfill_album(db: Any, enricher: Any, album: dict[str, Any], counts: dict[str, int]) -> None:
    tracks = [t for t in db.list_album_tracks_with_files(album["id"]) if not t["has_credits"]]
    if not tracks:
        return
    counts["tracks_musicbrainz"] += _credits_from_musicbrainz(db, enricher, album, tracks)
    for track in tracks:
        if track["has_credits"]:
            continue
        if _credits_from_tags(db, track):
            counts["tracks_tags"] += 1
        else:
            counts["tracks_skipped"] += 1


def run_credits_backfill(
    db: Any, enricher: Any, *, should_stop: Callable[[], bool] | None = None
) -> dict[str, int]:
    """Fills credits for library tracks that have a file but none stored; resumable and safe to re-run."""
    counts = {"albums": 0, "tracks_musicbrainz": 0, "tracks_tags": 0, "tracks_skipped": 0, "errors": 0}
    after: Optional[str] = None
    while True:
        albums = db.list_albums_needing_credits(after, _BATCH)
        if not albums:
            break
        for album in albums:
            if should_stop is not None and should_stop():
                logger.info("Track credits backfill stopped: %s", counts)
                return counts
            after = album["id"]
            try:
                _backfill_album(db, enricher, album, counts)
                counts["albums"] += 1
            except (sqlite3.Error, OSError, ValueError, RuntimeError, KeyError) as exc:
                counts["errors"] += 1
                logger.warning("Track credits backfill failed for album %s: %s", album["id"], exc)
    logger.info("Track credits backfill finished: %s", counts)
    return counts

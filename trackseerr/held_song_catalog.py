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
    track_matching,
)
from trackseerr.album_titles import album_titles_match
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


def _base_title(text: str) -> str:
    """Strip bracketed qualifiers and any trailing ' - ...' suffix; return '' if nothing was stripped."""
    normalized = " ".join(text.split()).strip()
    if not normalized:
        return ""
    stripped = lidarr_release._BRACKETED.sub(" ", normalized)
    parts = lidarr_release._DASH_SPLIT.split(stripped, maxsplit=1)
    result = " ".join(parts[0].split()).strip()
    if not result or result == normalized:
        return ""
    return result


def _pick_base_title_candidate(
    candidates: list[dict[str, Any]], base_title: str, duration: Optional[float]
) -> Optional[dict[str, Any]]:
    """Pick a unique duration-matched candidate for a base-title search."""
    if not duration or duration <= 0:
        return None
    matches: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        length_ms = candidate.get("length_ms")
        if length_ms is None:
            continue
        if not lidarr_release.titles_match(candidate.get("title") or "", base_title):
            continue
        gap = abs(float(length_ms) / 1000.0 - float(duration))
        if gap <= 3.0:
            matches.append((gap, candidate))
    if not matches:
        return None
    matches.sort(key=lambda pair: pair[0])
    if len(matches) > 1 and (matches[1][0] - matches[0][0]) <= 1.0:
        return None
    return matches[0][1]


def _search_recording(
    enricher: Any, artist_name: str, wanted_title: str, duration: Optional[float]
) -> tuple[Optional[dict[str, Any]], bool]:
    query_title = recording_search.search_title(wanted_title, artist_name)
    candidates = enricher.search_recordings(artist_name, query_title)
    if candidates is None:
        return None, False
    if candidates:
        chosen = recording_search.pick_search_recording(
            candidates, wanted_title, artist_name, duration, set()
        )
        if chosen and lidarr_release.titles_match(chosen.get("title") or "", wanted_title):
            return chosen, False
    base = _base_title(query_title)
    if not base:
        return None, False
    import re
    from trackseerr import release_matching

    stripped_query = re.sub(
        r"(?:\s*[\(\[]\s*\d{4}\s+(?:mix|version)\s*[\)\]]|\s*[-–—]\s*\d{4}\s+(?:mix|version))\s*$",
        "",
        query_title,
        flags=re.IGNORECASE,
    )
    if release_matching._version_words(stripped_query) - release_matching._version_words(base):
        return None, False
    base_candidates = enricher.search_recordings(artist_name, base)
    if not base_candidates:
        return None, False
    chosen = _pick_base_title_candidate(base_candidates, base, duration)
    if chosen is None:
        return None, False
    return chosen, True


def _home_album(
    db: Any,
    enricher: Any,
    artist: dict[str, Any],
    rec_id: str,
    album_title: Optional[str],
    wanted_title: str,
) -> Optional[dict[str, Any]]:
    """Return the library album for this recording's home release group, creating it if needed."""
    artist_id = str(artist["id"])
    groups = enricher.get_recording_release_groups(rec_id) or []
    if album_title:
        for group in groups:
            if group.get("is_various") or not group.get("id"):
                continue
            if album_titles_match(group.get("title"), album_title):
                holder = db.album_holding_release_group(artist_id, str(group["id"]), "")
                if holder is not None:
                    album = db.get_library_album(str(holder))
                    if album is not None:
                        return album

    home = compilation_filing.home_release_group(groups, artist.get("mbid"), wanted_title)
    if home is None or not home.get("id"):
        return None
    holder = db.album_holding_release_group(artist_id, str(home["id"]), "")
    if holder is not None:
        return db.get_library_album(str(holder))
    album_id = compilation_filing.ensure_release_group_album(db, enricher, artist_id, home)
    return db.get_library_album(album_id)


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
    title: Optional[str] = None,
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
            title=str(title or chosen["title"]),
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


def _log_skip(item: dict[str, Any], reason: str) -> None:
    logger.info("MusicBrainz add skipped for download %s: %s", item.get("id"), reason)


def add_wanted_song_from_musicbrainz(
    db: Any, item: dict[str, Any], metadata: dict[str, Any], duration: Optional[float]
) -> Optional[dict[str, Any]]:
    """Return a file-less library track for a wanted song from MusicBrainz, or None."""
    if str(item.get("item_type") or "track") != "track":
        _log_skip(item, "not a track request")
        return None
    try:
        wanted_title, _ = track_matching.wanted_song(db, item)
        if not wanted_title or not str(wanted_title).strip():
            _log_skip(item, "no wanted song title")
            return None
        if not lidarr_release.titles_match(metadata.get("title") or "", wanted_title):
            _log_skip(item, "file title is not the wanted song")
            return None

        artist = _resolve_artist(db, item, metadata)
        if not artist:
            _log_skip(item, "no library artist")
            return None
        artist_name = str(artist["name"]).strip()

        enricher = mb_metadata_store.get_shared_enricher(db)
        chosen, used_base = _search_recording(enricher, artist_name, wanted_title, duration)
        if not chosen or not chosen.get("id"):
            _log_skip(item, "no confident MusicBrainz recording")
            return None
        rec_id = str(chosen["id"])

        found_global, fileless_global = _unowned_recording_track(db, rec_id)
        if found_global:
            if fileless_global is None:
                _log_skip(item, "recording already has a file")
            return fileless_global

        album_title = metadata.get("album") or item.get("album")
        album = _home_album(db, enricher, artist, rec_id, album_title, wanted_title)
        if not album:
            _log_skip(item, "no home album")
            return None

        found_album, fileless_album = _unowned_recording_track(
            db, rec_id, album_id=str(album["id"])
        )
        if found_album:
            if fileless_album is None:
                _log_skip(item, "recording already has a file")
            return fileless_album

        track_title = str(wanted_title) if used_base else str(chosen["title"])
        return _create_album_track(
            db, album, artist, chosen, rec_id, duration, item.get("id"), title=track_title
        )
    except (requests.RequestException, sqlite3.Error) as exc:
        logger.warning("MusicBrainz held-song catalog add failed: %s", safe_exc(exc))
        return None

"""Finds a performer's Deezer release for compilation tracks that MusicBrainz cannot place."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

import requests

from trackseerr.album_titles import album_titles_match
from trackseerr.clients.mbid_enricher import artist_name_key
from trackseerr.models import LibraryAlbum
from trackseerr.recording_search import search_title
from trackseerr.redaction import safe_exc
from trackseerr.release_matching import _titles_close

logger = logging.getLogger(__name__)

_MAX_ALBUM_LOOKUPS = 8
_MAX_DURATION_GAP_SECONDS = 10.0


def _resolve_deezer_artist_id(db: Any, discovery: Any, artist: dict[str, Any]) -> Optional[str]:
    for candidate in (artist.get("discovery_id"), artist.get("foreign_artist_id")):
        text = str(candidate or "").strip()
        if text.startswith("deezer:artist:"):
            return text
    artist_id = str(artist.get("id") or "").strip()
    if artist_id and hasattr(db, "get_artist_link"):
        link = db.get_artist_link(library_artist_id=artist_id)
        linked_id = str((link or {}).get("discovery_id") or "").strip()
        if linked_id.startswith("deezer:artist:"):
            return linked_id
    artist_name = str(artist.get("name") or "").strip()
    if not artist_name:
        return None
    result = discovery.search_artist(artist_name)
    if not isinstance(result, dict):
        return None
    if artist_name_key(str(result.get("name") or "")) != artist_name_key(artist_name):
        return None
    deezer_id = str(result.get("id") or "").strip()
    return deezer_id or None


def _ordered_releases(
    details: dict[str, Any], song_title: str, artist_name: str, current_title: str
) -> list[dict[str, Any]]:
    albums = [
        r for r in (details.get("albums") or [])
        if not album_titles_match(r.get("title"), current_title)
    ]
    singles_eps = [
        r for r in (details.get("singles_eps") or [])
        if not album_titles_match(r.get("title"), current_title)
    ]
    song_key = search_title(song_title, artist_name)
    close: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for release in albums + singles_eps:
        if _titles_close(search_title(str(release.get("title") or "")), song_key):
            close.append(release)
        else:
            rest.append(release)
    return close + rest


def _duration_matches(deezer_dur: Any, track_dur: Any) -> bool:
    if not deezer_dur or not track_dur:
        return True
    return abs(float(deezer_dur) - float(track_dur)) <= _MAX_DURATION_GAP_SECONDS


def _album_has_track(
    album_details: dict[str, Any], song_title: str, artist_name: str, track_dur: Any
) -> bool:
    song_key = search_title(song_title, artist_name)
    for item in album_details.get("tracks") or []:
        item_title = search_title(str(item.get("title") or ""))
        if _titles_close(item_title, song_key) and _duration_matches(item.get("duration_seconds"), track_dur):
            return True
    return False


def _release_year(release_date: Any) -> Optional[int]:
    text = str(release_date or "").strip()
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def _ensure_deezer_album(
    db: Any, artist_id: str, release: dict[str, Any], album_details: dict[str, Any]
) -> Optional[str]:
    foreign_id = str(release.get("id") or "").strip()
    if not foreign_id:
        return None
    existing = db.get_library_album_by_foreign_id(foreign_id)
    if existing is not None:
        return str(existing["id"]) if str(existing["artist_id"]) == artist_id else None

    raw_type = str(release.get("record_type") or "album").lower().strip()
    album_type = raw_type if raw_type in ("single", "ep", "album") else "album"
    release_date = album_details.get("release_date")
    tracks = album_details.get("tracks") or []
    created = db.upsert_library_album(
        LibraryAlbum(
            id=str(uuid.uuid4()),
            artist_id=artist_id,
            title=str(release.get("title") or ""),
            foreign_album_id=foreign_id,
            release_date=release_date,
            year=_release_year(release_date),
            album_type=album_type,
            monitored=False,
            cover_url=release.get("cover_url"),
            total_tracks=len(tracks) or None,
        )
    )
    return str(created["id"])


def deezer_home_album(
    db: Any,
    discovery: Any,
    artist: dict[str, Any],
    track: dict[str, Any],
    current_album: dict[str, Any],
) -> Optional[str]:
    """Library album id of the performer's Deezer single/EP/album holding this song (created unmonitored), or None."""
    artist_id = str(artist.get("id") or "")
    artist_name = str(artist.get("name") or "")
    song_title = str(track.get("title") or "")
    current_title = str(current_album.get("title") or "")
    if not artist_id or not song_title:
        return None

    try:
        deezer_id = _resolve_deezer_artist_id(db, discovery, artist)
        if deezer_id is None:
            return None
        details = discovery.get_artist_details(deezer_id)
        if not isinstance(details, dict) or "error" in details:
            return None

        candidates = _ordered_releases(details, song_title, artist_name, current_title)
        for release in candidates[:_MAX_ALBUM_LOOKUPS]:
            release_id = str(release.get("id") or "").strip()
            if not release_id:
                continue
            album_details = discovery.get_album_details(release_id)
            if not isinstance(album_details, dict) or "error" in album_details:
                continue
            if not _album_has_track(album_details, song_title, artist_name, track.get("duration_seconds")):
                continue
            holder_id = _ensure_deezer_album(db, artist_id, release, album_details)
            if holder_id is not None:
                logger.info(
                    "Deezer home for %r by %r: %r (%s)",
                    song_title,
                    artist_name,
                    release.get("title"),
                    release_id,
                )
                return holder_id
    except requests.RequestException as exc:
        logger.warning("Deezer home lookup failed for %r by %r: %s", song_title, artist_name, safe_exc(exc))
        return None

    return None

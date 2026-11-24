"""Finds a performer's Deezer release for compilation tracks that MusicBrainz cannot place."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Optional

import requests

from trackseerr.album_titles import album_titles_match
from trackseerr.clients.mbid_enricher import artist_name_key
from trackseerr.lidarr_release import _fold, norm_title
from trackseerr.models import LibraryAlbum
from trackseerr.recording_search import search_title
from trackseerr.redaction import safe_exc

logger = logging.getLogger(__name__)

_MAX_ALBUM_LOOKUPS = 8
_MAX_SEARCH_ALBUMS = 5
_MAX_DURATION_GAP_SECONDS = 10.0

_HARD_WORDS = frozenset({"remix", "live", "vip", "dub", "rework", "bootleg", "acoustic", "demo", "karaoke", "cover"})
_SOFT_WORDS = frozenset(
    {
        "instrumental",
        "extended",
        "original",
        "radio",
        "edit",
        "mix",
        "remaster",
        "remastered",
        "version",
        "explicit",
        "clean",
        "feat",
        "ft",
        "featuring",
    }
)
_REISSUE_WORDS = ("deluxe", "expanded", "supersize", "anniversary", "remaster", "remastered", "edition", "bonus")
_TYPE_RANK = {"album": 0, "single": 1, "ep": 2}
_BRACKETED = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_DASH_SPLIT = re.compile(r"\s+[-–—]\s+")


def _fold_words(text: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", _fold(text))


def _is_soft_segment(segment: str) -> bool:
    words = _fold_words(segment)
    return not words or all(w in _SOFT_WORDS or w.isdigit() for w in words)


def _strip_soft_segments(text: str) -> str:
    cleaned = _BRACKETED.sub(lambda m: " " if _is_soft_segment(m.group(1)) else m.group(0), text)
    parts = _DASH_SPLIT.split(cleaned)
    while len(parts) > 1 and _is_soft_segment(parts[-1]):
        parts.pop()
    return " - ".join(parts)


def _soft_titles_close(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` match after dropping soft version/edition segments."""
    a_clean = search_title(a)
    b_clean = search_title(b)
    if (set(_fold_words(a_clean)) & _HARD_WORDS) != (set(_fold_words(b_clean)) & _HARD_WORDS):
        return False
    na = norm_title(_strip_soft_segments(a_clean))
    nb = norm_title(_strip_soft_segments(b_clean))
    return bool(na and nb and na == nb)


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


def _search_album_ids(discovery: Any, artist_name: str, song_title: str) -> list[str]:
    """Returns up to 5 unique Deezer album discovery IDs matching ``artist_name`` and ``song_title``."""
    if not artist_name or not song_title:
        return []
    results = discovery.search(f'artist:"{artist_name}" track:"{song_title}"', item_type="track", limit=10)
    if not isinstance(results, list):
        return []
    artist_key = artist_name_key(artist_name)
    song_key = search_title(song_title, artist_name)
    album_ids: list[str] = []
    seen: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        if not str(item.get("id") or "").startswith("deezer:track:"):
            continue
        if artist_name_key(str(item.get("artist") or "")) != artist_key:
            continue
        if not _soft_titles_close(search_title(str(item.get("title") or "")), song_key):
            continue
        album_id = str(item.get("album_discovery_id") or "").strip()
        if album_id and album_id not in seen:
            seen.add(album_id)
            album_ids.append(album_id)
            if len(album_ids) >= _MAX_SEARCH_ALBUMS:
                break
    return album_ids


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
        if _soft_titles_close(search_title(str(release.get("title") or "")), song_key):
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
        if _soft_titles_close(item_title, song_key) and _duration_matches(item.get("duration_seconds"), track_dur):
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


def _edition_rank(release: dict[str, Any], album_details: dict[str, Any]) -> tuple[bool, int, str]:
    title_folded = _fold(release.get("title") or album_details.get("title") or "")
    is_reissue = any(word in title_folded for word in _REISSUE_WORDS)
    record_type = str(release.get("record_type") or album_details.get("record_type") or "").casefold().strip()
    type_rank = _TYPE_RANK.get(record_type, 3)
    release_date = str(album_details.get("release_date") or "9999")
    return (is_reissue, type_rank, release_date)


def _search_album_valid(album_details: dict[str, Any], artist_name: str, current_title: str) -> bool:
    record_type = str(album_details.get("record_type") or "").casefold().strip()
    if record_type in ("compile", "compilation"):
        return False
    if artist_name_key(str(album_details.get("artist") or "")) != artist_name_key(artist_name):
        return False
    if current_title and album_titles_match(album_details.get("title"), current_title):
        return False
    return True


def _collect_matching_albums(
    discovery: Any,
    search_ids: list[str],
    discography: list[dict[str, Any]],
    artist_name: str,
    song_title: str,
    current_title: str,
    track_dur: Any,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    queue: list[tuple[str, Optional[dict[str, Any]], bool]] = [(aid, None, True) for aid in search_ids]
    queue.extend((str(r.get("id") or "").strip(), r, False) for r in discography)

    tried: set[str] = set()
    lookups = 0
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for album_id, release, from_search in queue:
        if not album_id or album_id in tried:
            continue
        if lookups >= _MAX_ALBUM_LOOKUPS:
            break
        tried.add(album_id)
        lookups += 1
        album_details = discovery.get_album_details(album_id)
        if not isinstance(album_details, dict) or "error" in album_details:
            continue
        if from_search and not _search_album_valid(album_details, artist_name, current_title):
            continue
        if not _album_has_track(album_details, song_title, artist_name, track_dur):
            continue
        rel = release if release is not None else {
            "id": album_id,
            "title": album_details.get("title"),
            "record_type": album_details.get("record_type"),
            "cover_url": album_details.get("cover_url"),
        }
        matches.append((rel, album_details))
    return matches


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
        search_ids = _search_album_ids(discovery, artist_name, song_title)
        deezer_id = _resolve_deezer_artist_id(db, discovery, artist)
        details = discovery.get_artist_details(deezer_id) if deezer_id is not None else None
        discography = (
            _ordered_releases(details, song_title, artist_name, current_title)
            if isinstance(details, dict) and "error" not in details
            else []
        )
        if not search_ids and not discography:
            return None

        matches = _collect_matching_albums(
            discovery, search_ids, discography, artist_name, song_title, current_title, track.get("duration_seconds")
        )
        matches.sort(key=lambda pair: _edition_rank(pair[0], pair[1]))
        for release, album_details in matches:
            holder_id = _ensure_deezer_album(db, artist_id, release, album_details)
            if holder_id is not None:
                logger.info(
                    "Deezer home for %r by %r: %r (%s)",
                    song_title,
                    artist_name,
                    release.get("title"),
                    release.get("id"),
                )
                return holder_id
    except requests.RequestException as exc:
        logger.warning("Deezer home lookup failed for %r by %r: %s", song_title, artist_name, safe_exc(exc))
        return None

    return None

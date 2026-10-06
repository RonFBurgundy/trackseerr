"""Unified artist profile: a discovery artist's full discography merged with what the library owns.

The library side is read through the same helpers ``api/routes/library.py`` uses for artist and album listing, so the
native and Lidarr library modes behave exactly as they do on the Library pages. On a gateway the local database holds no
library: availability is resolved on core per item (``annotate_item_statuses``) and the library section stays empty.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

import requests

from plex_playlist_sync import artist_links, lidarr_library
from plex_playlist_sync.artist_links import ArtistLink, LibraryIndex, LidarrLibraryIndex, NativeLibraryIndex
from plex_playlist_sync.clients.lidarr import LidarrApiError, LidarrClient, LidarrNotFound
from plex_playlist_sync.library_manager import MODE_LIDARR, get_library_mode
from plex_playlist_sync.storage import Database, clean_library_name

logger = logging.getLogger(__name__)

TOP_TRACKS_LIMIT = 10
_REQUEST_STATUSES = ("requested", "processing", "rejected")
DISCOGRAPHY_GROUPS = ("albums", "singles_eps", "compilations")

# Words that make a trailing "(...)" / "[...]" / " - ..." an edition marker rather than part of the title.
_EDITION_WORDS = (
    r"deluxe|edition|remaster(?:ed)?|expanded|anniversary|bonus|single|ep|version|mono|stereo|explicit|"
    r"reissue|special|collector'?s?|digital|super|extended|bonus tracks?|standard|mix|live at"
)
_BRACKET_SUFFIX = re.compile(rf"\s*[\(\[][^\)\]]*\b(?:{_EDITION_WORDS})\b[^\)\]]*[\)\]]\s*$", re.IGNORECASE)
_DASH_SUFFIX = re.compile(rf"\s+[-–—]\s+[^-–—]*\b(?:{_EDITION_WORDS})\b[^-–—]*$", re.IGNORECASE)


def normalize_album_title(title: Optional[str]) -> str:
    """Comparison key for album titles.

    Lowercases, repeatedly strips trailing edition markers ("(Deluxe Edition)", "[Remastered 2011]", "- Single",
    "- EP"), turns "&" into "and", then folds punctuation and whitespace with ``clean_library_name``.
    """
    text = str(title or "").strip()
    for _ in range(4):
        stripped = _BRACKET_SUFFIX.sub("", text)
        stripped = _DASH_SUFFIX.sub("", stripped)
        if stripped == text:
            break
        text = stripped
    return clean_library_name(text.replace("&", " and "))


def _year(value: Any) -> Optional[int]:
    text = str(value or "")
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None


# ---------------------------------------------------------------------------------------------------- library source


@dataclass
class LibrarySource:
    """The library as the active mode exposes it (native tables or Lidarr)."""

    db: Database
    mode: str
    lidarr: Optional[LidarrClient] = None

    def index(self) -> Optional[LibraryIndex]:
        """A lookup over library artists; None when the library cannot be read (e.g. Lidarr unconfigured/down)."""
        if self.mode != MODE_LIDARR:
            return NativeLibraryIndex(self.db)
        if self.lidarr is None:
            return None
        try:
            return LidarrLibraryIndex(row.record for row in lidarr_library.snapshot("artists", self.lidarr))
        except (LidarrApiError, LidarrNotFound, requests.RequestException) as exc:
            logger.warning("Lidarr artist list unavailable for artist profile: %s", exc)
            return None

    def artist_and_albums(self, artist_id: str) -> Optional[tuple[dict[str, Any], list[dict[str, Any]]]]:
        """``(summary, albums)`` for one library artist, or None when it does not exist / cannot be read.

        ``summary`` has ``artist_id, name, monitored, album_count, track_count, track_file_count``; each album has
        ``id, title, year, release_date, cover_url, track_count, track_file_count, monitored``.
        """
        if self.mode == MODE_LIDARR:
            return self._from_lidarr(artist_id)
        return self._from_native(artist_id)

    def _from_native(self, artist_id: str) -> Optional[tuple[dict[str, Any], list[dict[str, Any]]]]:
        from plex_playlist_sync.api.routes import library as library_routes

        try:
            artist = self.db.get_library_artist(artist_id)
            if artist is None:
                return None
            albums: list[dict[str, Any]] = []
            offset = 0
            while True:
                page = self.db.list_library_albums(artist_id=artist_id, limit=500, offset=offset)
                albums.extend(page)
                if len(page) < 500:
                    break
                offset += 500
            stored, with_files = library_routes._album_track_counts(self.db, artist_id)
            counts = library_routes._enrich_artists(self.db, [artist])[0]
        except sqlite3.Error as exc:
            logger.warning("Could not read library artist %s for profile: %s", artist_id, exc)
            return None

        out_albums = [
            {
                "id": str(alb["id"]),
                "title": alb.get("title") or "",
                "year": alb.get("year") or _year(alb.get("release_date")),
                "release_date": alb.get("release_date"),
                "cover_url": library_routes._versioned_art_url(
                    "album", alb["id"], alb.get("art_version"), alb.get("cover_url")
                ),
                "track_count": int(alb.get("total_tracks") or stored.get(alb["id"], 0) or 0),
                "track_file_count": with_files.get(alb["id"], 0),
                "monitored": bool(alb.get("monitored", True)),
            }
            for alb in albums
        ]
        summary = {
            "artist_id": str(artist["id"]),
            "name": artist.get("name") or "",
            "image_url": counts.get("image_url"),
            "monitored": bool(artist.get("monitored", True)),
            "album_count": int(counts.get("album_count") or len(out_albums)),
            "track_count": int(counts.get("track_count") or 0),
            "track_file_count": sum(a["track_file_count"] for a in out_albums),
        }
        return summary, out_albums

    def _from_lidarr(self, artist_id: str) -> Optional[tuple[dict[str, Any], list[dict[str, Any]]]]:
        if self.lidarr is None or not str(artist_id).isdigit():
            return None
        try:
            detail = lidarr_library.artist_detail(self.lidarr, int(artist_id))
        except LidarrNotFound:
            return None
        except (LidarrApiError, requests.RequestException) as exc:
            logger.warning("Lidarr artist %s unavailable for profile: %s", artist_id, exc)
            return None
        out_albums = [
            {
                "id": str(alb["id"]),
                "title": alb.get("title") or "",
                "year": alb.get("year"),
                "release_date": alb.get("release_date"),
                "cover_url": alb.get("cover_url"),
                "track_count": int(alb.get("track_count") or 0),
                "track_file_count": int(alb.get("track_file_count") or 0),
                "monitored": bool(alb.get("monitored", True)),
            }
            for alb in detail.get("albums", [])
        ]
        summary = {
            "artist_id": str(detail["id"]),
            "name": detail.get("name") or "",
            "image_url": detail.get("image_url"),
            "monitored": bool(detail.get("monitored", True)),
            "album_count": int(detail.get("album_count") or len(out_albums)),
            "track_count": int(detail.get("track_count") or 0),
            "track_file_count": int(detail.get("track_file_count") or sum(a["track_file_count"] for a in out_albums)),
        }
        return summary, out_albums


def make_library_source(db: Database, lidarr: Optional[LidarrClient]) -> LibrarySource:
    return LibrarySource(db=db, mode=get_library_mode(db), lidarr=lidarr)


# ---------------------------------------------------------------------------------------------------- album matching


def ownership_status(have: int, total: int) -> str:
    """``in_library`` when every known track has a file, ``partial`` when some do, ``missing`` when none."""
    if have <= 0:
        return "missing"
    if total <= 0 or have >= total:
        return "in_library"
    return "partial"


def match_discography(
    groups: dict[str, list[dict[str, Any]]], library_albums: list[dict[str, Any]]
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Matches discovery albums to library albums by normalised title (same-year match preferred among duplicates).

    Returns ``(groups, library_only)``: each matched item gains ``library_album_id``, ``have_tracks``,
    ``total_tracks`` and an ownership ``status`` (``in_library``/``partial``, or ``missing`` when owned without files,
    left for the caller to override from requests); library albums nothing matched are returned as new items.
    """
    by_title: dict[str, list[dict[str, Any]]] = {}
    for alb in library_albums:
        by_title.setdefault(normalize_album_title(alb["title"]), []).append(alb)

    claimed: set[str] = set()
    out: dict[str, list[dict[str, Any]]] = {}
    for group in DISCOGRAPHY_GROUPS:
        items: list[dict[str, Any]] = []
        for raw in groups.get(group, []):
            item = dict(raw)
            candidates = by_title.get(normalize_album_title(item.get("title")), [])
            if candidates:
                year = _year(item.get("release_date"))
                best = max(
                    candidates,
                    key=lambda c: (year is not None and c.get("year") == year, c["id"] not in claimed),
                )
                claimed.add(best["id"])
                total = int(best["track_count"] or item.get("track_count") or 0)
                have = int(best["track_file_count"])
                item.update(
                    library_album_id=best["id"],
                    have_tracks=have,
                    total_tracks=total,
                    status=ownership_status(have, total),
                )
            items.append(item)
        out[group] = items

    library_only: list[dict[str, Any]] = []
    for alb in library_albums:
        if alb["id"] in claimed:
            continue
        total = int(alb["track_count"] or 0)
        have = int(alb["track_file_count"])
        library_only.append(
            {
                "id": f"library:album:{alb['id']}",
                "item_type": "album",
                "title": alb["title"],
                "album": alb["title"],
                "year": alb.get("year") or _year(alb.get("release_date")),
                "cover_url": alb.get("cover_url"),
                "release_date": alb.get("release_date"),
                "library_album_id": alb["id"],
                "have_tracks": have,
                "total_tracks": total,
                "status": ownership_status(have, total),
            }
        )
    return out, library_only


# ---------------------------------------------------------------------------------------------------- profile


def _top_tracks(discovery: Any, discovery_id: Optional[str], annotate: Any) -> list[dict[str, Any]]:
    """Deezer top tracks (up to 10) with status; [] for non-Deezer ids or when Deezer fails."""
    if not discovery_id or not discovery_id.startswith("deezer:artist:"):
        return []
    try:
        raw = discovery.get_artist_top_tracks_detailed(discovery_id, limit=TOP_TRACKS_LIMIT)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Top tracks unavailable for %s: %s", discovery_id, exc)
        return []
    items = [{**t, "item_type": "track"} for t in raw[:TOP_TRACKS_LIMIT]]
    annotated = annotate(items)
    result = []
    for t in annotated:
        entry = {
            "id": t.get("id"),
            "title": t.get("title"),
            "album": t.get("album"),
            "duration": t.get("duration"),
            "preview_url": t.get("preview_url"),
            "status": t.get("status", "none"),
        }
        if t.get("request_id") is not None:
            entry["request_id"] = t["request_id"]
        result.append(entry)
    return result


def build_profile(
    db: Database,
    discovery: Any,
    source: Optional[LibrarySource],
    annotate: Any,
    discovery_id: Optional[str] = None,
    library_artist_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Assembles the profile; None when neither side resolves to an artist.

    ``source`` is None on a gateway (no local library). ``annotate(items)`` annotates discovery items with
    request/availability status (``annotate_item_statuses`` bound to the request's dependencies).
    """
    index = source.index() if source is not None else None
    link = ArtistLink(name="")
    details: Optional[dict[str, Any]] = None

    if discovery_id:
        try:
            details = discovery.get_artist_details(discovery_id)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Discovery artist %s unavailable: %s", discovery_id, exc)
        if index is not None:
            link = artist_links.resolve_from_discovery(db, discovery, discovery_id, library=index, details=details)
        else:
            link = ArtistLink(name=(details or {}).get("name", ""), discovery_id=discovery_id)
        if not details and not link.library_artist_id:
            return None
    else:
        if index is None or source is None:
            return None
        link = artist_links.resolve_from_library(db, discovery, str(library_artist_id), library=index)
        if not link.name:
            return None
        if link.discovery_id:
            try:
                details = discovery.get_artist_details(link.discovery_id)
            except (requests.RequestException, ValueError) as exc:
                logger.warning("Discovery artist %s unavailable: %s", link.discovery_id, exc)

    library_data = None
    if source is not None and link.library_artist_id:
        library_data = source.artist_and_albums(link.library_artist_id)

    effective_discovery_id = link.discovery_id or discovery_id

    raw_groups = {g: list((details or {}).get(g) or []) for g in DISCOGRAPHY_GROUPS}
    for items in raw_groups.values():
        for it in items:
            it.setdefault("item_type", "album")
            if effective_discovery_id:
                it.setdefault("artist_discovery_id", effective_discovery_id)

    library_albums = library_data[1] if library_data else []
    matched, library_only = match_discography(raw_groups, library_albums)

    # Unmatched items, and owned albums with no files, still need request/availability status.
    pending = [it for items in matched.values() for it in items if it.get("status") in (None, "missing")]
    if pending:
        for original, fresh in zip(pending, annotate(pending)):
            fresh_status = fresh.get("status") or "none"
            if original.get("library_album_id") and fresh_status not in _REQUEST_STATUSES:
                continue  # owned but empty: stays "missing"; annotate's name matching must not upgrade it
            original["status"] = fresh_status
            if fresh.get("request_id") is not None:
                original["request_id"] = fresh["request_id"]
            if fresh.get("quality") is not None:
                original["quality"] = fresh["quality"]

    name = link.name or (details or {}).get("name") or (library_data[0]["name"] if library_data else "")
    image_url = (details or {}).get("image_url") or (library_data[0].get("image_url") if library_data else None)
    summary = library_data[0] if library_data else None

    return {
        "artist": {
            "name": name,
            "image_url": image_url,
            "discovery_id": effective_discovery_id,
            "library_artist_id": link.library_artist_id,
            "mbid": link.mbid,
            "link_confidence": link.confidence,
        },
        "library": (
            {
                "artist_id": summary["artist_id"],
                "monitored": summary["monitored"],
                "album_count": summary["album_count"],
                "track_count": summary["track_count"],
                "track_file_count": summary["track_file_count"],
            }
            if summary
            else None
        ),
        "top_tracks": _top_tracks(discovery, effective_discovery_id, annotate),
        "discography": {**matched, "library_only": library_only},
    }


# Fields a non-admin may see on a discography item: discovery metadata plus ownership status and counts. Anything that
# identifies or manages a library object (library ids, paths, files, quality, monitoring) is deliberately absent.
_REQUESTER_ITEM_KEYS = (
    "id", "item_type", "title", "artist", "album", "cover_url", "release_date", "record_type", "track_count",
    "status", "request_id", "have_tracks", "total_tracks", "artist_discovery_id",
)


def _public_url(url: Any) -> Optional[str]:
    """The URL only when it is an absolute http(s) address (never one of our own ``/api/...`` art endpoints)."""
    text = str(url or "")
    return text if text.startswith(("http://", "https://")) else None


def shape_for_requester(profile: dict[str, Any]) -> dict[str, Any]:
    """Reduces a full profile to what a non-admin may see (no library ids, MBID, link data or library section)."""
    artist = profile["artist"]
    discography: dict[str, list[dict[str, Any]]] = {
        group: [{k: it[k] for k in _REQUESTER_ITEM_KEYS if k in it} for it in profile["discography"].get(group, [])]
        for group in DISCOGRAPHY_GROUPS
    }
    owned = [
        {"title": it["title"], "year": it.get("year"), "status": "in_library"}
        for it in profile["discography"].get("library_only", [])
        if int(it.get("have_tracks") or 0) > 0
    ]
    discography["library_only"] = owned
    return {
        "artist": {
            "name": artist["name"],
            "image_url": _public_url(artist.get("image_url")),
            "discovery_id": artist.get("discovery_id"),
        },
        "library": None,
        "top_tracks": profile["top_tracks"],
        "discography": discography,
    }

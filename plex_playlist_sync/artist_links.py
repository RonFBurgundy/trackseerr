"""Identity linking between discovery artists (Deezer/iTunes ids) and library artists.

``resolve_from_discovery`` and ``resolve_from_library`` return an :class:`ArtistLink` and cache the outcome in the
``artist_links`` table: positive links are trusted for 7 days, negative ones ("no match") for 1 day. A link made through
a MusicBrainz id beats one made through the normalised name (``clean_library_name``, the same function that fills
``library_artists.clean_name``).

Library access goes through a small :class:`LibraryIndex` so the same resolution works against the native tables
(:class:`NativeLibraryIndex`) and against a Lidarr artist list (:class:`LidarrLibraryIndex`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Protocol

import requests

from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.mb_metadata_store import get_shared_enricher
from plex_playlist_sync.storage import Database, clean_library_name

logger = logging.getLogger(__name__)

POSITIVE_TTL = timedelta(days=7)
NEGATIVE_TTL = timedelta(days=1)

_MBID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_DEEZER_PREFIX = "deezer:artist:"


@dataclass
class ArtistLink:
    name: str
    discovery_id: Optional[str] = None
    library_artist_id: Optional[str] = None
    mbid: Optional[str] = None
    confidence: str = "none"  # 'mbid' | 'name' | 'none'


def normalize_artist_name(name: Optional[str]) -> str:
    """The library's own normalisation (lowercase, punctuation stripped, whitespace folded)."""
    return clean_library_name(name or "")


def extract_mbid(*values: Any) -> Optional[str]:
    """First MusicBrainz uuid found in the given strings (handles ``musicbrainz:artist:<uuid>`` and bare ids)."""
    for value in values:
        if not value:
            continue
        found = _MBID_RE.search(str(value))
        if found:
            return found.group(0).lower()
    return None


def _artist_mbid(record: dict[str, Any]) -> Optional[str]:
    return extract_mbid(record.get("foreign_artist_id"), record.get("mbid"))


# ---------------------------------------------------------------------------------------------------- library access


class LibraryIndex(Protocol):
    def find_by_mbid(self, mbid: str) -> Optional[dict[str, Any]]: ...

    def find_by_clean_name(self, clean_name: str) -> Optional[dict[str, Any]]: ...

    def get(self, artist_id: str) -> Optional[dict[str, Any]]: ...


class NativeLibraryIndex:
    """Looks library artists up in the native ``library_artists`` table."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def find_by_mbid(self, mbid: str) -> Optional[dict[str, Any]]:
        with self._db._lock:
            row = self._db.conn.execute(
                "SELECT * FROM library_artists WHERE foreign_artist_id IN (?, ?) OR mbid = ? LIMIT 1",
                (mbid, f"musicbrainz:artist:{mbid}", mbid),
            ).fetchone()
            return self._db._map_library_artist(row) if row else None

    def find_by_clean_name(self, clean_name: str) -> Optional[dict[str, Any]]:
        if not clean_name:
            return None
        with self._db._lock:
            row = self._db.conn.execute(
                "SELECT * FROM library_artists WHERE clean_name = ? ORDER BY name COLLATE NOCASE, id LIMIT 1",
                (clean_name,),
            ).fetchone()
            return self._db._map_library_artist(row) if row else None

    def get(self, artist_id: str) -> Optional[dict[str, Any]]:
        return self._db.get_library_artist(str(artist_id))


class LidarrLibraryIndex:
    """Looks library artists up in a list of Lidarr artist records (``lidarr_library.artist_row`` shape)."""

    def __init__(self, records: Iterable[dict[str, Any]]) -> None:
        self._records = list(records)
        self._by_id = {str(r.get("id")): r for r in self._records}

    def find_by_mbid(self, mbid: str) -> Optional[dict[str, Any]]:
        return next((r for r in self._records if _artist_mbid(r) == mbid), None)

    def find_by_clean_name(self, clean_name: str) -> Optional[dict[str, Any]]:
        if not clean_name:
            return None
        return next(
            (r for r in self._records if (r.get("clean_name") or normalize_artist_name(r.get("name"))) == clean_name),
            None,
        )

    def get(self, artist_id: str) -> Optional[dict[str, Any]]:
        return self._by_id.get(str(artist_id))


# ---------------------------------------------------------------------------------------------------- cache helpers

def get_enricher(db: Database) -> MbidEnricherClient:
    """Process-wide MusicBrainz client so its 1 req/s rate limit is shared across requests."""
    return get_shared_enricher(db)


def _parse_stamp(raw: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_fresh(row: dict[str, Any], now: Optional[datetime] = None) -> bool:
    stamp = _parse_stamp(row.get("updated_at"))
    if stamp is None:
        return False
    ttl = NEGATIVE_TTL if row.get("confidence") == "none" else POSITIVE_TTL
    return (now or datetime.now(timezone.utc)) - stamp < ttl


def _from_row(row: dict[str, Any], name: str = "") -> ArtistLink:
    return ArtistLink(
        name=row.get("name") or name,
        discovery_id=row.get("discovery_id"),
        library_artist_id=row.get("library_artist_id"),
        mbid=row.get("mbid"),
        confidence=row.get("confidence") or "none",
    )


def _deezer_url(discovery_id: Optional[str]) -> Optional[str]:
    if discovery_id and discovery_id.startswith(_DEEZER_PREFIX):
        num = discovery_id[len(_DEEZER_PREFIX):]
        if num.isdigit():
            return f"https://www.deezer.com/artist/{num}"
    return None


def _lookup_mbid_for_discovery(db: Database, discovery_id: str, enricher: Optional[MbidEnricherClient]) -> Optional[str]:
    url = _deezer_url(discovery_id)
    if url is None:
        return None
    try:
        return (enricher or get_enricher(db)).lookup_artist_mbid_by_url(url)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("MusicBrainz URL lookup failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------------------------------- resolution


def resolve_from_discovery(
    db: Database,
    discovery: Any,
    discovery_artist_id: str,
    library: Optional[LibraryIndex] = None,
    details: Optional[dict[str, Any]] = None,
    enricher: Optional[MbidEnricherClient] = None,
) -> ArtistLink:
    """Finds the library artist for a discovery artist id: MBID match first, then normalised name.

    ``details`` (the ``get_artist_details`` payload) may be passed when the caller already fetched it.
    """
    index = library or NativeLibraryIndex(db)
    cached = db.get_artist_link(discovery_id=discovery_artist_id)
    if cached and _is_fresh(cached):
        lib_id = cached.get("library_artist_id")
        if not lib_id or index.get(lib_id) is not None:
            return _from_row(cached)

    if details is None:
        try:
            details = discovery.get_artist_details(discovery_artist_id)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Discovery artist lookup failed for %s: %s", discovery_artist_id, exc)
            details = None
    if not details:
        # Transient or unknown id: nothing worth caching.
        return ArtistLink(name="", discovery_id=discovery_artist_id)

    name = str(details.get("name") or "").strip()
    mbid = _lookup_mbid_for_discovery(db, discovery_artist_id, enricher)

    found: Optional[dict[str, Any]] = None
    confidence = "none"
    if mbid:
        found = index.find_by_mbid(mbid)
        if found:
            confidence = "mbid"
    if found is None:
        candidate = index.find_by_clean_name(normalize_artist_name(name))
        if candidate is not None:
            known = _artist_mbid(candidate)
            if mbid and known and known != mbid:
                logger.info("Name match for '%s' rejected: library MBID %s differs from %s", name, known, mbid)
            else:
                found, confidence = candidate, "name"

    link = ArtistLink(
        name=name,
        discovery_id=discovery_artist_id,
        library_artist_id=str(found["id"]) if found else None,
        mbid=mbid or (_artist_mbid(found) if found else None),
        confidence=confidence,
    )
    _store(db, link)
    return link


def resolve_from_library(
    db: Database,
    discovery: Any,
    library_artist_id: str,
    library: Optional[LibraryIndex] = None,
    enricher: Optional[MbidEnricherClient] = None,
) -> ArtistLink:
    """Finds the discovery artist for a library artist by exact normalised name (most fans wins ties)."""
    index = library or NativeLibraryIndex(db)
    artist = index.get(str(library_artist_id))
    if artist is None:
        return ArtistLink(name="", library_artist_id=str(library_artist_id))
    name = str(artist.get("name") or "")
    lib_mbid = _artist_mbid(artist)

    cached = db.get_artist_link(library_artist_id=str(library_artist_id))
    if cached and _is_fresh(cached):
        return _from_row(cached, name=name)

    try:
        candidates = discovery.search_artists(name, limit=10)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Discovery artist search failed for '%s': %s", name, exc)
        return ArtistLink(name=name, library_artist_id=str(library_artist_id), mbid=lib_mbid)

    if candidates is None:  # the lookup failed: transient, so nothing is cached
        return ArtistLink(name=name, library_artist_id=str(library_artist_id), mbid=lib_mbid)

    wanted = normalize_artist_name(name)
    exact = [c for c in candidates if normalize_artist_name(c.get("name")) == wanted]
    if not exact:
        link = ArtistLink(name=name, library_artist_id=str(library_artist_id), mbid=lib_mbid)
        _store(db, link)
        return link

    best = max(exact, key=lambda c: int(c.get("nb_fan") or 0))
    confidence = "name"
    if lib_mbid and _lookup_mbid_for_discovery(db, best["id"], enricher) == lib_mbid:
        confidence = "mbid"
    link = ArtistLink(
        name=name,
        discovery_id=best["id"],
        library_artist_id=str(library_artist_id),
        mbid=lib_mbid,
        confidence=confidence,
    )
    _store(db, link)
    return link


def _store(db: Database, link: ArtistLink) -> None:
    db.save_artist_link(link.library_artist_id, link.discovery_id, link.mbid, link.confidence, name=link.name or None)

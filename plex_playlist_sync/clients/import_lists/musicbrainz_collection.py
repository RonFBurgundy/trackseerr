"""MusicBrainz collection provider: artist, release-group and release collections."""

import re
import threading
import time
from typing import Any, Optional

from plex_playlist_sync.clients.import_lists.base import (
    ImportListError,
    ImportListItem,
    get_json,
    name_key,
    require_text,
)

DEFAULT_BASE_URL = "https://musicbrainz.org"
PAGE_SIZE = 100
# Browse endpoints tried in turn; a collection holds exactly one entity type, so the others answer 400/404 or empty.
_ENTITIES = ("release-group", "artist", "release")
_MBID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_JSON_KEYS = {"release-group": "release-groups", "artist": "artists", "release": "releases"}


_rate_lock = threading.Lock()
_last_request_at = 0.0
MIN_REQUEST_INTERVAL_SECONDS = 1.0


def _throttle(base: str) -> None:
    """Spaces requests to musicbrainz.org one second apart (probes of other entity types included)."""
    global _last_request_at
    if "musicbrainz.org" not in base:
        return
    with _rate_lock:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _artist_of(credit: Any) -> tuple[str, Optional[str]]:
    """First credited artist's name and MBID from an ``artist-credit`` list."""
    if isinstance(credit, list) and credit and isinstance(credit[0], dict):
        artist = credit[0].get("artist")
        if isinstance(artist, dict):
            return str(artist.get("name") or credit[0].get("name") or "").strip(), artist.get("id")
        return str(credit[0].get("name") or "").strip(), None
    return "", None


def _browse(base: str, entity: str, collection: str) -> Optional[list[dict[str, Any]]]:
    """All rows of ``collection`` browsed as ``entity``; None when the collection is not of that type."""
    rows: list[dict[str, Any]] = []
    offset = 0
    key = _JSON_KEYS[entity]
    while True:
        params: dict[str, Any] = {"collection": collection, "fmt": "json", "limit": PAGE_SIZE, "offset": offset}
        if entity == "release":
            params["inc"] = "artist-credits+release-groups"
        elif entity == "release-group":
            params["inc"] = "artist-credits"
        _throttle(base)
        try:
            data = get_json(f"{base}/ws/2/{entity}", params=params)
        except ImportListError as exc:
            if offset == 0 and ("HTTP 400" in str(exc) or "HTTP 404" in str(exc)):
                return None
            raise
        if not isinstance(data, dict):
            raise ImportListError("MusicBrainz returned an unexpected response")
        page = [r for r in (data.get(key) or []) if isinstance(r, dict)]
        rows.extend(page)
        total = int(data.get(f"{entity}-count") or 0)
        offset += PAGE_SIZE
        if not page or offset >= total:
            return rows


def _release_group_item(rg: dict[str, Any], fallback_artist: str = "", fallback_mbid: Optional[str] = None) -> Optional[ImportListItem]:
    title = str(rg.get("title") or "").strip()
    artist, artist_mbid = _artist_of(rg.get("artist-credit"))
    artist = artist or fallback_artist
    artist_mbid = artist_mbid or fallback_mbid
    rg_id = str(rg.get("id") or "").strip() or None
    if not title or not artist:
        return None
    return ImportListItem(
        kind="album",
        external_key=rg_id or name_key(artist, title),
        artist_name=artist,
        album_title=title,
        mbid=rg_id,
        artist_mbid=artist_mbid,
    )


def fetch(config: dict[str, Any], *, mb_base_url: Optional[str] = None) -> list[ImportListItem]:
    raw = require_text(config, "collection_mbid", "Collection MBID")
    match = _MBID_RE.search(raw)
    if not match:
        raise ImportListError("collection_mbid is not a valid MusicBrainz id")
    collection = match.group(0).lower()
    base = (mb_base_url or DEFAULT_BASE_URL).rstrip("/")

    for entity in _ENTITIES:
        rows = _browse(base, entity, collection)
        if not rows:
            continue
        items: list[ImportListItem] = []
        seen: set[str] = set()
        for row in rows:
            if entity == "artist":
                name = str(row.get("name") or "").strip()
                mbid = str(row.get("id") or "").strip() or None
                if not name:
                    continue
                item: Optional[ImportListItem] = ImportListItem(
                    kind="artist", external_key=mbid or name_key(name), artist_name=name, mbid=mbid, artist_mbid=mbid
                )
            elif entity == "release-group":
                item = _release_group_item(row)
            else:  # a release maps to its release group
                rg = row.get("release-group")
                item = None
                if isinstance(rg, dict):
                    artist, artist_mbid = _artist_of(row.get("artist-credit"))
                    item = _release_group_item({**rg, "title": rg.get("title") or row.get("title")}, artist, artist_mbid)
                if item is None:
                    title = str(row.get("title") or "").strip()
                    artist, artist_mbid = _artist_of(row.get("artist-credit"))
                    if title and artist:
                        item = ImportListItem(
                            kind="album",
                            external_key=name_key(artist, title),
                            artist_name=artist,
                            album_title=title,
                            artist_mbid=artist_mbid,
                        )
            if item is None or item.external_key in seen:
                continue
            seen.add(item.external_key)
            items.append(item)
        return items
    return []

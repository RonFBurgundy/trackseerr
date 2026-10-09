"""Last.fm provider: a user's loved tracks, top artists, top albums or top tracks."""

import os
from typing import Any, Optional

from trackseerr.clients.import_lists.base import (
    ImportListError,
    ImportListItem,
    clamp_limit,
    get_json,
    name_key,
    require_text,
)

API_URL = "https://ws.audioscrobbler.com/2.0/"
SOURCES = ("loved_tracks", "top_artists", "top_albums", "top_tracks")
PERIODS = ("overall", "7day", "1month", "3month", "6month", "12month")
PAGE_SIZE = 200

# source -> (API method, response root key, item key)
_METHODS = {
    "loved_tracks": ("user.getlovedtracks", "lovedtracks", "track"),
    "top_artists": ("user.gettopartists", "topartists", "artist"),
    "top_albums": ("user.gettopalbums", "topalbums", "album"),
    "top_tracks": ("user.gettoptracks", "toptracks", "track"),
}


def resolve_api_key(config: dict[str, Any]) -> str:
    key = str(config.get("api_key") or "").strip() or os.getenv("LASTFM_API_KEY", "").strip()
    if not key:
        raise ImportListError("A Last.fm API key is required (set it on the list or via LASTFM_API_KEY)")
    return key


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def _clean_mbid(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _to_item(source: str, raw: dict[str, Any]) -> Optional[ImportListItem]:
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    artist_raw = raw.get("artist")
    artist_name = ""
    artist_mbid: Optional[str] = None
    if isinstance(artist_raw, dict):
        artist_name = str(artist_raw.get("name") or artist_raw.get("#text") or "").strip()
        artist_mbid = _clean_mbid(artist_raw.get("mbid"))
    elif isinstance(artist_raw, str):
        artist_name = artist_raw.strip()
    if source == "top_artists":
        mbid = _clean_mbid(raw.get("mbid"))
        return ImportListItem(
            kind="artist", external_key=mbid or name_key(name), artist_name=name, mbid=mbid, artist_mbid=mbid
        )
    if not artist_name:
        return None
    if source == "top_albums":
        # Last.fm album ids are release ids, not release-group ids, so they are not kept as the album's mbid.
        return ImportListItem(
            kind="album",
            external_key=name_key(artist_name, name),
            artist_name=artist_name,
            album_title=name,
            artist_mbid=artist_mbid,
        )
    return ImportListItem(
        kind="track",
        external_key=name_key(artist_name, name),
        artist_name=artist_name,
        track_title=name,
        mbid=_clean_mbid(raw.get("mbid")),
        artist_mbid=artist_mbid,
    )


def fetch(config: dict[str, Any], *, mb_base_url: Optional[str] = None) -> list[ImportListItem]:
    """Pages through the chosen Last.fm list until ``limit`` items are collected or the pages run out."""
    username = require_text(config, "username", "Last.fm username")
    api_key = resolve_api_key(config)
    source = str(config.get("source") or "loved_tracks")
    if source not in SOURCES:
        raise ImportListError(f"source must be one of: {', '.join(SOURCES)}")
    period = str(config.get("period") or "overall")
    if period not in PERIODS:
        raise ImportListError(f"period must be one of: {', '.join(PERIODS)}")
    limit = clamp_limit(config.get("limit"), 50)
    method, root_key, item_key = _METHODS[source]

    items: list[ImportListItem] = []
    seen: set[str] = set()
    page = 1
    while len(items) < limit:
        params: dict[str, Any] = {
            "method": method,
            "user": username,
            "api_key": api_key,
            "format": "json",
            "limit": min(PAGE_SIZE, limit),
            "page": page,
        }
        if source != "loved_tracks":
            params["period"] = period
        data = get_json(API_URL, params=params, ok_statuses=(200,))
        if not isinstance(data, dict):
            raise ImportListError("Last.fm returned an unexpected response")
        if "error" in data:
            raise ImportListError(f"Last.fm error {data.get('error')}: {data.get('message') or 'request failed'}")
        root = data.get(root_key)
        if not isinstance(root, dict):
            raise ImportListError("Last.fm returned an unexpected response")
        for raw in _as_list(root.get(item_key)):
            item = _to_item(source, raw)
            if item is None or item.external_key in seen:
                continue
            seen.add(item.external_key)
            items.append(item)
            if len(items) >= limit:
                break
        attr = root.get("@attr") if isinstance(root.get("@attr"), dict) else {}
        try:
            total_pages = int(attr.get("totalPages") or 1)
        except (TypeError, ValueError):
            total_pages = 1
        if page >= total_pages or not _as_list(root.get(item_key)):
            break
        page += 1
    return items

"""ListenBrainz provider: a playlist, the playlists created for a user, or their top artists/release groups."""

import re
import urllib.parse
from typing import Any, Optional

from trackseerr.clients.import_lists.base import (
    ImportListError,
    ImportListItem,
    clamp_limit,
    get_json,
    name_key,
    require_text,
)

API_URL = "https://api.listenbrainz.org/1"
SOURCES = ("playlist", "created_for", "top_artists", "top_release_groups")
RANGES = (
    "this_week",
    "this_month",
    "this_year",
    "week",
    "month",
    "quarter",
    "half_yearly",
    "year",
    "all_time",
)
STATS_PAGE_SIZE = 100
PLAYLIST_PAGE_SIZE = 25
_MBID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_JSPF_TRACK_EXT = "https://musicbrainz.org/doc/jspf#track"
_JSPF_PLAYLIST_EXT = "https://musicbrainz.org/doc/jspf#playlist"


# MusicBrainz account names allow letters, digits, spaces and a few symbols; never path or URL syntax.
_USERNAME_RE = re.compile(r"^[\w.\- @+]{1,64}$")


def _valid_username(raw: str) -> str:
    username = raw.strip()
    if not _USERNAME_RE.fullmatch(username) or not username.strip("."):
        raise ImportListError("ListenBrainz username contains invalid characters")
    return username


def _headers(config: dict[str, Any]) -> dict[str, str]:
    token = str(config.get("token") or "").strip()
    return {"Authorization": f"Token {token}"} if token else {}


def _mbid_from(value: Any) -> Optional[str]:
    """The MBID inside an identifier URL (or a bare MBID), else None."""
    if isinstance(value, list):
        value = value[0] if value else None
    match = _MBID_RE.search(str(value or ""))
    return match.group(0).lower() if match else None


def _first_mbid(value: Any) -> Optional[str]:
    if isinstance(value, list):
        for v in value:
            mbid = _mbid_from(v)
            if mbid:
                return mbid
        return None
    return _mbid_from(value)


def _playlist_items(playlist_mbid: str, config: dict[str, Any]) -> list[ImportListItem]:
    data = get_json(f"{API_URL}/playlist/{playlist_mbid}", headers=_headers(config))
    playlist = data.get("playlist") if isinstance(data, dict) else None
    if not isinstance(playlist, dict):
        raise ImportListError("ListenBrainz returned an unexpected playlist response")
    items: list[ImportListItem] = []
    for raw in playlist.get("track") or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        artist = str(raw.get("creator") or "").strip()
        if not title or not artist:
            continue
        meta = (raw.get("extension") or {}).get(_JSPF_TRACK_EXT) or {}
        extra = meta.get("additional_metadata") if isinstance(meta, dict) else None
        artist_mbid: Optional[str] = None
        if isinstance(extra, dict):
            artists = extra.get("artists")
            if isinstance(artists, list) and artists and isinstance(artists[0], dict):
                artist_mbid = _mbid_from(artists[0].get("artist_mbid"))
        recording = _mbid_from(raw.get("identifier"))
        items.append(
            ImportListItem(
                kind="track",
                external_key=recording or name_key(artist, title),
                artist_name=artist,
                album_title=str(raw.get("album") or "").strip(),
                track_title=title,
                mbid=recording,
                artist_mbid=artist_mbid,
            )
        )
    return items


def _playlist_slug(inner: dict[str, Any]) -> str:
    """Stable kind of a created-for playlist (``weekly-jams``, ``weekly-exploration``, ...).

    ListenBrainz names them "Weekly Jams for user, week of 2025-01-06 Mon"; the generating patch is the reliable key
    and the title prefix before " for " the fallback.
    """
    ext = (inner.get("extension") or {}).get(_JSPF_PLAYLIST_EXT)
    meta = ext.get("additional_metadata") if isinstance(ext, dict) else None
    algo = meta.get("algorithm_metadata") if isinstance(meta, dict) else None
    patch = str(algo.get("source_patch") or "").strip().lower() if isinstance(algo, dict) else ""
    if patch:
        return patch
    title = str(inner.get("title") or "").strip()
    prefix = title.split(" for ", 1)[0].strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", prefix).strip("-")


def _list_playlist_entries(username: str, path: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Pages through a user's playlist listing: ``[{"mbid", "title", "date", "slug"}, ...]``, de-duplicated by mbid."""
    entries_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    while True:
        data = get_json(
            f"{API_URL}/user/{urllib.parse.quote(username, safe='')}/playlists{path}",
            params={"count": PLAYLIST_PAGE_SIZE, "offset": offset},
            headers=_headers(config),
        )
        if not isinstance(data, dict):
            raise ImportListError("ListenBrainz returned an unexpected response")
        entries = data.get("playlists") or []
        for entry in entries:
            inner = entry.get("playlist") if isinstance(entry, dict) else None
            if not isinstance(inner, dict):
                continue
            mbid = _mbid_from(inner.get("identifier"))
            if not mbid or mbid in seen:
                continue
            seen.add(mbid)
            entries_out.append(
                {
                    "mbid": mbid,
                    "title": str(inner.get("title") or "").strip(),
                    "date": str(inner.get("date") or "").strip(),
                    "slug": _playlist_slug(inner),
                }
            )
        total = int(data.get("playlist_count") or 0)
        offset += PLAYLIST_PAGE_SIZE
        if not entries or offset >= total:
            return entries_out


def list_created_for(username: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    """The playlists ListenBrainz generated for ``username`` (Weekly Jams, Weekly Exploration, ...), newest first."""
    entries = _list_playlist_entries(_valid_username(username), "/createdfor", config)
    return sorted(entries, key=lambda e: e["date"], reverse=True)


def list_user_playlists(username: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    """The playlists ``username`` created themselves, newest first."""
    entries = _list_playlist_entries(_valid_username(username), "", config)
    return sorted(entries, key=lambda e: e["date"], reverse=True)


def newest_created_for(username: str, slug: str, config: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The most recent created-for playlist of kind ``slug``, or None when ListenBrainz has none."""
    for entry in list_created_for(username, config):
        if entry["slug"] == slug:
            return entry
    return None


def playlist_items(playlist_mbid: str, config: dict[str, Any]) -> list[ImportListItem]:
    """The tracks of one ListenBrainz playlist (``config`` may carry a ``token`` for private playlists)."""
    mbid = _mbid_from(playlist_mbid)
    if not mbid:
        raise ImportListError("A playlist MBID (or playlist URL) is required")
    return _dedupe(_playlist_items(mbid, config))


def _created_for_playlist_mbids(username: str, config: dict[str, Any]) -> list[str]:
    return [entry["mbid"] for entry in _list_playlist_entries(username, "/createdfor", config)]


def _stats_items(source: str, username: str, config: dict[str, Any]) -> list[ImportListItem]:
    rng = str(config.get("range") or "all_time")
    if rng not in RANGES:
        raise ImportListError(f"range must be one of: {', '.join(RANGES)}")
    limit = clamp_limit(config.get("limit"), 100)
    path, list_key = ("artists", "artists") if source == "top_artists" else ("release-groups", "release_groups")
    items: list[ImportListItem] = []
    seen: set[str] = set()
    offset = 0
    while len(items) < limit:
        data = get_json(
            f"{API_URL}/stats/user/{urllib.parse.quote(username, safe='')}/{path}",
            params={"range": rng, "count": min(STATS_PAGE_SIZE, limit), "offset": offset},
            headers=_headers(config),
            ok_statuses=(200, 204),
        )
        if data is None:
            break  # 204: ListenBrainz has not computed stats for this user/range yet
        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            raise ImportListError("ListenBrainz returned an unexpected stats response")
        rows = payload.get(list_key) or []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            if source == "top_artists":
                name = str(raw.get("artist_name") or "").strip()
                mbid = _first_mbid(raw.get("artist_mbids") or raw.get("artist_mbid"))
                if not name:
                    continue
                item = ImportListItem(
                    kind="artist", external_key=mbid or name_key(name), artist_name=name, mbid=mbid, artist_mbid=mbid
                )
            else:
                title = str(raw.get("release_group_name") or "").strip()
                artist = str(raw.get("artist_name") or "").strip()
                mbid = _mbid_from(raw.get("release_group_mbid"))
                if not title or not artist:
                    continue
                item = ImportListItem(
                    kind="album",
                    external_key=mbid or name_key(artist, title),
                    artist_name=artist,
                    album_title=title,
                    mbid=mbid,
                    artist_mbid=_first_mbid(raw.get("artist_mbids")),
                )
            if item.external_key in seen:
                continue
            seen.add(item.external_key)
            items.append(item)
            if len(items) >= limit:
                break
        offset += STATS_PAGE_SIZE
        if not rows or len(rows) < min(STATS_PAGE_SIZE, limit):
            break
    return items


def fetch(config: dict[str, Any], *, mb_base_url: Optional[str] = None) -> list[ImportListItem]:
    source = str(config.get("source") or "")
    if source not in SOURCES:
        raise ImportListError(f"source must be one of: {', '.join(SOURCES)}")
    if source == "playlist":
        playlist_mbid = _mbid_from(config.get("playlist_mbid"))
        if not playlist_mbid:
            raise ImportListError("A playlist MBID (or playlist URL) is required")
        return _dedupe(_playlist_items(playlist_mbid, config))
    username = _valid_username(require_text(config, "username", "ListenBrainz username"))
    if source == "created_for":
        items: list[ImportListItem] = []
        for mbid in _created_for_playlist_mbids(username, config):
            items.extend(_playlist_items(mbid, config))
        return _dedupe(items)
    return _stats_items(source, username, config)


def _dedupe(items: list[ImportListItem]) -> list[ImportListItem]:
    seen: set[str] = set()
    out: list[ImportListItem] = []
    for it in items:
        if it.external_key in seen:
            continue
        seen.add(it.external_key)
        out.append(it)
    return out

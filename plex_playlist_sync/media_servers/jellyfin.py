"""Jellyfin adapter (REST API, API-key authentication).

Protocol notes
  * Every call carries ``Authorization: MediaBrowser Token="<api key>"``. The key lives in a header only: it is never
    put in a URL, never logged and never part of an exception message. Failures carry the HTTP status or the exception
    type name, never a URL.
  * Playlists are created private (``IsPublic=false``; Jellyfin's default is public) so each target user sees only
    their own copy. An API key has no user context, so every playlist call names the account explicitly (``UserId``). Jellyfin has
    real users, so ``capabilities.users`` is True: a playlist is created per target user, and Trackseerr targets are
    mapped to Jellyfin accounts by name (case-insensitive) or id. An empty target list means the default account:
    ``JELLYFIN_USER`` when set, else the first administrator.
  * Track search uses ``/Items?SearchTerm=`` (Jellyfin matches it against the item name only), so queries use the
    noise-free title and candidates are ranked with the same scorer as the Subsonic adapter, including the +-3 s
    duration check (``RunTimeTicks`` is 100 ns ticks).

Playlist ordering: a sync keeps the playlist id (and with it sharing / cover art). ``/Playlists/{id}/Items/{entry}/Move``
exists but needs a user session: with an API key Jellyfin answers 400 (found against a real 12.1 server). So the order is
expressed with remove-by-``EntryIds`` and append, like the Subsonic adapter: the longest prefix of the wanted order that
already sits in the playlist (as a subsequence) is untouched, the rest is removed and appended. Removal runs before the
append; if the connection dies in between, the next sync completes it. Re-syncing an unchanged playlist costs one read.

Resilience: reads retry 429/502/503/504 and transport failures with exponential backoff (honouring Retry-After).
Writes retry only on 429 (the server rejected them unprocessed); a request the server may already have applied is
never sent twice.
"""

import logging
import time
from typing import Any, Callable, Iterator, Optional, Sequence

import httpx

from plex_playlist_sync.lidarr_release import norm_title
from plex_playlist_sync.media_servers.base import (
    ConnectionTest,
    MediaServer,
    MediaServerAuthError,
    MediaServerConnectionError,
    MediaServerError,
    MediaServerNotFound,
    PlaylistSyncOptions,
    ServerCapabilities,
    ServerTrackRef,
    ServerUser,
)
from plex_playlist_sync.media_servers.subsonic import (
    _artist_variants,
    _chunks,
    _RedactHttpxUrls,
    _Song,
    score_candidate,
)
from plex_playlist_sync.missing_csv import delete_missing_csv, write_missing_csv
from plex_playlist_sync.models import Playlist, SyncResult, Track
from plex_playlist_sync.redaction import redact_text
from plex_playlist_sync.storage import clean_library_name

logger = logging.getLogger(__name__)

_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(f, _RedactHttpxUrls) for f in _httpx_logger.filters):
    _httpx_logger.addFilter(_RedactHttpxUrls())

JELLYFIN_CAPABILITIES = ServerCapabilities(playlists=True, users=True, library_refresh=True, mixes=False, search=True)

CLIENT_NAME = "Trackseerr"
_TICKS_PER_SECOND = 10_000_000
_PAGE = 200  # list pages (playlists, playlist entries)
_SEARCH_PAGE = 50
_SEARCH_MAX = 150  # candidates inspected per query before giving up
_ID_CHUNK = 50  # item / entry ids per request: keeps query strings far below common 8 KB proxy limits
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 10.0
_RETRY_STATUS = frozenset({429, 502, 503, 504})


def _song_from_item(item: dict[str, Any]) -> _Song:
    names: list[str] = [str(a) for a in item.get("Artists") or [] if a]
    names.append(str(item.get("AlbumArtist") or ""))
    for entry in list(item.get("ArtistItems") or []) + list(item.get("AlbumArtists") or []):
        if isinstance(entry, dict) and entry.get("Name"):
            names.append(str(entry["Name"]))
    ticks = item.get("RunTimeTicks")
    return _Song(
        id=str(item.get("Id") or ""),
        title=str(item.get("Name") or ""),
        artist=str((item.get("Artists") or [item.get("AlbumArtist") or ""])[0] or ""),
        album=str(item.get("Album") or ""),
        duration=ticks / _TICKS_PER_SECOND if isinstance(ticks, (int, float)) and ticks > 0 else None,
        artists=tuple(dict.fromkeys(n for n in names if n)),
        raw=item,
    )


def plan_entry_changes(current: Sequence[tuple[str, str]], desired: Sequence[str]) -> tuple[list[str], list[str]]:
    """``(entry_ids_to_remove, item_ids_to_append)`` turning the playlist into exactly ``desired``.

    ``current`` is ``(entry_id, item_id)`` in playlist order. The longest greedy prefix of ``desired`` that is a
    subsequence of ``current`` stays untouched; every other entry is removed and the rest of ``desired`` appended, so
    the result is in source order without ever recreating the playlist.
    """
    items = [item_id for _, item_id in current]
    keep: set[int] = set()
    matched = 0
    pos = 0
    while matched < len(desired):
        try:
            idx = items.index(desired[matched], pos)
        except ValueError:
            break
        keep.add(idx)
        pos = idx + 1
        matched += 1
    remove = [entry_id for i, (entry_id, _) in enumerate(current) if i not in keep]
    return remove, list(desired[matched:])


class JellyfinMediaServer(MediaServer):
    kind = "jellyfin"

    def __init__(
        self,
        url: str,
        api_key: str,
        user: str = "",
        *,
        verify_ssl: bool = True,
        timeout: float = 20.0,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        base = (url or "").strip().rstrip("/")
        if not base:
            raise MediaServerError("Jellyfin URL is not set", safe_detail="Jellyfin URL is not set")
        if not (api_key or "").strip():
            raise MediaServerAuthError("Jellyfin needs an API key", safe_detail="Jellyfin needs an API key")
        self._base = base
        self._user = (user or "").strip()
        headers = {
            "Authorization": f'MediaBrowser Client="{CLIENT_NAME}", Device="{CLIENT_NAME}", '
            f'DeviceId="{CLIENT_NAME.lower()}", Version="1.0", Token="{api_key.strip()}"',
            "Accept": "application/json",
        }
        self._http = httpx.Client(
            verify=verify_ssl, timeout=timeout, transport=transport, follow_redirects=False, headers=headers
        )
        self._sleep = sleep

    @property
    def capabilities(self) -> ServerCapabilities:
        return JELLYFIN_CAPABILITIES

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ transport

    @staticmethod
    def _backoff(attempt: int, response: Optional[httpx.Response]) -> float:
        delay = _BACKOFF_BASE * (2**attempt)
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                delay = max(delay, float(retry_after))
        return min(delay, _BACKOFF_CAP)

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Sequence[tuple[str, Any]]] = None,
        json: Optional[Any] = None,
    ) -> Any:
        """One Jellyfin call; returns the decoded JSON body (``None`` for an empty one), raises the generic hierarchy."""
        url = f"{self._base}{path}"
        query = [(k, str(v)) for k, v in params or ()]
        read_only = method == "GET"
        # A write is retried only on 429 (rejected before processing). 5xx or a transport failure may mean the server
        # already applied it, so replaying could double-apply a playlist edit.
        retry_status = _RETRY_STATUS if read_only else frozenset({429})
        response: Optional[httpx.Response] = None
        for attempt in range(_MAX_ATTEMPTS):
            last = attempt == _MAX_ATTEMPTS - 1
            try:
                response = self._http.request(method, url, params=query, json=json)
            except httpx.HTTPError as exc:
                if last or not read_only:
                    detail = f"Jellyfin server unreachable ({type(exc).__name__})"
                    raise MediaServerConnectionError(detail, safe_detail=detail) from exc
                logger.debug("Jellyfin %s %s transport failure (%s); retrying", method, path, type(exc).__name__)
                self._sleep(self._backoff(attempt, None))
                continue
            if response.status_code in retry_status and not last:
                logger.warning("Jellyfin %s answered HTTP %s; retrying", path, response.status_code)
                self._sleep(self._backoff(attempt, response))
                continue
            break
        assert response is not None  # the loop always sets it or raises
        return self._decode(path, response)

    @staticmethod
    def _decode(path: str, response: httpx.Response) -> Any:
        status = response.status_code
        if status == 401:
            raise MediaServerAuthError("Jellyfin rejected the API key", safe_detail="Jellyfin rejected the API key (HTTP 401)")
        if status == 403:
            detail = "Jellyfin refused the request (HTTP 403): the API key lacks permission"
            raise MediaServerAuthError(detail, safe_detail=detail)
        if status == 404:
            detail = f"Jellyfin {path.split('/')[1] if path.count('/') else path} not found (HTTP 404)"
            raise MediaServerNotFound(detail, safe_detail=detail)
        if status == 429 or status >= 500:
            detail = f"Jellyfin server error (HTTP {status})"
            raise MediaServerConnectionError(detail, safe_detail=detail)
        if status >= 400:
            message = redact_text(response.text[:200]).strip() if response.headers.get("content-type", "").startswith("text") else ""
            detail = f"Jellyfin request failed (HTTP {status})" + (f": {message}" if message else "")
            raise MediaServerError(detail, safe_detail=detail)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            detail = "Jellyfin returned an unexpected response (is the URL a Jellyfin server?)"
            raise MediaServerError(detail, safe_detail=detail) from exc

    def _paged(self, path: str, params: Sequence[tuple[str, Any]], page: int = _PAGE) -> Iterator[dict[str, Any]]:
        """Every item of a list endpoint, following ``StartIndex`` / ``Limit`` until ``TotalRecordCount`` is reached."""
        start = 0
        while True:
            body = self._request("GET", path, [*params, ("StartIndex", start), ("Limit", page)])
            items = [i for i in (body or {}).get("Items") or [] if isinstance(i, dict)]
            yield from items
            total = (body or {}).get("TotalRecordCount")
            start += len(items)  # a server may cap Limit below what was asked: continue from what actually arrived
            if not items or (start >= total if isinstance(total, int) else len(items) < page):
                return

    # ------------------------------------------------------------------ MediaServer

    def test_connection(self) -> ConnectionTest:
        try:
            info = self._request("GET", "/System/Info")
        except MediaServerError as exc:
            return ConnectionTest(False, exc.safe_detail)
        if not isinstance(info, dict):
            return ConnectionTest(False, "Jellyfin returned an unexpected response (is the URL a Jellyfin server?)")
        name = info.get("ServerName") or "Jellyfin"
        return ConnectionTest(True, f"Connected to {name} {info.get('Version') or ''}".strip())

    def _search_songs(self, query: str, limit: int) -> Iterator[_Song]:
        """Songs whose name contains ``query``, newest page last; stops after ``limit`` candidates."""
        if not query.strip():
            return
        start = 0
        while start < limit:
            page = min(_SEARCH_PAGE, limit - start)
            body = self._request(
                "GET",
                "/Items",
                [
                    ("IncludeItemTypes", "Audio"),
                    ("Recursive", "true"),
                    ("SearchTerm", query),
                    ("StartIndex", start),
                    ("Limit", page),
                ],
            )
            items = [i for i in (body or {}).get("Items") or [] if isinstance(i, dict) and i.get("Id")]
            for item in items:
                yield _song_from_item(item)
            total = (body or {}).get("TotalRecordCount")
            start += len(items)  # a server may cap Limit below what was asked: continue from what actually arrived
            if not items or (start >= total if isinstance(total, int) else len(items) < page):
                return

    def _get_song(self, item_id: str) -> Optional[_Song]:
        body = self._request("GET", "/Items", [("Ids", item_id), ("IncludeItemTypes", "Audio"), ("Recursive", "true")])
        items = [i for i in (body or {}).get("Items") or [] if isinstance(i, dict) and i.get("Id")]
        return _song_from_item(items[0]) if items else None

    def _best(self, track: Track, threshold: float, db: Optional[Any]) -> Optional[_Song]:
        if db is not None:
            try:
                override = db.get_match_override(track.title, track.artist)
            except (AttributeError, TypeError, ValueError) as exc:
                logger.debug("Match override lookup failed for '%s': %s", track.title, type(exc).__name__)
                override = None
            if override and override.get("plex_rating_key"):  # the column holds the server's own item id for every server
                try:
                    pinned = self._get_song(str(override["plex_rating_key"]))
                except MediaServerNotFound:
                    pinned = None
                if pinned is not None:
                    return pinned
        # SearchTerm matches the item name, so the query is the noise-free title; the first credited artist and the
        # album only take part in scoring.
        title = norm_title(track.title) or clean_library_name(track.title)
        queries = [title, clean_library_name(track.title)]
        best: Optional[_Song] = None
        best_score = 0.0
        for query in dict.fromkeys(q for q in queries if q):
            for song in self._search_songs(query, _SEARCH_MAX):
                score = score_candidate(track, song)
                if score > best_score:
                    best, best_score = song, score
            if best is not None and best_score >= threshold:
                break
        if best is not None and best_score + 1e-9 >= threshold:
            return best
        return None

    def match_track(self, track: Track, threshold: float = 0.9, db: Optional[Any] = None) -> Optional[ServerTrackRef]:
        song = self._best(track, threshold, db)
        return None if song is None else song.ref()

    def match_playlist_tracks(
        self, tracks: Sequence[Track], threshold: float = 0.9, db: Optional[Any] = None
    ) -> tuple[list[ServerTrackRef], list[Track]]:
        matched: list[ServerTrackRef] = []
        missing: list[Track] = []
        for track in tracks:
            ref = self.match_track(track, threshold, db)
            if ref is None:
                missing.append(track)
            else:
                matched.append(ref)
        return matched, missing

    def search_tracks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, limit)
        terms = " ".join(query.split())
        songs: list[_Song] = []
        for song in self._search_songs(terms, limit):
            songs.append(song)
            if len(songs) >= limit:
                break
        return [
            {
                "id": s.id,
                "rating_key": s.id,
                "title": s.title or "Unknown",
                "artist": s.artist or "Unknown Artist",
                "album": s.album,
                "duration": s.duration,
            }
            for s in songs
        ]

    def refresh_library(self) -> bool:
        self._request("POST", "/Library/Refresh")
        return True

    def list_users(self) -> list[ServerUser]:
        body = self._request("GET", "/Users")
        users = body if isinstance(body, list) else []
        return [
            ServerUser(
                id=str(u["Id"]),
                name=str(u.get("Name") or ""),
                is_admin=bool((u.get("Policy") or {}).get("IsAdministrator", False)),
                extra={"email": ""},
            )
            for u in users
            if isinstance(u, dict) and u.get("Id") and u.get("Name")
        ]

    # ------------------------------------------------------------------ playlists

    def _resolve_user(self, name: str, users: list[ServerUser]) -> Optional[ServerUser]:
        """The account for a Trackseerr target (``""`` = default account), or None when there is no such account."""
        wanted = (name or self._user).strip().lower()
        if wanted:
            return next((u for u in users if u.name.lower() == wanted or u.id.lower() == wanted), None)
        return next((u for u in users if u.is_admin), None)

    def _find_playlist(self, user_id: str, name: str) -> Optional[str]:
        found = [
            str(p["Id"])
            for p in self._paged(
                "/Items",
                [("IncludeItemTypes", "Playlist"), ("Recursive", "true"), ("MediaTypes", "Audio"), ("UserId", user_id)],
            )
            if p.get("Id") and p.get("Name") == name
        ]
        if len(found) > 1:
            logger.warning("Jellyfin has %d playlists named '%s'; updating the first", len(found), name)
        return found[0] if found else None

    def _entries(self, playlist_id: str, user_id: str) -> list[tuple[str, str]]:
        return [
            (str(e["PlaylistItemId"]), str(e["Id"]))
            for e in self._paged(f"/Playlists/{playlist_id}/Items", [("UserId", user_id)])
            if e.get("PlaylistItemId") and e.get("Id")
        ]

    def _add(self, playlist_id: str, user_id: str, item_ids: Sequence[str]) -> None:
        for chunk in _chunks(list(item_ids), _ID_CHUNK):
            self._request("POST", f"/Playlists/{playlist_id}/Items", [("Ids", ",".join(chunk)), ("UserId", user_id)])

    def _push(self, playlist: Playlist, user: ServerUser, ids: list[str], options: PlaylistSyncOptions) -> None:
        playlist_id = self._find_playlist(user.id, playlist.name)
        if playlist_id is None:
            created = self._request(
                "POST",
                "/Playlists",
                # IsPublic defaults to true when omitted, which would show the playlist to every Jellyfin user.
                json={"Name": playlist.name, "Ids": ids, "UserId": user.id, "MediaType": "Audio", "IsPublic": False},
            )
            if not isinstance(created, dict) or not created.get("Id"):
                raise MediaServerError(
                    "Jellyfin did not report the created playlist", safe_detail="Jellyfin did not report the created playlist"
                )
            return
        current = self._entries(playlist_id, user.id)
        if options.append:
            have = {item for _, item in current}
            self._add(playlist_id, user.id, [i for i in dict.fromkeys(ids) if i not in have])
            return
        remove, add = plan_entry_changes(current, ids)
        for chunk in _chunks(remove, _ID_CHUNK):
            self._request("DELETE", f"/Playlists/{playlist_id}/Items", [("EntryIds", ",".join(chunk))])
        if add:
            self._add(playlist_id, user.id, add)

    def sync_playlist(
        self, playlist: Playlist, targets: Sequence[str], options: PlaylistSyncOptions
    ) -> list[SyncResult]:
        names = list(targets) or [""]
        matched, missing = self.match_playlist_tracks(playlist.tracks, threshold=options.threshold, db=options.db)
        total = len(playlist.tracks)

        if options.write_missing_as_csv:
            if missing:
                write_missing_csv(missing, playlist.name, data_dir=options.data_dir)
            elif matched:
                delete_missing_csv(playlist.name, data_dir=options.data_dir)

        users = self.list_users()
        results: list[SyncResult] = []
        done: dict[str, SyncResult] = {}
        # Jellyfin drops an item that is added to a playlist more than once in an update (it only accepts repeats at
        # creation), so a track listed twice in the source appears once: first occurrence wins, keeping syncs idempotent.
        ids = list(dict.fromkeys(m.id for m in matched))
        for name in names:
            user = self._resolve_user(name, users)
            if user is None:
                who = f"'{name}'" if name else "the default account (set JELLYFIN_USER)"
                results.append(SyncResult(playlist.name, total, len(matched), len(missing), False, f"Jellyfin user {who} not found"))
                continue
            if user.id in done:
                results.append(done[user.id])
                continue
            if not matched:
                logger.warning("No tracks in playlist '%s' could be matched on the Jellyfin server", playlist.name)
                outcome = SyncResult(playlist.name, total, 0, len(missing), False, "Zero tracks matched on the Jellyfin server")
            else:
                self._push(playlist, user, ids, options)
                outcome = SyncResult(playlist.name, total, len(matched), len(missing), True)
            done[user.id] = outcome
            results.append(outcome)
        return results

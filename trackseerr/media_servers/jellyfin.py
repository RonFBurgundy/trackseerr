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

Ownership: the playlist list for a user (``/Items?IncludeItemTypes=Playlist&UserId=``) also contains other accounts'
PUBLIC playlists, and Jellyfin lets an API key remove entries from any of them. Before touching a same-named playlist
the adapter therefore asks the server whether the target may edit it, with an empty ``POST /Playlists/{id}/Items?UserId=``
(204 = may edit, 403 = visible but not theirs, 404 = not visible). ``GET /Playlists/{id}`` cannot be used: with an API key
it answers 400 (it needs a user session). A playlist the target does not own is never modified; the target gets its own
(a playlist another account explicitly shared with edit rights counts as editable).

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
import sqlite3
import time
import weakref
from datetime import datetime, timezone
from itertools import islice
from typing import Any, Callable, Iterator, Optional, Sequence

import httpx

from trackseerr.censored import censored_pattern, is_censored, search_text
from trackseerr.lidarr_release import norm_title
from trackseerr.mb_metadata_store import get_shared_enricher
from trackseerr.media_match import artist_matches
from trackseerr.media_servers.base import (
    ConnectionTest,
    MediaServer,
    MediaServerAuthError,
    MediaServerConnectionError,
    MediaServerError,
    MediaServerNotFound,
    MediaServerUnsupported,
    PlaylistSyncOptions,
    ServerCapabilities,
    ServerFileRef,
    ServerTrackRef,
    ServerUser,
)
from trackseerr.media_servers.song_scoring import (
    artist_query_credits,
    best_scored_match,
    clean_title,
    tail_two_components,
)
from trackseerr.media_servers.subsonic import (
    _parse_iso_utc,
    _chunks,
    _RedactHttpxUrls,
    _Song,
)
from trackseerr.missing_csv import delete_missing_csv, write_missing_csv
from trackseerr.models import Playlist, SyncResult, Track
from trackseerr.playlist_identity import (
    LibraryHit,
    MusicBrainzBudget,
    find_by_isrc,
    resolve_with_musicbrainz,
)
from trackseerr.redaction import redact_text
from trackseerr.storage import clean_library_name

logger = logging.getLogger(__name__)

_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(f, _RedactHttpxUrls) for f in _httpx_logger.filters):
    _httpx_logger.addFilter(_RedactHttpxUrls())

JELLYFIN_CAPABILITIES = ServerCapabilities(playlists=True, users=True, library_refresh=True, mixes=False, search=True, file_paths=True, folder_refresh=True)

CLIENT_NAME = "Trackseerr"
_TICKS_PER_SECOND = 10_000_000
_PAGE = 200  # list pages (playlists, playlist entries)
_MAX_PAGES = 1000  # hard stop for a listing (200k rows at the default page size)
_SEARCH_PAGE = 50
_FILE_PAGE = 500  # file listing pages
_SEARCH_MAX = 150  # candidates inspected per query before giving up
_ID_CHUNK = 50  # item / entry ids per request: keeps query strings far below common 8 KB proxy limits
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 10.0
_RETRY_STATUS = frozenset({429, 502, 503, 504})


def _page_key(item: dict[str, Any]) -> str:
    """Identity of a list row: the playlist entry id when there is one (a track may repeat), else the item id."""
    return str(item.get("PlaylistItemId") or item.get("Id") or repr(sorted(item.items(), key=lambda kv: kv[0]))[:200])


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
        # A replaced adapter is simply dropped by the builder: its pooled client is closed once the last in-flight
        # user lets go of it, never while a sync still holds it (the callback holds the client, not the adapter).
        self._finalizer = weakref.finalize(self, self._http.close)
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
        """Every item of a list endpoint, following ``StartIndex`` / ``Limit`` until ``TotalRecordCount`` is reached.

        Never loops forever: it stops after ``_MAX_PAGES`` requests and as soon as a page brings nothing new (a server
        that ignores ``StartIndex`` would otherwise serve the same page endlessly).
        """
        start = 0
        seen: set[str] = set()
        for _ in range(_MAX_PAGES):
            body = self._request("GET", path, [*params, ("StartIndex", start), ("Limit", page)])
            items = [i for i in (body or {}).get("Items") or [] if isinstance(i, dict)]
            fresh = [i for i in items if _page_key(i) not in seen]
            if items and not fresh:
                logger.warning("Jellyfin repeated a page of %s; stopping the listing", path)
                return
            seen.update(_page_key(i) for i in fresh)
            yield from fresh
            total = (body or {}).get("TotalRecordCount")
            start += len(items)  # a server may cap Limit below what was asked: continue from what actually arrived
            if not items or (start >= total if isinstance(total, int) else len(items) < page):
                return
        logger.warning("Jellyfin listing of %s exceeded %d pages; stopping", path, _MAX_PAGES)

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

    def _search_songs(self, query: str, limit: int, *, artist_ids: Sequence[str] = ()) -> Iterator[_Song]:
        """Songs whose name contains ``query`` (and/or belonging to ``artist_ids``); stops after ``limit`` candidates."""
        if not query.strip() and not artist_ids:
            return
        base_params: list[tuple[str, Any]] = [
            ("IncludeItemTypes", "Audio"),
            ("Recursive", "true"),
            ("Fields", "Path"),
        ]
        if query.strip():
            base_params.append(("SearchTerm", query.strip()))
        if artist_ids:
            base_params.append(("ArtistIds", ",".join(artist_ids)))
        start = 0
        while start < limit:
            page = min(_SEARCH_PAGE, limit - start)
            body = self._request("GET", "/Items", [*base_params, ("StartIndex", start), ("Limit", page)])
            items = [i for i in (body or {}).get("Items") or [] if isinstance(i, dict) and i.get("Id")]
            for item in items:
                yield _song_from_item(item)
            total = (body or {}).get("TotalRecordCount")
            start += len(items)  # a server may cap Limit below what was asked: continue from what actually arrived
            if not items or (start >= total if isinstance(total, int) else len(items) < page):
                return

    def _search_artists(self, query: str, limit: int = 10) -> list[tuple[str, str]]:
        """Return ``(id, name)`` pairs for Jellyfin MusicArtist items matching ``query``."""
        if not query.strip():
            return []
        body = self._request(
            "GET",
            "/Items",
            [
                ("IncludeItemTypes", "MusicArtist"),
                ("Recursive", "true"),
                ("SearchTerm", query.strip()),
                ("Limit", limit),
            ],
        )
        items = [i for i in (body or {}).get("Items") or [] if isinstance(i, dict) and i.get("Id")]
        return [(str(i["Id"]), str(i.get("Name") or "")) for i in items]

    def _resolve_artist_ids(self, artist_query: str) -> list[str]:
        """Resolve ``artist_query`` (possibly multi-artist) to matching Jellyfin MusicArtist IDs."""
        ids: list[str] = []
        for credit in artist_query_credits(artist_query):
            for aid, name in self._search_artists(credit):
                if artist_matches(name, artist_query, threshold=0.8) or artist_matches(name, credit, threshold=0.8):
                    if aid not in ids:
                        ids.append(aid)
            if len(ids) >= 5:
                break
        return ids[:5]

    def _get_song(self, item_id: str) -> Optional[_Song]:
        body = self._request(
            "GET",
            "/Items",
            [("Ids", item_id), ("IncludeItemTypes", "Audio"), ("Recursive", "true"), ("Fields", "Path")],
        )
        items = [i for i in (body or {}).get("Items") or [] if isinstance(i, dict) and i.get("Id")]
        return _song_from_item(items[0]) if items else None

    def _match_override(self, track: Track, db: Optional[Any]) -> Optional[_Song]:
        if db is None:
            return None
        try:
            override = db.get_match_override(track.title, track.artist)
        except (AttributeError, TypeError, ValueError) as exc:
            logger.debug("Match override lookup failed for '%s': %s", track.title, type(exc).__name__)
            return None
        if not (override and override.get("plex_rating_key")):  # the column holds the server's own item id
            return None
        try:
            return self._get_song(str(override["plex_rating_key"]))
        except MediaServerError as exc:  # stale or unreachable pin: fall back to a normal search, never abort the sync
            logger.warning(
                "Pinned match for '%s' could not be looked up (%s); searching instead", track.title, type(exc).__name__
            )
            return None

    def _song_for_hit(self, hit: LibraryHit) -> Optional[_Song]:
        target_tail = tail_two_components(hit.file_path)
        if target_tail is None or not hit.title:
            return None
        for song in self._search_songs(hit.title, _SEARCH_MAX):
            song_path = str(song.raw.get("Path") or "")
            if song_path and tail_two_components(song_path) == target_tail:
                return song
        if hit.artist:
            artist_ids = self._resolve_artist_ids(hit.artist)
            if artist_ids:
                for q in (hit.title, ""):
                    for song in self._search_songs(q, _SEARCH_MAX, artist_ids=artist_ids):
                        song_path = str(song.raw.get("Path") or "")
                        if song_path and tail_two_components(song_path) == target_tail:
                            return song
        return None

    def _match_by_isrc(self, track: Track, db: Optional[Any]) -> Optional[_Song]:
        if db is None or not track.isrc or not hasattr(db, "find_library_track_by_isrc"):
            return None
        try:
            hit = find_by_isrc(db, track.isrc)
        except (sqlite3.Error, AttributeError) as exc:
            logger.debug("ISRC lookup failed for '%s': %s", track.title, type(exc).__name__)
            return None
        if hit is None:
            return None
        return self._song_for_hit(hit)

    def _search_and_pick(
        self,
        track: Track,
        query: str,
        threshold: float,
        *,
        artist_ids: Sequence[str] = (),
    ) -> Optional[_Song]:
        songs = list(self._search_songs(query, _SEARCH_MAX, artist_ids=artist_ids))
        best, best_score, _ = best_scored_match(track, songs, threshold)
        return best if best is not None and best_score + 1e-9 >= threshold else None

    def _search_fuzzy(self, track: Track, threshold: float) -> Optional[_Song]:
        # SearchTerm matches the item name, so the query is the noise-free title; artist/album participate in scoring.
        title = norm_title(track.title.replace("$", "s")) or (
            "" if is_censored(track.title) else clean_library_name(track.title)
        )
        cleaned_raw = clean_title(track.title)
        cleaned = norm_title(cleaned_raw.replace("$", "s")) or (
            "" if is_censored(track.title) else clean_library_name(cleaned_raw)
        )
        queries = [title, cleaned]
        if not is_censored(track.title):
            queries.append(clean_library_name(track.title))
        for query in dict.fromkeys(q for q in queries if q):
            match = self._search_and_pick(track, query, threshold)
            if match is not None:
                return match
        artist_ids = self._resolve_artist_ids(track.artist)
        if artist_ids:
            for query in dict.fromkeys([cleaned or title, ""]):
                match = self._search_and_pick(track, query, threshold, artist_ids=artist_ids)
                if match is not None:
                    return match
        return None

    def _match_censored(self, track: Track, threshold: float) -> Optional[_Song]:
        pattern = censored_pattern(track.title)
        query = search_text(track.title)
        if pattern is None or not query:
            return None
        kept = [s for s in self._search_songs(query, _SEARCH_MAX) if pattern.fullmatch(clean_library_name(s.title))]
        best, best_score, _ = best_scored_match(track, kept, threshold, censored=True)
        return best if best is not None and best_score + 1e-9 >= threshold else None

    def _match_by_musicbrainz(
        self,
        track: Track,
        db: Optional[Any],
        enricher: Optional[Any],
        budget: Optional[MusicBrainzBudget],
    ) -> Optional[_Song]:
        if db is None or enricher is None or budget is None:
            return None
        hit = resolve_with_musicbrainz(db, enricher, track, budget)
        if hit is None:
            return None
        return self._song_for_hit(hit)

    def _best(
        self,
        track: Track,
        threshold: float,
        db: Optional[Any],
        enricher: Optional[Any] = None,
        budget: Optional[MusicBrainzBudget] = None,
    ) -> Optional[_Song]:
        override = self._match_override(track, db)
        if override is not None:
            return override
        isrc_match = self._match_by_isrc(track, db)
        if isrc_match is not None:
            return isrc_match
        fuzzy = self._search_fuzzy(track, threshold)
        if fuzzy is not None:
            return fuzzy
        censored = self._match_censored(track, threshold)
        if censored is not None:
            return censored
        return self._match_by_musicbrainz(track, db, enricher, budget)

    def match_track(
        self,
        track: Track,
        threshold: float = 0.9,
        db: Optional[Any] = None,
        enricher: Optional[Any] = None,
        budget: Optional[MusicBrainzBudget] = None,
    ) -> Optional[ServerTrackRef]:
        song = self._best(track, threshold, db, enricher=enricher, budget=budget)
        return None if song is None else song.ref()

    def match_playlist_tracks(
        self,
        tracks: Sequence[Track],
        threshold: float = 0.9,
        db: Optional[Any] = None,
        enricher: Optional[Any] = None,
        budget: Optional[MusicBrainzBudget] = None,
    ) -> tuple[list[ServerTrackRef], list[Track]]:
        if enricher is not None and budget is None:
            budget = MusicBrainzBudget()
        matched: list[ServerTrackRef] = []
        missing: list[Track] = []
        for track in tracks:
            ref = self.match_track(track, threshold, db, enricher=enricher, budget=budget)
            if ref is None:
                missing.append(track)
            else:
                matched.append(ref)
        return matched, missing

    def _search_tracks_by_artist(self, terms: str, limit: int) -> list[_Song]:
        words = terms.split()
        for i in range(1, min(len(words), 5)):
            artist_prefix = " ".join(words[:i])
            title_words = [w for w in clean_library_name(" ".join(words[i:])).split() if w]
            if not title_words:
                continue
            artist_ids = [aid for aid, _ in self._search_artists(artist_prefix, limit=5)]
            if not artist_ids:
                continue
            matched: list[_Song] = []
            for song in self._search_songs("", max(limit, _SEARCH_MAX), artist_ids=artist_ids):
                normed_title = clean_library_name(song.title)
                if all(w in normed_title for w in title_words):
                    matched.append(song)
                    if len(matched) >= limit:
                        return matched
            if matched:
                return matched
        artist_ids = [aid for aid, _ in self._search_artists(terms, limit=5)]
        if artist_ids:
            return list(islice(self._search_songs("", limit, artist_ids=artist_ids), limit))
        return []

    def search_tracks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, limit)
        terms = " ".join(query.split())
        if not terms:
            return []
        songs = list(islice(self._search_songs(terms, limit), limit))
        if not songs:
            songs = self._search_tracks_by_artist(terms, limit)
        return [
            {
                "id": s.id,
                "rating_key": s.id,
                "title": s.title or "Unknown",
                "artist": s.artist or "Unknown Artist",
                "album": s.album,
                "duration": s.duration,
            }
            for s in songs[:limit]
        ]

    def iter_library_files(self) -> Iterator[ServerFileRef]:
        """All Audio items with their ``Path``. Needs an administrator API key: a user key returns items without
        ``Path``, which raises :class:`MediaServerUnsupported` on the first page."""
        params = [
            ("IncludeItemTypes", "Audio"),
            ("Recursive", "true"),
            ("Fields", "Path,MediaSources"),
        ]
        first = True
        for item in self._paged("/Items", params, page=_FILE_PAGE):
            path = str(item.get("Path") or "")
            if first:
                first = False
                if not path:
                    detail = "Jellyfin API key must belong to an administrator to read file paths"
                    raise MediaServerUnsupported(detail, safe_detail=detail)
            if not path:
                continue
            container = str(item.get("Container") or "")
            if not container:
                sources = item.get("MediaSources") or []
                if sources and isinstance(sources[0], dict):
                    container = str(sources[0].get("Container") or "")
            artists = item.get("Artists") or []
            yield ServerFileRef(
                server_id=str(item.get("Id") or ""),
                path=path,
                title=str(item.get("Name") or ""),
                artist=str(artists[0] if artists else item.get("AlbumArtist") or ""),
                album=str(item.get("Album") or ""),
                container=container.lower(),
            )

    def last_scan_at(self) -> Optional[datetime]:
        tasks = self._request("GET", "/ScheduledTasks")
        for task in tasks if isinstance(tasks, list) else []:
            if not isinstance(task, dict) or task.get("Name") != "Scan Media Library":
                continue
            result = task.get("LastExecutionResult")
            raw = str(result.get("EndTimeUtc") or "") if isinstance(result, dict) else ""
            if not raw:
                return None
            return _parse_iso_utc(raw)
        return None

    def library_roots(self) -> list[str]:
        folders = self._request("GET", "/Library/VirtualFolders")
        roots: list[str] = []
        for folder in folders if isinstance(folders, list) else []:
            if not isinstance(folder, dict):
                continue
            kind = str(folder.get("CollectionType") or "").lower()
            if kind and kind != "music":
                continue
            roots.extend(str(loc) for loc in folder.get("Locations") or [] if loc)
        return roots

    def refresh_library(self) -> bool:
        folders = self._request("GET", "/Library/VirtualFolders")
        posted = False
        for folder in folders if isinstance(folders, list) else []:
            if not isinstance(folder, dict):
                continue
            kind = str(folder.get("CollectionType") or "").lower()
            if kind != "music":
                continue
            item_id = str(folder.get("ItemId") or folder.get("Id") or "").strip()
            if not item_id:
                continue
            params = (
                ("Recursive", "true"),
                ("MetadataRefreshMode", "Default"),
                ("ImageRefreshMode", "Default"),
                ("ReplaceAllMetadata", "false"),
                ("ReplaceAllImages", "false"),
            )
            self._request("POST", f"/Items/{item_id}/Refresh", params)
            posted = True
        if not posted:
            logger.warning("Jellyfin: no music library found to refresh")
            return False
        return True

    def refresh_paths(self, server_paths: list[str]) -> bool:
        body = {"Updates": [{"Path": p, "UpdateType": "Modified"} for p in server_paths]}
        self._request("POST", "/Library/Media/Updated", json=body)
        return True

    def list_users(self) -> list[ServerUser]:
        body = self._request("GET", "/Users")
        users = body if isinstance(body, list) else []
        # A disabled account is neither imported nor offered as a playlist target. ``Policy.IsHidden`` is NOT used: it only
        # hides a user from the login screen and a real 12.1 server sets it to true for ordinary accounts by default.
        return [
            ServerUser(
                id=str(u["Id"]),
                name=str(u.get("Name") or ""),
                is_admin=bool((u.get("Policy") or {}).get("IsAdministrator", False)),
                extra={"email": ""},
            )
            for u in users
            if isinstance(u, dict)
            and u.get("Id")
            and u.get("Name")
            and not (u.get("Policy") or {}).get("IsDisabled", False)
        ]

    def recent_plays(self, user_id: str, since: datetime, limit: int = 200) -> list[dict[str, Any]]:
        """Audio items played by ``user_id`` strictly after ``since``, newest first.

        Note: Jellyfin only keeps each song's LAST play (``UserData.LastPlayedDate``), so a song replayed
        between two polls counts once.
        """
        since_utc = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since.astimezone(timezone.utc)
        params = (
            ("IncludeItemTypes", "Audio"),
            ("Recursive", "true"),
            ("Filters", "IsPlayed"),
            ("SortBy", "DatePlayed"),
            ("SortOrder", "Descending"),
            ("Fields", "UserData"),
        )
        plays: list[dict[str, Any]] = []
        page_size = max(1, min(limit, _PAGE))
        for item in self._paged(f"/Users/{user_id}/Items", params, page=page_size):
            user_data = item.get("UserData")
            raw_date = str(user_data.get("LastPlayedDate") or "") if isinstance(user_data, dict) else ""
            played_at = _parse_iso_utc(raw_date) if raw_date else None
            if played_at is None:
                continue
            if played_at <= since_utc:
                break
            item_id = str(item.get("Id") or "").strip()
            artists = item.get("Artists") or []
            artist = str(artists[0] if artists else item.get("AlbumArtist") or "").strip()
            title = str(item.get("Name") or "").strip()
            if not item_id or not artist or not title:
                continue
            ticks = item.get("RunTimeTicks")
            duration_ms = int(ticks // 10_000) if isinstance(ticks, (int, float)) and ticks > 0 else None
            plays.append(
                {
                    "item_id": item_id,
                    "artist": artist,
                    "title": title,
                    "album": str(item.get("Album") or "").strip() or None,
                    "duration_ms": duration_ms,
                    "played_at": played_at,
                }
            )
            if len(plays) >= limit:
                break
        return plays

    # ------------------------------------------------------------------ playlists

    def _resolve_user(self, name: str, users: list[ServerUser]) -> Optional[ServerUser]:
        """The account for a Trackseerr target (``""`` = default account), or None when there is no such account."""
        wanted = (name or self._user).strip().lower()
        if wanted:
            return next((u for u in users if u.name.lower() == wanted or u.id.lower() == wanted), None)
        return next((u for u in users if u.is_admin), None)

    def _can_edit(self, playlist_id: str, user_id: str) -> bool:
        """Whether ``user_id`` may edit the playlist, asked with an empty add (a no-op write the server authorises as
        that user): 204 yes, 403 visible but someone else's, 404 not visible to them."""
        try:
            self._request("POST", f"/Playlists/{playlist_id}/Items", [("Ids", ""), ("UserId", user_id)])
        except MediaServerNotFound:
            return False
        except MediaServerAuthError as exc:
            if "HTTP 403" in exc.safe_detail:  # a 403 here is about this playlist; a 401 (bad key) is not
                return False
            raise
        return True

    def _find_playlist(self, user_id: str, name: str) -> Optional[str]:
        """The id of the target's own playlist called ``name``. Other accounts' public playlists that carry the same
        name are listed too but are never returned, so they are never modified."""
        owned: list[str] = []
        for p in self._paged(
            "/Items",
            [("IncludeItemTypes", "Playlist"), ("Recursive", "true"), ("MediaTypes", "Audio"), ("UserId", user_id)],
        ):
            if not p.get("Id") or p.get("Name") != name:
                continue
            if self._can_edit(str(p["Id"]), user_id):
                owned.append(str(p["Id"]))
            else:
                logger.info("Ignoring another account's playlist named '%s'; the user gets their own", name)
        if len(owned) > 1:
            logger.warning("Jellyfin has %d playlists named '%s' for this user; updating the first", len(owned), name)
        return owned[0] if owned else None

    def _resolve_source_user(self, user: Optional[str]) -> ServerUser:
        """Resolve a Jellyfin user name/id, or fall back to the adapter's default account."""
        resolved = self._resolve_user(user or "", self.list_users())
        if resolved is None:
            who = f"'{user}'" if user else "the default account"
            raise MediaServerNotFound(f"Jellyfin user {who} not found", safe_detail=f"Jellyfin user {who} not found")
        return resolved

    def list_source_playlists(self, user: Optional[str] = None) -> list[dict[str, Any]]:
        """Return audio playlists visible to ``user`` (or the default Jellyfin account)."""
        resolved = self._resolve_source_user(user)
        out: list[dict[str, Any]] = []
        for p in self._paged(
            "/Items",
            [("IncludeItemTypes", "Playlist"), ("Recursive", "true"), ("MediaTypes", "Audio"), ("UserId", resolved.id)],
        ):
            pid = str(p.get("Id") or "")
            if not pid:
                continue
            raw_count = p.get("ChildCount") if p.get("ChildCount") is not None else p.get("RecursiveItemCount")
            track_count = int(raw_count) if isinstance(raw_count, (int, float)) and not isinstance(raw_count, bool) else 0
            out.append(
                {
                    "id": pid,
                    "title": str(p.get("Name") or ""),
                    "owner": str(p.get("Owner") or ""),
                    "track_count": track_count,
                }
            )
        return out

    def source_playlist_tracks(self, playlist_id: str, user: Optional[str] = None) -> list[dict[str, str]]:
        """Return ``{title, artist, album}`` dicts for every track in Jellyfin playlist ``playlist_id``."""
        resolved = self._resolve_source_user(user)
        tracks: list[dict[str, str]] = []
        for item in self._paged(f"/Playlists/{playlist_id}/Items", [("UserId", resolved.id)]):
            artists = [str(a) for a in item.get("Artists") or [] if a]
            artist = artists[0] if artists else str(item.get("AlbumArtist") or "")
            tracks.append(
                {
                    "title": str(item.get("Name") or ""),
                    "artist": artist,
                    "album": str(item.get("Album") or ""),
                }
            )
        return tracks

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
        enricher = (
            get_shared_enricher(options.db)
            if options.db is not None and hasattr(options.db, "get_media_management_settings")
            else None
        )
        budget = MusicBrainzBudget() if enricher is not None else None
        matched, missing = self.match_playlist_tracks(
            playlist.tracks, threshold=options.threshold, db=options.db, enricher=enricher, budget=budget
        )
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
                try:
                    self._push(playlist, user, ids, options)
                except (MediaServerAuthError, MediaServerConnectionError):
                    raise  # the key or the server is the problem: it applies to every target
                except MediaServerError as exc:  # e.g. this user cannot see the library: the other targets still sync
                    logger.warning("Jellyfin sync of '%s' failed for one target: %s", playlist.name, exc.safe_detail)
                    outcome = SyncResult(playlist.name, total, len(matched), len(missing), False, exc.safe_detail)
                else:
                    outcome = SyncResult(playlist.name, total, len(matched), len(missing), True)
            done[user.id] = outcome
            results.append(outcome)
        return results

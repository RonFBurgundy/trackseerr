"""Subsonic-API adapter (Navidrome, Gonic, Airsonic(-Advanced), Subsonic itself, ...).

Protocol notes
  * Every call is ``GET {url}/rest/{endpoint}`` with ``f=json`` and API version 1.16.1.
  * Auth is the salted-token scheme ``t=md5(password + salt)&s=salt`` with a fresh random salt per request, so the
    password never travels. When an API key is configured the OpenSubsonic ``apiKey`` parameter is used instead, and
    only if the server advertises the ``apiKeyAuthentication`` extension.
  * Subsonic playlists belong to the authenticated account. Writing for another user would need that user's own
    credentials, which Trackseerr does not hold, so v1 syncs to the configured account only (``capabilities.users`` is
    False and a named target other than that account yields an unsuccessful result). ``list_users`` still works when
    the account is an admin (``getUsers``) and raises ``MediaServerUnsupported`` otherwise.

Playlist ordering: ``updatePlaylist`` removes by index and appends, which is enough to express any target order. The
target list is matched greedily as a subsequence of the current list; everything else is removed and the remainder
appended. Nothing is ever recreated, so a playlist keeps its id (and its sharing / cover) across syncs.

Secrets: the password, salt, token and API key are never logged and never put in an exception message; failures carry
the Subsonic error code/message or the exception type name only, never a URL.
"""

import hashlib
import logging
import re
import secrets
import time
import weakref
from datetime import datetime, timezone
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable, Iterator, Optional, Sequence

import httpx

from trackseerr.lidarr_release import norm_title
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
from trackseerr.missing_csv import delete_missing_csv, write_missing_csv
from trackseerr.models import Playlist, SyncResult, Track
from trackseerr.redaction import redact_text
from trackseerr.storage import clean_library_name

logger = logging.getLogger(__name__)


class _RedactHttpxUrls(logging.Filter):
    """httpx logs every request URL at INFO; a Subsonic URL carries the (replayable) auth token and salt."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        redacted = redact_text(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(f, _RedactHttpxUrls) for f in _httpx_logger.filters):
    _httpx_logger.addFilter(_RedactHttpxUrls())

SUBSONIC_API_VERSION = "1.16.1"
CLIENT_NAME = "Trackseerr"
SUBSONIC_CAPABILITIES = ServerCapabilities(playlists=True, users=False, library_refresh=True, mixes=False, search=True, file_paths=True)

DURATION_TOLERANCE_SECONDS = 3.0
_DURATION_PENALTY = 0.15
_W_TITLE, _W_ARTIST, _W_ALBUM = 0.60, 0.35, 0.05
_SEARCH_PAGE = 20
_FILE_PAGE = 500
_ID_CHUNK = 100  # song ids per request: keeps every GET URL far below common 8 KB proxy limits
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 10.0
_RETRY_STATUS = frozenset({429, 502, 503, 504})
_MUTATING_RETRY_STATUS = frozenset({429})  # a 5xx may follow a change the server already applied
# Subsonic error codes -> generic hierarchy. 40-44: auth; 50: not authorised for the operation; 70: not found.
_AUTH_CODES = frozenset({40, 41, 42, 43, 44})
_ARTIST_SPLIT = re.compile(r"\s*(?:,|;|/|&|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bwith\b|\band\b|\bx\b)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class _Song:
    id: str
    title: str
    artist: str
    album: str
    duration: Optional[float]
    artists: tuple[str, ...]
    raw: dict[str, Any]

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "_Song":
        names = [str(raw.get("artist") or "")]
        for key in ("artists", "albumArtists"):
            for entry in raw.get(key) or []:
                if isinstance(entry, dict) and entry.get("name"):
                    names.append(str(entry["name"]))
        if raw.get("displayArtist"):
            names.append(str(raw["displayArtist"]))
        duration = raw.get("duration")
        return cls(
            id=str(raw.get("id") or ""),
            title=str(raw.get("title") or ""),
            artist=str(raw.get("artist") or ""),
            album=str(raw.get("album") or ""),
            duration=float(duration) if isinstance(duration, (int, float)) else None,
            artists=tuple(dict.fromkeys(n for n in names if n)),
            raw=raw,
        )

    def ref(self) -> ServerTrackRef:
        return ServerTrackRef(id=self.id, title=self.title, artist=self.artist, album=self.album, native=self.raw)


def _parse_iso_utc(raw: str) -> Optional[datetime]:
    """ISO-8601 timestamp (Z suffix, 7-digit .NET fractions tolerated) as aware UTC; None when unparseable."""
    text = raw.strip().replace("Z", "+00:00")
    head, _, tail = text.partition(".")
    if tail:
        digits = "".join(ch for ch in tail if ch.isdigit())
        text = f"{head}.{digits[:6]}{tail[len(digits):]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()


def _artist_variants(name: str) -> list[str]:
    """The full artist string plus its first credited artist ('A feat. B', 'A, B' -> 'A')."""
    full = clean_library_name(name)
    first = clean_library_name(_ARTIST_SPLIT.split(name or "", 1)[0])
    return [v for v in dict.fromkeys([full, first]) if v]


def score_candidate(track: Track, song: _Song) -> float:
    """Similarity of a library song to the wanted track in ``[0, 1]``.

    Weighted normalised title (``norm_title`` ignores remaster / feat. / version noise), artist (best of the credited
    names on either side) and, when the source has one, album. A source length that is known on both sides and differs
    by more than ``DURATION_TOLERANCE_SECONDS`` costs a flat penalty (a different edit / live version).
    """
    title = _ratio(norm_title(track.title), norm_title(song.title))
    wanted = _artist_variants(track.artist)
    have = [v for name in song.artists for v in _artist_variants(name)]
    artist = max((_ratio(w, h) for w in wanted for h in have), default=0.0)
    score = _W_TITLE * title + _W_ARTIST * artist
    weight = _W_TITLE + _W_ARTIST
    if track.album and song.album:
        score += _W_ALBUM * _ratio(clean_library_name(track.album), clean_library_name(song.album))
        weight += _W_ALBUM
    score /= weight
    if track.duration_seconds and song.duration and abs(track.duration_seconds - song.duration) > DURATION_TOLERANCE_SECONDS:
        score -= _DURATION_PENALTY
    return max(0.0, min(1.0, score))


def plan_playlist_update(current: Sequence[str], desired: Sequence[str]) -> tuple[list[int], list[str]]:
    """``(indexes_to_remove, ids_to_append)`` turning ``current`` into exactly ``desired``.

    The longest greedy prefix of ``desired`` that is a subsequence of ``current`` stays in place; every other current
    entry is removed and the rest of ``desired`` appended. Indexes refer to the playlist before any removal.
    """
    keep: set[int] = set()
    matched = 0
    pos = 0
    while matched < len(desired):
        try:
            idx = current.index(desired[matched], pos)
        except ValueError:
            break
        keep.add(idx)
        pos = idx + 1
        matched += 1
    removals = [i for i in range(len(current)) if i not in keep]
    return removals, list(desired[matched:])


def _chunks(items: Sequence[str], size: int = _ID_CHUNK) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


class SubsonicMediaServer(MediaServer):
    kind = "subsonic"
    paths_relative = True  # song ``path`` is relative to the music folder

    def __init__(
        self,
        url: str,
        username: str = "",
        password: str = "",
        *,
        api_key: str = "",
        verify_ssl: bool = True,
        timeout: float = 15.0,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        base = (url or "").strip().rstrip("/")
        if base.endswith("/rest"):
            base = base[: -len("/rest")]
        if not base:
            raise MediaServerError("Subsonic URL is not set", safe_detail="Subsonic URL is not set")
        if not api_key and not (username and password):
            raise MediaServerAuthError(
                "Subsonic needs a username and password, or an API key",
                safe_detail="Subsonic needs a username and password, or an API key",
            )
        self._base = base
        self._user = username
        self._password = password
        self._api_key = api_key
        self._http = httpx.Client(verify=verify_ssl, timeout=timeout, transport=transport, follow_redirects=False)
        # A replaced adapter is simply dropped by the builder: its pooled client is closed once the last in-flight
        # user lets go of it, never while a sync still holds it (the callback holds the client, not the adapter).
        self._finalizer = weakref.finalize(self, self._http.close)
        self._sleep = sleep
        self._api_key_checked = False

    # ------------------------------------------------------------------ transport

    @property
    def capabilities(self) -> ServerCapabilities:
        return SUBSONIC_CAPABILITIES

    def close(self) -> None:
        self._http.close()

    def _auth_params(self) -> list[tuple[str, str]]:
        if self._api_key:
            return [("apiKey", self._api_key)]
        salt = secrets.token_hex(8)
        token = hashlib.md5((self._password + salt).encode("utf-8")).hexdigest()  # noqa: S324 - mandated by the Subsonic protocol
        return [("u", self._user), ("t", token), ("s", salt)]

    def _params(self, extra: Sequence[tuple[str, Any]]) -> list[tuple[str, str]]:
        params = [*self._auth_params(), ("v", SUBSONIC_API_VERSION), ("c", CLIENT_NAME), ("f", "json")]
        params.extend((k, str(v)) for k, v in extra)
        return params

    def _backoff(self, attempt: int, response: Optional[httpx.Response]) -> float:
        delay = _BACKOFF_BASE * (2**attempt)
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                delay = max(delay, float(retry_after))
        return min(delay, _BACKOFF_CAP)

    def _request(self, endpoint: str, extra: Sequence[tuple[str, Any]] = (), *, mutating: bool = False) -> dict[str, Any]:
        """One Subsonic call; returns the unwrapped ``subsonic-response`` dict, raises the generic hierarchy.

        Read-only calls retry 429 and 502/503/504 with exponential backoff (honouring Retry-After) and retry transport
        failures too. A mutating call retries only on 429 (the server rejected it unprocessed): after a 5xx or a
        transport failure it may already have applied the change, and replaying it would double-apply (a repeated
        ``createPlaylist`` makes a duplicate playlist, a repeated index removal drops the wrong tracks).
        """
        if self._api_key and not self._api_key_checked:
            self._require_api_key_support()
        url = f"{self._base}/rest/{endpoint}"
        retry_status = _MUTATING_RETRY_STATUS if mutating else _RETRY_STATUS
        response: Optional[httpx.Response] = None
        for attempt in range(_MAX_ATTEMPTS):
            last = attempt == _MAX_ATTEMPTS - 1
            try:
                response = self._http.get(url, params=self._params(extra))
            except httpx.HTTPError as exc:
                if mutating or last:
                    raise self._transport_error(exc) from exc
                logger.debug("Subsonic %s transport failure (%s); retrying", endpoint, type(exc).__name__)
                self._sleep(self._backoff(attempt, None))
                continue
            if response.status_code in retry_status and not last:
                logger.warning("Subsonic %s answered HTTP %s; retrying", endpoint, response.status_code)
                self._sleep(self._backoff(attempt, response))
                continue
            break
        assert response is not None  # the loop always sets it or raises
        return self._unwrap(endpoint, response)

    @staticmethod
    def _transport_error(exc: httpx.HTTPError) -> MediaServerConnectionError:
        detail = f"Subsonic server unreachable ({type(exc).__name__})"
        return MediaServerConnectionError(detail, safe_detail=detail)

    def _unwrap(self, endpoint: str, response: httpx.Response) -> dict[str, Any]:
        status = response.status_code
        if status in (401, 403):
            raise MediaServerAuthError("Subsonic rejected the credentials", safe_detail=f"Subsonic rejected the credentials (HTTP {status})")
        if status >= 500 or status == 429:
            detail = f"Subsonic server error (HTTP {status})"
            raise MediaServerConnectionError(detail, safe_detail=detail)
        if status >= 400:
            detail = f"Subsonic {endpoint} failed (HTTP {status})"
            if status == 404:
                raise MediaServerNotFound(detail, safe_detail=detail)
            raise MediaServerError(detail, safe_detail=detail)
        try:
            payload = response.json()
            body = payload["subsonic-response"]
        except (ValueError, KeyError, TypeError) as exc:
            detail = "Subsonic server returned an unexpected response (is the URL a Subsonic server?)"
            raise MediaServerError(detail, safe_detail=detail) from exc
        if not isinstance(body, dict):
            detail = "Subsonic server returned an unexpected response"
            raise MediaServerError(detail, safe_detail=detail)
        if body.get("status") == "ok":
            return body
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = error.get("code")
        message = redact_text(str(error.get("message") or "")).strip()
        detail = f"Subsonic error {code}: {message}" if message else f"Subsonic error {code}"
        if code in _AUTH_CODES:
            raise MediaServerAuthError(detail, safe_detail=detail)
        if code == 70:
            raise MediaServerNotFound(detail, safe_detail=detail)
        if code == 50:
            raise MediaServerUnsupported(detail, safe_detail=detail)
        raise MediaServerError(detail, safe_detail=detail)

    def _require_api_key_support(self) -> None:
        try:
            body = self._raw_get("getOpenSubsonicExtensions")
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from exc
        extensions = body.get("openSubsonicExtensions") or []
        names = {e.get("name") for e in extensions if isinstance(e, dict)}
        if "apiKeyAuthentication" not in names:
            detail = "This server does not advertise OpenSubsonic apiKeyAuthentication; use a username and password"
            raise MediaServerAuthError(detail, safe_detail=detail)
        self._api_key_checked = True

    def _raw_get(self, endpoint: str) -> dict[str, Any]:
        """Unauthenticated GET used only for capability discovery; failures read as 'not advertised'."""
        response = self._http.get(
            f"{self._base}/rest/{endpoint}", params=[("v", SUBSONIC_API_VERSION), ("c", CLIENT_NAME), ("f", "json")]
        )
        try:
            body = response.json()["subsonic-response"]
        except (ValueError, KeyError, TypeError):
            return {}
        return body if isinstance(body, dict) and body.get("status") == "ok" else {}

    # ------------------------------------------------------------------ MediaServer

    def test_connection(self) -> ConnectionTest:
        try:
            body = self._request("ping")
        except MediaServerError as exc:
            return ConnectionTest(False, exc.safe_detail)
        version = body.get("serverVersion") or body.get("version") or ""
        server = body.get("type") or "Subsonic"
        return ConnectionTest(True, f"Connected to {server} {version}".strip())

    def _search_songs(self, query: str, limit: int) -> list[_Song]:
        if not query.strip():
            return []
        body = self._request(
            "search3",
            [("query", query), ("songCount", limit), ("songOffset", 0), ("artistCount", 0), ("albumCount", 0)],
        )
        raw = (body.get("searchResult3") or {}).get("song") or []
        return [_Song.from_api(s) for s in raw if isinstance(s, dict) and s.get("id")]

    def _get_song(self, song_id: str) -> Optional[_Song]:
        try:
            body = self._request("getSong", [("id", song_id)])
        except MediaServerNotFound:
            return None
        song = body.get("song")
        return _Song.from_api(song) if isinstance(song, dict) and song.get("id") else None

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
                except MediaServerError as exc:  # stale or unreachable pin: fall back to a normal search, never abort the sync
                    logger.warning(
                        "Pinned match for '%s' could not be looked up (%s); searching instead", track.title, type(exc).__name__
                    )
                    pinned = None
                if pinned is not None:
                    return pinned
        # Servers AND every query word, so the query uses the noise-free title (no "(feat. X)" / "Remastered") and
        # only the first credited artist; the title alone is the fallback for artists tagged differently.
        title = norm_title(track.title) or clean_library_name(track.title)
        first_artist = (_artist_variants(track.artist) or [""])[-1]
        queries = [" ".join(p for p in (title, first_artist) if p), title]
        best: Optional[_Song] = None
        best_score = 0.0
        for query in dict.fromkeys(q for q in queries if q):
            for song in self._search_songs(query, _SEARCH_PAGE):
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
        songs = self._search_songs(query.strip(), max(1, limit))
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
        """Every song via ``search3`` with an empty query, paged on ``songOffset``. Paths are relative to the music
        folder. Raises :class:`MediaServerUnsupported` when the server's songs carry no ``path``."""
        offset = 0
        first = True
        while True:
            body = self._request(
                "search3",
                [
                    ("query", ""),
                    ("artistCount", 0),
                    ("albumCount", 0),
                    ("songCount", _FILE_PAGE),
                    ("songOffset", offset),
                ],
            )
            result = body.get("searchResult3")
            songs = [x for x in (result.get("song") if isinstance(result, dict) else None) or [] if isinstance(x, dict)]
            if first and songs and not any(x.get("path") for x in songs):
                detail = "server does not expose file paths"
                raise MediaServerUnsupported(detail, safe_detail=detail)
            first = False
            for song in songs:
                path = str(song.get("path") or "")
                if not path:
                    continue
                container = str(song.get("suffix") or "").lower().lstrip(".")
                yield ServerFileRef(
                    server_id=str(song.get("id") or ""),
                    path=path,
                    title=str(song.get("title") or ""),
                    artist=str(song.get("artist") or ""),
                    album=str(song.get("album") or ""),
                    container=container,
                )
            if len(songs) < _FILE_PAGE:
                return
            offset += len(songs)

    def last_scan_at(self) -> Optional[datetime]:
        body = self._request("getScanStatus")
        status = body.get("scanStatus")
        raw = status.get("lastScan") if isinstance(status, dict) else None
        if not raw:
            return None
        try:
            if isinstance(raw, (int, float)):
                return datetime.fromtimestamp(raw / 1000 if raw > 1e11 else raw, tz=timezone.utc)
            return _parse_iso_utc(str(raw))
        except (ValueError, OverflowError, OSError):
            logger.debug("Subsonic getScanStatus returned an unparseable lastScan")
            return None

    def refresh_library(self) -> bool:
        body = self._request("startScan", mutating=True)
        scan = body.get("scanStatus")
        return isinstance(scan, dict) or body.get("status") == "ok"

    def list_users(self) -> list[ServerUser]:
        body = self._request("getUsers")  # MediaServerUnsupported (code 50) when the account is not an admin
        raw = (body.get("users") or {}).get("user") or []
        return [
            ServerUser(
                id=str(u.get("username")),
                name=str(u.get("username")),
                is_admin=bool(u.get("adminRole", False)),
                extra={"email": u.get("email", "")},
            )
            for u in raw
            if isinstance(u, dict) and u.get("username")
        ]

    # ------------------------------------------------------------------ playlists

    def _own_playlists(self) -> list[dict[str, Any]]:
        body = self._request("getPlaylists")
        playlists = [p for p in (body.get("playlists") or {}).get("playlist") or [] if isinstance(p, dict)]
        if self._user:  # servers list other users' public playlists too; only ours are writable
            playlists = [p for p in playlists if not p.get("owner") or str(p["owner"]).lower() == self._user.lower()]
        else:
            playlists = [p for p in playlists if not p.get("readonly")]
        return playlists

    def _playlist_entry_ids(self, playlist_id: str) -> tuple[list[str], dict[str, Any]]:
        body = self._request("getPlaylist", [("id", playlist_id)])
        playlist = body.get("playlist") or {}
        return [str(e["id"]) for e in playlist.get("entry") or [] if isinstance(e, dict) and e.get("id")], playlist

    def _push(self, playlist: Playlist, ids: list[str], options: PlaylistSyncOptions) -> None:
        existing = [p for p in self._own_playlists() if p.get("name") == playlist.name]
        if len(existing) > 1:
            logger.warning("Subsonic has %d playlists named '%s'; updating the first", len(existing), playlist.name)
        comment = playlist.description if options.add_description and playlist.description else None
        if not existing:
            first, rest = ids[:_ID_CHUNK], ids[_ID_CHUNK:]
            body = self._request("createPlaylist", [("name", playlist.name), *(("songId", i) for i in first)], mutating=True)
            created = body.get("playlist") or {}
            playlist_id = str(created.get("id") or "")
            if not playlist_id:  # older servers answer with a bare ok; look the playlist up by name
                found = [p for p in self._own_playlists() if p.get("name") == playlist.name]
                playlist_id = str(found[-1]["id"]) if found else ""
            if not playlist_id:
                raise MediaServerError("Subsonic did not report the created playlist", safe_detail="Subsonic did not report the created playlist")
            extra: list[tuple[str, Any]] = []
            if comment:
                extra.append(("comment", comment))
            for chunk in _chunks(rest):
                self._request("updatePlaylist", [("playlistId", playlist_id), *extra, *(("songIdToAdd", i) for i in chunk)], mutating=True)
                extra = []
            if extra:
                self._request("updatePlaylist", [("playlistId", playlist_id), *extra], mutating=True)
            return

        playlist_id = str(existing[0]["id"])
        current, details = self._playlist_entry_ids(playlist_id)
        if options.append:
            have = set(current)
            removals: list[int] = []
            additions = [i for i in dict.fromkeys(ids) if i not in have]
        else:
            removals, additions = plan_playlist_update(current, ids)
        change_comment = comment if comment is not None and comment != (details.get("comment") or "") else None
        if not removals and not additions and change_comment is None:
            return
        first_add, rest_add = additions[:_ID_CHUNK], additions[_ID_CHUNK:]
        params: list[tuple[str, Any]] = [("playlistId", playlist_id)]
        if change_comment is not None:
            params.append(("comment", change_comment))
        # Highest index first: correct whether a server applies the indexes against the original list or one at a time
        # against the live list (a lower index removed first would shift every later one).
        params.extend(("songIndexToRemove", i) for i in sorted(removals, reverse=True))
        params.extend(("songIdToAdd", i) for i in first_add)
        self._request("updatePlaylist", params, mutating=True)
        for chunk in _chunks(rest_add):
            self._request("updatePlaylist", [("playlistId", playlist_id), *(("songIdToAdd", i) for i in chunk)], mutating=True)

    def sync_playlist(
        self, playlist: Playlist, targets: Sequence[str], options: PlaylistSyncOptions
    ) -> list[SyncResult]:
        names = list(targets) or [""]
        matched, missing = self.match_playlist_tracks(playlist.tracks, threshold=options.threshold, db=options.db)
        total = len(playlist.tracks)

        def result(success: bool, error: str = "") -> SyncResult:
            return SyncResult(playlist.name, total, len(matched), len(missing), success, error)

        if options.write_missing_as_csv:
            if missing:
                write_missing_csv(missing, playlist.name, data_dir=options.data_dir)
            elif matched:
                delete_missing_csv(playlist.name, data_dir=options.data_dir)

        results: list[SyncResult] = []
        pushed: Optional[SyncResult] = None
        for name in names:
            if name and name.lower() != self._user.lower():
                results.append(
                    result(False, f"Subsonic playlists can only be written to the configured account (not '{name}')")
                )
                continue
            if pushed is not None:
                results.append(pushed)
                continue
            if not matched:
                logger.warning("No tracks in playlist '%s' could be matched on the Subsonic server", playlist.name)
                pushed = SyncResult(playlist.name, total, 0, len(missing), False, "Zero tracks matched on the Subsonic server")
            else:
                self._push(playlist, [m.id for m in matched], options)
                pushed = result(True)
            results.append(pushed)
        return results

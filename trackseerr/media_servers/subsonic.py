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
import secrets
import sqlite3
import time
import weakref
from datetime import datetime, timezone
from dataclasses import FrozenInstanceError, dataclass
from itertools import islice
from typing import Any, Callable, Iterator, Optional, Sequence

import httpx

from trackseerr.censored import censored_pattern, is_censored, search_text
from trackseerr.lidarr_release import norm_title
from trackseerr.mb_metadata_store import get_shared_enricher
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
from trackseerr.media_servers.navidrome import NavidromeAdmin
from trackseerr.media_servers.song_scoring import (
    DURATION_TOLERANCE_SECONDS as DURATION_TOLERANCE_SECONDS,
    artist_query_credits,
    best_scored_match,
    clean_title,
    first_artist_query,
    score_candidate as _score_fields,
    tail_two_components,
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


class _SubsonicCapabilities(ServerCapabilities):
    """Dynamic ``ServerCapabilities`` that only probes Navidrome when ``users`` is read."""

    def __init__(self, check_users: Callable[[], bool]) -> None:
        self.__dict__.update(
            playlists=True,
            library_refresh=True,
            mixes=False,
            search=True,
            file_paths=True,
            folder_refresh=False,
            _check_users=check_users,
        )

    @property  # type: ignore[override]
    def users(self) -> bool:
        return bool(self.__dict__["_check_users"]())

    def __setattr__(self, name: str, value: Any) -> None:
        raise FrozenInstanceError(f"cannot assign to field {name!r}")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError(f"cannot delete field {name!r}")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ServerCapabilities):
            return NotImplemented
        return (
            self.playlists,
            self.users,
            self.library_refresh,
            self.mixes,
            self.search,
            self.file_paths,
            self.folder_refresh,
        ) == (
            other.playlists,
            other.users,
            other.library_refresh,
            other.mixes,
            other.search,
            other.file_paths,
            other.folder_refresh,
        )

_SEARCH_PAGE = 20
_TITLE_SEARCH_MAX = 100
_ARTIST_SCOPED_MAX = 300
_FILE_PAGE = 500
_ID_CHUNK = 100  # song ids per request: keeps every GET URL far below common 8 KB proxy limits
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 10.0
_RETRY_STATUS = frozenset({429, 502, 503, 504})
_MUTATING_RETRY_STATUS = frozenset({429})  # a 5xx may follow a change the server already applied
# Subsonic error codes -> generic hierarchy. 40-44: auth; 50: not authorised for the operation; 70: not found.
_AUTH_CODES = frozenset({40, 41, 42, 43, 44})


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


def score_candidate(track: Track, song: _Song) -> float:
    """Similarity of a library song to the wanted track in ``[0, 1]``."""
    return _score_fields(
        track.title,
        track.artist,
        track.album,
        track.duration_seconds,
        song.title,
        song.artists,
        song.album,
        song.duration,
    )


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
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._transport = transport
        self._http = httpx.Client(verify=verify_ssl, timeout=timeout, transport=transport, follow_redirects=False)
        # A replaced adapter is simply dropped by the builder: its pooled client is closed once the last in-flight
        # user lets go of it, never while a sync still holds it (the callback holds the client, not the adapter).
        self._finalizer = weakref.finalize(self, self._http.close)
        self._sleep = sleep
        self._api_key_checked = False
        self._is_navidrome: Optional[bool] = None
        self._navidrome_admin: Optional[NavidromeAdmin] = None
        self._navidrome_admin_checked = False

    def with_login(self, username: str, password: str) -> "SubsonicMediaServer":
        """Return a new adapter against the same server authenticated with ``username`` and ``password``."""
        copy = SubsonicMediaServer(
            self._base,
            username,
            password,
            api_key="",
            verify_ssl=self._verify_ssl,
            timeout=self._timeout,
            transport=self._transport,
            sleep=self._sleep,
        )
        copy.server_id = self.server_id
        copy.legacy_overrides = self.legacy_overrides
        return copy

    def navidrome_admin(self) -> Optional[NavidromeAdmin]:
        """Return a cached :class:`NavidromeAdmin` client when this server is Navidrome and uses an admin password login."""
        if self._api_key or not self._user or not self._password:
            return None
        if self._navidrome_admin_checked:
            return self._navidrome_admin
        if self._is_navidrome is None:
            try:
                self._request("ping")
            except MediaServerError as exc:
                logger.debug("Navidrome detection ping failed: %s", exc.safe_detail)
                return None
        if not self._is_navidrome:
            self._navidrome_admin_checked = True
            self._navidrome_admin = None
            return None
        admin = NavidromeAdmin(
            self._base,
            self._user,
            self._password,
            verify_ssl=self._verify_ssl,
            transport=self._transport,
            timeout=self._timeout,
            sleep=self._sleep,
        )
        try:
            is_admin = admin.is_admin
        except MediaServerAuthError as exc:
            logger.warning("Navidrome native login rejected credentials: %s", exc.safe_detail)
            admin.close()
            self._navidrome_admin_checked = True
            self._navidrome_admin = None
            return None
        except MediaServerError as exc:
            logger.debug("Navidrome admin check failed: %s", exc.safe_detail)
            admin.close()
            return None
        if not is_admin:
            admin.close()
            self._navidrome_admin_checked = True
            self._navidrome_admin = None
            return None
        self._navidrome_admin_checked = True
        self._navidrome_admin = admin
        return admin

    # ------------------------------------------------------------------ transport

    @property
    def capabilities(self) -> ServerCapabilities:
        return _SubsonicCapabilities(lambda: self.navidrome_admin() is not None)

    def close(self) -> None:
        if self._navidrome_admin is not None:
            self._navidrome_admin.close()
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
            if endpoint == "ping":
                self._is_navidrome = str(body.get("type") or "").strip().lower() == "navidrome"
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

    def _search_songs(self, query: str, limit: int, offset: int = 0) -> Iterator[_Song]:
        if not query.strip() or limit <= 0:
            return
        fetched = 0
        seen: set[str] = set()
        while fetched < limit:
            page = min(_SEARCH_PAGE, limit - fetched)
            body = self._request(
                "search3",
                [
                    ("query", query),
                    ("songCount", page),
                    ("songOffset", offset + fetched),
                    ("artistCount", 0),
                    ("albumCount", 0),
                ],
            )
            raw = (body.get("searchResult3") or {}).get("song") or []
            items = [s for s in raw if isinstance(s, dict) and s.get("id")]
            fresh = [s for s in items if str(s["id"]) not in seen]
            if not fresh:
                return
            for s in fresh:
                seen.add(str(s["id"]))
                yield _Song.from_api(s)
            fetched += len(items)
            if len(items) < page:
                return

    def _get_song(self, song_id: str) -> Optional[_Song]:
        try:
            body = self._request("getSong", [("id", song_id)])
        except MediaServerNotFound:
            return None
        song = body.get("song")
        return _Song.from_api(song) if isinstance(song, dict) and song.get("id") else None

    def _match_override(self, track: Track, db: Optional[Any]) -> Optional[_Song]:
        if db is None:
            return None
        try:
            override = db.get_match_override(
                track.title,
                track.artist,
                server_id=self.server_id,
                use_legacy=self.legacy_overrides,
            )
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
        artist = first_artist_query(hit.artist) if hit.artist else ""
        queries = [q for q in dict.fromkeys([f"{hit.title} {artist}".strip(), hit.title]) if q]
        for idx, query in enumerate(queries):
            limit = _SEARCH_PAGE if idx == 0 and artist else _TITLE_SEARCH_MAX
            for song in self._search_songs(query, limit):
                song_path = str(song.raw.get("path") or "")
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
        limit: int,
        threshold: float,
        *,
        offset: int = 0,
    ) -> tuple[Optional[_Song], int]:
        songs = list(self._search_songs(query, limit, offset=offset))
        best, best_score, _ = best_scored_match(track, songs, threshold)
        if best is not None and best_score + 1e-9 >= threshold:
            return best, len(songs)
        return None, len(songs)

    def _search_artist_scoped(
        self,
        track: Track,
        search_title: str,
        paged_title: str,
        paged_count: int,
        threshold: float,
    ) -> Optional[_Song]:
        if not search_title:
            return None
        first_artist = first_artist_query(track.artist)
        for credit in artist_query_credits(track.artist):
            if credit == first_artist:
                continue
            match, _ = self._search_and_pick(track, f"{search_title} {credit}", _SEARCH_PAGE, threshold)
            if match is not None:
                return match
        if paged_title and paged_count >= _TITLE_SEARCH_MAX:
            match, _ = self._search_and_pick(
                track,
                paged_title,
                _ARTIST_SCOPED_MAX - _TITLE_SEARCH_MAX,
                threshold,
                offset=_TITLE_SEARCH_MAX,
            )
            if match is not None:
                return match
        return None

    def _search_fuzzy(self, track: Track, threshold: float) -> Optional[_Song]:
        # Servers AND every query word, so the query uses the noise-free title (no "(feat. X)" / "Remastered") and
        # only the first credited artist; the title alone is the fallback for artists tagged differently.
        title = norm_title(track.title.replace("$", "s")) or (
            "" if is_censored(track.title) else clean_library_name(track.title)
        )
        first_artist = first_artist_query(track.artist)
        if title and first_artist:
            match, _ = self._search_and_pick(track, f"{title} {first_artist}", _SEARCH_PAGE, threshold)
            if match is not None:
                return match
        paged_title, paged_count = "", 0
        if title:
            match, paged_count = self._search_and_pick(track, title, _TITLE_SEARCH_MAX, threshold)
            if match is not None:
                return match
            paged_title = title
        cleaned_raw = clean_title(track.title)
        cleaned = norm_title(cleaned_raw.replace("$", "s")) or (
            "" if is_censored(track.title) else clean_library_name(cleaned_raw)
        )
        if cleaned and cleaned != title:
            if first_artist:
                match, _ = self._search_and_pick(track, f"{cleaned} {first_artist}", _SEARCH_PAGE, threshold)
                if match is not None:
                    return match
            match, cleaned_count = self._search_and_pick(track, cleaned, _TITLE_SEARCH_MAX, threshold)
            if match is not None:
                return match
            if cleaned_count >= paged_count:
                paged_title, paged_count = cleaned, cleaned_count
        return self._search_artist_scoped(track, cleaned or title, paged_title, paged_count, threshold)

    def _match_censored(self, track: Track, threshold: float) -> Optional[_Song]:
        pattern = censored_pattern(track.title)
        query = search_text(track.title)
        if pattern is None or not query:
            return None
        kept = [s for s in self._search_songs(query, _TITLE_SEARCH_MAX) if pattern.fullmatch(clean_library_name(s.title))]
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

    def search_tracks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        capped = max(1, limit)
        songs = list(islice(self._search_songs(query.strip(), capped), capped))
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
        admin = self.navidrome_admin()
        if admin is not None:
            return admin.list_users()
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

    def list_source_playlists(self, user: Optional[str] = None) -> list[dict[str, Any]]:
        """Return every playlist visible on the Subsonic server (own and public); ``user`` is ignored."""
        del user
        body = self._request("getPlaylists")
        playlists = [p for p in (body.get("playlists") or {}).get("playlist") or [] if isinstance(p, dict)]
        out: list[dict[str, Any]] = []
        for p in playlists:
            pid = str(p.get("id") or "")
            if not pid:
                continue
            raw_count = p.get("songCount")
            track_count = int(raw_count) if isinstance(raw_count, (int, float)) and not isinstance(raw_count, bool) else 0
            out.append(
                {
                    "id": pid,
                    "title": str(p.get("name") or ""),
                    "owner": str(p.get("owner") or ""),
                    "track_count": track_count,
                }
            )
        return out

    def source_playlist_tracks(self, playlist_id: str, user: Optional[str] = None) -> list[dict[str, str]]:
        """Return ``{title, artist, album}`` dicts for every entry in ``playlist_id``; ``user`` is ignored."""
        del user
        body = self._request("getPlaylist", [("id", playlist_id)])
        playlist = body.get("playlist") or {}
        entries = playlist.get("entry") or []
        return [
            {
                "title": str(e.get("title") or ""),
                "artist": str(e.get("artist") or ""),
                "album": str(e.get("album") or ""),
            }
            for e in entries
            if isinstance(e, dict)
        ]

    def _playlist_entry_ids(self, playlist_id: str) -> tuple[list[str], dict[str, Any]]:
        body = self._request("getPlaylist", [("id", playlist_id)])
        playlist = body.get("playlist") or {}
        return [str(e["id"]) for e in playlist.get("entry") or [] if isinstance(e, dict) and e.get("id")], playlist

    def _create_playlist_with_ids(
        self,
        name: str,
        ids: list[str],
        comment: Optional[str],
        *,
        lookup_own: bool,
    ) -> str:
        first, rest = ids[:_ID_CHUNK], ids[_ID_CHUNK:]
        body = self._request("createPlaylist", [("name", name), *(("songId", i) for i in first)], mutating=True)
        created = body.get("playlist") or {}
        playlist_id = str(created.get("id") or "")
        if not playlist_id and lookup_own:  # older servers answer with a bare ok; look the playlist up by name
            found = [p for p in self._own_playlists() if p.get("name") == name]
            playlist_id = str(found[-1]["id"]) if found else ""
        if not playlist_id:
            raise MediaServerError(
                "Subsonic did not report the created playlist",
                safe_detail="Subsonic did not report the created playlist",
            )
        extra: list[tuple[str, Any]] = []
        if comment:
            extra.append(("comment", comment))
        for chunk in _chunks(rest):
            self._request("updatePlaylist", [("playlistId", playlist_id), *extra, *(("songIdToAdd", i) for i in chunk)], mutating=True)
            extra = []
        if extra:
            self._request("updatePlaylist", [("playlistId", playlist_id), *extra], mutating=True)
        return playlist_id

    def _update_playlist_by_id(
        self,
        playlist_id: str,
        ids: list[str],
        options: PlaylistSyncOptions,
        comment: Optional[str],
    ) -> None:
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

    def _push(self, playlist: Playlist, ids: list[str], options: PlaylistSyncOptions) -> None:
        existing = [p for p in self._own_playlists() if p.get("name") == playlist.name]
        if len(existing) > 1:
            logger.warning("Subsonic has %d playlists named '%s'; updating the first", len(existing), playlist.name)
        comment = playlist.description if options.add_description and playlist.description else None
        if not existing:
            self._create_playlist_with_ids(playlist.name, ids, comment, lookup_own=True)
            return
        self._update_playlist_by_id(str(existing[0]["id"]), ids, options, comment)

    def match_and_prepare(
        self, playlist: Playlist, options: PlaylistSyncOptions
    ) -> tuple[list[ServerTrackRef], list[Track]]:
        """Match ``playlist`` tracks against the Subsonic library and update missing CSV if enabled."""
        enricher = (
            get_shared_enricher(options.db)
            if options.db is not None and hasattr(options.db, "get_media_management_settings")
            else None
        )
        budget = MusicBrainzBudget() if enricher is not None else None
        matched, missing = self.match_playlist_tracks(
            playlist.tracks, threshold=options.threshold, db=options.db, enricher=enricher, budget=budget
        )
        if options.write_missing_as_csv:
            if missing:
                write_missing_csv(missing, playlist.name, data_dir=options.data_dir)
            elif matched:
                delete_missing_csv(playlist.name, data_dir=options.data_dir)
        return matched, missing

    def push_playlist_for_id(
        self,
        playlist: Playlist,
        ids: list[str],
        options: PlaylistSyncOptions,
        *,
        existing_id: Optional[str] = None,
    ) -> tuple[str, bool]:
        """Update an existing playlist by ``existing_id`` (if present) or create a fresh one; returns ``(id, created_new)``."""
        comment = playlist.description if options.add_description and playlist.description else None
        if existing_id:
            try:
                self._update_playlist_by_id(existing_id, ids, options, comment)
                return existing_id, False
            except MediaServerNotFound:
                pass
        created_id = self._create_playlist_with_ids(playlist.name, ids, comment, lookup_own=False)
        return created_id, True

    def delete_playlist(self, playlist_id: str) -> None:
        """Delete a playlist by id on the Subsonic server."""
        self._request("deletePlaylist", [("id", playlist_id)], mutating=True)

    def sync_playlist(
        self, playlist: Playlist, targets: Sequence[str], options: PlaylistSyncOptions
    ) -> list[SyncResult]:
        names = list(targets) or [""]
        matched, missing = self.match_and_prepare(playlist, options)
        total = len(playlist.tracks)

        def result(success: bool, error: str = "") -> SyncResult:
            return SyncResult(playlist.name, total, len(matched), len(missing), success, error, missing=list(missing))

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
                pushed = SyncResult(
                    playlist.name,
                    total,
                    0,
                    len(missing),
                    False,
                    "Zero tracks matched on the Subsonic server",
                    missing=list(missing),
                )
            else:
                self._push(playlist, [m.id for m in matched], options)
                pushed = result(True)
            results.append(pushed)
        return results

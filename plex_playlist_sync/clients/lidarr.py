"""Lidarr REST API Client for automated music discovery and library queuing."""

import logging
import re
import threading
import time
from typing import Any, NamedTuple, Optional
from urllib.parse import quote, urlencode

import httpx

from plex_playlist_sync.lidarr_release import albums_containing_song, match_named_album, select_release_for_song
from plex_playlist_sync.redaction import safe_exc

logger = logging.getLogger(__name__)

_COVER_FILE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\.(?:jpg|jpeg|png|webp|gif)")


def _exc_text(exc: BaseException) -> str:
    """Secret-safe exception text: httpx/Lidarr errors keep their (redacted) message, anything else the type name."""
    return safe_exc(exc, safe_types=(httpx.HTTPError, LidarrApiError))


class LidarrApiError(Exception):
    """A Lidarr request failed. The message is application-authored and never carries the API key."""


class LidarrRateLimited(LidarrApiError):
    """Lidarr (or the metadata service behind it) answered 429 or a transient 5xx; ``retry_after`` is in seconds."""

    def __init__(self, message: str, retry_after: int = 60) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LidarrNotFound(LidarrApiError):
    """Lidarr answered 404 for the requested item."""


class LidarrBadArtwork(LidarrApiError):
    """Lidarr's artwork response was not an allowed raster image type."""


# Raster formats only: SVG (and anything else) is refused so a proxied cover can never carry script.
COVER_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
COVER_DEADLINE_SECONDS = 10.0
_monotonic = time.monotonic


OUTCOME_NOT_IN_PROFILE = "not_in_metadata_profile"
OUTCOME_MONITOR_FAILED = "monitor_failed"
OUTCOME_ALBUMS_PENDING = "albums_pending"
NOT_IN_PROFILE_MESSAGE = "Not available with your Lidarr metadata profile"
ALBUMS_PENDING_MESSAGE = "Lidarr has not finished loading this artist's releases yet"
MONITOR_FAILED_MESSAGE = "Lidarr did not keep the album monitored"

ALBUM_WAIT_ATTEMPTS = 6
ALBUM_WAIT_SECONDS = 3.0
DEFAULTS_TTL_SECONDS = 300.0
_MAX_PER_ALBUM_TRACK_FETCHES = 200
_RETRYABLE_STATUS = (429, 502, 503, 504)


class LidarrAddDefaults(NamedTuple):
    """What Lidarr's own root-folder defaults say an added artist should get."""

    root_folder_path: str
    quality_profile_id: int
    metadata_profile_id: int
    monitor: str
    new_item_monitor: str
    tag_ids: list[int]
    source: str  # "rootfolder" (Lidarr reported them) or "fallback" (first profiles, monitor all, no tags)


_DEFAULTS_CACHE: dict[tuple[str, str, str], tuple[float, LidarrAddDefaults]] = {}
_DEFAULTS_LOCK = threading.Lock()
_SINGLES_CACHE: dict[tuple[str, str, int], tuple[float, bool]] = {}


def invalidate_add_defaults() -> None:
    """Drops every cached root-folder default (call when the Lidarr settings change)."""
    with _DEFAULTS_LOCK:
        _DEFAULTS_CACHE.clear()
        _SINGLES_CACHE.clear()


def _singles_allowed(profile: Any) -> Optional[bool]:
    """``allowed`` of the "Single" entry in a metadata profile resource, or None when the shape is unexpected."""
    items = profile.get("primaryAlbumTypes") if isinstance(profile, dict) else None
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            return None
        album_type = item.get("albumType")
        if isinstance(album_type, dict) and str(album_type.get("name") or "").strip().casefold() == "single":
            allowed = item.get("allowed")
            return allowed if isinstance(allowed, bool) else None
    return None


def _positive_int(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _outcome(status: str, album_id: Optional[int], message: str) -> dict[str, Any]:
    return {"status": status, "album_id": album_id, "message": message}


def _retry_after(resp: httpx.Response) -> int:
    try:
        return max(1, int(resp.headers.get("Retry-After", 60)))
    except ValueError:
        return 60


def _rate_limited(artist: str, exc: "LidarrRateLimited", message: str) -> dict[str, Any]:
    return {
        "status": "rate_limited",
        "artist": artist,
        "retry_after": exc.retry_after,
        "message": f"{message} ({_exc_text(exc)}). Backing off for {exc.retry_after}s.",
    }


class MediaCover(NamedTuple):
    body: bytes
    content_type: str
    validator: str  # upstream ETag / Last-Modified, "" when Lidarr sent neither


class LidarrClient:
    """Client for interacting with Lidarr's REST API (v1)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        verify_ssl: bool = True,
        auto_search: bool = True,
        root_folder: Optional[str] = None,
        timeout: float = 15.0,
        prefer_singles: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.verify_ssl = verify_ssl
        self.auto_search = auto_search
        self.root_folder = root_folder
        self.timeout = timeout
        self.prefer_singles = prefer_singles

    def _get_json(self, path: str) -> Any:
        """GET ``/api/v1/<path>`` with the bounded client timeout; raises LidarrApiError on any failure."""
        url = f"{self.base_url}/api/v1/{path}"
        try:
            with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as client:
                resp = client.get(url, headers=self._get_headers())
        except httpx.HTTPError as exc:
            raise LidarrApiError(f"Could not reach Lidarr ({type(exc).__name__})") from exc
        if resp.status_code in (401, 403):
            raise LidarrApiError("Lidarr rejected the API key")
        if resp.status_code == 404:
            raise LidarrNotFound(f"Lidarr could not find the item for {path}")
        if resp.status_code in _RETRYABLE_STATUS:
            raise LidarrRateLimited(f"Lidarr returned HTTP {resp.status_code} for {path}", _retry_after(resp))
        if resp.status_code != 200:
            raise LidarrApiError(f"Lidarr returned HTTP {resp.status_code} for {path}")
        try:
            return resp.json()
        except ValueError as exc:
            raise LidarrApiError(f"Lidarr returned invalid JSON for {path}") from exc

    def get_options(self) -> dict[str, list[dict[str, Any]]]:
        """Live root folders, quality profiles, metadata profiles and tags (for the settings pickers)."""
        roots = self._get_json("rootfolder")
        quality = self._get_json("qualityprofile")
        metadata = self._get_json("metadataprofile")
        tags = self._get_json("tag")
        if not all(isinstance(v, list) for v in (roots, quality, metadata, tags)):
            raise LidarrApiError("Lidarr returned an unexpected response shape")
        return {
            "root_folders": [
                {"path": str(r.get("path", "")), "free_space": int(r.get("freeSpace") or 0)}
                for r in roots
                if isinstance(r, dict)
            ],
            "quality_profiles": [
                {"id": int(q["id"]), "name": str(q.get("name", ""))} for q in quality if isinstance(q, dict) and "id" in q
            ],
            "metadata_profiles": [
                {"id": int(m["id"]), "name": str(m.get("name", ""))} for m in metadata if isinstance(m, dict) and "id" in m
            ],
            "tags": [{"id": int(t["id"]), "label": str(t.get("label", ""))} for t in tags if isinstance(t, dict) and "id" in t],
        }

    def get_health(self) -> list[dict[str, Any]]:
        """Lidarr's ``/health`` checks, normalised to ``{source, type, message, wiki_url}``."""
        data = self._get_json("health")
        if not isinstance(data, list):
            raise LidarrApiError("Lidarr returned an unexpected health response")
        allowed = {"ok", "notice", "warning", "error"}
        out: list[dict[str, Any]] = []
        for h in data:
            if not isinstance(h, dict):
                continue
            kind = str(h.get("type", "")).lower()
            out.append(
                {
                    "source": str(h.get("source", "")),
                    "type": kind if kind in allowed else "notice",
                    "message": str(h.get("message", "")),
                    "wiki_url": h.get("wikiUrl") or None,
                }
            )
        return out

    def _get_headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def test_connection(self) -> dict[str, Any]:
        """Validates connectivity and authentication against Lidarr."""
        url = f"{self.base_url}/api/v1/system/status"
        try:
            with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as client:
                resp = client.get(url, headers=self._get_headers())
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "online": True,
                        "version": data.get("version", "unknown"),
                        "app_name": data.get("appName", "Lidarr"),
                    }
                return {
                    "online": False,
                    "error": f"HTTP {resp.status_code}: {resp.text[:100]}",
                }
        except Exception as e:
            return {"online": False, "error": _exc_text(e)}

    # ------------------------------------------------------------------ Root-folder defaults (the source of truth)

    def _first_profile_id(self, path: str, label: str) -> int:
        data = self._get_json(path)
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and _positive_int(row.get("id")) is not None:
                    return int(row["id"])
        raise LidarrApiError(f"Lidarr has no {label} profile to fall back to")

    def get_root_folder_defaults(self, path_or_none: Optional[str] = None) -> LidarrAddDefaults:
        """What Lidarr itself would use when adding an artist: the defaults of one of its root folders.

        The root folder is ``path_or_none`` (default: this client's ``root_folder`` setting) when Lidarr has such a
        folder, otherwise Lidarr's first one. A Lidarr that does not report per-folder defaults (older versions) gets
        its first quality / metadata profile, monitor ``all`` and no tags, with ``source="fallback"``. The result is
        cached per process for ``DEFAULTS_TTL_SECONDS``; ``invalidate_add_defaults`` drops it. Raises LidarrApiError.
        """
        requested = path_or_none if path_or_none is not None else self.root_folder
        key = (self.base_url, self.api_key, requested or "")
        now = _monotonic()
        with _DEFAULTS_LOCK:
            hit = _DEFAULTS_CACHE.get(key)
            if hit is not None and now - hit[0] < DEFAULTS_TTL_SECONDS:
                return hit[1]._replace(tag_ids=list(hit[1].tag_ids))

        roots = self._get_json("rootfolder")
        if not isinstance(roots, list):
            raise LidarrApiError("Lidarr returned an unexpected response shape for rootfolder")
        folders = [r for r in roots if isinstance(r, dict)]
        wanted = (requested or "").rstrip("/")
        chosen = next((r for r in folders if wanted and str(r.get("path") or "").rstrip("/") == wanted), None)
        if chosen is None and folders:
            chosen = folders[0]
        raw: dict[str, Any] = chosen or {}
        path = str(raw.get("path") or requested or "/music")

        quality_id = _positive_int(raw.get("defaultQualityProfileId"))
        metadata_id = _positive_int(raw.get("defaultMetadataProfileId"))
        source = "rootfolder"
        if quality_id is None or metadata_id is None:
            source = "fallback"
            if quality_id is None:
                quality_id = self._first_profile_id("qualityprofile", "quality")
            if metadata_id is None:
                metadata_id = self._first_profile_id("metadataprofile", "metadata")
            logger.warning("Lidarr reports no root-folder defaults for %s; using its first profiles", path)
        defaults = LidarrAddDefaults(
            root_folder_path=path,
            quality_profile_id=quality_id,
            metadata_profile_id=metadata_id,
            monitor=str(raw.get("defaultMonitorOption") or "all"),
            new_item_monitor=str(raw.get("defaultNewItemMonitorOption") or "all"),
            tag_ids=[int(t) for t in (raw.get("defaultTags") or []) if _positive_int(t) is not None],
            source=source,
        )
        with _DEFAULTS_LOCK:
            _DEFAULTS_CACHE[key] = (now, defaults)
        return defaults._replace(tag_ids=list(defaults.tag_ids))

    def metadata_profile_allows_singles(self, profile_id: int) -> bool:
        """Whether Lidarr metadata profile ``profile_id`` allows the "Single" primary album type.

        ``GET /metadataprofile/{id}`` returns ``primaryAlbumTypes``: ``[{"albumType": {"id", "name"}, "allowed"}]``.
        An unexpected shape (or no "Single" entry) answers ``True`` with a warning, so the setting stays visible.
        Cached for ``DEFAULTS_TTL_SECONDS`` next to the root-folder defaults. Raises LidarrApiError on a request failure.
        """
        key = (self.base_url, self.api_key, int(profile_id))
        now = _monotonic()
        with _DEFAULTS_LOCK:
            hit = _SINGLES_CACHE.get(key)
            if hit is not None and now - hit[0] < DEFAULTS_TTL_SECONDS:
                return hit[1]
        data = self._get_json(f"metadataprofile/{int(profile_id)}")
        allowed = _singles_allowed(data)
        if allowed is None:
            logger.warning("Lidarr metadata profile %s has an unexpected shape; assuming singles are allowed", profile_id)
            allowed = True
        with _DEFAULTS_LOCK:
            _SINGLES_CACHE[key] = (now, allowed)
        return allowed

    def add_artist_with_defaults(
        self, candidate: dict[str, Any], *, whole_artist: bool, search: bool = False
    ) -> dict[str, Any]:
        """``POST /artist`` for a looked-up artist, carrying every root-folder default of Lidarr.

        ``whole_artist=True`` adds the artist the way Lidarr's own add-artist screen does (``addOptions.monitor`` is
        the root folder's monitor option, ``searchForMissingAlbums`` is ``search``). ``whole_artist=False`` is for a
        song or album request: same profiles, tags, root folder and ``monitorNewItems``, but nothing monitored and no
        search yet, because the caller then monitors exactly the release it needs. Raises LidarrApiError.
        """
        defaults = self.get_root_folder_defaults()
        payload = {
            **candidate,
            "monitored": True,
            "rootFolderPath": defaults.root_folder_path,
            "qualityProfileId": defaults.quality_profile_id,
            "metadataProfileId": defaults.metadata_profile_id,
            "tags": list(defaults.tag_ids),
            "monitorNewItems": defaults.new_item_monitor,
            "addOptions": {
                "monitor": defaults.monitor if whole_artist else "none",
                "searchForMissingAlbums": bool(search) if whole_artist else False,
            },
        }
        result = self._send_json("POST", "artist", payload)
        if not isinstance(result, dict):
            raise LidarrApiError("Lidarr returned an unexpected response shape when adding an artist")
        return result

    # ------------------------------------------------------------------ Song / album requests

    def artist_refresh_state(self, artist_id: int) -> str:
        """Whether Lidarr's ``RefreshArtist`` command for ``artist_id`` is still working: ``running``, ``done`` or ``unknown``.

        ``GET /api/v1/command`` lists command resources ``{id, name: "RefreshArtist", commandName, status, body}``
        where ``status`` is queued, started, completed, failed, aborted, cancelled or orphaned and ``body.artistIds``
        holds the artist ids (``body.artistId`` is accepted too). ``running`` means a queued/started refresh exists
        for the artist, ``done`` that only finished ones do, ``unknown`` that Lidarr lists none (never seen or
        already trimmed) or the list could not be read. Raises LidarrRateLimited.
        """
        try:
            data = self._get_json("command")
        except LidarrRateLimited:
            raise
        except LidarrApiError as exc:
            logger.debug("Lidarr command list unavailable: %s", _exc_text(exc))
            return "unknown"
        if not isinstance(data, list):
            return "unknown"
        mine: list[dict[str, Any]] = []
        for cmd in data:
            if not isinstance(cmd, dict) or str(cmd.get("name") or "").casefold() != "refreshartist":
                continue
            body = cmd.get("body") if isinstance(cmd.get("body"), dict) else {}
            ids = body.get("artistIds") if isinstance(body.get("artistIds"), list) else []
            if body.get("artistId") is not None:
                ids = [*ids, body.get("artistId")]
            if any(_positive_int(i) == int(artist_id) for i in ids):
                mine.append(cmd)
        if not mine:
            return "unknown"
        if any(str(c.get("status") or "").casefold() in ("queued", "started") for c in mine):
            return "running"
        return "done"

    def wait_for_artist_albums(
        self, artist_id: int, attempts: int = ALBUM_WAIT_ATTEMPTS, seconds: float = ALBUM_WAIT_SECONDS
    ) -> tuple[list[dict[str, Any]], bool]:
        """``(albums, settled)`` for a just-added artist: ``settled`` once Lidarr has finished loading it.

        Lidarr loads a new artist's albums and tracks piecemeal, so the first non-empty list is not complete.
        The artist is settled when its ``RefreshArtist`` command has finished and albums exist, or, when Lidarr lists
        no such command, when the album and track counts are the same on two consecutive polls. Sleeps between
        attempts, so call it from a worker or background thread only (pass ``attempts=1`` otherwise).
        """
        albums: list[dict[str, Any]] = []
        previous: Optional[tuple[int, int]] = None
        total = max(1, attempts)
        for attempt in range(total):
            albums = self.fetch_artist_albums(artist_id)
            state = self.artist_refresh_state(artist_id)
            if albums and state == "done":
                return albums, True
            if albums and state == "unknown":
                fingerprint = (len(albums), len(self.fetch_artist_tracks(artist_id, albums)))
                if fingerprint == previous:
                    return albums, True
                previous = fingerprint
            else:
                previous = None
            if attempt + 1 < total:
                time.sleep(seconds)
        return albums, False

    def fetch_artist_tracks(self, artist_id: int, albums: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
        """Every track Lidarr lists for the artist: ``/track?artistId=``, else album by album when that is empty."""
        try:
            data = self._get_json(f"track?artistId={int(artist_id)}")
        except LidarrRateLimited:
            raise
        except LidarrApiError as exc:
            logger.warning("Lidarr track list by artist failed, trying per album: %s", _exc_text(exc))
            data = []
        tracks = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
        if tracks or not albums:
            return tracks
        for album in albums[:_MAX_PER_ALBUM_TRACK_FETCHES]:
            if album.get("id") is not None:
                tracks.extend(self.fetch_album_tracks(int(album["id"])))
        return tracks

    @staticmethod
    def _normalise_wants(album_names: Optional[list[str]], wants: Optional[list[dict[str, Any]]]) -> list[dict[str, str]]:
        if wants is None:
            wants = [{"album": a, "title": "", "item_type": "album"} for a in (album_names or [])]
        out: list[dict[str, str]] = []
        for w in wants:
            album = str(w.get("album") or "").strip()
            title = str(w.get("title") or "").strip()
            kind = "track" if (str(w.get("item_type") or "").lower() == "track" and title) else "album"
            if kind == "album" and not album:
                album = title  # an album request carrying only a title
            out.append({"album": album, "title": title if kind == "track" else "", "item_type": kind})
        return out

    def add_artist_and_albums(
        self,
        artist_name: str,
        album_names: Optional[list[str]] = None,
        auto_search: Optional[bool] = None,
        wants: Optional[list[dict[str, Any]]] = None,
        album_wait_attempts: int = ALBUM_WAIT_ATTEMPTS,
        album_wait_seconds: float = ALBUM_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Makes Lidarr fetch exactly the releases requested, leaving its own configuration alone.

        ``wants`` are ``{"album", "title", "item_type"}`` dicts (``album_names`` is shorthand for album wants).
        An artist already in Lidarr is never modified; a new one is added unmonitored with the root-folder defaults
        (see ``add_artist_with_defaults``). Each want then selects one release from Lidarr's own album list (see
        ``lidarr_release``), that album is monitored (and searched when auto-search is on) and re-read to confirm
        ``monitored`` stuck. The result carries ``outcomes``, one per want and in order, each
        ``{"status", "album_id", "message"}`` with status ``monitored``, ``not_in_metadata_profile``,
        ``monitor_failed``, ``albums_pending`` or ``error``. Never raises for a Lidarr failure.

        A new artist's albums load asynchronously in Lidarr, so ``album_wait_attempts`` polls (sleeping
        ``album_wait_seconds`` between them); pass 1 from a request thread that must not block.
        """
        clean_artist = artist_name.strip()
        if not clean_artist:
            return {"status": "error", "message": "Empty artist name"}
        want_list = self._normalise_wants(album_names, wants)
        should_search = self.auto_search if auto_search is None else auto_search

        try:
            try:
                results = self.lookup_artist(clean_artist)
            except LidarrRateLimited as exc:
                return _rate_limited(clean_artist, exc, "Rate limited by Lidarr/MusicBrainz")
            except LidarrApiError as exc:
                return {"status": "error", "artist": clean_artist, "message": f"Lookup failed: {_exc_text(exc)}"}
            if not results:
                return {"status": "not_found", "artist": clean_artist, "message": "Artist not found in Lidarr lookup"}

            candidate = results[0]
            artist_id = int(candidate.get("id") or 0)
            artist_title = str(candidate.get("artistName") or clean_artist)
            was_new = False
            if not artist_id:
                try:
                    added = self.add_artist_with_defaults(candidate, whole_artist=False)
                except LidarrRateLimited as exc:
                    return _rate_limited(artist_title, exc, "Rate limited during artist add")
                except LidarrApiError as exc:
                    return {
                        "status": "error",
                        "artist": artist_title,
                        "message": f"Failed to add artist: {_exc_text(exc)}",
                    }
                artist_id = int(added.get("id") or 0)
                if not artist_id:
                    return {"status": "error", "artist": artist_title, "message": "Lidarr did not return the new artist id"}
                was_new = True
                logger.info("Added artist '%s' (ID %s) to Lidarr unmonitored", artist_title, artist_id)

            outcomes: list[dict[str, Any]] = []
            monitored_ids: list[int] = []
            if want_list:
                try:
                    if was_new:
                        albums, settled = self.wait_for_artist_albums(artist_id, album_wait_attempts, album_wait_seconds)
                    else:
                        albums, settled = self.fetch_artist_albums(artist_id), True
                    outcomes, monitored_ids = self._monitor_wants(artist_id, albums, want_list, should_search, settled)
                except LidarrRateLimited as exc:
                    return _rate_limited(artist_title, exc, "Rate limited while selecting releases")
                except LidarrApiError as exc:
                    logger.warning("Selecting Lidarr releases for artist %s failed: %s", artist_id, _exc_text(exc))
                    outcomes = [_outcome("error", None, f"Lidarr request failed: {_exc_text(exc)}") for _ in want_list]
                    monitored_ids = []

            ok = [o for o in outcomes if o["status"] == "monitored"]
            searched = bool(should_search and monitored_ids)
            if outcomes and not ok:
                statuses = {o["status"] for o in outcomes}
                status = next(
                    (s for s in (OUTCOME_NOT_IN_PROFILE, OUTCOME_MONITOR_FAILED, OUTCOME_ALBUMS_PENDING) if s in statuses),
                    "error",
                )
                return {
                    "status": status,
                    "artist": artist_title,
                    "artist_id": artist_id,
                    "added": was_new,
                    "matched_album_ids": [],
                    "searched": False,
                    "outcomes": outcomes,
                    "message": outcomes[0]["message"],
                }
            return {
                "status": "success",
                "artist": artist_title,
                "artist_id": artist_id,
                "added": was_new,
                "matched_album_ids": monitored_ids,
                "searched": searched,
                "outcomes": outcomes,
                "message": f"{'Added and monitored' if was_new else 'Monitored'} in Lidarr ({len(monitored_ids)} album(s))",
            }
        except Exception as e:  # last-resort guard for one artist: the root cause is logged (redacted) and returned
            logger.error("Exception in Lidarr add_artist_and_albums: %s", _exc_text(e))
            return {"status": "error", "artist": clean_artist, "message": _exc_text(e)}

    def _monitor_wants(
        self,
        artist_id: int,
        albums: list[dict[str, Any]],
        wants: list[dict[str, str]],
        should_search: bool,
        settled: bool = True,
    ) -> tuple[list[dict[str, Any]], list[int]]:
        """Selects, monitors, searches and verifies one release per want; returns (outcomes, monitored album ids).

        Until the artist is ``settled`` (Lidarr has finished loading its releases) a want that finds no release is
        ``albums_pending`` (retry later), never ``not_in_metadata_profile``: the release may simply not have loaded.
        """
        outcomes: list[dict[str, Any]] = [_outcome("error", None, "Nothing to look up") for _ in wants]
        if not albums:
            pending = _outcome(OUTCOME_ALBUMS_PENDING, None, ALBUMS_PENDING_MESSAGE)
            return [dict(pending) for _ in wants], []

        tracks_cache: list[list[dict[str, Any]]] = []

        def tracks() -> list[dict[str, Any]]:
            if not tracks_cache:
                tracks_cache.append(self.fetch_artist_tracks(artist_id, albums))
            return tracks_cache[0]

        chosen: dict[int, dict[str, Any]] = {}
        for idx, want in enumerate(wants):
            if not (want["album"] or want["title"]):
                continue
            if want["item_type"] == "track":
                # The requested album is only a tie-breaker among releases that really hold the song.
                target = select_release_for_song(
                    albums_containing_song(tracks(), albums, want["title"]),
                    want["title"],
                    want["album"],
                    prefer_singles=self.prefer_singles,
                )
            else:
                target = match_named_album(albums, want["album"])
            if target is None or target.get("id") is None:
                if settled:
                    outcomes[idx] = _outcome(OUTCOME_NOT_IN_PROFILE, None, NOT_IN_PROFILE_MESSAGE)
                else:
                    outcomes[idx] = _outcome(OUTCOME_ALBUMS_PENDING, None, ALBUMS_PENDING_MESSAGE)
                continue
            album_id = int(target["id"])
            chosen[idx] = target
            outcomes[idx] = _outcome("monitored", album_id, "")

        to_monitor = list(dict.fromkeys(int(a["id"]) for a in chosen.values()))
        if not to_monitor:
            return outcomes, []
        unmonitored = [i for i in to_monitor if not next(a for a in chosen.values() if int(a["id"]) == i).get("monitored")]
        if unmonitored:
            self.set_albums_monitored(unmonitored, True)
        if should_search:
            try:
                self.run_command("AlbumSearch", albumIds=to_monitor)
            except LidarrApiError as exc:  # the albums are monitored; Lidarr's own schedule will still find them
                logger.warning("Lidarr AlbumSearch failed for artist %s: %s", artist_id, _exc_text(exc))

        verified: dict[int, bool] = {}
        for album_id in to_monitor:
            verified[album_id] = bool(self.fetch_album(album_id).get("monitored"))
        for idx in chosen:
            album_id = int(outcomes[idx]["album_id"])
            if not verified[album_id]:
                outcomes[idx] = _outcome(OUTCOME_MONITOR_FAILED, album_id, MONITOR_FAILED_MESSAGE)
        return outcomes, [i for i in to_monitor if verified[i]]

    def search_and_add_track(
        self,
        artist_name: str,
        album_name: str = "",
        title: str = "",
        auto_search: Optional[bool] = None,
        album_wait_attempts: int = ALBUM_WAIT_ATTEMPTS,
    ) -> dict[str, Any]:
        """Makes Lidarr fetch one song: its release is chosen and monitored (see ``add_artist_and_albums``)."""
        res = self.add_artist_and_albums(
            artist_name=artist_name,
            auto_search=auto_search,
            wants=[{"album": album_name, "title": title, "item_type": "track"}],
            album_wait_attempts=album_wait_attempts,
        )
        if res.get("status") == "success":
            return {
                "status": "added" if res.get("added") else "already_monitored",
                "artist": res.get("artist", artist_name),
                "album": album_name,
                "lidarr_id": res.get("artist_id", 0),
                "searched": res.get("searched", False),
                "message": res.get("message", ""),
            }
        return res

    def get_all_artists(self, client: Optional[httpx.Client] = None) -> list[dict[str, Any]]:
        """Retrieves all artists monitored or unmonitored from Lidarr."""
        url = f"{self.base_url}/api/v1/artist"
        headers = self._get_headers()
        try:
            if client:
                resp = client.get(url, headers=headers)
            else:
                with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as c:
                    resp = c.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
            else:
                logger.warning("Failed to fetch Lidarr artists: HTTP %s", resp.status_code)
        except Exception as e:
            logger.warning("Exception fetching artists from Lidarr: %s", _exc_text(e))
        return []

    def get_all_albums(
        self,
        artist_id: Optional[int] = None,
        client: Optional[httpx.Client] = None,
    ) -> list[dict[str, Any]]:
        """Retrieves albums from Lidarr, optionally filtered by artist ID."""
        url = f"{self.base_url}/api/v1/album"
        params: dict[str, Any] = {}
        if artist_id is not None:
            params["artistId"] = artist_id
        headers = self._get_headers()
        try:
            if client:
                resp = client.get(url, headers=headers, params=params if params else None)
            else:
                with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as c:
                    resp = c.get(url, headers=headers, params=params if params else None)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
            else:
                logger.warning("Failed to fetch Lidarr albums: HTTP %s", resp.status_code)
        except Exception as e:
            logger.warning("Exception fetching albums from Lidarr: %s", _exc_text(e))
        return []

    def get_all_tracks(
        self,
        artist_id: Optional[int] = None,
        album_id: Optional[int] = None,
        client: Optional[httpx.Client] = None,
    ) -> list[dict[str, Any]]:
        """Retrieves tracks from Lidarr, optionally filtered by artistId and/or albumId."""
        url = f"{self.base_url}/api/v1/track"
        params: dict[str, Any] = {}
        if artist_id is not None:
            params["artistId"] = artist_id
        if album_id is not None:
            params["albumId"] = album_id
        headers = self._get_headers()
        try:
            if client:
                resp = client.get(url, headers=headers, params=params if params else None)
            else:
                with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as c:
                    resp = c.get(url, headers=headers, params=params if params else None)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
            else:
                logger.warning("Failed to fetch Lidarr tracks: HTTP %s", resp.status_code)
        except Exception as e:
            logger.warning("Exception fetching tracks from Lidarr: %s", _exc_text(e))
        return []

    def get_all_track_files(
        self,
        artist_id: Optional[int] = None,
        client: Optional[httpx.Client] = None,
    ) -> list[dict[str, Any]]:
        """Retrieves physical track files from Lidarr, optionally filtered by artist ID."""
        url = f"{self.base_url}/api/v1/trackfile"
        params: dict[str, Any] = {}
        if artist_id is not None:
            params["artistId"] = artist_id
        headers = self._get_headers()
        try:
            if client:
                resp = client.get(url, headers=headers, params=params if params else None)
            else:
                with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as c:
                    resp = c.get(url, headers=headers, params=params if params else None)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
            else:
                logger.warning("Failed to fetch Lidarr track files: HTTP %s", resp.status_code)
        except Exception as e:
            logger.warning("Exception fetching track files from Lidarr: %s", _exc_text(e))
        return []

    # ------------------------------------------------------------------ Activity / Wanted (admin proxy)

    def _send_json(self, method: str, path: str, json_body: Optional[dict[str, Any]] = None) -> Any:
        """POST, PUT or DELETE ``/api/v1/<path>``; returns the parsed JSON body (``None`` when empty).

        Raises LidarrApiError on any failure, with a message that never carries the API key.
        """
        url = f"{self.base_url}/api/v1/{path}"
        try:
            with httpx.Client(verify=self.verify_ssl, timeout=self.timeout) as client:
                if method == "POST":
                    resp = client.post(url, headers=self._get_headers(), json=json_body)
                elif method == "PUT":
                    resp = client.put(url, headers=self._get_headers(), json=json_body)
                elif method == "DELETE":
                    resp = client.delete(url, headers=self._get_headers())
                else:
                    raise ValueError(f"Unsupported method: {method}")
        except httpx.HTTPError as exc:
            raise LidarrApiError(f"Could not reach Lidarr ({type(exc).__name__})") from exc
        if resp.status_code in (401, 403):
            raise LidarrApiError("Lidarr rejected the API key")
        if resp.status_code == 404:
            raise LidarrNotFound(f"Lidarr could not find the item for {path}")
        if resp.status_code in _RETRYABLE_STATUS:
            raise LidarrRateLimited(f"Lidarr returned HTTP {resp.status_code} for {path}", _retry_after(resp))
        if resp.status_code not in (200, 201, 202, 204):
            raise LidarrApiError(f"Lidarr returned HTTP {resp.status_code} for {path}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _get_page(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET a paged Lidarr resource and check it has the ``{page, pageSize, totalRecords, records}`` shape."""
        data = self._get_json(f"{path}?{urlencode(params)}")
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise LidarrApiError(f"Lidarr returned an unexpected response shape for {path}")
        return data

    @staticmethod
    def _page_params(page: int, page_size: int, sort_key: str, sort_dir: str, **extra: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": int(page),
            "pageSize": int(page_size),
            "sortKey": sort_key,
            "sortDirection": "descending" if str(sort_dir).lower() == "desc" else "ascending",
        }
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def get_queue(self, page: int, page_size: int, sort_key: str, sort_dir: str) -> dict[str, Any]:
        return self._get_page(
            "queue",
            self._page_params(page, page_size, sort_key, sort_dir, includeArtist="true", includeAlbum="true"),
        )

    def delete_queue_item(self, queue_id: int, remove_from_client: bool, blocklist: bool) -> None:
        query = urlencode(
            {"removeFromClient": str(bool(remove_from_client)).lower(), "blocklist": str(bool(blocklist)).lower()}
        )
        self._send_json("DELETE", f"queue/{int(queue_id)}?{query}")

    def get_history(
        self, page: int, page_size: int, sort_key: str, sort_dir: str, event_type: Optional[int] = None
    ) -> dict[str, Any]:
        return self._get_page(
            "history",
            self._page_params(
                page, page_size, sort_key, sort_dir, includeArtist="true", includeAlbum="true", eventType=event_type
            ),
        )

    def mark_history_failed(self, history_id: int) -> None:
        self._send_json("POST", f"history/failed/{int(history_id)}")

    def get_blocklist(self, page: int, page_size: int, sort_key: str, sort_dir: str) -> dict[str, Any]:
        return self._get_page("blocklist", self._page_params(page, page_size, sort_key, sort_dir))

    def delete_blocklist_item(self, blocklist_id: int) -> None:
        self._send_json("DELETE", f"blocklist/{int(blocklist_id)}")

    def get_wanted(self, kind: str, page: int, page_size: int, sort_key: str, sort_dir: str) -> dict[str, Any]:
        """``kind`` is ``missing`` or ``cutoff``."""
        if kind not in ("missing", "cutoff"):
            raise ValueError(f"Unknown wanted list: {kind!r}")
        return self._get_page(
            f"wanted/{kind}",
            self._page_params(page, page_size, sort_key, sort_dir, includeArtist="true", monitored="true"),
        )

    def run_command(self, name: str, **body: Any) -> dict[str, Any]:
        """POST ``/command`` (e.g. ``AlbumSearch`` with ``albumIds``); returns Lidarr's command resource."""
        result = self._send_json("POST", "command", {"name": name, **body})
        return result if isinstance(result, dict) else {}


    # ------------------------------------------------------------------ Library browsing (live proxy)

    def fetch_artists(self) -> list[dict[str, Any]]:
        """Every artist (raises LidarrApiError, unlike ``get_all_artists`` which swallows failures)."""
        data = self._get_json("artist")
        if not isinstance(data, list):
            raise LidarrApiError("Lidarr returned an unexpected response shape for artist")
        return [row for row in data if isinstance(row, dict)]

    def fetch_albums(self) -> list[dict[str, Any]]:
        """Every album, including all of an artist's albums, with ``artist`` and ``statistics`` attached."""
        data = self._get_json("album?includeAllArtistAlbums=true")
        if not isinstance(data, list):
            raise LidarrApiError("Lidarr returned an unexpected response shape for album")
        return [row for row in data if isinstance(row, dict)]

    def fetch_artist(self, artist_id: int) -> dict[str, Any]:
        data = self._get_json(f"artist/{int(artist_id)}")
        if not isinstance(data, dict):
            raise LidarrApiError("Lidarr returned an unexpected response shape for artist")
        return data

    def fetch_album(self, album_id: int) -> dict[str, Any]:
        data = self._get_json(f"album/{int(album_id)}")
        if not isinstance(data, dict):
            raise LidarrApiError("Lidarr returned an unexpected response shape for album")
        return data

    def fetch_artist_albums(self, artist_id: int) -> list[dict[str, Any]]:
        data = self._get_json(f"album?artistId={int(artist_id)}")
        if not isinstance(data, list):
            raise LidarrApiError("Lidarr returned an unexpected response shape for album")
        return [row for row in data if isinstance(row, dict)]

    def fetch_album_tracks(self, album_id: int) -> list[dict[str, Any]]:
        data = self._get_json(f"track?albumId={int(album_id)}")
        if not isinstance(data, list):
            raise LidarrApiError("Lidarr returned an unexpected response shape for track")
        return [row for row in data if isinstance(row, dict)]

    def lookup_artist(self, term: str) -> list[dict[str, Any]]:
        """``GET /artist/lookup``. ``term`` may be a name or ``lidarr:<musicbrainz id>``; raises LidarrApiError."""
        data = self._get_json(f"artist/lookup?term={quote(term)}")
        if not isinstance(data, list):
            raise LidarrApiError("Lidarr returned an unexpected response shape for artist lookup")
        return [row for row in data if isinstance(row, dict)]

    def set_artist_monitored(self, artist_id: int, monitored: bool) -> dict[str, Any]:
        """Fetch-modify-put, so no other field of the artist resource is lost."""
        artist = self.fetch_artist(artist_id)
        artist["monitored"] = bool(monitored)
        result = self._send_json("PUT", f"artist/{int(artist_id)}", artist)
        return result if isinstance(result, dict) else artist

    def bulk_edit_artists(
        self,
        artist_ids: list[int],
        monitored: Optional[bool] = None,
        quality_profile_id: Optional[int] = None,
    ) -> None:
        """``PUT /api/v1/artist/editor``: one call changing ``monitored`` and/or the quality profile of many artists."""
        body: dict[str, Any] = {"artistIds": [int(i) for i in artist_ids]}
        if monitored is not None:
            body["monitored"] = bool(monitored)
        if quality_profile_id is not None:
            body["qualityProfileId"] = int(quality_profile_id)
        self._send_json("PUT", "artist/editor", body)

    def set_albums_monitored(self, album_ids: list[int], monitored: bool) -> None:
        self._send_json("PUT", "album/monitor", {"albumIds": [int(i) for i in album_ids], "monitored": bool(monitored)})

    def fetch_mediacover(
        self,
        kind: str,
        entity_id: int,
        filename: str,
        max_bytes: int,
        deadline_seconds: float = COVER_DEADLINE_SECONDS,
    ) -> "MediaCover":
        """``MediaCover(body, content_type, validator)`` of ``/mediacover/<kind>/<id>/<filename>``.

        ``kind`` is ``artist`` or ``album``; ``filename`` must be a plain image file name (no separators). The API key
        goes in a header only, redirects are not followed, the body is capped at ``max_bytes``, the whole fetch must
        finish within ``deadline_seconds`` (wall clock, not just per read) and the content type must be one of
        ``COVER_CONTENT_TYPES`` (never SVG). ``validator`` is Lidarr's ETag or Last-Modified when it sent one.
        Raises LidarrNotFound for 404, LidarrBadArtwork for a disallowed content type and LidarrApiError otherwise.
        """
        if kind not in ("artist", "album") or not _COVER_FILE_RE.fullmatch(filename):
            raise LidarrNotFound("Invalid artwork path")
        url = f"{self.base_url}/api/v1/mediacover/{kind}/{int(entity_id)}/{filename}"
        headers = {"X-Api-Key": self.api_key, "Accept": ", ".join(sorted(COVER_CONTENT_TYPES))}
        deadline = _monotonic() + float(deadline_seconds)
        try:
            with httpx.Client(verify=self.verify_ssl, timeout=self.timeout, follow_redirects=False) as client:
                with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code == 404:
                        raise LidarrNotFound("Lidarr has no such artwork")
                    if resp.status_code in (401, 403):
                        raise LidarrApiError("Lidarr rejected the API key")
                    if resp.status_code != 200:
                        raise LidarrApiError(f"Lidarr returned HTTP {resp.status_code} for artwork")
                    content_type = str(resp.headers.get("content-type", "")).split(";")[0].strip().lower()
                    if content_type not in COVER_CONTENT_TYPES:
                        raise LidarrBadArtwork(f"Lidarr artwork has a disallowed content type ({content_type[:40]!r})")
                    declared = str(resp.headers.get("content-length", "")).strip()
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise LidarrApiError("Lidarr artwork exceeds the size limit")
                    validator = str(resp.headers.get("etag") or resp.headers.get("last-modified") or "")[:200]
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise LidarrApiError("Lidarr artwork exceeds the size limit")
                        if _monotonic() > deadline:
                            raise LidarrApiError("Lidarr artwork download timed out")
                    if _monotonic() > deadline:
                        raise LidarrApiError("Lidarr artwork download timed out")
        except httpx.HTTPError as exc:
            raise LidarrApiError(f"Could not reach Lidarr ({type(exc).__name__})") from exc
        return MediaCover(bytes(body), content_type, validator)

"""Zero-key music discovery client wrapping iTunes and Deezer public APIs with TTL caching."""

import concurrent.futures
import copy
import logging
import threading
import time
import urllib.parse
from typing import Any, Optional

import requests

from plex_playlist_sync.models import DiscoveryItem
from plex_playlist_sync.track_counts import positive_int
from plex_playlist_sync.system_paths import is_system_folder_name

logger = logging.getLogger(__name__)


def _deezer_artist_ref(artist_obj: Any) -> Optional[str]:
    """``deezer:artist:<n>`` for a Deezer API artist object carrying an id, else None."""
    if isinstance(artist_obj, dict) and artist_obj.get("id"):
        return f"deezer:artist:{artist_obj['id']}"
    return None


class DiscoveryUpstreamError(Exception):
    """Raised when a keyless discovery provider could not be reached (distinct from 'not found')."""


def _deezer_album_ref(album_obj: Any) -> Optional[str]:
    """``deezer:album:<n>`` for a Deezer API album object carrying an id, else None."""
    if isinstance(album_obj, dict) and album_obj.get("id"):
        return f"deezer:album:{album_obj['id']}"
    return None


class DiscoveryClient:
    """Thread-safe zero-key client for querying public trending, new release, and search APIs."""

    def __init__(self, ttl_seconds: float = 900.0, timeout: float = 5.0) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.timeout = float(timeout)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "TrackSeerr/1.0 (ZeroKey Music Discovery; https://github.com/trackseerr)",
                "Accept": "application/json",
            }
        )

    def _get_cached(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._cache.get(key)
            if entry:
                timestamp, data = entry
                if time.time() - timestamp < self.ttl_seconds:
                    return copy.deepcopy(data)
                del self._cache[key]
        return None

    def _set_cached(self, key: str, data: Any) -> None:
        with self._lock:
            self._cache[key] = (time.time(), copy.deepcopy(data))

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # -------------------------------------------------------------------------
    # Public Methods
    # -------------------------------------------------------------------------

    def get_trending(self, limit: int = 25) -> list[dict[str, Any]]:
        """Retrieves top trending tracks and albums from Deezer charts with iTunes fallback."""
        cache_key = f"trending:{limit}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        items: list[dict[str, Any]] = []

        # 1. Attempt Deezer Charts
        try:
            # Query trending tracks
            track_url = f"https://api.deezer.com/chart/0/tracks?limit={max(10, limit)}"
            resp = self.session.get(track_url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                for t in data.get("data", []):
                    art = t.get("artist", {}) if isinstance(t.get("artist"), dict) else {}
                    alb = t.get("album", {}) if isinstance(t.get("album"), dict) else {}
                    cover = alb.get("cover_big") or alb.get("cover_medium") or ""
                    items.append(
                        DiscoveryItem(
                            id=f"deezer:track:{t.get('id')}",
                            item_type="track",
                            title=str(t.get("title", "")).strip(),
                            artist=str(art.get("name", "")).strip() or "Unknown Artist",
                            album=str(alb.get("title", "")).strip() or None,
                            cover_url=cover or None,
                            preview_url=t.get("preview") or None,
                            release_date=t.get("release_date") or None,
                            artist_discovery_id=_deezer_artist_ref(art),
                        album_discovery_id=_deezer_album_ref(alb),
                        ).to_dict()
                    )

            # Query trending albums
            album_url = f"https://api.deezer.com/chart/0/albums?limit={max(10, limit)}"
            resp_alb = self.session.get(album_url, timeout=self.timeout)
            if resp_alb.status_code == 200:
                data_alb = resp_alb.json()
                for a in data_alb.get("data", []):
                    art = a.get("artist", {}) if isinstance(a.get("artist"), dict) else {}
                    cover = a.get("cover_big") or a.get("cover_medium") or ""
                    items.append(
                        DiscoveryItem(
                            id=f"deezer:album:{a.get('id')}",
                            item_type="album",
                            title=str(a.get("title", "")).strip(),
                            artist=str(art.get("name", "")).strip() or "Unknown Artist",
                            album=str(a.get("title", "")).strip(),
                            cover_url=cover or None,
                            preview_url=None,
                            release_date=a.get("release_date") or None,
                            artist_discovery_id=_deezer_artist_ref(art),
                        ).to_dict()
                    )
        except Exception as e:
            logger.warning("Deezer trending charts query failed: %s; falling back to iTunes RSS", e)

        # 2. Fallback to iTunes Top Albums RSS if Deezer returned empty
        if not items:
            items = self._fetch_itunes_top_albums(limit=limit)

        deduped = self._deduplicate_items(items, limit=limit)
        self._set_cached(cache_key, deduped)
        return deduped

    def get_new_releases(self, limit: int = 25) -> list[dict[str, Any]]:
        """Retrieves new release albums from iTunes RSS feed with Deezer fallback."""
        cache_key = f"new_releases:{limit}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        # 1. Primary: iTunes Top Albums / New Releases RSS
        items = self._fetch_itunes_top_albums(limit=limit)

        # 2. Fallback: Deezer Chart Albums
        if not items:
            try:
                album_url = f"https://api.deezer.com/chart/0/albums?limit={limit}"
                resp = self.session.get(album_url, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    for a in data.get("data", []):
                        art = a.get("artist", {}) if isinstance(a.get("artist"), dict) else {}
                        cover = a.get("cover_big") or a.get("cover_medium") or ""
                        items.append(
                            DiscoveryItem(
                                id=f"deezer:album:{a.get('id')}",
                                item_type="album",
                                title=str(a.get("title", "")).strip(),
                                artist=str(art.get("name", "")).strip() or "Unknown Artist",
                                album=str(a.get("title", "")).strip(),
                                cover_url=cover or None,
                                preview_url=None,
                                release_date=a.get("release_date") or None,
                                artist_discovery_id=_deezer_artist_ref(art),
                            ).to_dict()
                        )
            except Exception as e:
                logger.warning("Deezer fallback albums query failed: %s", e)

        deduped = self._deduplicate_items(items, limit=limit)
        self._set_cached(cache_key, deduped)
        return deduped

    def search(self, query: str, item_type: str = "all", limit: int = 25) -> list[dict[str, Any]]:
        """Performs multi-source search across iTunes and Deezer public APIs."""
        clean_q = (query or "").strip()
        if not clean_q:
            return []

        clean_type = (item_type or "all").lower().strip()
        cache_key = f"search:{clean_q}:{clean_type}:{limit}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        items: list[dict[str, Any]] = []

        # Concurrently search Deezer and iTunes
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_deezer = executor.submit(self._search_deezer, clean_q, clean_type, limit)
            future_itunes = executor.submit(self._search_itunes, clean_q, clean_type, limit)

            done, not_done = concurrent.futures.wait(
                [future_deezer, future_itunes], timeout=self.timeout
            )
            for future in done:
                try:
                    res = future.result()
                    if res and isinstance(res, list):
                        items.extend(res)
                except Exception as exc:
                    logger.warning("DiscoveryClient search worker failed: %s", exc)

            for future in not_done:
                logger.warning("DiscoveryClient search worker timed out after %ss", self.timeout)
                future.cancel()

        deduped = self._deduplicate_items(items, limit=limit)
        self._set_cached(cache_key, deduped)
        return deduped

    def search_artist(self, artist_name: str) -> Optional[dict[str, Any]]:
        """Queries Deezer public search API to resolve canonical artist metadata and ID."""
        clean_name = (artist_name or "").strip()
        if not clean_name:
            return None
        if is_system_folder_name(clean_name):
            logger.info("DiscoveryClient: skipping artist search for system/trash folder name %r", clean_name)
            return None

        cache_key = f"artist_search:{clean_name.lower()}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            encoded_q = urllib.parse.quote(clean_name)
            url = f"https://api.deezer.com/search/artist?q={encoded_q}&limit=5"
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                artists = data.get("data", []) if isinstance(data, dict) else []
                for art in artists:
                    if not isinstance(art, dict):
                        continue
                    art_id = art.get("id")
                    name = str(art.get("name", "")).strip()
                    if art_id and (name.lower() == clean_name.lower() or clean_name.lower() in name.lower()):
                        result = {
                            "id": f"deezer:artist:{art_id}",
                            "name": name,
                            "image_url": art.get("picture_xl") or art.get("picture_big") or art.get("picture_medium") or art.get("picture"),
                            "banner_url": art.get("picture_xl") or art.get("picture_big"),
                        }
                        self._set_cached(cache_key, result)
                        return result
                if artists and isinstance(artists[0], dict):
                    first = artists[0]
                    art_id = first.get("id")
                    if art_id:
                        result = {
                            "id": f"deezer:artist:{art_id}",
                            "name": str(first.get("name", "")).strip(),
                            "image_url": first.get("picture_xl") or first.get("picture_big") or first.get("picture_medium") or first.get("picture"),
                            "banner_url": first.get("picture_xl") or first.get("picture_big"),
                        }
                        self._set_cached(cache_key, result)
                        return result
        except Exception as exc:
            logger.warning("DiscoveryClient search_artist error for '%s': %s", clean_name, exc)

        return None

    @staticmethod
    def _numeric_artist_id(deezer_artist_id: Any) -> str:
        """Accepts ``deezer:artist:123``, ``123`` or an int and returns the bare numeric id ('' when invalid)."""
        raw = str(deezer_artist_id or "").strip()
        num = raw.rsplit(":", 1)[-1]
        return num if num.isdigit() else ""

    def get_related_artists(self, deezer_artist_id: Any, limit: int = 20) -> list[dict[str, Any]]:
        """Keyless Deezer ``/artist/{id}/related``; returns ``[{id, name}]`` (id is the numeric Deezer id)."""
        num_id = self._numeric_artist_id(deezer_artist_id)
        if not num_id:
            return []
        cache_key = f"related_artists:{num_id}:{limit}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            resp = self.session.get(
                f"https://api.deezer.com/artist/{num_id}/related?limit={int(limit)}", timeout=self.timeout
            )
            if resp.status_code != 200:
                logger.warning("Deezer related artists for %s returned HTTP %s", num_id, resp.status_code)
                return []
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Deezer related artists query failed for %s: %s", num_id, exc)
            return []

        results: list[dict[str, Any]] = []
        for art in data.get("data", []) if isinstance(data, dict) else []:
            if not isinstance(art, dict) or not art.get("id"):
                continue
            name = str(art.get("name", "")).strip()
            if name:
                results.append({"id": str(art["id"]), "name": name})
        results = results[: max(0, int(limit))]
        self._set_cached(cache_key, results)
        return results

    def get_artist_top_tracks(self, deezer_artist_id: Any, limit: int = 10) -> list[dict[str, Any]]:
        """Keyless Deezer ``/artist/{id}/top``; returns ``[{title, artist, album}]``."""
        num_id = self._numeric_artist_id(deezer_artist_id)
        if not num_id:
            return []
        cache_key = f"artist_top:{num_id}:{limit}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            resp = self.session.get(
                f"https://api.deezer.com/artist/{num_id}/top?limit={int(limit)}", timeout=self.timeout
            )
            if resp.status_code != 200:
                logger.warning("Deezer artist top for %s returned HTTP %s", num_id, resp.status_code)
                return []
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Deezer artist top tracks query failed for %s: %s", num_id, exc)
            return []

        results: list[dict[str, Any]] = []
        for t in data.get("data", []) if isinstance(data, dict) else []:
            if not isinstance(t, dict):
                continue
            title = str(t.get("title", "")).strip()
            if not title:
                continue
            art = t.get("artist") if isinstance(t.get("artist"), dict) else {}
            alb = t.get("album") if isinstance(t.get("album"), dict) else {}
            results.append(
                {
                    "title": title,
                    "artist": str(art.get("name", "")).strip() or "Unknown Artist",
                    "album": str(alb.get("title", "")).strip() or None,
                }
            )
        results = results[: max(0, int(limit))]
        self._set_cached(cache_key, results)
        return results

    def search_artists(self, query: str, limit: int = 10) -> Optional[list[dict[str, Any]]]:
        """Keyless Deezer ``/search/artist``; returns ``[{id, name, image_url, nb_fan}]`` (``id`` is ``deezer:artist:<n>``).

        ``[]`` means Deezer answered with no artists; None means the lookup itself failed (callers must not cache it).
        """
        clean_q = (query or "").strip()
        if not clean_q:
            return []
        cache_key = f"artist_search_all:{clean_q.lower()}:{limit}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        try:
            resp = self.session.get(
                f"https://api.deezer.com/search/artist?q={urllib.parse.quote(clean_q)}&limit={int(limit)}",
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                logger.warning("Deezer artist search for '%s' returned HTTP %s", clean_q, resp.status_code)
                return None
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Deezer artist search failed for '%s': %s", clean_q, exc)
            return None

        results: list[dict[str, Any]] = []
        for art in data.get("data", []) if isinstance(data, dict) else []:
            if not isinstance(art, dict) or not art.get("id"):
                continue
            name = str(art.get("name", "")).strip()
            if not name:
                continue
            try:
                nb_fan = int(art.get("nb_fan") or 0)
            except (TypeError, ValueError):
                nb_fan = 0
            results.append(
                {
                    "id": f"deezer:artist:{art['id']}",
                    "name": name,
                    "image_url": art.get("picture_xl") or art.get("picture_big") or art.get("picture_medium") or art.get("picture"),
                    "nb_fan": nb_fan,
                }
            )
        self._set_cached(cache_key, results)
        return results

    def get_artist_top_tracks_detailed(self, deezer_artist_id: Any, limit: int = 10) -> list[dict[str, Any]]:
        """Keyless Deezer ``/artist/{id}/top``; returns ``[{id, title, artist, album, duration, preview_url}]``."""
        num_id = self._numeric_artist_id(deezer_artist_id)
        if not num_id:
            return []
        cache_key = f"artist_top_detailed:{num_id}:{limit}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        try:
            resp = self.session.get(
                f"https://api.deezer.com/artist/{num_id}/top?limit={int(limit)}", timeout=self.timeout
            )
            if resp.status_code != 200:
                logger.warning("Deezer artist top for %s returned HTTP %s", num_id, resp.status_code)
                return []
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Deezer artist top tracks query failed for %s: %s", num_id, exc)
            return []

        results: list[dict[str, Any]] = []
        for t in data.get("data", []) if isinstance(data, dict) else []:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            title = str(t.get("title", "")).strip()
            if not title:
                continue
            art = t.get("artist") if isinstance(t.get("artist"), dict) else {}
            alb = t.get("album") if isinstance(t.get("album"), dict) else {}
            try:
                duration = int(t.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0
            results.append(
                {
                    "id": f"deezer:track:{t['id']}",
                    "title": title,
                    "artist": str(art.get("name", "")).strip() or "Unknown Artist",
                    "album": str(alb.get("title", "")).strip() or None,
                    "duration": duration,
                    "preview_url": t.get("preview") or None,
                    **({"album_discovery_id": _deezer_album_ref(alb)} if _deezer_album_ref(alb) else {}),
                }
            )
        results = results[: max(0, int(limit))]
        self._set_cached(cache_key, results)
        return results

    def get_album_details(self, album_id: str) -> Optional[dict[str, Any]]:
        """Fetches full album details, tracklist, and audio previews from Deezer or iTunes with TTL caching."""
        clean_id = (album_id or "").strip()
        if not clean_id:
            return None

        cache_key = f"album:{clean_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        result: Optional[dict[str, Any]] = None
        if clean_id.startswith("deezer:album:"):
            num_id = clean_id.removeprefix("deezer:album:")
            result = self._get_deezer_album_details(num_id)
        elif clean_id.startswith("itunes:album:"):
            num_id = clean_id.removeprefix("itunes:album:")
            result = self._get_itunes_album_details(num_id)
        elif clean_id.isdigit():
            result = self._get_deezer_album_details(clean_id)
            if result is None:
                result = self._get_itunes_album_details(clean_id)
        elif clean_id.startswith("deezer:"):
            num_id = clean_id.split(":")[-1]
            result = self._get_deezer_album_details(num_id)
        elif clean_id.startswith("itunes:"):
            num_id = clean_id.split(":")[-1]
            result = self._get_itunes_album_details(num_id)

        if result is not None:
            self._set_cached(cache_key, result)

        return result

    def get_track_details(self, track_id: str) -> Optional[dict[str, Any]]:
        """Fetches a single track (Deezer or iTunes) with full metadata, TTL cached. None when not found."""
        clean_id = (track_id or "").strip()
        if not clean_id:
            return None
        cache_key = f"track:{clean_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        result: Optional[dict[str, Any]] = None
        if clean_id.startswith("itunes:track:"):
            result = self._get_itunes_track_details(clean_id.removeprefix("itunes:track:"))
        elif clean_id.startswith("deezer:track:"):
            result = self._get_deezer_track_details(clean_id.removeprefix("deezer:track:"))
        elif clean_id.isdigit():
            result = self._get_deezer_track_details(clean_id)
        if result is not None:
            self._set_cached(cache_key, result)
        return result

    def _get_deezer_track_details(self, num_id: str) -> Optional[dict[str, Any]]:
        """Deezer ``/track/{id}``; label/genres come from the (cached) album lookup and are omitted on failure."""
        if not num_id.isdigit():
            return None
        try:
            resp = self.session.get(f"https://api.deezer.com/track/{num_id}", timeout=self.timeout)
            if resp.status_code != 200:
                logger.warning("Deezer track query returned status %d for id %s", resp.status_code, num_id)
                return None
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Deezer track query failed for %s: %s", num_id, exc)
            raise DiscoveryUpstreamError(f"Deezer track lookup failed: {exc}") from exc
        if not isinstance(data, dict) or "error" in data or not data.get("id"):
            return None

        art = data.get("artist") if isinstance(data.get("artist"), dict) else {}
        alb = data.get("album") if isinstance(data.get("album"), dict) else {}
        album_ref = _deezer_album_ref(alb)
        try:
            duration = int(data.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        result: dict[str, Any] = {
            "id": f"deezer:track:{data['id']}",
            "item_type": "track",
            "title": str(data.get("title", "")).strip(),
            "artist": str(art.get("name", "")).strip() or "Unknown Artist",
            "album": str(alb.get("title", "")).strip() or None,
            "cover_url": alb.get("cover_xl") or alb.get("cover_big") or alb.get("cover_medium") or None,
            "preview_url": data.get("preview") or None,
            "duration": duration,
            "track_position": data.get("track_position") or None,
            "disk_number": data.get("disk_number") or None,
            "release_date": data.get("release_date") or alb.get("release_date") or None,
            "isrc": data.get("isrc") or None,
            "explicit": bool(data.get("explicit_lyrics")),
            "contributors": [
                {"name": str(c.get("name", "")).strip(), "role": str(c.get("role") or "Main").strip()}
                for c in (data.get("contributors") or [])
                if isinstance(c, dict) and c.get("name")
            ],
        }
        artist_ref = _deezer_artist_ref(art)
        if artist_ref:
            result["artist_discovery_id"] = artist_ref
        if album_ref:
            result["album_discovery_id"] = album_ref
        try:
            if int(data.get("bpm") or 0) > 0:
                result["bpm"] = int(data["bpm"])
        except (TypeError, ValueError):
            logger.debug("Deezer track %s has non-numeric bpm %r", num_id, data.get("bpm"))
        if data.get("gain") is not None:
            result["gain"] = data["gain"]
        if album_ref:
            try:
                album_data = self.get_album_details(album_ref)
            except (requests.RequestException, ValueError) as exc:
                logger.warning("Deezer album enrichment failed for track %s: %s", num_id, exc)
                album_data = None
            if album_data:
                if album_data.get("label"):
                    result["label"] = album_data["label"]
                if album_data.get("genres"):
                    result["genres"] = list(album_data["genres"])
        return result

    def _get_itunes_track_details(self, num_id: str) -> Optional[dict[str, Any]]:
        """iTunes lookup by track id."""
        if not num_id.isdigit():
            return None
        try:
            resp = self.session.get(f"https://itunes.apple.com/lookup?id={num_id}&entity=song", timeout=self.timeout)
            if resp.status_code != 200:
                logger.warning("iTunes track query returned status %d for id %s", resp.status_code, num_id)
                return None
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("iTunes track query failed for %s: %s", num_id, exc)
            raise DiscoveryUpstreamError(f"iTunes track lookup failed: {exc}") from exc
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return None
        r = next(
            (x for x in results if isinstance(x, dict) and (x.get("wrapperType") == "track" or x.get("kind") == "song")),
            None,
        )
        if r is None or not r.get("trackId"):
            return None
        cover = r.get("artworkUrl100") or ""
        if cover:
            cover = cover.replace("100x100bb", "600x600bb")
        millis = r.get("trackTimeMillis") or 0
        genre = r.get("primaryGenreName")
        result: dict[str, Any] = {
            "id": f"itunes:track:{r['trackId']}",
            "item_type": "track",
            "title": str(r.get("trackName", "")).strip(),
            "artist": str(r.get("artistName", "")).strip() or "Unknown Artist",
            "album": str(r.get("collectionName", "")).strip() or None,
            "cover_url": cover or None,
            "preview_url": r.get("previewUrl") or None,
            "duration": int(round(millis / 1000.0)),
            "track_position": r.get("trackNumber") or None,
            "disk_number": r.get("discNumber") or None,
            "release_date": r.get("releaseDate") or None,
            "isrc": None,
            "explicit": r.get("trackExplicitness") == "explicit",
            "contributors": [],
        }
        if r.get("artistId"):
            result["artist_discovery_id"] = f"itunes:artist:{r['artistId']}"
        if r.get("collectionId"):
            result["album_discovery_id"] = f"itunes:album:{r['collectionId']}"
        if genre:
            result["genres"] = [str(genre).strip()]
        return result

    def get_artist_details(self, artist_id: str) -> Optional[dict[str, Any]]:
        """Fetches artist profile and discography grouped into albums, singles_eps, and compilations."""
        clean_id = (artist_id or "").strip()
        if not clean_id:
            return None

        cache_key = f"artist:{clean_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        result: Optional[dict[str, Any]] = None
        if clean_id.startswith("deezer:artist:"):
            num_id = clean_id.removeprefix("deezer:artist:")
            result = self._get_deezer_artist_details(num_id)
        elif clean_id.startswith("itunes:artist:"):
            num_id = clean_id.removeprefix("itunes:artist:")
            result = self._get_itunes_artist_details(num_id)
        elif clean_id.isdigit():
            result = self._get_deezer_artist_details(clean_id)
            if result is None:
                result = self._get_itunes_artist_details(clean_id)
        elif clean_id.startswith("deezer:"):
            num_id = clean_id.split(":")[-1]
            result = self._get_deezer_artist_details(num_id)
        elif clean_id.startswith("itunes:"):
            num_id = clean_id.split(":")[-1]
            result = self._get_itunes_artist_details(num_id)

        if result is not None:
            self._set_cached(cache_key, result)

        return result

    # -------------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------------

    def _fetch_itunes_top_albums(self, limit: int = 25) -> list[dict[str, Any]]:
        """Fetches top albums from iTunes RSS JSON feed."""
        url = f"https://itunes.apple.com/us/rss/topalbums/limit={limit}/json"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                logger.warning("iTunes RSS returned status %d", resp.status_code)
                return []

            data = resp.json()
            feed = data.get("feed", {})
            entries = feed.get("entry", [])
            if not isinstance(entries, list):
                entries = [entries] if entries else []

            results: list[dict[str, Any]] = []
            for entry in entries:
                title = entry.get("im:name", {}).get("label", "")
                artist = entry.get("im:artist", {}).get("label", "")
                images = entry.get("im:image", [])
                cover = ""
                if isinstance(images, list) and images:
                    # Pick largest image available
                    cover = images[-1].get("label", "")
                    if cover:
                        cover = cover.replace("170x170bb", "600x600bb")

                item_id = ""
                id_obj = entry.get("id", {})
                if isinstance(id_obj, dict):
                    attrs = id_obj.get("attributes", {})
                    if isinstance(attrs, dict):
                        item_id = attrs.get("im:id", "")

                rel_date = entry.get("im:releaseDate", {}).get("label", "")

                if title and artist:
                    results.append(
                        DiscoveryItem(
                            id=f"itunes:album:{item_id}" if item_id else f"itunes:album:{hash(title + artist)}",
                            item_type="album",
                            title=str(title).strip(),
                            artist=str(artist).strip(),
                            album=str(title).strip(),
                            cover_url=cover or None,
                            preview_url=None,
                            release_date=rel_date or None,
                        ).to_dict()
                    )
            return results
        except Exception as e:
            logger.warning("iTunes RSS query error: %s", e)
            return []

    def _search_deezer(self, query: str, item_type: str, limit: int = 25) -> list[dict[str, Any]]:
        """Queries Deezer public search API."""
        encoded_q = urllib.parse.quote(query)
        results: list[dict[str, Any]] = []

        try:
            if item_type in ("track", "all"):
                url = f"https://api.deezer.com/search/track?q={encoded_q}&limit={limit}"
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 200:
                    for t in resp.json().get("data", []):
                        art = t.get("artist", {}) if isinstance(t.get("artist"), dict) else {}
                        alb = t.get("album", {}) if isinstance(t.get("album"), dict) else {}
                        cover = alb.get("cover_big") or alb.get("cover_medium") or ""
                        results.append(
                            DiscoveryItem(
                                id=f"deezer:track:{t.get('id')}",
                                item_type="track",
                                title=str(t.get("title", "")).strip(),
                                artist=str(art.get("name", "")).strip() or "Unknown Artist",
                                album=str(alb.get("title", "")).strip() or None,
                                cover_url=cover or None,
                                preview_url=t.get("preview") or None,
                                release_date=t.get("release_date") or None,
                                artist_discovery_id=_deezer_artist_ref(art),
                            album_discovery_id=_deezer_album_ref(alb),
                            ).to_dict()
                        )

            if item_type in ("album", "all"):
                url = f"https://api.deezer.com/search/album?q={encoded_q}&limit={limit}"
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 200:
                    for a in resp.json().get("data", []):
                        art = a.get("artist", {}) if isinstance(a.get("artist"), dict) else {}
                        cover = a.get("cover_big") or a.get("cover_medium") or ""
                        results.append(
                            DiscoveryItem(
                                id=f"deezer:album:{a.get('id')}",
                                item_type="album",
                                title=str(a.get("title", "")).strip(),
                                artist=str(art.get("name", "")).strip() or "Unknown Artist",
                                album=str(a.get("title", "")).strip(),
                                cover_url=cover or None,
                                preview_url=None,
                                release_date=a.get("release_date") or None,
                                artist_discovery_id=_deezer_artist_ref(art),
                            ).to_dict()
                        )
        except Exception as e:
            logger.warning("Deezer search error: %s", e)

        return results

    def _search_itunes(self, query: str, item_type: str, limit: int = 25) -> list[dict[str, Any]]:
        """Queries iTunes search API."""
        encoded_q = urllib.parse.quote(query)
        results: list[dict[str, Any]] = []

        entities = []
        if item_type in ("album", "all"):
            entities.append(("album", "album"))
        if item_type in ("track", "all"):
            entities.append(("song", "track"))

        for entity_param, mapped_type in entities:
            try:
                url = f"https://itunes.apple.com/search?term={encoded_q}&entity={entity_param}&limit={limit}"
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    for r in data.get("results", []):
                        title = r.get("trackName") if mapped_type == "track" else r.get("collectionName")
                        artist = r.get("artistName", "")
                        album = r.get("collectionName")
                        cover = r.get("artworkUrl100", "")
                        if cover:
                            cover = cover.replace("100x100bb", "600x600bb")
                        preview = r.get("previewUrl") if mapped_type == "track" else None
                        item_id = r.get("trackId") if mapped_type == "track" else r.get("collectionId")

                        if title and artist:
                            results.append(
                                DiscoveryItem(
                                    id=f"itunes:{mapped_type}:{item_id}",
                                    item_type=mapped_type,
                                    title=str(title).strip(),
                                    artist=str(artist).strip(),
                                    album=str(album).strip() if album else None,
                                    cover_url=cover or None,
                                    preview_url=preview or None,
                                    release_date=r.get("releaseDate") or None,
                                    artist_discovery_id=(
                                        f"itunes:artist:{r['artistId']}" if r.get("artistId") else None
                                    ),
                                    album_discovery_id=(
                                        f"itunes:album:{r['collectionId']}"
                                        if mapped_type == "track" and r.get("collectionId")
                                        else None
                                    ),
                                ).to_dict()
                            )
            except Exception as e:
                logger.warning("iTunes search error for entity %s: %s", entity_param, e)

        return results

    def _deduplicate_items(self, items: list[dict[str, Any]], limit: int = 25) -> list[dict[str, Any]]:
        """Deduplicates items prioritizing entries with preview URLs."""
        seen: dict[tuple[str, str, str], dict[str, Any]] = {}
        for it in items:
            item_type = it.get("item_type", "")
            artist = (it.get("artist") or "").lower().strip()
            title = (it.get("title") or "").lower().strip()
            key = (item_type, artist, title)

            if key not in seen:
                seen[key] = it
            else:
                # If existing has no preview_url but new one has one, replace
                if not seen[key].get("preview_url") and it.get("preview_url"):
                    seen[key] = it

        out = list(seen.values())
        return out[:limit]

    def _get_deezer_album_details(self, num_id: str) -> Optional[dict[str, Any]]:
        """Queries Deezer album details API endpoint."""
        url = f"https://api.deezer.com/album/{num_id}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                logger.warning("Deezer album query returned status %d for id %s", resp.status_code, num_id)
                return None
            data = resp.json()
            if not isinstance(data, dict) or "error" in data:
                return None

            alb_id = data.get("id")
            if not alb_id:
                return None

            title = str(data.get("title", "")).strip()
            artist_obj = data.get("artist") if isinstance(data.get("artist"), dict) else {}
            artist_name = str(artist_obj.get("name", "")).strip() or "Unknown Artist"
            artist_id_str = f"deezer:artist:{artist_obj.get('id')}" if artist_obj.get("id") else None
            cover = (
                data.get("cover_xl")
                or data.get("cover_big")
                or data.get("cover_medium")
                or data.get("cover")
                or None
            )
            rel_date = data.get("release_date") or None
            label = data.get("label") or None

            genres_obj = data.get("genres") if isinstance(data.get("genres"), dict) else {}
            genres_list = genres_obj.get("data", []) if isinstance(genres_obj.get("data"), list) else []
            genres = [
                str(g.get("name")).strip()
                for g in genres_list
                if isinstance(g, dict) and g.get("name")
            ]

            duration_sec = int(data.get("duration", 0) or 0)
            nb_tracks = int(data.get("nb_tracks", 0) or 0)

            tracks_obj = data.get("tracks") if isinstance(data.get("tracks"), dict) else {}
            tracks_raw = tracks_obj.get("data", []) if isinstance(tracks_obj.get("data"), list) else []
            parsed_tracks: list[dict[str, Any]] = []

            for idx, t in enumerate(tracks_raw, start=1):
                t_id = t.get("id")
                t_title = str(t.get("title", "")).strip()
                t_art = t.get("artist") if isinstance(t.get("artist"), dict) else {}
                t_art_name = str(t_art.get("name", "")).strip() or artist_name
                t_pos = int(t.get("track_position") or t.get("track_number") or idx)
                d_num = int(t.get("disk_number") or 1)
                t_dur = int(t.get("duration", 0) or 0)
                t_prev = t.get("preview") or None

                parsed_tracks.append(
                    {
                        "id": f"deezer:track:{t_id}",
                        "item_type": "track",
                        "title": t_title,
                        "artist": t_art_name,
                        "album": title,
                        "track_number": t_pos,
                        "disc_number": d_num,
                        "duration_seconds": t_dur,
                        "preview_url": t_prev,
                        "release_date": rel_date,
                        "album_discovery_id": f"deezer:album:{alb_id}",
                    }
                )

            if duration_sec == 0 and parsed_tracks:
                duration_sec = sum(pt["duration_seconds"] for pt in parsed_tracks)
            if nb_tracks == 0:
                nb_tracks = len(parsed_tracks)

            return {
                "id": f"deezer:album:{alb_id}",
                "item_type": "album",
                "title": title,
                "artist": artist_name,
                "artist_id": artist_id_str,
                "cover_url": cover,
                "release_date": rel_date,
                "label": label,
                "genres": genres,
                "duration_seconds": duration_sec,
                "track_count": nb_tracks,
                "tracks": parsed_tracks,
            }
        except Exception as e:
            logger.warning("Error fetching Deezer album %s: %s", num_id, e)
            return None

    def _get_itunes_album_details(self, num_id: str) -> Optional[dict[str, Any]]:
        """Queries iTunes lookup API endpoint for album details and songs."""
        url = f"https://itunes.apple.com/lookup?id={num_id}&entity=song"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                logger.warning("iTunes album query returned status %d for id %s", resp.status_code, num_id)
                return None
            data = resp.json()
            if not isinstance(data, dict):
                return None
            results = data.get("results", [])
            if not isinstance(results, list) or not results:
                return None

            col = next((r for r in results if r.get("wrapperType") == "collection"), results[0])
            col_id = col.get("collectionId")
            if not col_id:
                return None

            title = str(col.get("collectionName", "")).strip()
            artist_name = str(col.get("artistName", "")).strip() or "Unknown Artist"
            artist_id_str = f"itunes:artist:{col.get('artistId')}" if col.get("artistId") else None
            cover = col.get("artworkUrl100", "")
            if cover:
                cover = cover.replace("100x100bb", "600x600bb")
            else:
                cover = None
            rel_date = col.get("releaseDate") or None
            label = col.get("copyright") or None
            primary_genre = col.get("primaryGenreName")
            genres = [str(primary_genre).strip()] if primary_genre else []
            nb_tracks = int(col.get("trackCount", 0) or 0)

            song_results = [
                r for r in results if r.get("wrapperType") == "track" or r.get("kind") == "song"
            ]
            parsed_tracks: list[dict[str, Any]] = []
            for idx, r in enumerate(song_results, start=1):
                t_id = r.get("trackId")
                t_title = str(r.get("trackName", "")).strip()
                t_artist = str(r.get("artistName", "")).strip() or artist_name
                t_pos = int(r.get("trackNumber") or idx)
                d_num = int(r.get("discNumber") or 1)
                millis = r.get("trackTimeMillis", 0) or 0
                t_dur = int(round(millis / 1000.0))
                t_prev = r.get("previewUrl") or None

                parsed_tracks.append(
                    {
                        "id": f"itunes:track:{t_id}",
                        "item_type": "track",
                        "title": t_title,
                        "artist": t_artist,
                        "album": title,
                        "track_number": t_pos,
                        "disc_number": d_num,
                        "duration_seconds": t_dur,
                        "preview_url": t_prev,
                        "release_date": r.get("releaseDate") or rel_date,
                        "album_discovery_id": f"itunes:album:{col_id}",
                    }
                )

            duration_sec = sum(pt["duration_seconds"] for pt in parsed_tracks)
            if nb_tracks == 0:
                nb_tracks = len(parsed_tracks)

            return {
                "id": f"itunes:album:{col_id}",
                "item_type": "album",
                "title": title,
                "artist": artist_name,
                "artist_id": artist_id_str,
                "cover_url": cover,
                "release_date": rel_date,
                "label": label,
                "genres": genres,
                "duration_seconds": duration_sec,
                "track_count": nb_tracks,
                "tracks": parsed_tracks,
            }
        except Exception as e:
            logger.warning("Error fetching iTunes album %s: %s", num_id, e)
            return None

    def _get_deezer_artist_details(self, num_id: str) -> Optional[dict[str, Any]]:
        """Queries Deezer artist and discography endpoints."""
        url = f"https://api.deezer.com/artist/{num_id}"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                logger.warning("Deezer artist query returned status %d for id %s", resp.status_code, num_id)
                return None
            data = resp.json()
            if not isinstance(data, dict) or "error" in data:
                return None

            art_id = data.get("id")
            if not art_id:
                return None

            name = str(data.get("name", "")).strip() or "Unknown Artist"
            image_url = (
                data.get("picture_xl")
                or data.get("picture_big")
                or data.get("picture_medium")
                or data.get("picture")
                or None
            )
            nb_album = int(data.get("nb_album", 0) or 0)
            nb_fan = int(data.get("nb_fan", 0) or 0)

            # Query albums
            albums_url = f"https://api.deezer.com/artist/{num_id}/albums?limit=100"
            resp_albs = self.session.get(albums_url, timeout=self.timeout)
            albs_data = resp_albs.json() if resp_albs.status_code == 200 else {}
            raw_albs = albs_data.get("data", []) if isinstance(albs_data, dict) else []

            albums: list[dict[str, Any]] = []
            singles_eps: list[dict[str, Any]] = []
            compilations: list[dict[str, Any]] = []

            for a in raw_albs:
                a_id = a.get("id")
                a_title = str(a.get("title", "")).strip()
                a_cover = (
                    a.get("cover_xl")
                    or a.get("cover_big")
                    or a.get("cover_medium")
                    or a.get("cover")
                    or None
                )
                a_rel = a.get("release_date") or None
                rec_type = str(a.get("record_type", "")).lower().strip()

                item = {
                    "id": f"deezer:album:{a_id}",
                    "item_type": "album",
                    "title": a_title,
                    "artist": name,
                    "album": a_title,
                    "cover_url": a_cover,
                    "release_date": a_rel,
                    "record_type": rec_type or "album",
                    # Deezer's artist-albums listing may omit nb_tracks; None means unknown (never 0).
                    "track_count": positive_int(a.get("nb_tracks")),
                }

                if rec_type == "album":
                    albums.append(item)
                elif rec_type in ("single", "ep"):
                    singles_eps.append(item)
                elif rec_type in ("compile", "compilation"):
                    compilations.append(item)
                else:
                    if "single" in rec_type or "ep" in rec_type:
                        singles_eps.append(item)
                    elif "compile" in rec_type or "compilation" in rec_type:
                        compilations.append(item)
                    else:
                        albums.append(item)

            if nb_album == 0:
                nb_album = len(albums) + len(singles_eps) + len(compilations)

            return {
                "id": f"deezer:artist:{art_id}",
                "name": name,
                "image_url": image_url,
                "nb_album": nb_album,
                "nb_fan": nb_fan,
                "albums": albums,
                "singles_eps": singles_eps,
                "compilations": compilations,
            }
        except Exception as e:
            logger.warning("Error fetching Deezer artist %s: %s", num_id, e)
            return None

    def _get_itunes_artist_details(self, num_id: str) -> Optional[dict[str, Any]]:
        """Queries iTunes lookup API endpoint for artist and discography."""
        url = f"https://itunes.apple.com/lookup?id={num_id}&entity=album&limit=100"
        try:
            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                logger.warning("iTunes artist query returned status %d for id %s", resp.status_code, num_id)
                return None
            data = resp.json()
            if not isinstance(data, dict):
                return None
            results = data.get("results", [])
            if not isinstance(results, list) or not results:
                return None

            art_entry = next((r for r in results if r.get("wrapperType") == "artist"), None)
            if art_entry:
                name = str(art_entry.get("artistName", "")).strip()
                art_id = art_entry.get("artistId")
            else:
                name = str(results[0].get("artistName", "")).strip() or "Unknown Artist"
                art_id = results[0].get("artistId") or num_id

            col_results = [r for r in results if r.get("wrapperType") == "collection"]
            albums: list[dict[str, Any]] = []
            singles_eps: list[dict[str, Any]] = []
            compilations: list[dict[str, Any]] = []
            image_url: Optional[str] = None

            for c in col_results:
                c_id = c.get("collectionId")
                c_title = str(c.get("collectionName", "")).strip()
                c_artist = str(c.get("artistName", "")).strip() or name
                cover = c.get("artworkUrl100", "")
                if cover:
                    cover = cover.replace("100x100bb", "600x600bb")
                else:
                    cover = None

                if not image_url and cover:
                    image_url = cover

                rel_date = c.get("releaseDate") or None
                track_count = positive_int(c.get("trackCount")) or 0
                col_type = str(c.get("collectionType", "")).lower()
                title_lower = c_title.lower()

                if (
                    col_type == "compilation"
                    or "compilation" in title_lower
                    or "greatest hits" in title_lower
                    or "best of" in title_lower
                ):
                    rec_type = "compilation"
                    compilations.append(
                        {
                            "id": f"itunes:album:{c_id}",
                            "item_type": "album",
                            "title": c_title,
                            "artist": c_artist,
                            "album": c_title,
                            "cover_url": cover,
                            "release_date": rel_date,
                            "record_type": rec_type,
                            "track_count": track_count or None,
                        }
                    )
                elif (
                    " - single" in title_lower
                    or " - ep" in title_lower
                    or "(single)" in title_lower
                    or "(ep)" in title_lower
                    or (0 < track_count <= 3)
                ):
                    rec_type = "single" if ("single" in title_lower or track_count <= 2) else "ep"
                    singles_eps.append(
                        {
                            "id": f"itunes:album:{c_id}",
                            "item_type": "album",
                            "title": c_title,
                            "artist": c_artist,
                            "album": c_title,
                            "cover_url": cover,
                            "release_date": rel_date,
                            "record_type": rec_type,
                            "track_count": track_count or None,
                        }
                    )
                else:
                    rec_type = "album"
                    albums.append(
                        {
                            "id": f"itunes:album:{c_id}",
                            "item_type": "album",
                            "title": c_title,
                            "artist": c_artist,
                            "album": c_title,
                            "cover_url": cover,
                            "release_date": rel_date,
                            "record_type": rec_type,
                            "track_count": track_count or None,
                        }
                    )

            total_albums = len(albums) + len(singles_eps) + len(compilations)
            return {
                "id": f"itunes:artist:{art_id}",
                "name": name,
                "image_url": image_url,
                "nb_album": total_albums,
                "nb_fan": 0,
                "albums": albums,
                "singles_eps": singles_eps,
                "compilations": compilations,
            }
        except Exception as e:
            logger.warning("Error fetching iTunes artist %s: %s", num_id, e)
            return None

"""MusicBrainz (MBID) and BrainzMash metadata enrichment client with in-memory TTL caching."""

import logging
import os
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Optional
import urllib.parse

import requests

from plex_playlist_sync.system_paths import is_system_folder_name

if TYPE_CHECKING:
    from plex_playlist_sync.mb_metadata_store import MbMetadataStore

logger = logging.getLogger(__name__)


def _sanitize_lucene_query(text: str) -> str:
    """Removes or escapes characters that disrupt Lucene query parsing."""
    if not text:
        return ""
    # Strip double quotes, backslashes, and control characters
    return re.sub(r'["\\/]', " ", text).strip()


class _CircuitBreaker:
    """Thread-safe circuit breaker tracking consecutive failures per host."""

    def __init__(
        self,
        host: str,
        failure_threshold: int = 5,
        cooldown_seconds: float = 300.0,
    ) -> None:
        self.host = host
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._state: str = "closed"  # "closed", "open", "half_open"
        self._consecutive_failures: int = 0
        self._last_failure_time: float = 0.0
        self._half_open_in_flight: bool = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            self._check_cooldown_locked()
            return self._state

    def _check_cooldown_locked(self) -> None:
        if self._state == "open":
            now = time.monotonic()
            if now - self._last_failure_time >= self.cooldown_seconds:
                self._state = "half_open"
                self._half_open_in_flight = False

    def can_attempt(self) -> bool:
        """Returns True if a request can be attempted under breaker policy."""
        with self._lock:
            self._check_cooldown_locked()
            if self._state == "closed":
                return True
            if self._state == "half_open":
                if not self._half_open_in_flight:
                    self._half_open_in_flight = True
                    return True
                return False
            return False

    def record_result(self, is_failure: bool) -> None:
        """Records outcome of an attempted call."""
        with self._lock:
            now = time.monotonic()
            if is_failure:
                self._consecutive_failures += 1
                self._last_failure_time = now
                if self._state == "half_open" or self._consecutive_failures >= self.failure_threshold:
                    if self._state != "open":
                        logger.warning(
                            "Circuit breaker opened for host %s after %d consecutive failures (cooldown=%ss)",
                            self.host,
                            self._consecutive_failures,
                            self.cooldown_seconds,
                        )
                    self._state = "open"
                    self._half_open_in_flight = False
            else:
                was_not_closed = (self._state != "closed")
                self._consecutive_failures = 0
                self._state = "closed"
                self._half_open_in_flight = False
                if was_not_closed:
                    logger.info("Circuit breaker closed for host %s after successful response", self.host)


def _extract_host(url: str) -> str:
    """Extracts host / netloc from URL string."""
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc or url


def _is_breaker_failure(resp: Optional[requests.Response]) -> bool:
    """Returns True if response constitutes a circuit breaker failure."""
    if resp is None:
        return True
    return resp.status_code in (429, 500, 502, 503, 504)


class MbidEnricherClient:
    """High-speed cached MBID resolver querying MusicBrainz REST mirrors and Cover Art Archive."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 3.0,
        cache_ttl: float = 3600.0,
        min_interval: float = 1.0,
        store: Optional["MbMetadataStore"] = None,
    ) -> None:
        default_base = (
            os.getenv("MUSICBRAINZ_URL")
            or os.getenv("MUSICBRAINZ_MIRROR_URL")
            or "https://api.brainzmash.cc"
        )
        self.base_url = (base_url or default_base).rstrip("/")
        self.timeout = float(timeout)
        self.cache_ttl = float(cache_ttl)
        self._min_interval = float(min_interval)
        self._last_request_time: float = 0.0
        self._rate_limit_lock = threading.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()
        self._store = store
        self._network_requests: int = 0
        self._cache_hits: int = 0
        self._breakers: dict[str, _CircuitBreaker] = {}
        mirror_host = _extract_host(self.base_url)
        self._breakers[mirror_host] = _CircuitBreaker(mirror_host)
        if mirror_host != "musicbrainz.org":
            self._breakers["musicbrainz.org"] = _CircuitBreaker("musicbrainz.org")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": "Lidarr/2.0.0 (TrackSeerr; https://github.com/trackseerr)",
                "Accept": "application/json",
            }
        )

    def _get_breaker(self, host: str) -> _CircuitBreaker:
        with self._lock:
            if host not in self._breakers:
                self._breakers[host] = _CircuitBreaker(host)
            return self._breakers[host]

    def source_available(self, host: Optional[str] = None) -> bool:
        """Returns False only when the specified host (or every usable host) has an open circuit breaker."""
        with self._lock:
            if host is not None:
                clean_host = _extract_host(host)
                if clean_host not in self._breakers:
                    self._breakers[clean_host] = _CircuitBreaker(clean_host)
                return self._breakers[clean_host].state != "open"
            mirror_host = _extract_host(self.base_url)
            usable_hosts = [mirror_host]
            if mirror_host != "musicbrainz.org":
                usable_hosts.append("musicbrainz.org")
            return any(
                self._breakers[h].state != "open"
                for h in usable_hosts
                if h in self._breakers
            )

    def stats(self) -> dict[str, Any]:
        """Returns thread-safe network request, cache hit counters, and circuit breaker states."""
        with self._lock:
            breaker_states = {
                host: breaker.state for host, breaker in self._breakers.items()
            }
            return {
                "network_requests": self._network_requests,
                "cache_hits": self._cache_hits,
                "breaker_state": breaker_states,
            }

    def _rate_limit(self, url: str) -> None:
        """Enforces rate limiting (1 req/sec) when communicating with musicbrainz.org."""
        if "musicbrainz.org" in url and self._min_interval > 0:
            with self._rate_limit_lock:
                now = time.time()
                elapsed = now - self._last_request_time
                if elapsed < self._min_interval:
                    time.sleep(self._min_interval - elapsed)
                self._last_request_time = time.time()

    def _do_http_call(
        self, url: str, params: Optional[dict[str, Any]] = None, max_retries: int = 1
    ) -> Optional[requests.Response]:
        """Executes a single HTTP request with rate limiting and exponential backoff for 429/503."""
        for attempt in range(max_retries + 1):
            self._rate_limit(url)
            try:
                with self._lock:
                    self._network_requests += 1
                resp = self._session.get(url, params=params, timeout=self.timeout)
                if resp.status_code in (429, 503):
                    logger.warning(
                        "MbidEnricherClient: HTTP %d received for %s (attempt %d/%d)",
                        resp.status_code,
                        url,
                        attempt + 1,
                        max_retries + 1,
                    )
                    if attempt < max_retries:
                        retry_after = 0.5
                        try:
                            retry_hdr = resp.headers.get("Retry-After") if resp.headers else None
                            if retry_hdr:
                                retry_after = float(retry_hdr)
                        except (ValueError, TypeError):
                            pass
                        max_wait = 30.0 if "musicbrainz.org" in url else 1.0
                        time.sleep(min(retry_after, max_wait))
                        continue
                    return resp
                return resp
            except requests.RequestException as exc:
                logger.warning(
                    "MbidEnricherClient: Network error for %s (attempt %d/%d): %s",
                    url,
                    attempt + 1,
                    max_retries + 1,
                    exc,
                )
                if attempt < max_retries:
                    time.sleep(0.2)
                    continue
                return None
            except Exception as exc:
                logger.warning("MbidEnricherClient: Unexpected error for %s: %s", url, exc)
                return None
        return None

    def _request(
        self, url: str, params: Optional[dict[str, Any]] = None, max_retries: int = 1
    ) -> Optional[requests.Response]:
        """Executes HTTP request with circuit breakers and fallback to https://musicbrainz.org."""
        mirror_host = _extract_host(self.base_url)
        is_mirror_base = (self.base_url != "https://musicbrainz.org" and mirror_host != "musicbrainz.org")

        if not is_mirror_base:
            mb_breaker = self._get_breaker("musicbrainz.org")
            if not mb_breaker.can_attempt():
                return None
            resp = self._do_http_call(url, params=params, max_retries=max_retries)
            mb_breaker.record_result(_is_breaker_failure(resp))
            return resp

        mirror_breaker = self._get_breaker(mirror_host)
        mb_breaker = self._get_breaker("musicbrainz.org")
        fallback_url = "https://musicbrainz.org" + url[len(self.base_url):] if url.startswith(self.base_url) else None

        if not mirror_breaker.can_attempt():
            if fallback_url is None or not mb_breaker.can_attempt():
                return None
            fallback_resp = self._do_http_call(fallback_url, params=params, max_retries=max_retries)
            mb_breaker.record_result(_is_breaker_failure(fallback_resp))
            return fallback_resp

        resp = self._do_http_call(url, params=params, max_retries=max_retries)
        mirror_breaker.record_result(_is_breaker_failure(resp))

        if resp is not None and resp.status_code == 200:
            return resp

        is_error = resp is None or resp.status_code in (403, 500, 502, 503, 504)
        if is_error and fallback_url is not None:
            if not mb_breaker.can_attempt():
                return resp
            logger.warning(
                "MbidEnricherClient: Mirror request failed (status=%s), falling back to %s",
                resp.status_code if resp is not None else "None",
                fallback_url,
            )
            fallback_resp = self._do_http_call(fallback_url, params=params, max_retries=max_retries)
            mb_breaker.record_result(_is_breaker_failure(fallback_resp))
            if fallback_resp is not None and fallback_resp.status_code == 200:
                return fallback_resp
            return fallback_resp

        return resp

    def _get_cached(self, key: str) -> tuple[bool, Any]:
        with self._lock:
            if key in self._cache:
                timestamp, data = self._cache[key]
                if time.time() - timestamp < self.cache_ttl:
                    return True, data
                del self._cache[key]
            return False, None

    def _set_cached(self, key: str, data: Any) -> None:
        with self._lock:
            self._cache[key] = (time.time(), data)

    def _lookup(self, key: str, force: bool = False) -> tuple[bool, Any]:
        """Lookup in L1 in-memory cache, then L2 persistent store. Increments cache_hits on hit."""
        if force:
            return False, None
        hit, data = self._get_cached(key)
        if hit:
            with self._lock:
                self._cache_hits += 1
            return True, data
        if self._store is not None:
            hit, data = self._store.get(key)
            if hit:
                self._set_cached(key, data)
                with self._lock:
                    self._cache_hits += 1
                return True, data
        return False, None

    def _remember(
        self, key: str, kind: str, data: Any, ttl: Optional[float] = None
    ) -> None:
        """Stores parsed 200 / 404 result in L1 and (if store present) L2 with appropriate TTL."""
        from plex_playlist_sync.mb_metadata_store import (
            TTL_ARTIST_DETAILS,
            TTL_DISCOGRAPHY,
            TTL_LOOKUP,
            TTL_NEGATIVE,
            TTL_RG_LOOKUP,
            TTL_RG_TRACKS,
            ttl_with_jitter,
        )

        self._set_cached(key, data)
        if self._store is not None:
            if data is None or data == []:
                store_ttl = TTL_NEGATIVE
            elif ttl is not None:
                store_ttl = ttl_with_jitter(float(ttl))
            else:
                default_ttls = {
                    "artist_details": TTL_ARTIST_DETAILS,
                    "discography": TTL_DISCOGRAPHY,
                    "rg_tracks": TTL_RG_TRACKS,
                    "rg_lookup": TTL_RG_LOOKUP,
                }
                base_ttl = default_ttls.get(kind, TTL_LOOKUP)
                store_ttl = ttl_with_jitter(base_ttl)
            self._store.put(key, kind, data, store_ttl)

    def lookup_track_mbids(
        self,
        artist: str,
        album: str,
        title: str,
        isrc: Optional[str] = None,
    ) -> Optional[dict[str, Optional[str]]]:
        """Looks up recording, artist, album, and release group MBIDs by ISRC or title/artist/album."""
        clean_artist = _sanitize_lucene_query(artist)
        clean_album = _sanitize_lucene_query(album)
        clean_title = _sanitize_lucene_query(title)
        clean_isrc = (isrc or "").strip().upper()

        cache_key = f"track:{clean_artist.lower()}:{clean_album.lower()}:{clean_title.lower()}:{clean_isrc}"
        hit, cached_data = self._lookup(cache_key)
        if hit:
            return cached_data

        try:
            recordings = None
            had_success_200 = False

            # 1. Try ISRC lookup first if present
            if clean_isrc:
                url = f"{self.base_url}/ws/2/recording"
                params = {"query": f"isrc:{clean_isrc}", "fmt": "json"}
                resp = self._request(url, params=params)
                if resp is not None and resp.status_code == 200:
                    try:
                        data = resp.json()
                        if isinstance(data, dict):
                            recordings = data.get("recordings") or []
                            had_success_200 = True
                    except ValueError as exc:
                        logger.warning(
                            "MbidEnricherClient: lookup_track_mbids JSON decode failed for %s (%s): %s",
                            url,
                            cache_key,
                            exc,
                        )
                elif resp is not None and resp.status_code == 404:
                    had_success_200 = True
                    recordings = []

            # 2. If no ISRC match, search by recording, artist, and release
            if not recordings and clean_title:
                url = f"{self.base_url}/ws/2/recording"
                query_parts = [f'recording:"{clean_title}"']
                if clean_artist:
                    query_parts.append(f'artist:"{clean_artist}"')
                if clean_album:
                    query_parts.append(f'release:"{clean_album}"')
                query_str = " AND ".join(query_parts)
                params = {"query": query_str, "fmt": "json"}
                resp = self._request(url, params=params)
                if resp is not None and resp.status_code == 200:
                    try:
                        data = resp.json()
                        if isinstance(data, dict):
                            recordings = data.get("recordings") or []
                            had_success_200 = True
                    except ValueError as exc:
                        logger.warning(
                            "MbidEnricherClient: lookup_track_mbids JSON decode failed for %s (%s): %s",
                            url,
                            cache_key,
                            exc,
                        )
                elif resp is not None and resp.status_code == 404:
                    had_success_200 = True
                    recordings = []

            if not recordings:
                if had_success_200:
                    self._remember(cache_key, "track_lookup", None)
                else:
                    self._set_cached(cache_key, None)
                return None

            rec = recordings[0]
            if not isinstance(rec, dict):
                self._set_cached(cache_key, None)
                return None

            musicbrainz_trackid = rec.get("id")

            musicbrainz_artistid: Optional[str] = None
            artist_credit = rec.get("artist-credit") or []
            if artist_credit and isinstance(artist_credit, list):
                ac0 = artist_credit[0]
                if isinstance(ac0, dict):
                    art = ac0.get("artist")
                    if isinstance(art, dict):
                        musicbrainz_artistid = art.get("id")
                    elif "id" in ac0:
                        musicbrainz_artistid = ac0.get("id")

            musicbrainz_albumid: Optional[str] = None
            musicbrainz_releasegroupid: Optional[str] = None
            releases = rec.get("releases") or []
            if releases and isinstance(releases, list):
                rel0 = releases[0]
                if isinstance(rel0, dict):
                    musicbrainz_albumid = rel0.get("id")
                    rg = rel0.get("release-group")
                    if isinstance(rg, dict):
                        musicbrainz_releasegroupid = rg.get("id")

            result = {
                "musicbrainz_artistid": musicbrainz_artistid,
                "musicbrainz_albumid": musicbrainz_albumid,
                "musicbrainz_releasegroupid": musicbrainz_releasegroupid,
                "musicbrainz_trackid": musicbrainz_trackid,
            }
            self._remember(cache_key, "track_lookup", result)
            return result

        except Exception as exc:
            logger.warning("MbidEnricherClient: lookup_track_mbids failed for '%s - %s': %s", artist, title, exc)
            self._set_cached(cache_key, None)
            return None

    def lookup_artist_mbid_by_url(self, resource_url: str) -> Optional[str]:
        """Resolves the artist MBID that has a MusicBrainz URL relation to ``resource_url`` (e.g. a Deezer artist page).

        Uses ``/ws/2/url?resource=...&inc=artist-rels``; None when MusicBrainz has no such URL, no artist relation,
        or cannot be reached. Shares the client's rate limit, User-Agent, timeout and cache.
        """
        resource = (resource_url or "").strip()
        if not resource:
            return None
        cache_key = f"urlrel:{resource.lower()}"
        hit, cached_data = self._lookup(cache_key)
        if hit:
            return cached_data

        try:
            resp = self._request(
                f"{self.base_url}/ws/2/url", params={"resource": resource, "inc": "artist-rels", "fmt": "json"}
            )
            if resp is None or resp.status_code != 200:
                if resp is not None and resp.status_code == 404:
                    self._remember(cache_key, "url_lookup", None)
                else:
                    self._set_cached(cache_key, None)
                return None

            try:
                data = resp.json()
            except ValueError as exc:
                logger.warning(
                    "MbidEnricherClient: lookup_artist_mbid_by_url JSON decode failed for %s: %s",
                    cache_key,
                    exc,
                )
                self._set_cached(cache_key, None)
                return None

            if not isinstance(data, dict):
                self._set_cached(cache_key, None)
                return None

            mbid: Optional[str] = None
            for rel in data.get("relations") or []:
                artist = rel.get("artist") if isinstance(rel, dict) else None
                if isinstance(artist, dict) and artist.get("id"):
                    mbid = str(artist["id"])
                    break
            self._remember(cache_key, "url_lookup", mbid)
            return mbid
        except (ValueError, AttributeError, TypeError) as exc:
            logger.warning("MbidEnricherClient: lookup_artist_mbid_by_url failed for '%s': %s", resource, exc)
            self._set_cached(cache_key, None)
            return None
        except Exception as exc:
            logger.warning("MbidEnricherClient: lookup_artist_mbid_by_url unexpected error for '%s': %s", resource, exc)
            self._set_cached(cache_key, None)
            return None

    def lookup_artist_mbid(self, artist_name: str) -> Optional[str]:
        """Queries the mirror for the canonical artist MBID."""
        if is_system_folder_name(artist_name):
            logger.info("MbidEnricherClient: skipping artist lookup for system/trash folder name %r", artist_name)
            return None
        clean_name = _sanitize_lucene_query(artist_name)
        if not clean_name:
            return None

        cache_key = f"artist:{clean_name.lower()}"
        hit, cached_data = self._lookup(cache_key)
        if hit:
            return cached_data

        try:
            url = f"{self.base_url}/ws/2/artist"
            params = {"query": f'artist:"{clean_name}"', "fmt": "json"}
            resp = self._request(url, params=params)
            if resp is None or resp.status_code != 200:
                if resp is not None and resp.status_code == 404:
                    self._remember(cache_key, "artist_lookup", None)
                else:
                    self._set_cached(cache_key, None)
                return None

            try:
                data = resp.json()
            except ValueError as exc:
                logger.warning(
                    "MbidEnricherClient: lookup_artist_mbid JSON decode failed for %s: %s",
                    cache_key,
                    exc,
                )
                self._set_cached(cache_key, None)
                return None

            if not isinstance(data, dict):
                self._set_cached(cache_key, None)
                return None

            artists = data.get("artists") or []
            if not artists:
                self._remember(cache_key, "artist_lookup", None)
                return None

            mbid = artists[0].get("id") if isinstance(artists[0], dict) else None
            self._remember(cache_key, "artist_lookup", mbid)
            return mbid

        except Exception as exc:
            logger.warning("MbidEnricherClient: lookup_artist_mbid failed for '%s': %s", artist_name, exc)
            self._set_cached(cache_key, None)
            return None

    def lookup_album_mbids(
        self, artist_name: str, album_title: str
    ) -> Optional[dict[str, Optional[str]]]:
        """Queries the mirror for release group and artist MBIDs."""
        if is_system_folder_name(artist_name) or is_system_folder_name(album_title):
            logger.info(
                "MbidEnricherClient: skipping album lookup for system/trash folder name %r / %r", artist_name, album_title
            )
            return None
        clean_artist = _sanitize_lucene_query(artist_name)
        clean_album = _sanitize_lucene_query(album_title)
        if not clean_album:
            return None

        cache_key = f"album:{clean_artist.lower()}:{clean_album.lower()}"
        hit, cached_data = self._lookup(cache_key)
        if hit:
            return cached_data

        try:
            url = f"{self.base_url}/ws/2/release-group"
            query_parts = [f'releasegroup:"{clean_album}"']
            if clean_artist:
                query_parts.append(f'artist:"{clean_artist}"')
            params = {"query": " AND ".join(query_parts), "fmt": "json"}
            resp = self._request(url, params=params)
            if resp is None or resp.status_code != 200:
                if resp is not None and resp.status_code == 404:
                    self._remember(cache_key, "album_lookup", None)
                else:
                    self._set_cached(cache_key, None)
                return None

            try:
                data = resp.json()
            except ValueError as exc:
                logger.warning(
                    "MbidEnricherClient: lookup_album_mbids JSON decode failed for %s: %s",
                    cache_key,
                    exc,
                )
                self._set_cached(cache_key, None)
                return None

            if not isinstance(data, dict):
                self._set_cached(cache_key, None)
                return None

            release_groups = data.get("release-groups") or []
            if not release_groups:
                self._remember(cache_key, "album_lookup", None)
                return None

            rg0 = release_groups[0]
            if not isinstance(rg0, dict):
                self._set_cached(cache_key, None)
                return None

            rg_id = rg0.get("id")
            art_id: Optional[str] = None
            artist_credit = rg0.get("artist-credit") or []
            if artist_credit and isinstance(artist_credit, list):
                ac0 = artist_credit[0]
                if isinstance(ac0, dict):
                    art = ac0.get("artist")
                    if isinstance(art, dict):
                        art_id = art.get("id")
                    elif "id" in ac0:
                        art_id = ac0.get("id")

            result = {
                "mb_release_group_id": rg_id,
                "mb_artist_id": art_id,
            }
            self._remember(cache_key, "album_lookup", result)
            return result

        except Exception as exc:
            logger.warning(
                "MbidEnricherClient: lookup_album_mbids failed for '%s - %s': %s",
                artist_name,
                album_title,
                exc,
            )
            self._set_cached(cache_key, None)
            return None

    def get_cover_art_url(
        self, release_group_id: Optional[str] = None, release_id: Optional[str] = None
    ) -> Optional[str]:
        """Resolves high-resolution 500px front cover URL from Cover Art Archive."""
        if release_group_id:
            return f"https://coverartarchive.org/release-group/{release_group_id}/front-500"
        elif release_id:
            return f"https://coverartarchive.org/release/{release_id}/front-500"
        return None

    def search_artist_mbid(self, artist_name: str) -> Optional[str]:
        """Queries the mirror for the canonical artist MBID (alias for lookup_artist_mbid)."""
        return self.lookup_artist_mbid(artist_name)

    def get_artist_details(self, mbid: str, force: bool = False) -> Optional[dict[str, Any]]:
        """Queries the mirror for artist metadata (country, disambiguation, genres, urls)."""
        if not mbid or not str(mbid).strip():
            return None

        clean_mbid = str(mbid).strip()
        if self._store is not None:
            clean_mbid = self._store.resolve_redirect(clean_mbid)

        cache_key = f"artist_details:{clean_mbid.lower()}"
        hit, cached_data = self._lookup(cache_key, force=force)
        if hit:
            return cached_data

        try:
            url = f"{self.base_url}/ws/2/artist/{clean_mbid}"
            params = {"inc": "genres+tags+url-rels", "fmt": "json"}
            resp = self._request(url, params=params)
            if resp is None or resp.status_code != 200:
                if resp is not None and resp.status_code == 404:
                    self._remember(cache_key, "artist_details", None)
                else:
                    self._set_cached(cache_key, None)
                return None

            try:
                data = resp.json()
            except ValueError as exc:
                logger.warning(
                    "MbidEnricherClient: get_artist_details JSON decode failed for %s: %s",
                    clean_mbid,
                    exc,
                )
                self._set_cached(cache_key, None)
                return None

            if not isinstance(data, dict):
                self._set_cached(cache_key, None)
                return None

            resp_id = data.get("id")
            if resp_id and str(resp_id).lower() != clean_mbid.lower() and self._store is not None:
                self._store.record_redirect(clean_mbid, str(resp_id), "artist")

            genre_names: list[str] = []
            for g in (data.get("genres") or []):
                if isinstance(g, dict) and g.get("name"):
                    genre_names.append(str(g["name"]))
                elif isinstance(g, str):
                    genre_names.append(g)

            for t in (data.get("tags") or []):
                if isinstance(t, dict) and t.get("name"):
                    tname = str(t["name"])
                    if tname not in genre_names:
                        genre_names.append(tname)
                elif isinstance(t, str) and t not in genre_names:
                    genre_names.append(t)

            urls: dict[str, str] = {}
            relations = data.get("relations") or []
            if isinstance(relations, list):
                for rel in relations:
                    if isinstance(rel, dict):
                        rel_type = str(rel.get("type") or "").strip().lower()
                        url_obj = rel.get("url")
                        if isinstance(url_obj, dict):
                            resource = url_obj.get("resource")
                            if resource and rel_type:
                                urls[rel_type] = str(resource)

            result: dict[str, Any] = {
                "id": data.get("id") or clean_mbid,
                "name": data.get("name"),
                "country": data.get("country"),
                "disambiguation": data.get("disambiguation"),
                "bio": data.get("disambiguation"),
                "genres": genre_names,
                "urls": urls,
            }
            if resp_id and str(resp_id).lower() != clean_mbid.lower():
                self._remember(f"artist_details:{str(resp_id).lower()}", "artist_details", result)
            self._remember(cache_key, "artist_details", result)
            return result

        except Exception as exc:
            logger.warning(
                "MbidEnricherClient: get_artist_details failed for '%s': %s",
                clean_mbid,
                exc,
            )
            self._set_cached(cache_key, None)
            return None

    def resolve_release_group(self, rg_id: str) -> Optional[str]:
        """Resolves canonical release group MBID, recording redirects if updated."""
        if not rg_id or not str(rg_id).strip():
            return None

        clean_id = str(rg_id).strip()
        if self._store is not None:
            clean_id = self._store.resolve_redirect(clean_id)

        cache_key = f"rg_lookup:{clean_id.lower()}"
        hit, cached_data = self._lookup(cache_key)
        if hit:
            return cached_data

        try:
            url = f"{self.base_url}/ws/2/release-group/{clean_id}"
            params = {"fmt": "json"}
            resp = self._request(url, params=params)
            if resp is None or resp.status_code != 200:
                if resp is not None and resp.status_code == 404:
                    self._remember(cache_key, "rg_lookup", None)
                else:
                    self._set_cached(cache_key, None)
                return None

            try:
                data = resp.json()
            except ValueError as exc:
                logger.warning(
                    "MbidEnricherClient: resolve_release_group JSON decode failed for %s: %s",
                    clean_id,
                    exc,
                )
                self._set_cached(cache_key, None)
                return None

            if not isinstance(data, dict):
                self._set_cached(cache_key, None)
                return None

            resp_id = data.get("id")
            if not resp_id:
                self._set_cached(cache_key, None)
                return None

            canonical_id = str(resp_id).strip()
            if canonical_id.lower() != clean_id.lower() and self._store is not None:
                self._store.record_redirect(clean_id, canonical_id, "release_group")
                self._remember(f"rg_lookup:{canonical_id.lower()}", "rg_lookup", canonical_id)
            self._remember(cache_key, "rg_lookup", canonical_id)
            return canonical_id
        except Exception as exc:
            logger.warning("MbidEnricherClient: resolve_release_group failed for '%s': %s", clean_id, exc)
            self._set_cached(cache_key, None)
            return None

    def get_artist_discography_result(
        self, mbid: str, limit: int = 100, force: bool = False
    ) -> tuple[list[dict[str, Any]], bool]:
        """Paginates artist release groups. Returns (release_groups, complete)."""
        if not mbid or not str(mbid).strip():
            return [], False

        clean_mbid = str(mbid).strip()
        if self._store is not None:
            clean_mbid = self._store.resolve_redirect(clean_mbid)

        cache_key = f"discography:{clean_mbid.lower()}"
        hit, cached_data = self._lookup(cache_key, force=force)
        if hit and isinstance(cached_data, list):
            return cached_data, True

        page_size = max(1, min(int(limit), 100))
        all_items: list[dict[str, Any]] = []
        max_pages = 25
        page_num = 0
        complete = False

        try:
            while page_num < max_pages:
                offset = page_num * page_size
                url = f"{self.base_url}/ws/2/release-group"
                params = {"artist": clean_mbid, "limit": page_size, "offset": offset, "fmt": "json"}
                resp = self._request(url, params=params)
                if resp is None or resp.status_code != 200:
                    if page_num == 0:
                        return [], False
                    logger.warning(
                        "MbidEnricherClient: discography page failed for artist %s at offset %d (status=%s)",
                        clean_mbid,
                        offset,
                        resp.status_code if resp is not None else "None",
                    )
                    return all_items, False

                try:
                    data = resp.json()
                except Exception as exc:
                    if page_num == 0:
                        return [], False
                    logger.warning(
                        "MbidEnricherClient: discography JSON decode failed for artist %s at offset %d: %s",
                        clean_mbid,
                        offset,
                        exc,
                    )
                    return all_items, False

                if not isinstance(data, dict):
                    if page_num == 0:
                        return [], False
                    logger.warning(
                        "MbidEnricherClient: discography non-dict JSON for artist %s at offset %d",
                        clean_mbid,
                        offset,
                    )
                    return all_items, False

                release_groups = data.get("release-groups") or []
                if not isinstance(release_groups, list) or not release_groups:
                    complete = True
                    break

                for rg in release_groups:
                    if not isinstance(rg, dict):
                        continue
                    rg_id = str(rg.get("id") or "")
                    title = str(rg.get("title") or "Unknown Album")
                    primary_type = str(rg.get("primary-type") or "Album")
                    raw_secondary = rg.get("secondary-types") or []
                    secondary_types = [str(st).lower() for st in raw_secondary if st]

                    first_release_date = rg.get("first-release-date")
                    year: Optional[int] = None
                    if first_release_date:
                        date_str = str(first_release_date).strip()
                        if len(date_str) >= 4 and date_str[:4].isdigit():
                            year = int(date_str[:4])

                    pt_lower = primary_type.lower()
                    st_set = set(secondary_types)

                    if "live" in st_set:
                        album_type = "live"
                    elif bool(st_set.intersection({"compilation", "soundtrack", "remix"})):
                        album_type = "compilation"
                    elif pt_lower in ("single", "ep"):
                        album_type = pt_lower
                    else:
                        album_type = "album"

                    cover_url = (
                        f"https://coverartarchive.org/release-group/{rg_id}/front-500"
                        if rg_id
                        else None
                    )

                    all_items.append(
                        {
                            "id": rg_id,
                            "title": title,
                            "primary_type": primary_type,
                            "secondary_types": secondary_types,
                            "first_release_date": first_release_date,
                            "year": year,
                            "album_type": album_type,
                            "cover_url": cover_url,
                        }
                    )

                rg_count_raw = data.get("release-group-count")
                if rg_count_raw is not None:
                    try:
                        total_count = int(rg_count_raw)
                    except (ValueError, TypeError):
                        total_count = len(release_groups)
                else:
                    total_count = len(release_groups)

                page_num += 1
                if (page_num * page_size) >= total_count or len(release_groups) < page_size:
                    complete = True
                    break

            if not complete and page_num >= max_pages:
                logger.warning(
                    "MbidEnricherClient: discography paging hard cap (25 pages) reached for artist %s",
                    clean_mbid,
                )

            if complete:
                self._remember(cache_key, "discography", all_items)
            return all_items, complete

        except Exception as exc:
            logger.warning(
                "MbidEnricherClient: get_artist_discography failed for '%s': %s",
                clean_mbid,
                exc,
            )
            return all_items if page_num > 0 else [], False

    def get_artist_discography(
        self, mbid: str, limit: int = 100, force: bool = False
    ) -> list[dict[str, Any]]:
        """Queries the mirror for full artist release groups and categorizes release types."""
        items, _ = self.get_artist_discography_result(mbid, limit=limit, force=force)
        return items

    def get_release_group_tracks(
        self, release_group_id: str, force: bool = False
    ) -> list[dict[str, Any]]:
        """Queries MusicBrainz / BrainzMash for canonical tracks within a release group."""
        if not release_group_id or not str(release_group_id).strip():
            return []

        clean_rg_id = str(release_group_id).strip()
        if self._store is not None:
            clean_rg_id = self._store.resolve_redirect(clean_rg_id)

        cache_key = f"tracks:rg:{clean_rg_id.lower()}"
        hit, cached_data = self._lookup(cache_key, force=force)
        if hit and isinstance(cached_data, list):
            return cached_data

        try:
            url = f"{self.base_url}/ws/2/release"
            params = {
                "release-group": clean_rg_id,
                "inc": "recordings",
                "limit": 1,
                "fmt": "json",
            }
            resp = self._request(url, params=params)
            if resp is None or resp.status_code != 200:
                if resp is not None and resp.status_code == 404:
                    self._remember(cache_key, "rg_tracks", [])
                else:
                    self._set_cached(cache_key, [])
                return []

            try:
                data = resp.json()
            except ValueError as exc:
                logger.warning(
                    "MbidEnricherClient: get_release_group_tracks JSON decode failed for %s: %s",
                    cache_key,
                    exc,
                )
                self._set_cached(cache_key, [])
                return []

            if not isinstance(data, dict):
                self._set_cached(cache_key, [])
                return []

            releases = data.get("releases") or []
            if not releases or not isinstance(releases, list):
                self._remember(cache_key, "rg_tracks", [])
                return []

            first_release = releases[0]
            if not isinstance(first_release, dict):
                self._set_cached(cache_key, [])
                return []

            media = first_release.get("media") or []
            results: list[dict[str, Any]] = []

            for medium in media:
                if not isinstance(medium, dict):
                    continue
                try:
                    disc_number = int(medium.get("position") or 1)
                except (ValueError, TypeError):
                    disc_number = 1

                tracks = medium.get("tracks") or []
                for track in tracks:
                    if not isinstance(track, dict):
                        continue
                    try:
                        track_number = int(track.get("position") or 1)
                    except (ValueError, TypeError):
                        track_number = 1

                    title = str(track.get("title") or "Unknown Track").strip()
                    length_ms = track.get("length")
                    duration_seconds: Optional[float] = None
                    if length_ms is not None:
                        try:
                            duration_seconds = round(float(length_ms) / 1000.0, 2)
                        except (ValueError, TypeError):
                            duration_seconds = None

                    rec_obj = track.get("recording")
                    mb_recording_id: Optional[str] = None
                    if isinstance(rec_obj, dict) and rec_obj.get("id"):
                        mb_recording_id = str(rec_obj["id"]).strip() or None

                    results.append(
                        {
                            "track_number": track_number,
                            "disc_number": disc_number,
                            "title": title,
                            "duration_seconds": duration_seconds,
                            "mb_recording_id": mb_recording_id,
                        }
                    )

            self._remember(cache_key, "rg_tracks", results)
            return results

        except Exception as exc:
            logger.warning(
                "MbidEnricherClient: get_release_group_tracks failed for '%s': %s",
                clean_rg_id,
                exc,
            )
            self._set_cached(cache_key, [])
            return []

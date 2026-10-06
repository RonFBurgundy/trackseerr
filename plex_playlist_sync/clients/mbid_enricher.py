"""MusicBrainz (MBID) and BrainzMash metadata enrichment client with in-memory TTL caching."""

import logging
import os
import re
import threading
import time
from typing import Any, Optional
import urllib.parse

import requests

from plex_playlist_sync.system_paths import is_system_folder_name

logger = logging.getLogger(__name__)


def _sanitize_lucene_query(text: str) -> str:
    """Removes or escapes characters that disrupt Lucene query parsing."""
    if not text:
        return ""
    # Strip double quotes, backslashes, and control characters
    return re.sub(r'["\\/]', " ", text).strip()


class MbidEnricherClient:
    """High-speed cached MBID resolver querying MusicBrainz REST mirrors and Cover Art Archive."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 3.0,
        cache_ttl: float = 3600.0,
        min_interval: float = 1.0,
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
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": "Lidarr/2.0.0 (TrackSeerr; https://github.com/trackseerr)",
                "Accept": "application/json",
            }
        )

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
                        time.sleep(min(retry_after, 1.0))
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
        """Executes HTTP request with resilient fallback to https://musicbrainz.org upon error/403/5xx."""
        resp = self._do_http_call(url, params=params, max_retries=max_retries)
        if resp is not None and resp.status_code == 200:
            return resp

        # Resilient fallback: if request to self.base_url returns a network error, 403, or 5xx
        is_error = resp is None or resp.status_code in (403, 500, 502, 503, 504)
        if is_error and self.base_url != "https://musicbrainz.org" and url.startswith(self.base_url):
            fallback_url = "https://musicbrainz.org" + url[len(self.base_url):]
            logger.warning(
                "MbidEnricherClient: Mirror request failed (status=%s), falling back to %s",
                resp.status_code if resp is not None else "None",
                fallback_url,
            )
            fallback_resp = self._do_http_call(fallback_url, params=params, max_retries=max_retries)
            if fallback_resp is not None and fallback_resp.status_code == 200:
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
        hit, cached_data = self._get_cached(cache_key)
        if hit:
            return cached_data

        try:
            # 1. Try ISRC lookup first if present
            recordings = []
            if clean_isrc:
                url = f"{self.base_url}/ws/2/recording"
                params = {"query": f"isrc:{clean_isrc}", "fmt": "json"}
                resp = self._request(url, params=params)
                if resp is not None and resp.status_code == 200:
                    data = resp.json()
                    recordings = data.get("recordings") or []

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
                    data = resp.json()
                    recordings = data.get("recordings") or []

            if not recordings:
                self._set_cached(cache_key, None)
                return None

            rec = recordings[0]
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
            self._set_cached(cache_key, result)
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
        hit, cached_data = self._get_cached(cache_key)
        if hit:
            return cached_data

        try:
            resp = self._request(
                f"{self.base_url}/ws/2/url", params={"resource": resource, "inc": "artist-rels", "fmt": "json"}
            )
            if resp is None or resp.status_code != 200:
                if resp is not None and resp.status_code == 404:
                    self._set_cached(cache_key, None)
                return None
            data = resp.json()
            mbid: Optional[str] = None
            for rel in data.get("relations") or []:
                artist = rel.get("artist") if isinstance(rel, dict) else None
                if isinstance(artist, dict) and artist.get("id"):
                    mbid = str(artist["id"])
                    break
            self._set_cached(cache_key, mbid)
            return mbid
        except (ValueError, AttributeError, TypeError) as exc:
            logger.warning("MbidEnricherClient: lookup_artist_mbid_by_url failed for '%s': %s", resource, exc)
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
        hit, cached_data = self._get_cached(cache_key)
        if hit:
            return cached_data

        try:
            url = f"{self.base_url}/ws/2/artist"
            params = {"query": f'artist:"{clean_name}"', "fmt": "json"}
            resp = self._request(url, params=params)
            if resp is None or resp.status_code != 200:
                self._set_cached(cache_key, None)
                return None

            data = resp.json()
            artists = data.get("artists") or []
            if not artists:
                self._set_cached(cache_key, None)
                return None

            mbid = artists[0].get("id")
            self._set_cached(cache_key, mbid)
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
        hit, cached_data = self._get_cached(cache_key)
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
                self._set_cached(cache_key, None)
                return None

            data = resp.json()
            release_groups = data.get("release-groups") or []
            if not release_groups:
                self._set_cached(cache_key, None)
                return None

            rg0 = release_groups[0]
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
            self._set_cached(cache_key, result)
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

    def get_artist_details(self, mbid: str) -> Optional[dict[str, Any]]:
        """Queries the mirror for artist metadata (country, disambiguation, genres, urls)."""
        if not mbid or not str(mbid).strip():
            return None

        clean_mbid = str(mbid).strip()
        cache_key = f"artist_details:{clean_mbid.lower()}"
        hit, cached_data = self._get_cached(cache_key)
        if hit:
            return cached_data

        try:
            url = f"{self.base_url}/ws/2/artist/{clean_mbid}"
            params = {"inc": "genres+tags+url-rels", "fmt": "json"}
            resp = self._request(url, params=params)
            if resp is None or resp.status_code != 200:
                self._set_cached(cache_key, None)
                return None

            data = resp.json()
            if not isinstance(data, dict):
                self._set_cached(cache_key, None)
                return None

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
            self._set_cached(cache_key, result)
            return result

        except Exception as exc:
            logger.warning(
                "MbidEnricherClient: get_artist_details failed for '%s': %s",
                clean_mbid,
                exc,
            )
            self._set_cached(cache_key, None)
            return None

    def get_artist_discography(self, mbid: str, limit: int = 100) -> list[dict[str, Any]]:
        """Queries the mirror for full artist release groups and categorizes release types."""
        if not mbid or not str(mbid).strip():
            return []

        clean_mbid = str(mbid).strip()
        cache_key = f"discography:{clean_mbid.lower()}:{limit}"
        hit, cached_data = self._get_cached(cache_key)
        if hit:
            return cached_data

        try:
            url = f"{self.base_url}/ws/2/release-group"
            params = {"artist": clean_mbid, "limit": limit, "fmt": "json"}
            resp = self._request(url, params=params)
            if resp is None or resp.status_code != 200:
                self._set_cached(cache_key, [])
                return []

            data = resp.json()
            if not isinstance(data, dict):
                self._set_cached(cache_key, [])
                return []

            release_groups = data.get("release-groups") or []
            results: list[dict[str, Any]] = []

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

                results.append(
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

            self._set_cached(cache_key, results)
            return results

        except Exception as exc:
            logger.warning(
                "MbidEnricherClient: get_artist_discography failed for '%s': %s",
                clean_mbid,
                exc,
            )
            self._set_cached(cache_key, [])
            return []

    def get_release_group_tracks(self, release_group_id: str) -> list[dict[str, Any]]:
        """Queries MusicBrainz / BrainzMash for canonical tracks within a release group."""
        if not release_group_id or not str(release_group_id).strip():
            return []

        clean_rg_id = str(release_group_id).strip()
        cache_key = f"tracks:rg:{clean_rg_id.lower()}"
        hit, cached_data = self._get_cached(cache_key)
        if hit:
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
                self._set_cached(cache_key, [])
                return []

            data = resp.json()
            if not isinstance(data, dict):
                self._set_cached(cache_key, [])
                return []

            releases = data.get("releases") or []
            if not releases or not isinstance(releases, list):
                self._set_cached(cache_key, [])
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

            self._set_cached(cache_key, results)
            return results

        except Exception as exc:
            logger.warning(
                "MbidEnricherClient: get_release_group_tracks failed for '%s': %s",
                clean_rg_id,
                exc,
            )
            self._set_cached(cache_key, [])
            return []


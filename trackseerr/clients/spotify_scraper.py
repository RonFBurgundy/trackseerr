"""Keyless Spotify scraper client utilizing public embed and web page hydration data.

Requires zero Spotify developer API keys, client secrets, or user login.
Extracts playlist titles, descriptions, high-resolution artwork, and tracklists.
"""

import base64
import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any, List, Optional

from ..models import Playlist, Track

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

EMBED_TRACK_CAP = 100
MAX_PLAYLIST_TRACKS = 10_000
PATHFINDER_URL = "https://api-partner.spotify.com/pathfinder/v2/query"
DEFAULT_PATHFINDER_HASH = "8964e8eafb21aa992a7d951d256d83285c04be2105d209262901de70cb97584a"
PATHFINDER_HASH_TTL_SECONDS = 24 * 3600.0
_FETCH_ERRORS = (urllib.error.URLError, TimeoutError, OSError)
_PARSE_ERRORS = (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError)


def _primary_embed_artist(subtitle: str) -> str:
    """First artist of an embed subtitle. The embed joins artists with a comma + NO-BREAK SPACE; a plain
    ", " belongs to a band name ("Tyler, The Creator") and is left alone."""
    first = subtitle.split(",\u00a0", 1)[0]
    return first.replace("\u00a0", " ").strip()


class SpotifyWebScraper:
    """Scrapes public Spotify playlist metadata and tracks without requiring API credentials."""

    def __init__(self, timeout: int = 15, cache_ttl_seconds: float = 300.0) -> None:
        self.timeout = timeout
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._embed_tokens: dict[str, str] = {}
        self._hash_cache: Optional[tuple[float, str]] = None
        logger.info("Initialized keyless SpotifyWebScraper client")

    def _fetch_embed(self, playlist_id: str) -> Optional[dict[str, Any]]:
        """Fetch playlist data from public embed Next.js SSR block."""
        url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                html = resp.read().decode("utf-8")
        except Exception as e:
            logger.debug("Embed fetch error for playlist %s: %s", playlist_id, e)
            return None

        token = re.search(r'"accessToken":"([^"]+)"', html)
        if token:
            self._embed_tokens[playlist_id] = token.group(1)
        else:
            self._embed_tokens.pop(playlist_id, None)

        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
        if not match:
            return None

        try:
            data = json.loads(match.group(1))
            props = data.get("props", {}).get("pageProps", {})
            if props.get("status") == 404:
                return None
            entity = props.get("state", {}).get("data", {}).get("entity", {})
            if not entity:
                return None
            return entity
        except Exception as e:
            logger.debug("Failed to parse embed JSON for %s: %s", playlist_id, e)
            return None

    def _get_text(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8")

    def _discover_pathfinder_hash(self) -> Optional[str]:
        """Find the persisted-query hash for fetchPlaylistContents in the live web-player bundle."""
        try:
            home = self._get_text("https://open.spotify.com/")
            js = re.search(r"https://open\.spotifycdn\.com/cdn/build/web-player/web-player\.[a-f0-9]+\.js", home)
            if not js:
                return None
            bundle = self._get_text(js.group(0))
        except (*_FETCH_ERRORS, UnicodeDecodeError) as e:
            logger.debug("Pathfinder hash discovery failed: %s", e)
            return None
        for op in ("fetchPlaylistContents", "fetchPlaylist"):
            m = re.search(rf'"{op}","query","([a-f0-9]{{64}})"', bundle)
            if m:
                return m.group(1)
        return None

    def _pathfinder_hash(self) -> str:
        cached = self._hash_cache
        if cached is not None and time.monotonic() - cached[0] < PATHFINDER_HASH_TTL_SECONDS:
            return cached[1]
        value = self._discover_pathfinder_hash() or DEFAULT_PATHFINDER_HASH
        self._hash_cache = (time.monotonic(), value)
        return value

    def _pathfinder_page(self, playlist_id: str, token: str, offset: int, query_hash: str) -> dict[str, Any]:
        body = json.dumps(
            {
                "operationName": "fetchPlaylistContents",
                "variables": {"uri": f"spotify:playlist:{playlist_id}", "offset": offset, "limit": 100},
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": query_hash}},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            PATHFINDER_URL,
            data=body,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _pathfinder_page_retrying(self, playlist_id: str, token: str, offset: int, rediscovered: bool) -> tuple[dict[str, Any], bool]:
        """One page; on HTTP 412 (stale hash) rediscover the hash once per fetch and retry."""
        try:
            return self._pathfinder_page(playlist_id, token, offset, self._pathfinder_hash()), rediscovered
        except urllib.error.HTTPError as e:
            if e.code != 412 or rediscovered:
                raise
        self._hash_cache = None
        return self._pathfinder_page(playlist_id, token, offset, self._pathfinder_hash()), True

    @staticmethod
    def _parse_pathfinder_items(items: list[Any]) -> list[Track]:
        out: list[Track] = []
        for item in items:
            data = (item.get("itemV2") or {}).get("data") or {}
            title = (data.get("name") or "").strip()
            if not title:
                continue
            artists = (data.get("artists") or {}).get("items") or []
            artist = ((artists[0].get("profile") or {}).get("name") or "").strip() if artists else ""
            album = ((data.get("albumOfTrack") or {}).get("name") or "").strip()
            ms = (data.get("trackDuration") or data.get("duration") or {}).get("totalMilliseconds")
            out.append(
                Track(title=title, artist=artist, album=album, duration_seconds=ms / 1000 if ms else None)
            )
        return out

    def _fetch_remaining(self, playlist_id: str, token: str, offset: int, max_tracks: int = MAX_PLAYLIST_TRACKS) -> list[Track]:
        """Page the web player's GraphQL endpoint from ``offset`` for tracks beyond the embed's 100-row cap."""
        collected: list[Track] = []
        rediscovered = False
        try:
            while offset < max_tracks:
                payload, rediscovered = self._pathfinder_page_retrying(playlist_id, token, offset, rediscovered)
                content = payload["data"]["playlistV2"]["content"]
                raw_items = content.get("items") or []
                if not raw_items:
                    break
                collected.extend(self._parse_pathfinder_items(raw_items))
                offset += len(raw_items)
                if offset >= int(content.get("totalCount") or 0):
                    break
        except (urllib.error.HTTPError, *_FETCH_ERRORS, *_PARSE_ERRORS) as e:
            logger.warning(
                "Spotify returned the first %d tracks only for playlist %s (%s)",
                offset,
                playlist_id,
                e,
            )
        return collected

    def _fetch_page(self, playlist_id: str) -> Optional[dict[str, Any]]:
        """Fallback to fetch playlist data from public web page initialState."""
        url = f"https://open.spotify.com/playlist/{playlist_id}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                html = resp.read().decode("utf-8")
        except Exception as e:
            logger.debug("Public page fetch error for playlist %s: %s", playlist_id, e)
            return None

        match = re.search(r'<script id="initialState"[^>]*>(.*?)</script>', html)
        if not match:
            return None

        try:
            raw_b64 = match.group(1).strip()
            data = json.loads(base64.b64decode(raw_b64).decode("utf-8"))
            pl_key = f"spotify:playlist:{playlist_id}"
            entity = data.get("entities", {}).get("items", {}).get(pl_key)
            return entity
        except Exception as e:
            logger.debug("Failed to decode initialState for %s: %s", playlist_id, e)
            return None

    def _load_playlist(self, playlist_id: str, suffix: str = "") -> Optional[dict[str, Any]]:
        """Load and cache playlist metadata and tracks from embed or public page."""
        entry = self._cache.get(playlist_id)
        if entry is not None:
            if time.monotonic() - entry[0] < self.cache_ttl_seconds:
                return entry[1]
            del self._cache[playlist_id]

        name: Optional[str] = None
        desc: str = ""
        poster_url: str = ""
        tracks: list[Track] = []

        # 1. Primary method: Embed page (fast, up to 100 tracks)
        embed_entity = self._fetch_embed(playlist_id)
        if embed_entity:
            name = embed_entity.get("title") or embed_entity.get("name")
            desc = embed_entity.get("subtitle") or ""
            cover_sources = embed_entity.get("coverArt", {}).get("sources", [])
            if cover_sources and len(cover_sources) > 0 and cover_sources[0].get("url"):
                poster_url = cover_sources[0]["url"]

            for item in embed_entity.get("trackList", []):
                t_title = (item.get("title") or "").strip()
                t_artist = _primary_embed_artist(item.get("subtitle") or "")
                if t_title:
                    tracks.append(
                        Track(
                            title=t_title,
                            artist=t_artist,
                            album="",
                        )
                    )

            token = self._embed_tokens.get(playlist_id)
            if token and len(tracks) == EMBED_TRACK_CAP:
                tracks.extend(self._fetch_remaining(playlist_id, token, EMBED_TRACK_CAP))

        # 2. Secondary fallback: Public web page initialState
        if not tracks or not name:
            page_entity = self._fetch_page(playlist_id)
            if page_entity:
                name = name or page_entity.get("name")
                desc = desc or page_entity.get("description", "")
                images = page_entity.get("images", [])
                if not poster_url and images and len(images) > 0 and images[0].get("url"):
                    poster_url = images[0]["url"]

                content = page_entity.get("content", {})
                for item in content.get("items", []):
                    data = item.get("itemV2", {}).get("data", {})
                    t_title = (data.get("name") or "").strip()
                    artists_items = data.get("artists", {}).get("items", [])
                    t_artist = ""
                    if artists_items and artists_items[0].get("profile"):
                        t_artist = artists_items[0]["profile"].get("name", "").strip()
                    album_data = data.get("albumOfTrack", {})
                    t_album = album_data.get("name", "").strip() if album_data else ""

                    if t_title and not any(t.title == t_title and t.artist == t_artist for t in tracks):
                        tracks.append(
                            Track(
                                title=t_title,
                                artist=t_artist,
                                album=t_album,
                            )
                        )

        if not name:
            logger.warning("Could not extract metadata for Spotify playlist ID '%s'", playlist_id)
            return None

        playlist_name = f"{name}{suffix}"
        cached = {
            "id": playlist_id,
            "name": playlist_name,
            "description": desc,
            "poster": poster_url,
            "tracks": tracks,
        }
        self._cache[playlist_id] = (time.monotonic(), cached)
        logger.info(
            "Successfully scraped Spotify playlist '%s' (%d tracks) without API key",
            playlist_name,
            len(tracks),
        )
        return cached

    def get_playlist_by_id(self, playlist_id: str, suffix: str = "") -> Optional[Playlist]:
        """Fetch metadata for a single public playlist by its ID without credentials."""
        data = self._load_playlist(playlist_id, suffix)
        if not data:
            return None
        return Playlist(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            poster=data["poster"],
            tracks=data["tracks"],
        )

    def get_playlist_tracks(self, playlist_id: str) -> List[Track]:
        """Retrieve tracks for a playlist without credentials."""
        data = self._load_playlist(playlist_id)
        if not data:
            return []
        return list(data.get("tracks", []))

    def get_user_playlists(self, user_id: str, suffix: str = "") -> List[Playlist]:
        """Listing entire private user profiles requires Spotify OAuth; returns empty list in keyless mode."""
        logger.warning(
            "Listing user '%s' playlists requires Spotify Developer credentials. "
            "To sync individual playlists without keys, paste public playlist URLs directly.",
            user_id,
        )
        return []

    def fetch_all_playlists(
        self,
        user_id: Optional[str] = None,
        playlist_ids: Optional[List[str]] = None,
        suffix: str = "",
    ) -> List[Playlist]:
        """Fetch all targeted playlists by IDs and populate their tracks."""
        collected: List[Playlist] = []
        seen_ids = set()

        if playlist_ids:
            for pid in playlist_ids:
                if pid not in seen_ids:
                    p = self.get_playlist_by_id(pid, suffix=suffix)
                    if p:
                        seen_ids.add(p.id)
                        collected.append(p)

        for pl in collected:
            if not pl.tracks:
                pl.tracks = self.get_playlist_tracks(pl.id)

        return collected

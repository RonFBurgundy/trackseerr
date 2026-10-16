"""YouTube Music client using ytmusicapi for public and unlisted playlists."""

import logging
import re
import threading
import time
from typing import Any

import requests
import ytmusicapi
from ytmusicapi.exceptions import YTMusicError, YTMusicServerError

from ..models import Playlist, Track

logger = logging.getLogger(__name__)

MAX_PLAYLIST_TRACKS = 10_000
_RETRYABLE_STATUS_CODES = ("429", "502", "503", "504")

_sleep = time.sleep

_HANDLED_ERRORS = (
    YTMusicError,
    requests.RequestException,
    KeyError,
    TypeError,
    ValueError,
)

_BRACKET_CONTENT_RE = re.compile(
    r"(?i)\b(?:official\s+(?:music\s+)?(?:video|audio)|music\s+video|lyrics?|lyric\s+video|visualizer|audio|hd|4k|mv)\b"
)
_TRAILING_BRACKET_RE = re.compile(r"\s*[\[\(\{]([^\[\]\(\)\{\}]+)[\]\)\}]\s*$")
_CHANNEL_LIKE_RE = re.compile(
    r"(?i)(?:\b(?:channel|records|recordings|music|official|tv|media|entertainment|studios?|productions?|sounds?|labels?|hq|audio|videos?|fm|radio|beats|club|city|nation|network|central|collective|vault|archives)\b|vevo\b|vevo$)"
)


def _strip_trailing_video_brackets(title: str) -> str:
    """Strip trailing bracket groups containing media descriptors like Official Video, HD, 4K."""
    while True:
        m = _TRAILING_BRACKET_RE.search(title)
        if not m:
            break
        inner = m.group(1).strip()
        if _BRACKET_CONTENT_RE.search(inner):
            title = title[: m.start()].strip()
        else:
            break
    return title


def _is_channel_like(artist: str) -> bool:
    if not artist or not artist.strip():
        return True
    return bool(_CHANNEL_LIKE_RE.search(artist.strip()))


def clean_video_title(title: str, artist: str) -> tuple[str, str]:
    """Clean video titles for uploads / music videos without an album.

    Removes trailing bracket groups whose text contains official video/audio, music video,
    lyric(s), lyric video, visualizer, audio, HD, 4K, MV (case-insensitive); then if the title
    has ' - ' and the left part equals the channel artist (casefold) or the artist is a
    channel-like name, split into (artist, title).
    """
    cleaned_title = _strip_trailing_video_brackets(title)
    if " - " in cleaned_title:
        left, right = cleaned_title.split(" - ", 1)
        left = left.strip()
        right = right.strip()
        if left and right and (left.casefold() == artist.strip().casefold() or _is_channel_like(artist)):
            return left, right
    return artist, cleaned_title


def _largest_thumbnail_url(thumbnails: list[dict[str, Any]]) -> str:
    """Find the highest resolution thumbnail URL."""
    if not thumbnails:
        return ""

    def _size(t: dict[str, Any]) -> int:
        try:
            return int(t.get("width", 0)) * int(t.get("height", 0))
        except (ValueError, TypeError):
            return 0

    best = max(thumbnails, key=_size)
    if _size(best) > 0:
        return str(best.get("url") or "")
    for t in reversed(thumbnails):
        url = t.get("url")
        if url:
            return str(url)
    return ""


def _to_track(item: dict[str, Any]) -> Track | None:
    """Map a raw ytmusicapi playlist track item to a Track model."""
    if not isinstance(item, dict):
        return None
    video_id = item.get("videoId")
    if not video_id:
        return None
    if item.get("isAvailable") is False:
        return None

    raw_title = item.get("title") or ""
    artists = item.get("artists") or []
    raw_artist = ""
    if isinstance(artists, list) and len(artists) > 0 and isinstance(artists[0], dict):
        raw_artist = artists[0].get("name") or ""
    elif isinstance(artists, list) and len(artists) > 0 and isinstance(artists[0], str):
        raw_artist = artists[0]

    if raw_artist.endswith(" - Topic"):
        raw_artist = raw_artist[:-8].strip()

    album_obj = item.get("album")
    album_name = ""
    if isinstance(album_obj, dict):
        album_name = (album_obj.get("name") or "").strip()
    elif isinstance(album_obj, str):
        album_name = album_obj.strip()

    if not album_name:
        final_artist, final_title = clean_video_title(raw_title, raw_artist)
    else:
        final_artist = raw_artist
        final_title = raw_title

    raw_duration = item.get("duration_seconds")
    duration: float | None = None
    if isinstance(raw_duration, (int, float)):
        duration = float(raw_duration)
    elif isinstance(raw_duration, str):
        try:
            duration = float(raw_duration)
        except ValueError:
            duration = None

    url = f"https://music.youtube.com/watch?v={video_id}"
    return Track(
        title=final_title,
        artist=final_artist,
        album=album_name,
        url=url,
        duration_seconds=duration,
    )


class YouTubeMusicClient:
    """Manages interactions with YouTube Music using unauthenticated ytmusicapi."""

    def __init__(self, cache_ttl_seconds: float = 300.0) -> None:
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()
        self._ytmusic: ytmusicapi.YTMusic | None = None
        self._lock = threading.Lock()

    @property
    def ytmusic(self) -> ytmusicapi.YTMusic:
        """Lazily initialize the YTMusic client instance."""
        if self._ytmusic is None:
            with self._lock:
                if self._ytmusic is None:
                    self._ytmusic = ytmusicapi.YTMusic()
                    logger.info("Initialized unauthenticated YTMusic client")
        return self._ytmusic

    def _load_playlist(self, playlist_id: str) -> dict[str, Any] | None:
        """Load and cache raw playlist payload with retry/backoff on transient server errors."""
        now = time.monotonic()
        with self._cache_lock:
            entry = self._cache.get(playlist_id)
            if entry is not None:
                cached_time, cached_payload = entry
                if now - cached_time < self.cache_ttl_seconds:
                    return cached_payload
                del self._cache[playlist_id]

        attempts = 0
        max_attempts = 3
        backoff_delays = (1, 2)

        while attempts < max_attempts:
            attempts += 1
            try:
                data = self.ytmusic.get_playlist(playlist_id, limit=MAX_PLAYLIST_TRACKS)
                if not isinstance(data, dict):
                    return None

                track_count_raw = data.get("trackCount")
                if track_count_raw is not None:
                    try:
                        track_count = int(track_count_raw)
                        if track_count > MAX_PLAYLIST_TRACKS:
                            logger.warning(
                                "YouTube Music playlist '%s' track count (%d) exceeds cap (%d); limited to first %d tracks",
                                playlist_id,
                                track_count,
                                MAX_PLAYLIST_TRACKS,
                                MAX_PLAYLIST_TRACKS,
                            )
                    except (ValueError, TypeError):
                        pass

                with self._cache_lock:
                    self._cache[playlist_id] = (time.monotonic(), data)
                return data
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempts < max_attempts:
                    delay = backoff_delays[attempts - 1]
                    logger.warning(
                        "Transient network error fetching YouTube Music playlist '%s' (attempt %d/%d): %s. Retrying in %ds...",
                        playlist_id,
                        attempts,
                        max_attempts,
                        e,
                        delay,
                    )
                    _sleep(delay)
                else:
                    logger.warning(
                        "Failed fetching YouTube Music playlist '%s' after %d attempts: %s",
                        playlist_id,
                        max_attempts,
                        e,
                    )
                    return None
            except YTMusicServerError as e:
                err_msg = str(e)
                if any(code in err_msg for code in _RETRYABLE_STATUS_CODES):
                    if attempts < max_attempts:
                        delay = backoff_delays[attempts - 1]
                        logger.warning(
                            "Server error fetching YouTube Music playlist '%s' (attempt %d/%d): %s. Retrying in %ds...",
                            playlist_id,
                            attempts,
                            max_attempts,
                            e,
                            delay,
                        )
                        _sleep(delay)
                    else:
                        logger.warning(
                            "Failed fetching YouTube Music playlist '%s' after %d attempts: %s",
                            playlist_id,
                            max_attempts,
                            e,
                        )
                        return None
                else:
                    logger.warning("Non-retryable YTMusicServerError fetching playlist '%s': %s", playlist_id, e)
                    return None
            except _HANDLED_ERRORS as e:
                logger.warning("Failed fetching YouTube Music playlist '%s': %s", playlist_id, e)
                return None

        return None

    def get_playlist_by_id(self, playlist_id: str, suffix: str = "") -> Playlist | None:
        """Fetch metadata for a single YouTube Music playlist by ID."""
        try:
            data = self._load_playlist(playlist_id)
            if not isinstance(data, dict):
                return None
            title = data.get("title")
            if not title:
                return None
            desc = data.get("description") or ""
            thumbnails = data.get("thumbnails") or []
            poster = _largest_thumbnail_url(thumbnails)
            return Playlist(
                id=str(playlist_id),
                name=f"{title}{suffix}",
                description=desc,
                poster=poster,
            )
        except _HANDLED_ERRORS as e:
            logger.error("Failed to fetch YouTube Music playlist with ID '%s': %s", playlist_id, e)
            return None

    def get_playlist_tracks(self, playlist_id: str) -> list[Track]:
        """Fetch all tracks for a YouTube Music playlist."""
        tracks: list[Track] = []
        try:
            data = self._load_playlist(playlist_id)
            if not isinstance(data, dict):
                return []
            raw_tracks = data.get("tracks") or []
            for item in raw_tracks:
                t = _to_track(item)
                if t is not None:
                    tracks.append(t)
            logger.debug("Fetched %d tracks for YouTube Music playlist '%s'", len(tracks), playlist_id)
        except _HANDLED_ERRORS as e:
            logger.error("Failed to fetch tracks for YouTube Music playlist '%s': %s", playlist_id, e)
            return []
        return tracks


_client_lock = threading.Lock()
_shared_client: YouTubeMusicClient | None = None


def get_shared_youtube_music_client() -> YouTubeMusicClient:
    """Return the thread-safe module singleton YouTubeMusicClient."""
    global _shared_client
    if _shared_client is None:
        with _client_lock:
            if _shared_client is None:
                _shared_client = YouTubeMusicClient()
    return _shared_client

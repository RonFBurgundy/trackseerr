"""MediaCover local image caching service.

Provides high-speed local caching for artist posters, artist banners, and album covers
with SSRF protection, magic byte verification, atomic writes, and responsive fallbacks.
"""

from concurrent.futures import ThreadPoolExecutor
import logging
import os
from pathlib import Path
import threading
import time
from typing import Callable, Optional, Union
from urllib.parse import urlparse
import uuid

import requests

from plex_playlist_sync.security import is_safe_image_url

logger = logging.getLogger(__name__)

# Max image size allowed for caching: 10 Megabytes
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

# (connect, read) seconds. Connect is short so a black-holed host fails fast.
_FETCH_TIMEOUT = (3.0, 5.0)
# Wall-clock cap on one whole download (slow-drip bodies never trip the per-read timeout).
_FETCH_DEADLINE_SECONDS = 15.0
# A URL that failed is not retried for this long.
_NEGATIVE_TTL_SECONDS = 6 * 3600
_NEGATIVE_MAX_ENTRIES = 20000
# Per-host circuit breaker: after this many consecutive network failures, skip the host for a while.
_BREAKER_THRESHOLD = 5
_BREAKER_OPEN_SECONDS = 300
# Background download pool: small, bounded queue, excess work is dropped (it is re-requested on the next view).
_POOL_WORKERS = 4
_MAX_PENDING = 256


class MediaCoverService:
    """Manages local filesystem caching for artist and album artwork."""

    def __init__(self, base_dir: Optional[Union[str, Path]] = None) -> None:
        if base_dir:
            self.base_dir = Path(base_dir).resolve()
        else:
            config_dir = os.getenv("CONFIG_DIR")
            if config_dir or os.path.isdir("/config"):
                self.base_dir = Path(config_dir or "/config").resolve()
            else:
                self.base_dir = Path(os.getenv("DATA_DIR", "/data")).resolve()

        self.artists_dir = self.base_dir / "mediacover" / "artists"
        self.albums_dir = self.base_dir / "mediacover" / "albums"

        try:
            self.artists_dir.mkdir(parents=True, exist_ok=True)
            self.albums_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("MediaCoverService: Failed to ensure cache directories: %s", exc)

        self._state_lock = threading.Lock()
        self._negative: dict[str, float] = {}
        self._host_failures: dict[str, int] = {}
        self._host_open_until: dict[str, float] = {}
        # target path -> callbacks to run when that download lands (merged when a second caller dedupes onto it)
        self._inflight: dict[str, list[Callable[[Path], None]]] = {}
        self._executor: Optional[ThreadPoolExecutor] = None

    # ------------------------------------------------------------------ failure memory
    @staticmethod
    def _host_of(url: str) -> str:
        try:
            return (urlparse(url).hostname or "").lower()
        except ValueError:
            return ""

    def _should_skip(self, url: str) -> bool:
        """True while ``url`` is negative-cached or its host's circuit breaker is open."""
        now = time.monotonic()
        host = self._host_of(url)
        with self._state_lock:
            exp = self._negative.get(url)
            if exp is not None:
                if exp > now:
                    return True
                del self._negative[url]
            until = self._host_open_until.get(host)
            if until is not None:
                if until > now:
                    return True
                # Half-open: let the next attempt through; one more failure re-opens it.
                del self._host_open_until[host]
                self._host_failures[host] = _BREAKER_THRESHOLD - 1
        return False

    def _record_failure(self, url: str, network: bool) -> None:
        now = time.monotonic()
        host = self._host_of(url)
        with self._state_lock:
            if len(self._negative) >= _NEGATIVE_MAX_ENTRIES:
                for k in [k for k, v in self._negative.items() if v <= now]:
                    del self._negative[k]
                if len(self._negative) >= _NEGATIVE_MAX_ENTRIES:
                    self._negative.clear()
            self._negative[url] = now + _NEGATIVE_TTL_SECONDS
            if network and host:
                n = self._host_failures.get(host, 0) + 1
                self._host_failures[host] = n
                if n >= _BREAKER_THRESHOLD and host not in self._host_open_until:
                    self._host_open_until[host] = now + _BREAKER_OPEN_SECONDS
                    logger.warning(
                        "MediaCoverService: %d consecutive failures for %s; skipping it for %ds",
                        n,
                        host,
                        _BREAKER_OPEN_SECONDS,
                    )

    def _record_success(self, url: str) -> None:
        host = self._host_of(url)
        with self._state_lock:
            self._host_failures.pop(host, None)
            self._host_open_until.pop(host, None)
            self._negative.pop(url, None)

    # ------------------------------------------------------------------ background pool
    def schedule_cache(
        self, target_path: Path, remote_url: str, on_cached: Optional[Callable[[Path], None]] = None
    ) -> bool:
        """Queues a background download. Never blocks; returns False if skipped, deduped, or dropped (queue full).

        ``on_cached(target_path)`` runs on the worker once the file is on disk (thumbnail pre-generation, art version).
        A call deduped onto a queued/running download still returns False, but its ``on_cached`` is merged and fires too.
        """
        if not remote_url or not isinstance(remote_url, str) or self._should_skip(remote_url):
            return False
        key = str(target_path)
        with self._state_lock:
            if key in self._inflight:
                if on_cached is not None:
                    self._inflight[key].append(on_cached)
                return False
            if len(self._inflight) >= _MAX_PENDING:
                return False
            self._inflight[key] = [on_cached] if on_cached is not None else []
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=_POOL_WORKERS, thread_name_prefix="mediacover"
                )
            executor = self._executor

        def _job() -> None:
            try:
                if self.cache_image(target_path, remote_url):
                    while True:  # drain until empty: a caller may dedupe onto this download while callbacks run
                        with self._state_lock:
                            pending = self._inflight.get(key)
                            callbacks = list(pending) if pending else []
                            if pending:
                                pending.clear()
                            else:
                                self._inflight.pop(key, None)  # atomic with the emptiness check: no late callback is lost
                        if not callbacks:
                            break
                        for callback in callbacks:
                            try:
                                callback(target_path)
                            except Exception as exc:
                                logger.warning("MediaCoverService: on_cached callback for %s failed: %s", target_path, exc)
            except Exception as exc:
                logger.warning("MediaCoverService: background cache of %s failed: %s", remote_url, exc)
            finally:
                with self._state_lock:
                    self._inflight.pop(key, None)

        try:
            executor.submit(_job)
        except RuntimeError as exc:
            with self._state_lock:
                self._inflight.pop(key, None)
            logger.warning("MediaCoverService: background pool unavailable: %s", exc)
            return False
        return True

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Blocks until no background download is pending (for tests and shutdown)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._state_lock:
                if not self._inflight:
                    return True
            time.sleep(0.01)
        return False

    def get_artist_poster_path(self, artist_id: str) -> Path:
        """Returns the canonical filesystem path for an artist's poster image."""
        return self.artists_dir / f"{artist_id}_poster.jpg"

    def get_artist_banner_path(self, artist_id: str) -> Path:
        """Returns the canonical filesystem path for an artist's banner image."""
        return self.artists_dir / f"{artist_id}_banner.jpg"

    def get_album_cover_path(self, album_id: str) -> Path:
        """Returns the canonical filesystem path for an album's cover image."""
        return self.albums_dir / f"{album_id}_cover.jpg"

    def cache_image(
        self, target_path: Path, remote_url: str, timeout: Union[float, tuple[float, float]] = _FETCH_TIMEOUT
    ) -> bool:
        """Fetches and verifies remote image with magic byte check, writing atomically to target_path."""
        if not remote_url or not isinstance(remote_url, str):
            return False

        if not is_safe_image_url(remote_url):
            logger.warning("MediaCoverService: Blocked unsafe image URL: %s", remote_url)
            return False

        try:
            if target_path.is_file() and target_path.stat().st_size > 0:
                return True
        except OSError:
            pass

        if self._should_skip(remote_url):
            return False

        try:
            headers = {
                "User-Agent": "Lidarr/2.0.0 (TrackSeerr; https://github.com/trackseerr)",
                "Accept": "image/webp,image/jpeg,image/png,image/*;q=0.8",
            }
            resp = requests.get(
                remote_url,
                timeout=timeout,
                stream=True,
                headers=headers,
            )
            if resp.status_code != 200:
                self._record_failure(remote_url, network=resp.status_code >= 500)
                logger.debug(
                    "MediaCoverService: Remote image fetch failed (status=%d) for %s",
                    resp.status_code,
                    remote_url,
                )
                return False

            content = bytearray()
            deadline = time.monotonic() + _FETCH_DEADLINE_SECONDS
            for chunk in resp.iter_content(chunk_size=65536):
                if time.monotonic() > deadline:
                    self._record_failure(remote_url, network=True)
                    logger.warning(
                        "MediaCoverService: Download of %s exceeded %.0fs deadline", remote_url, _FETCH_DEADLINE_SECONDS
                    )
                    return False
                if chunk:
                    content.extend(chunk)
                    if len(content) > _MAX_IMAGE_BYTES:
                        logger.warning(
                            "MediaCoverService: Image from %s exceeded %d bytes limit",
                            remote_url,
                            _MAX_IMAGE_BYTES,
                        )
                        return False

            data = bytes(content)
            if len(data) < 12:
                logger.warning(
                    "MediaCoverService: Downloaded image from %s too short (%d bytes)",
                    remote_url,
                    len(data),
                )
                return False

            # Magic bytes validation
            is_jpeg = data.startswith(b"\xff\xd8\xff")
            is_png = data.startswith(b"\x89PNG\r\n\x1a\n")
            is_webp = data.startswith(b"RIFF") and data[8:12] == b"WEBP"

            if not (is_jpeg or is_png or is_webp):
                logger.warning(
                    "MediaCoverService: Image from %s failed magic byte verification",
                    remote_url,
                )
                return False

            target_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = target_path.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}")
            tmp_path.write_bytes(data)
            tmp_path.replace(target_path)
            self._record_success(remote_url)
            return True

        except (requests.RequestException, OSError) as exc:
            self._record_failure(remote_url, network=True)
            logger.warning(
                "MediaCoverService: Error downloading/saving image from %s: %s",
                remote_url,
                exc,
            )
            return False
        except Exception as exc:
            self._record_failure(remote_url, network=False)
            logger.warning(
                "MediaCoverService: Unexpected error caching image from %s: %s",
                remote_url,
                exc,
            )
            return False

    def ensure_artwork(
        self,
        category: str,
        item_id: str,
        remote_url: Optional[str],
        block: bool = False,
        on_cached: Optional[Callable[[Path], None]] = None,
    ) -> Optional[Path]:
        """Resolves target path for category, returning it if already cached.

        On a miss the download is queued on the background pool and ``None`` is returned immediately, so request
        handlers never wait on a remote host. ``block=True`` downloads inline (tests / explicit one-shot callers).
        """
        if category == "artist_poster":
            target = self.get_artist_poster_path(item_id)
        elif category == "artist_banner":
            target = self.get_artist_banner_path(item_id)
        elif category == "album_cover":
            target = self.get_album_cover_path(item_id)
        else:
            return None

        try:
            if target.is_file() and target.stat().st_size > 0:
                return target
        except OSError:
            pass

        if not remote_url:
            return None
        if block:
            if not self.cache_image(target, remote_url):
                return None
            if on_cached is not None:
                on_cached(target)
            return target
        self.schedule_cache(target, remote_url, on_cached)
        return None


# Module singleton
mediacover_service = MediaCoverService()

__all__ = ["MediaCoverService", "mediacover_service"]

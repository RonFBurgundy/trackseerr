"""Process-wide coalescing scheduler for media-server library refreshes.

Batches rapid bursts of changes (e.g. multi-album imports, bulk tagging, renames)
so Trackseerr never floods Plex, Jellyfin, or Subsonic with concurrent rescans.
"""

from collections.abc import Callable
import logging
import os
import threading
import time

import requests

from trackseerr.media_servers.base import MediaServer, MediaServerError

logger = logging.getLogger(__name__)

QUIET_SECONDS: float = 60.0
_monotonic: Callable[[], float] = time.monotonic


def _parse_env_window() -> int:
    raw = os.getenv("MEDIA_SERVER_REFRESH_MINUTES")
    if raw is None:
        return 10
    try:
        val = int(raw.strip())
        return max(0, val)
    except (ValueError, TypeError):
        logger.warning(
            "Invalid MEDIA_SERVER_REFRESH_MINUTES %r; defaulting to 10",
            raw,
        )
        return 10


class RefreshScheduler:
    """Coalesces library refresh requests per media server over a time window."""

    def __init__(self, window_minutes: int | None = None) -> None:
        self._lock = threading.Lock()
        init_mins = window_minutes if window_minutes is not None else _parse_env_window()
        self._window_seconds: float = float(max(0, init_mins) * 60)
        self._timer: threading.Timer | None = None
        self._pending_server: MediaServer | None = None
        self._pending_count: int = 0
        self._pending_reasons: list[str] = []
        self._pending_delay: float | None = None
        self._last_fired: float | None = None
        self._disabled_logged: bool = False

    def configure(self, window_minutes: int) -> None:
        with self._lock:
            try:
                val = int(window_minutes)
                self._window_seconds = float(max(0, val) * 60)
            except (ValueError, TypeError):
                logger.warning(
                    "Invalid media_server_refresh_minutes %r; defaulting to 10",
                    window_minutes,
                )
                self._window_seconds = 10.0 * 60
            self._disabled_logged = False

    def request_library_refresh(self, server: MediaServer | None, reason: str) -> None:
        if server is None:
            return

        with self._lock:
            if self._window_seconds == 0:
                if not self._disabled_logged:
                    logger.debug("Media-server refresh scheduler disabled (window=0)")
                    self._disabled_logged = True
                return

            self._pending_server = server
            self._pending_count += 1
            if reason:
                self._pending_reasons.append(reason)

            if self._timer is None:
                now = _monotonic()
                if self._last_fired is None:
                    delay = QUIET_SECONDS
                else:
                    delay = max(QUIET_SECONDS, self._last_fired + self._window_seconds - now)
                self._pending_delay = delay
                timer = threading.Timer(delay, self._fire)
                timer.daemon = True
                self._timer = timer
                timer.start()

    def _fire(self) -> None:
        with self._lock:
            server = self._pending_server
            count = self._pending_count
            reasons = list(self._pending_reasons)
            self._timer = None
            self._pending_server = None
            self._pending_count = 0
            self._pending_reasons = []
            self._pending_delay = None

        if server is not None and count > 0:
            self._execute_refresh(server, count, reasons)

    def _execute_refresh(self, server: MediaServer, count: int, reasons: list[str]) -> None:
        kind = getattr(server, "kind", "") or "media server"
        try:
            server.refresh_library()
            distinct_reasons: list[str] = []
            for r in reasons:
                if r and r not in distinct_reasons:
                    distinct_reasons.append(r)
            reasons_summary = ", ".join(distinct_reasons[:3]) or "unspecified"
            logger.info(
                "Asked %s to rescan its music library (%d change(s) batched: %s)",
                kind,
                count,
                reasons_summary,
            )
        except (MediaServerError, OSError, requests.RequestException) as exc:
            logger.warning(
                "Media server %s rescan failed: %s",
                kind,
                exc,
            )
        finally:
            with self._lock:
                self._last_fired = _monotonic()

    def flush_now(self) -> bool:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            server = self._pending_server
            count = self._pending_count
            reasons = list(self._pending_reasons)
            self._pending_server = None
            self._pending_count = 0
            self._pending_reasons = []
            self._pending_delay = None

        if server is None or count == 0:
            return False

        self._execute_refresh(server, count, reasons)
        return True

    def reset_for_tests(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._pending_server = None
            self._pending_count = 0
            self._pending_reasons = []
            self._pending_delay = None
            self._last_fired = None
            self._window_seconds = 10.0 * 60
            self._disabled_logged = False

    def pending_delay(self) -> float | None:
        with self._lock:
            return self._pending_delay


_scheduler = RefreshScheduler()


def request_library_refresh(server: MediaServer | None, reason: str) -> None:
    _scheduler.request_library_refresh(server, reason)


def configure(window_minutes: int) -> None:
    _scheduler.configure(window_minutes)


def flush_now() -> bool:
    return _scheduler.flush_now()


def reset_for_tests() -> None:
    _scheduler.reset_for_tests()


def pending_delay() -> float | None:
    return _scheduler.pending_delay()

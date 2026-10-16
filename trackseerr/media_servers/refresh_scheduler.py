"""Process-wide coalescing scheduler for media-server library refreshes.

Batches rapid bursts of changes (e.g. multi-album imports, bulk tagging, renames)
so Trackseerr never floods Plex, Jellyfin, or Subsonic with concurrent rescans.
"""

import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

import requests

from trackseerr.itunes_import import apply_mappings
from trackseerr.media_servers.base import (
    MediaServer,
    MediaServerError,
    MediaServerUnsupported,
)
from trackseerr.redaction import safe_exc

logger = logging.getLogger(__name__)

QUIET_SECONDS: float = 60.0
MAX_FOLDERS_PER_BATCH: int = 100
_monotonic: Callable[[], float] = time.monotonic
_roots_cache: dict[str, tuple[float, list[str]]] = {}


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


def _get_cached_library_roots(server: MediaServer) -> list[str]:
    kind = getattr(server, "kind", "") or ""
    now = _monotonic()
    if kind in _roots_cache:
        cached_at, roots = _roots_cache[kind]
        if now - cached_at < 3600.0:
            return roots
    try:
        roots = list(server.library_roots())
    except MediaServerError as exc:
        logger.warning("Could not fetch library roots for %s: %s", kind or "media server", safe_exc(exc))
        roots = []
    _roots_cache[kind] = (now, roots)
    return roots


def _try_saved_mapping(db: Any, server: MediaServer, local: str) -> str | None:
    if db is None or not hasattr(db, "get_media_server_path_mapping"):
        return None
    try:
        saved = db.get_media_server_path_mapping()
    except (AttributeError, TypeError, ValueError, OSError) as exc:
        logger.debug("Could not read path mapping: %s", safe_exc(exc))
        saved = None
    if not (saved and isinstance(saved, dict)):
        return None
    kind = getattr(server, "kind", "") or ""
    sk = saved.get("server_kind", "")
    if sk not in ("", kind):
        return None
    lp = str(saved.get("local_prefix") or "")
    sp = str(saved.get("server_prefix") or "")
    if not (lp or sp):
        return None
    return apply_mappings(local, [{"from": lp, "to": sp}])


def _try_library_roots(roots: list[str], local: str) -> str | None:
    for r in roots:
        if r and apply_mappings(local, [{"from": r, "to": r}]) is not None:
            return local
    return None


def _try_media_management_mapping(db: Any, roots: list[str], local: str) -> str | None:
    if len(roots) != 1 or not roots[0] or db is None or not hasattr(db, "get_media_management_settings"):
        return None
    try:
        settings = db.get_media_management_settings() or {}
    except (AttributeError, TypeError, ValueError, OSError) as exc:
        logger.debug("Could not read media management settings: %s", safe_exc(exc))
        settings = {}
    if not isinstance(settings, dict):
        return None
    local_root = str(settings.get("root_folder_path") or "")
    if not local_root:
        return None
    return apply_mappings(local, [{"from": local_root, "to": roots[0]}])


def to_server_folder(db: Any, server: MediaServer, local: str) -> str | None:
    if getattr(server, "paths_relative", False) is True or not local:
        return None

    # a. Saved mapping
    mapped = _try_saved_mapping(db, server, local)
    if mapped is not None:
        return mapped

    roots = _get_cached_library_roots(server)

    # b. Local already under one of server.library_roots() -> unchanged
    if _try_library_roots(roots, local) is not None:
        return local

    # c. Exactly one server root and local sits under root_folder_path
    return _try_media_management_mapping(db, roots, local)


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
        self._pending_full: bool = False
        self._pending_folders: set[str] = set()
        self._last_full_fired: float | None = None
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

    def request_library_refresh(
        self,
        server: MediaServer | None,
        reason: str,
        paths: Iterable[str] | None = None,
        db: Any = None,
    ) -> None:
        if server is None:
            return

        can_folder = bool(getattr(getattr(server, "capabilities", None), "folder_refresh", False))
        is_folder_request = False
        translated_paths: list[str] = []

        if paths is not None and db is not None and can_folder:
            all_translated = True
            for p in paths:
                t = to_server_folder(db, server, p)
                if t is None:
                    all_translated = False
                    break
                translated_paths.append(t)
            if all_translated and translated_paths:
                is_folder_request = True

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

            if is_folder_request and not self._pending_full:
                self._pending_folders.update(translated_paths)
                if self._timer is None:
                    delay = QUIET_SECONDS
                    self._pending_delay = delay
                    timer = threading.Timer(delay, self._fire)
                    timer.daemon = True
                    self._timer = timer
                    timer.start()
            else:
                was_folder_only = (not self._pending_full) and (self._timer is not None)
                self._pending_full = True
                if translated_paths:
                    self._pending_folders.update(translated_paths)

                if was_folder_only and self._timer is not None:
                    self._timer.cancel()
                    self._timer = None

                if self._timer is None:
                    now = _monotonic()
                    if self._last_full_fired is None:
                        delay = QUIET_SECONDS
                    else:
                        delay = max(QUIET_SECONDS, self._last_full_fired + self._window_seconds - now)
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
            is_full = self._pending_full
            folders = set(self._pending_folders)

            self._timer = None
            self._pending_server = None
            self._pending_count = 0
            self._pending_reasons = []
            self._pending_delay = None
            self._pending_full = False
            self._pending_folders = set()

        if server is not None and count > 0:
            self._execute_refresh(server, count, reasons, is_full, folders)

    def _execute_refresh(
        self,
        server: MediaServer,
        count: int,
        reasons: list[str],
        is_full: bool,
        folders: set[str],
    ) -> None:
        kind = getattr(server, "kind", "") or "media server"
        distinct_reasons: list[str] = []
        for r in reasons:
            if r and r not in distinct_reasons:
                distinct_reasons.append(r)
        reasons_summary = ", ".join(distinct_reasons[:3]) or "unspecified"

        if is_full or len(folders) > MAX_FOLDERS_PER_BATCH:
            self._execute_full_refresh(server, kind, count, reasons_summary)
            return

        failed = False
        try:
            ok = server.refresh_paths(sorted(folders))
            if not ok:
                failed = True
                logger.warning(
                    "Media server %s folder rescan returned False; falling back to full library rescan",
                    kind,
                )
        except (MediaServerUnsupported, MediaServerError, OSError, requests.RequestException) as exc:
            failed = True
            logger.warning(
                "Media server %s folder rescan failed (%s); falling back to full library rescan",
                kind,
                safe_exc(exc),
            )

        if failed:
            self._execute_full_refresh(server, kind, count, reasons_summary)
        else:
            logger.info(
                "Asked %s to rescan %d folder(s) (%d change(s) batched: %s)",
                kind,
                len(folders),
                count,
                reasons_summary,
            )
            with self._lock:
                self._last_fired = _monotonic()

    def _execute_full_refresh(
        self, server: MediaServer, kind: str, count: int, reasons_summary: str
    ) -> None:
        try:
            server.refresh_library()
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
                now = _monotonic()
                self._last_full_fired = now
                self._last_fired = now

    def flush_now(self) -> bool:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            server = self._pending_server
            count = self._pending_count
            reasons = list(self._pending_reasons)
            is_full = self._pending_full
            folders = set(self._pending_folders)

            self._pending_server = None
            self._pending_count = 0
            self._pending_reasons = []
            self._pending_delay = None
            self._pending_full = False
            self._pending_folders = set()

        if server is None or count == 0:
            return False

        self._execute_refresh(server, count, reasons, is_full, folders)
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
            self._pending_full = False
            self._pending_folders = set()
            self._last_full_fired = None
            self._last_fired = None
            self._window_seconds = 10.0 * 60
            self._disabled_logged = False
        _roots_cache.clear()

    def pending_delay(self) -> float | None:
        with self._lock:
            return self._pending_delay


_scheduler = RefreshScheduler()


def request_library_refresh(
    server: MediaServer | None,
    reason: str,
    paths: Iterable[str] | None = None,
    db: Any = None,
) -> None:
    _scheduler.request_library_refresh(server, reason, paths=paths, db=db)


def configure(window_minutes: int) -> None:
    _scheduler.configure(window_minutes)


def flush_now() -> bool:
    return _scheduler.flush_now()


def reset_for_tests() -> None:
    _scheduler.reset_for_tests()


def pending_delay() -> float | None:
    return _scheduler.pending_delay()

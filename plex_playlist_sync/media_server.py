"""Media-server status and the shared "no media server" API contract.

Plex is the only supported media server today; ``none`` means Trackseerr manages the library and does not
push playlists anywhere. Features that need a media server answer ``409`` with
``{"detail": "No media server connected", "code": "media_server_unavailable"}`` when none is configured.
(A configured-but-unreachable Plex keeps its own 503s: that is an outage, not a configuration choice.)
"""

import logging
import threading
import time
from typing import Any, Callable, Optional

from fastapi.responses import JSONResponse
from starlette.requests import Request

from plex_playlist_sync.config import MEDIA_SERVER_NONE, Config
from plex_playlist_sync.redaction import safe_exc

logger = logging.getLogger(__name__)

NO_MEDIA_SERVER_DETAIL = "No media server connected"
NO_MEDIA_SERVER_CODE = "media_server_unavailable"
NO_MEDIA_SERVER_STATUS = 409
NO_MEDIA_SERVER_BOOT_MESSAGE = (
    "No media server configured — library management runs; playlist push to a media server is disabled"
)


class MediaServerUnavailable(Exception):
    """Raised when a media-server-only feature is used while no media server is configured."""


def media_server_unavailable_response(_request: Request, _exc: Exception) -> JSONResponse:
    """Exception handler rendering the consistent flat 409 body."""
    return JSONResponse(
        status_code=NO_MEDIA_SERVER_STATUS,
        content={"detail": NO_MEDIA_SERVER_DETAIL, "code": NO_MEDIA_SERVER_CODE},
    )


_PROBE_TTL_SECONDS = 30.0
_PROBE_TIMEOUT_SECONDS = 4.0
_probe_lock = threading.Lock()  # guards the cache dict only; never held while connecting
_probe_inflight: set[tuple[str, str]] = set()
_probe_cache: dict[tuple[str, str], tuple[float, bool]] = {}


def reset_probe_cache() -> None:
    with _probe_lock:
        _probe_cache.clear()
        _probe_inflight.clear()


def _run_probe(key: tuple[str, str], connect: Callable[[], Optional[Any]]) -> None:
    reachable = False
    try:
        reachable = connect() is not None
    except Exception as exc:  # noqa: BLE001 - a probe must never raise; the failure is logged and reported as down
        logger.warning("Media server probe failed: %s", safe_exc(exc))
    finally:
        with _probe_lock:
            _probe_cache[key] = (time.monotonic(), reachable)
            _probe_inflight.discard(key)


def _plex_reachable(config: Config, connect: Callable[[], Optional[Any]]) -> bool:
    """Whether Plex answers, cached for ``_PROBE_TTL_SECONDS``. The status endpoint is unauthenticated, so it must
    never block on Plex: one caller starts a background refresh and waits at most ``_PROBE_TIMEOUT_SECONDS`` for
    it; every other caller gets the cached (possibly stale) value immediately. With no cache yet, they get False."""
    key = (config.plex_url, config.plex_token)
    with _probe_lock:
        hit = _probe_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < _PROBE_TTL_SECONDS:
            return hit[1]
        if key in _probe_inflight:
            return hit[1] if hit is not None else False
        _probe_inflight.add(key)
    worker = threading.Thread(target=_run_probe, args=(key, connect), name="media-server-probe", daemon=True)
    worker.start()
    worker.join(_PROBE_TIMEOUT_SECONDS)
    with _probe_lock:
        hit = _probe_cache.get(key)
    return hit[1] if hit is not None else False


def media_server_status(config: Config, connect: Callable[[], Optional[Any]]) -> dict[str, Any]:
    """Public, non-sensitive description of the active media server and what the UI may offer.

    ``connect`` returns a connected client or None; it is only called when Plex is the configured server.
    """
    server_type = config.media_server_type
    has_server = server_type != MEDIA_SERVER_NONE
    connected = has_server and _plex_reachable(config, connect)
    return {
        "type": server_type,
        "connected": connected,
        "capabilities": {
            "playlists": has_server,
            "users": has_server,
            "mixes": has_server,
            "library_refresh": has_server,
        },
    }


__all__ = [
    "NO_MEDIA_SERVER_BOOT_MESSAGE",
    "NO_MEDIA_SERVER_CODE",
    "NO_MEDIA_SERVER_DETAIL",
    "NO_MEDIA_SERVER_STATUS",
    "MediaServerUnavailable",
    "media_server_status",
    "reset_probe_cache",
    "media_server_unavailable_response",
]

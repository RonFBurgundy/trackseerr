"""Pluggable media-server sinks. Plex is the default; see :mod:`plex_playlist_sync.media_servers.base`."""

import logging
import threading
from typing import Any, Optional

from plex_playlist_sync.config import (
    MEDIA_SERVER_JELLYFIN,
    MEDIA_SERVER_PLEX,
    MEDIA_SERVER_SOURCE_SETTINGS,
    MEDIA_SERVER_SUBSONIC,
    Config,
)
from plex_playlist_sync.media_servers.base import (
    NO_CAPABILITIES,
    ConnectionTest,
    MediaServer,
    MediaServerAuthError,
    MediaServerConnectionError,
    MediaServerError,
    MediaServerNotFound,
    MediaServerUnavailable,
    MediaServerUnsupported,
    PlaylistSyncOptions,
    ServerCapabilities,
    ServerTrackRef,
    ServerUser,
    as_media_server,
)
from plex_playlist_sync.media_servers.jellyfin import JELLYFIN_CAPABILITIES, JellyfinMediaServer
from plex_playlist_sync.media_servers.plex import PLEX_CAPABILITIES, PlexMediaServer, plex_extras
from plex_playlist_sync.media_servers.subsonic import SUBSONIC_CAPABILITIES, SubsonicMediaServer
from plex_playlist_sync.media_servers.user_import import import_server_users
from plex_playlist_sync.redaction import safe_exc
from plex_playlist_sync.security import is_safe_service_url

logger = logging.getLogger(__name__)


def get_media_server(config: Config, *, plex_client: Optional[Any] = None) -> Optional[MediaServer]:
    """The adapter for the configured media server, or None when none is configured or it cannot be reached.

    ``plex_client`` lets callers that already hold a connected client (workers, tests) avoid reconnecting.
    """
    if config.media_server_type == MEDIA_SERVER_PLEX:
        if plex_client is not None:
            return as_media_server(plex_client)
        from plex_playlist_sync.clients.plex import PlexClient

        if not config.plex_enabled:
            return None
        try:
            return PlexMediaServer(
                PlexClient(config.plex_url, config.plex_token, verify_ssl=config.plex_verify_ssl)
            )
        except Exception as exc:  # noqa: BLE001 - PlexServer raises plexapi/requests/ssl errors; cause is logged
            logger.error("Failed to initialize media server: %s", safe_exc(exc))
            return None
    if config.media_server_type == MEDIA_SERVER_SUBSONIC:
        return build_subsonic(config)
    if config.media_server_type == MEDIA_SERVER_JELLYFIN:
        return build_jellyfin(config)
    return None


def _saved_url_is_unsafe(source: str, kind: str, url: str) -> bool:
    """True (and logged) when a URL that came from the Settings page, not the operator's environment, fails the SSRF
    check. The save/test routes check it too; this covers rows saved before the check existed or written directly."""
    if source != MEDIA_SERVER_SOURCE_SETTINGS or is_safe_service_url(url):
        return False
    logger.error("Refusing to connect to the saved %s URL: it is not an allowed service address", kind)
    return True


_subsonic_lock = threading.Lock()
_subsonic_cached: Optional[tuple[tuple[Any, ...], SubsonicMediaServer]] = None


def build_subsonic(config: Config) -> Optional[SubsonicMediaServer]:
    """The Subsonic adapter for ``config`` (env, else the Settings page), or None when it is incomplete.

    One adapter (one pooled HTTP client) is shared per distinct configuration. When the settings change a new adapter
    replaces it; the old one is NOT closed here because a running sync may still be using it. Its HTTP client closes
    when the last holder lets go of it. All values come from one ``config.media_server_view()`` snapshot, so a save that
    lands mid-call can never pair the new URL with the old credentials. No network call is made here: connectivity is
    checked by ``test_connection`` and surfaces per operation.
    """
    global _subsonic_cached
    view = config.media_server_view()
    if not config.subsonic_ready(view):
        return None
    if _saved_url_is_unsafe(view.source, "Subsonic", view.subsonic_url):
        return None
    key = (view.subsonic_url, view.subsonic_user, view.subsonic_password, view.subsonic_api_key, config.plex_verify_ssl)
    with _subsonic_lock:
        if _subsonic_cached is not None and _subsonic_cached[0] == key:
            return _subsonic_cached[1]
        try:
            server = SubsonicMediaServer(
                view.subsonic_url,
                view.subsonic_user,
                view.subsonic_password,
                api_key=view.subsonic_api_key,
                verify_ssl=config.plex_verify_ssl,
            )
        except MediaServerError as exc:
            logger.error("Failed to initialize Subsonic media server: %s", exc.safe_detail)
            return None
        _subsonic_cached = (key, server)
        return server


_jellyfin_lock = threading.Lock()
_jellyfin_cached: Optional[tuple[tuple[Any, ...], JellyfinMediaServer]] = None


def build_jellyfin(config: Config) -> Optional[JellyfinMediaServer]:
    """The Jellyfin adapter for ``config`` (env, else the Settings page), or None when it is incomplete.

    Shared per distinct configuration like :func:`build_subsonic` (one consistent snapshot, replaced adapters are not
    closed under a running sync); no network call is made here.
    """
    global _jellyfin_cached
    view = config.media_server_view()
    if not config.jellyfin_ready(view):
        return None
    if _saved_url_is_unsafe(view.source, "Jellyfin", view.jellyfin_url):
        return None
    key = (view.jellyfin_url, view.jellyfin_api_key, view.jellyfin_user, config.plex_verify_ssl)
    with _jellyfin_lock:
        if _jellyfin_cached is not None and _jellyfin_cached[0] == key:
            return _jellyfin_cached[1]
        try:
            server = JellyfinMediaServer(
                view.jellyfin_url, view.jellyfin_api_key, view.jellyfin_user, verify_ssl=config.plex_verify_ssl
            )
        except MediaServerError as exc:
            logger.error("Failed to initialize Jellyfin media server: %s", exc.safe_detail)
            return None
        _jellyfin_cached = (key, server)
        return server


def build_media_server(config: Config) -> Optional[MediaServer]:
    """The non-Plex adapter (Subsonic or Jellyfin) for ``config``, or None."""
    if config.media_server_type == MEDIA_SERVER_SUBSONIC:
        return build_subsonic(config)
    if config.media_server_type == MEDIA_SERVER_JELLYFIN:
        return build_jellyfin(config)
    return None


def capabilities_for(kind: str) -> ServerCapabilities:
    """Static capabilities of a server kind, known without connecting (the public status endpoint needs them)."""
    if kind == MEDIA_SERVER_PLEX:
        return PLEX_CAPABILITIES
    if kind == MEDIA_SERVER_SUBSONIC:
        return SUBSONIC_CAPABILITIES
    if kind == MEDIA_SERVER_JELLYFIN:
        return JELLYFIN_CAPABILITIES
    return NO_CAPABILITIES


def describe_error(exc: BaseException) -> str:
    """Redacted one-line description of any failure; adapter errors carry their pre-redacted cause."""
    return exc.safe_detail if isinstance(exc, MediaServerError) else safe_exc(exc)


__all__ = [
    "JELLYFIN_CAPABILITIES",
    "NO_CAPABILITIES",
    "ConnectionTest",
    "JellyfinMediaServer",
    "MediaServer",
    "MediaServerAuthError",
    "MediaServerConnectionError",
    "MediaServerError",
    "MediaServerNotFound",
    "MediaServerUnavailable",
    "MediaServerUnsupported",
    "PlaylistSyncOptions",
    "PlexMediaServer",
    "SUBSONIC_CAPABILITIES",
    "ServerCapabilities",
    "ServerTrackRef",
    "ServerUser",
    "SubsonicMediaServer",
    "as_media_server",
    "build_jellyfin",
    "build_media_server",
    "build_subsonic",
    "capabilities_for",
    "describe_error",
    "get_media_server",
    "import_server_users",
    "plex_extras",
]

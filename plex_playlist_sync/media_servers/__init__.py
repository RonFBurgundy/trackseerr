"""Pluggable media-server sinks. Plex is the default; see :mod:`plex_playlist_sync.media_servers.base`."""

import logging
from typing import Any, Optional

from plex_playlist_sync.config import MEDIA_SERVER_PLEX, Config
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
from plex_playlist_sync.media_servers.plex import PLEX_CAPABILITIES, PlexMediaServer, plex_extras
from plex_playlist_sync.redaction import safe_exc

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
    return None


def capabilities_for(kind: str) -> ServerCapabilities:
    """Static capabilities of a server kind, known without connecting (the public status endpoint needs them)."""
    return PLEX_CAPABILITIES if kind == MEDIA_SERVER_PLEX else NO_CAPABILITIES


def describe_error(exc: BaseException) -> str:
    """Redacted one-line description of any failure; adapter errors carry their pre-redacted cause."""
    return exc.safe_detail if isinstance(exc, MediaServerError) else safe_exc(exc)


__all__ = [
    "NO_CAPABILITIES",
    "ConnectionTest",
    "MediaServer",
    "MediaServerAuthError",
    "MediaServerConnectionError",
    "MediaServerError",
    "MediaServerNotFound",
    "MediaServerUnavailable",
    "MediaServerUnsupported",
    "PlaylistSyncOptions",
    "PlexMediaServer",
    "ServerCapabilities",
    "ServerTrackRef",
    "ServerUser",
    "as_media_server",
    "capabilities_for",
    "describe_error",
    "get_media_server",
    "plex_extras",
]

"""Pluggable media-server sinks. Plex is the default; see :mod:`trackseerr.media_servers.base`."""

import logging
from typing import Any, Optional

from trackseerr.config import (
    MEDIA_SERVER_JELLYFIN,
    MEDIA_SERVER_PLEX,
    MEDIA_SERVER_SUBSONIC,
    Config,
)
from trackseerr.media_servers.base import (
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
from trackseerr.media_servers.jellyfin import JELLYFIN_CAPABILITIES, JellyfinMediaServer
from trackseerr.media_servers.navidrome import NavidromeAdmin
from trackseerr.media_servers.plex import PLEX_CAPABILITIES, PlexMediaServer, plex_extras
from trackseerr.media_servers.subsonic import SUBSONIC_CAPABILITIES, SubsonicMediaServer
from trackseerr.media_servers.user_import import import_server_users
from trackseerr.redaction import safe_exc

logger = logging.getLogger(__name__)


def get_media_server(config: Config, *, plex_client: Optional[Any] = None) -> Optional[MediaServer]:
    """The adapter for the primary media server, or None when none is configured or it cannot be reached.

    ``plex_client`` lets callers that already hold a connected client (workers, tests) avoid reconnecting.
    """
    if plex_client is not None:
        return as_media_server(plex_client, music_section=config.plex_music_section)
    from trackseerr.media_servers import registry

    pair = registry.primary_server(config)
    return pair[1] if pair is not None else None


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
    "NavidromeAdmin",
    "PlaylistSyncOptions",
    "PlexMediaServer",
    "SUBSONIC_CAPABILITIES",
    "ServerCapabilities",
    "ServerTrackRef",
    "ServerUser",
    "SubsonicMediaServer",
    "as_media_server",
    "capabilities_for",
    "describe_error",
    "get_media_server",
    "import_server_users",
    "plex_extras",
]

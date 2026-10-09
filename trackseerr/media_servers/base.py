"""Generic media-server interface: the surface every pluggable sink (Plex, Subsonic, Jellyfin, ...) implements.

Only operations that make sense on every server live here. Server-specific extras (Plexamp mixes, Plex playlist
adoption, Plex OAuth, ...) stay on the concrete adapter and are reached through an explicit, capability-gated
accessor such as ``plex_extras()``; they are deliberately not forced into this interface.

Adapters translate their library's native exceptions into the hierarchy below at the adapter boundary, so generic
code never imports ``plexapi`` / ``requests`` just to catch a failure.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Optional, Sequence

from trackseerr.models import Playlist, SyncResult, Track


class MediaServerError(Exception):
    """Base class for every failure a media-server adapter reports.

    ``safe_detail`` is the already-redacted description of the underlying cause (no tokens, no URLs with
    credentials); it is what generic code logs, stores, or returns to clients.
    """

    def __init__(self, message: str = "", *, safe_detail: str = "") -> None:
        super().__init__(message)
        self.safe_detail = safe_detail or type(self).__name__


class MediaServerAuthError(MediaServerError):
    """The server rejected our credentials (bad token / user / password)."""


class MediaServerNotFound(MediaServerError):
    """A playlist, user, or item the server was asked about does not exist."""


class MediaServerConnectionError(MediaServerError):
    """The server could not be reached or timed out (transport level)."""


class MediaServerUnsupported(MediaServerError):
    """The adapter does not implement the requested optional capability."""


class MediaServerUnavailable(Exception):
    """A media-server-only feature was used while no media server is configured (rendered as a flat 409).

    This is a configuration state, not an outage: a configured-but-unreachable server raises
    :class:`MediaServerConnectionError` instead.
    """


@dataclass(frozen=True)
class ServerCapabilities:
    """What a server supports; drives the capability flags the UI reads."""

    playlists: bool = False
    users: bool = False
    library_refresh: bool = False
    mixes: bool = False
    search: bool = False
    file_paths: bool = False  # can list library file paths; an adapter may still raise MediaServerUnsupported at iteration

    def to_dict(self) -> dict[str, bool]:
        return {
            "playlists": self.playlists,
            "users": self.users,
            "mixes": self.mixes,
            "library_refresh": self.library_refresh,
            "file_paths": self.file_paths,
        }


NO_CAPABILITIES = ServerCapabilities()


@dataclass(frozen=True)
class ServerFileRef:
    """One audio file as the server reports it. ``path`` is absolute as the SERVER sees it (relative to the music
    folder when the adapter has ``paths_relative``). ``container`` is the lowercased extension/codec, '' if unknown."""

    server_id: str
    path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    container: str = ""


@dataclass(frozen=True)
class ServerTrackRef:
    """A track as the server knows it. ``id`` is the server's own identifier; ``native`` is the opaque server
    object (adapters use it to build playlists, generic code never touches it)."""

    id: str
    title: str = ""
    artist: str = ""
    album: str = ""
    native: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class ServerUser:
    """A server-side account a playlist can be pushed to."""

    id: str
    name: str
    is_admin: bool = False
    extra: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class ConnectionTest:
    ok: bool
    message: str = ""


@dataclass
class PlaylistSyncOptions:
    """Per-sync knobs, identical for every server. ``db`` supplies match overrides / registry bookkeeping;
    ``skip_item_ids`` are server item ids that must never be written to (e.g. the source of an adopted playlist)."""

    append: bool = False
    add_description: bool = True
    add_poster: bool = True
    write_missing_as_csv: bool = False
    data_dir: str = "/data"
    threshold: float = 0.9
    db: Optional[Any] = None
    skip_item_ids: Optional[set[str]] = None

    @classmethod
    def from_config(
        cls, config: Any, db: Optional[Any] = None, skip_item_ids: Optional[set[str]] = None
    ) -> "PlaylistSyncOptions":
        return cls(
            append=config.append_instead_of_sync,
            add_description=config.add_playlist_description,
            add_poster=config.add_playlist_poster,
            write_missing_as_csv=config.write_missing_as_csv,
            data_dir=config.data_dir,
            threshold=config.search_similarity_threshold,
            db=db,
            skip_item_ids=skip_item_ids,
        )


class MediaServer(ABC):
    """A pluggable media-server sink."""

    kind: str = ""
    paths_relative: bool = False  # True when ServerFileRef.path is relative to the music folder (Subsonic)

    @property
    @abstractmethod
    def capabilities(self) -> ServerCapabilities:
        """What this server supports."""

    @abstractmethod
    def test_connection(self) -> ConnectionTest:
        """Cheap reachability/credential check. Never raises for an unreachable server: reports ``ok=False``."""

    @abstractmethod
    def match_track(self, track: Track, threshold: float = 0.9, db: Optional[Any] = None) -> Optional[ServerTrackRef]:
        """Find ``track`` in the server library, or None."""

    @abstractmethod
    def match_playlist_tracks(
        self, tracks: Sequence[Track], threshold: float = 0.9, db: Optional[Any] = None
    ) -> tuple[list[ServerTrackRef], list[Track]]:
        """Match many tracks; returns ``(matched, missing)`` with ``matched`` in input order."""

    @abstractmethod
    def sync_playlist(
        self, playlist: Playlist, targets: Sequence[str], options: PlaylistSyncOptions
    ) -> list[SyncResult]:
        """Match ``playlist`` against the library and create or update it for each target user name.

        Re-syncing is idempotent and keeps the source order; with ``options.append`` tracks are only added,
        otherwise tracks no longer in the source are removed. An empty ``targets`` means the server's default
        account and yields one result. Per-target failures are reported as unsuccessful ``SyncResult`` entries;
        unexpected server failures raise a :class:`MediaServerError`.
        """

    @abstractmethod
    def refresh_library(self) -> bool:
        """Ask the server to rescan its music library. True when a scan was triggered."""

    @abstractmethod
    def search_tracks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Free-text track search for manual matching and availability checks (dicts with title/artist/album)."""

    def list_users(self) -> list[ServerUser]:
        """Accounts playlists can target. Only valid when ``capabilities.users``."""
        raise MediaServerUnsupported(f"{self.kind or 'This media server'} does not support listing users")

    def iter_library_files(self) -> Iterator[ServerFileRef]:
        """Every audio file in the server's music libraries, paged and lazily. Raises MediaServerUnsupported when the
        server cannot (or will not, e.g. a non-admin key) expose file paths."""
        raise MediaServerUnsupported(f"{self.kind or 'This media server'} does not expose library file paths")

    def last_scan_at(self) -> Optional[datetime]:
        """When the server last finished scanning its music library (timezone-aware UTC), or None if unknown."""
        return None

    def library_roots(self) -> list[str]:
        """Music library folder roots as the server sees them; empty when unknown or relative."""
        return []


def as_media_server(server_or_client: Any, music_section: Optional[str] = None) -> Optional[MediaServer]:
    """Normalise whatever a flow was handed into a :class:`MediaServer`.

    ``None`` stays ``None``; an existing adapter is returned as is; anything else is a Plex client (the only
    server whose raw client is still injected into flows) and is wrapped in a ``PlexMediaServer``.
    """
    if server_or_client is None:
        return None
    if isinstance(server_or_client, MediaServer):
        from trackseerr.media_servers.plex import PlexMediaServer

        if isinstance(server_or_client, PlexMediaServer) and music_section and not getattr(server_or_client, "_music_section", None):
            server_or_client._music_section = music_section
        return server_or_client
    from trackseerr.media_servers.plex import PlexMediaServer

    return PlexMediaServer(server_or_client, music_section=music_section)


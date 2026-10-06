"""Plex adapter: wraps the existing :class:`PlexClient` behind the generic :class:`MediaServer` interface.

This is the only module in the generic-flow path that knows about ``plexapi`` exceptions: they are translated to the
``MediaServerError`` hierarchy here. Plex-only features (Plexamp mixes, the playlist registry / adoption, home-user
switching, OAuth, machine identifier) are reached through :meth:`PlexMediaServer.plex_extras` / :func:`plex_extras`,
never through the generic interface.
"""

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional, Sequence

import requests
from plexapi.exceptions import NotFound, PlexApiException, Unauthorized

from plex_playlist_sync.clients.plex import PlexClient
from plex_playlist_sync.media_servers.base import (
    ConnectionTest,
    MediaServer,
    MediaServerAuthError,
    MediaServerConnectionError,
    MediaServerError,
    MediaServerNotFound,
    PlaylistSyncOptions,
    ServerCapabilities,
    ServerFileRef,
    ServerTrackRef,
    ServerUser,
)
from plex_playlist_sync.models import Playlist, SyncResult, Track
from plex_playlist_sync.redaction import redact_text, safe_exc

logger = logging.getLogger(__name__)

PLEX_CAPABILITIES = ServerCapabilities(playlists=True, users=True, library_refresh=True, mixes=True, search=True, file_paths=True)


def translate_plex_error(exc: BaseException) -> MediaServerError:
    """Map a plexapi / requests failure to the generic hierarchy, keeping the redacted cause in ``safe_detail``."""
    detail = safe_exc(exc)
    if isinstance(exc, Unauthorized):
        return MediaServerAuthError(detail, safe_detail=detail)
    if isinstance(exc, NotFound):
        return MediaServerNotFound(detail, safe_detail=detail)
    if isinstance(exc, requests.RequestException):
        return MediaServerConnectionError(detail, safe_detail=detail)
    return MediaServerError(detail, safe_detail=detail)


@contextmanager
def _translated() -> Iterator[None]:
    try:
        yield
    except (PlexApiException, requests.RequestException) as exc:
        raise translate_plex_error(exc) from exc


_FILE_PAGE = 500


def _utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.astimezone()  # plexapi yields naive local datetimes
    return value.astimezone(timezone.utc)


def _ref(item: Any) -> ServerTrackRef:
    return ServerTrackRef(
        id=str(getattr(item, "ratingKey", "") or ""),
        title=str(getattr(item, "title", "") or ""),
        artist=str(getattr(item, "grandparentTitle", "") or ""),
        album=str(getattr(item, "parentTitle", "") or ""),
        native=item,
    )


class PlexMediaServer(MediaServer):
    kind = "plex"

    def __init__(self, client: PlexClient) -> None:
        self._client = client

    @property
    def capabilities(self) -> ServerCapabilities:
        return PLEX_CAPABILITIES

    def plex_extras(self) -> PlexClient:
        """The wrapped client, for Plex-only features. Callers must be on a Plex-specific path."""
        return self._client

    def test_connection(self) -> ConnectionTest:
        fn = getattr(self._client, "test_connection", None)
        if fn is None:
            return ConnectionTest(True, "Connected to Plex")
        res = fn()
        if isinstance(res, tuple):
            return ConnectionTest(bool(res[0]), redact_text(str(res[1])))
        if isinstance(res, dict):
            online = bool(res.get("online", False))
            msg = str(res.get("message") or res.get("error") or ("Connected to Plex" if online else "Plex unreachable"))
            return ConnectionTest(online, msg)
        online = bool(res)
        return ConnectionTest(online, "Connected to Plex" if online else "Plex unreachable")

    def match_track(self, track: Track, threshold: float = 0.9, db: Optional[Any] = None) -> Optional[ServerTrackRef]:
        with _translated():
            found = self._client.match_track(track, threshold=threshold, db=db)
        return None if found is None else _ref(found)

    def match_playlist_tracks(
        self, tracks: Sequence[Track], threshold: float = 0.9, db: Optional[Any] = None
    ) -> tuple[list[ServerTrackRef], list[Track]]:
        with _translated():
            matched, missing = self._client.match_playlist_tracks(list(tracks), threshold=threshold, db=db)
        return [_ref(m) for m in matched], list(missing)

    def sync_playlist(
        self, playlist: Playlist, targets: Sequence[str], options: PlaylistSyncOptions
    ) -> list[SyncResult]:
        with _translated():
            if not targets:
                return [
                    self._client.sync_playlist(
                        playlist=playlist,
                        append=options.append,
                        add_description=options.add_description,
                        add_poster=options.add_poster,
                        write_missing_as_csv=options.write_missing_as_csv,
                        data_dir=options.data_dir,
                        threshold=options.threshold,
                    )
                ]
            return list(
                self._client.sync_playlist_to_users(
                    playlist=playlist,
                    target_usernames=list(targets),
                    append=options.append,
                    add_description=options.add_description,
                    add_poster=options.add_poster,
                    write_missing_as_csv=options.write_missing_as_csv,
                    data_dir=options.data_dir,
                    threshold=options.threshold,
                    db=options.db,
                    skip_rating_keys=options.skip_item_ids,
                )
            )

    def refresh_library(self) -> bool:
        fn = getattr(self._client, "refresh_music_library", None)
        if fn is None:
            return False
        with _translated():
            return bool(fn())

    def search_tracks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        with _translated():
            return self._client.search_library_tracks(query=query, limit=limit)

    def list_users(self) -> list[ServerUser]:
        with _translated():
            raw = self._client.get_home_users()
        return [
            ServerUser(
                id=str(u["id"]),
                name=str(u["username"]),
                is_admin=bool(u.get("is_admin", False)),
                extra={"email": u.get("email", ""), "thumb": u.get("thumb", "")},
            )
            for u in raw
        ]

    def _music_sections(self) -> list[Any]:
        with _translated():
            sections = self._client.server.library.sections()
        return [s for s in sections if getattr(s, "type", "") == "artist"]

    def iter_library_files(self) -> Iterator[ServerFileRef]:
        """Every part of every track in every music section; sections are searched in pages of 500."""
        for section in self._music_sections():
            start = 0
            while True:
                with _translated():
                    tracks = list(section.search(libtype="track", container_start=start, container_size=_FILE_PAGE))
                for track in tracks:
                    for media in getattr(track, "media", None) or []:
                        for part in getattr(media, "parts", None) or []:
                            path = str(getattr(part, "file", "") or "")
                            if not path:
                                continue
                            container = str(getattr(part, "container", "") or getattr(media, "container", "") or "")
                            yield ServerFileRef(
                                server_id=str(getattr(track, "ratingKey", "") or ""),
                                path=path,
                                title=str(getattr(track, "title", "") or ""),
                                artist=str(getattr(track, "grandparentTitle", "") or ""),
                                album=str(getattr(track, "parentTitle", "") or ""),
                                container=container.lower(),
                            )
                if len(tracks) < _FILE_PAGE:
                    break
                start += len(tracks)

    def last_scan_at(self) -> Optional[datetime]:
        stamps = [
            t
            for t in (_utc(getattr(s, "scannedAt", None) or getattr(s, "updatedAt", None)) for s in self._music_sections())
            if t is not None
        ]
        return max(stamps) if stamps else None

    def library_roots(self) -> list[str]:
        return [str(loc) for s in self._music_sections() for loc in (getattr(s, "locations", None) or []) if loc]


def plex_extras(server: Optional[MediaServer]) -> Optional[PlexClient]:
    """The raw :class:`PlexClient` when ``server`` is Plex, else None. The one gate to Plex-only features."""
    return server.plex_extras() if isinstance(server, PlexMediaServer) else None


def refresh_mix_snapshots(server: Optional[MediaServer], db: Any) -> int:
    """Plexamp auto-mix snapshots (Plex-only). 0 on any other server; translated errors on failure."""
    client = plex_extras(server)
    if client is None:
        return 0
    with _translated():
        return int(client.refresh_auto_mix_snapshots(db) or 0)


def read_playlist_items(server: Optional[MediaServer], username: str, rating_key: str) -> list[Any]:
    """Items of a Plex playlist owned by ``username`` (Plex-only: adopted-playlist sources)."""
    client = plex_extras(server)
    if client is None:
        raise MediaServerNotFound("Plex source playlists are unavailable on this media server")
    with _translated():
        user_server = client.get_user_server(username)
        source = client.get_playlist(user_server, rating_key)
        return list(client.get_playlist_items(source))

import logging
from typing import TYPE_CHECKING, List, Optional

from .clients.deezer import DeezerClient
from .clients.plex import PlexClient
from .media_servers import PlaylistSyncOptions, as_media_server
from .clients.spotify import SpotifyClient
from .config import Config
from .models import Playlist, SyncResult
from .native_match import match_playlist_tracks_native

if TYPE_CHECKING:
    from .storage import Database

logger = logging.getLogger(__name__)


class SyncCoordinator:
    """Orchestrates syncing Spotify and Deezer playlists into Plex Media Server.

    ``plex_client`` is None when no media server is configured: playlists are then fetched and matched against the
    native library (when a ``db`` is given) but nothing is pushed anywhere.
    """

    def __init__(
        self,
        config: Config,
        plex_client: Optional[PlexClient],
        spotify_client: Optional[SpotifyClient] = None,
        deezer_client: Optional[DeezerClient] = None,
        db: Optional["Database"] = None,
    ):
        self.config = config
        self.plex = plex_client
        self.spotify = spotify_client
        self.deezer = deezer_client
        self.db = db

    def _sync_one(self, pl: Playlist) -> SyncResult:
        server = as_media_server(self.plex)
        if server is not None:
            return server.sync_playlist(pl, [], PlaylistSyncOptions.from_config(self.config))[0]
        return self._match_without_media_server(pl)

    def _match_without_media_server(self, pl: Playlist) -> SyncResult:
        """No media server: nothing is pushed. With a database the playlist is matched against the native library
        and its missing tracks are recorded, so monitoring and wanted keep working."""
        if self.db is None:
            logger.info("No media server: playlist push skipped for '%s'", pl.name)
            return SyncResult(pl.name, len(pl.tracks), 0, 0, True)
        matched, missing = match_playlist_tracks_native(self.db, pl.tracks)
        if self.db.get_playlist(pl.id) is not None:
            self.db.record_sync_result(pl.id, status="success", missing_tracks=missing)
        logger.info(
            "No media server: matched '%s' against the native library (%d matched, %d missing)",
            pl.name,
            len(matched),
            len(missing),
        )
        return SyncResult(pl.name, len(pl.tracks), len(matched), len(missing), True)

    def run_sync_cycle(self) -> List[SyncResult]:
        """Execute one complete sync cycle across all configured music providers."""
        results: List[SyncResult] = []
        logger.info("================ Starting Playlist Synchronization Cycle ================")

        # 1. Spotify Sync
        if self.spotify and self.config.has_spotify:
            suffix = " - Spotify" if self.config.append_service_suffix else ""
            logger.info("Syncing Spotify playlists...")
            sp_playlists = self.spotify.fetch_all_playlists(
                user_id=self.config.spotify_user_id,
                playlist_ids=self.config.spotify_playlist_ids,
                suffix=suffix,
            )

            if sp_playlists:
                for pl in sp_playlists:
                    results.append(self._sync_one(pl))
            else:
                logger.info("No Spotify playlists discovered for configured user/IDs")
        else:
            logger.info("Spotify sync skipped (no credentials or target playlists configured)")

        # 2. Deezer Sync
        if self.deezer and self.config.has_deezer:
            suffix = " - Deezer" if self.config.append_service_suffix else ""
            logger.info("Syncing Deezer playlists...")
            dz_playlists = self.deezer.fetch_all_playlists(
                user_id=self.config.deezer_user_id,
                playlist_ids=self.config.deezer_playlist_ids,
                suffix=suffix,
            )

            if dz_playlists:
                for pl in dz_playlists:
                    results.append(self._sync_one(pl))
            else:
                logger.info("No Deezer playlists discovered for configured user/IDs")
        else:
            logger.info("Deezer sync skipped (no user or playlist IDs configured)")

        # Summary
        success_count = sum(1 for r in results if r.success)
        total_matched = sum(r.matched_tracks for r in results)
        total_missing = sum(r.missing_tracks for r in results)
        logger.info(
            "Sync cycle finished: %d/%d playlist(s) succeeded, %d track(s) matched, %d missing",
            success_count,
            len(results),
            total_matched,
            total_missing,
        )
        if self.db:
            try:
                self.db.record_event(
                    "sync_completed",
                    f"Playlist sync completed: {len(results)} playlists processed ({total_matched} matched, {total_missing} missing)",
                    source="SyncCoordinator",
                    severity="info",
                )
            except Exception as ev_err:
                logger.warning("Failed to record sync_completed event: %s", ev_err)
        logger.info("========================================================================")
        return results

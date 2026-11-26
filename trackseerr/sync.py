import logging
from typing import TYPE_CHECKING, List, Optional

from .clients.deezer import DeezerClient
from .clients.plex import PlexClient
from .media_servers import MediaServerError, PlaylistSyncOptions, as_media_server, describe_error, registry
from .media_servers.user_logins import sync_subsonic_for_users
from .clients.spotify import SpotifyClient
from .config import MEDIA_SERVER_SUBSONIC, Config
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

    def _push_to_server(
        self, rec: registry.ServerRecord, adapter: object, pl: Playlist, options: PlaylistSyncOptions
    ) -> list[SyncResult]:
        try:
            if rec.kind == MEDIA_SERVER_SUBSONIC:
                return sync_subsonic_for_users(self.db, self.config, rec, adapter, pl, [], options)
            return list(adapter.sync_playlist(pl, [], options))  # type: ignore[attr-defined]
        except MediaServerError as exc:
            logger.error("Error syncing playlist '%s' to %s: %s", pl.name, rec.name, exc.safe_detail)
            return [SyncResult(pl.name, len(pl.tracks), 0, len(pl.tracks), False, exc.safe_detail, missing=list(pl.tracks))]
        except Exception as e:
            detail = describe_error(e)
            logger.error("Error syncing playlist '%s' to %s: %s", pl.name, rec.name, detail)
            logger.debug("Server playlist sync traceback", exc_info=True)
            return [SyncResult(pl.name, len(pl.tracks), 0, len(pl.tracks), False, detail, missing=list(pl.tracks))]

    def _sync_one(self, pl: Playlist) -> SyncResult:
        if registry.is_installed():
            servers = registry.media_servers(registry.ROLE_PLAYLISTS, self.config)
            if not servers:
                return self._match_without_media_server(pl)
            options = PlaylistSyncOptions.from_config(self.config, db=self.db)
            first_result: Optional[SyncResult] = None
            for rec, adapter in servers:
                res_list = self._push_to_server(rec, adapter, pl, options)
                if first_result is None and res_list:
                    first_result = res_list[0]
            if first_result is not None:
                return first_result
            return self._match_without_media_server(pl)

        server = as_media_server(self.plex)
        if server is not None:
            return server.sync_playlist(pl, [], PlaylistSyncOptions.from_config(self.config, db=self.db))[0]
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

from unittest.mock import MagicMock

from trackseerr.config import Config
from trackseerr.models import Playlist, SyncResult, Track
from trackseerr.sync import SyncCoordinator


def test_sync_coordinator_cycle():
    cfg = Config(
        plex_url="http://localhost:32400",
        plex_token="token",
        spotify_client_id="cid",
        spotify_client_secret="csecret",
        spotify_user_id="sp_user",
        deezer_user_id="dz_user",
    )

    mock_plex = MagicMock()
    mock_spotify = MagicMock()
    mock_deezer = MagicMock()

    sp_pl = Playlist(id="sp_1", name="Spotify Top 50", tracks=[Track("Song A", "Artist A", "Album A")])
    dz_pl = Playlist(id="dz_1", name="Deezer Top 50", tracks=[Track("Song B", "Artist B", "Album B")])

    mock_spotify.fetch_all_playlists.return_value = [sp_pl]
    mock_deezer.fetch_all_playlists.return_value = [dz_pl]

    mock_plex.sync_playlist.side_effect = [
        SyncResult(playlist_name="Spotify Top 50", total_tracks=1, matched_tracks=1, missing_tracks=0, success=True),
        SyncResult(playlist_name="Deezer Top 50", total_tracks=1, matched_tracks=1, missing_tracks=0, success=True),
    ]

    coord = SyncCoordinator(
        config=cfg,
        plex_client=mock_plex,
        spotify_client=mock_spotify,
        deezer_client=mock_deezer,
    )

    results = coord.run_sync_cycle()

    assert len(results) == 2
    assert results[0].playlist_name == "Spotify Top 50"
    assert results[1].playlist_name == "Deezer Top 50"
    assert mock_plex.sync_playlist.call_count == 2

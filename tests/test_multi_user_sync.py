"""Comprehensive tests for multi-user Plex playlist synchronization."""

import urllib.parse
from unittest.mock import MagicMock, patch

from plexapi.exceptions import NotFound

from trackseerr.clients.plex import PlexClient
from trackseerr.models import Playlist, Track


class MockArtist:
    def __init__(self, title: str):
        self.title = title


class MockAlbum:
    def __init__(self, title: str):
        self.title = title


class MockPlexTrack:
    def __init__(self, title: str, artist: str, album: str):
        self.title = title
        self._artist = MockArtist(artist)
        self._album = MockAlbum(album)

    def artist(self):
        return self._artist

    def album(self):
        return self._album


class TestPlexMachineIdentifier:
    """Tests for PlexClient.machine_identifier property."""

    @patch("trackseerr.clients.plex.PlexServer")
    def test_machine_identifier_returned(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server.machineIdentifier = "unique-machine-identifier-12345"
        mock_server_cls.return_value = mock_server

        client = PlexClient("http://localhost:32400", "token")
        assert client.machine_identifier == "unique-machine-identifier-12345"


class TestPlexGetHomeUsers:
    """Tests for PlexClient.get_home_users."""

    @patch("trackseerr.clients.plex.PlexServer")
    def test_get_home_users_with_managed_users(self, mock_server_cls):
        mock_server = MagicMock()
        mock_account = MagicMock()
        mock_account.username = "plex_admin"
        mock_account.id = "admin-id-1"
        mock_account.email = "admin@plex.tv"
        mock_account.thumb = "https://plex.tv/admin.png"

        # Mock managed users
        user_1 = MagicMock()
        user_1.username = "alice"
        user_1.title = "Alice"
        user_1.id = "alice-id-2"
        user_1.email = ""
        user_1.thumb = "https://plex.tv/alice.png"

        user_2 = MagicMock()
        user_2.username = None
        user_2.title = "Bob (Managed)"
        user_2.name = "Bob"
        user_2.id = "bob-id-3"
        user_2.email = ""
        user_2.thumb = ""

        mock_account.users.return_value = [user_1, user_2]
        mock_server.myPlexAccount.return_value = mock_account
        mock_server_cls.return_value = mock_server

        client = PlexClient("http://localhost:32400", "token")
        users = client.get_home_users()

        assert len(users) == 3
        # Admin
        assert users[0]["id"] == "admin-id-1"
        assert users[0]["username"] == "plex_admin"
        assert users[0]["email"] == "admin@plex.tv"
        assert users[0]["is_admin"] is True

        # Alice
        assert users[1]["id"] == "alice-id-2"
        assert users[1]["username"] == "alice"
        assert users[1]["is_admin"] is False

        # Bob
        assert users[2]["id"] == "bob-id-3"
        assert users[2]["username"] == "Bob (Managed)"
        assert users[2]["is_admin"] is False

    @patch("trackseerr.clients.plex.PlexServer")
    def test_get_home_users_deduplicates_admin_in_users(self, mock_server_cls):
        mock_server = MagicMock()
        mock_account = MagicMock()
        mock_account.username = "plex_admin"
        mock_account.id = "admin-1"
        mock_account.email = "admin@plex.tv"

        admin_dup = MagicMock()
        admin_dup.username = "plex_admin"
        admin_dup.id = "admin-1"

        normal_user = MagicMock()
        normal_user.username = "charlie"
        normal_user.id = "charlie-2"

        mock_account.users.return_value = [admin_dup, normal_user]
        mock_server.myPlexAccount.return_value = mock_account
        mock_server_cls.return_value = mock_server

        client = PlexClient("http://localhost:32400", "token")
        users = client.get_home_users()

        assert len(users) == 2
        assert [u["username"] for u in users] == ["plex_admin", "charlie"]

    @patch("trackseerr.clients.plex.PlexServer")
    def test_get_home_users_non_plexpass_fallback(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server.friendlyName = "Local Media Server"
        # Simulate non-PlexPass or not logged into plex.tv
        mock_server.myPlexAccount.side_effect = Exception("Not connected to myPlex")
        mock_server_cls.return_value = mock_server

        client = PlexClient("http://localhost:32400", "token")
        users = client.get_home_users()

        assert len(users) == 1
        assert users[0]["id"] == "admin"
        assert users[0]["username"] == "Local Media Server"
        assert users[0]["is_admin"] is True


class TestMultiUserSync:
    """Tests for PlexClient.sync_playlist_to_users."""

    @patch("trackseerr.clients.plex.PlexServer")
    def test_track_match_caching_across_users(self, mock_server_cls):
        """Verify that track search/matching occurs ONCE regardless of how many users are synced."""
        mock_server = MagicMock()
        mock_account = MagicMock()
        mock_account.username = "admin_user"
        mock_server.myPlexAccount.return_value = mock_account

        mock_track = MockPlexTrack("Track 1", "Artist 1", "Album 1")
        mock_server.search.return_value = [mock_track]

        user_server_alice = MagicMock()
        user_server_bob = MagicMock()

        def switch_user_side_effect(user):
            if user == "alice":
                return user_server_alice
            elif user == "bob":
                return user_server_bob
            raise NotFound("User not found")

        mock_server.switchUser.side_effect = switch_user_side_effect
        mock_server.playlist.side_effect = [NotFound("Not found"), MagicMock()]
        user_server_alice.playlist.side_effect = [NotFound("Not found"), MagicMock()]
        user_server_bob.playlist.side_effect = [NotFound("Not found"), MagicMock()]
        mock_server_cls.return_value = mock_server

        client = PlexClient("http://localhost:32400", "token")

        playlist = Playlist(
            id="p1",
            name="Multi User Playlist",
            tracks=[Track("Track 1", "Artist 1", "Album 1")],
        )

        results = client.sync_playlist_to_users(
            playlist=playlist,
            target_usernames=["admin_user", "alice", "bob"],
        )

        # Track matching MUST only be executed once across all 3 users
        assert mock_server.search.call_count == 1

        # All 3 users should have succeeded
        assert len(results) == 3
        assert all(r.success for r in results)
        assert all(r.matched_tracks == 1 for r in results)

        # Admin user synced directly on self.server
        mock_server.createPlaylist.assert_called_once()
        # Alice synced on user_server_alice
        user_server_alice.createPlaylist.assert_called_once()
        # Bob synced on user_server_bob
        user_server_bob.createPlaylist.assert_called_once()

    @patch("trackseerr.clients.plex.PlexServer")
    def test_admin_poster_injection_fallback_on_managed_user_401(self, mock_server_cls):
        """Verify managed user 401 uploadPoster failure triggers admin session poster injection."""
        mock_server = MagicMock()
        mock_account = MagicMock()
        mock_account.username = "admin_user"
        mock_server.myPlexAccount.return_value = mock_account

        mock_track = MockPlexTrack("Track 1", "Artist 1", "Album 1")
        mock_server.search.return_value = [mock_track]

        # Managed user server
        managed_server = MagicMock()
        managed_playlist = MagicMock()
        managed_playlist.ratingKey = "98765"
        # Simulate managed user 401 bug when calling uploadPoster
        managed_playlist.uploadPoster.side_effect = Exception("401 Unauthorized: Managed user cannot upload poster")
        managed_server.playlist.return_value = managed_playlist

        mock_server.switchUser.return_value = managed_server
        mock_server_cls.return_value = mock_server

        client = PlexClient("http://localhost:32400", "token")

        poster_url = "https://images.example.com/cover.jpg"
        playlist = Playlist(
            id="p2",
            name="Managed User Playlist",
            poster=poster_url,
            tracks=[Track("Track 1", "Artist 1", "Album 1")],
        )

        results = client.sync_playlist_to_users(
            playlist=playlist,
            target_usernames=["managed_user"],
            add_poster=True,
        )

        assert len(results) == 1
        assert results[0].success is True

        # Verify managed user uploadPoster was attempted and failed
        managed_playlist.uploadPoster.assert_called_once_with(url=poster_url)

        # Verify admin server session executed fallback query via /library/metadata/<ratingKey>/posters?url=...
        encoded_poster = urllib.parse.quote_plus(poster_url)
        expected_key = f"/library/metadata/98765/posters?url={encoded_poster}"
        mock_server.query.assert_called_once()
        actual_key = mock_server.query.call_args[0][0]
        assert actual_key == expected_key

    @patch("trackseerr.clients.plex.PlexServer")
    def test_sync_playlist_to_users_zero_matches(self, mock_server_cls, tmp_path):
        mock_server = MagicMock()
        mock_server.search.return_value = []
        mock_server_cls.return_value = mock_server

        client = PlexClient("http://localhost:32400", "token")
        playlist = Playlist(
            id="p3",
            name="Unmatched Playlist",
            tracks=[Track("Ghost Track", "Phantom Artist", "Void Album")],
        )

        results = client.sync_playlist_to_users(
            playlist=playlist,
            target_usernames=["alice", "bob"],
            write_missing_as_csv=True,
            data_dir=str(tmp_path),
        )

        assert len(results) == 2
        assert not results[0].success
        assert not results[1].success
        assert results[0].missing_tracks == 1

        # Missing CSV should be created once
        csv_file = tmp_path / "Unmatched Playlist.csv"
        assert csv_file.exists()
        assert "Ghost Track" in csv_file.read_text()

    @patch("trackseerr.clients.plex.PlexServer")
    def test_sync_playlist_to_users_partial_user_failure(self, mock_server_cls):
        mock_server = MagicMock()
        mock_account = MagicMock()
        mock_account.username = "admin"
        mock_server.myPlexAccount.return_value = mock_account

        mock_track = MockPlexTrack("Track 1", "Artist 1", "Album 1")
        mock_server.search.return_value = [mock_track]

        # Switch user fails for bad_user, succeeds for good_user
        def switch_side_effect(user):
            if user == "bad_user":
                raise NotFound("Managed user not found")
            return MagicMock()

        mock_server.switchUser.side_effect = switch_side_effect
        mock_server_cls.return_value = mock_server

        client = PlexClient("http://localhost:32400", "token")
        playlist = Playlist(
            id="p4",
            name="Partial Playlist",
            tracks=[Track("Track 1", "Artist 1", "Album 1")],
        )

        results = client.sync_playlist_to_users(
            playlist=playlist,
            target_usernames=["bad_user", "good_user"],
        )

        assert len(results) == 2
        assert results[0].success is False
        assert "bad_user" in results[0].error
        assert results[1].success is True

    @patch("trackseerr.clients.plex.PlexServer")
    def test_sync_playlist_to_users_empty_targets(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server

        client = PlexClient("http://localhost:32400", "token")
        playlist = Playlist(id="p5", name="Empty Targets", tracks=[])

        results = client.sync_playlist_to_users(playlist=playlist, target_usernames=[])
        assert results == []

    @patch("trackseerr.clients.plex.PlexServer")
    def test_get_home_users_users_call_fails(self, mock_server_cls):
        mock_server = MagicMock()
        mock_account = MagicMock()
        mock_account.username = "plex_admin"
        mock_account.id = "admin-1"
        mock_account.users.side_effect = Exception("Users endpoint unavailable")
        mock_server.myPlexAccount.return_value = mock_account
        mock_server_cls.return_value = mock_server

        client = PlexClient("http://localhost:32400", "token")
        users = client.get_home_users()
        assert len(users) == 1
        assert users[0]["username"] == "plex_admin"
        assert users[0]["is_admin"] is True

    @patch("trackseerr.clients.plex.PlexServer")
    def test_admin_poster_injection_fallback_failure_does_not_crash(self, mock_server_cls):
        mock_server = MagicMock()
        mock_account = MagicMock()
        mock_account.username = "admin"
        mock_server.myPlexAccount.return_value = mock_account

        mock_track = MockPlexTrack("Track 1", "Artist 1", "Album 1")
        mock_server.search.return_value = [mock_track]

        managed_server = MagicMock()
        managed_playlist = MagicMock()
        managed_playlist.ratingKey = "111"
        managed_playlist.uploadPoster.side_effect = Exception("401 Unauthorized")
        managed_server.playlist.return_value = managed_playlist

        # Both user upload and admin fallback fail
        mock_server.switchUser.return_value = managed_server
        mock_server.query.side_effect = Exception("Admin query failed")
        mock_server_cls.return_value = mock_server

        client = PlexClient("http://localhost:32400", "token")
        playlist = Playlist(
            id="p6",
            name="Double Fail Poster",
            poster="https://image.com/art.png",
            tracks=[Track("Track 1", "Artist 1", "Album 1")],
        )

        results = client.sync_playlist_to_users(
            playlist=playlist,
            target_usernames=["managed_user"],
            add_poster=True,
        )
        assert len(results) == 1
        assert results[0].success is True

    @patch("trackseerr.clients.plex.PlexServer")
    def test_sync_playlist_to_users_cleans_obsolete_missing_csv(self, mock_server_cls, tmp_path):
        mock_server = MagicMock()
        mock_account = MagicMock()
        mock_account.username = "admin"
        mock_server.myPlexAccount.return_value = mock_account

        mock_track = MockPlexTrack("Found Track", "Artist 1", "Album 1")
        mock_server.search.return_value = [mock_track]
        mock_server_cls.return_value = mock_server

        client = PlexClient("http://localhost:32400", "token")
        csv_file = tmp_path / "Clean CSV Playlist.csv"
        csv_file.write_text("dummy old missing tracks")
        assert csv_file.exists()

        playlist = Playlist(
            id="p7",
            name="Clean CSV Playlist",
            tracks=[Track("Found Track", "Artist 1", "Album 1")],
        )

        results = client.sync_playlist_to_users(
            playlist=playlist,
            target_usernames=["admin"],
            write_missing_as_csv=True,
            data_dir=str(tmp_path),
        )
        assert len(results) == 1
        assert results[0].success is True
        # Obsolete CSV was deleted
        assert not csv_file.exists()

    @patch("trackseerr.clients.plex.PlexServer")
    def test_sync_playlist_to_users_fallback_admin_friendly_name(self, mock_server_cls):
        mock_server = MagicMock()
        # myPlexAccount raises
        mock_server.myPlexAccount.side_effect = Exception("Not available")
        mock_server.friendlyName = "HomePlex"

        mock_track = MockPlexTrack("Track 1", "Artist 1", "Album 1")
        mock_server.search.return_value = [mock_track]
        mock_server_cls.return_value = mock_server

        client = PlexClient("http://localhost:32400", "token")
        playlist = Playlist(
            id="p8",
            name="Friendly Server Playlist",
            tracks=[Track("Track 1", "Artist 1", "Album 1")],
        )

        results = client.sync_playlist_to_users(
            playlist=playlist,
            target_usernames=["homeplex"],
        )
        assert len(results) == 1
        assert results[0].success is True
        # Since 'homeplex' matches friendlyName, switchUser was not called
        mock_server.switchUser.assert_not_called()



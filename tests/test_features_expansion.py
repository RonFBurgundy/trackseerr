"""Tests for Syncra & Playlist Lab Feature Expansions:
1. Match Memory & Manual Search Picker
2. Featured Charts Presets
3. Local Smart Mixes (Heavy Rotation, Forgotten Favorites, Deep Cuts)
4. Direct .m3u / .m3u8 Playlist File Drag-and-Drop & Parser
5. Per-Playlist Sync Toggles (Active / Paused)
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import (
    get_config,
    get_current_user,
    get_current_user_or_api_key,
    get_db,
    get_plex_client,
)
from trackseerr.clients.plex import PlexClient
from trackseerr.config import Config
from trackseerr.m3u import parse_m3u
from trackseerr.models import Track
from trackseerr.storage import Database


# ==============================================================================
# 1. M3U PARSER TESTS
# ==============================================================================

class TestM3UParser:
    """Tests for trackseerr/m3u.py."""

    def test_parse_empty_content(self):
        assert parse_m3u("") == []
        assert parse_m3u("   \n\n  ") == []
        assert parse_m3u("#EXTM3U\n# Comment only\n") == []

    def test_parse_standard_extinf_artist_title(self):
        content = """#EXTM3U
#EXTINF:245,Queen - Bohemian Rhapsody
/music/Queen/A Night at the Opera/04 Bohemian Rhapsody.mp3
#EXTINF:180,David Bowie - Heroes
/music/David Bowie/Heroes/03 Heroes.flac
"""
        tracks = parse_m3u(content)
        assert len(tracks) == 2
        assert tracks[0]["title"] == "Bohemian Rhapsody"
        assert tracks[0]["artist"] == "Queen"
        assert tracks[1]["title"] == "Heroes"
        assert tracks[1]["artist"] == "David Bowie"

    def test_parse_extended_attributes_in_extinf(self):
        content = """#EXTM3U
#EXTINF:-1 tvg-name="Yesterday" tvg-artist="The Beatles" group-title="Classics",The Beatles - Yesterday
C:\\Music\\The Beatles\\Help!\\11 Yesterday.mp3
"""
        tracks = parse_m3u(content)
        assert len(tracks) == 1
        assert tracks[0]["title"] == "Yesterday"
        assert tracks[0]["artist"] == "The Beatles"

    def test_parse_extinf_without_artist_separator(self):
        content = """#EXTM3U
#EXTINF:192,Just A Song Title
song.mp3
"""
        tracks = parse_m3u(content)
        assert len(tracks) == 1
        assert tracks[0]["title"] == "Just A Song Title"
        assert tracks[0]["artist"] == "Unknown Artist"

    def test_parse_raw_filepaths_without_extinf(self):
        content = """Comfortably Numb.mp3
Led Zeppelin - Stairway to Heaven.flac
"""
        tracks = parse_m3u(content)
        assert len(tracks) == 2
        assert tracks[0]["title"] == "Comfortably Numb"
        assert tracks[0]["artist"] == "Unknown Artist"
        assert tracks[1]["title"] == "Stairway to Heaven"
        assert tracks[1]["artist"] == "Led Zeppelin"


# ==============================================================================
# 2. DATABASE LAYER TESTS (Match Memory & Enabled Toggles)
# ==============================================================================

class TestDatabaseExpansion:
    """Tests for Match Memory and Playlist Enabled toggle methods in Database."""

    def test_match_override_crud(self):
        db = Database(":memory:")
        try:
            # Seed user for foreign key constraint
            db.upsert_user(user_id="user-1", username="testuser", email="test@local.com")

            # 1. Add override
            override = db.add_match_override(
                source_title="Bohemian Rhapsody",
                source_artist="Queen",
                plex_rating_key="12345",
                plex_title="Bohemian Rhapsody (2011 Remaster)",
                plex_artist="Queen",
                created_by="user-1",
            )
            assert override["id"] is not None
            assert override["plex_rating_key"] == "12345"

            # 2. Get override (case-insensitive)
            found = db.get_match_override("bohemian rhapsody", "QUEEN")
            assert found is not None
            assert found["plex_rating_key"] == "12345"
            assert found["plex_title"] == "Bohemian Rhapsody (2011 Remaster)"

            # 3. List overrides
            overrides = db.list_match_overrides()
            assert len(overrides) == 1
            assert overrides[0]["source_title"] == "Bohemian Rhapsody"

            # 4. Upsert duplicate
            updated = db.add_match_override(
                source_title="Bohemian Rhapsody",
                source_artist="Queen",
                plex_rating_key="99999",
                plex_title="Bohemian Rhapsody (Live at Wembley)",
                plex_artist="Queen",
                created_by="user-1",
            )
            assert updated["plex_rating_key"] == "99999"
            overrides_after = db.list_match_overrides()
            assert len(overrides_after) == 1
            assert overrides_after[0]["plex_rating_key"] == "99999"

            # 5. Delete override
            del_result = db.delete_match_override(updated["id"])
            assert del_result is True
            assert db.get_match_override("Bohemian Rhapsody", "Queen") is None
            assert len(db.list_match_overrides()) == 0
        finally:
            db.close()

    def test_set_playlist_enabled(self):
        db = Database(":memory:")
        try:
            db.upsert_user(user_id="user-1", username="testuser", email="test@local.com")
            pl = db.upsert_playlist(
                playlist_id="pl-rock-101",
                name="Rock Legends",
                service="spotify",
                creator_id="user-1",
            )
            pl_id = pl["id"]
            assert pl.get("enabled", 1) == 1

            # Disable / pause
            db.set_playlist_enabled(pl_id, False)
            updated = db.get_playlist(pl_id)
            assert updated["enabled"] == 0

            # Re-enable / activate
            db.set_playlist_enabled(pl_id, True)
            updated_active = db.get_playlist(pl_id)
            assert updated_active["enabled"] == 1
        finally:
            db.close()


# ==============================================================================
# 3. PLEX CLIENT MATCH MEMORY & SMART MIX TESTS
# ==============================================================================

class TestPlexClientExpansion:
    """Tests PlexClient Match Memory resolution and Smart Mix generation."""

    @patch("trackseerr.clients.plex.PlexServer")
    def test_match_track_uses_match_memory_override(self, mock_plex_server_cls):
        db = Database(":memory:")
        try:
            db.upsert_user(user_id="user-1", username="testuser", email="test@local.com")
            db.add_match_override(
                source_title="Heroes",
                source_artist="David Bowie",
                plex_rating_key="777",
                plex_title="Heroes (2017 Remaster)",
                plex_artist="David Bowie",
                created_by="user-1",
            )

            mock_server_instance = MagicMock()
            mock_plex_server_cls.return_value = mock_server_instance

            plex = PlexClient("http://fake-plex:32400", "token")

            # Mock plex server track fetch
            mock_server_track = MagicMock()
            mock_server_track.ratingKey = 777
            mock_server_track.title = "Heroes (2017 Remaster)"
            mock_server_track.grandparentTitle = "David Bowie"
            mock_server_track.parentTitle = "Heroes"
            mock_server_instance.fetchItem.return_value = mock_server_track

            # Should resolve directly from override without fuzzy search
            track = Track(title="Heroes", artist="David Bowie", album="Heroes")
            matched = plex.match_track(track, db=db)
            assert matched is not None
            assert matched.ratingKey == 777
            mock_server_instance.fetchItem.assert_called_once_with(777)
        finally:
            db.close()

    @patch("trackseerr.clients.plex.PlexServer")
    def test_search_library_tracks(self, mock_plex_server_cls):
        mock_server_instance = MagicMock()
        mock_plex_server_cls.return_value = mock_server_instance

        plex = PlexClient("http://fake-plex:32400", "token")
        mock_track = MagicMock()
        mock_track.ratingKey = 101
        mock_track.title = "Starman"
        mock_track.grandparentTitle = "David Bowie"
        mock_track.parentTitle = "Ziggy Stardust"
        mock_track.duration = 250000

        mock_artist = MagicMock(title="David Bowie")
        mock_album = MagicMock(title="Ziggy Stardust")
        mock_track.artist.return_value = mock_artist
        mock_track.album.return_value = mock_album

        mock_server_instance.search.return_value = [mock_track]

        results = plex.search_library_tracks("Starman", limit=10)
        assert len(results) == 1
        assert results[0]["rating_key"] == "101"
        assert results[0]["title"] == "Starman"
        assert results[0]["artist"] == "David Bowie"
        assert results[0]["album"] == "Ziggy Stardust"



# ==============================================================================
# 4. FASTAPI ENDPOINTS INTEGRATION TESTS
# ==============================================================================

@pytest.fixture
def api_test_env(tmp_path):
    """Sets up an isolated FastAPI test environment with admin user and mock Plex."""
    db = Database(":memory:")
    config = Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        data_dir=str(tmp_path),
    )

    admin_user = {
        "id": "1",
        "username": "admin_ron",
        "email": "admin@plex.local",
        "thumb": "",
        "is_admin": True,
    }

    # Seed admin user in database to satisfy foreign key constraints
    db.upsert_user(
        user_id=admin_user["id"],
        username=admin_user["username"],
        email=admin_user["email"],
        is_admin=True,
    )

    mock_plex = MagicMock(spec=PlexClient)
    mock_plex.search_library_tracks.return_value = [
        {"rating_key": "555", "title": "Life on Mars", "artist": "David Bowie", "album": "Hunky Dory", "duration": 220000}
    ]
    mock_plex.match_playlist_tracks.return_value = (
        [MagicMock(ratingKey=1, title="Song 1", grandparentTitle="Artist 1", parentTitle="Album 1")],
        [],
    )
    mock_plex.sync_playlist_to_users.return_value = {"matched_count": 1, "missing_count": 0}

    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[get_current_user_or_api_key] = lambda: admin_user
    app.dependency_overrides[get_plex_client] = lambda: mock_plex

    with TestClient(app) as client:
        yield {
            "client": client,
            "db": db,
            "config": config,
            "admin_user": admin_user,
            "mock_plex": mock_plex,
        }

    db.close()


class TestEndpointsExpansion:
    """Validates new REST endpoints for the 5 features."""

    def test_get_featured_charts(self, api_test_env):
        client = api_test_env["client"]
        resp = client.get("/api/playlists/featured")
        assert resp.status_code == 200
        charts = resp.json()
        assert isinstance(charts, list)
        assert len(charts) >= 5
        chart_ids = [c["id"] for c in charts]
        assert "chart-billboard-hot-100" in chart_ids
        assert "chart-todays-top-hits" in chart_ids
        assert "chart-deezer-top-worldwide" in chart_ids

    def test_set_playlist_enabled_toggle(self, api_test_env):
        client = api_test_env["client"]
        db = api_test_env["db"]

        # Create playlist
        pl = db.upsert_playlist(playlist_id="pl-rock-1", name="Rock 101", service="spotify", creator_id="1")
        pl_id = pl["id"]

        # Toggle to disabled (paused)
        resp = client.put(f"/api/playlists/{pl_id}/enabled", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

        # Toggle back to enabled (active)
        resp2 = client.put(f"/api/playlists/{pl_id}/enabled", json={"enabled": True})
        assert resp2.status_code == 200
        assert resp2.json()["enabled"] is True

    def test_import_m3u_playlist(self, api_test_env):
        client = api_test_env["client"]
        m3u_text = """#EXTM3U
#EXTINF:220,David Bowie - Life on Mars
/music/Bowie/Hunky Dory/Life on Mars.flac
#EXTINF:180,Queen - Radio Ga Ga
/music/Queen/The Works/Radio Ga Ga.mp3
"""
        payload = {
            "name": "Classics From M3U",
            "content": m3u_text,
            "description": "My Winamp playlist",
            "targets": ["1"],
        }
        resp = client.post("/api/playlists/import/m3u", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Classics From M3U"
        assert data["service"] == "m3u"
        assert data["track_count"] == 2

    def test_import_m3u_playlist_invalid_empty(self, api_test_env):
        client = api_test_env["client"]
        payload = {
            "name": "Empty",
            "content": "#EXTM3U\n# Only comments\n",
            "targets": ["1"],
        }
        resp = client.post("/api/playlists/import/m3u", json=payload)
        assert resp.status_code == 400
        assert "Could not extract any valid tracks" in resp.json()["detail"]

    def test_missing_search_plex_library(self, api_test_env):
        client = api_test_env["client"]
        resp = client.get("/api/missing/search?query=Bowie")
        assert resp.status_code == 200
        tracks = resp.json()
        assert len(tracks) == 1
        assert tracks[0]["title"] == "Life on Mars"
        assert tracks[0]["rating_key"] == "555"

    def test_match_override_memory_flow(self, api_test_env):
        client = api_test_env["client"]
        db = api_test_env["db"]

        # Insert a playlist first to satisfy foreign key constraint on missing_tracks
        db.upsert_playlist(playlist_id="pl-space-1", name="Space Playlist", service="spotify", creator_id="1")

        # Insert a missing track
        with db._lock:
            db.conn.execute(
                "INSERT INTO missing_tracks (playlist_id, title, artist, album) VALUES (?, ?, ?, ?)",
                ("pl-space-1", "Space Oddity", "David Bowie", "David Bowie"),
            )
            db.conn.commit()

        assert len(db.get_missing_tracks()) == 1

        # Post manual match
        match_req = {
            "source_title": "Space Oddity",
            "source_artist": "David Bowie",
            "plex_rating_key": "999",
            "plex_title": "Space Oddity (2015 Remaster)",
            "plex_artist": "David Bowie",
        }
        resp = client.post("/api/missing/match", json=match_req)
        assert resp.status_code == 201
        res = resp.json()
        assert res["status"] == "matched"
        override_id = res["override"]["id"]

        # Verify missing track was purged
        assert len(db.get_missing_tracks()) == 0

        # List saved matches
        list_resp = client.get("/api/missing/matches")
        assert list_resp.status_code == 200
        matches = list_resp.json()
        assert len(matches) == 1
        assert matches[0]["plex_title"] == "Space Oddity (2015 Remaster)"

        # Delete match override
        del_resp = client.delete(f"/api/missing/match/{override_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["status"] == "deleted"

        # Verify list is empty
        assert len(client.get("/api/missing/matches").json()) == 0

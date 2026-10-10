"""Tests for Missing Tracks Feeds (RSS, Lidarr, Text), Webhook, and Self-Healing Sync."""

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import (
    get_config,
    get_current_user,
    get_current_user_or_api_key,
    get_db,
)
from trackseerr.config import Config
from trackseerr.models import SyncResult, Track
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    """In-memory database for testing."""
    db = Database(":memory:")
    # Seed admin user and regular user
    db.upsert_user("admin_1", "admin_user", is_admin=True)
    db.upsert_user("user_2", "regular_user", is_admin=False)
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    """Test configuration."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-token",
        data_dir=str(tmp_path),
        lidarr_url="http://127.0.0.1:8686",
        lidarr_api_key="test-lidarr-key",
        feed_token="secret-feed-token",
    )


@pytest.fixture
def client(test_db, test_config):
    """TestClient with dependency overrides."""
    app = create_app(db=test_db, config=test_config)
    admin_user = {"id": "admin_1", "username": "admin_user", "is_admin": True}

    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[get_current_user_or_api_key] = lambda: admin_user

    with TestClient(app) as test_client:
        yield test_client


class TestMissingFeeds:
    """Tests for RSS, Lidarr, and plain text feeds."""

    def test_rss_feed_generation_and_xml_structure(self, client, test_db):
        # Create a playlist and missing tracks
        test_db.upsert_playlist("pl_1", "Rock Classics", service="spotify")
        test_db.record_sync_result(
            "pl_1",
            status="partial",
            missing_tracks=[
                {"title": "Bohemian Rhapsody", "artist": "Queen", "album": "A Night at the Opera"},
                {"title": "Heroes", "artist": "David Bowie", "album": "Heroes"},
            ],
        )

        resp = client.get("/api/missing/rss?token=secret-feed-token")
        assert resp.status_code == 200
        assert "application/rss+xml" in resp.headers.get("content-type", "")

        body = resp.text
        assert "<rss version=\"2.0\"" in body
        assert "<title>TrackSeerr - Missing Music</title>" in body
        assert "<title>Queen - Bohemian Rhapsody</title>" in body
        assert "<title>David Bowie - Heroes</title>" in body
        assert "A Night at the Opera" in body
        assert "<guid isPermaLink=\"false\">trackseerr-missing-" in body

    def test_rss_feed_token_protection(self, client):
        # Invalid token returns 401 when feed_token is set
        resp = client.get("/api/missing/rss?token=wrong-token")
        assert resp.status_code == 401

        # Valid token via header
        resp = client.get("/api/missing/rss", headers={"X-Api-Key": "secret-feed-token"})
        assert resp.status_code == 200

    def test_text_feed_generation(self, client, test_db):
        test_db.upsert_playlist("pl_1", "List 1", service="spotify")
        test_db.record_sync_result(
            "pl_1",
            status="partial",
            missing_tracks=[
                {"title": "Bohemian Rhapsody", "artist": "Queen", "album": "A Night at the Opera"},
            ],
        )

        resp = client.get("/api/missing/text?token=secret-feed-token")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers.get("content-type", "")
        assert "Queen - Bohemian Rhapsody" in resp.text




class TestWebhookAndSelfHealingSync:
    """Tests for sync webhook trigger and self-healing imported playlist re-sync."""

    def test_sync_webhook_trigger(self, client):
        resp = client.post(
            "/api/sync/webhook?token=secret-feed-token",
            json={"eventType": "Download", "artist": "Queen"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("triggered", "already_running")

    def test_sync_webhook_unauthorized(self, client):
        resp = client.post("/api/sync/webhook?token=wrong-token")
        assert resp.status_code == 401

    def test_self_healing_imported_playlist_resync(self, test_db, test_config):
        """Validates that imported playlists with stored tracks_json are re-evaluated and self-heal when songs are added to Plex."""
        from trackseerr.api.routes.sync import sync_state

        # 1. Create imported playlist with tracks_json
        tracks = [
            {"title": "Song A", "artist": "Artist A", "album": "Album A"},
            {"title": "Song B", "artist": "Artist B", "album": "Album B"},
        ]
        test_db.upsert_playlist(
            playlist_id="imp_12345",
            name="Imported Test Mix",
            service="spotify",
            tracks_json=json.dumps(tracks),
            creator_id="admin_1",
        )
        test_db.set_playlist_targets("imp_12345", ["admin_1"])

        # 2. First sync: Plex has Song A, but Song B is missing
        mock_plex = MagicMock()
        mock_plex.sync_playlist_to_users.return_value = [
            SyncResult(playlist_name="Imported Test Mix", total_tracks=2, matched_tracks=1, missing_tracks=1, success=True)
        ]
        mock_plex.match_playlist_tracks.return_value = (
            [MagicMock(title="Song A")],
            [Track(title="Song B", artist="Artist B", album="Album B")],
        )

        res1 = sync_state.execute_sync(
            db=test_db,
            config=test_config,
            plex_client=mock_plex,
            spotify_client=None,
            deezer_client=None,
        )
        assert res1["status"] == "success"
        assert res1["stats"]["success_count"] == 1
        missing1 = test_db.get_missing_tracks("imp_12345")
        assert len(missing1) == 1
        assert missing1[0]["title"] == "Song B"

        # 3. Second sync: Song B was downloaded and now matches in Plex!
        mock_plex.match_playlist_tracks.return_value = (
            [MagicMock(title="Song A"), MagicMock(title="Song B")],
            [],  # No more missing tracks!
        )

        res2 = sync_state.execute_sync(
            db=test_db,
            config=test_config,
            plex_client=mock_plex,
            spotify_client=None,
            deezer_client=None,
        )
        assert res2["status"] == "success"
        assert res2["stats"]["success_count"] == 1
        # missing_tracks table is now completely empty!
        missing2 = test_db.get_missing_tracks("imp_12345")
        assert len(missing2) == 0
        pl = test_db.get_playlist("imp_12345")
        assert pl["sync_status"] == "success"





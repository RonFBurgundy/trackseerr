"""Tests for Missing Tracks Feeds (RSS, Lidarr, Text), Webhook, and Self-Healing Sync."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import (
    get_config,
    get_current_user,
    get_current_user_or_api_key,
    get_db,
    get_lidarr_client,
    get_plex_client,
)
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import Playlist, SyncResult, Track
from plex_playlist_sync.storage import Database


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


class TestLidarrClientAndPush:
    """Tests for Lidarr API client and push integration."""

    @patch("plex_playlist_sync.clients.lidarr.httpx.Client")
    def test_lidarr_status_endpoint(self, mock_client_cls, client, test_db):
        test_db.update_media_management_settings({"library_mode": "lidarr"})
        mock_http = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_http
        mock_http.get.return_value.status_code = 200
        mock_http.get.return_value.json.return_value = {"version": "2.4.3.4248", "appName": "Lidarr"}

        resp = client.get("/api/missing/lidarr/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["configured"] is True
        assert data["status"]["online"] is True
        assert data["status"]["version"] == "2.4.3.4248"

    @patch("plex_playlist_sync.clients.lidarr.httpx.Client")
    def test_lidarr_push_endpoint(self, mock_client_cls, client, test_db):
        test_db.update_media_management_settings({"library_mode": "lidarr"})
        mock_http = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_http

        # Lookup returns artist not in library (id=0)
        mock_http.get.side_effect = [
            # 1. lookup artist
            MagicMock(status_code=200, json=lambda: [{"id": 0, "artistName": "Queen", "foreignArtistId": "mb-queen"}]),
            # 2. get root folder
            MagicMock(status_code=200, json=lambda: [{"path": "/music"}]),
            # 3. get quality profile
            MagicMock(status_code=200, json=lambda: [{"id": 1}]),
            # 4. get metadata profile
            MagicMock(status_code=200, json=lambda: [{"id": 1}]),
        ]
        # POST to add artist
        mock_http.post.return_value.status_code = 201
        mock_http.post.return_value.json.return_value = {"id": 42, "artistName": "Queen"}

        test_db.upsert_playlist("pl_1", "Rock", service="spotify")
        test_db.record_sync_result(
            "pl_1",
            status="partial",
            missing_tracks=[
                {"title": "Bohemian Rhapsody", "artist": "Queen", "album": "A Night at the Opera"},
            ],
        )

        resp = client.post("/api/missing/lidarr/push", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requested"] == 1
        assert data["added"] == 1
        assert data["results"][0]["status"] == "added"
        assert data["results"][0]["artist"] == "Queen"


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
        from plex_playlist_sync.api.routes.sync import sync_state

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


class TestLidarrTrickleWorkerAndEndpoints:
    """Tests for Lidarr trickle background worker, pacing, and queue endpoints."""

    def test_queue_endpoints(self, client):
        # 1. GET queue status
        resp = client.get("/api/missing/lidarr/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert "is_running" in data
        assert "is_paused" in data
        assert "remaining_items" in data

        # 2. Pause when worker is running
        from plex_playlist_sync.lidarr_queue import lidarr_worker
        with lidarr_worker._lock:
            lidarr_worker._is_running = True
            lidarr_worker._total_items = 5
            lidarr_worker._processed_items = 1

        try:
            resp_pause = client.post("/api/missing/lidarr/queue/pause")
            assert resp_pause.status_code == 200
            assert resp_pause.json()["is_paused"] is True

            # 3. Resume
            resp_resume = client.post("/api/missing/lidarr/queue/resume")
            assert resp_resume.status_code == 200
            assert resp_resume.json()["is_paused"] is False

            # 4. Cancel
            resp_cancel = client.post("/api/missing/lidarr/queue/cancel")
            assert resp_cancel.status_code == 200
        finally:
            with lidarr_worker._lock:
                lidarr_worker._is_running = False
                lidarr_worker._total_items = 0
                lidarr_worker._processed_items = 0
                lidarr_worker._is_paused = False

    @patch("plex_playlist_sync.lidarr_queue.lidarr_worker.start_trickle")
    def test_lidarr_push_trickle_enqueues(self, mock_start, client, test_db):
        test_db.update_media_management_settings({"library_mode": "lidarr"})
        mock_start.return_value = {
            "status": "started",
            "message": "Enqueued 2 tracks",
            "queued_count": 2,
        }
        test_db.upsert_playlist("pl_trickle", "Synthwave", service="spotify")
        test_db.record_sync_result(
            "pl_trickle",
            status="partial",
            missing_tracks=[
                {"title": "Track 1", "artist": "Artist A", "album": "Album 1"},
                {"title": "Track 2", "artist": "Artist B", "album": "Album 2"},
            ],
        )

        resp = client.post("/api/missing/lidarr/push", json={"trickle": True, "batch_size": 25})
        assert resp.status_code == 200
        data = resp.json()
        assert data["trickle"] is True
        assert data["queued_count"] == 2
        assert mock_start.called



"""Comprehensive tests for Wanted Backlog Worker and RSS Sync Worker.

Covers:
1. TorznabDriver.fetch_recent with XML parsing and fallback logic
2. WantedBacklogWorker polling, deduplication against active downloads, and pacing
3. RSSSyncWorker release scanning, request matching, quality evaluation, and grab dispatch
4. POST /api/requests/{request_id}/retry endpoint authorization and lifecycle
5. GET /api/system/status worker telemetry schema
"""

from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
import httpx

from plex_playlist_sync.acquisition_coordinator import acquisition_coordinator
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db, get_lidarr_client, get_plex_client
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.backlog_worker import (
    WantedBacklogWorker,
    RSSSyncWorker,
    _matches_request,
)
from plex_playlist_sync.clients.acquisition.torznab import TorznabDriver
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import (
    AcquisitionSearchResult,
    ActiveDownload,
    DownloadStatus,
    MusicRequest,
    RequestStatus,
)
from plex_playlist_sync.storage import Database


SAMPLE_TORZNAB_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Prowlarr Recent Releases</title>
    <item>
      <title>Daft Punk - Random Access Memories (2013) [FLAC]</title>
      <guid>https://indexer.local/details/1001</guid>
      <link>https://indexer.local/dl/1001.torrent</link>
      <size>450000000</size>
      <enclosure url="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&amp;dn=Daft+Punk" length="450000000" type="application/x-bittorrent" />
      <torznab:attr name="seeders" value="42" />
      <torznab:attr name="peers" value="10" />
      <torznab:attr name="category" value="3020" />
    </item>
    <item>
      <title>Coldplay - Parachutes (2000) [MP3 320kbps]</title>
      <guid>https://indexer.local/details/1002</guid>
      <link>https://indexer.local/dl/1002.torrent</link>
      <size>105000000</size>
      <enclosure url="https://indexer.local/dl/1002.torrent" length="105000000" type="application/x-bittorrent" />
      <torznab:attr name="seeders" value="15" />
      <torznab:attr name="bitrate" value="320" />
      <torznab:attr name="category" value="3010" />
    </item>
  </channel>
</rss>
"""

EMPTY_TORZNAB_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Empty Channel</title>
  </channel>
</rss>
"""


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    """Provides a test Config pointing to tmp_path."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        data_dir=str(tmp_path),
        user_request_quota=10,
        auto_approve_requests=False,
    )


@pytest.fixture
def seeded_users(test_db):
    """Seeds admin and standard user in test DB."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    bob = test_db.upsert_user("user-bob", "bob", "bob@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice, "bob": bob}


def _auth_headers(user: dict, test_db: Database, config: Config) -> dict[str, str]:
    """Generates Bearer authorization header for test requests."""
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app_and_client(test_db, test_config):
    """Creates a FastAPI test client with injected DB and Config."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    client = TestClient(app)
    return app, client


# =============================================================================
# 1. TorznabDriver.fetch_recent Tests
# =============================================================================


class TestTorznabFetchRecent:
    def test_fetch_recent_success_music_endpoint(self):
        """Validates fetch_recent parses items from t=music endpoint."""
        driver = TorznabDriver("http://prowlarr.local:9696/1", api_key="test-api-key")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = SAMPLE_TORZNAB_FEED_XML

        with patch("httpx.Client.get", return_value=mock_resp) as mock_get:
            results = driver.fetch_recent(limit=50)

            assert len(results) == 2
            mock_get.assert_called_once()
            called_url = mock_get.call_args[0][0]
            assert "t=music" in called_url
            assert "limit=50" in called_url
            assert "apikey=test-api-key" in called_url

            r1 = results[0]
            assert r1.title == "Daft Punk - Random Access Memories (2013) [FLAC]"
            assert r1.artist == "Daft Punk"
            assert r1.album == "Random Access Memories"
            assert r1.size_bytes == 450000000
            assert r1.seeders == 42
            assert r1.magnet_url is not None
            assert r1.protocol == "torrent"

            r2 = results[1]
            assert r2.artist == "Coldplay"
            assert r2.album == "Parachutes"
            assert r2.seeders == 15
            assert r2.bit_rate == 320

    def test_fetch_recent_fallback_to_search_endpoint(self):
        """Validates fetch_recent falls back to t=search when t=music returns empty channel."""
        driver = TorznabDriver("http://prowlarr.local:9696/1", api_key="test-key")

        empty_resp = MagicMock()
        empty_resp.status_code = 200
        empty_resp.text = EMPTY_TORZNAB_FEED_XML

        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.text = SAMPLE_TORZNAB_FEED_XML

        with patch("httpx.Client.get", side_effect=[empty_resp, success_resp]) as mock_get:
            results = driver.fetch_recent(limit=100)

            assert len(results) == 2
            assert mock_get.call_count == 2
            first_url = mock_get.call_args_list[0][0][0]
            second_url = mock_get.call_args_list[1][0][0]
            assert "t=music" in first_url
            assert "t=search" in second_url

    def test_fetch_recent_ssrf_rejection(self):
        """Validates fetch_recent immediately blocks unsafe SSRF targets."""
        driver = TorznabDriver("http://169.254.169.254/latest/meta-data")
        results = driver.fetch_recent()
        assert results == []

    def test_fetch_recent_network_error_resilience(self):
        """Validates fetch_recent returns empty list without crashing on network timeout."""
        driver = TorznabDriver("http://prowlarr.local:9696/1")
        with patch("httpx.Client.get", side_effect=httpx.ConnectTimeout("Timed out")):
            results = driver.fetch_recent()
            assert results == []


# =============================================================================
# 2. WantedBacklogWorker Tests
# =============================================================================


class TestWantedBacklogWorker:
    def test_backlog_worker_start_stop_lifecycle(self, test_db):
        """Tests start, running state, duplicate start rejection, and clean stop."""
        worker = WantedBacklogWorker()
        assert worker.is_running() is False

        started = worker.start(db=test_db, interval_seconds=3600, pace_delay=0.1)
        assert started is True
        assert worker.is_running() is True

        # Second start should be rejected
        started_again = worker.start(db=test_db)
        assert started_again is False

        worker.stop(timeout=2.0)
        assert worker.is_running() is False

    def test_backlog_worker_poll_once_finds_unfulfilled_and_executes_search(self, test_db, seeded_users):
        """Validates poll_once finds unfulfilled requests and missing tracks, updating statuses."""
        worker = WantedBacklogWorker()
        worker.pace_delay = 0.01

        # 1. Seed unfulfilled request
        req = MusicRequest(
            id="req-sweep-1",
            user_id=seeded_users["alice"]["id"],
            item_type="album",
            title="Discovery",
            artist="Daft Punk",
            album="Discovery",
            status=RequestStatus.PENDING,
        )
        test_db.create_request(req)

        # 2. Seed unmonitored missing track
        test_db.upsert_playlist("pl-1", "My Playlist", "spotify")
        test_db.record_sync_result(
            playlist_id="pl-1",
            status="success",
            missing_tracks=[
                {
                    "title": "One More Time",
                    "artist": "Daft Punk",
                    "album": "Discovery",
                    "url": "http://spotify/track/1",
                }
            ],
        )

        mock_grab = MagicMock(return_value={"success": True, "download_id": "dl-auto-1"})

        with patch.object(acquisition_coordinator, "search_and_grab", mock_grab):
            stats = worker.poll_once(test_db)

            assert stats["items_checked"] == 2
            assert stats["items_grabbed"] == 2
            assert stats["errors"] == 0
            assert mock_grab.call_count == 2

            # Verify request status transitioned to processing
            updated_req = test_db.get_request("req-sweep-1")
            assert updated_req is not None
            assert updated_req["status"] == "processing"

            # Verify missing track status transitioned to grabbed
            missing_tracks = test_db.get_missing_tracks()
            assert len(missing_tracks) == 1
            assert missing_tracks[0]["lidarr_status"] == "grabbed"

            # Verify telemetry status
            status_dict = worker.get_status()
            assert status_dict["items_checked"] == 2
            assert status_dict["items_grabbed"] == 2
            assert status_dict["last_run_at"] is not None

    def test_backlog_worker_poll_once_skips_active_downloads(self, test_db, seeded_users):
        """Validates poll_once skips requests and tracks that already have active transfers."""
        worker = WantedBacklogWorker()
        worker.pace_delay = 0.01

        # 1. Seed request with active download
        req = MusicRequest(
            id="req-active-1",
            user_id=seeded_users["alice"]["id"],
            item_type="album",
            title="Currents",
            artist="Tame Impala",
            album="Currents",
            status=RequestStatus.PROCESSING,
        )
        test_db.create_request(req)

        test_db.create_download_client(
            {
                "id": "c1",
                "name": "qBit",
                "driver_type": "qbittorrent",
                "host_url": "http://127.0.0.1:8080",
                "enabled": True,
            }
        )

        active_dl = ActiveDownload(
            id="dl-100",
            request_id="req-active-1",
            client_id="c1",
            download_hash="hash-100",
            title="Tame Impala - Currents",
            artist="Tame Impala",
            item_type="album",
            status=DownloadStatus.DOWNLOADING.value,
        )
        test_db.create_active_download(active_dl)

        # 2. Seed missing track that matches an active download
        test_db.upsert_playlist("pl-2", "Indie Playlist", "spotify")
        test_db.record_sync_result(
            playlist_id="pl-2",
            status="success",
            missing_tracks=[
                {
                    "title": "Tame Impala - Currents",
                    "artist": "Tame Impala",
                    "album": "Currents",
                    "url": "http://spotify/track/2",
                }
            ],
        )

        mock_grab = MagicMock()
        with patch.object(acquisition_coordinator, "search_and_grab", mock_grab):
            stats = worker.poll_once(test_db)
            assert stats["items_checked"] == 0
            assert stats["items_grabbed"] == 0
            mock_grab.assert_not_called()


# =============================================================================
# 3. RSSSyncWorker Tests
# =============================================================================


class TestRSSSyncWorker:
    def test_rss_worker_start_stop_lifecycle(self, test_db):
        """Tests start, running state, and clean stop of RSSSyncWorker."""
        worker = RSSSyncWorker()
        assert worker.is_running() is False

        started = worker.start(db=test_db, interval_seconds=900)
        assert started is True
        assert worker.is_running() is True

        worker.stop(timeout=2.0)
        assert worker.is_running() is False

    def test_rss_worker_poll_once_matches_wanted_and_dispatches_grab(self, test_db, seeded_users):
        """Validates poll_once scans recent releases, matches wanted request, and dispatches grab."""
        worker = RSSSyncWorker()

        # 1. Seed enabled indexer and qBittorrent client
        test_db.create_indexer(
            {
                "id": "idx-rss-1",
                "name": "Local Indexer",
                "indexer_type": "torznab",
                "host_url": "http://127.0.0.1:9696",
                "enabled": True,
            }
        )
        test_db.create_download_client(
            {
                "id": "c-qbit-1",
                "name": "Main qBittorrent",
                "driver_type": "qbittorrent",
                "host_url": "http://127.0.0.1:8080",
                "enabled": True,
                "priority": 1,
            }
        )

        # 2. Seed wanted request
        req = MusicRequest(
            id="req-rss-target",
            user_id=seeded_users["bob"]["id"],
            item_type="album",
            title="Random Access Memories",
            artist="Daft Punk",
            album="Random Access Memories",
            status=RequestStatus.PENDING,
        )
        test_db.create_request(req)

        # 3. Mock indexer fetch_recent returning 1 non-match and 1 match
        candidate_non_match = AcquisitionSearchResult(
            download_id="dl-other",
            title="The Cure - Disintegration (1989) [FLAC]",
            artist="The Cure",
            album="Disintegration",
            size_bytes=300000000,
            protocol="torrent",
        )
        candidate_match = AcquisitionSearchResult(
            download_id="dl-daft",
            title="Daft Punk - Random Access Memories (2013) [FLAC 16bit]",
            artist="Daft Punk",
            album="Random Access Memories",
            size_bytes=420000000,
            protocol="torrent",
            download_url="magnet:?xt=urn:btih:daftpunkhash123",
        )

        mock_idx_driver = MagicMock()
        mock_idx_driver.fetch_recent.return_value = [candidate_non_match, candidate_match]

        mock_client_driver = MagicMock()
        mock_client_driver.download.return_value = "daftpunkhash123"

        with patch("plex_playlist_sync.backlog_worker.get_indexer_driver", return_value=mock_idx_driver), \
             patch("plex_playlist_sync.backlog_worker.get_acquisition_driver", return_value=mock_client_driver):

            stats = worker.poll_once(test_db)

            assert stats["releases_scanned"] == 2
            assert stats["grabs_triggered"] == 1
            assert stats["errors"] == 0

            # Verify active download was recorded in database
            downloads = test_db.list_active_downloads()
            assert len(downloads) == 1
            dl = downloads[0]
            assert dl["request_id"] == "req-rss-target"
            assert dl["client_id"] == "c-qbit-1"
            assert dl["download_hash"] == "daftpunkhash123"
            assert dl["status"] == "queued"

            # Verify request status transitioned to processing
            updated_req = test_db.get_request("req-rss-target")
            assert updated_req is not None
            assert updated_req["status"] == "processing"

    def test_rss_worker_quality_profile_rejection(self, test_db, seeded_users):
        """Validates that candidate releases rejected by the quality profile are not grabbed."""
        worker = RSSSyncWorker()

        test_db.create_indexer(
            {
                "id": "idx-q-1",
                "name": "Indexer",
                "indexer_type": "torznab",
                "host_url": "http://127.0.0.1:9696",
                "enabled": True,
            }
        )
        test_db.create_download_client(
            {
                "id": "c-qbit-2",
                "name": "qBit",
                "driver_type": "qbittorrent",
                "host_url": "http://127.0.0.1:8080",
                "enabled": True,
            }
        )

        req = MusicRequest(
            id="req-q-reject",
            user_id=seeded_users["alice"]["id"],
            item_type="album",
            title="Ok Computer",
            artist="Radiohead",
            album="Ok Computer",
            status=RequestStatus.PENDING,
        )
        test_db.create_request(req)

        # Mock candidate with unacceptable format
        candidate_bad_quality = AcquisitionSearchResult(
            download_id="dl-bad",
            title="Radiohead - OK Computer (1997) [MP3 192kbps]",
            artist="Radiohead",
            album="OK Computer",
            size_bytes=80000000,
            protocol="torrent",
        )

        mock_idx_driver = MagicMock()
        mock_idx_driver.fetch_recent.return_value = [candidate_bad_quality]

        # Mock evaluate_release returning unacceptable
        mock_eval = MagicMock()
        mock_eval.is_acceptable = False
        mock_eval.rejection_reasons = ["MP3 192 not allowed in profile"]

        with patch("plex_playlist_sync.backlog_worker.get_indexer_driver", return_value=mock_idx_driver), \
             patch("plex_playlist_sync.backlog_worker.evaluate_release", return_value=mock_eval):

            stats = worker.poll_once(test_db)
            assert stats["releases_scanned"] == 1
            assert stats["grabs_triggered"] == 0

            # No download should be recorded
            assert len(test_db.list_active_downloads()) == 0


# =============================================================================
# 4. POST /api/requests/{id}/retry Endpoint Tests
# =============================================================================


class TestRetryRequestEndpoint:
    def test_retry_request_admin_success(self, app_and_client, seeded_users, test_db, test_config):
        """Admin can retry any request; executes search_and_grab and returns processing status."""
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        # Create request owned by Alice
        req = MusicRequest(
            id="req-retry-1",
            user_id=seeded_users["alice"]["id"],
            item_type="album",
            title="Kid A",
            artist="Radiohead",
            album="Kid A",
            status=RequestStatus.PENDING,
        )
        test_db.create_request(req)

        # Seed native client and indexer
        test_db.create_download_client(
            {
                "id": "c-retry",
                "name": "qBit",
                "driver_type": "qbittorrent",
                "host_url": "http://127.0.0.1:8080",
                "enabled": True,
            }
        )
        test_db.create_indexer(
            {
                "id": "idx-retry",
                "name": "Torznab",
                "indexer_type": "torznab",
                "host_url": "http://127.0.0.1:9696",
                "enabled": True,
            }
        )

        mock_grab = MagicMock(return_value={"success": True, "download_id": "dl-retried-99", "release": "Radiohead - Kid A [FLAC]"})

        with patch.object(acquisition_coordinator, "search_and_grab", mock_grab):
            resp = client.post("/api/requests/req-retry-1/retry", headers=admin_headers)

            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["status"] == "processing"
            assert data["download_id"] == "dl-retried-99"
            assert "Kid A" in data["message"]

            # Request in DB is updated to processing
            updated = test_db.get_request("req-retry-1")
            assert updated is not None
            assert updated["status"] == "processing"

    def test_retry_request_owner_forbidden(self, app_and_client, seeded_users, test_db, test_config):
        """Retry is admin-only: even the owner of a request cannot trigger it."""
        _, client = app_and_client
        alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)

        req = MusicRequest(
            id="req-alice-own",
            user_id=seeded_users["alice"]["id"],
            item_type="track",
            title="Idioteque",
            artist="Radiohead",
            status=RequestStatus.PENDING,
        )
        test_db.create_request(req)

        resp = client.post("/api/requests/req-alice-own/retry", headers=alice_headers)
        assert resp.status_code == 403
        assert test_db.get_request("req-alice-own")["status"] == "pending"

    def test_retry_request_non_owner_forbidden(self, app_and_client, seeded_users, test_db, test_config):
        """Non-admin user cannot retry a request owned by someone else."""
        _, client = app_and_client
        bob_headers = _auth_headers(seeded_users["bob"], test_db, test_config)

        req = MusicRequest(
            id="req-alice-secret",
            user_id=seeded_users["alice"]["id"],
            item_type="album",
            title="In Rainbows",
            artist="Radiohead",
            status=RequestStatus.PENDING,
        )
        test_db.create_request(req)

        resp = client.post("/api/requests/req-alice-secret/retry", headers=bob_headers)
        assert resp.status_code == 403
        assert "Administrator access required" in resp.json()["detail"]

    def test_retry_request_already_available_bad_request(self, app_and_client, seeded_users, test_db, test_config):
        """Retrying an already available/fulfilled request returns HTTP 400."""
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        req = MusicRequest(
            id="req-avail",
            user_id=seeded_users["alice"]["id"],
            item_type="album",
            title="Amnesiac",
            artist="Radiohead",
            status=RequestStatus.AVAILABLE,
        )
        test_db.create_request(req)

        resp = client.post("/api/requests/req-avail/retry", headers=admin_headers)
        assert resp.status_code == 400
        assert "already fulfilled and available" in resp.json()["detail"]

    def test_retry_request_fallback_to_lidarr(self, app_and_client, seeded_users, test_db, test_config):
        """When native search yields no grab, retrying dispatches to Lidarr queue."""
        app, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        mock_lidarr = MagicMock()
        app.dependency_overrides[get_lidarr_client] = lambda: mock_lidarr
        test_db.update_media_management_settings({"library_mode": "lidarr"})

        req = MusicRequest(
            id="req-lidarr-retry",
            user_id=seeded_users["admin"]["id"],
            item_type="album",
            title="Hail to the Thief",
            artist="Radiohead",
            status=RequestStatus.PENDING,
        )
        test_db.create_request(req)

        with patch(
            "plex_playlist_sync.lidarr_queue.lidarr_worker.start_trickle", return_value={"status": "started"}
        ) as mock_trickle:
            resp = client.post("/api/requests/req-lidarr-retry/retry", headers=admin_headers)

            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert data["status"] == "processing"
            assert "Lidarr" in data["message"]
            mock_trickle.assert_called_once()

    def test_retry_request_not_found(self, app_and_client, seeded_users, test_db, test_config):
        """Retrying a nonexistent request returns HTTP 404."""
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        resp = client.post("/api/requests/req-ghost-999/retry", headers=admin_headers)
        assert resp.status_code == 404


# =============================================================================
# 5. System Status Telemetry Tests
# =============================================================================


class TestSystemStatusWorkerTelemetry:
    def test_system_status_includes_backlog_and_rss_workers(self, app_and_client, seeded_users, test_db, test_config):
        """GET /api/system/status includes backlog_worker and rss_worker status dictionaries."""
        app, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        mock_plex = MagicMock()
        mock_plex.test_connection.return_value = (True, "Plex Online")
        app.dependency_overrides[get_plex_client] = lambda: mock_plex

        resp = client.get("/api/system/status", headers=admin_headers)
        assert resp.status_code == 200

        data = resp.json()
        assert "workers" in data
        workers = data["workers"]

        # Check backlog_worker
        assert "backlog_worker" in workers
        bw = workers["backlog_worker"]
        assert isinstance(bw, dict)
        assert "running" in bw
        assert "interval_seconds" in bw
        assert "items_checked" in bw
        assert "items_grabbed" in bw

        # Check rss_worker
        assert "rss_worker" in workers
        rw = workers["rss_worker"]
        assert isinstance(rw, dict)
        assert "running" in rw
        assert "interval_seconds" in rw
        assert "releases_scanned" in rw
        assert "grabs_triggered" in rw

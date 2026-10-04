"""Unit and integration tests for Automated Search & Grab Coordinator (Phase 3).

Covers:
- search_all_indexers across Torznab, Newznab, and slskd
- Resilient multi-indexer error isolation
- evaluate_and_rank scoring, filtering, and torrent seeder tiebreakers
- find_client_for_protocol priority resolution
- search_and_grab lifecycle for qBittorrent, SABnzbd, and slskd
- Active downloads recording in database
- Graceful Lidarr trickle fallback on requests API
- Remote path mapping translation and path traversal prevention
- POST /api/missing/{track_id}/grab endpoint
"""

import json
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.acquisition_coordinator import (
    AcquisitionCoordinator,
    acquisition_coordinator,
)
from plex_playlist_sync.acquisition_worker import translate_remote_path
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db, get_lidarr_client
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import (
    AcquisitionSearchResult,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    IndexerConfig,
    MusicRequest,
    QualityProfile,
    QualityProfileItem,
    RequestStatus,
)
from plex_playlist_sync.storage import Database


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    """Provides test Config pointing to tmp_path."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-token",
        data_dir=str(tmp_path),
        auto_approve_requests=True,
    )


@pytest.fixture
def seeded_users(test_db):
    """Seeds admin and standard user."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice}


@pytest.fixture
def app_and_client(test_db, test_config):
    """Creates FastAPI test client."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    client = TestClient(app)
    return app, client


def _auth_headers(user: dict, test_db: Database, config: Config) -> dict[str, str]:
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user.get("username", "user"),
        is_admin=bool(user.get("is_admin")),
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. search_all_indexers Tests
# ---------------------------------------------------------------------------

def test_search_all_indexers_aggregates_sources(test_db):
    """Verifies search_all_indexers queries Torznab, Newznab, and slskd and aggregates results."""
    # Seed Torznab and Newznab indexers
    test_db.create_indexer(
        IndexerConfig(
            id="idx-torznab",
            name="Prowlarr Torznab",
            indexer_type="torznab",
            host_url="http://prowlarr:9696",
            api_key="key1",
            enabled=True,
        )
    )
    test_db.create_indexer(
        IndexerConfig(
            id="idx-newznab",
            name="NZBGeek Newznab",
            indexer_type="newznab",
            host_url="http://nzbgeek.info",
            api_key="key2",
            enabled=True,
        )
    )
    # Seed slskd client
    test_db.create_download_client(
        DownloadClientConfig(
            id="cli-slskd",
            name="slskd Soulseek",
            driver_type="slskd",
            host_url="http://slskd:5030",
            api_key="slskdkey",
            enabled=True,
        )
    )

    torznab_res = [
        AcquisitionSearchResult(
            download_id="t1",
            title="Radiohead - Karma Police [FLAC]",
            artist="Radiohead",
            item_type="track",
            source="torznab",
            protocol="torrent",
        )
    ]
    newznab_res = [
        AcquisitionSearchResult(
            download_id="n1",
            title="Radiohead - Karma Police (1997) [MP3 320]",
            artist="Radiohead",
            item_type="track",
            source="newznab",
            protocol="usenet",
        )
    ]
    slskd_res = [
        AcquisitionSearchResult(
            download_id="s1",
            title="Radiohead - Karma Police.flac",
            artist="Radiohead",
            item_type="track",
            source="slskd",
            protocol="slskd",
        )
    ]

    mock_torznab_driver = MagicMock()
    mock_torznab_driver.search.return_value = torznab_res

    mock_newznab_driver = MagicMock()
    mock_newznab_driver.search.return_value = newznab_res

    mock_slskd_driver = MagicMock()
    mock_slskd_driver.search.return_value = slskd_res

    def mock_get_indexer_driver(cfg):
        return mock_torznab_driver if cfg.get("indexer_type") == "torznab" else mock_newznab_driver

    def mock_get_acquisition_driver(cfg):
        return mock_slskd_driver

    coordinator = AcquisitionCoordinator()

    with patch(
        "plex_playlist_sync.acquisition_coordinator.get_indexer_driver",
        side_effect=mock_get_indexer_driver,
    ), patch(
        "plex_playlist_sync.acquisition_coordinator.get_acquisition_driver",
        side_effect=mock_get_acquisition_driver,
    ):
        results = coordinator.search_all_indexers("Radiohead", "Karma Police", db=test_db)

    assert len(results) == 3
    titles = [r.title for r in results]
    assert "Radiohead - Karma Police [FLAC]" in titles
    assert "Radiohead - Karma Police (1997) [MP3 320]" in titles
    assert "Radiohead - Karma Police.flac" in titles


def test_search_all_indexers_error_isolation(test_db):
    """Ensures a failure on one indexer does not abort searches on other indexers."""
    test_db.create_indexer(
        IndexerConfig(
            id="idx-fail",
            name="Failing Indexer",
            indexer_type="torznab",
            host_url="http://offline-indexer:9696",
            enabled=True,
        )
    )
    test_db.create_indexer(
        IndexerConfig(
            id="idx-healthy",
            name="Healthy Indexer",
            indexer_type="torznab",
            host_url="http://healthy-indexer:9696",
            enabled=True,
        )
    )

    healthy_res = [
        AcquisitionSearchResult(
            download_id="h1",
            title="Daft Punk - One More Time [FLAC]",
            artist="Daft Punk",
            item_type="track",
            source="torznab",
            protocol="torrent",
        )
    ]

    mock_fail = MagicMock()
    mock_fail.search.side_effect = RuntimeError("Connection refused")

    mock_healthy = MagicMock()
    mock_healthy.search.return_value = healthy_res

    def mock_get_indexer(cfg):
        return mock_fail if cfg.get("id") == "idx-fail" else mock_healthy

    coordinator = AcquisitionCoordinator()
    with patch(
        "plex_playlist_sync.acquisition_coordinator.get_indexer_driver",
        side_effect=mock_get_indexer,
    ):
        results = coordinator.search_all_indexers("Daft Punk", "One More Time", db=test_db)

    assert len(results) == 1
    assert results[0].download_id == "h1"


# ---------------------------------------------------------------------------
# 2. evaluate_and_rank Tests
# ---------------------------------------------------------------------------

def test_evaluate_and_rank_quality_and_seeder_tiebreaker():
    """Validates ranking order, quality rejection filtering, and torrent seeder tiebreaking."""
    profile = QualityProfile(
        id="p1",
        name="Test Profile",
        cutoff="FLAC 16bit",
        items=[
            QualityProfileItem(quality="FLAC 24bit", allowed=True, weight=1000),
            QualityProfileItem(quality="FLAC 16bit", allowed=True, weight=900),
            QualityProfileItem(quality="MP3 320", allowed=True, weight=800),
            QualityProfileItem(quality="MP3 192", allowed=False, weight=500),
            QualityProfileItem(quality="Unknown", allowed=False, weight=100),
        ],
        preferred_tags=["cd", "web"],
        ignored_tags=["live", "bootleg"],
    )

    cands = [
        # Candidate 1: FLAC 16bit, 5 seeders
        AcquisitionSearchResult(
            download_id="c1",
            title="Pink Floyd - Time (1973) [FLAC]",
            artist="Pink Floyd",
            source="torznab",
            protocol="torrent",
            seeders=5,
        ),
        # Candidate 2: FLAC 16bit, 50 seeders (tiebreaker should place this above Candidate 1)
        AcquisitionSearchResult(
            download_id="c2",
            title="Pink Floyd - Time [FLAC]",
            artist="Pink Floyd",
            source="torznab",
            protocol="torrent",
            seeders=50,
        ),
        # Candidate 3: FLAC 24bit (should rank highest overall)
        AcquisitionSearchResult(
            download_id="c3",
            title="Pink Floyd - Time [24bit Hi-Res]",
            artist="Pink Floyd",
            source="torznab",
            protocol="torrent",
            seeders=2,
        ),
        # Candidate 4: Live bootleg (rejected by ignored_tags)
        AcquisitionSearchResult(
            download_id="c4",
            title="Pink Floyd - Time Live Bootleg [FLAC]",
            artist="Pink Floyd",
            source="torznab",
            protocol="torrent",
            seeders=100,
        ),
        # Candidate 5: Disallowed MP3 192 (rejected by profile.items)
        AcquisitionSearchResult(
            download_id="c5",
            title="Pink Floyd - Time [192kbps]",
            artist="Pink Floyd",
            source="torznab",
            protocol="torrent",
            seeders=20,
        ),
    ]

    coordinator = AcquisitionCoordinator()
    ranked = coordinator.evaluate_and_rank(cands, profile)

    assert len(ranked) == 3
    # First place: FLAC 24bit (c3)
    assert ranked[0][0].download_id == "c3"
    # Second place: FLAC 16bit with 50 seeders (c2)
    assert ranked[1][0].download_id == "c2"
    # Third place: FLAC 16bit with 5 seeders (c1)
    assert ranked[2][0].download_id == "c1"


# ---------------------------------------------------------------------------
# 3. find_client_for_protocol Tests
# ---------------------------------------------------------------------------

def test_find_client_for_protocol(test_db):
    """Validates selecting enabled download client by protocol and priority."""
    # Seed qBittorrent priority 2, SABnzbd priority 1, slskd priority 1
    test_db.create_download_client(
        DownloadClientConfig(
            id="qbit-low",
            name="qBittorrent Backup",
            driver_type="qbittorrent",
            host_url="http://qbit-backup:8080",
            priority=2,
            enabled=True,
        )
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="qbit-primary",
            name="qBittorrent Primary",
            driver_type="qbittorrent",
            host_url="http://qbit:8080",
            priority=1,
            enabled=True,
        )
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="sab-primary",
            name="SABnzbd Usenet",
            driver_type="sabnzbd",
            host_url="http://sab:8080",
            priority=1,
            enabled=True,
        )
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="slskd-client",
            name="slskd Client",
            driver_type="slskd",
            host_url="http://slskd:5030",
            priority=1,
            enabled=True,
        )
    )

    coordinator = AcquisitionCoordinator()

    # Torrent protocol -> should pick highest priority qbittorrent (qbit-primary)
    torrent_client = coordinator.find_client_for_protocol("torrent", test_db)
    assert torrent_client is not None
    assert torrent_client["id"] == "qbit-primary"

    # Usenet protocol -> sabnzbd
    usenet_client = coordinator.find_client_for_protocol("usenet", test_db)
    assert usenet_client is not None
    assert usenet_client["id"] == "sab-primary"

    # slskd protocol -> slskd
    slskd_client = coordinator.find_client_for_protocol("slskd", test_db)
    assert slskd_client is not None
    assert slskd_client["id"] == "slskd-client"

    # Unsupported protocol
    none_client = coordinator.find_client_for_protocol("ftp", test_db)
    assert none_client is None


# ---------------------------------------------------------------------------
# 4. search_and_grab Lifecycle Tests
# ---------------------------------------------------------------------------

def test_search_and_grab_qbittorrent(test_db):
    """Tests end-to-end search_and_grab for a torrent candidate with active_download record."""
    # Seed indexer & qBittorrent client
    test_db.create_indexer(
        IndexerConfig(
            id="idx-1",
            name="Prowlarr",
            indexer_type="torznab",
            host_url="http://prowlarr:9696",
            enabled=True,
        )
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="client-qbit",
            name="qBittorrent",
            driver_type="qbittorrent",
            host_url="http://qbittorrent:8080",
            enabled=True,
        )
    )

    # Seed user and request to satisfy request_id foreign key constraint
    test_db.upsert_user("admin-1", "admin", "admin@test.com", is_admin=True)
    test_db.create_request(
        MusicRequest(
            id="req-123",
            user_id="admin-1",
            item_type="track",
            title="In Bloom",
            artist="Nirvana",
        )
    )

    candidate = AcquisitionSearchResult(
        download_id="dl-hash-12345",
        title="Nirvana - In Bloom [FLAC]",
        artist="Nirvana",
        item_type="track",
        size_bytes=35 * 1024 * 1024,
        magnet_url="magnet:?xt=urn:btih:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        source="torznab",
        protocol="torrent",
        seeders=42,
    )

    coordinator = AcquisitionCoordinator()

    mock_driver = MagicMock()
    mock_driver.download.return_value = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    with patch.object(coordinator, "search_all_indexers", return_value=[candidate]), patch(
        "plex_playlist_sync.acquisition_coordinator.get_acquisition_driver",
        return_value=mock_driver,
    ):
        result = coordinator.search_and_grab(
            artist="Nirvana",
            title="In Bloom",
            request_id="req-123",
            db=test_db,
        )

    assert result["success"] is True
    assert result["download_hash"] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert result["client"] == "qBittorrent"
    assert result["release"] == "Nirvana - In Bloom [FLAC]"

    # Verify active_download in database
    dl_id = result["download_id"]
    active_dl = test_db.get_active_download(dl_id)
    assert active_dl is not None
    assert active_dl["request_id"] == "req-123"
    assert active_dl["client_id"] == "client-qbit"
    assert active_dl["status"] == DownloadStatus.QUEUED.value
    assert active_dl["title"] == "Nirvana - In Bloom [FLAC]"
    assert active_dl["size_bytes"] == 35 * 1024 * 1024


def test_search_and_grab_sabnzbd(test_db):
    """Tests search_and_grab dispatching to SABnzbd for usenet releases."""
    test_db.create_indexer(
        IndexerConfig(
            id="idx-nzb",
            name="NZBGeek",
            indexer_type="newznab",
            host_url="http://nzbgeek:5076",
            enabled=True,
        )
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="client-sab",
            name="SABnzbd",
            driver_type="sabnzbd",
            host_url="http://sabnzbd:8080",
            enabled=True,
        )
    )

    candidate = AcquisitionSearchResult(
        download_id="nzb-100",
        title="Fleetwood Mac - Dreams [FLAC]",
        artist="Fleetwood Mac",
        download_url="http://nzbgeek:5076/getnzb/100.nzb",
        source="newznab",
        protocol="usenet",
        size_bytes=28 * 1024 * 1024,
    )

    coordinator = AcquisitionCoordinator()
    mock_sab = MagicMock()
    mock_sab.download.return_value = "SABnzbd_nzo_abc999"

    with patch.object(coordinator, "search_all_indexers", return_value=[candidate]), patch(
        "plex_playlist_sync.acquisition_coordinator.get_acquisition_driver",
        return_value=mock_sab,
    ):
        res = coordinator.search_and_grab(
            artist="Fleetwood Mac",
            title="Dreams",
            db=test_db,
        )

    assert res["success"] is True
    assert res["download_hash"] == "SABnzbd_nzo_abc999"
    assert res["client"] == "SABnzbd"


def test_search_and_grab_slskd(test_db):
    """Tests search_and_grab dispatching to slskd for Soulseek P2P results."""
    test_db.create_download_client(
        DownloadClientConfig(
            id="client-slskd",
            name="slskd",
            driver_type="slskd",
            host_url="http://slskd:5030",
            enabled=True,
        )
    )

    candidate = AcquisitionSearchResult(
        download_id="user1::Music/Song.flac",
        title="Aphex Twin - Xtal [FLAC]",
        artist="Aphex Twin",
        source="slskd",
        protocol="slskd",
        size_bytes=40 * 1024 * 1024,
    )

    coordinator = AcquisitionCoordinator()
    mock_slskd = MagicMock()
    mock_slskd.download.return_value = "user1::Music/Song.flac"

    with patch.object(coordinator, "search_all_indexers", return_value=[candidate]), patch(
        "plex_playlist_sync.acquisition_coordinator.get_acquisition_driver",
        return_value=mock_slskd,
    ):
        res = coordinator.search_and_grab(
            artist="Aphex Twin",
            title="Xtal",
            db=test_db,
        )

    assert res["success"] is True
    assert res["download_hash"] == "user1::Music/Song.flac"
    assert res["client"] == "slskd"


def test_search_and_grab_no_acceptable_releases(test_db):
    """Verifies search_and_grab returns failure when all releases are filtered out."""
    candidate_live = AcquisitionSearchResult(
        download_id="live-1",
        title="The Beatles - Hey Jude (Live Bootleg)",
        artist="The Beatles",
        source="torznab",
        protocol="torrent",
    )

    coordinator = AcquisitionCoordinator()
    with patch.object(coordinator, "search_all_indexers", return_value=[candidate_live]):
        res = coordinator.search_and_grab("The Beatles", "Hey Jude", db=test_db)

    assert res["success"] is False
    assert "No acceptable releases found" in res["message"]


def test_search_and_grab_no_client_available(test_db):
    """Verifies graceful failure when no client is enabled for the candidate's protocol."""
    # Usenet candidate, but no SABnzbd client configured
    candidate_nzb = AcquisitionSearchResult(
        download_id="nzb-1",
        title="Led Zeppelin - Kashmir [FLAC]",
        artist="Led Zeppelin",
        source="newznab",
        protocol="usenet",
    )

    coordinator = AcquisitionCoordinator()
    with patch.object(coordinator, "search_all_indexers", return_value=[candidate_nzb]):
        res = coordinator.search_and_grab("Led Zeppelin", "Kashmir", db=test_db)

    assert res["success"] is False
    assert "No enabled download client available for usenet" in res["message"]


# ---------------------------------------------------------------------------
# 5. Remote Path Mapping Tests
# ---------------------------------------------------------------------------

def test_translate_remote_path_scenarios():
    """Tests remote path translation and security traversal defense."""
    mappings = [
        {"remote_path": "/downloads/qbittorrent", "local_path": "/downloads"},
        {"remote_path": "/data/music", "local_path": "/mnt/music"},
    ]

    # Valid prefix match
    translated = translate_remote_path("/downloads/qbittorrent/Artist/Album", mappings)
    assert translated == "/downloads/Artist/Album"

    # Exact directory match without trailing slash
    exact = translate_remote_path("/downloads/qbittorrent", mappings)
    assert exact == "/downloads"

    # Path not matching any mapping
    unmatched = translate_remote_path("/var/lib/other/file.flac", mappings)
    assert unmatched == "/var/lib/other/file.flac"

    # None input
    assert translate_remote_path(None, mappings) is None

    # Path traversal attack defense
    traversal = translate_remote_path("/downloads/qbittorrent/../../etc/passwd", mappings)
    assert traversal is None


# ---------------------------------------------------------------------------
# 6. Requests API Native First with Lidarr Fallback
# ---------------------------------------------------------------------------

def test_requests_api_native_grab_precedence(app_and_client, test_db, test_config, seeded_users):
    """Tests requests API triggering native search_and_grab and skipping Lidarr when successful."""
    _, client = app_and_client
    admin = seeded_users["admin"]
    headers = _auth_headers(admin, test_db, test_config)

    # Seed native indexer and client
    test_db.create_indexer(
        IndexerConfig(
            id="idx-1",
            name="Prowlarr",
            indexer_type="torznab",
            host_url="http://prowlarr:9696",
            enabled=True,
        )
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="cli-qbit",
            name="qBittorrent",
            driver_type="qbittorrent",
            host_url="http://qbit:8080",
            enabled=True,
        )
    )

    with patch(
        "plex_playlist_sync.api.routes.requests.acquisition_coordinator.search_and_grab",
        return_value={"success": True, "download_id": "dl-12345", "download_hash": "hash123"},
    ) as mock_grab, patch(
        "plex_playlist_sync.lidarr_queue.lidarr_worker.start_trickle"
    ) as mock_lidarr:
        resp = client.post(
            "/api/requests",
            headers=headers,
            json={
                "item_type": "album",
                "title": "OK Computer",
                "artist": "Radiohead",
            },
        )

    assert resp.status_code == 201
    assert mock_grab.called
    assert not mock_lidarr.called


def test_requests_api_no_lidarr_fallback_when_native_unmatched(app_and_client, test_db, test_config, seeded_users):
    """In native mode an unmatched native search never falls back to Lidarr (the interlock is strict)."""
    app, client = app_and_client
    admin = seeded_users["admin"]
    headers = _auth_headers(admin, test_db, test_config)

    # Seed native indexer and client
    test_db.create_indexer(
        IndexerConfig(
            id="idx-1",
            name="Prowlarr",
            indexer_type="torznab",
            host_url="http://prowlarr:9696",
            enabled=True,
        )
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="cli-qbit",
            name="qBittorrent",
            driver_type="qbittorrent",
            host_url="http://qbit:8080",
            enabled=True,
        )
    )

    mock_lidarr_client = MagicMock()
    app.dependency_overrides[get_lidarr_client] = lambda: mock_lidarr_client

    try:
        with patch(
            "plex_playlist_sync.api.routes.requests.acquisition_coordinator.search_and_grab",
            return_value={"success": False, "message": "No acceptable releases found"},
        ) as mock_grab, patch(
            "plex_playlist_sync.lidarr_queue.lidarr_worker.start_trickle"
        ) as mock_lidarr:
            resp = client.post(
                "/api/requests",
                headers=headers,
                json={
                    "item_type": "album",
                    "title": "Rare B-Sides",
                    "artist": "Radiohead",
                },
            )

        assert resp.status_code == 201
        assert mock_grab.called
        assert not mock_lidarr.called
    finally:
        app.dependency_overrides.pop(get_lidarr_client, None)


# ---------------------------------------------------------------------------
# 7. POST /api/missing/{track_id}/grab Endpoint Tests
# ---------------------------------------------------------------------------

def test_missing_track_grab_endpoint(app_and_client, test_db, test_config, seeded_users):
    """Tests the admin-only POST /api/missing/{track_id}/grab endpoint."""
    _, client = app_and_client
    admin = seeded_users["admin"]
    alice = seeded_users["alice"]

    # Insert playlist first to satisfy foreign key constraint on missing_tracks
    test_db.upsert_playlist("pl-1", "My Playlist", service="spotify")

    # Insert missing track into DB
    with test_db._lock:
        cur = test_db.conn.execute(
            """
            INSERT INTO missing_tracks (playlist_id, title, artist, album, url, lidarr_status)
            VALUES ('pl-1', 'Creep', 'Radiohead', 'Pablo Honey', '', 'unmonitored')
            """
        )
        test_db.conn.commit()
        track_id = cur.lastrowid

    # 1. Non-admin access should be forbidden (403)
    user_headers = _auth_headers(alice, test_db, test_config)
    resp_user = client.post(f"/api/missing/{track_id}/grab", headers=user_headers)
    assert resp_user.status_code == 403

    # 2. Non-existent track ID should return 404
    admin_headers = _auth_headers(admin, test_db, test_config)
    resp_404 = client.post("/api/missing/999999/grab", headers=admin_headers)
    assert resp_404.status_code == 404

    # 3. Successful admin grab dispatch
    with patch(
        "plex_playlist_sync.api.routes.missing.acquisition_coordinator.search_and_grab",
        return_value={
            "success": True,
            "download_id": "dl-missing-1",
            "download_hash": "hash-missing-1",
            "release": "Radiohead - Creep [FLAC]",
            "client": "qBittorrent",
            "score": 950,
        },
    ) as mock_grab:
        resp_admin = client.post(f"/api/missing/{track_id}/grab", headers=admin_headers)

    assert resp_admin.status_code == 200
    data = resp_admin.json()
    assert data["success"] is True
    assert data["download_id"] == "dl-missing-1"
    assert data["client"] == "qBittorrent"
    mock_grab.assert_called_once_with(
        artist="Radiohead",
        title="Creep",
        album="Pablo Honey",
        item_type="track",
        db=test_db,
    )


# ---------------------------------------------------------------------------
# 8. Download Client Category & Remote Path Mappings Payload Tests
# ---------------------------------------------------------------------------

def test_download_client_category_and_mappings_payload(app_and_client, test_db, test_config, seeded_users):
    """Tests saving category and remote_path_mappings via download clients API."""
    _, client = app_and_client
    admin = seeded_users["admin"]
    headers = _auth_headers(admin, test_db, test_config)

    payload = {
        "name": "qBittorrent Mapped",
        "driver_type": "qbittorrent",
        "host_url": "http://qbittorrent:8080",
        "category": "music",
        "remote_path_mappings": [
            {"remote_path": "/downloads/torrents", "local_path": "/local/downloads"}
        ],
    }
    resp = client.post("/api/settings/download-clients", headers=headers, json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["category"] == "music"
    assert len(data["remote_path_mappings"]) == 1
    assert data["remote_path_mappings"][0]["remote_path"] == "/downloads/torrents"
    assert data["remote_path_mappings"][0]["local_path"] == "/local/downloads"

    # Verify stored in DB extra_settings_json
    client_row = test_db.get_download_client(data["id"])
    assert client_row is not None
    extra = json.loads(client_row["extra_settings_json"])
    assert extra["category"] == "music"
    assert extra["remote_path_mappings"] == [
        {"remote_path": "/downloads/torrents", "local_path": "/local/downloads"}
    ]


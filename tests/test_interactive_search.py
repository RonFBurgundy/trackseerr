"""Unit and integration tests for Interactive Manual Search & Release Browser (Phase 4).

Covers:
- Unauthenticated and non-admin RBAC (401 / 403)
- Multi-indexer search with quality evaluation and ranking
- Custom quality profile selection
- Release candidate sorting (acceptable first, score descending, seeder tiebreakers)
- Manual grab dispatch to download clients (qBittorrent, slskd / p2p)
- Active download tracking in SQLite database
- Automatic music request transition to 'processing'
- Missing download client error handling (HTTP 400)
- Explicit client_id resolution
- Client driver exception handling (HTTP 400 / 500)
"""

from typing import Any
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.models import (
    AcquisitionSearchResult,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    MusicRequest,
    QualityProfile,
    QualityProfileItem,
    RequestStatus,
)
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance with migrations applied."""
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
    """Seeds admin and standard user accounts in test DB."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice}


@pytest.fixture
def app_and_client(test_db, test_config):
    """Instantiates the FastAPI test client with dependency overrides."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    client = TestClient(app)
    return app, client


def _auth_headers(user: dict[str, Any], test_db: Database, config: Config) -> dict[str, str]:
    """Generates a valid signed Bearer token and active DB session."""
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. RBAC Permissions Tests (401 / 403)
# ---------------------------------------------------------------------------

def test_interactive_search_rbac_unauthenticated(app_and_client):
    """Unauthenticated requests must receive HTTP 401."""
    _, client = app_and_client

    search_resp = client.post("/api/acquisition/search", json={"artist": "Daft Punk"})
    assert search_resp.status_code == 401

    grab_payload = {
        "release": {
            "id": "rel-1",
            "title": "Daft Punk - Discovery [FLAC]",
            "indexer_name": "TestIndexer",
            "protocol": "torrent",
            "size_bytes": 350000000,
            "parsed_quality": "FLAC 16bit",
            "is_acceptable": True,
            "score": 900,
            "meets_cutoff": True,
        },
        "artist": "Daft Punk",
        "title": "Discovery",
    }
    grab_resp = client.post("/api/acquisition/grab", json=grab_payload)
    assert grab_resp.status_code == 401


def test_interactive_search_rbac_non_admin(app_and_client, test_db, test_config, seeded_users):
    """Non-admin users (Alice) must receive HTTP 403."""
    _, client = app_and_client
    alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)

    search_resp = client.post(
        "/api/acquisition/search",
        headers=alice_headers,
        json={"artist": "Daft Punk"},
    )
    assert search_resp.status_code == 403

    grab_payload = {
        "release": {
            "id": "rel-1",
            "title": "Daft Punk - Discovery [FLAC]",
            "indexer_name": "TestIndexer",
            "protocol": "torrent",
            "size_bytes": 350000000,
            "parsed_quality": "FLAC 16bit",
            "is_acceptable": True,
            "score": 900,
            "meets_cutoff": True,
        },
        "artist": "Daft Punk",
        "title": "Discovery",
    }
    grab_resp = client.post(
        "/api/acquisition/grab",
        headers=alice_headers,
        json=grab_payload,
    )
    assert grab_resp.status_code == 403


# ---------------------------------------------------------------------------
# 2. Interactive Search & Quality Evaluation Tests
# ---------------------------------------------------------------------------

def test_interactive_search_with_quality_ranking(app_and_client, test_db, test_config, seeded_users):
    """Tests POST /api/acquisition/search ranking and evaluation."""
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    mock_candidates = [
        AcquisitionSearchResult(
            download_id="idx-1",
            title="Daft Punk - Discovery (2001) [FLAC 24bit-96kHz WEB]",
            artist="Daft Punk",
            album="Discovery",
            item_type="album",
            size_bytes=800 * 1024 * 1024,
            seeders=30,
            source="torznab",
            download_url="http://indexer.local/dl/1.torrent",
            extra={"indexer_name": "Redacted"},
        ),
        AcquisitionSearchResult(
            download_id="idx-2",
            title="Daft Punk - Discovery (2001) [MP3 320kbps CD]",
            artist="Daft Punk",
            album="Discovery",
            item_type="album",
            size_bytes=140 * 1024 * 1024,
            seeders=100,
            source="torznab",
            download_url="http://indexer.local/dl/2.torrent",
            extra={"indexer_name": "Gazelle"},
        ),
        AcquisitionSearchResult(
            download_id="idx-3",
            title="Daft Punk - Discovery Live Bootleg [FLAC 16bit]",
            artist="Daft Punk",
            album="Discovery",
            item_type="album",
            size_bytes=400 * 1024 * 1024,
            seeders=15,
            source="torznab",
            download_url="http://indexer.local/dl/3.torrent",
            extra={"indexer_name": "Redacted"},
        ),
    ]

    with patch(
        "trackseerr.api.routes.acquisition.acquisition_coordinator.search_all_indexers",
        return_value=mock_candidates,
    ):
        resp = client.post(
            "/api/acquisition/search",
            headers=admin_headers,
            json={"artist": "Daft Punk", "album": "Discovery", "item_type": "album"},
        )

    assert resp.status_code == 200
    data = resp.json()

    assert data["query"]["artist"] == "Daft Punk"
    assert data["query"]["album"] == "Discovery"
    assert data["profile_name"] == "Lossless (FLAC)"
    assert data["count"] == 3

    results = data["results"]
    assert len(results) == 3

    # The top candidate must be the acceptable FLAC 24bit release
    top = results[0]
    assert top["is_acceptable"] is True
    assert top["parsed_quality"] == "FLAC 24bit"
    assert top["protocol"] == "torrent"
    assert top["seeders"] == 30
    assert top["meets_cutoff"] is True
    assert top["score"] > 0
    assert top["rejection_reasons"] == []
    assert top["indexer_name"] == "Redacted"

    # Second and third candidates must be rejected by default Lossless profile
    assert results[1]["is_acceptable"] is False
    assert results[2]["is_acceptable"] is False

    # Check rejection reasons
    mp3_cand = next(r for r in results if "MP3 320" in r["title"])
    assert any("MP3 320" in r for r in mp3_cand["rejection_reasons"])

    bootleg_cand = next(r for r in results if "Bootleg" in r["title"])
    assert any("live" in r.lower() or "bootleg" in r.lower() for r in bootleg_cand["rejection_reasons"])


def test_interactive_search_custom_profile(app_and_client, test_db, test_config, seeded_users):
    """Tests search evaluated against a custom QualityProfile accepting MP3 320."""
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    custom_profile = QualityProfile(
        id="profile-mp3-only",
        name="MP3 Standard",
        cutoff="MP3 320",
        items=[
            QualityProfileItem(quality="MP3 320", allowed=True, weight=1000),
            QualityProfileItem(quality="FLAC 16bit", allowed=False, weight=500),
        ],
        preferred_tags=["cd"],
        ignored_tags=[],
        is_default=False,
    )
    test_db.upsert_quality_profile(custom_profile)

    mock_candidates = [
        AcquisitionSearchResult(
            download_id="idx-1",
            title="Radiohead - OK Computer [FLAC 16bit]",
            artist="Radiohead",
            item_type="album",
            size_bytes=300 * 1024 * 1024,
            source="torznab",
        ),
        AcquisitionSearchResult(
            download_id="idx-2",
            title="Radiohead - OK Computer [MP3 320 CD]",
            artist="Radiohead",
            item_type="album",
            size_bytes=120 * 1024 * 1024,
            source="torznab",
        ),
    ]

    with patch(
        "trackseerr.api.routes.acquisition.acquisition_coordinator.search_all_indexers",
        return_value=mock_candidates,
    ):
        resp = client.post(
            "/api/acquisition/search",
            headers=admin_headers,
            json={
                "artist": "Radiohead",
                "album": "OK Computer",
                "quality_profile_id": "profile-mp3-only",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["profile_name"] == "MP3 Standard"
    results = data["results"]

    # In this profile, MP3 320 is acceptable and FLAC 16bit is rejected
    assert results[0]["title"] == "Radiohead - OK Computer [MP3 320 CD]"
    assert results[0]["is_acceptable"] is True
    assert results[1]["title"] == "Radiohead - OK Computer [FLAC 16bit]"
    assert results[1]["is_acceptable"] is False


def test_interactive_search_empty_and_error_resilience(app_and_client, test_db, test_config, seeded_users):
    """Tests search when no indexers find releases or when an exception occurs."""
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # Empty candidate list
    with patch(
        "trackseerr.api.routes.acquisition.acquisition_coordinator.search_all_indexers",
        return_value=[],
    ):
        resp = client.post(
            "/api/acquisition/search",
            headers=admin_headers,
            json={"artist": "Unknown Obscure Artist"},
        )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["results"] == []

    # Search raises exception
    with patch(
        "trackseerr.api.routes.acquisition.acquisition_coordinator.search_all_indexers",
        side_effect=RuntimeError("Indexers network down"),
    ):
        resp_err = client.post(
            "/api/acquisition/search",
            headers=admin_headers,
            json={"artist": "Some Artist"},
        )
    assert resp_err.status_code == 200
    assert resp_err.json()["count"] == 0


# ---------------------------------------------------------------------------
# 3. Manual Grab Dispatch & Active Download Tests
# ---------------------------------------------------------------------------

def test_manual_grab_torrent_success(app_and_client, test_db, test_config, seeded_users):
    """Tests manual grab for a torrent release dispatched to qBittorrent."""
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # Seed qBittorrent client
    qbit_cfg = DownloadClientConfig(
        id="qbit-client-1",
        name="qBittorrent Primary",
        driver_type="qbittorrent",
        host_url="http://127.0.0.1:8080",
        enabled=True,
        priority=1,
    )
    test_db.create_download_client(qbit_cfg)

    # Seed a pending request in database
    req = MusicRequest(
        id="req-manual-1",
        user_id="user-alice",
        item_type="album",
        title="Random Access Memories",
        artist="Daft Punk",
        status=RequestStatus.PENDING,
    )
    test_db.create_request(req)

    grab_payload = {
        "release": {
            "id": "rel-qbit-101",
            "title": "Daft Punk - Random Access Memories (2013) [FLAC 24bit]",
            "indexer_name": "Redacted",
            "protocol": "torrent",
            "size_bytes": 1200000000,
            "seeders": 45,
            "download_url": "http://indexer.local/dl/ram.torrent",
            "magnet_url": "magnet:?xt=urn:btih:daftpunkram1234567890abcdef",
            "parsed_quality": "FLAC 24bit",
            "is_acceptable": True,
            "score": 1050,
            "meets_cutoff": True,
            "rejection_reasons": [],
            "extra": {"indexer_name": "Redacted"},
        },
        "artist": "Daft Punk",
        "title": "Random Access Memories",
        "album": "Random Access Memories",
        "item_type": "album",
        "request_id": "req-manual-1",
    }

    mock_driver = MagicMock()
    mock_driver.download.return_value = "daftpunkram1234567890abcdef"

    with patch(
        "trackseerr.api.routes.acquisition.get_acquisition_driver",
        return_value=mock_driver,
    ):
        resp = client.post(
            "/api/acquisition/grab",
            headers=admin_headers,
            json=grab_payload,
        )

    assert resp.status_code == 200
    res = resp.json()
    assert res["success"] is True
    assert res["client"] == "qBittorrent Primary"
    assert res["download_id"].startswith("dl-")
    assert "Daft Punk - Random Access Memories" in res["message"]

    # Verify download call arguments
    mock_driver.download.assert_called_once()
    download_arg = mock_driver.download.call_args[0][0]
    assert download_arg.title == "Daft Punk - Random Access Memories (2013) [FLAC 24bit]"
    assert download_arg.protocol == "torrent"

    # Verify active download inserted into DB
    active_dl = test_db.get_active_download(res["download_id"])
    assert active_dl is not None
    assert active_dl["download_hash"] == "daftpunkram1234567890abcdef"
    assert active_dl["client_id"] == "qbit-client-1"
    assert active_dl["request_id"] == "req-manual-1"
    assert active_dl["status"] == DownloadStatus.QUEUED.value

    # Verify linked request updated to processing
    updated_req = test_db.get_request("req-manual-1")
    assert updated_req is not None
    assert updated_req["status"] == RequestStatus.PROCESSING.value


def test_manual_grab_slskd_p2p_success(app_and_client, test_db, test_config, seeded_users):
    """Tests manual grab for a Soulseek p2p release dispatched to slskd."""
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # Seed slskd client
    slskd_cfg = DownloadClientConfig(
        id="slskd-client-1",
        name="slskd Local",
        driver_type="slskd",
        host_url="http://127.0.0.1:5030",
        enabled=True,
        priority=1,
    )
    test_db.create_download_client(slskd_cfg)

    grab_payload = {
        "release": {
            "id": "rel-slskd-202",
            "title": "Aphex Twin - Selected Ambient Works 85-92 [FLAC]",
            "indexer_name": "slskd",
            "protocol": "p2p",
            "size_bytes": 450000000,
            "parsed_quality": "FLAC 16bit",
            "is_acceptable": True,
            "score": 900,
            "meets_cutoff": True,
            "extra": {"username": "ambient_fan", "filename": "SAW8592.flac"},
        },
        "artist": "Aphex Twin",
        "title": "Selected Ambient Works 85-92",
        "item_type": "album",
    }

    mock_driver = MagicMock()
    mock_driver.download.return_value = "slskd-dl-987"

    with patch(
        "trackseerr.api.routes.acquisition.get_acquisition_driver",
        return_value=mock_driver,
    ):
        resp = client.post(
            "/api/acquisition/grab",
            headers=admin_headers,
            json=grab_payload,
        )

    assert resp.status_code == 200
    res = resp.json()
    assert res["success"] is True
    assert res["client"] == "slskd Local"

    # Verify active download recorded in DB
    active_dl = test_db.get_active_download(res["download_id"])
    assert active_dl is not None
    assert active_dl["download_hash"] == "slskd-dl-987"
    assert active_dl["client_id"] == "slskd-client-1"


def test_manual_grab_missing_download_client(app_and_client, test_db, test_config, seeded_users):
    """When no download client is available for protocol, grab must return HTTP 400."""
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # Empty download clients in DB
    grab_payload = {
        "release": {
            "id": "rel-nzb-1",
            "title": "Radiohead - Kid A [FLAC]",
            "indexer_name": "NZBGeek",
            "protocol": "usenet",
            "size_bytes": 300000000,
            "parsed_quality": "FLAC 16bit",
            "is_acceptable": True,
            "score": 900,
            "meets_cutoff": True,
        },
        "artist": "Radiohead",
        "title": "Kid A",
    }

    resp = client.post(
        "/api/acquisition/grab",
        headers=admin_headers,
        json=grab_payload,
    )
    assert resp.status_code == 400
    assert "No download client configured or available for protocol" in resp.json()["detail"]


def test_manual_grab_explicit_client_id(app_and_client, test_db, test_config, seeded_users):
    """Tests manual grab targeting a specific client_id."""
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    c1 = DownloadClientConfig(
        id="qbit-1",
        name="qBittorrent Primary",
        driver_type="qbittorrent",
        host_url="http://127.0.0.1:8080",
        priority=1,
    )
    c2 = DownloadClientConfig(
        id="qbit-2",
        name="qBittorrent Secondary",
        driver_type="qbittorrent",
        host_url="http://127.0.0.1:8081",
        priority=2,
    )
    test_db.create_download_client(c1)
    test_db.create_download_client(c2)

    grab_payload = {
        "release": {
            "id": "rel-1",
            "title": "Boards of Canada - Music Has the Right to Children [FLAC]",
            "indexer_name": "Torznab",
            "protocol": "torrent",
            "size_bytes": 400000000,
            "parsed_quality": "FLAC 16bit",
            "is_acceptable": True,
            "score": 900,
            "meets_cutoff": True,
        },
        "client_id": "qbit-2",
        "artist": "Boards of Canada",
        "title": "Music Has the Right to Children",
    }

    mock_driver = MagicMock()
    mock_driver.download.return_value = "sec-hash-123"

    with patch(
        "trackseerr.api.routes.acquisition.get_acquisition_driver",
        return_value=mock_driver,
    ):
        resp = client.post(
            "/api/acquisition/grab",
            headers=admin_headers,
            json=grab_payload,
        )

    assert resp.status_code == 200
    res = resp.json()
    assert res["client"] == "qBittorrent Secondary"

    active_dl = test_db.get_active_download(res["download_id"])
    assert active_dl["client_id"] == "qbit-2"


def test_manual_grab_driver_error_handling(app_and_client, test_db, test_config, seeded_users):
    """Tests proper HTTP 400/500 mapping when client driver download fails."""
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    c1 = DownloadClientConfig(
        id="qbit-1",
        name="qBittorrent Primary",
        driver_type="qbittorrent",
        host_url="http://127.0.0.1:8080",
    )
    test_db.create_download_client(c1)

    grab_payload = {
        "release": {
            "id": "rel-1",
            "title": "Burial - Untrue [FLAC]",
            "indexer_name": "Torznab",
            "protocol": "torrent",
            "size_bytes": 300000000,
            "parsed_quality": "FLAC 16bit",
            "is_acceptable": True,
            "score": 900,
            "meets_cutoff": True,
        },
        "artist": "Burial",
        "title": "Untrue",
    }

    # ValueError -> HTTP 400
    mock_val_err = MagicMock()
    mock_val_err.download.side_effect = ValueError("Corrupt torrent file")
    with patch(
        "trackseerr.api.routes.acquisition.get_acquisition_driver",
        return_value=mock_val_err,
    ):
        resp_400 = client.post("/api/acquisition/grab", headers=admin_headers, json=grab_payload)
    assert resp_400.status_code == 400
    assert "Corrupt torrent file" in resp_400.json()["detail"]

    # Generic Exception -> HTTP 500
    mock_gen_err = MagicMock()
    mock_gen_err.download.side_effect = RuntimeError("qBittorrent connection refused")
    with patch(
        "trackseerr.api.routes.acquisition.get_acquisition_driver",
        return_value=mock_gen_err,
    ):
        resp_500 = client.post("/api/acquisition/grab", headers=admin_headers, json=grab_payload)
    assert resp_500.status_code == 500
    assert "qBittorrent connection refused" in resp_500.json()["detail"]

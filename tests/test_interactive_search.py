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
from trackseerr.api.routes.acquisition import _strip_api_key
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.models import (
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


# ---------------------------------------------------------------------------
# Manual-search readiness: item profile, blocklisted releases, indexer errors
# ---------------------------------------------------------------------------

def _seed_artist_profile(test_db):
    test_db.upsert_quality_profile(
        QualityProfile(
            id="profile-artist",
            name="Artist Profile",
            cutoff="MP3 320",
            items=[QualityProfileItem(quality="MP3 320", allowed=True, weight=1000)],
            preferred_tags=[],
            ignored_tags=[],
            is_default=False,
        )
    )
    test_db.upsert_library_artist({"id": "art-p", "name": "Radiohead", "quality_profile_id": "profile-artist"})
    test_db.upsert_library_album({"id": "alb-p", "artist_id": "art-p", "title": "OK Computer"})
    test_db.upsert_library_track(
        {"id": "trk-p", "album_id": "alb-p", "artist_id": "art-p", "title": "Karma Police"}
    )


def _search(client, headers, body, candidates=None):
    with patch(
        "trackseerr.api.routes.acquisition.acquisition_coordinator.search_all_indexers",
        return_value=candidates or [],
    ):
        return client.post("/api/acquisition/search", headers=headers, json=body)


def test_search_uses_artist_profile_from_album_id(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    _seed_artist_profile(test_db)
    resp = _search(client, headers, {"artist": "Radiohead", "album": "OK Computer", "album_id": "alb-p"})
    assert resp.status_code == 200
    assert resp.json()["profile_name"] == "Artist Profile"


def test_search_uses_artist_profile_from_track_id(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    _seed_artist_profile(test_db)
    resp = _search(client, headers, {"artist": "Radiohead", "title": "Karma Police", "track_id": "trk-p"})
    assert resp.status_code == 200
    assert resp.json()["profile_name"] == "Artist Profile"


def test_search_falls_back_to_default_profile_for_unknown_album(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    _seed_artist_profile(test_db)
    resp = _search(client, headers, {"artist": "Radiohead", "album_id": "does-not-exist"})
    assert resp.status_code == 200
    assert resp.json()["profile_name"] == test_db.get_default_quality_profile()["name"]


def _patched_drivers(healthy_results, failing=False):
    healthy = MagicMock()
    healthy.search.return_value = healthy_results
    broken = MagicMock()
    broken.search.side_effect = RuntimeError("Connection refused")

    def factory(cfg):
        return broken if failing and cfg.get("id") == "idx-bad" else healthy

    return patch("trackseerr.acquisition_coordinator.get_indexer_driver", side_effect=factory)


def _make_indexer(test_db, idx_id, name):
    from trackseerr.models import IndexerConfig

    test_db.create_indexer(
        IndexerConfig(id=idx_id, name=name, indexer_type="torznab", host_url=f"http://{idx_id}:9696", enabled=True)
    )


def test_search_shows_blocklisted_release_as_rejected(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    _make_indexer(test_db, "idx-ok", "Good Indexer")
    title = "Radiohead - OK Computer [FLAC 16bit]"
    test_db.add_to_blocklist(source_title=title)
    cand = AcquisitionSearchResult(
        download_id="bl-1", title=title, artist="Radiohead", item_type="album",
        size_bytes=300 * 1024 * 1024, source="torznab",
    )
    with _patched_drivers([cand]):
        resp = client.post(
            "/api/acquisition/search", headers=headers, json={"artist": "Radiohead", "album": "OK Computer"}
        )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["is_acceptable"] is False
    assert results[0]["rejection_reasons"][0].startswith("Blocklisted")


def test_search_reports_indexer_errors(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    _make_indexer(test_db, "idx-bad", "Broken Indexer")
    _make_indexer(test_db, "idx-ok", "Good Indexer")
    cand = AcquisitionSearchResult(
        download_id="ok-1", title="Radiohead - OK Computer [FLAC 16bit]", artist="Radiohead",
        item_type="album", size_bytes=300 * 1024 * 1024, source="torznab",
    )
    with _patched_drivers([cand], failing=True):
        resp = client.post(
            "/api/acquisition/search", headers=headers, json={"artist": "Radiohead", "album": "OK Computer"}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert [e["source"] for e in data["indexer_errors"]] == ["Broken Indexer"]
    assert "Connection refused" in data["indexer_errors"][0]["error"]
    assert data["count"] >= 1


def _one_candidate(title="Radiohead - OK Computer [FLAC 16bit]", download_id="rel-1"):
    return AcquisitionSearchResult(
        download_id=download_id, title=title, artist="Radiohead", item_type="album",
        size_bytes=300 * 1024 * 1024, source="torznab",
    )


def _search_one(client, headers, cand, body=None):
    with _patched_drivers([cand]):
        return client.post(
            "/api/acquisition/search", headers=headers,
            json=body or {"artist": "Radiohead", "album": "OK Computer"},
        )


def test_blocklisted_reason_and_date_in_rejection(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    _make_indexer(test_db, "idx-ok", "Good Indexer")
    cand = _one_candidate()
    test_db.add_to_blocklist(source_title=cand.title, reason="Wrong release", indexer="ix")
    item = _search_one(client, headers, cand).json()["results"][0]
    assert item["is_acceptable"] is False
    first = item["rejection_reasons"][0]
    assert first.startswith("Blocklisted: Wrong release (") and first.endswith(")")
    assert len(first.split("(")[1].rstrip(")")) == 10
    assert item["blocklist"]["reason"] == "Wrong release" and item["blocklist"]["indexer"] == "ix"


def test_in_queue_release_flagged(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    _make_indexer(test_db, "idx-ok", "Good Indexer")
    cand = _one_candidate()
    test_db.create_download_client(
        DownloadClientConfig(
            id="c1", name="qbit", driver_type="qbittorrent", host_url="http://127.0.0.1:8080", enabled=True, priority=1
        )
    )
    test_db.create_active_download(
        {"id": "dl-1", "client_id": "c1", "title": cand.title, "artist": "Radiohead", "status": "downloading"}
    )
    item = _search_one(client, headers, cand).json()["results"][0]
    assert item["in_queue"] is True and item["is_acceptable"] is False
    assert item["rejection_reasons"][0] == "Already downloading"

    other = _one_candidate(title="Radiohead - Kid A [FLAC]", download_id="rel-2")
    assert _search_one(client, headers, other).json()["results"][0]["in_queue"] is False


def test_pack_song_not_flagged_in_queue_when_other_file_queued(
    app_and_client, test_db, test_config, seeded_users
):
    _, client = app_and_client
    headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    _make_indexer(test_db, "idx-ok", "Good Indexer")
    info_hash = "abcdef0123456789abcdef0123456789abcdef01"
    cand = AcquisitionSearchResult(
        download_id="pack-rel-1",
        title="Radiohead - Song 2 [FLAC] (in pack: Best Hits)",
        artist="Radiohead",
        item_type="track",
        size_bytes=30 * 1024 * 1024,
        source="torznab",
        magnet_url=f"magnet:?xt=urn:btih:{info_hash}&dn=Best+Hits",
        extra={
            "file_indices": [2],
            "info_hash": info_hash,
        },
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="c1",
            name="qbit",
            driver_type="qbittorrent",
            host_url="http://127.0.0.1:8080",
            enabled=True,
            priority=1,
        )
    )
    test_db.create_active_download(
        {
            "id": "dl-pack-song-1",
            "client_id": "c1",
            "title": "Radiohead - Song 1 [FLAC] (in pack: Best Hits)",
            "artist": "Radiohead",
            "download_hash": info_hash,
            "status": "downloading",
            "selected_files": [{"file_index": 1, "path": "01 - Song 1.flac"}],
        }
    )
    item = _search_one(client, headers, cand).json()["results"][0]
    assert item["in_queue"] is False
    assert "Already downloading" not in item.get("rejection_reasons", [])


def test_pack_song_flagged_in_queue_when_same_file_queued(
    app_and_client, test_db, test_config, seeded_users
):
    _, client = app_and_client
    headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    _make_indexer(test_db, "idx-ok", "Good Indexer")
    info_hash = "abcdef0123456789abcdef0123456789abcdef01"
    cand = AcquisitionSearchResult(
        download_id="pack-rel-1",
        title="Radiohead - Song 2 [FLAC] (in pack: Best Hits)",
        artist="Radiohead",
        item_type="track",
        size_bytes=30 * 1024 * 1024,
        source="torznab",
        magnet_url=f"magnet:?xt=urn:btih:{info_hash}&dn=Best+Hits",
        extra={
            "file_indices": [2],
            "info_hash": info_hash,
        },
    )
    test_db.create_download_client(
        DownloadClientConfig(
            id="c1",
            name="qbit",
            driver_type="qbittorrent",
            host_url="http://127.0.0.1:8080",
            enabled=True,
            priority=1,
        )
    )
    test_db.create_active_download(
        {
            "id": "dl-pack-song-2",
            "client_id": "c1",
            "title": "Radiohead - Song 2 [FLAC] (in pack: Best Hits)",
            "artist": "Radiohead",
            "download_hash": info_hash,
            "status": "downloading",
            "selected_files": [{"file_index": 2, "path": "02 - Song 2.flac"}],
        }
    )
    item = _search_one(client, headers, cand).json()["results"][0]
    assert item["in_queue"] is True
    assert item["is_acceptable"] is False
    assert item["rejection_reasons"][0] == "Already downloading"


def test_pending_release_has_pending_until(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    _make_indexer(test_db, "idx-ok", "Good Indexer")
    cand = _one_candidate()
    test_db.upsert_pending_release(
        {"item_key": "album:x", "title": cand.title, "added_at": "2026-10-11T10:00:00+00:00",
         "release_at": "2026-10-12T10:00:00+00:00"}
    )
    item = _search_one(client, headers, cand).json()["results"][0]
    assert item["pending_until"] == "2026-10-12T10:00:00+00:00"
    assert "Held by delay profile until 2026-10-12T10:00:00+00:00" in item["rejection_reasons"]
    assert item["in_queue"] is False


def test_last_search_from_item_events(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    _make_indexer(test_db, "idx-ok", "Good Indexer")
    test_db.upsert_library_artist({"id": "ar-1", "name": "Radiohead"})
    test_db.upsert_library_album({"id": "alb-1", "artist_id": "ar-1", "title": "OK Computer"})
    test_db.upsert_library_track(
        {"id": "trk-1", "album_id": "alb-1", "artist_id": "ar-1", "title": "Airbag", "track_number": 1}
    )
    cand = _one_candidate()
    body = {"artist": "Radiohead", "album": "OK Computer", "album_id": "alb-1", "track_id": "trk-1"}
    assert _search_one(client, headers, cand, body).json().get("last_search") is None

    test_db.record_item_event("searched", album_id="alb-1")
    test_db.record_item_event("searched", track_id="trk-1")
    last = _search_one(client, headers, cand, body).json()["last_search"]
    assert last["at"].endswith("Z") and last["message"]
    album_only = _search_one(client, headers, cand, {**body, "track_id": None}).json()["last_search"]
    assert album_only["at"]


def test_manual_grab_explicit_client_no_failover(app_and_client, test_db, test_config, seeded_users):
    """An explicit client_id targets exactly that client: its failure is an error, never a fall-over."""
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    for cid, prio in (("qbit-a", 1), ("qbit-b", 2)):
        test_db.create_download_client(
            DownloadClientConfig(
                id=cid, name=cid, driver_type="qbittorrent", host_url=f"http://127.0.0.1:{8080 + prio}",
                enabled=True, priority=prio,
            )
        )
    payload = {
        "release": {
            "id": "rel-1", "title": "A - B [FLAC]", "indexer_name": "Idx", "protocol": "torrent",
            "size_bytes": 1000, "magnet_url": "magnet:?xt=urn:btih:" + "b" * 40, "parsed_quality": "FLAC",
            "is_acceptable": True, "score": 1, "meets_cutoff": True,
        },
        "artist": "A", "title": "B", "item_type": "track", "client_id": "qbit-b",
    }
    driver = MagicMock()
    driver.download.side_effect = RuntimeError("qbit-b down")
    with patch("trackseerr.api.routes.acquisition.get_acquisition_driver", return_value=driver) as factory:
        resp = client.post("/api/acquisition/grab", headers=admin_headers, json=payload)
    assert resp.status_code == 500
    driver.download.assert_called_once()
    assert [c.args[0]["id"] for c in factory.call_args_list] == ["qbit-b"]


def test_strip_api_key_keeps_other_params():
    # Case-insensitive ApiKey
    assert (
        _strip_api_key("http://prowlarr:9696/download?ApiKey=SECRET&link=abc")
        == "http://prowlarr:9696/download?link=abc"
    )
    # Case-insensitive api_key
    assert (
        _strip_api_key("http://prowlarr:9696/download?api_key=SECRET&link=abc")
        == "http://prowlarr:9696/download?link=abc"
    )
    # URL without query
    assert _strip_api_key("http://prowlarr:9696/download") == "http://prowlarr:9696/download"
    # URL without apikey retains all params and order
    assert (
        _strip_api_key("http://prowlarr:9696/download?link=abc&file=x")
        == "http://prowlarr:9696/download?link=abc&file=x"
    )


def test_strip_api_key_none():
    assert _strip_api_key(None) is None
    assert _strip_api_key("") == ""


def test_search_hides_indexer_api_key(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    mock_candidates = [
        AcquisitionSearchResult(
            download_id="cand-1",
            title="Daft Punk - Discovery (2001) [FLAC 24bit-96kHz WEB]",
            artist="Daft Punk",
            album="Discovery",
            item_type="album",
            size_bytes=800 * 1024 * 1024,
            seeders=30,
            source="torznab",
            download_url="http://prowlarr:9696/1/download?apikey=SECRET123&link=abc&file=x",
            extra={
                "indexer_id": "idx-1",
                "indexer_name": "Prowlarr",
                "details_url": "http://prowlarr:9696/1/details?apikey=SECRET123&link=abc",
            },
        )
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
    assert "SECRET123" not in resp.text
    data = resp.json()
    assert len(data["results"]) == 1
    result = data["results"][0]
    assert "link=abc" in result["download_url"]
    assert "file=x" in result["download_url"]
    assert "apikey" not in result["download_url"]


def test_manual_grab_restores_indexer_api_key(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    test_db.create_download_client(
        DownloadClientConfig(
            id="qbit-client-1",
            name="qBittorrent Primary",
            driver_type="qbittorrent",
            host_url="http://127.0.0.1:8080",
            enabled=True,
            priority=1,
        )
    )
    test_db.create_indexer(
        IndexerConfig(
            id="idx-1",
            name="Prowlarr",
            indexer_type="torznab",
            host_url="http://prowlarr:9696/13/",
            api_key="SECRET123",
            enabled=True,
        )
    )
    test_db.create_request(
        MusicRequest(
            id="req-manual-1",
            user_id="user-alice",
            item_type="album",
            title="Random Access Memories",
            artist="Daft Punk",
            status=RequestStatus.PENDING,
        )
    )

    grab_payload = {
        "release": {
            "id": "rel-qbit-101",
            "title": "Daft Punk - Random Access Memories (2013) [FLAC 24bit]",
            "indexer_name": "Prowlarr",
            "protocol": "torrent",
            "size_bytes": 1200000000,
            "seeders": 45,
            "download_url": "http://prowlarr:9696/1/download?link=abc",
            "magnet_url": None,
            "parsed_quality": "FLAC 24bit",
            "is_acceptable": True,
            "score": 1050,
            "meets_cutoff": True,
            "rejection_reasons": [],
            "extra": {"indexer_id": "idx-1", "indexer_name": "Prowlarr"},
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
    mock_driver.download.assert_called_once()
    download_arg = mock_driver.download.call_args[0][0]
    assert "apikey=SECRET123" in download_arg.download_url
    assert "link=abc" in download_arg.download_url


def test_manual_grab_never_sends_key_to_other_host(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    test_db.create_download_client(
        DownloadClientConfig(
            id="qbit-client-1",
            name="qBittorrent Primary",
            driver_type="qbittorrent",
            host_url="http://127.0.0.1:8080",
            enabled=True,
            priority=1,
        )
    )
    test_db.create_indexer(
        IndexerConfig(
            id="idx-1",
            name="Prowlarr",
            indexer_type="torznab",
            host_url="http://prowlarr:9696/13/",
            api_key="SECRET123",
            enabled=True,
        )
    )
    test_db.create_request(
        MusicRequest(
            id="req-manual-1",
            user_id="user-alice",
            item_type="album",
            title="Random Access Memories",
            artist="Daft Punk",
            status=RequestStatus.PENDING,
        )
    )

    grab_payload = {
        "release": {
            "id": "rel-qbit-101",
            "title": "Daft Punk - Random Access Memories (2013) [FLAC 24bit]",
            "indexer_name": "Prowlarr",
            "protocol": "torrent",
            "size_bytes": 1200000000,
            "seeders": 45,
            "download_url": "http://evil.example/x?link=abc",
            "magnet_url": None,
            "parsed_quality": "FLAC 24bit",
            "is_acceptable": True,
            "score": 1050,
            "meets_cutoff": True,
            "rejection_reasons": [],
            "extra": {"indexer_id": "idx-1", "indexer_name": "Prowlarr"},
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
    mock_driver.download.assert_called_once()
    download_arg = mock_driver.download.call_args[0][0]
    assert "apikey" not in download_arg.download_url
    assert download_arg.download_url == "http://evil.example/x?link=abc"


# ---------------------------------------------------------------------------
# Manual Search Fallback Tests
# ---------------------------------------------------------------------------

def test_manual_search_also_searches_album(app_and_client, test_db, test_config, seeded_users):
    """Track search with album 'BEYONCÉ' != title 'XO' -> second call with title=None, album='BEYONCÉ'."""
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    track_cand = AcquisitionSearchResult(
        download_id="idx-track-1",
        title="Beyonce - XO [FLAC]",
        artist="Beyoncé",
        album="BEYONCÉ",
        protocol="slskd",
        item_type="track",
        size_bytes=30 * 1024 * 1024,
    )
    album_cand = AcquisitionSearchResult(
        download_id="idx-album-1",
        title="Beyonce - BEYONCE (2013) [FLAC]",
        artist="Beyoncé",
        album="BEYONCÉ",
        protocol="usenet",
        item_type="album",
        size_bytes=400 * 1024 * 1024,
    )

    def fake_search_all_indexers(*args, **kwargs):
        if kwargs.get("title") == "XO":
            return [track_cand]
        if kwargs.get("title") is None and kwargs.get("album") == "BEYONCÉ":
            return [album_cand]
        return []

    with patch(
        "trackseerr.api.routes.acquisition.acquisition_coordinator.search_all_indexers",
        side_effect=fake_search_all_indexers,
    ) as mock_search:
        resp = client.post(
            "/api/acquisition/search",
            headers=admin_headers,
            json={"artist": "Beyoncé", "title": "XO", "album": "BEYONCÉ", "item_type": "track"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert mock_search.call_count == 2
    # Verify second call args
    second_call = mock_search.call_args_list[1]
    assert second_call.kwargs.get("title") is None
    assert second_call.kwargs.get("album") == "BEYONCÉ"
    # Result appears in the response
    returned_ids = [r["id"] for r in data["results"]]
    assert "idx-track-1" in returned_ids
    assert "idx-album-1" in returned_ids


def test_manual_search_skips_album_when_same_as_title(app_and_client, test_db, test_config, seeded_users):
    """Album == title -> one call only."""
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    cand = AcquisitionSearchResult(
        download_id="idx-1",
        title="Daft Punk - Discovery [FLAC]",
        artist="Daft Punk",
        album="Discovery",
        protocol="slskd",
        item_type="track",
        size_bytes=40 * 1024 * 1024,
    )

    with patch(
        "trackseerr.api.routes.acquisition.acquisition_coordinator.search_all_indexers",
        return_value=[cand],
    ) as mock_search:
        resp = client.post(
            "/api/acquisition/search",
            headers=admin_headers,
            json={"artist": "Daft Punk", "title": "Discovery", "album": "Discovery", "item_type": "track"},
        )

    assert resp.status_code == 200
    assert mock_search.call_count == 1


def test_manual_search_uses_banned_term_tier_when_no_soulseek_results(app_and_client, test_db, test_config, seeded_users):
    """First call returns only a usenet result -> _banned_term_tier called, its slskd result appears."""
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    usenet_cand = AcquisitionSearchResult(
        download_id="idx-usenet-1",
        title="Beyonce - XO [FLAC]",
        artist="Beyoncé",
        album="BEYONCÉ",
        protocol="usenet",
        item_type="track",
        size_bytes=30 * 1024 * 1024,
    )
    slskd_cand = AcquisitionSearchResult(
        download_id="idx-slskd-1",
        title="Beyonce - XO (Single) [FLAC]",
        artist="Beyoncé",
        album="BEYONCÉ",
        protocol="slskd",
        item_type="track",
        size_bytes=30 * 1024 * 1024,
    )

    with patch(
        "trackseerr.api.routes.acquisition.acquisition_coordinator.search_all_indexers",
        return_value=[usenet_cand],
    ), patch(
        "trackseerr.api.routes.acquisition.acquisition_coordinator._banned_term_tier",
        return_value=[slskd_cand],
    ) as mock_banned:
        resp = client.post(
            "/api/acquisition/search",
            headers=admin_headers,
            json={"artist": "Beyoncé", "title": "XO", "album": "BEYONCÉ", "item_type": "track"},
        )

    assert resp.status_code == 200
    mock_banned.assert_called_once()
    assert mock_banned.call_args.kwargs.get("start_rung") == 2
    data = resp.json()
    returned_ids = [r["id"] for r in data["results"]]
    assert "idx-usenet-1" in returned_ids
    assert "idx-slskd-1" in returned_ids


def test_manual_search_skips_banned_tier_when_soulseek_answered(app_and_client, test_db, test_config, seeded_users):
    """Soulseek answered in search -> _banned_term_tier is skipped."""
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    slskd_cand = AcquisitionSearchResult(
        download_id="idx-slskd-1",
        title="Beyonce - XO [FLAC]",
        artist="Beyoncé",
        album="BEYONCÉ",
        protocol="slskd",
        item_type="track",
        size_bytes=30 * 1024 * 1024,
    )

    with patch(
        "trackseerr.api.routes.acquisition.acquisition_coordinator.search_all_indexers",
        return_value=[slskd_cand],
    ), patch(
        "trackseerr.api.routes.acquisition.acquisition_coordinator._banned_term_tier",
    ) as mock_banned:
        resp = client.post(
            "/api/acquisition/search",
            headers=admin_headers,
            json={"artist": "Beyoncé", "title": "XO", "album": "BEYONCÉ", "item_type": "track"},
        )

    assert resp.status_code == 200
    mock_banned.assert_not_called()


def test_manual_search_dedupes_results(app_and_client, test_db, test_config, seeded_users):
    """The same download_id from both calls appears once."""
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    dup_cand1 = AcquisitionSearchResult(
        download_id="dup-1",
        title="Beyonce - XO [FLAC 16bit]",
        artist="Beyoncé",
        album="BEYONCÉ",
        protocol="slskd",
        item_type="track",
        size_bytes=30 * 1024 * 1024,
    )
    dup_cand2 = AcquisitionSearchResult(
        download_id="dup-1",
        title="Beyonce - XO [FLAC 16bit]",
        artist="Beyoncé",
        album="BEYONCÉ",
        protocol="slskd",
        item_type="track",
        size_bytes=30 * 1024 * 1024,
    )

    # First search (track) and second search (album) both return dup-1
    with patch(
        "trackseerr.api.routes.acquisition.acquisition_coordinator.search_all_indexers",
        side_effect=[[dup_cand1], [dup_cand2]],
    ):
        resp = client.post(
            "/api/acquisition/search",
            headers=admin_headers,
            json={"artist": "Beyoncé", "title": "XO", "album": "BEYONCÉ", "item_type": "track"},
        )

    assert resp.status_code == 200
    data = resp.json()
    returned_ids = [r["id"] for r in data["results"]]
    assert returned_ids.count("dup-1") == 1



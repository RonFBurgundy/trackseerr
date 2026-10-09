"""Tests for Transmission BitTorrent Acquisition Driver."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
import httpx
import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.acquisition import (
    get_acquisition_driver,
    is_torrent_driver_type,
)
from trackseerr.clients.acquisition.base import AcquisitionRetryableError
from trackseerr.clients.acquisition.transmission import TransmissionDriver
from trackseerr.config import Config
from trackseerr.models import (
    AcquisitionSearchResult,
    DownloadDriverType,
    DownloadStatus,
)
from trackseerr.storage import Database


def _mock_response(status_code: int = 200, payload: Any = None, headers: Any = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.text = str(payload or "")
    resp.headers = headers or {}
    return resp


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()


@pytest.fixture
def config(tmp_path: Path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="token", data_dir=str(tmp_path))


@pytest.fixture
def client(db: Database, config: Config):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


@pytest.fixture
def auth_headers(db: Database, config: Config) -> dict[str, str]:
    admin = db.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=admin["id"], username="admin_user", is_admin=True, secret_key=secret)
    db.create_session(token, admin["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Connection & Auth Tests
# ---------------------------------------------------------------------------
def test_transmission_test_connection_success():
    driver = TransmissionDriver("http://transmission.local:9091", username="user", password="pwd")
    resp = _mock_response(
        200,
        {"result": "success", "arguments": {"version": "4.0.5"}},
    )

    with patch("httpx.Client.post", return_value=resp):
        ok, msg = driver.test_connection()
        assert ok is True
        assert "4.0.5" in msg


def test_transmission_test_connection_auth_failure():
    driver = TransmissionDriver("http://transmission.local:9091", username="wrong", password="pwd")
    resp = _mock_response(401, {"result": "unauthorized"})

    with patch("httpx.Client.post", return_value=resp):
        ok, msg = driver.test_connection()
        assert ok is False
        assert "Authentication failed" in msg


def test_transmission_409_handshake():
    driver = TransmissionDriver("http://transmission.local:9091")
    resp_409 = _mock_response(409, headers={"X-Transmission-Session-Id": "sess-token-999"})
    resp_200 = _mock_response(200, {"result": "success", "arguments": {"version": "4.0.5"}})

    with patch("httpx.Client.post", side_effect=[resp_409, resp_200]) as mock_post:
        ok, msg = driver.test_connection()
        assert ok is True
        assert "4.0.5" in msg
        assert driver._session_id == "sess-token-999"
        assert mock_post.call_count == 2
        # Second call must include the new session id
        second_call_headers = mock_post.call_args_list[1].kwargs["headers"]
        assert second_call_headers.get("X-Transmission-Session-Id") == "sess-token-999"


def test_transmission_ssrf_rejection():
    driver = TransmissionDriver("http://169.254.169.254/latest/meta-data")
    ok, msg = driver.test_connection()
    assert ok is False
    assert "SSRF" in msg

    item = AcquisitionSearchResult(
        download_id="hash1",
        title="Album",
        artist="Artist",
        download_url="https://example.com/torrent.torrent",
    )
    with pytest.raises(ValueError, match="Prohibited host URL"):
        driver.download(item)


# ---------------------------------------------------------------------------
# Download submission (URL + Magnet)
# ---------------------------------------------------------------------------
def test_transmission_download_url():
    driver = TransmissionDriver("http://transmission.local:9091", category="music")
    resp = _mock_response(
        200,
        {
            "result": "success",
            "arguments": {
                "torrent-added": {
                    "hashString": "1122334455667788990011223344556677889900",
                    "id": 1,
                    "name": "Radiohead - Kid A",
                }
            },
        },
    )

    item = AcquisitionSearchResult(
        download_id="placeholder",
        title="Kid A",
        artist="Radiohead",
        download_url="https://tracker.org/download/kida.torrent",
    )

    with patch("httpx.Client.post", return_value=resp) as mock_post:
        torrent_hash = driver.download(item)
        assert torrent_hash == "1122334455667788990011223344556677889900"
        payload = mock_post.call_args.kwargs["json"]
        assert payload["method"] == "torrent-add"
        assert payload["arguments"]["filename"] == "https://tracker.org/download/kida.torrent"
        assert payload["arguments"]["labels"] == ["music"]


def test_transmission_download_magnet():
    driver = TransmissionDriver("http://transmission.local:9091", category="trackseerr")
    magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Album"
    resp = _mock_response(
        200,
        {
            "result": "success",
            "arguments": {
                "torrent-duplicate": {
                    "hashString": "0123456789abcdef0123456789abcdef01234567",
                    "id": 2,
                    "name": "Album",
                }
            },
        },
    )

    item = AcquisitionSearchResult(
        download_id="original",
        title="Album",
        artist="Artist",
        magnet_url=magnet,
    )

    with patch("httpx.Client.post", return_value=resp) as mock_post:
        torrent_hash = driver.download(item)
        assert torrent_hash == "0123456789abcdef0123456789abcdef01234567"
        payload = mock_post.call_args.kwargs["json"]
        assert payload["arguments"]["filename"] == magnet
        assert payload["arguments"]["labels"] == ["trackseerr"]


def test_transmission_download_network_error_raises_retryable():
    driver = TransmissionDriver("http://transmission.local:9091")
    item = AcquisitionSearchResult(
        download_id="dl1",
        title="Album",
        artist="Artist",
        download_url="https://tracker.org/album.torrent",
    )

    with patch("httpx.Client.post", side_effect=httpx.ConnectError("Connection refused")):
        with pytest.raises(AcquisitionRetryableError, match="Network error connecting to Transmission"):
            driver.download(item)


# ---------------------------------------------------------------------------
# Status Mapping Tests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rpc_status,percent_done,expected_status,expected_progress",
    [
        (4, 0.45, DownloadStatus.DOWNLOADING.value, 45.0),
        (6, 1.0, DownloadStatus.COMPLETED.value, 100.0),
        (5, 1.0, DownloadStatus.COMPLETED.value, 100.0),
        (0, 1.0, DownloadStatus.COMPLETED.value, 100.0),  # stopped / finished seeding
        (0, 0.2, DownloadStatus.QUEUED.value, 20.0),     # paused during download
        (1, 0.1, DownloadStatus.QUEUED.value, 10.0),     # check wait
        (2, 0.1, DownloadStatus.QUEUED.value, 10.0),     # checking
        (3, 0.0, DownloadStatus.QUEUED.value, 0.0),      # download wait
    ],
)
def test_transmission_status_mapping(rpc_status, percent_done, expected_status, expected_progress):
    driver = TransmissionDriver("http://transmission.local:9091")
    resp = _mock_response(
        200,
        {
            "result": "success",
            "arguments": {
                "torrents": [
                    {
                        "id": 1,
                        "hashString": "abc123hash",
                        "name": "Album Name",
                        "totalSize": 250000000,
                        "percentDone": percent_done,
                        "rateDownload": 1250000,
                        "eta": 180,
                        "status": rpc_status,
                        "error": 0,
                        "errorString": "",
                        "downloadDir": "/downloads/complete",
                        "uploadRatio": 1.25,
                        "secondsSeeding": 3600,
                    }
                ]
            },
        },
    )

    with patch("httpx.Client.post", return_value=resp):
        status = driver.get_status("abc123hash")
        assert status["status"] == expected_status
        assert status["progress"] == expected_progress
        assert status["size_bytes"] == 250000000
        assert status["speed_bps"] == 1250000
        assert status["eta_seconds"] == 180
        assert status["ratio"] == 1.25
        assert status["seeding_time_seconds"] == 3600
        assert status["source_path"] == "/downloads/complete/Album Name"


def test_transmission_status_mapping_error():
    driver = TransmissionDriver("http://transmission.local:9091")
    resp = _mock_response(
        200,
        {
            "result": "success",
            "arguments": {
                "torrents": [
                    {
                        "id": 1,
                        "hashString": "abc123hash",
                        "name": "Failed Album",
                        "status": 0,
                        "error": 3,
                        "errorString": "No space left on device",
                        "percentDone": 0.5,
                    }
                ]
            },
        },
    )

    with patch("httpx.Client.post", return_value=resp):
        status = driver.get_status("abc123hash")
        assert status["status"] == DownloadStatus.FAILED.value
        assert status["error_message"] == "No space left on device"


def test_transmission_status_not_found():
    driver = TransmissionDriver("http://transmission.local:9091")
    resp = _mock_response(200, {"result": "success", "arguments": {"torrents": []}})

    with patch("httpx.Client.post", return_value=resp):
        status = driver.get_status("missing")
        assert status["status"] == DownloadStatus.QUEUED.value
        assert status["progress"] == 0.0


# ---------------------------------------------------------------------------
# Cancel & Cleanup Tests
# ---------------------------------------------------------------------------
def test_transmission_cancel():
    driver = TransmissionDriver("http://transmission.local:9091")
    resp = _mock_response(200, {"result": "success", "arguments": {}})

    with patch("httpx.Client.post", return_value=resp) as mock_post:
        assert driver.cancel("abc123hash") is True
        payload = mock_post.call_args.kwargs["json"]
        assert payload["method"] == "torrent-remove"
        assert payload["arguments"]["ids"] == ["abc123hash"]
        assert payload["arguments"]["delete-local-data"] is True


def test_transmission_cleanup_completed():
    driver = TransmissionDriver("http://transmission.local:9091")
    resp = _mock_response(200, {"result": "success", "arguments": {}})

    with patch("httpx.Client.post", return_value=resp) as mock_post:
        # Without deleting files
        assert driver.cleanup_completed("abc123hash", delete_files=False) is True
        assert mock_post.call_args.kwargs["json"]["arguments"]["delete-local-data"] is False

        # With deleting files
        assert driver.cleanup_completed("abc123hash", delete_files=True) is True
        assert mock_post.call_args.kwargs["json"]["arguments"]["delete-local-data"] is True


# ---------------------------------------------------------------------------
# Download Roots Tests
# ---------------------------------------------------------------------------
def test_transmission_roots():
    driver = TransmissionDriver("http://transmission.local:9091", download_dir="/override/dir")
    resp = _mock_response(200, {"result": "success", "arguments": {"download-dir": "/data/downloads"}})

    with patch("httpx.Client.post", return_value=resp):
        roots = driver.get_download_roots()
        assert roots == ["/data/downloads", "/override/dir"]
        assert driver.last_roots_error is None


def test_transmission_roots_failure():
    driver = TransmissionDriver("http://transmission.local:9091")
    with patch("httpx.Client.post", side_effect=httpx.ConnectError("down")):
        roots = driver.get_download_roots()
        assert roots == []
        assert "down" in (driver.last_roots_error or "")


# ---------------------------------------------------------------------------
# Share Limits Tests
# ---------------------------------------------------------------------------
def test_transmission_set_share_limits():
    driver = TransmissionDriver("http://transmission.local:9091")
    resp = _mock_response(200, {"result": "success", "arguments": {}})

    with patch("httpx.Client.post", return_value=resp) as mock_post:
        # Specific limits
        assert driver.set_share_limits("abc123hash", ratio=2.5, seed_time_minutes=120) is True
        args = mock_post.call_args.kwargs["json"]["arguments"]
        assert args["seedRatioMode"] == 1
        assert args["seedRatioLimit"] == 2.5
        assert args["seedIdleMode"] == 1
        assert args["seedIdleLimit"] == 120

        # Global limits (None)
        assert driver.set_share_limits("abc123hash", ratio=None, seed_time_minutes=None) is True
        args2 = mock_post.call_args.kwargs["json"]["arguments"]
        assert args2["seedRatioMode"] == 0
        assert args2["seedIdleMode"] == 0

        # Unlimited (0)
        assert driver.set_share_limits("abc123hash", ratio=0, seed_time_minutes=0) is True
        args3 = mock_post.call_args.kwargs["json"]["arguments"]
        assert args3["seedRatioMode"] == 2
        assert args3["seedIdleMode"] == 2


# ---------------------------------------------------------------------------
# List Category Tests
# ---------------------------------------------------------------------------
def test_transmission_list_category():
    driver = TransmissionDriver("http://transmission.local:9091", category="trackseerr")
    resp = _mock_response(
        200,
        {
            "result": "success",
            "arguments": {
                "torrents": [
                    {
                        "hashString": "1111111111111111111111111111111111111111",
                        "name": "TrackSeerr Album",
                        "totalSize": 1000,
                        "uploadRatio": 1.5,
                        "secondsSeeding": 600,
                        "downloadDir": "/downloads",
                        "status": 6,
                        "labels": ["trackseerr"],
                    },
                    {
                        "hashString": "2222222222222222222222222222222222222222",
                        "name": "Other Torrent",
                        "labels": ["other"],
                    },
                ]
            },
        },
    )

    with patch("httpx.Client.post", return_value=resp):
        items = driver.list_category()
        assert items is not None
        assert len(items) == 1
        assert items[0]["name"] == "TrackSeerr Album"
        assert items[0]["ratio"] == 1.5
        assert items[0]["content_path"] == "/downloads/TrackSeerr Album"

    driver_no_cat = TransmissionDriver("http://transmission.local:9091", category="")
    assert driver_no_cat.list_category() is None


# ---------------------------------------------------------------------------
# Factory, Classification, and Route Test
# ---------------------------------------------------------------------------
def test_transmission_factory_and_classification():
    cfg = {
        "id": "c_trans",
        "name": "Transmission Local",
        "driver_type": DownloadDriverType.TRANSMISSION.value,
        "host_url": "http://transmission:9091",
        "username": "user",
        "password": "pwd",
        "extra_settings_json": '{"rpc_path": "/custom/rpc", "category": "music"}',
    }
    driver = get_acquisition_driver(cfg)
    assert isinstance(driver, TransmissionDriver)
    assert driver.rpc_path == "/custom/rpc"
    assert driver.category == "music"
    assert is_torrent_driver_type(DownloadDriverType.TRANSMISSION.value) is True


def test_route_create_transmission(client: TestClient, auth_headers: dict[str, str]):
    payload = {
        "name": "My Transmission",
        "driver_type": "transmission",
        "host_url": "http://192.168.1.50:9091",
        "username": "user",
        "password": "secretpassword",
        "category": "trackseerr",
    }
    resp = client.post("/api/settings/download-clients", json=payload, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "My Transmission"
    assert data["driver_type"] == "transmission"
    assert "••••" in data["password"]
    assert "secretpassword" not in data["password"]

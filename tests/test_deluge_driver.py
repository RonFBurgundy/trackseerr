"""Tests for Deluge Web UI Acquisition Driver."""

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
from trackseerr.clients.acquisition.deluge import DelugeDriver
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
def test_deluge_test_connection_success():
    driver = DelugeDriver("http://deluge.local:8112", password="secretpassword")

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        if method == "auth.login":
            return _mock_response(200, {"result": True, "error": None}, {"set-cookie": "_session_id=deluge123"})
        elif method == "web.connected":
            return _mock_response(200, {"result": True, "error": None})
        elif method == "daemon.get_version":
            return _mock_response(200, {"result": "2.1.1", "error": None})
        return _mock_response(200, {"result": None, "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        ok, msg = driver.test_connection()
        assert ok is True
        assert "2.1.1" in msg


def test_deluge_test_connection_auth_failure():
    driver = DelugeDriver("http://deluge.local:8112", password="wrongpassword")
    resp = _mock_response(200, {"result": False, "error": None})

    with patch("httpx.Client.post", return_value=resp):
        ok, msg = driver.test_connection()
        assert ok is False
        assert "Authentication failed" in msg


def test_deluge_test_connection_connects_to_daemon():
    driver = DelugeDriver("http://deluge.local:8112", password="pwd")
    calls = []

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        calls.append(method)
        if method == "auth.login":
            return _mock_response(200, {"result": True, "error": None})
        elif method == "web.connected":
            return _mock_response(200, {"result": False, "error": None})
        elif method == "web.get_hosts":
            return _mock_response(200, {"result": [["host_id_1", "127.0.0.1", 58846, "Online"]], "error": None})
        elif method == "web.connect":
            return _mock_response(200, {"result": True, "error": None})
        elif method == "daemon.get_version":
            return _mock_response(200, {"result": "2.1.1", "error": None})
        return _mock_response(200, {"result": None, "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        ok, msg = driver.test_connection()
        assert ok is True
        assert "web.get_hosts" in calls
        assert "web.connect" in calls


def test_deluge_test_connection_daemon_version_fallback():
    driver = DelugeDriver("http://deluge.local:8112", password="pwd")
    calls = []

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        calls.append(method)
        if method == "auth.login":
            return _mock_response(200, {"result": True, "error": None})
        elif method == "web.connected":
            return _mock_response(200, {"result": True, "error": None})
        elif method == "daemon.get_version":
            return _mock_response(200, {"result": None, "error": {"message": "Unknown method", "code": 1}})
        elif method == "web.get_version":
            return _mock_response(200, {"result": "2.1.1-web", "error": None})
        return _mock_response(200, {"result": None, "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        ok, msg = driver.test_connection()
        assert ok is True
        assert "2.1.1-web" in msg
        assert "daemon.get_version" in calls
        assert "web.get_version" in calls


def test_deluge_ssrf_rejection():
    driver = DelugeDriver("http://169.254.169.254/latest/meta-data")
    ok, msg = driver.test_connection()
    assert ok is False
    assert "SSRF" in msg

    item = AcquisitionSearchResult(
        download_id="hash1",
        title="Album",
        artist="Artist",
        download_url="https://tracker.org/download.torrent",
    )
    with pytest.raises(ValueError, match="Prohibited host URL"):
        driver.download(item)


# ---------------------------------------------------------------------------
# Download submission (URL + Magnet + Label Plugin)
# ---------------------------------------------------------------------------
def test_deluge_download_url_and_label_plugin():
    driver = DelugeDriver("http://deluge.local:8112", password="pwd", label="trackseerr", download_location="/data/torrents")
    calls = []

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        params = json.get("params", []) if json else []
        calls.append((method, params))
        if method == "auth.login":
            return _mock_response(200, {"result": True, "error": None}, {"set-cookie": "sess=1"})
        elif method == "web.connected":
            return _mock_response(200, {"result": True, "error": None})
        elif method == "core.add_torrent_url":
            return _mock_response(200, {"result": "fedcba9876543210fedcba9876543210fedcba98", "error": None})
        elif method == "core.get_enabled_plugins":
            return _mock_response(200, {"result": ["Label"], "error": None})
        elif method in ("label.add", "label.set_torrent"):
            return _mock_response(200, {"result": True, "error": None})
        return _mock_response(200, {"result": None, "error": None})

    item = AcquisitionSearchResult(
        download_id="placeholder",
        title="Album",
        artist="Artist",
        download_url="https://tracker.org/album.torrent",
    )

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        thash = driver.download(item)
        assert thash == "fedcba9876543210fedcba9876543210fedcba98"

        methods = [c[0] for c in calls]
        assert "core.add_torrent_url" in methods
        assert "core.get_enabled_plugins" in methods
        assert "label.add" in methods
        assert "label.set_torrent" in methods

        # Verify add_torrent_url received download_location
        add_call = [c for c in calls if c[0] == "core.add_torrent_url"][0]
        assert add_call[1][1] == {"download_location": "/data/torrents"}


def test_deluge_download_label_add_raises_still_sets_torrent_label():
    driver = DelugeDriver("http://deluge.local:8112", password="pwd", label="trackseerr")
    calls = []

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        params = json.get("params", []) if json else []
        calls.append((method, params))
        if method == "auth.login":
            return _mock_response(200, {"result": True, "error": None}, {"set-cookie": "sess=1"})
        elif method == "web.connected":
            return _mock_response(200, {"result": True, "error": None})
        elif method == "core.add_torrent_url":
            return _mock_response(200, {"result": "fedcba9876543210fedcba9876543210fedcba98", "error": None})
        elif method == "core.get_enabled_plugins":
            return _mock_response(200, {"result": ["Label"], "error": None})
        elif method == "label.add":
            return _mock_response(200, {"result": None, "error": {"message": "Label already exists", "code": 1}})
        elif method == "label.set_torrent":
            return _mock_response(200, {"result": True, "error": None})
        return _mock_response(200, {"result": None, "error": None})

    item = AcquisitionSearchResult(
        download_id="placeholder",
        title="Album",
        artist="Artist",
        download_url="https://tracker.org/album.torrent",
    )

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        thash = driver.download(item)
        assert thash == "fedcba9876543210fedcba9876543210fedcba98"

        methods = [c[0] for c in calls]
        assert "label.add" in methods
        assert "label.set_torrent" in methods
        set_call = [c for c in calls if c[0] == "label.set_torrent"][0]
        assert set_call[1] == ["fedcba9876543210fedcba9876543210fedcba98", "trackseerr"]


def test_deluge_download_magnet():
    driver = DelugeDriver("http://deluge.local:8112", password="pwd", label=None)
    magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Album"

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        if method == "auth.login":
            return _mock_response(200, {"result": True, "error": None}, {"set-cookie": "sess=1"})
        elif method == "web.connected":
            return _mock_response(200, {"result": True, "error": None})
        elif method == "core.add_torrent_magnet":
            return _mock_response(200, {"result": "0123456789abcdef0123456789abcdef01234567", "error": None})
        return _mock_response(200, {"result": None, "error": None})

    item = AcquisitionSearchResult(
        download_id="original",
        title="Album",
        artist="Artist",
        magnet_url=magnet,
    )

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        thash = driver.download(item)
        assert thash == "0123456789abcdef0123456789abcdef01234567"


def test_deluge_download_network_error_raises_retryable():
    driver = DelugeDriver("http://deluge.local:8112", password="pwd")
    item = AcquisitionSearchResult(
        download_id="dl1",
        title="Album",
        artist="Artist",
        download_url="https://tracker.org/album.torrent",
    )

    with patch("httpx.Client.post", side_effect=httpx.ConnectError("Connection refused")):
        with pytest.raises(AcquisitionRetryableError, match="Network error connecting to Deluge"):
            driver.download(item)


# ---------------------------------------------------------------------------
# Re-login on auth error
# ---------------------------------------------------------------------------
def test_deluge_relogin_on_auth_error():
    driver = DelugeDriver("http://deluge.local:8112", password="pwd")
    driver._cookie = "expired_sess"

    state = {"login_called": 0}

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        if method == "auth.login":
            state["login_called"] += 1
            return _mock_response(200, {"result": True, "error": None}, {"set-cookie": "fresh_sess"})
        elif method == "web.connected":
            return _mock_response(200, {"result": True, "error": None})
        elif method == "core.get_config_value":
            if state["login_called"] == 0:
                # First time: return auth error
                return _mock_response(200, {"result": None, "error": {"code": 1, "message": "Not authenticated"}})
            # After re-login: succeed
            return _mock_response(200, {"result": "/data/complete", "error": None})
        return _mock_response(200, {"result": None, "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        roots = driver.get_download_roots()
        assert roots == ["/data/complete"]
        assert state["login_called"] == 1


# ---------------------------------------------------------------------------
# Status Mapping Tests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "deluge_state,deluge_progress,expected_status,expected_progress",
    [
        ("Downloading", 42.5, DownloadStatus.DOWNLOADING.value, 42.5),
        ("Seeding", 100.0, DownloadStatus.COMPLETED.value, 100.0),
        ("Paused", 100.0, DownloadStatus.COMPLETED.value, 100.0),
        ("Paused", 50.0, DownloadStatus.QUEUED.value, 50.0),
        ("Queued", 0.0, DownloadStatus.QUEUED.value, 0.0),
        ("Checking", 10.0, DownloadStatus.QUEUED.value, 10.0),
        ("Error", 80.0, DownloadStatus.FAILED.value, 80.0),
    ],
)
def test_deluge_status_mapping(deluge_state, deluge_progress, expected_status, expected_progress):
    driver = DelugeDriver("http://deluge.local:8112", password="pwd")

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        if method == "core.get_torrents_status":
            return _mock_response(
                200,
                {
                    "result": {
                        "hash123": {
                            "name": "Album",
                            "state": deluge_state,
                            "progress": deluge_progress,
                            "total_size": 500000000,
                            "download_payload_rate": 2000000,
                            "eta": 90,
                            "save_path": "/data/torrents",
                            "ratio": 1.75,
                            "seeding_time": 7200,
                            "message": "Disk full" if deluge_state == "Error" else "",
                        }
                    },
                    "error": None,
                },
            )
        return _mock_response(200, {"result": True, "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        st = driver.get_status("hash123")
        assert st["status"] == expected_status
        assert st["progress"] == expected_progress
        assert st["size_bytes"] == 500000000
        assert st["speed_bps"] == 2000000
        assert st["eta_seconds"] == 90
        assert st["ratio"] == 1.75
        assert st["seeding_time_seconds"] == 7200
        assert st["source_path"] == "/data/torrents/Album"
        if deluge_state == "Error":
            assert st["error_message"] == "Disk full"


# ---------------------------------------------------------------------------
# Cancel & Cleanup Tests
# ---------------------------------------------------------------------------
def test_deluge_cancel():
    driver = DelugeDriver("http://deluge.local:8112", password="pwd")
    calls = []

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        params = json.get("params", []) if json else []
        calls.append((method, params))
        return _mock_response(200, {"result": True, "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        assert driver.cancel("hash123") is True
        remove_calls = [c for c in calls if c[0] == "core.remove_torrent"]
        assert len(remove_calls) == 1
        assert remove_calls[0][1] == ["hash123", True]


def test_deluge_cleanup_completed():
    driver = DelugeDriver("http://deluge.local:8112", password="pwd")
    calls = []

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        params = json.get("params", []) if json else []
        calls.append((method, params))
        return _mock_response(200, {"result": True, "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        # Keep files
        assert driver.cleanup_completed("hash123", delete_files=False) is True
        assert calls[-1][1] == ["hash123", False]

        # Delete files
        assert driver.cleanup_completed("hash123", delete_files=True) is True
        assert calls[-1][1] == ["hash123", True]


# ---------------------------------------------------------------------------
# Download Roots Tests
# ---------------------------------------------------------------------------
def test_deluge_roots():
    driver = DelugeDriver("http://deluge.local:8112", password="pwd", download_location="/override/path")

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        if method == "core.get_config_value":
            return _mock_response(200, {"result": "/data/downloads", "error": None})
        return _mock_response(200, {"result": True, "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        roots = driver.get_download_roots()
        assert roots == ["/data/downloads", "/override/path"]
        assert driver.last_roots_error is None


def test_deluge_roots_failure():
    driver = DelugeDriver("http://deluge.local:8112", password="pwd")
    with patch("httpx.Client.post", side_effect=httpx.ConnectError("down")):
        roots = driver.get_download_roots()
        assert roots == []
        assert driver.last_roots_error is not None


# ---------------------------------------------------------------------------
# Share Limits Tests
# ---------------------------------------------------------------------------
def test_deluge_set_share_limits():
    driver = DelugeDriver("http://deluge.local:8112", password="pwd")
    calls = []

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        params = json.get("params", []) if json else []
        calls.append((method, params))
        return _mock_response(200, {"result": True, "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        # Limit set
        assert driver.set_share_limits("hash123", ratio=2.0, seed_time_minutes=60) is True
        set_call = [c for c in calls if c[0] == "core.set_torrent_options"][-1]
        assert set_call[1] == [["hash123"], {"remove_at_ratio": False, "stop_at_ratio": True, "stop_ratio": 2.0}]

        # Limit None (don't stop)
        assert driver.set_share_limits("hash123", ratio=None, seed_time_minutes=None) is True
        set_call = [c for c in calls if c[0] == "core.set_torrent_options"][-1]
        assert set_call[1] == [["hash123"], {"remove_at_ratio": False, "stop_at_ratio": False}]


# ---------------------------------------------------------------------------
# List Category Tests
# ---------------------------------------------------------------------------
def test_deluge_list_category():
    driver = DelugeDriver("http://deluge.local:8112", password="pwd", label="trackseerr")

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        if method == "core.get_torrents_status":
            return _mock_response(
                200,
                {
                    "result": {
                        "h1": {
                            "name": "TrackSeerr Torrent",
                            "total_size": 1000,
                            "ratio": 1.5,
                            "seeding_time": 600,
                            "save_path": "/data/torrents",
                            "state": "Seeding",
                            "label": "trackseerr",
                        },
                        "h2": {
                            "name": "Other Torrent",
                            "label": "other",
                        },
                    },
                    "error": None,
                },
            )
        return _mock_response(200, {"result": True, "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        items = driver.list_category()
        assert items is not None
        assert len(items) == 1
        assert items[0]["hash"] == "h1"
        assert items[0]["name"] == "TrackSeerr Torrent"
        assert items[0]["content_path"] == "/data/torrents/TrackSeerr Torrent"


# ---------------------------------------------------------------------------
# Factory, Classification, and Route Test
# ---------------------------------------------------------------------------
def test_deluge_factory_and_classification():
    cfg = {
        "id": "c_deluge",
        "name": "Deluge Client",
        "driver_type": DownloadDriverType.DELUGE.value,
        "host_url": "http://deluge:8112",
        "password": "pwd",
        "extra_settings_json": '{"label": "music", "download_location": "/data/music"}',
    }
    driver = get_acquisition_driver(cfg)
    assert isinstance(driver, DelugeDriver)
    assert driver.label == "music"
    assert driver.download_location == "/data/music"
    assert is_torrent_driver_type(DownloadDriverType.DELUGE.value) is True


def test_route_create_deluge(client: TestClient, auth_headers: dict[str, str]):
    # Missing password fails 400
    bad_payload = {
        "name": "My Deluge",
        "driver_type": "deluge",
        "host_url": "http://192.168.1.50:8112",
    }
    resp = client.post("/api/settings/download-clients", json=bad_payload, headers=auth_headers)
    assert resp.status_code == 400
    assert "Password is required" in resp.json()["detail"]

    # With password succeeds
    payload = {
        "name": "My Deluge",
        "driver_type": "deluge",
        "host_url": "http://192.168.1.50:8112",
        "password": "secretpassword",
        "category": "trackseerr",
    }
    resp = client.post("/api/settings/download-clients", json=payload, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "My Deluge"
    assert data["driver_type"] == "deluge"
    assert "••••" in data["password"]
    assert "secretpassword" not in data["password"]

"""Tests for NZBGet Usenet Downloader Acquisition Driver."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
import httpx
import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.clients.acquisition import (
    get_acquisition_driver,
    is_torrent_driver_type,
)
from plex_playlist_sync.clients.acquisition.base import AcquisitionRetryableError
from plex_playlist_sync.clients.acquisition.nzbget import NzbgetDriver
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import (
    AcquisitionSearchResult,
    DownloadDriverType,
    DownloadStatus,
)
from plex_playlist_sync.storage import Database


def _mock_response(status_code: int = 200, payload: Any = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.text = str(payload or "")
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
def test_nzbget_test_connection_success():
    driver = NzbgetDriver("http://nzbget.local:6789", username="nzbget", password="tegbzn678")
    resp = _mock_response(200, {"version": "1.1", "result": "24.0", "error": None})

    with patch("httpx.Client.post", return_value=resp):
        ok, msg = driver.test_connection()
        assert ok is True
        assert "24.0" in msg


def test_nzbget_test_connection_auth_failure():
    driver = NzbgetDriver("http://nzbget.local:6789", username="wrong", password="pwd")
    resp = _mock_response(401, {"error": "Unauthorized"})

    with patch("httpx.Client.post", return_value=resp):
        ok, msg = driver.test_connection()
        assert ok is False
        assert "Authentication failed" in msg


def test_nzbget_ssrf_rejection():
    driver = NzbgetDriver("http://169.254.169.254/latest/meta-data")
    ok, msg = driver.test_connection()
    assert ok is False
    assert "SSRF" in msg

    item = AcquisitionSearchResult(
        download_id="nzb1",
        title="Album",
        artist="Artist",
        download_url="https://indexer.org/getnzb.nzb",
    )
    with pytest.raises(ValueError, match="Prohibited host URL"):
        driver.download(item)


# ---------------------------------------------------------------------------
# Download submission (append)
# ---------------------------------------------------------------------------
def test_nzbget_download():
    driver = NzbgetDriver("http://nzbget.local:6789", username="user", password="pwd", category="music")
    resp = _mock_response(200, {"result": 42, "error": None})

    item = AcquisitionSearchResult(
        download_id="orig_id",
        title="Random Access Memories",
        artist="Daft Punk",
        download_url="https://indexer.org/getnzb/123.nzb",
    )

    with patch("httpx.Client.post", return_value=resp) as mock_post:
        dl_id = driver.download(item)
        assert dl_id == "42"
        payload = mock_post.call_args.kwargs["json"]
        assert payload["method"] == "append"
        assert payload["params"][0] == "Daft Punk - Random Access Memories.nzb"
        assert payload["params"][1] == "https://indexer.org/getnzb/123.nzb"
        assert payload["params"][2] == "music"
        assert payload["params"][3] == 0


def test_nzbget_download_network_error_raises_retryable():
    driver = NzbgetDriver("http://nzbget.local:6789", username="user", password="pwd")
    item = AcquisitionSearchResult(
        download_id="orig_id",
        title="Album",
        artist="Artist",
        download_url="https://indexer.org/getnzb/123.nzb",
    )

    with patch("httpx.Client.post", side_effect=httpx.ConnectError("down")):
        with pytest.raises(AcquisitionRetryableError, match="Network error connecting to NZBGet"):
            driver.download(item)


# ---------------------------------------------------------------------------
# Status Mapping Tests
# ---------------------------------------------------------------------------
def test_nzbget_status_mapping_queue_downloading():
    driver = NzbgetDriver("http://nzbget.local:6789", username="user", password="pwd")

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        if method == "listgroups":
            return _mock_response(
                200,
                {
                    "result": [
                        {
                            "NZBID": 42,
                            "NZBName": "Daft Punk - RAM",
                            "Status": "DOWNLOADING",
                            "FileSizeMB": 500,
                            "RemainingSizeMB": 200,
                            "DownloadRate": 1500000,
                        }
                    ],
                    "error": None,
                },
            )
        return _mock_response(200, {"result": [], "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        st = driver.get_status("42")
        assert st["status"] == DownloadStatus.DOWNLOADING.value
        assert st["progress"] == 60.0
        assert st["size_bytes"] == 500 * 1024 * 1024
        assert st["speed_bps"] == 1500000
        assert st["eta_seconds"] > 0


def test_nzbget_status_mapping_queue_paused():
    driver = NzbgetDriver("http://nzbget.local:6789", username="user", password="pwd")

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        if method == "listgroups":
            return _mock_response(
                200,
                {
                    "result": [
                        {
                            "NZBID": 42,
                            "Status": "PAUSED",
                            "FileSizeMB": 100,
                            "RemainingSizeMB": 50,
                            "DownloadRate": 0,
                        }
                    ],
                    "error": None,
                },
            )
        return _mock_response(200, {"result": [], "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        st = driver.get_status("42")
        assert st["status"] == DownloadStatus.QUEUED.value
        assert st["progress"] == 50.0


def test_nzbget_status_mapping_history_success():
    driver = NzbgetDriver("http://nzbget.local:6789", username="user", password="pwd")

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        if method == "listgroups":
            return _mock_response(200, {"result": [], "error": None})
        elif method == "history":
            return _mock_response(
                200,
                {
                    "result": [
                        {
                            "NZBID": 42,
                            "Name": "Daft Punk - RAM",
                            "Status": "SUCCESS/ALL",
                            "FileSizeMB": 500,
                            "DestDir": "/data/complete/Daft Punk - RAM",
                        }
                    ],
                    "error": None,
                },
            )
        return _mock_response(200, {"result": [], "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        st = driver.get_status("42")
        assert st["status"] == DownloadStatus.COMPLETED.value
        assert st["progress"] == 100.0
        assert st["source_path"] == "/data/complete/Daft Punk - RAM"


@pytest.mark.parametrize("fail_status", ["FAILURE/PAR", "FAILURE/UNPACK", "FAILURE/HEALTH"])
def test_nzbget_status_mapping_history_failure(fail_status):
    driver = NzbgetDriver("http://nzbget.local:6789", username="user", password="pwd")

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        if method == "listgroups":
            return _mock_response(200, {"result": [], "error": None})
        elif method == "history":
            return _mock_response(
                200,
                {
                    "result": [
                        {
                            "NZBID": 42,
                            "Name": "Failed Album",
                            "Status": fail_status,
                            "FileSizeMB": 500,
                            "DestDir": "",
                        }
                    ],
                    "error": None,
                },
            )
        return _mock_response(200, {"result": [], "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        st = driver.get_status("42")
        assert st["status"] == DownloadStatus.FAILED.value
        assert st["error_message"] == fail_status


def test_nzbget_status_not_found():
    driver = NzbgetDriver("http://nzbget.local:6789", username="user", password="pwd")

    def _rpc_handler(url, json=None, **kwargs):
        return _mock_response(200, {"result": [], "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        st = driver.get_status("999")
        assert st["status"] == DownloadStatus.QUEUED.value
        assert st["progress"] == 0.0


# ---------------------------------------------------------------------------
# Cancel & Cleanup Tests
# ---------------------------------------------------------------------------
def test_nzbget_cancel():
    driver = NzbgetDriver("http://nzbget.local:6789", username="user", password="pwd")
    calls = []

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        params = json.get("params", []) if json else []
        calls.append((method, params))
        return _mock_response(200, {"result": True, "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        assert driver.cancel("42") is True
        cmds = [c[1][0] for c in calls if c[0] == "editqueue"]
        assert "GroupDelete" in cmds
        assert "HistoryDelete" in cmds


def test_nzbget_cleanup_completed():
    driver = NzbgetDriver("http://nzbget.local:6789", username="user", password="pwd")
    calls = []

    def _rpc_handler(url, json=None, **kwargs):
        method = json.get("method") if json else ""
        params = json.get("params", []) if json else []
        calls.append((method, params))
        return _mock_response(200, {"result": True, "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        # Keep files
        assert driver.cleanup_completed("42", delete_files=False) is True
        assert calls[-1][1][0] == "HistoryDelete"

        # Delete files
        assert driver.cleanup_completed("42", delete_files=True) is True
        assert calls[-1][1][0] == "HistoryFinalDelete"


# ---------------------------------------------------------------------------
# Download Roots Tests
# ---------------------------------------------------------------------------
def test_nzbget_roots():
    driver = NzbgetDriver("http://nzbget.local:6789", username="user", password="pwd", category="music")
    cfg_list = [
        {"Name": "DestDir", "Value": "/data/usenet/complete"},
        {"Name": "Category1.Name", "Value": "music"},
        {"Name": "Category1.DestDir", "Value": "music"},
        {"Name": "Category2.Name", "Value": "movies"},
        {"Name": "Category2.DestDir", "Value": "/data/movies"},
    ]

    def _rpc_handler(url, json=None, **kwargs):
        return _mock_response(200, {"result": cfg_list, "error": None})

    with patch("httpx.Client.post", side_effect=_rpc_handler):
        roots = driver.get_download_roots()
        assert roots == ["/data/usenet/complete", "/data/usenet/complete/music"]
        assert driver.last_roots_error is None


def test_nzbget_roots_failure():
    driver = NzbgetDriver("http://nzbget.local:6789", username="user", password="pwd")
    with patch("httpx.Client.post", side_effect=httpx.ConnectError("down")):
        roots = driver.get_download_roots()
        assert roots == []
        assert driver.last_roots_error is not None


# ---------------------------------------------------------------------------
# Factory, Classification, and Route Test
# ---------------------------------------------------------------------------
def test_nzbget_factory_and_classification():
    cfg = {
        "id": "c_nzb",
        "name": "NZBGet Client",
        "driver_type": DownloadDriverType.NZBGET.value,
        "host_url": "http://nzbget:6789",
        "username": "nzbget",
        "password": "pwd",
        "extra_settings_json": '{"category": "music"}',
    }
    driver = get_acquisition_driver(cfg)
    assert isinstance(driver, NzbgetDriver)
    assert driver.category == "music"
    assert is_torrent_driver_type(DownloadDriverType.NZBGET.value) is False


def test_route_create_nzbget(client: TestClient, auth_headers: dict[str, str]):
    # Missing username or password fails 400
    bad_payload = {
        "name": "My NZBGet",
        "driver_type": "nzbget",
        "host_url": "http://192.168.1.50:6789",
        "username": "admin",
    }
    resp = client.post("/api/settings/download-clients", json=bad_payload, headers=auth_headers)
    assert resp.status_code == 400
    assert "Username and password are required" in resp.json()["detail"]

    # With credentials succeeds
    payload = {
        "name": "My NZBGet",
        "driver_type": "nzbget",
        "host_url": "http://192.168.1.50:6789",
        "username": "nzbget",
        "password": "secretpassword",
        "category": "music",
    }
    resp = client.post("/api/settings/download-clients", json=payload, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "My NZBGet"
    assert data["driver_type"] == "nzbget"
    assert "••••" in data["password"]
    assert "secretpassword" not in data["password"]

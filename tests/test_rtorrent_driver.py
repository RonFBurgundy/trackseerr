"""Tests for the rTorrent (XML-RPC) acquisition driver."""

import xmlrpc.client
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.bencode import info_hash
from trackseerr.clients.acquisition import (
    get_acquisition_driver,
    is_torrent_driver_type,
)
from trackseerr.clients.acquisition.base import (
    AcquisitionRetryableError,
    AcquisitionUnavailableError,
)
from trackseerr.clients.acquisition.rtorrent import RtorrentDriver
from trackseerr.config import Config
from trackseerr.models import (
    AcquisitionSearchResult,
    DownloadDriverType,
    DownloadStatus,
)
from trackseerr.storage import Database

HOST = "http://rtorrent.local"
HASH = "ab" * 20
TORRENT = b"d4:infod6:lengthi1e4:name1:a12:piece lengthi16384e6:pieces20:" + b"x" * 20 + b"ee"


def _ok(value: Any) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = xmlrpc.client.dumps((value,), methodresponse=True)
    return resp


def _fault(code: int = -501, text: str = "Could not find info-hash.") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = xmlrpc.client.dumps(xmlrpc.client.Fault(code, text))
    return resp


def _status_reply(**overrides: Any) -> MagicMock:
    base: dict[str, Any] = {
        "d.name": "Album",
        "d.size_bytes": 1000,
        "d.completed_bytes": 250,
        "d.down.rate": 50,
        "d.complete": 0,
        "d.state": 1,
        "d.is_active": 1,
        "d.hashing": 0,
        "d.message": "",
        "d.base_path": "/dl/Album",
        "d.directory": "/dl",
        "d.is_multi_file": 1,
        "d.ratio": 1500,
        "d.timestamp.finished": 0,
    }
    base.update(overrides)
    order = list(base)
    return _ok([[base[k]] for k in order])


def _body(mock_post: MagicMock, index: int = -1) -> str:
    return mock_post.call_args_list[index].kwargs["content"]


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


def test_rtorrent_test_connection_success():
    driver = RtorrentDriver(HOST, username="u", password="p")
    with patch("httpx.Client.post", return_value=_ok("0.9.8")):
        ok, msg = driver.test_connection()
    assert ok is True
    assert "0.9.8" in msg


def test_rtorrent_test_connection_auth_failure():
    driver = RtorrentDriver(HOST, username="u", password="bad")
    resp = MagicMock()
    resp.status_code = 401
    with patch("httpx.Client.post", return_value=resp):
        ok, msg = driver.test_connection()
    assert ok is False
    assert "Authentication failed" in msg


def test_rtorrent_ssrf_rejection():
    driver = RtorrentDriver("http://169.254.169.254/latest/meta-data")
    ok, msg = driver.test_connection()
    assert ok is False
    assert "SSRF" in msg
    item = AcquisitionSearchResult(
        download_id="h", title="Album", artist="Artist", download_url="https://example.com/a.torrent"
    )
    with pytest.raises(ValueError, match="Prohibited host URL"):
        driver.download(item)


def test_rtorrent_download_torrent_bytes():
    driver = RtorrentDriver(HOST, download_dir="/data/music")
    item = AcquisitionSearchResult(
        download_id="x", title="Album", artist="Artist", download_url="https://indexer.example.com/a.torrent"
    )
    with (
        patch("trackseerr.clients.acquisition.torznab.fetch_torrent_file", return_value=TORRENT),
        patch("httpx.Client.post", side_effect=[_fault(), _ok(0)]) as mock_post,
    ):
        result = driver.download(item)
    assert result == info_hash(TORRENT)
    body = _body(mock_post)
    assert "load.raw_start" in body
    assert "d.custom1.set=trackseerr" in body
    assert "d.directory.set=/data/music" in body


def test_rtorrent_partial_download_sets_priorities():
    driver = RtorrentDriver(HOST, download_dir="/data/music")
    item = AcquisitionSearchResult(
        download_id="x",
        title="Album",
        artist="Artist",
        download_url="https://indexer.example.com/a.torrent",
        extra={"file_indices": [1], "file_paths": ["Album/02 - Track.flac"]},
    )
    thash = info_hash(TORRENT).upper()
    file_rows = [["01 - Intro.flac"], ["02 - Track.flac"], ["03 - Outro.flac"]]
    replies = [
        _fault(),  # d.hash
        _ok(0),  # load.raw
        _ok(file_rows),  # f.multicall
        _ok(0),  # f.priority.set f0
        _ok(0),  # f.priority.set f1
        _ok(0),  # f.priority.set f2
        _ok(0),  # d.update_priorities
        _ok(0),  # d.start
    ]
    with (
        patch("trackseerr.clients.acquisition.torznab.fetch_torrent_file", return_value=TORRENT),
        patch("httpx.Client.post", side_effect=replies) as mock_post,
    ):
        result = driver.download(item)

    assert result == info_hash(TORRENT)
    calls = [xmlrpc.client.loads(c.kwargs["content"]) for c in mock_post.call_args_list]
    methods = [m for _p, m in calls]
    assert methods == [
        "d.hash",
        "load.raw",
        "f.multicall",
        "f.priority.set",
        "f.priority.set",
        "f.priority.set",
        "d.update_priorities",
        "d.start",
    ]
    assert "load.raw_start" not in methods
    prio_params = [p for p, m in calls if m == "f.priority.set"]
    assert prio_params == [
        (f"{thash}:f0", 0),
        (f"{thash}:f1", 1),
        (f"{thash}:f2", 0),
    ]
    assert calls[6] == ((thash,), "d.update_priorities")
    assert calls[7] == ((thash,), "d.start")


def test_rtorrent_partial_download_magnet_is_unavailable():
    driver = RtorrentDriver(HOST)
    magnet = f"magnet:?xt=urn:btih:{HASH.upper()}&dn=Album"
    item = AcquisitionSearchResult(
        download_id="x",
        title="Album",
        artist="Artist",
        magnet_url=magnet,
        extra={"file_indices": [0], "file_paths": ["01 - Track.flac"]},
    )
    with pytest.raises(AcquisitionUnavailableError, match="needs the .torrent file") as exc_info:
        driver.download(item)
    assert exc_info.value.reason == "release_unavailable"


def test_rtorrent_partial_download_path_mismatch_erases():
    driver = RtorrentDriver(HOST)
    item = AcquisitionSearchResult(
        download_id="x",
        title="Album",
        artist="Artist",
        download_url="https://indexer.example.com/a.torrent",
        extra={"file_indices": [1], "file_paths": ["Album/99 - Wrong.flac"]},
    )
    thash = info_hash(TORRENT).upper()
    file_rows = [["01 - Intro.flac"], ["02 - Track.flac"], ["03 - Outro.flac"]]
    replies = [
        _fault(),  # d.hash
        _ok(0),  # load.raw
        _ok(file_rows),  # f.multicall
        _ok(0),  # d.erase
    ]
    with (
        patch("trackseerr.clients.acquisition.torznab.fetch_torrent_file", return_value=TORRENT),
        patch("httpx.Client.post", side_effect=replies) as mock_post,
        pytest.raises(AcquisitionUnavailableError) as exc_info,
    ):
        driver.download(item)
    assert exc_info.value.reason == "release_unavailable"
    calls = [xmlrpc.client.loads(c.kwargs["content"]) for c in mock_post.call_args_list]
    assert calls[-1] == ((thash,), "d.erase")


def test_rtorrent_full_download_unchanged():
    driver = RtorrentDriver(HOST, download_dir="/data/music")
    item = AcquisitionSearchResult(
        download_id="x",
        title="Album",
        artist="Artist",
        download_url="https://indexer.example.com/a.torrent",
        extra={},
    )
    with (
        patch("trackseerr.clients.acquisition.torznab.fetch_torrent_file", return_value=TORRENT),
        patch("httpx.Client.post", side_effect=[_fault(), _ok(0)]) as mock_post,
    ):
        result = driver.download(item)
    assert result == info_hash(TORRENT)
    body = _body(mock_post)
    assert "load.raw_start" in body


def test_rtorrent_download_magnet():
    driver = RtorrentDriver(HOST)
    magnet = f"magnet:?xt=urn:btih:{HASH.upper()}&dn=Album"
    item = AcquisitionSearchResult(download_id="x", title="Album", artist="Artist", magnet_url=magnet)
    with patch("httpx.Client.post", side_effect=[_fault(), _ok(0)]) as mock_post:
        assert driver.download(item) == HASH
    body = _body(mock_post)
    assert "load.start" in body
    assert "urn:btih:" in body


def test_rtorrent_download_existing_skips_add():
    driver = RtorrentDriver(HOST)
    item = AcquisitionSearchResult(
        download_id="x", title="Album", artist="Artist", magnet_url=f"magnet:?xt=urn:btih:{HASH}"
    )
    with patch("httpx.Client.post", return_value=_ok(HASH.upper())) as mock_post:
        assert driver.download(item) == HASH
    assert mock_post.call_count == 1
    assert "d.hash" in _body(mock_post)
    assert "load." not in _body(mock_post)


def test_rtorrent_download_network_error_raises_retryable():
    driver = RtorrentDriver(HOST)
    item = AcquisitionSearchResult(
        download_id="x", title="Album", artist="Artist", magnet_url=f"magnet:?xt=urn:btih:{HASH}"
    )
    with patch("httpx.Client.post", side_effect=httpx.ConnectError("down")):
        with pytest.raises(AcquisitionRetryableError):
            driver.download(item)


@pytest.mark.parametrize(
    ("overrides", "status", "progress", "error"),
    [
        ({"d.hashing": 1}, DownloadStatus.QUEUED.value, 25.0, None),
        ({"d.complete": 1, "d.completed_bytes": 1000}, DownloadStatus.COMPLETED.value, 100.0, None),
        ({}, DownloadStatus.DOWNLOADING.value, 25.0, None),
        (
            {"d.state": 0, "d.is_active": 0, "d.message": "Tracker: unregistered"},
            DownloadStatus.QUEUED.value,
            25.0,
            "Tracker: unregistered",
        ),
        ({"d.message": "Tracker: timeout"}, DownloadStatus.DOWNLOADING.value, 25.0, None),
    ],
)
def test_rtorrent_status_mapping(overrides: dict[str, Any], status: str, progress: float, error: Any):
    driver = RtorrentDriver(HOST)
    with patch("httpx.Client.post", return_value=_status_reply(**overrides)):
        res = driver.get_status(HASH)
    assert res["status"] == status
    assert res["progress"] == progress
    assert res["error_message"] == error
    assert res["ratio"] == 1.5


def test_rtorrent_status_eta_and_speed():
    driver = RtorrentDriver(HOST)
    with patch("httpx.Client.post", return_value=_status_reply()):
        res = driver.get_status(HASH)
    assert res["speed_bps"] == 50
    assert res["eta_seconds"] == 15
    assert res["size_bytes"] == 1000


def test_rtorrent_status_not_loaded():
    driver = RtorrentDriver(HOST)
    faults = [{"faultCode": -501, "faultString": "Could not find info-hash."}] * 14
    with patch("httpx.Client.post", return_value=_ok(faults)):
        res = driver.get_status(HASH)
    assert res["status"] == DownloadStatus.QUEUED.value
    assert res["progress"] == 0.0
    assert res["error_message"] is None


def test_rtorrent_status_error_returns_failed():
    driver = RtorrentDriver(HOST)
    with patch("httpx.Client.post", side_effect=httpx.ConnectError("down")):
        res = driver.get_status(HASH)
    assert res["status"] == DownloadStatus.FAILED.value
    assert "down" in res["error_message"]


def test_rtorrent_status_content_path():
    driver = RtorrentDriver(HOST)
    with patch("httpx.Client.post", return_value=_status_reply(**{"d.base_path": "/moved/Album"})):
        res = driver.get_status(HASH)
    assert res["content_path"] == "/moved/Album"
    assert res["save_path"] == "/dl"

    single = _status_reply(**{"d.base_path": "", "d.is_multi_file": 0, "d.name": "a.flac"})
    with patch("httpx.Client.post", return_value=single):
        res = driver.get_status(HASH)
    assert res["content_path"] == "/dl/a.flac"
    assert res["save_path"] == "/dl"

    multi = _status_reply(**{"d.base_path": "", "d.is_multi_file": 1})
    with patch("httpx.Client.post", return_value=multi):
        res = driver.get_status(HASH)
    assert res["content_path"] == "/dl"
    assert res["save_path"] == "/dl"


def test_rtorrent_cancel():
    driver = RtorrentDriver(HOST)
    with patch("httpx.Client.post", return_value=_ok(0)) as mock_post:
        assert driver.cancel(HASH) is True
    body = _body(mock_post)
    assert "d.erase" in body
    assert HASH.upper() in body

    with patch("httpx.Client.post", side_effect=httpx.ConnectError("down")):
        assert driver.cancel(HASH) is False


def test_rtorrent_cleanup_completed():
    driver = RtorrentDriver(HOST)
    with patch("httpx.Client.post", return_value=_ok(0)) as mock_post:
        assert driver.cleanup_completed(HASH, delete_files=False) is True
        assert driver.cleanup_completed(HASH, delete_files=True) is True
    body = _body(mock_post)
    assert "d.erase" in body
    assert HASH.upper() in body


def test_rtorrent_roots():
    driver = RtorrentDriver(HOST, download_dir="/override/dir")
    with patch("httpx.Client.post", return_value=_ok("/data/downloads")):
        assert driver.get_download_roots() == ["/data/downloads", "/override/dir"]
    assert driver.last_roots_error is None


def test_rtorrent_roots_failure():
    driver = RtorrentDriver(HOST)
    with patch("httpx.Client.post", side_effect=httpx.ConnectError("down")):
        assert driver.get_download_roots() == []
    assert "down" in (driver.last_roots_error or "")


def test_rtorrent_roots_empty():
    driver = RtorrentDriver(HOST)
    with patch("httpx.Client.post", return_value=_ok("")):
        assert driver.get_download_roots() == []
    assert driver.last_roots_error == "rTorrent reported no default directory"


def test_rtorrent_invalid_reply_is_runtime_error():
    driver = RtorrentDriver(HOST)
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "<html>not xml-rpc"
    with patch("httpx.Client.post", return_value=resp):
        ok, msg = driver.test_connection()
    assert ok is False
    assert "invalid XML-RPC" in msg


def test_rtorrent_list_category():
    driver = RtorrentDriver(HOST, category="trackseerr")
    rows = [
        [HASH.upper(), "TrackSeerr Album", 1000, 1500, 0, "/dl/TrackSeerr Album", "/dl", "trackseerr", 1, 1],
        ["CD" * 20, "Other", 5, 0, 0, "/dl/Other", "/dl", "other", 1, 0],
    ]
    with patch("httpx.Client.post", return_value=_ok(rows)):
        items = driver.list_category()
    assert items is not None
    assert len(items) == 1
    assert items[0]["hash"] == HASH
    assert items[0]["ratio"] == 1.5
    assert items[0]["state"] == "seeding"
    assert items[0]["content_path"] == "/dl/TrackSeerr Album"

    assert RtorrentDriver(HOST, category="").list_category() is None


def test_rtorrent_factory_and_classification():
    cfg = {
        "id": "c_rt",
        "name": "rTorrent",
        "driver_type": DownloadDriverType.RTORRENT.value,
        "host_url": "http://rtorrent:8080",
        "username": "user",
        "password": "pwd",
        "extra_settings_json": '{"rpc_path": "/custom/RPC2", "category": "music"}',
    }
    driver = get_acquisition_driver(cfg)
    assert isinstance(driver, RtorrentDriver)
    assert driver.rpc_path == "/custom/RPC2"
    assert driver.category == "music"
    assert is_torrent_driver_type("rtorrent") is True


def test_route_create_rtorrent(client: TestClient, auth_headers: dict[str, str]):
    payload = {
        "name": "My rTorrent",
        "driver_type": "rtorrent",
        "host_url": "http://192.168.1.50:8080",
        "username": "user",
        "password": "secretpassword",
        "category": "trackseerr",
    }
    resp = client.post("/api/settings/download-clients", json=payload, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["driver_type"] == "rtorrent"
    assert "secretpassword" not in data["password"]

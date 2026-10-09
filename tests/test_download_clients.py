"""Integration and route tests for all download client types."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.models import DownloadDriverType
from trackseerr.clients.acquisition import (
    get_acquisition_driver,
    is_torrent_driver_type,
    TransmissionDriver,
    DelugeDriver,
    NzbgetDriver,
    QbittorrentDriver,
    SabnzbdDriver,
    SlskdDriver,
)
from trackseerr.acquisition_coordinator import AcquisitionCoordinator
from trackseerr.config import Config
from trackseerr.storage import Database


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
    token = create_session_token(
        user_id=admin["id"],
        username=admin["username"],
        is_admin=admin["is_admin"],
        secret_key=secret,
    )
    db.create_session(token, admin["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def test_download_client_types_classification():
    assert is_torrent_driver_type(DownloadDriverType.TRANSMISSION) is True
    assert is_torrent_driver_type(DownloadDriverType.DELUGE) is True
    assert is_torrent_driver_type(DownloadDriverType.QBITTORRENT) is True
    assert is_torrent_driver_type(DownloadDriverType.NZBGET) is False
    assert is_torrent_driver_type(DownloadDriverType.SABNZBD) is False
    assert is_torrent_driver_type(DownloadDriverType.SLSKD) is False

    assert is_torrent_driver_type("transmission") is True
    assert is_torrent_driver_type("deluge") is True
    assert is_torrent_driver_type("nzbget") is False


def test_download_client_factory():
    c_trans = {
        "driver_type": "transmission",
        "host_url": "http://192.168.1.50:9091",
        "username": "admin",
        "password": "secret",
        "extra_settings_json": '{"rpc_path": "/custom/rpc", "category": "music"}',
    }
    driver = get_acquisition_driver(c_trans)
    assert isinstance(driver, TransmissionDriver)
    assert driver.rpc_path == "/custom/rpc"
    assert driver.category == "music"

    c_deluge = {
        "driver_type": "deluge",
        "host_url": "http://192.168.1.50:8112",
        "password": "deluge_pass",
        "extra_settings_json": '{"category": "music"}',
    }
    driver = get_acquisition_driver(c_deluge)
    assert isinstance(driver, DelugeDriver)
    assert driver.category == "music"

    c_nzb = {
        "driver_type": "nzbget",
        "host_url": "http://192.168.1.50:6789",
        "username": "nzbuser",
        "password": "nzbpass",
        "extra_settings_json": '{"category": "music"}',
    }
    driver = get_acquisition_driver(c_nzb)
    assert isinstance(driver, NzbgetDriver)
    assert driver.category == "music"


def test_protocol_coordinator_routing():
    clients = [
        {"id": "c1", "driver_type": "slskd", "enabled": True, "priority": 1, "created_at": "2026-01-01"},
        {"id": "c2", "driver_type": "transmission", "enabled": True, "priority": 2, "created_at": "2026-01-01"},
        {"id": "c3", "driver_type": "nzbget", "enabled": True, "priority": 3, "created_at": "2026-01-01"},
        {"id": "c4", "driver_type": "deluge", "enabled": True, "priority": 4, "created_at": "2026-01-01"},
        {"id": "c5", "driver_type": "sabnzbd", "enabled": True, "priority": 5, "created_at": "2026-01-01"},
    ]
    db_mock = MagicMock()
    db_mock.list_download_clients.return_value = clients

    coordinator = AcquisitionCoordinator()

    torrent_match = coordinator.find_client_for_protocol("torrent", db_mock)
    assert torrent_match is not None
    assert torrent_match["driver_type"] == "transmission"

    torznab_match = coordinator.find_client_for_protocol("torznab", db_mock)
    assert torznab_match is not None
    assert torznab_match["driver_type"] == "transmission"

    usenet_match = coordinator.find_client_for_protocol("usenet", db_mock)
    assert usenet_match is not None
    assert usenet_match["driver_type"] == "nzbget"

    newznab_match = coordinator.find_client_for_protocol("newznab", db_mock)
    assert newznab_match is not None
    assert newznab_match["driver_type"] == "nzbget"

    slskd_match = coordinator.find_client_for_protocol("slskd", db_mock)
    assert slskd_match is not None
    assert slskd_match["driver_type"] == "slskd"


def test_create_and_test_routes_all_client_types(client: TestClient, auth_headers: dict[str, str]):
    # 1. Deluge requires password
    deluge_bad = {
        "name": "Deluge No Pass",
        "driver_type": "deluge",
        "host_url": "http://192.168.1.50:8112",
    }
    res = client.post("/api/settings/download-clients", json=deluge_bad, headers=auth_headers)
    assert res.status_code == 400
    assert "password" in res.json()["detail"].lower()

    # 2. Deluge success
    deluge_good = {
        "name": "My Deluge",
        "driver_type": "deluge",
        "host_url": "http://192.168.1.50:8112",
        "password": "delugepassword",
        "extra_settings_json": '{"category": "music"}',
    }
    res = client.post("/api/settings/download-clients", json=deluge_good, headers=auth_headers)
    assert res.status_code == 200
    d_id = res.json()["id"]

    # 3. NZBGet requires username and password
    nzbget_no_user = {
        "name": "NZBGet No User",
        "driver_type": "nzbget",
        "host_url": "http://192.168.1.50:6789",
        "password": "pass",
    }
    res = client.post("/api/settings/download-clients", json=nzbget_no_user, headers=auth_headers)
    assert res.status_code == 400
    assert "username" in res.json()["detail"].lower()

    nzbget_no_pass = {
        "name": "NZBGet No Pass",
        "driver_type": "nzbget",
        "host_url": "http://192.168.1.50:6789",
        "username": "user",
    }
    res = client.post("/api/settings/download-clients", json=nzbget_no_pass, headers=auth_headers)
    assert res.status_code == 400
    assert "password" in res.json()["detail"].lower()

    # 4. NZBGet success
    nzbget_good = {
        "name": "My NZBGet",
        "driver_type": "nzbget",
        "host_url": "http://192.168.1.50:6789",
        "username": "nzbuser",
        "password": "nzbpassword",
        "extra_settings_json": '{"category": "music"}',
    }
    res = client.post("/api/settings/download-clients", json=nzbget_good, headers=auth_headers)
    assert res.status_code == 200
    n_id = res.json()["id"]

    # 5. Transmission success (credentials optional)
    trans_good = {
        "name": "My Transmission",
        "driver_type": "transmission",
        "host_url": "http://192.168.1.50:9091",
        "extra_settings_json": '{"rpc_path": "/transmission/rpc"}',
    }
    res = client.post("/api/settings/download-clients", json=trans_good, headers=auth_headers)
    assert res.status_code == 200
    t_id = res.json()["id"]

    # 6. List and verify masking
    res = client.get("/api/settings/download-clients", headers=auth_headers)
    assert res.status_code == 200
    clients_list = res.json()
    assert len(clients_list) >= 3

    for item in clients_list:
        if item["id"] == d_id:
            assert "delugepassword" not in item["password"]
            assert "••••" in item["password"]
        elif item["id"] == n_id:
            assert item["username"] == "nzbuser"
            assert "nzbpassword" not in item["password"]
            assert "••••" in item["password"]

    # 7. Test connection endpoint validations
    with patch("trackseerr.api.routes.download_clients.get_acquisition_driver") as mock_factory:
        mock_driver = MagicMock()
        mock_driver.test_connection.return_value = (True, "Connected ok")
        mock_factory.return_value = mock_driver

        # Transmission test
        res = client.post("/api/settings/download-clients/test", json=trans_good, headers=auth_headers)
        assert res.status_code == 200
        assert res.json()["success"] is True

        # Deluge test without password fails validation
        res = client.post("/api/settings/download-clients/test", json=deluge_bad, headers=auth_headers)
        assert res.status_code == 200
        assert res.json()["success"] is False
        assert "password" in res.json()["message"].lower()

        # NZBGet test without user/pass fails validation
        res = client.post("/api/settings/download-clients/test", json=nzbget_no_pass, headers=auth_headers)
        assert res.status_code == 200
        assert res.json()["success"] is False
        assert "password" in res.json()["message"].lower()

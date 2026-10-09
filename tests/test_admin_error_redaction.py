"""Admin routes must not echo secrets embedded in exception text (responses or logs)."""

import logging
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.storage import Database

API_SECRET = "SECRET123"
PLEX_SECRET = "plexsecret456"
CRED_SECRET = "hunter2pw"
LEAKY = (
    f"HTTPConnectionPool: http://user:{CRED_SECRET}@host:9117/api?apikey={API_SECRET} failed "
    f"(X-Plex-Token={PLEX_SECRET})"
)
ALL_SECRETS = (API_SECRET, PLEX_SECRET, CRED_SECRET)


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def client_and_headers(db, tmp_path):
    config = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    admin = db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    token = create_session_token(
        user_id=admin["id"],
        username=admin["username"],
        is_admin=True,
        secret_key=get_or_create_secret_key(data_dir=config.data_dir),
    )
    db.create_session(token, admin["id"], {"auth": "test"})
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def _assert_clean(resp_text: str, caplog) -> None:
    for secret in ALL_SECRETS:
        assert secret not in resp_text
        assert secret not in caplog.text


def test_download_client_test_redacts(client_and_headers, caplog):
    client, headers = client_and_headers
    caplog.set_level(logging.DEBUG)
    with patch("trackseerr.api.routes.download_clients.is_safe_service_url", return_value=True), patch(
        "trackseerr.api.routes.download_clients.get_acquisition_driver", side_effect=RuntimeError(LEAKY)
    ):
        resp = client.post(
            "/api/settings/download-clients/test",
            headers=headers,
            json={"driver_type": "qbittorrent", "host_url": "http://dl.example"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["message"].startswith("Connection failed:")
    _assert_clean(resp.text, caplog)


def test_indexer_test_redacts(client_and_headers, caplog):
    client, headers = client_and_headers
    caplog.set_level(logging.DEBUG)
    with patch("trackseerr.api.routes.indexers.is_safe_service_url", return_value=True), patch(
        "trackseerr.api.routes.indexers.get_indexer_driver", side_effect=RuntimeError(LEAKY)
    ):
        resp = client.post(
            "/api/settings/indexers/test",
            headers=headers,
            json={"indexer_type": "torznab", "host_url": "http://idx.example"},
        )
    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert "REDACTED" in resp.json()["message"]
    _assert_clean(resp.text, caplog)


def test_lidarr_test_redacts(client_and_headers, caplog):
    client, headers = client_and_headers
    caplog.set_level(logging.DEBUG)
    with patch("trackseerr.api.routes.settings.is_safe_service_url", return_value=True), patch(
        "trackseerr.api.routes.settings.LidarrClient", side_effect=RuntimeError(LEAKY)
    ):
        resp = client.post(
            "/api/settings/lidarr/test",
            headers=headers,
            json={"url": "http://lidarr.example", "api_key": "realkey"},
        )
    assert resp.status_code == 200
    assert resp.json()["online"] is False
    _assert_clean(resp.text, caplog)


def test_lidarr_test_result_error_redacted(client_and_headers, caplog):
    client, headers = client_and_headers
    caplog.set_level(logging.DEBUG)

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def test_connection(self):
            return {"online": False, "error": LEAKY}

    with patch("trackseerr.api.routes.settings.is_safe_service_url", return_value=True), patch(
        "trackseerr.api.routes.settings.LidarrClient", FakeClient
    ):
        resp = client.post(
            "/api/settings/lidarr/test",
            headers=headers,
            json={"url": "http://lidarr.example", "api_key": "realkey"},
        )
    assert resp.status_code == 200
    _assert_clean(resp.text, caplog)


def test_general_settings_db_error_redacts(client_and_headers, db, caplog):
    client, headers = client_and_headers
    caplog.set_level(logging.DEBUG)
    with patch.object(db, "update_general_settings", side_effect=RuntimeError(LEAKY)):
        resp = client.post("/api/settings/general", headers=headers, json={"application_url": "https://x.example"})
    assert resp.status_code == 500
    assert resp.json()["detail"].startswith("Database update failed:")
    _assert_clean(resp.text, caplog)


def test_quality_profile_integrity_error_redacts(client_and_headers, db, caplog):
    import sqlite3

    client, headers = client_and_headers
    caplog.set_level(logging.DEBUG)
    with patch.object(db, "upsert_quality_profile", side_effect=sqlite3.IntegrityError(LEAKY)):
        resp = client.post(
            "/api/settings/quality-profiles",
            headers=headers,
            json={"name": "p", "cutoff": "FLAC", "items": []},
        )
    assert resp.status_code == 400
    assert "violates constraint" in resp.json()["detail"]
    _assert_clean(resp.text, caplog)

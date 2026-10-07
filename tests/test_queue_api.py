"""Integration tests for Activity Queue, Download Clients, and Indexers API endpoints."""

from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    IndexerConfig,
    MusicRequest,
)
from plex_playlist_sync.storage import Database


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        data_dir=str(tmp_path),
    )


@pytest.fixture
def seeded_users(test_db):
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice}


@pytest.fixture
def app_and_client(test_db, test_config):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    client = TestClient(app)
    return app, client


def _auth_headers(user: dict, test_db: Database, config: Config) -> dict[str, str]:
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
# Download Clients API Tests
# ---------------------------------------------------------------------------
def test_download_clients_permissions(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # Alice (non-admin) cannot list clients (403 Forbidden)
    resp = client.get("/api/settings/download-clients", headers=alice_headers)
    assert resp.status_code == 403

    # Admin can list clients
    resp_admin = client.get("/api/settings/download-clients", headers=admin_headers)
    assert resp_admin.status_code == 200

    # Alice cannot create or delete
    resp = client.post("/api/settings/download-clients", json={"name": "test"}, headers=alice_headers)
    assert resp.status_code == 403

    resp = client.delete("/api/settings/download-clients/c1", headers=alice_headers)
    assert resp.status_code == 403


def test_download_clients_crud(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # 1. Initially empty
    resp = client.get("/api/settings/download-clients", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json() == []

    # 2. Create client
    payload = {
        "name": "My Slskd",
        "driver_type": "slskd",
        "host_url": "http://192.168.1.100:5030",
        "username": "user",
        "password": "pwd",
        "priority": 2,
        "enabled": True,
    }
    resp = client.post("/api/settings/download-clients", json=payload, headers=admin_headers)
    assert resp.status_code == 200
    created = resp.json()
    assert created["id"] is not None
    assert created["name"] == "My Slskd"
    assert created["driver_type"] == "slskd"

    # 3. List contains created client
    resp = client.get("/api/settings/download-clients", headers=admin_headers)
    assert len(resp.json()) == 1

    # 4. Update client
    update_payload = {
        "id": created["id"],
        "name": "Updated Slskd",
        "driver_type": "slskd",
        "host_url": "http://192.168.1.100:5030",
        "priority": 1,
        "enabled": False,
    }
    resp = client.post("/api/settings/download-clients", json=update_payload, headers=admin_headers)
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["name"] == "Updated Slskd"
    assert updated["enabled"] is False

    # 5. Delete client
    resp = client.delete(f"/api/settings/download-clients/{created['id']}", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"

    # List is empty again
    resp = client.get("/api/settings/download-clients", headers=admin_headers)
    assert len(resp.json()) == 0


def test_download_clients_ssrf_rejection(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # Creating client with cloud metadata URL should fail 400
    bad_payload = {
        "name": "Evil Client",
        "driver_type": "slskd",
        "host_url": "http://169.254.169.254/latest/meta-data",
    }
    resp = client.post("/api/settings/download-clients", json=bad_payload, headers=admin_headers)
    assert resp.status_code == 400
    assert "SSRF" in resp.json()["detail"] or "Prohibited" in resp.json()["detail"]

    # Testing client with cloud metadata URL should return success=False
    test_payload = {
        "driver_type": "sabnzbd",
        "host_url": "http://metadata.google.internal/computeMetadata/v1/",
    }
    resp = client.post("/api/settings/download-clients/test", json=test_payload, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert "SSRF" in resp.json()["message"] or "Prohibited" in resp.json()["message"]


def test_download_clients_test_connection_endpoint(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    test_payload = {
        "driver_type": "slskd",
        "host_url": "http://192.168.1.50:5030",
        "username": "admin",
        "password": "pwd",
    }

    with patch("plex_playlist_sync.api.routes.download_clients.get_acquisition_driver") as mock_factory:
        mock_driver = MagicMock()
        mock_driver.test_connection.return_value = (True, "slskd 0.20.0 connected")
        mock_factory.return_value = mock_driver

        resp = client.post("/api/settings/download-clients/test", json=test_payload, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "0.20.0" in data["message"]


# ---------------------------------------------------------------------------
# Indexers API Tests
# ---------------------------------------------------------------------------
def test_indexers_crud_and_permissions(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # 1. Non-admin cannot list indexers (403 Forbidden)
    resp = client.get("/api/settings/indexers", headers=alice_headers)
    assert resp.status_code == 403

    # Admin can list indexers
    resp_admin = client.get("/api/settings/indexers", headers=admin_headers)
    assert resp_admin.status_code == 200

    # Non-admin cannot create or delete
    resp = client.post("/api/settings/indexers", json={"name": "test"}, headers=alice_headers)
    assert resp.status_code == 403

    resp = client.delete("/api/settings/indexers/idx-1", headers=alice_headers)
    assert resp.status_code == 403

    # 2. Admin create indexer
    payload = {
        "name": "Prowlarr Music",
        "indexer_type": "torznab",
        "host_url": "http://192.168.1.100:9696/1/api",
        "api_key": "secret",
        "categories": "3000,3020",
        "priority": 1,
        "enabled": True,
    }
    resp = client.post("/api/settings/indexers", json=payload, headers=admin_headers)
    assert resp.status_code == 200
    created = resp.json()
    assert created["id"] is not None
    assert created["name"] == "Prowlarr Music"

    # 3. List indexers
    resp = client.get("/api/settings/indexers", headers=admin_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    # 4. Delete indexer
    resp = client.delete(f"/api/settings/indexers/{created['id']}", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"


def test_indexers_ssrf_rejection(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    bad_payload = {
        "name": "SSRF Indexer",
        "indexer_type": "torznab",
        "host_url": "http://169.254.169.254/latest/meta-data",
    }
    resp = client.post("/api/settings/indexers", json=bad_payload, headers=admin_headers)
    assert resp.status_code == 400


def test_indexers_test_endpoint(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    test_payload = {
        "indexer_type": "torznab",
        "host_url": "http://prowlarr:9696/1/api",
        "api_key": "secret",
    }

    with patch("plex_playlist_sync.api.routes.indexers.get_indexer_driver") as mock_factory:
        mock_indexer = MagicMock()
        mock_indexer.test_connection.return_value = (True, "Torznab Indexer Online")
        mock_factory.return_value = mock_indexer

        resp = client.post("/api/settings/indexers/test", json=test_payload, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["success"] is True


# ---------------------------------------------------------------------------
# Queue API Tests
# ---------------------------------------------------------------------------
def test_queue_api(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # 1. Seed download client and active system download (no request_id)
    dl_client = test_db.create_download_client(
        DownloadClientConfig(
            id="client-slskd-1",
            name="Slskd Test",
            driver_type=DownloadDriverType.SLSKD,
            host_url="http://slskd:5030",
        )
    )

    download = test_db.create_active_download(
        ActiveDownload(
            id="dl-q-1",
            title="Daft Punk - One More Time",
            artist="Daft Punk",
            client_id="client-slskd-1",
            download_hash="slskd-dl-1",
            status=DownloadStatus.DOWNLOADING.value,
            progress=65.5,
            source_path="/downloads/staging/01.mp3",
            target_path="/music/Daft Punk/01.mp3",
        )
    )

    # 2. The queue is admin-only: a regular user is refused (no filtered view)
    resp_alice = client.get("/api/queue", headers=alice_headers)
    assert resp_alice.status_code == 403

    # Admin sees system download with sensitive filesystem paths intact
    resp_admin = client.get("/api/queue", headers=admin_headers)
    assert resp_admin.status_code == 200
    admin_items = resp_admin.json()
    assert len(admin_items) == 1
    assert admin_items[0]["title"] == "Daft Punk - One More Time"
    assert admin_items[0]["source_path"] == "/downloads/staging/01.mp3"
    assert admin_items[0]["target_path"] == "/music/Daft Punk/01.mp3"

    # 3. A download tied to Alice's own request is still not manageable by Alice
    test_db.create_request(
        MusicRequest(
            id="req-alice-1",
            user_id="user-alice",
            item_type="track",
            title="Alice Song",
            artist="Alice Artist",
        )
    )
    alice_dl = test_db.create_active_download(
        ActiveDownload(
            id="dl-alice-1",
            title="Alice Song",
            artist="Alice Artist",
            client_id="client-slskd-1",
            request_id="req-alice-1",
            download_hash="slskd-alice-1",
            status=DownloadStatus.DOWNLOADING.value,
            source_path="/downloads/staging/alice.flac",
            target_path="/music/Alice Artist/alice.flac",
        )
    )
    assert client.get("/api/queue", headers=alice_headers).status_code == 403

    # Admin sees both downloads
    resp_admin_all = client.get("/api/queue", headers=admin_headers)
    assert len(resp_admin_all.json()) == 2

    # 4. Cancellation is admin-only
    resp_cancel_sys = client.delete(f"/api/queue/{download['id']}", headers=alice_headers)
    assert resp_cancel_sys.status_code == 403
    resp_cancel_own = client.delete(f"/api/queue/{alice_dl['id']}", headers=alice_headers)
    assert resp_cancel_own.status_code == 403
    assert test_db.get_active_download(alice_dl["id"]) is not None

    with patch("plex_playlist_sync.api.routes.queue.get_acquisition_driver") as mock_factory:
        mock_driver = MagicMock()
        mock_driver.cancel.return_value = True
        mock_factory.return_value = mock_driver

        resp_admin_cancel = client.delete(f"/api/queue/{download['id']}", headers=admin_headers)
        assert resp_admin_cancel.status_code == 200
        assert resp_admin_cancel.json()["status"] == "cancelled"

        resp_admin_cancel_alice = client.delete(f"/api/queue/{alice_dl['id']}", headers=admin_headers)
        assert resp_admin_cancel_alice.status_code == 200


def test_download_client_masked_secret_round_trip(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    base = {"name": "C", "driver_type": "slskd", "host_url": "http://192.168.1.100:5030", "username": "u"}
    created = client.post("/api/settings/download-clients", json={**base, "password": "hunter2-secret-9999"}, headers=h).json()
    masked = created["password"]
    assert masked and masked != "hunter2-secret-9999"

    # Unchanged mask keeps the stored secret.
    r = client.post("/api/settings/download-clients", json={**base, "id": created["id"], "password": masked}, headers=h)
    assert r.status_code == 200, r.text
    assert test_db.get_download_client(created["id"])["password"] == "hunter2-secret-9999"

    # A half-edited placeholder is rejected, not silently reverted.
    r = client.post("/api/settings/download-clients", json={**base, "id": created["id"], "password": "x" + masked}, headers=h)
    assert r.status_code == 400
    assert test_db.get_download_client(created["id"])["password"] == "hunter2-secret-9999"

    # A new full value replaces it.
    r = client.post("/api/settings/download-clients", json={**base, "id": created["id"], "password": "brand-new-pw"}, headers=h)
    assert r.status_code == 200
    assert test_db.get_download_client(created["id"])["password"] == "brand-new-pw"

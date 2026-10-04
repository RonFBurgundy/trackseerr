"""Unit and integration tests for Media Management settings, permissions, and live preview API."""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.storage import Database


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    """Provides a test Config pointing to tmp_path."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        data_dir=str(tmp_path),
    )


@pytest.fixture
def seeded_users(test_db):
    """Seeds admin and regular users into test DB."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice}


@pytest.fixture
def app_and_client(test_db, test_config):
    """Creates a FastAPI test client with injected test database and config."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config

    client = TestClient(app)
    return app, client


def _auth_headers(user: dict, test_db: Database, config: Config) -> dict[str, str]:
    """Generates an authenticated Bearer header for a given user."""
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


class TestMediaManagementStorage:
    """Validates DB migration v7 and CRUD operations."""

    def test_default_settings_initialized(self, test_db):
        settings = test_db.get_media_management_settings()
        assert settings["artist_folder_format"] == "{Artist Name}"
        assert settings["album_folder_format"] == "{Album Title} ({Release Year}){[ - Album Type]}"
        # Lidarr-style: the track formats are full paths relative to the artist folder (album folder included)
        assert settings["standard_track_format"] == (
            "{Album Title} ({Release Year}){[ - Album Type]}/{track:00} - {Track Title}{[ (Quality Full)]}"
        )
        assert settings["multi_disc_track_format"] == (
            "{Album Title} ({Release Year}){[ - Album Type]}/{Medium Format} {medium:00}/"
            "{track:00} - {Track Title}{[ (Quality Full)]}"
        )
        assert settings["compilation_track_format"] == "{track:00} - {Artist Name} - {Track Title}{[ (Quality Full)]}"
        assert settings["multi_disc_folder_format"] == "{Medium Format} {medium:00}"
        assert settings["root_folder_path"] == "/data/media/music"
        assert settings["colon_replacement_format"] == " - "
        assert settings["clean_artist_names"] is True

    def test_update_settings_partial_and_full(self, test_db):
        updated = test_db.update_media_management_settings({
            "artist_folder_format": "{Artist CleanName}",
            "clean_artist_names": False,
            "colon_replacement_format": "_",
        })
        assert updated["artist_folder_format"] == "{Artist CleanName}"
        assert updated["clean_artist_names"] is False
        assert updated["colon_replacement_format"] == "_"
        # Unchanged fields remain intact
        assert updated["root_folder_path"] == "/data/media/music"

        # Verify persistence on subsequent fetch
        fetched = test_db.get_media_management_settings()
        assert fetched["artist_folder_format"] == "{Artist CleanName}"
        assert fetched["clean_artist_names"] is False

    def test_migration_v9(self, test_db):
        cursor = test_db.conn.cursor()
        cursor.execute("PRAGMA table_info(media_management_settings)")
        cols = {row[1]: row for row in cursor.fetchall()}
        assert "staging_folder_path" in cols
        assert "import_mode" in cols

        mm = test_db.get_media_management_settings()
        assert mm["staging_folder_path"] == "/data/downloads"
        assert mm["import_mode"] == "move"

        cursor.execute("SELECT * FROM lidarr_settings WHERE id = 1")
        row = dict(cursor.fetchone())
        assert row["auto_search"] == 1
        assert float(row["trickle_rate_seconds"]) == 3.0
        assert row["trickle_batch_size"] == 25
        assert row["auto_trickle"] == 0
        assert row["auto_trickle_interval_minutes"] == 30

    def test_get_and_update_lidarr_settings(self, test_db):
        defaults = test_db.get_lidarr_settings()
        assert defaults["auto_search"] is True
        assert defaults["auto_trickle"] is False
        assert defaults["trickle_rate_seconds"] == 3.0
        assert defaults["trickle_batch_size"] == 25
        assert defaults["auto_trickle_interval_minutes"] == 30
        assert defaults["url"] is None
        assert defaults["api_key"] is None

        updated = test_db.update_lidarr_settings({
            "url": "http://lidarr:8686",
            "api_key": "my-secret-key-123",
            "auto_search": False,
            "trickle_rate_seconds": 5.0,
            "trickle_batch_size": 10,
            "auto_trickle": True,
            "auto_trickle_interval_minutes": 60,
        })
        assert updated["url"] == "http://lidarr:8686"
        assert updated["api_key"] == "my-secret-key-123"
        assert updated["auto_search"] is False
        assert updated["trickle_rate_seconds"] == 5.0
        assert updated["trickle_batch_size"] == 10
        assert updated["auto_trickle"] is True
        assert updated["auto_trickle_interval_minutes"] == 60

        fetched = test_db.get_lidarr_settings()
        assert fetched["url"] == "http://lidarr:8686"
        assert fetched["api_key"] == "my-secret-key-123"


class TestMediaManagementAPI:
    """Validates API endpoints, permission checks, and live preview rendering."""

    def test_unauthenticated_access_rejected(self, app_and_client):
        _, client = app_and_client
        resp = client.get("/api/settings/media-management")
        assert resp.status_code == 401

        resp_preview = client.post("/api/settings/media-management/preview", json={})
        assert resp_preview.status_code == 401

    def test_regular_user_cannot_get_settings_forbidden(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["alice"], test_db, test_config)
        resp = client.get("/api/settings/media-management", headers=headers)
        assert resp.status_code == 403

    def test_admin_can_get_settings_and_presets(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        resp = client.get("/api/settings/media-management", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()

        assert "settings" in data
        assert data["settings"]["artist_folder_format"] == "{Artist Name}"
        assert "presets" in data
        assert "Lidarr Standard" in data["presets"]
        assert "Clean Minimal" in data["presets"]
        assert "Audiophile / Detailed" in data["presets"]

    def test_non_admin_cannot_update_settings_forbidden(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["alice"], test_db, test_config)

        payload = {"artist_folder_format": "{Artist CleanName}"}
        resp = client.post("/api/settings/media-management", json=payload, headers=headers)
        assert resp.status_code == 403
        assert "Administrator access required" in resp.json()["detail"]

    def test_admin_can_update_settings(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        payload = {
            "root_folder_path": "/data/music",
            "colon_replacement_format": "_",
            "clean_artist_names": False,
        }
        resp = client.post("/api/settings/media-management", json=payload, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["root_folder_path"] == "/data/music"
        assert data["colon_replacement_format"] == "_"
        assert data["clean_artist_names"] is False

        # Verify DB persisted
        db_settings = test_db.get_media_management_settings()
        assert db_settings["root_folder_path"] == "/data/music"

    def test_live_preview_renders_valid_paths(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)
        assert client.post("/api/settings/media-management/preview", json={}, headers=alice_headers).status_code == 403
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        resp = client.post("/api/settings/media-management/preview", json={}, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "previews" in data
        assert len(data["previews"]) == 4

        previews_by_id = {p["id"]: p for p in data["previews"]}

        # 1. Standard single disc
        std = previews_by_id["standard"]
        assert std["name"] == "Standard Single-Disc Track"
        assert std["output_path"] == "/data/media/music/Pink Floyd/The Dark Side of the Moon (1973)/01 - Speak to Me (FLAC 24bit 96kHz).flac"

        # 2. Multi-disc track
        multi = previews_by_id["multi_disc"]
        assert multi["name"] == "Multi-Disc Track (Disc 2)"
        assert multi["output_path"] == "/data/media/music/The Beatles/The Beatles (White Album) (1968)/CD 02/01 - Revolution 1 (FLAC 16bit 44.1kHz).flac"

        # 3. Compilation track
        comp = previews_by_id["compilation"]
        assert comp["name"] == "Compilation / Various Artists Track"
        assert comp["output_path"] == "/data/media/music/Various Artists/Wayne's World - Music from the Motion Picture (1992) - Soundtrack/01 - Queen - Bohemian Rhapsody (MP3 320kbps).mp3"

    def test_live_preview_with_custom_template_overrides(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        custom_override = {
            "root_folder_path": "/library",
            "standard_track_format": "{Album Title} ({Release Year})/{track:0} {Track Title}",
        }
        resp = client.post("/api/settings/media-management/preview", json=custom_override, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        previews_by_id = {p["id"]: p for p in data["previews"]}

        std = previews_by_id["standard"]
        assert std["output_path"] == "/library/Pink Floyd/The Dark Side of the Moon (1973)/1 Speak to Me.flac"

    def test_update_media_management_staging_and_import_mode(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        payload = {
            "staging_folder_path": "/data/downloads/completed",
            "import_mode": "hardlink",
        }
        resp = client.post("/api/settings/media-management", json=payload, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["staging_folder_path"] == "/data/downloads/completed"
        assert data["import_mode"] == "hardlink"

        # Verify DB persisted
        db_settings = test_db.get_media_management_settings()
        assert db_settings["staging_folder_path"] == "/data/downloads/completed"
        assert db_settings["import_mode"] == "hardlink"

    def test_update_media_management_library_mode(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        # 1. Default should be 'native'
        resp = client.get("/api/settings/media-management", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["settings"]["library_mode"] == "native"

        # 2. The media-management PUT can no longer change the mode (PUT /api/settings/library-manager does)
        payload = {"library_mode": "lidarr", "import_mode": "hardlink"}
        resp_up = client.post("/api/settings/media-management", json=payload, headers=admin_headers)
        assert resp_up.status_code == 200
        assert resp_up.json()["library_mode"] == "native"
        assert resp_up.json()["import_mode"] == "hardlink"

        # 3. Verify DB kept native
        db_settings = test_db.get_media_management_settings()
        assert db_settings["library_mode"] == "native"


class TestLidarrSettingsAPI:
    """Validates Lidarr settings API endpoints, auth checks, secret masking, and test connection."""

    def test_unauthenticated_access_rejected(self, app_and_client):
        _, client = app_and_client
        resp = client.get("/api/settings/lidarr")
        assert resp.status_code == 401

        resp_post = client.post("/api/settings/lidarr", json={})
        assert resp_post.status_code == 401

        resp_test = client.post("/api/settings/lidarr/test", json={"url": "http://127.0.0.1:8686", "api_key": "test"})
        assert resp_test.status_code == 401

    def test_regular_user_forbidden(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)

        assert client.get("/api/settings/lidarr", headers=alice_headers).status_code == 403
        assert client.post("/api/settings/lidarr", json={}, headers=alice_headers).status_code == 403
        assert client.post(
            "/api/settings/lidarr/test",
            json={"url": "http://127.0.0.1:8686", "api_key": "test"},
            headers=alice_headers,
        ).status_code == 403

    def test_get_lidarr_settings_masked_api_key(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
        test_db.update_lidarr_settings({"url": "http://lidarr:8686", "api_key": "supersecretkey12345"})

        resp = client.get("/api/settings/lidarr", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["url"] == "http://lidarr:8686"
        assert data["api_key"] != "supersecretkey12345"
        assert "••••" in data["api_key"]

    def test_post_lidarr_settings_preserves_masked_key(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        # 1. First save raw key and settings
        payload1 = {
            "url": "http://lidarr:8686",
            "api_key": "original-secret-api-key-999",
            "auto_search": True,
            "trickle_rate_seconds": 2.5,
            "trickle_batch_size": 30,
            "auto_trickle": True,
            "auto_trickle_interval_minutes": 15,
        }
        resp1 = client.post("/api/settings/lidarr", json=payload1, headers=admin_headers)
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["auto_search"] is True
        assert data1["trickle_rate_seconds"] == 2.5
        assert data1["trickle_batch_size"] == 30
        assert data1["auto_trickle"] is True
        assert data1["auto_trickle_interval_minutes"] == 15
        assert "••••" in data1["api_key"]

        # 2. Resend masked key with updated pacing
        masked_key = data1["api_key"]
        payload2 = {
            "api_key": masked_key,
            "auto_search": False,
            "trickle_rate_seconds": 4.0,
        }
        resp2 = client.post("/api/settings/lidarr", json=payload2, headers=admin_headers)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["auto_search"] is False
        assert data2["trickle_rate_seconds"] == 4.0

        # Verify raw secret persisted unchanged in database
        db_settings = test_db.get_lidarr_settings()
        assert db_settings["api_key"] == "original-secret-api-key-999"
        assert db_settings["auto_search"] is False
        assert db_settings["trickle_rate_seconds"] == 4.0

    def test_post_lidarr_test_connection_mock(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        with patch("plex_playlist_sync.api.routes.settings.LidarrClient") as mock_client_cls:
            mock_inst = MagicMock()
            mock_inst.test_connection.return_value = {"online": True, "version": "2.8.2.4233", "error": None}
            mock_client_cls.return_value = mock_inst

            resp = client.post(
                "/api/settings/lidarr/test",
                json={"url": "http://192.168.1.50:8686", "api_key": "test-key-abc"},
                headers=admin_headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["online"] is True
            assert data["version"] == "2.8.2.4233"
            assert data["error"] is None

    def test_post_lidarr_test_connection_reuses_db_key_when_masked(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
        test_db.update_lidarr_settings({"url": "http://192.168.1.50:8686", "api_key": "saved-database-secret"})

        with patch("plex_playlist_sync.api.routes.settings.LidarrClient") as mock_client_cls:
            mock_inst = MagicMock()
            mock_inst.test_connection.return_value = {"online": True, "version": "2.8.2"}
            mock_client_cls.return_value = mock_inst

            resp = client.post(
                "/api/settings/lidarr/test",
                json={"url": "http://192.168.1.50:8686", "api_key": "••••••••"},
                headers=admin_headers,
            )
            assert resp.status_code == 200
            mock_client_cls.assert_called_once_with(base_url="http://192.168.1.50:8686", api_key="saved-database-secret")


class TestGeneralSettingsAPI:
    """Tests for GET /api/settings/general and POST /api/settings/general."""

    def test_get_general_settings_rbac(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client

        # Unauthenticated request
        resp_unauth = client.get("/api/settings/general")
        assert resp_unauth.status_code in (401, 403)

        # Non-admin request
        alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)
        resp_non_admin = client.get("/api/settings/general", headers=alice_headers)
        assert resp_non_admin.status_code == 403

        # Admin request
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
        resp_admin = client.get("/api/settings/general", headers=admin_headers)
        assert resp_admin.status_code == 200
        data = resp_admin.json()
        assert "application_url" in data
        assert data["application_url"] == ""

    def test_get_general_settings_env_fallback(self, app_and_client, test_db, test_config, seeded_users, monkeypatch):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        monkeypatch.setenv("APPLICATION_URL", "https://trackseerr.mydomain.com")
        resp = client.get("/api/settings/general", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["application_url"] == "https://trackseerr.mydomain.com"

    def test_post_general_settings_admin_success(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        payload = {"application_url": "https://music.home.arpa"}
        resp = client.post("/api/settings/general", json=payload, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["application_url"] == "https://music.home.arpa"
        assert data["updated_at"] is not None

        # Verify DB directly
        db_settings = test_db.get_general_settings()
        assert db_settings["application_url"] == "https://music.home.arpa"

    def test_post_general_settings_strips_trailing_slash(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        payload = {"application_url": "https://trackseerr.external.io///"}
        resp = client.post("/api/settings/general", json=payload, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["application_url"] == "https://trackseerr.external.io"

    def test_post_general_settings_invalid_scheme_returns_400(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        payload = {"application_url": "ftp://files.example.com"}
        resp = client.post("/api/settings/general", json=payload, headers=admin_headers)
        assert resp.status_code == 400
        assert "http://" in resp.json()["detail"] or "https://" in resp.json()["detail"]

    def test_post_general_settings_clear_url(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        test_db.update_general_settings({"application_url": "https://old.domain.com"})
        assert test_db.get_general_settings()["application_url"] == "https://old.domain.com"

        resp = client.post("/api/settings/general", json={"application_url": ""}, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["application_url"] == ""


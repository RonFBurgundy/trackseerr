from pathlib import Path

from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.storage import SCHEMA_VERSION, Database
from trackseerr.track_matching import find_owned_equivalent


def test_schema_version_is_at_least_89() -> None:
    assert SCHEMA_VERSION >= 89


def test_setting_defaults_off_and_round_trips(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "test_allow_multiple_editions.db"))
    try:
        # Default off
        settings = db.get_media_management_settings()
        assert settings["allow_multiple_editions"] is False

        # Update to True
        db.update_media_management_settings({"allow_multiple_editions": True})
        assert db.get_media_management_settings()["allow_multiple_editions"] is True

        # Update back to False
        db.update_media_management_settings({"allow_multiple_editions": False})
        assert db.get_media_management_settings()["allow_multiple_editions"] is False
    finally:
        db.close()


def test_migration_v89_idempotent(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "test_migration_v89.db"))
    try:
        conn = db.conn
        assert db.get_media_management_settings()["allow_multiple_editions"] is False
        conn.execute("ALTER TABLE media_management_settings DROP COLUMN allow_multiple_editions")
        conn.commit()

        cur = conn.cursor()
        db._migration_v89(cur)
        db._migration_v89(cur)
        conn.commit()

        cols = {r[1] for r in conn.execute("PRAGMA table_info(media_management_settings)")}
        assert "allow_multiple_editions" in cols
        assert db.get_media_management_settings()["allow_multiple_editions"] is False
    finally:
        db.close()


def test_setting_on_disables_owned_equivalent(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "test_equiv.db"))
    try:
        db.upsert_library_artist({"id": "art-1", "name": "Prince", "monitored": True})
        db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": "Sign O' The Times", "monitored": True})
        db.upsert_library_track({
            "id": "trk-1",
            "album_id": "alb-1",
            "artist_id": "art-1",
            "title": "It",
            "duration_seconds": 310.0,
            "monitored": True,
        })
        db.upsert_library_file({
            "track_id": "trk-1",
            "file_path": "/music/Prince/Sign O The Times/03 It.flac",
            "cutoff_met": True,
        })

        # When setting is off (default), match is returned
        assert db.get_media_management_settings()["allow_multiple_editions"] is False
        match = find_owned_equivalent(db, "art-1", "It - 2020 Remaster")
        assert match is not None
        assert match["id"] == "trk-1"

        # When setting is on, match is None
        db.update_media_management_settings({"allow_multiple_editions": True})
        assert db.get_media_management_settings()["allow_multiple_editions"] is True
        match_disabled = find_owned_equivalent(db, "art-1", "It - 2020 Remaster")
        assert match_disabled is None
    finally:
        db.close()


def test_api_settings_exposes_flag(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "api_test.db"))
    config = Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        data_dir=str(tmp_path),
    )
    admin = db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=admin["id"],
        username=admin["username"],
        is_admin=admin["is_admin"],
        secret_key=secret,
    )
    db.create_session(token, admin["id"], {"auth": "test"})
    admin_headers = {"Authorization": f"Bearer {token}"}

    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config

    client = TestClient(app)
    try:
        # GET: verify default is False
        resp = client.get("/api/settings/media-management", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["settings"]["allow_multiple_editions"] is False

        # PUT: update to True
        put_resp = client.put(
            "/api/settings/media-management",
            json={"allow_multiple_editions": True},
            headers=admin_headers,
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["allow_multiple_editions"] is True

        # GET: verify change persisted
        resp2 = client.get("/api/settings/media-management", headers=admin_headers)
        assert resp2.status_code == 200
        assert resp2.json()["settings"]["allow_multiple_editions"] is True

        # POST: update back to False
        post_resp = client.post(
            "/api/settings/media-management",
            json={"allow_multiple_editions": False},
            headers=admin_headers,
        )
        assert post_resp.status_code == 200
        assert post_resp.json()["allow_multiple_editions"] is False
    finally:
        db.close()

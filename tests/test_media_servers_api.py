"""Tests for the multi-server media-servers admin API."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_plex_client
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.media_servers import capabilities_for, registry
from trackseerr.media_servers import settings as ms_settings
from trackseerr.media_servers.base import ConnectionTest
from trackseerr.security import mask_secret
from trackseerr.storage import Database

SUBSONIC_URL = "http://navidrome:4533"
PLEX_URL = "http://plex:32400"


def _headers(db: Database, cfg: Config, user: dict) -> dict[str, str]:
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=get_or_create_secret_key(data_dir=cfg.data_dir),
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(tmp_path):
    registry._reset_for_tests()
    ms_settings.clear_listeners()
    db = Database(":memory:")
    cfg = Config(plex_url="", plex_token="", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_plex_client] = lambda: None
    admin = db.upsert_user("a1", "admin", "a@x", is_admin=True)
    alice = db.upsert_user("u1", "alice", "u@x", is_admin=False)
    yield SimpleNamespace(
        tc=TestClient(app),
        db=db,
        cfg=cfg,
        admin=_headers(db, cfg, admin),
        alice=_headers(db, cfg, alice),
        app=app,
    )
    registry._reset_for_tests()
    ms_settings.clear_listeners()


SUBSONIC_BODY = {
    "kind": "subsonic",
    "name": "Navidrome",
    "url": SUBSONIC_URL,
    "username": "bob",
    "password": "super-secret-password",
    "api_key": "",
}


def test_routes_require_admin(env):
    assert env.tc.get("/api/settings/media-servers").status_code in (401, 403)
    assert env.tc.post("/api/settings/media-servers", json=SUBSONIC_BODY).status_code in (401, 403)
    assert env.tc.post("/api/settings/media-servers/test", json=SUBSONIC_BODY).status_code in (401, 403)
    assert env.tc.delete("/api/settings/media-servers/some-id").status_code in (401, 403)
    assert env.tc.put("/api/settings/media-servers/login", json={"server_id": None}).status_code in (401, 403)

    assert env.tc.get("/api/settings/media-servers", headers=env.alice).status_code == 403
    assert env.tc.post("/api/settings/media-servers", json=SUBSONIC_BODY, headers=env.alice).status_code == 403
    assert env.tc.post("/api/settings/media-servers/test", json=SUBSONIC_BODY, headers=env.alice).status_code == 403
    assert env.tc.delete("/api/settings/media-servers/some-id", headers=env.alice).status_code == 403
    assert env.tc.put("/api/settings/media-servers/login", json={"server_id": None}, headers=env.alice).status_code == 403


def test_create_list_masks_secrets(env):
    created = env.tc.post("/api/settings/media-servers", json=SUBSONIC_BODY, headers=env.admin)
    assert created.status_code == 200
    item = created.json()
    expected_mask = mask_secret("super-secret-password")
    assert item["password"] == expected_mask
    assert "super-secret-password" not in item["password"]
    assert item["api_key"] == ""
    assert item["locked"] is False
    assert item["capabilities"] == capabilities_for("subsonic").to_dict()

    listed = env.tc.get("/api/settings/media-servers", headers=env.admin)
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["password"] == expected_mask
    assert rows[0]["locked"] is False
    assert rows[0]["capabilities"] == capabilities_for("subsonic").to_dict()


def test_create_plex_requires_token(env):
    resp = env.tc.post(
        "/api/settings/media-servers",
        json={"kind": "plex", "name": "My Plex", "url": PLEX_URL, "api_key": ""},
        headers=env.admin,
    )
    assert resp.status_code == 422


def test_create_rejects_unsafe_url(env):
    resp = env.tc.post(
        "/api/settings/media-servers",
        json={**SUBSONIC_BODY, "url": "http://169.254.169.254"},
        headers=env.admin,
    )
    assert resp.status_code == 422


def test_update_keeps_masked_secret(env):
    created = env.tc.post("/api/settings/media-servers", json=SUBSONIC_BODY, headers=env.admin).json()
    updated = env.tc.post(
        "/api/settings/media-servers",
        json={**SUBSONIC_BODY, "id": created["id"], "name": "Renamed Navidrome", "password": created["password"]},
        headers=env.admin,
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Renamed Navidrome"
    row = env.db.get_media_server_row(created["id"])
    assert row is not None
    assert row["password"] == "super-secret-password"


def test_masked_secret_with_changed_url_is_rejected(env):
    created = env.tc.post("/api/settings/media-servers", json=SUBSONIC_BODY, headers=env.admin).json()
    resp = env.tc.post(
        "/api/settings/media-servers",
        json={**SUBSONIC_BODY, "id": created["id"], "url": "http://other-host:4533", "password": created["password"]},
        headers=env.admin,
    )
    assert resp.status_code == 400


def test_kind_cannot_change(env):
    created = env.tc.post("/api/settings/media-servers", json=SUBSONIC_BODY, headers=env.admin).json()
    resp = env.tc.post(
        "/api/settings/media-servers",
        json={"id": created["id"], "kind": "jellyfin", "url": SUBSONIC_URL, "api_key": "jf-key"},
        headers=env.admin,
    )
    assert resp.status_code == 422


def test_env_row_only_toggles(env):
    plex_cfg = Config(plex_url=PLEX_URL, plex_token="env-plex-token", data_dir=env.cfg.data_dir)
    env.app.dependency_overrides[get_config] = lambda: plex_cfg
    registry.sync_env_rows(plex_cfg, env.db)

    listed = env.tc.get("/api/settings/media-servers", headers=env.admin).json()
    assert len(listed) == 1
    env_row = listed[0]
    assert env_row["id"] == "env-plex"
    assert env_row["locked"] is True

    # Renaming and disabling works
    toggled = env.tc.post(
        "/api/settings/media-servers",
        json={
            "id": "env-plex",
            "kind": "plex",
            "name": "Home Plex",
            "enabled": False,
            "url": env_row["url"],
            "username": env_row["username"],
            "password": env_row["password"],
            "api_key": env_row["api_key"],
            "music_section": env_row["music_section"],
            "verify_ssl": env_row["verify_ssl"],
        },
        headers=env.admin,
    )
    assert toggled.status_code == 200
    assert toggled.json()["name"] == "Home Plex"
    assert toggled.json()["enabled"] is False

    # Changing URL is rejected with 409
    bad_url = env.tc.post(
        "/api/settings/media-servers",
        json={"id": "env-plex", "kind": "plex", "name": "Home Plex", "url": "http://other:32400"},
        headers=env.admin,
    )
    assert bad_url.status_code == 409

    # Deleting env row is rejected with 409
    bad_del = env.tc.delete("/api/settings/media-servers/env-plex", headers=env.admin)
    assert bad_del.status_code == 409


def test_delete_and_404(env):
    created = env.tc.post("/api/settings/media-servers", json=SUBSONIC_BODY, headers=env.admin).json()
    deleted = env.tc.delete(f"/api/settings/media-servers/{created['id']}", headers=env.admin)
    assert deleted.status_code == 200
    assert env.db.get_media_server_row(created["id"]) is None

    missing = env.tc.delete(f"/api/settings/media-servers/{created['id']}", headers=env.admin)
    assert missing.status_code == 404


def test_set_login_plex_only(env):
    sub_row = env.tc.post("/api/settings/media-servers", json=SUBSONIC_BODY, headers=env.admin).json()
    plex_row = env.tc.post(
        "/api/settings/media-servers",
        json={"kind": "plex", "name": "Plex One", "url": PLEX_URL, "api_key": "plex-tok-123"},
        headers=env.admin,
    ).json()

    ok_resp = env.tc.put("/api/settings/media-servers/login", json={"server_id": plex_row["id"]}, headers=env.admin)
    assert ok_resp.status_code == 200

    rows = {r["id"]: r for r in env.tc.get("/api/settings/media-servers", headers=env.admin).json()}
    assert rows[plex_row["id"]]["is_login"] is True
    assert rows[sub_row["id"]]["is_login"] is False

    bad_resp = env.tc.put("/api/settings/media-servers/login", json={"server_id": sub_row["id"]}, headers=env.admin)
    assert bad_resp.status_code == 422

    clear_resp = env.tc.put("/api/settings/media-servers/login", json={"server_id": None}, headers=env.admin)
    assert clear_resp.status_code == 200
    rows_after = env.tc.get("/api/settings/media-servers", headers=env.admin).json()
    assert all(not r["is_login"] for r in rows_after)


def test_save_reloads_registry_and_notifies(env):
    calls: list[int] = []
    ms_settings.on_change(lambda: calls.append(1))

    created = env.tc.post("/api/settings/media-servers", json=SUBSONIC_BODY, headers=env.admin)
    assert created.status_code == 200
    server_id = created.json()["id"]

    recs = registry.records()
    assert any(rec.id == server_id and rec.url == SUBSONIC_URL for rec in recs)
    assert len(calls) == 1


def test_test_endpoint_uses_saved_secret_and_never_caches(env, monkeypatch):
    seen: list[tuple[str, str, str, str, bool]] = []

    class FakeSubsonicAdapter:
        def __init__(self, url, username, password, api_key="", verify_ssl=True):
            self.closed = False
            seen.append((url, username, password, api_key, verify_ssl))

        def test_connection(self) -> ConnectionTest:
            return ConnectionTest(ok=True, message="Navidrome ok")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(registry, "SubsonicMediaServer", FakeSubsonicAdapter)

    created = env.tc.post("/api/settings/media-servers", json=SUBSONIC_BODY, headers=env.admin).json()
    assert registry._adapters == {}

    resp = env.tc.post(
        "/api/settings/media-servers/test",
        json={**SUBSONIC_BODY, "id": created["id"], "password": created["password"]},
        headers=env.admin,
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "message": "Navidrome ok"}
    assert seen == [(SUBSONIC_URL, "bob", "super-secret-password", "", True)]
    assert registry._adapters == {}


def test_env_row_lists_env_url_and_masked_token(env):
    plex_cfg = Config(plex_url=PLEX_URL, plex_token="env-plex-token-secret", data_dir=env.cfg.data_dir)
    env.app.dependency_overrides[get_config] = lambda: plex_cfg
    registry.reload(plex_cfg, env.db)

    listed = env.tc.get("/api/settings/media-servers", headers=env.admin).json()
    assert len(listed) == 1
    row = listed[0]
    assert row["id"] == "env-plex"
    assert row["url"] == PLEX_URL
    assert row["api_key"] == mask_secret("env-plex-token-secret")
    assert "env-plex-token-secret" not in row["api_key"]


def test_env_row_test_uses_env_credentials(env, monkeypatch):
    seen: list[tuple[str, str, str, str, bool]] = []

    class FakeSubsonicAdapter:
        def __init__(self, url, username, password, api_key="", verify_ssl=True):
            seen.append((url, username, password, api_key, verify_ssl))

        def test_connection(self) -> ConnectionTest:
            return ConnectionTest(ok=True, message="Env Subsonic ok")

        def close(self) -> None:
            pass

    monkeypatch.setattr(registry, "SubsonicMediaServer", FakeSubsonicAdapter)

    sub_cfg = Config(
        plex_url="",
        plex_token="",
        subsonic_url=SUBSONIC_URL,
        subsonic_user="env-user",
        subsonic_password="env-password",
        data_dir=env.cfg.data_dir,
    )
    env.app.dependency_overrides[get_config] = lambda: sub_cfg
    registry.reload(sub_cfg, env.db)

    resp = env.tc.post(
        "/api/settings/media-servers/test",
        json={
            "id": "env-subsonic",
            "kind": "subsonic",
            "name": "Subsonic",
            "url": "http://ignored-submitted:4533",
            "username": "ignored",
            "password": "ignored",
        },
        headers=env.admin,
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "message": "Env Subsonic ok"}
    assert seen == [(SUBSONIC_URL, "env-user", "env-password", "", True)]


def test_second_plex_gets_numbered_name(env):
    r1 = env.tc.post(
        "/api/settings/media-servers",
        json={"kind": "plex", "url": "https://plex1.example.com", "api_key": "tok1"},
        headers=env.admin,
    )
    r2 = env.tc.post(
        "/api/settings/media-servers",
        json={"kind": "plex", "url": "https://plex2.example.com", "api_key": "tok2"},
        headers=env.admin,
    )
    r3 = env.tc.post(
        "/api/settings/media-servers",
        json={"kind": "plex", "url": "https://plex3.example.com", "api_key": "tok3"},
        headers=env.admin,
    )
    assert r1.status_code == 200 and r1.json()["name"] == "Plex"
    assert r2.status_code == 200 and r2.json()["name"] == "Plex 2"
    assert r3.status_code == 200 and r3.json()["name"] == "Plex 3"


def test_duplicate_name_rejected(env):
    first = env.tc.post(
        "/api/settings/media-servers",
        json={"kind": "plex", "name": "Living Room", "url": "https://plex1.example.com", "api_key": "tok1"},
        headers=env.admin,
    )
    assert first.status_code == 200

    dup = env.tc.post(
        "/api/settings/media-servers",
        json={"kind": "jellyfin", "name": "living room", "url": "https://jf.example.com", "api_key": "tok2"},
        headers=env.admin,
    )
    assert dup.status_code == 422
    assert "Another server already uses that name" in dup.json()["detail"]

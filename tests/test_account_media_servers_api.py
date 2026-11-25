"""Tests for /api/account/media-servers self-service Subsonic login endpoints."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_plex_client
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.media_servers import registry
from trackseerr.media_servers import settings as ms_settings
from trackseerr.media_servers.subsonic import SubsonicMediaServer
from trackseerr.secret_box import unseal
from trackseerr.storage import Database
from tests.subsonic_fake import FakeSubsonic, FakeUser, default_state

SUBSONIC_URL = "http://navidrome:4533"


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
def env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    registry._reset_for_tests()
    ms_settings.clear_listeners()
    db = Database(":memory:")
    cfg = Config(plex_url="", plex_token="", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_plex_client] = lambda: None

    alice = db.upsert_user("u1", "alice", "alice@x", is_admin=False)
    bob = db.upsert_user("u2", "bob", "bob@x", is_admin=False)

    state = default_state([("Song A", "Artist 1", "Album X")], password="pw")
    state.users["alice"] = FakeUser("alice", admin=False, password="alice-pw")
    fake = FakeSubsonic(state)

    sub_row = db.insert_media_server_row(
        {
            "kind": "subsonic",
            "name": "Navidrome",
            "url": SUBSONIC_URL,
            "username": "admin",
            "password": "pw",
            "enabled": True,
            "use_playlists": True,
        }
    )
    disabled_row = db.insert_media_server_row(
        {
            "kind": "subsonic",
            "name": "Disabled Sub",
            "url": "http://nav2:4533",
            "username": "admin",
            "password": "pw",
            "enabled": False,
            "use_playlists": True,
        }
    )
    no_playlists_row = db.insert_media_server_row(
        {
            "kind": "subsonic",
            "name": "No Playlists Sub",
            "url": "http://nav3:4533",
            "username": "admin",
            "password": "pw",
            "enabled": True,
            "use_playlists": False,
        }
    )
    db.insert_media_server_row(
        {"kind": "plex", "name": "Plex", "url": "http://plex:32400", "api_key": "tok", "enabled": True}
    )
    db.insert_media_server_row(
        {"kind": "jellyfin", "name": "Jellyfin", "url": "http://jf:8096", "api_key": "tok", "enabled": True}
    )
    registry.reload(cfg, db)

    adapter = SubsonicMediaServer(
        SUBSONIC_URL, "admin", "pw", transport=fake.transport(), sleep=lambda _s: None
    )
    monkeypatch.setattr(
        "trackseerr.media_servers.registry.build_adapter",
        lambda rec: adapter if rec.id == sub_row["id"] else None,
    )

    yield SimpleNamespace(
        tc=TestClient(app),
        db=db,
        cfg=cfg,
        alice=_headers(db, cfg, alice),
        bob=_headers(db, cfg, bob),
        sub_id=sub_row["id"],
        disabled_id=disabled_row["id"],
        no_playlists_id=no_playlists_row["id"],
        fake=fake,
    )
    adapter.close()
    registry._reset_for_tests()
    ms_settings.clear_listeners()


def test_requires_authenticated_user(env) -> None:
    assert env.tc.get("/api/account/media-servers").status_code == 401
    assert env.tc.put(
        f"/api/account/media-servers/{env.sub_id}",
        json={"username": "alice", "password": "alice-pw"},
    ).status_code == 401
    assert env.tc.delete(f"/api/account/media-servers/{env.sub_id}").status_code == 401


def test_list_hides_password_and_filters_subsonic_playlist_servers(env) -> None:
    resp = env.tc.get("/api/account/media-servers", headers=env.alice)
    assert resp.status_code == 200
    assert resp.json() == [
        {"server_id": env.sub_id, "name": "Navidrome", "username": "", "saved": False}
    ]
    assert "pw" not in resp.text


def test_put_tests_then_saves_and_bad_password_422(env) -> None:
    bad = env.tc.put(
        f"/api/account/media-servers/{env.sub_id}",
        json={"username": "alice", "password": "wrong"},
        headers=env.alice,
    )
    assert bad.status_code == 422
    assert "40" in bad.json()["detail"]
    assert env.db.get_server_account("u1", env.sub_id) is None

    ok = env.tc.put(
        f"/api/account/media-servers/{env.sub_id}",
        json={"username": "alice", "password": "alice-pw"},
        headers=env.alice,
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body == {"server_id": env.sub_id, "name": "Navidrome", "username": "alice", "saved": True}
    assert "alice-pw" not in ok.text

    stored = env.db.get_server_account("u1", env.sub_id)
    assert stored is not None
    assert stored["secret_sealed"] != "alice-pw"
    assert unseal(stored["secret_sealed"], data_dir=env.cfg.data_dir) == "alice-pw"

    listed = env.tc.get("/api/account/media-servers", headers=env.alice)
    assert listed.status_code == 200
    assert listed.json() == [
        {"server_id": env.sub_id, "name": "Navidrome", "username": "alice", "saved": True}
    ]
    assert "alice-pw" not in listed.text
    assert stored["secret_sealed"] not in listed.text


def test_put_unknown_or_unlisted_server_404(env) -> None:
    for sid in ("does-not-exist", env.disabled_id, env.no_playlists_id):
        resp = env.tc.put(
            f"/api/account/media-servers/{sid}",
            json={"username": "alice", "password": "alice-pw"},
            headers=env.alice,
        )
        assert resp.status_code == 404


def test_delete_removes_login_and_isolates_users(env) -> None:
    put_resp = env.tc.put(
        f"/api/account/media-servers/{env.sub_id}",
        json={"username": "alice", "password": "alice-pw"},
        headers=env.alice,
    )
    assert put_resp.status_code == 200

    # Bob cannot see Alice's saved login
    bob_list = env.tc.get("/api/account/media-servers", headers=env.bob)
    assert bob_list.status_code == 200
    assert bob_list.json() == [
        {"server_id": env.sub_id, "name": "Navidrome", "username": "", "saved": False}
    ]

    # Bob deleting does not affect Alice
    assert env.tc.delete(f"/api/account/media-servers/{env.sub_id}", headers=env.bob).status_code == 200
    assert env.db.get_server_account("u1", env.sub_id) is not None

    # Alice deleting removes her saved login
    del_resp = env.tc.delete(f"/api/account/media-servers/{env.sub_id}", headers=env.alice)
    assert del_resp.status_code == 200
    assert env.db.get_server_account("u1", env.sub_id) is None
    after = env.tc.get("/api/account/media-servers", headers=env.alice)
    assert after.json() == [
        {"server_id": env.sub_id, "name": "Navidrome", "username": "", "saved": False}
    ]

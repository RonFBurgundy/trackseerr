"""Tests for per-playlist media server exclusions and state in the playlists API."""

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
from trackseerr.storage import Database


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


def _seed_servers(env) -> tuple[str, str]:
    s1 = env.tc.post(
        "/api/settings/media-servers",
        json={
            "kind": "subsonic",
            "name": "Navidrome",
            "url": "http://navidrome:4533",
            "username": "bob",
            "password": "secret",
            "api_key": "",
        },
        headers=env.admin,
    )
    assert s1.status_code == 200
    s2 = env.tc.post(
        "/api/settings/media-servers",
        json={
            "kind": "jellyfin",
            "name": "Jellyfin",
            "url": "http://jellyfin:8096",
            "username": "",
            "password": "",
            "api_key": "jf-key",
        },
        headers=env.admin,
    )
    assert s2.status_code == 200
    return s1.json()["id"], s2.json()["id"]


def _seed_playlist(env, playlist_id: str = "pl-1", creator_id: str = "u1") -> None:
    env.db.upsert_playlist(playlist_id, name="Test Mix", service="spotify", creator_id=creator_id)
    env.db.set_playlist_targets(playlist_id, [creator_id])


def test_list_includes_excludes_and_states_for_admin(env):
    s1_id, s2_id = _seed_servers(env)
    _seed_playlist(env)
    env.db.set_playlist_server_excludes("pl-1", [s2_id, s1_id])
    env.db.record_playlist_server_state("pl-1", s1_id, "ok", matched=12, missing=2)
    env.db.record_playlist_server_state("pl-1", s2_id, "error", matched=0, missing=0, error="timeout")

    res = env.tc.get("/api/playlists", headers=env.admin)
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 1
    item = rows[0]
    assert item["excluded_servers"] == sorted([s1_id, s2_id])
    states = {st["server_id"]: st for st in item["server_states"]}
    assert states[s1_id]["status"] == "ok"
    assert states[s1_id]["matched"] == 12
    assert states[s1_id]["missing"] == 2
    assert states[s2_id]["status"] == "error"
    assert states[s2_id]["error"] == "timeout"


def test_non_admin_gets_empty_server_fields(env):
    s1_id, _s2_id = _seed_servers(env)
    _seed_playlist(env, creator_id="u1")
    env.db.set_playlist_server_excludes("pl-1", [s1_id])
    env.db.record_playlist_server_state("pl-1", s1_id, "ok", matched=5, missing=1)

    res = env.tc.get("/api/playlists", headers=env.alice)
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 1
    assert rows[0]["excluded_servers"] == []
    assert rows[0]["server_states"] == []


def test_put_servers_admin_only(env):
    s1_id, _s2_id = _seed_servers(env)
    _seed_playlist(env, creator_id="u1")

    unauth = env.tc.put("/api/playlists/pl-1/servers", json={"excluded_server_ids": [s1_id]})
    assert unauth.status_code in (401, 403)

    non_admin = env.tc.put(
        "/api/playlists/pl-1/servers",
        json={"excluded_server_ids": [s1_id]},
        headers=env.alice,
    )
    assert non_admin.status_code == 403


def test_put_unknown_server_422(env):
    _seed_playlist(env)
    res = env.tc.put(
        "/api/playlists/pl-1/servers",
        json={"excluded_server_ids": ["nonexistent-server"]},
        headers=env.admin,
    )
    assert res.status_code == 422

    missing_pl = env.tc.put(
        "/api/playlists/does-not-exist/servers",
        json={"excluded_server_ids": []},
        headers=env.admin,
    )
    assert missing_pl.status_code == 404


def test_put_round_trip(env):
    s1_id, s2_id = _seed_servers(env)
    _seed_playlist(env)

    put_res = env.tc.put(
        "/api/playlists/pl-1/servers",
        json={"excluded_server_ids": [s2_id]},
        headers=env.admin,
    )
    assert put_res.status_code == 200
    assert put_res.json() == {"id": "pl-1", "excluded_servers": [s2_id]}

    listed = env.tc.get("/api/playlists", headers=env.admin)
    assert listed.status_code == 200
    assert listed.json()[0]["excluded_servers"] == [s2_id]

    clear_res = env.tc.put(
        "/api/playlists/pl-1/servers",
        json={"excluded_server_ids": []},
        headers=env.admin,
    )
    assert clear_res.status_code == 200
    assert clear_res.json() == {"id": "pl-1", "excluded_servers": []}
    assert env.tc.get("/api/playlists", headers=env.admin).json()[0]["excluded_servers"] == []

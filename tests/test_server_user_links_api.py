"""Tests for ``/api/media-servers/links`` admin endpoints."""

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
    admin = db.import_media_server_user("a1", "admin", "a@x", auth_type="local", grant_admin=True)
    alice = db.upsert_user("u1", "alice", "u1@x", is_admin=False)
    bob = db.upsert_user("u2", "bob", "u2@x", is_admin=False)
    carol = db.import_media_server_user("u3", "carol", "u3@x", auth_type="local", grant_admin=False)
    assert admin is not None and carol is not None
    yield SimpleNamespace(
        tc=TestClient(app),
        db=db,
        cfg=cfg,
        admin=_headers(db, cfg, admin),
        alice=_headers(db, cfg, alice),
        alice_user=alice,
        bob_user=bob,
        carol_user=carol,
    )
    registry._reset_for_tests()
    ms_settings.clear_listeners()


def _create_server(tc: TestClient, headers: dict[str, str], payload: dict) -> dict:
    resp = tc.post("/api/settings/media-servers", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_links_routes_require_admin(env):
    assert env.tc.get("/api/media-servers/links").status_code in (401, 403)
    assert env.tc.put("/api/media-servers/links/s1/u1", json={"server_username": "x"}).status_code in (401, 403)
    assert env.tc.delete("/api/media-servers/links/s1/u1").status_code in (401, 403)

    assert env.tc.get("/api/media-servers/links", headers=env.alice).status_code == 403
    assert (
        env.tc.put("/api/media-servers/links/s1/u1", json={"server_username": "x"}, headers=env.alice).status_code
        == 403
    )
    assert env.tc.delete("/api/media-servers/links/s1/u1", headers=env.alice).status_code == 403


def test_list_links_filters_and_includes_plex_fallback(env):
    plex = _create_server(
        env.tc,
        env.admin,
        {
            "kind": "plex",
            "name": "Main Plex",
            "url": "http://plex:32400",
            "api_key": "plex-tok",
            "enabled": True,
            "use_playlists": True,
        },
    )
    assert env.tc.put("/api/settings/media-servers/login", json={"server_id": plex["id"]}, headers=env.admin).status_code == 200

    nd = _create_server(
        env.tc,
        env.admin,
        {
            "kind": "subsonic",
            "name": "Navidrome",
            "url": "http://navidrome:4533",
            "username": "admin",
            "password": "secret",
            "enabled": True,
            "use_playlists": True,
        },
    )
    # Disabled server and non-playlist server must not appear.
    _create_server(
        env.tc,
        env.admin,
        {
            "kind": "subsonic",
            "name": "Disabled ND",
            "url": "http://navidrome2:4533",
            "username": "admin",
            "password": "secret",
            "enabled": False,
            "use_playlists": True,
        },
    )
    _create_server(
        env.tc,
        env.admin,
        {
            "kind": "jellyfin",
            "name": "JF No Playlists",
            "url": "http://jellyfin:8096",
            "api_key": "jf-key",
            "enabled": True,
            "use_playlists": False,
        },
    )

    env.db.link_server_user("u1", plex["id"], "plex-u1", "alice_plex", source="imported")
    env.db.link_server_user("u3", nd["id"], "nd-carol", "carol_nd", source="imported")

    resp = env.tc.get("/api/media-servers/links", headers=env.admin)
    assert resp.status_code == 200, resp.text
    groups = resp.json()
    assert [g["server_id"] for g in groups] == [plex["id"], nd["id"]]

    plex_group, nd_group = groups
    assert plex_group["server_name"] == "Main Plex"
    assert plex_group["kind"] == "plex"
    # Linked Plex user uses link's server_username; unlinked auth_type=plex user (bob) falls back to username.
    assert plex_group["users"] == [
        {"user_id": "u1", "server_username": "alice_plex"},
        {"user_id": "u2", "server_username": "bob"},
    ]

    assert nd_group["server_name"] == "Navidrome"
    assert nd_group["kind"] == "subsonic"
    assert nd_group["users"] == [{"user_id": "u3", "server_username": "carol_nd"}]


def test_put_and_delete_manual_link(env):
    nd = _create_server(
        env.tc,
        env.admin,
        {
            "kind": "subsonic",
            "name": "Navidrome",
            "url": "http://navidrome:4533",
            "username": "admin",
            "password": "secret",
            "enabled": True,
            "use_playlists": True,
        },
    )

    put_resp = env.tc.put(
        f"/api/media-servers/links/{nd['id']}/u1",
        json={"server_username": "alice_nd"},
        headers=env.admin,
    )
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json() == {
        "user_id": "u1",
        "server_id": nd["id"],
        "server_username": "alice_nd",
        "source": "manual",
    }
    stored = env.db.get_server_user_link("u1", nd["id"])
    assert stored is not None
    assert stored["source"] == "manual"
    assert stored["server_username"] == "alice_nd"

    # Blank username is rejected.
    blank_resp = env.tc.put(
        f"/api/media-servers/links/{nd['id']}/u1",
        json={"server_username": "   "},
        headers=env.admin,
    )
    assert blank_resp.status_code == 422

    # Unknown server or user returns 404.
    assert (
        env.tc.put(
            "/api/media-servers/links/missing-srv/u1",
            json={"server_username": "x"},
            headers=env.admin,
        ).status_code
        == 404
    )
    assert (
        env.tc.put(
            f"/api/media-servers/links/{nd['id']}/missing-user",
            json={"server_username": "x"},
            headers=env.admin,
        ).status_code
        == 404
    )

    # Delete removes the link; deleting again returns 404.
    del_resp = env.tc.delete(f"/api/media-servers/links/{nd['id']}/u1", headers=env.admin)
    assert del_resp.status_code == 200, del_resp.text
    assert del_resp.json() == {"status": "deleted", "server_id": nd["id"], "user_id": "u1"}
    assert env.db.get_server_user_link("u1", nd["id"]) is None

    assert env.tc.delete(f"/api/media-servers/links/{nd['id']}/u1", headers=env.admin).status_code == 404

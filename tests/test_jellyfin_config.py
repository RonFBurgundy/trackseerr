"""Jellyfin media-server configuration: env validation, admin API (masking, SSRF, secret reuse), status, wiring."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import media_server
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_media_client, get_plex_client
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config, ConfigError
from trackseerr.media_servers import JellyfinMediaServer, capabilities_for, registry
from trackseerr.role_guard import GATEWAY_FORBIDDEN_ENV, check_role_environment
from trackseerr.storage import Database
from tests.jellyfin_fake import API_KEY, FakeJellyfin, default_state

URL = "http://jellyfin:8096"


def jf(**kw) -> Config:
    base = dict(plex_url="", plex_token="", jellyfin_url=URL, jellyfin_api_key="k")
    base.update(kw)
    return Config(**base)


# ----------------------------------------------------------------------------- env validation


def test_explicit_jellyfin_is_valid_and_user_is_optional():
    cfg = jf()
    cfg.validate_media_server()
    assert [r.kind for r in registry.env_records(cfg)] == ["jellyfin"]


@pytest.mark.parametrize(
    "kw,needle", [({"jellyfin_url": ""}, "JELLYFIN_URL"), ({"jellyfin_api_key": ""}, "JELLYFIN_API_KEY")]
)
def test_incomplete_jellyfin_fails_loudly(kw, needle):
    with pytest.raises(ConfigError, match=needle):
        jf(**kw).validate_media_server()


def test_from_env_reads_jellyfin_variables(monkeypatch):
    for name, value in {"JELLYFIN_URL": f" {URL} ", "JELLYFIN_API_KEY": " key ", "JELLYFIN_USER": " bob "}.items():
        monkeypatch.setenv(name, value)
    cfg = Config.from_env()
    assert (cfg.jellyfin_url, cfg.jellyfin_api_key, cfg.jellyfin_user) == (URL, "key", "bob")
    assert [r.kind for r in registry.env_records(cfg)] == ["jellyfin"]


def test_jellyfin_key_is_forbidden_on_the_gateway():
    assert "JELLYFIN_API_KEY" in GATEWAY_FORBIDDEN_ENV
    problems = check_role_environment("gateway", {"JELLYFIN_API_KEY": "x", "ROLE": "gateway"})
    assert any("JELLYFIN_API_KEY" in p.message for p in problems)


# ----------------------------------------------------------------------------- factory & dependency


def test_capabilities_for_jellyfin_enable_users():
    assert capabilities_for("jellyfin").to_dict() == {"playlists": True, "users": True, "mixes": False, "library_refresh": True, "file_paths": True}


def test_get_media_client_returns_the_jellyfin_adapter():
    assert isinstance(get_media_client(jf(), None), JellyfinMediaServer)


# ----------------------------------------------------------------------------- API


def _headers(db, cfg, user):
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=get_or_create_secret_key(data_dir=cfg.data_dir)
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(tmp_path):
    db = Database(":memory:")
    cfg = Config(plex_url="", plex_token="", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_plex_client] = lambda: None
    admin = db.upsert_user("a1", "admin", "a@x", is_admin=True)
    media_server.reset_probe_cache()
    yield SimpleNamespace(tc=TestClient(app), db=db, cfg=cfg, admin=_headers(db, cfg, admin), app=app)
    media_server.reset_probe_cache()


def test_status_endpoint_reports_jellyfin_capabilities(env):
    env.cfg.jellyfin_url, env.cfg.jellyfin_api_key = URL, API_KEY
    fake = FakeJellyfin(default_state([]))
    adapter = JellyfinMediaServer(URL, API_KEY, transport=fake.transport(), sleep=lambda _s: None)
    with patch("trackseerr.media_servers.registry.build_adapter", return_value=adapter):
        body = env.tc.get("/api/system/media-server").json()
    assert body == {
        "type": "jellyfin",
        "connected": True,
        "capabilities": {"playlists": True, "users": True, "mixes": False, "library_refresh": True, "file_paths": True},
        "kinds": ["jellyfin"],
        "all_capabilities": {
            "playlists": True,
            "users": True,
            "mixes": False,
            "library_refresh": True,
            "file_paths": True,
        },
    }


def test_status_endpoint_unreachable(env):
    env.cfg.jellyfin_url, env.cfg.jellyfin_api_key = URL, "k"
    with patch("trackseerr.media_servers.registry.build_adapter", return_value=None):
        body = env.tc.get("/api/system/media-server").json()
    assert body["type"] == "jellyfin" and body["connected"] is False


def test_user_refresh_discovers_jellyfin_users(env):
    env.cfg.jellyfin_url, env.cfg.jellyfin_api_key = URL, API_KEY
    fake = FakeJellyfin(default_state([]))
    adapter = JellyfinMediaServer(URL, API_KEY, transport=fake.transport(), sleep=lambda _s: None)
    env.app.dependency_overrides[get_media_client] = lambda: adapter
    res = env.tc.post("/api/users/refresh", headers=env.admin)
    assert res.status_code == 200, res.text
    names = {u["username"] for u in res.json()}
    assert {"admin", "kid"} <= names


# ----------------------------------------------------------------------------- startup user discovery


def test_discover_media_server_users_populates_the_user_table():
    from trackseerr.cli import _discover_media_server_users

    db = Database(":memory:")
    fake = FakeJellyfin(default_state([]))
    adapter = JellyfinMediaServer(URL, API_KEY, transport=fake.transport(), sleep=lambda _s: None)
    _discover_media_server_users(db, adapter)
    users = {u["username"]: u for u in db.list_users()}
    assert {"admin", "kid"} <= set(users)
    # a Jellyfin administrator is NOT a Trackseerr administrator, and imported accounts have no Trackseerr login
    assert not users["admin"]["is_admin"] and not users["kid"]["is_admin"]
    assert users["admin"]["auth_type"] == "jellyfin" and users["kid"]["auth_type"] == "jellyfin"


def test_discover_media_server_users_survives_an_unreachable_server():
    from trackseerr.cli import _discover_media_server_users

    db = Database(":memory:")
    adapter = JellyfinMediaServer(URL, "wrong-key", transport=FakeJellyfin(default_state([])).transport(), sleep=lambda _s: None)
    _discover_media_server_users(db, adapter)  # auth error is logged, not raised
    assert db.list_users() == []

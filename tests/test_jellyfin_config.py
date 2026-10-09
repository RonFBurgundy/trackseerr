"""Jellyfin media-server configuration: env validation, Settings overlay, admin API (masking, SSRF, secret reuse), status, wiring."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import media_server
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_media_client, get_plex_client
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config, ConfigError, set_media_server_overlay
from trackseerr.media_servers import JellyfinMediaServer, build_jellyfin, build_media_server, capabilities_for
from trackseerr.media_servers import settings as ms_settings
from trackseerr.role_guard import GATEWAY_FORBIDDEN_ENV, check_role_environment, core_like_reasons
from trackseerr.storage import Database
from tests.jellyfin_fake import API_KEY, FakeJellyfin, default_state

URL = "http://jellyfin:8096"


def jf(**kw) -> Config:
    base = dict(plex_url="", plex_token="", media_server="jellyfin", jellyfin_url=URL, jellyfin_api_key="k")
    base.update(kw)
    return Config(**base)


# ----------------------------------------------------------------------------- env validation


def test_explicit_jellyfin_is_valid_and_user_is_optional():
    cfg = jf()
    cfg.validate_media_server()
    assert cfg.media_server_type == "jellyfin" and cfg.jellyfin_configured and not cfg.plex_enabled and not cfg.subsonic_configured


@pytest.mark.parametrize(
    "kw,needle", [({"jellyfin_url": ""}, "JELLYFIN_URL"), ({"jellyfin_api_key": ""}, "JELLYFIN_API_KEY")]
)
def test_incomplete_jellyfin_fails_loudly(kw, needle):
    with pytest.raises(ConfigError, match=needle):
        jf(**kw).validate_media_server()


def test_jellyfin_variables_without_media_server_choice_fail_loudly():
    with pytest.raises(ConfigError, match="MEDIA_SERVER=jellyfin"):
        Config(plex_url="", plex_token="", jellyfin_url=URL).validate_media_server()


def test_from_env_reads_jellyfin_variables(monkeypatch):
    for name, value in {"MEDIA_SERVER": "jellyfin", "JELLYFIN_URL": f" {URL} ", "JELLYFIN_API_KEY": " key ", "JELLYFIN_USER": " bob "}.items():
        monkeypatch.setenv(name, value)
    cfg = Config.from_env()
    assert (cfg.jellyfin_url, cfg.jellyfin_api_key, cfg.jellyfin_user) == (URL, "key", "bob")
    assert cfg.media_server_env_controlled and cfg.media_server_source == "env"


def test_jellyfin_key_is_forbidden_on_the_gateway():
    assert "JELLYFIN_API_KEY" in GATEWAY_FORBIDDEN_ENV
    problems = check_role_environment("gateway", {"JELLYFIN_API_KEY": "x", "ROLE": "gateway"})
    assert any("JELLYFIN_API_KEY" in p.message for p in problems)


def test_gateway_db_with_saved_jellyfin_key_is_core_like():
    db = Database(":memory:")
    db.save_media_server_settings({"type": "jellyfin", "url": URL, "api_key": "k"})
    assert "a media server API key" in core_like_reasons(db.conn)


# ----------------------------------------------------------------------------- settings overlay & validation


def test_settings_overlay_applies_and_env_wins(monkeypatch):
    for name in ("MEDIA_SERVER", "PLEX_URL", "PLEX_TOKEN", "JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_USER"):
        monkeypatch.delenv(name, raising=False)
    set_media_server_overlay({"type": "jellyfin", "url": URL, "username": "bob", "api_key": "k"})
    cfg = Config.from_env()
    assert cfg.jellyfin_configured and cfg.jellyfin_user == "bob" and cfg.media_server_source == "settings"
    cfg.validate_media_server()
    monkeypatch.setenv("MEDIA_SERVER", "none")
    env_cfg = Config.from_env()
    assert env_cfg.media_server_type == "none" and not env_cfg.jellyfin_url
    set_media_server_overlay(None)


def test_reapplying_overlay_clears_jellyfin_fields():
    cfg = Config(plex_url="", plex_token="")
    cfg.apply_media_server_overlay({"type": "jellyfin", "url": URL, "api_key": "k"})
    assert cfg.jellyfin_configured
    cfg.apply_media_server_overlay({"type": "none"})
    assert not cfg.jellyfin_url and not cfg.jellyfin_api_key


def test_validate_and_masking():
    ok = ms_settings.validate({"type": "jellyfin", "url": URL, "username": " bob ", "password": "ignored", "api_key": " k "})
    assert ok == {"type": "jellyfin", "url": URL, "username": "bob", "password": "", "api_key": "k"}
    for bad in ({"type": "jellyfin", "url": URL}, {"type": "jellyfin", "url": "jellyfin", "api_key": "k"}):
        with pytest.raises(ms_settings.MediaServerSettingsError):
            ms_settings.validate(bad)
    db = Database(":memory:")
    ms_settings.save(db, {"type": "jellyfin", "url": URL, "api_key": "k1", "username": "a"})
    assert ms_settings.save(db, {"type": "jellyfin", "url": URL + "/", "api_key": "********", "username": "b"})["api_key"] == "k1"
    assert ms_settings.present(Config(plex_url="", plex_token=""), db)["api_key"] == "********"


def test_masked_key_is_refused_for_a_different_url():
    db = Database(":memory:")
    ms_settings.save(db, {"type": "jellyfin", "url": URL, "api_key": "k1"})
    with pytest.raises(ms_settings.SecretReuseError):
        ms_settings.save(db, {"type": "jellyfin", "url": "http://evil:8096", "api_key": "********"})
    assert db.get_media_server_settings()["url"] == URL
    with pytest.raises(ms_settings.SecretReuseError):  # switching over from another server type is not "the same server"
        ms_settings.merge_secrets({"type": "jellyfin", "url": URL, "api_key": "********"}, {"type": "subsonic", "url": URL, "api_key": "x"})


# ----------------------------------------------------------------------------- factory & dependency


def test_build_jellyfin_shares_one_adapter_per_configuration():
    a = build_jellyfin(jf())
    assert isinstance(a, JellyfinMediaServer) and build_jellyfin(jf()) is a
    assert build_jellyfin(jf(jellyfin_api_key="other")) is not a
    assert build_jellyfin(jf(jellyfin_api_key="")) is None
    assert isinstance(build_media_server(jf()), JellyfinMediaServer)
    assert build_media_server(Config(plex_url="", plex_token="")) is None


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


BODY = {"type": "jellyfin", "url": URL, "username": "bob", "password": "", "api_key": "sekret-key"}
ENV_VARS = ("MEDIA_SERVER", "PLEX_URL", "PLEX_TOKEN", "JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_USER")


def test_save_masks_key_activates_and_keeps_it_for_the_same_url(env, monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    res = env.tc.put("/api/settings/media-server", json=BODY, headers=env.admin)
    assert res.status_code == 200, res.text
    assert res.json()["api_key"] == "********" and "sekret-key" not in res.text
    assert res.json()["effective_type"] == "jellyfin" and Config.from_env().jellyfin_configured
    again = env.tc.put("/api/settings/media-server", json={**BODY, "api_key": "********", "username": "kid", "url": URL + "/"}, headers=env.admin)
    assert again.status_code == 200 and env.db.get_media_server_settings()["api_key"] == "sekret-key"
    assert env.db.get_media_server_settings()["username"] == "kid"


def test_masked_key_with_a_new_url_is_rejected_with_400(env, monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    env.tc.put("/api/settings/media-server", json=BODY, headers=env.admin)
    moved = {**BODY, "api_key": "********", "url": "http://other.example:8096"}
    assert env.tc.put("/api/settings/media-server", json=moved, headers=env.admin).status_code == 400
    assert env.tc.post("/api/settings/media-server/test", json=moved, headers=env.admin).status_code == 400
    assert env.db.get_media_server_settings()["url"] == URL


def test_unsafe_or_incomplete_jellyfin_saves_are_rejected(env):
    assert env.tc.put("/api/settings/media-server", json={**BODY, "api_key": ""}, headers=env.admin).status_code == 422
    assert env.tc.put("/api/settings/media-server", json={**BODY, "url": "http://169.254.169.254"}, headers=env.admin).status_code == 400
    assert env.db.get_media_server_settings()["type"] == ""


def test_locked_by_env_refuses_save(env):
    env.cfg.media_server, env.cfg.jellyfin_url, env.cfg.jellyfin_api_key = "jellyfin", URL, "k"
    assert env.tc.put("/api/settings/media-server", json=BODY, headers=env.admin).status_code == 409


def test_test_connection_ok_and_bad_key(env, monkeypatch):
    fake = FakeJellyfin(default_state([]))
    real = JellyfinMediaServer
    monkeypatch.setattr(
        "trackseerr.api.routes.settings.JellyfinMediaServer",
        lambda *a, **kw: real(*a, transport=fake.transport(), sleep=lambda _s: None, **kw),
    )
    ok = env.tc.post("/api/settings/media-server/test", json={**BODY, "api_key": API_KEY}, headers=env.admin).json()
    assert ok["ok"] is True and "Connected" in ok["message"]
    bad = env.tc.post("/api/settings/media-server/test", json=BODY, headers=env.admin).json()
    assert bad["ok"] is False and "sekret-key" not in bad["message"]
    assert env.tc.post("/api/settings/media-server/test", json={**BODY, "api_key": ""}, headers=env.admin).json()["ok"] is False


def test_status_endpoint_reports_jellyfin_capabilities(env):
    env.cfg.media_server, env.cfg.jellyfin_url, env.cfg.jellyfin_api_key = "jellyfin", URL, API_KEY
    fake = FakeJellyfin(default_state([]))
    adapter = JellyfinMediaServer(URL, API_KEY, transport=fake.transport(), sleep=lambda _s: None)
    with patch("trackseerr.api.routes.system.status.build_jellyfin", return_value=adapter):
        body = env.tc.get("/api/system/media-server").json()
    assert body == {
        "type": "jellyfin",
        "connected": True,
        "capabilities": {"playlists": True, "users": True, "mixes": False, "library_refresh": True, "file_paths": True},
    }


def test_status_endpoint_unreachable(env):
    env.cfg.media_server, env.cfg.jellyfin_url, env.cfg.jellyfin_api_key = "jellyfin", URL, "k"
    with patch("trackseerr.api.routes.system.status.build_jellyfin", return_value=None):
        body = env.tc.get("/api/system/media-server").json()
    assert body["type"] == "jellyfin" and body["connected"] is False


def test_user_refresh_discovers_jellyfin_users(env):
    env.cfg.media_server, env.cfg.jellyfin_url, env.cfg.jellyfin_api_key = "jellyfin", URL, API_KEY
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

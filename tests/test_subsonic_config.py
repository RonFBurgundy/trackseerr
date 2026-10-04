"""Subsonic media-server configuration: env validation, the Settings-page overlay, the admin API, status and wiring."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import media_server
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db, get_media_client, get_plex_client
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config, ConfigError, get_media_server_overlay, set_media_server_overlay
from plex_playlist_sync.media_servers import SubsonicMediaServer, build_subsonic, capabilities_for
from plex_playlist_sync.media_servers import settings as ms_settings
from plex_playlist_sync.role_guard import GATEWAY_FORBIDDEN_ENV, check_role_environment, core_like_reasons
from plex_playlist_sync.storage import Database
from tests.subsonic_fake import FakeSubsonic, default_state

URL = "http://navidrome:4533"


def sub(**kw) -> Config:
    base = dict(plex_url="", plex_token="", media_server="subsonic", subsonic_url=URL, subsonic_user="u", subsonic_password="p")
    base.update(kw)
    return Config(**base)


# ----------------------------------------------------------------------------- env validation


def test_explicit_subsonic_with_credentials_is_valid():
    cfg = sub()
    cfg.validate_media_server()
    assert cfg.media_server_type == "subsonic" and cfg.subsonic_configured and not cfg.plex_enabled


def test_api_key_alone_is_enough():
    cfg = sub(subsonic_user="", subsonic_password="", subsonic_api_key="k")
    cfg.validate_media_server()
    assert cfg.subsonic_configured


@pytest.mark.parametrize(
    "kw,needle",
    [
        ({"subsonic_url": ""}, "SUBSONIC_URL"),
        ({"subsonic_password": ""}, "SUBSONIC_PASSWORD"),
        ({"subsonic_user": ""}, "SUBSONIC_USER"),
        ({"subsonic_user": "", "subsonic_password": ""}, "SUBSONIC_USER and SUBSONIC_PASSWORD"),
    ],
)
def test_explicit_subsonic_without_complete_credentials_fails_loudly(kw, needle):
    with pytest.raises(ConfigError, match=needle):
        sub(**kw).validate_media_server()


def test_subsonic_variables_without_media_server_choice_fail_loudly():
    with pytest.raises(ConfigError, match="MEDIA_SERVER=subsonic"):
        Config(plex_url="", plex_token="", subsonic_url=URL).validate_media_server()


def test_from_env_reads_subsonic_variables(monkeypatch):
    for name, value in {
        "MEDIA_SERVER": "subsonic",
        "SUBSONIC_URL": f" {URL} ",
        "SUBSONIC_USER": "bob",
        "SUBSONIC_PASSWORD": " pass with spaces ",
        "SUBSONIC_API_KEY": "",
    }.items():
        monkeypatch.setenv(name, value)
    cfg = Config.from_env()
    assert (cfg.subsonic_url, cfg.subsonic_user, cfg.subsonic_password) == (URL, "bob", " pass with spaces ")
    assert cfg.media_server_env_controlled and cfg.media_server_source == "env"


def test_subsonic_secrets_are_forbidden_on_the_gateway():
    assert {"SUBSONIC_PASSWORD", "SUBSONIC_API_KEY"} <= set(GATEWAY_FORBIDDEN_ENV)
    problems = check_role_environment("gateway", {"SUBSONIC_PASSWORD": "x", "ROLE": "gateway"})
    assert any("SUBSONIC_PASSWORD" in p.message for p in problems)


def test_gateway_db_with_saved_media_server_secret_is_core_like():
    db = Database(":memory:")
    assert not any("media server" in r for r in core_like_reasons(db.conn))
    db.save_media_server_settings({"type": "subsonic", "url": URL, "username": "u", "password": "p"})
    assert "a media server password" in core_like_reasons(db.conn)


# ----------------------------------------------------------------------------- Settings overlay


def test_settings_overlay_applies_when_env_is_silent(monkeypatch):
    for name in ("MEDIA_SERVER", "PLEX_URL", "PLEX_TOKEN", "SUBSONIC_URL", "SUBSONIC_USER", "SUBSONIC_PASSWORD", "SUBSONIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    set_media_server_overlay({"type": "subsonic", "url": URL, "username": "u", "password": "p"})
    cfg = Config.from_env()
    assert cfg.media_server_type == "subsonic" and cfg.subsonic_configured and cfg.media_server_source == "settings"
    assert not cfg.media_server_env_controlled
    cfg.validate_media_server()  # never blocks boot
    set_media_server_overlay(None)
    assert Config.from_env().media_server_type == "none"


@pytest.mark.parametrize("env", [{"PLEX_URL": "http://plex", "PLEX_TOKEN": "t"}, {"MEDIA_SERVER": "none"}])
def test_environment_wins_over_saved_settings(monkeypatch, env):
    for name in ("MEDIA_SERVER", "PLEX_URL", "PLEX_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    set_media_server_overlay({"type": "subsonic", "url": URL, "username": "u", "password": "p"})
    cfg = Config.from_env()
    assert cfg.media_server_source == "env" and cfg.media_server_env_controlled
    assert cfg.media_server_type != "subsonic" and not cfg.subsonic_url


def test_reapplying_overlay_on_a_live_config_follows_changes():
    cfg = Config(plex_url="", plex_token="")
    cfg.apply_media_server_overlay({"type": "subsonic", "url": URL, "username": "u", "password": "p"})
    assert cfg.subsonic_configured
    cfg.apply_media_server_overlay({"type": "none"})
    assert cfg.media_server_type == "none" and not cfg.subsonic_url and not cfg.subsonic_password
    cfg.apply_media_server_overlay({})
    assert cfg.media_server_source == "env" and cfg.media_server_type == "none"


def test_db_roundtrip_and_validation():
    db = Database(":memory:")
    assert db.get_media_server_settings() == {"type": "", "url": "", "username": "", "password": "", "api_key": ""}
    saved = ms_settings.save(db, {"type": "subsonic", "url": URL, "username": "u", "password": "p", "api_key": ""})
    assert saved["password"] == "p" and get_media_server_overlay()["type"] == "subsonic"
    # masked secret keeps the stored value
    again = ms_settings.save(db, {"type": "subsonic", "url": URL, "username": "u2", "password": "********", "api_key": ""})
    assert again["password"] == "p" and again["username"] == "u2"
    # switching to none clears everything
    assert ms_settings.save(db, {"type": "none"}) == {"type": "none", "url": "", "username": "", "password": "", "api_key": ""}
    for bad in (
        {"type": "subsonic", "url": "navidrome", "username": "u", "password": "p"},
        {"type": "subsonic", "url": URL, "username": "u"},
        {"type": "plex"},
        {"type": "jellyfin"},
    ):
        with pytest.raises(ms_settings.MediaServerSettingsError):
            ms_settings.validate(bad)


def test_save_notifies_listeners_and_survives_a_failing_one():
    db = Database(":memory:")
    calls = []
    ms_settings.on_change(lambda: (_ for _ in ()).throw(ValueError("boom")))
    ms_settings.on_change(lambda: calls.append(1))
    ms_settings.save(db, {"type": "none"})
    assert calls == [1]


# ----------------------------------------------------------------------------- factory & dependency


def test_build_subsonic_shares_one_adapter_per_configuration():
    a, b = build_subsonic(sub()), build_subsonic(sub())
    assert isinstance(a, SubsonicMediaServer) and a is b
    c = build_subsonic(sub(subsonic_password="other"))
    assert c is not a
    assert build_subsonic(sub(subsonic_password="")) is None
    assert build_subsonic(Config(plex_url="", plex_token="")) is None


def test_capabilities_for_subsonic_hides_plex_only_features():
    assert capabilities_for("subsonic").to_dict() == {
        "playlists": True,
        "users": False,
        "mixes": False,
        "library_refresh": True,
    }


def test_get_media_client_prefers_plex_then_subsonic_then_none():
    sentinel = object()
    assert get_media_client(sub(), sentinel) is sentinel
    assert isinstance(get_media_client(sub(), None), SubsonicMediaServer)
    assert get_media_client(Config(plex_url="", plex_token=""), None) is None


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
    alice = db.upsert_user("u1", "alice", "u@x", is_admin=False)
    media_server.reset_probe_cache()
    yield SimpleNamespace(
        tc=TestClient(app), db=db, cfg=cfg, admin=_headers(db, cfg, admin), alice=_headers(db, cfg, alice), app=app
    )
    media_server.reset_probe_cache()


BODY = {"type": "subsonic", "url": URL, "username": "bob", "password": "hunter2", "api_key": ""}


def test_settings_requires_admin(env):
    assert env.tc.get("/api/settings/media-server").status_code in (401, 403)
    assert env.tc.get("/api/settings/media-server", headers=env.alice).status_code == 403
    assert env.tc.put("/api/settings/media-server", json=BODY, headers=env.alice).status_code == 403
    assert env.tc.post("/api/settings/media-server/test", json=BODY, headers=env.alice).status_code == 403


def test_save_masks_secrets_and_activates(env, monkeypatch):
    for name in ("MEDIA_SERVER", "PLEX_URL", "PLEX_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    res = env.tc.put("/api/settings/media-server", json=BODY, headers=env.admin)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["password"] == "********" and "hunter2" not in res.text
    assert body["effective_type"] == "subsonic" and body["locked_by_env"] is False
    assert env.db.get_media_server_settings()["password"] == "hunter2"
    assert "hunter2" not in env.tc.get("/api/settings/media-server", headers=env.admin).text
    # the round trip of the masked value keeps the stored secret
    again = env.tc.put("/api/settings/media-server", json={**BODY, "password": "********", "username": "bobby"}, headers=env.admin)
    assert again.status_code == 200 and env.db.get_media_server_settings()["password"] == "hunter2"
    assert env.db.get_media_server_settings()["username"] == "bobby"
    assert Config.from_env().subsonic_configured


def test_save_none_clears(env, monkeypatch):
    for name in ("MEDIA_SERVER", "PLEX_URL", "PLEX_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    env.tc.put("/api/settings/media-server", json=BODY, headers=env.admin)
    res = env.tc.put("/api/settings/media-server", json={"type": "none"}, headers=env.admin)
    assert res.status_code == 200 and res.json()["effective_type"] == "none"
    assert env.db.get_media_server_settings()["password"] == ""


def test_incomplete_and_unsafe_saves_are_rejected(env):
    assert env.tc.put("/api/settings/media-server", json={**BODY, "password": ""}, headers=env.admin).status_code == 422
    assert env.tc.put("/api/settings/media-server", json={**BODY, "url": "ftp://x"}, headers=env.admin).status_code == 400
    unsafe = env.tc.put("/api/settings/media-server", json={**BODY, "url": "http://169.254.169.254"}, headers=env.admin)
    assert unsafe.status_code == 400
    assert env.tc.put("/api/settings/media-server", json={**BODY, "type": "plex"}, headers=env.admin).status_code == 422
    assert env.db.get_media_server_settings()["type"] == ""


def test_save_is_refused_while_the_environment_configures_a_server(env):
    env.cfg.media_server = "plex"
    env.cfg.plex_url, env.cfg.plex_token = "http://plex", "t"
    res = env.tc.put("/api/settings/media-server", json=BODY, headers=env.admin)
    assert res.status_code == 409
    assert env.tc.get("/api/settings/media-server", headers=env.admin).json()["locked_by_env"] is True


@pytest.fixture
def fake_server(monkeypatch):
    fake = FakeSubsonic(default_state([("Song A", "Artist 1", "Album X")]))
    real = SubsonicMediaServer

    def factory(*a, **kw):
        return real(*a, transport=fake.transport(), sleep=lambda _s: None, **kw)

    monkeypatch.setattr("plex_playlist_sync.api.routes.settings.SubsonicMediaServer", factory)
    return fake


def test_test_connection_reports_ok_and_failure(env, fake_server):
    fake_server.state.users["bob"] = fake_server.state.users["admin"].__class__("bob", True, "hunter2")
    ok = env.tc.post("/api/settings/media-server/test", json=BODY, headers=env.admin).json()
    assert ok["ok"] is True and "Connected" in ok["message"]
    bad = env.tc.post("/api/settings/media-server/test", json={**BODY, "password": "wrong"}, headers=env.admin).json()
    assert bad["ok"] is False and "40" in bad["message"] and "wrong" not in bad["message"]


def test_test_connection_resolves_masked_secret_from_saved(env, fake_server, monkeypatch):
    fake_server.state.users["bob"] = fake_server.state.users["admin"].__class__("bob", True, "hunter2")
    for name in ("MEDIA_SERVER", "PLEX_URL", "PLEX_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    env.tc.put("/api/settings/media-server", json=BODY, headers=env.admin)
    res = env.tc.post("/api/settings/media-server/test", json={**BODY, "password": "********"}, headers=env.admin).json()
    assert res["ok"] is True


def test_test_connection_validates_input(env):
    assert env.tc.post("/api/settings/media-server/test", json={**BODY, "password": ""}, headers=env.admin).json()["ok"] is False
    unsafe = env.tc.post("/api/settings/media-server/test", json={**BODY, "url": "http://169.254.169.254"}, headers=env.admin).json()
    assert unsafe["ok"] is False and "SSRF" in unsafe["message"]
    assert env.tc.post("/api/settings/media-server/test", json={"type": "none"}, headers=env.admin).json()["ok"] is False


def test_status_endpoint_reports_subsonic(env):
    env.cfg.media_server = "subsonic"
    env.cfg.subsonic_url, env.cfg.subsonic_user, env.cfg.subsonic_password = URL, "u", "p"
    fake = FakeSubsonic(default_state([]))
    fake.state.users["u"] = fake.state.users["admin"].__class__("u", True, "p")
    adapter = SubsonicMediaServer(URL, "u", "p", transport=fake.transport(), sleep=lambda _s: None)
    with patch("plex_playlist_sync.api.routes.system.build_subsonic", return_value=adapter):
        body = env.tc.get("/api/system/media-server").json()
    assert body == {
        "type": "subsonic",
        "connected": True,
        "capabilities": {"playlists": True, "users": False, "mixes": False, "library_refresh": True},
    }


def test_status_endpoint_subsonic_unreachable(env):
    env.cfg.media_server = "subsonic"
    env.cfg.subsonic_url, env.cfg.subsonic_user, env.cfg.subsonic_password = URL, "u", "p"
    with patch("plex_playlist_sync.api.routes.system.build_subsonic", return_value=None):
        body = env.tc.get("/api/system/media-server").json()
    assert body["type"] == "subsonic" and body["connected"] is False


def test_generic_search_route_works_on_subsonic(env):
    env.cfg.media_server = "subsonic"
    env.cfg.subsonic_url, env.cfg.subsonic_user, env.cfg.subsonic_password = URL, "u", "p"
    fake = FakeSubsonic(default_state([("Song A", "Artist 1", "Album X")]))
    fake.state.users["u"] = fake.state.users["admin"].__class__("u", True, "p")
    adapter = SubsonicMediaServer(URL, "u", "p", transport=fake.transport(), sleep=lambda _s: None)
    with patch("plex_playlist_sync.api.dependencies.build_subsonic", return_value=adapter):
        res = env.tc.get("/api/missing/search", params={"query": "song"}, headers=env.admin)
    assert res.status_code == 200 and [t["title"] for t in res.json()] == ["Song A"]


def test_plex_only_routes_do_not_get_a_subsonic_client(env):
    env.app.dependency_overrides.pop(get_plex_client, None)
    env.cfg.media_server = "subsonic"
    env.cfg.subsonic_url, env.cfg.subsonic_user, env.cfg.subsonic_password = URL, "u", "p"
    from plex_playlist_sync.api.dependencies import get_plex_client as real_get_plex_client

    assert real_get_plex_client(env.cfg) is None  # plex_enabled is False whenever Subsonic is the active server

"""Subsonic media-server configuration: env validation, the admin API, status and wiring."""

import logging
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import media_server
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_media_client, get_plex_client
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config, ConfigError
from trackseerr.media_servers import SubsonicMediaServer, capabilities_for, registry
from trackseerr.role_guard import GATEWAY_FORBIDDEN_ENV, check_role_environment, core_like_reasons
from trackseerr.storage import Database
from tests.subsonic_fake import FakeSubsonic, default_state

URL = "http://navidrome:4533"


def sub(**kw) -> Config:
    base = dict(plex_url="", plex_token="", subsonic_url=URL, subsonic_user="u", subsonic_password="p")
    base.update(kw)
    return Config(**base)


# ----------------------------------------------------------------------------- env validation


def test_explicit_subsonic_with_credentials_is_valid():
    cfg = sub()
    cfg.validate_media_server()
    assert [r.kind for r in registry.env_records(cfg)] == ["subsonic"]


def test_api_key_alone_is_enough():
    cfg = sub(subsonic_user="", subsonic_password="", subsonic_api_key="k")
    cfg.validate_media_server()
    assert [r.kind for r in registry.env_records(cfg)] == ["subsonic"]


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


def test_plex_and_subsonic_env_blocks_both_become_servers():
    cfg = Config(
        plex_url="http://plex:32400",
        plex_token="tok",
        subsonic_url=URL,
        subsonic_user="u",
        subsonic_password="p",
    )
    cfg.validate_media_server()
    recs = registry.env_records(cfg)
    assert [(r.id, r.kind) for r in recs] == [("env-plex", "plex"), ("env-subsonic", "subsonic")]


@pytest.mark.parametrize(
    "kw,needle",
    [
        ({"jellyfin_url": "http://jf:8096"}, "JELLYFIN_API_KEY"),
        ({"jellyfin_api_key": "k"}, "JELLYFIN_URL"),
        ({"jellyfin_user": "bob"}, "JELLYFIN_URL"),
    ],
)
def test_partial_jellyfin_block_is_config_error(kw, needle):
    with pytest.raises(ConfigError, match=needle):
        Config(plex_url="", plex_token="", **kw).validate_media_server()


def test_media_server_variable_is_ignored_with_warning(tmp_path, caplog):
    from trackseerr.cli import main

    env = {
        "MEDIA_SERVER": "none",
        "PLEX_URL": "http://plex:32400",
        "PLEX_TOKEN": "tok",
        "RUN_ONCE": "1",
        "DATA_DIR": str(tmp_path),
        "CONFIG_DIR": str(tmp_path),
    }
    with (
        patch("trackseerr.clients.plex.PlexClient"),
        patch("trackseerr.cli.SyncCoordinator"),
        patch.dict(os.environ, env, clear=True),
        caplog.at_level(logging.WARNING),
    ):
        assert main() == 0
    assert any(
        "MEDIA_SERVER is no longer used: every PLEX_*, SUBSONIC_* and JELLYFIN_* block you set is added "
        "as a server; add or remove servers in Settings." in r.getMessage()
        for r in caplog.records
    )


def test_from_env_reads_subsonic_variables(monkeypatch):
    for name, value in {
        "SUBSONIC_URL": f" {URL} ",
        "SUBSONIC_USER": "bob",
        "SUBSONIC_PASSWORD": " pass with spaces ",
        "SUBSONIC_API_KEY": "",
    }.items():
        monkeypatch.setenv(name, value)
    cfg = Config.from_env()
    assert (cfg.subsonic_url, cfg.subsonic_user, cfg.subsonic_password) == (URL, "bob", " pass with spaces ")
    assert [r.kind for r in registry.env_records(cfg)] == ["subsonic"]


def test_subsonic_secrets_are_forbidden_on_the_gateway():
    assert {"SUBSONIC_PASSWORD", "SUBSONIC_API_KEY"} <= set(GATEWAY_FORBIDDEN_ENV)
    problems = check_role_environment("gateway", {"SUBSONIC_PASSWORD": "x", "ROLE": "gateway"})
    assert any("SUBSONIC_PASSWORD" in p.message for p in problems)


def test_gateway_db_with_saved_media_server_secret_is_core_like():
    db = Database(":memory:")
    assert not any("media server" in r for r in core_like_reasons(db.conn))
    db.conn.execute(
        "INSERT INTO media_server_settings (id, type, url, username, password) VALUES (1, 'subsonic', ?, 'u', 'p')",
        (URL,),
    )
    db.conn.commit()
    assert "a media server password" in core_like_reasons(db.conn)


def test_gateway_db_with_media_servers_secret_is_core_like():
    db = Database(":memory:")
    assert not any("media server" in r for r in core_like_reasons(db.conn))
    db.insert_media_server_row({"kind": "subsonic", "name": "Subsonic", "url": URL, "username": "u", "password": "p"})
    assert "a media server password" in core_like_reasons(db.conn)


# ----------------------------------------------------------------------------- factory & dependency


def test_capabilities_for_subsonic_hides_plex_only_features():
    assert capabilities_for("subsonic").to_dict() == {
        "playlists": True,
        "users": False,
        "mixes": False,
        "library_refresh": True,
        "file_paths": True,
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


def test_status_endpoint_reports_subsonic(env):
    env.cfg.subsonic_url, env.cfg.subsonic_user, env.cfg.subsonic_password = URL, "u", "p"
    fake = FakeSubsonic(default_state([]))
    fake.state.users["u"] = fake.state.users["admin"].__class__("u", True, "p")
    adapter = SubsonicMediaServer(URL, "u", "p", transport=fake.transport(), sleep=lambda _s: None)
    with patch("trackseerr.media_servers.registry.build_adapter", return_value=adapter):
        body = env.tc.get("/api/system/media-server").json()
    assert body == {
        "type": "subsonic",
        "connected": True,
        "capabilities": {"playlists": True, "users": False, "mixes": False, "library_refresh": True, "file_paths": True},
    }


def test_status_endpoint_subsonic_unreachable(env):
    env.cfg.subsonic_url, env.cfg.subsonic_user, env.cfg.subsonic_password = URL, "u", "p"
    with patch("trackseerr.media_servers.registry.build_adapter", return_value=None):
        body = env.tc.get("/api/system/media-server").json()
    assert body["type"] == "subsonic" and body["connected"] is False


def test_generic_search_route_works_on_subsonic(env):
    env.cfg.subsonic_url, env.cfg.subsonic_user, env.cfg.subsonic_password = URL, "u", "p"
    fake = FakeSubsonic(default_state([("Song A", "Artist 1", "Album X")]))
    fake.state.users["u"] = fake.state.users["admin"].__class__("u", True, "p")
    adapter = SubsonicMediaServer(URL, "u", "p", transport=fake.transport(), sleep=lambda _s: None)
    with patch("trackseerr.media_servers.registry.build_adapter", return_value=adapter):
        res = env.tc.get("/api/missing/search", params={"query": "song"}, headers=env.admin)
    assert res.status_code == 200 and [t["title"] for t in res.json()] == ["Song A"]


def test_plex_only_routes_do_not_get_a_subsonic_client(env):
    env.app.dependency_overrides.pop(get_plex_client, None)
    env.cfg.subsonic_url, env.cfg.subsonic_user, env.cfg.subsonic_password = URL, "u", "p"
    from trackseerr.api.dependencies import get_plex_client as real_get_plex_client

    assert real_get_plex_client(env.cfg) is None  # Subsonic is not a Plex server

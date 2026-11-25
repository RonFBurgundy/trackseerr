"""Tests for the process-wide media server registry and adapter cache."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from trackseerr.config import Config
from trackseerr.media_servers import registry
from trackseerr.media_servers.base import MediaServerConnectionError
from trackseerr.storage import Database

URL = "http://navidrome:4533"
PLEX_URL = "http://plex.example.com:32400"


def cfg(**kw: Any) -> Config:
    base: dict[str, Any] = dict(plex_url="", plex_token="")
    base.update(kw)
    return Config(**base)


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    yield
    registry.install(())
    registry.reset_adapters()


class _FakeAdapter:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


def test_env_plex_becomes_locked_login_row() -> None:
    db = Database(":memory:")
    config = cfg(plex_url=PLEX_URL, plex_token="secret-plex-token", plex_music_section="Music")
    registry.reload(config, db)

    recs = registry.records()
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == "env-plex"
    assert rec.kind == "plex"
    assert rec.locked is True
    assert rec.is_login is True
    assert rec.api_key == "secret-plex-token"
    assert rec.music_section == "Music"

    row = db.get_media_server_row("env-plex")
    assert row is not None
    assert row["is_login"] is True
    assert row["password"] == ""
    assert row["api_key"] == ""


def test_env_row_removed_when_env_drops_it() -> None:
    db = Database(":memory:")
    registry.reload(cfg(plex_url=PLEX_URL, plex_token="tok"), db)
    assert db.get_media_server_row("env-plex") is not None

    registry.reload(cfg(), db)
    assert db.get_media_server_row("env-plex") is None
    assert registry.records() == ()


def test_env_secrets_never_written_to_db() -> None:
    db = Database(":memory:")
    config = cfg(
        plex_url=PLEX_URL,
        plex_token="plex-secret",
        subsonic_url=URL,
        subsonic_user="alice",
        subsonic_password="subsonic-secret-password",
        jellyfin_url="http://jellyfin.example.com:8096",
        jellyfin_user="admin",
        jellyfin_api_key="jellyfin-secret-key",
    )
    registry.sync_env_rows(config, db)

    with db.reader() as conn:
        rows = conn.execute("SELECT id, password, api_key FROM media_servers").fetchall()
    assert len(rows) == 3
    for row in rows:
        assert row["password"] == ""
        assert row["api_key"] == ""


def test_settings_rows_load_with_roles() -> None:
    db = Database(":memory:")
    row = db.insert_media_server_row(
        {
            "kind": "subsonic",
            "name": "My Navidrome",
            "url": URL,
            "username": "u",
            "password": "p",
            "use_scrobbles": False,
        }
    )
    recs = registry.load_records(cfg(), db)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.id == row["id"]
    assert rec.locked is False
    assert registry.ROLE_SCROBBLES not in rec.roles
    assert {registry.ROLE_REFRESH, registry.ROLE_PLAYLISTS, registry.ROLE_USERS} <= rec.roles


def test_media_servers_filters_by_role_and_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("trackseerr.media_servers.registry.SubsonicMediaServer", _FakeAdapter)
    db = Database(":memory:")
    disabled = db.insert_media_server_row(
        {
            "kind": "subsonic",
            "name": "Disabled",
            "url": URL,
            "username": "u",
            "password": "p",
            "enabled": False,
            "sort_order": 1,
        }
    )
    no_playlists = db.insert_media_server_row(
        {
            "kind": "subsonic",
            "name": "NoPlaylists",
            "url": URL,
            "username": "u",
            "password": "p",
            "use_playlists": False,
            "sort_order": 2,
        }
    )
    all_roles = db.insert_media_server_row(
        {"kind": "subsonic", "name": "AllRoles", "url": URL, "username": "u", "password": "p", "sort_order": 3}
    )

    registry.reload(cfg(), db)

    enabled_servers = registry.media_servers()
    assert [rec.id for rec, _ in enabled_servers] == [no_playlists["id"], all_roles["id"]]

    playlist_servers = registry.media_servers(role=registry.ROLE_PLAYLISTS)
    assert [rec.id for rec, _ in playlist_servers] == [all_roles["id"]]

    # server_by_id still returns disabled servers if their adapter builds
    found_disabled = registry.server_by_id(disabled["id"])
    assert found_disabled is not None
    assert found_disabled[0].id == disabled["id"]


def test_unknown_role_raises() -> None:
    with pytest.raises(ValueError, match="Unknown media server role"):
        registry.media_servers(role="nonexistent")


def test_adapter_cached_per_record_and_rebuilt_on_change(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("trackseerr.media_servers.registry.SubsonicMediaServer", _FakeAdapter)

    rec1 = registry.ServerRecord(
        id="s1", kind="subsonic", name="S1", source="settings", url=URL, username="u", password="p1"
    )
    rec2 = registry.ServerRecord(
        id="s2", kind="subsonic", name="S2", source="settings", url=URL, username="u", password="p1"
    )

    a1 = registry.build_adapter(rec1)
    a1_again = registry.build_adapter(rec1)
    a2 = registry.build_adapter(rec2)
    assert a1 is not None
    assert a1 is a1_again
    assert a2 is not None
    assert a1 is not a2

    rec1_changed = registry.ServerRecord(
        id="s1", kind="subsonic", name="S1", source="settings", url=URL, username="u", password="p2"
    )
    a1_new = registry.build_adapter(rec1_changed)
    assert a1_new is not None
    assert a1_new is not a1


def test_failed_build_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def flaky_subsonic(*args: Any, **kwargs: Any) -> _FakeAdapter:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MediaServerConnectionError("connection refused", safe_detail="connection refused")
        return _FakeAdapter(*args, **kwargs)

    monkeypatch.setattr("trackseerr.media_servers.registry.SubsonicMediaServer", flaky_subsonic)
    rec = registry.ServerRecord(
        id="s1", kind="subsonic", name="S1", source="settings", url=URL, username="u", password="p"
    )

    assert registry.build_adapter(rec) is None
    adapter = registry.build_adapter(rec)
    assert adapter is not None
    assert calls == 2


def test_unsafe_saved_url_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("trackseerr.media_servers.registry.SubsonicMediaServer", _FakeAdapter)

    unsafe_settings = registry.ServerRecord(
        id="s1",
        kind="subsonic",
        name="Metadata",
        source="settings",
        url="http://169.254.169.254/latest/meta-data",
        username="u",
        password="p",
    )
    assert registry.build_adapter(unsafe_settings) is None

    unsafe_env = registry.ServerRecord(
        id="env-subsonic",
        kind="subsonic",
        name="EnvSubsonic",
        source="env",
        url="http://169.254.169.254/latest/meta-data",
        username="u",
        password="p",
    )
    assert registry.build_adapter(unsafe_env) is not None


def test_reload_keeps_previous_list_on_db_error() -> None:
    initial = registry.ServerRecord(
        id="s1", kind="subsonic", name="Initial", source="settings", url=URL, username="u", password="p"
    )
    registry.install([initial])

    class _FailingDb:
        def get_media_server_row(self, server_id: str) -> None:
            return None

        def list_media_server_rows(self) -> list[dict[str, Any]]:
            raise sqlite3.OperationalError("database is locked")

    registry.reload(cfg(), _FailingDb())
    assert registry.records() == (initial,)


def test_record_repr_hides_secrets() -> None:
    rec = registry.ServerRecord(
        id="s1",
        kind="subsonic",
        name="S1",
        source="settings",
        url=URL,
        username="u",
        password="super-secret-password",
        api_key="super-secret-api-key",
    )
    rendered = repr(rec)
    assert "super-secret-password" not in rendered
    assert "super-secret-api-key" not in rendered


def test_login_server_and_plex_and_jellyfin_builders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("trackseerr.clients.plex.PlexClient", _FakeAdapter)
    monkeypatch.setattr("trackseerr.media_servers.registry.PlexMediaServer", _FakeAdapter)
    monkeypatch.setattr("trackseerr.media_servers.registry.JellyfinMediaServer", _FakeAdapter)

    db = Database(":memory:")
    config = cfg(
        plex_url=PLEX_URL,
        plex_token="tok",
        plex_music_section="Music",
        jellyfin_url="http://jellyfin.example.com:8096",
        jellyfin_user="admin",
        jellyfin_api_key="jf-key",
    )
    registry.reload(config, db)

    login = registry.login_server()
    assert login is not None
    assert login[0].id == "env-plex"

    jf = registry.server_by_id("env-jellyfin")
    assert jf is not None
    assert jf[0].kind == "jellyfin"

    # Disabled login server returns None
    db.update_media_server_row("env-plex", {"enabled": False})
    registry.reload(config, db)
    assert registry.login_server() is None

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
    now = 100.0
    monkeypatch.setattr(registry.time, "monotonic", lambda: now)
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
    now += registry.RETRY_SECONDS + 1.0
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


def test_records_fall_back_to_config_before_install() -> None:
    config = cfg(plex_url=PLEX_URL, plex_token="tok", plex_music_section="Music")
    recs = registry.records(config)
    assert len(recs) == 1
    assert recs[0].id == "env-plex"
    assert recs[0].kind == "plex"
    assert recs[0].locked is True
    assert recs[0].is_login is True

    registry.install(())
    assert registry.records(config) == ()


def test_fallback_prefers_plex_then_subsonic_then_jellyfin() -> None:
    sub_base = dict(
        plex_url="",
        plex_token="",
        media_server="subsonic",
        subsonic_url=URL,
        subsonic_user="u",
        subsonic_password="p",
    )
    both_plex_sub = Config(
        plex_url=PLEX_URL,
        plex_token="tok",
        subsonic_url=URL,
        subsonic_user="u",
        subsonic_password="p",
    )
    assert [r.id for r in registry.fallback_records(both_plex_sub)] == ["env-plex"]

    only_sub = Config(**sub_base)
    sub_recs = registry.fallback_records(only_sub)
    assert [r.id for r in sub_recs] == ["env-subsonic"]
    assert sub_recs[0].kind == "subsonic"
    assert sub_recs[0].source == "env"
    assert sub_recs[0].username == "u"
    assert sub_recs[0].password == "p"

    only_jf = Config(
        plex_url="",
        plex_token="",
        media_server="jellyfin",
        jellyfin_url="http://jellyfin.example.com:8096",
        jellyfin_user="admin",
        jellyfin_api_key="jf-key",
    )
    jf_recs = registry.fallback_records(only_jf)
    assert [r.id for r in jf_recs] == ["env-jellyfin"]
    assert jf_recs[0].kind == "jellyfin"
    assert jf_recs[0].source == "env"
    assert jf_recs[0].username == "admin"
    assert jf_recs[0].api_key == "jf-key"

    assert registry.fallback_records(cfg()) == []


def test_build_does_not_hold_lock_during_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    lock_free_during_init = False

    class _LockCheckingAdapter(_FakeAdapter):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            nonlocal lock_free_during_init
            acquired = registry._adapters_lock.acquire(blocking=False)
            if acquired:
                lock_free_during_init = True
                registry._adapters_lock.release()
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("trackseerr.media_servers.registry.SubsonicMediaServer", _LockCheckingAdapter)
    rec = registry.ServerRecord(
        id="s1", kind="subsonic", name="S1", source="settings", url=URL, username="u", password="p"
    )
    adapter = registry.build_adapter(rec)
    assert adapter is not None
    assert lock_free_during_init is True


def test_failed_build_backs_off_then_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr(registry.time, "monotonic", lambda: now)
    calls = 0

    def flaky_subsonic(*args: Any, **kwargs: Any) -> _FakeAdapter:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MediaServerConnectionError("down", safe_detail="down")
        return _FakeAdapter(*args, **kwargs)

    monkeypatch.setattr("trackseerr.media_servers.registry.SubsonicMediaServer", flaky_subsonic)
    rec = registry.ServerRecord(
        id="s1", kind="subsonic", name="S1", source="settings", url=URL, username="u", password="p"
    )

    assert registry.build_adapter(rec) is None
    assert calls == 1

    now += 10.0
    assert registry.build_adapter(rec) is None
    assert calls == 1

    now += 51.0
    adapter = registry.build_adapter(rec)
    assert adapter is not None
    assert calls == 2


def test_changed_key_ignores_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr(registry.time, "monotonic", lambda: now)
    calls = 0

    def subsonic_builder(url: str, *args: Any, **kwargs: Any) -> _FakeAdapter:
        nonlocal calls
        calls += 1
        if url == URL:
            raise MediaServerConnectionError("down", safe_detail="down")
        return _FakeAdapter(url, *args, **kwargs)

    monkeypatch.setattr("trackseerr.media_servers.registry.SubsonicMediaServer", subsonic_builder)
    rec1 = registry.ServerRecord(
        id="s1", kind="subsonic", name="S1", source="settings", url=URL, username="u", password="p"
    )
    rec2 = registry.ServerRecord(
        id="s1", kind="subsonic", name="S1", source="settings", url="http://navidrome2:4533", username="u", password="p"
    )

    assert registry.build_adapter(rec1) is None
    assert calls == 1
    assert registry.build_adapter(rec2) is not None
    assert calls == 2


def test_primary_is_login_server_and_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_plex(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("plex unreachable")

    monkeypatch.setattr("trackseerr.clients.plex.PlexClient", failing_plex)
    monkeypatch.setattr("trackseerr.media_servers.registry.SubsonicMediaServer", _FakeAdapter)

    plex_rec = registry.ServerRecord(
        id="env-plex", kind="plex", name="Plex", source="env", url=PLEX_URL, api_key="tok", is_login=True
    )
    sub_rec = registry.ServerRecord(
        id="s1", kind="subsonic", name="Sub", source="settings", url=URL, username="u", password="p"
    )
    registry.install([plex_rec, sub_rec])

    assert registry.has_servers() is True
    assert registry.primary_server() is None


def test_primary_without_login_is_first_buildable(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_if_bad_url(url: str, *args: Any, **kwargs: Any) -> _FakeAdapter:
        if "bad" in url:
            raise MediaServerConnectionError("bad", safe_detail="bad")
        return _FakeAdapter(url, *args, **kwargs)

    monkeypatch.setattr("trackseerr.media_servers.registry.SubsonicMediaServer", failing_if_bad_url)
    bad = registry.ServerRecord(
        id="s1", kind="subsonic", name="Bad", source="settings", url="http://bad:4533", username="u", password="p"
    )
    good = registry.ServerRecord(
        id="s2", kind="subsonic", name="Good", source="settings", url=URL, username="u", password="p"
    )
    registry.install([bad, good])

    primary = registry.primary_server()
    assert primary is not None
    assert primary[0].id == "s2"


def test_plex_login_client_and_client_for(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_plex_client = object()

    class _FakePlexAdapter(registry.PlexMediaServer):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def plex_extras(self) -> Any:
            return raw_plex_client

    monkeypatch.setattr("trackseerr.clients.plex.PlexClient", _FakeAdapter)
    monkeypatch.setattr("trackseerr.media_servers.registry.PlexMediaServer", _FakePlexAdapter)
    monkeypatch.setattr("trackseerr.media_servers.registry.SubsonicMediaServer", _FakeAdapter)

    plex_rec = registry.ServerRecord(
        id="env-plex", kind="plex", name="Plex", source="env", url=PLEX_URL, api_key="tok", is_login=True
    )
    sub_rec = registry.ServerRecord(
        id="s1", kind="subsonic", name="Sub", source="settings", url=URL, username="u", password="p"
    )
    registry.install([plex_rec, sub_rec])

    assert registry.plex_login_client() is raw_plex_client
    assert registry.client_for(registry.server_by_id("env-plex")) is raw_plex_client
    sub_pair = registry.server_by_id("s1")
    assert sub_pair is not None
    assert registry.client_for(sub_pair) is sub_pair[1]
    assert registry.client_for(None) is None

    registry.install([sub_rec])
    assert registry.plex_login_client() is None

"""Regression tests for the media-server audit findings (user import, atomic settings, SSRF, secret reuse, run-once)."""

import gc
import logging
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import local_auth, media_server
from trackseerr.admin_bootstrap import ensure_bootstrap_admin
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_plex_client, require_core_tier
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.cli import _Clients, _apply_saved_media_server_settings, _discover_media_server_users
from trackseerr.config import Config, MediaServerView, set_media_server_overlay
from trackseerr.local_login import LoginError, verify_local_login
from trackseerr.media_servers import (
    ServerUser,
    SubsonicMediaServer,
    build_jellyfin,
    build_subsonic,
    import_server_users,
)
from trackseerr.media_servers import settings as ms_settings
from trackseerr.media_servers.base import MediaServerError
from trackseerr.models import Playlist, Track
from trackseerr.security import is_safe_service_url
from trackseerr.storage import Database
from tests.subsonic_fake import FakeSubsonic, default_state

PW = "correct horse battery staple"
SUB_URL = "http://navidrome:4533"


@pytest.fixture(autouse=True)
def _clean_overlay():
    set_media_server_overlay(None)
    yield
    set_media_server_overlay(None)


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def jf_user(uid="jf-1", name="jelly", admin=False) -> ServerUser:
    return ServerUser(id=uid, name=name, is_admin=admin, extra={"email": ""})


def local_admin(db: Database, name="admin") -> dict:
    return db.create_local_user_with_password(name, local_auth.hash_password(PW), 35)


# ----------------------------------------------------------------------------- J3: names, auth type, admin


def test_import_skips_a_name_that_belongs_to_a_different_user(db):
    local = local_admin(db, "Boss")
    imported, skipped = import_server_users(db, "jellyfin", [jf_user("jf-1", "boss"), jf_user("jf-2", "kid")])
    assert (imported, skipped) == (1, 1)
    assert db.get_user("jf-1") is None and db.get_user("jf-2") is not None
    assert db.get_user(local["id"])["auth_type"] == "local"  # untouched


def test_imported_jellyfin_accounts_are_inert_and_never_admin(db):
    import_server_users(db, "jellyfin", [jf_user("jf-1", "root", admin=True)])
    row = db.get_user("jf-1")
    assert row["auth_type"] == "jellyfin" and row["is_admin"] is False and not row["permissions"] & 1
    creds = db.get_local_credentials("jf-1")
    assert not creds["password_hash"]
    with pytest.raises(LoginError):
        verify_local_login(db, username="root", password=PW, totp_code=None, recovery_code=None, client_ip="198.51.100.1")
    import_server_users(db, "jellyfin", [jf_user("12345678", "digits")])  # an all-digit id passes the gateway id shape
    with pytest.raises(PermissionError):
        db.ensure_user("12345678", "digits")


def test_reimport_never_demotes_an_admin_granted_in_the_ui(db):
    import_server_users(db, "jellyfin", [jf_user("jf-1", "root")])
    db.upsert_user("jf-1", "root", None, is_admin=True)  # what the admin UI does
    import_server_users(db, "jellyfin", [jf_user("jf-1", "root", admin=False)])
    assert db.get_user("jf-1")["is_admin"] is True


def test_rows_imported_with_the_old_default_type_are_retyped(db):
    db.upsert_user("jf-1", "jelly", None, is_admin=False)  # stage-4 import: auth_type fell back to 'plex'
    assert db.get_user("jf-1")["auth_type"] == "plex"
    import_server_users(db, "jellyfin", [jf_user("jf-1", "jelly")])
    assert db.get_user("jf-1")["auth_type"] == "jellyfin"


def test_plex_import_keeps_plex_admin_semantics(db):
    import_server_users(db, "plex", [jf_user("1001", "owner", admin=True), jf_user("1002", "kid")])
    assert db.get_user("1001")["is_admin"] and db.get_user("1001")["auth_type"] == "plex"
    assert not db.get_user("1002")["is_admin"]
    import_server_users(db, "plex", [jf_user("1001", "owner", admin=False)])
    assert db.get_user("1001")["is_admin"]


def test_bootstrap_before_import_keeps_a_usable_local_admin(db, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", PW)
    assert ensure_bootstrap_admin(db) is True
    import_server_users(db, "jellyfin", [jf_user("jf-1", "admin", admin=True)])
    assert db.get_user("jf-1") is None  # the colliding Jellyfin name was skipped
    result = verify_local_login(db, username="admin", password=PW, totp_code=None, recovery_code=None, client_ip="198.51.100.2")
    assert result.username == "admin" and db.get_user_by_username("admin")["auth_type"] == "local"
    assert db.count_local_login_admins() == 1


def test_bootstrap_after_a_legacy_import_fails_with_a_distinct_message_and_a_new_name_works(db, monkeypatch, caplog):
    db.upsert_user("jf-1", "admin", None, is_admin=True)
    db.conn.execute("UPDATE users SET auth_type = 'jellyfin' WHERE id = 'jf-1'")
    db.conn.commit()
    monkeypatch.setenv("ADMIN_PASSWORD", PW)
    with caplog.at_level(logging.ERROR):
        assert ensure_bootstrap_admin(db) is False
    assert any("jellyfin user with that name already exists" in r.getMessage() for r in caplog.records)
    assert db.count_local_login_admins() == 0
    monkeypatch.setenv("ADMIN_PASSWORD", PW)
    monkeypatch.setenv("ADMIN_USERNAME", "owner")
    assert ensure_bootstrap_admin(db) is True and db.count_local_login_admins() == 1


def test_get_user_by_username_is_deterministic_and_prefers_local(db):
    for uid, kind, created in (("jf-old", "jellyfin", "2000-01-01 00:00:00"), ("jf-new", "jellyfin", "2001-01-01 00:00:00")):
        db.conn.execute(
            "INSERT INTO users (id, username, is_admin, permissions, auth_type, created_at) VALUES (?, 'ghost', 0, 34, ?, ?)",
            (uid, kind, created),
        )
    db.conn.execute(
        "INSERT INTO users (id, username, is_admin, permissions, auth_type, created_at) "
        "VALUES ('local-x', 'GHOST', 0, 34, 'local', '2010-01-01 00:00:00')"
    )
    db.conn.commit()
    assert db.get_user_by_username("ghost")["id"] == "local-x"  # not the oldest row
    assert [u["id"] for u in db.list_users_by_username("Ghost")] == ["local-x", "jf-old", "jf-new"]
    db.conn.execute("DELETE FROM users WHERE id = 'local-x'")
    db.conn.commit()
    assert db.get_user_by_username("ghost")["id"] == "jf-old"  # oldest, stable


def test_plex_login_is_refused_when_any_same_named_row_is_local(db, tmp_path):
    # An older imported row with the same name would hide the local account from a first-match lookup.
    db.conn.execute(
        "INSERT INTO users (id, username, is_admin, permissions, auth_type, created_at) "
        "VALUES ('jf-old', 'ghost', 0, 34, 'jellyfin', '2000-01-01 00:00:00')"
    )
    db.conn.execute(
        "INSERT INTO users (id, username, is_admin, permissions, auth_type, created_at) "
        "VALUES ('local-x', 'ghost', 0, 34, 'local', '2010-01-01 00:00:00')"
    )
    db.conn.commit()
    cfg = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), role="all-in-one")
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    client = TestClient(app)
    plex_user = {"id": "5555", "username": "ghost", "email": "g@x.tv", "thumb": ""}
    with patch("trackseerr.api.routes.auth.check_plex_pin", return_value="tok"), patch(
        "trackseerr.api.routes.auth.verify_server_access", return_value=(True, False)
    ), patch("trackseerr.api.routes.auth.get_plex_user", return_value=plex_user), patch.dict(
        "os.environ", {"PLEX_MACHINE_IDENTIFIER": "m1"}
    ):
        assert client.post("/api/auth/plex/verify", json={"pin_id": 1}).status_code == 403
    assert db.get_user("5555") is None


# ----------------------------------------------------------------------------- J2: discovery never raises


class _Server:
    kind = "jellyfin"

    def __init__(self, users=None, error=None):
        self._users, self._error = users or [], error

    def list_users(self):
        if self._error is not None:
            raise self._error
        return self._users


@pytest.mark.parametrize("error", [RuntimeError("boom"), ValueError("bad payload"), MediaServerError("x", safe_detail="x")])
def test_discovery_survives_any_list_users_failure(db, error):
    _discover_media_server_users(db, _Server(error=error))
    assert db.list_users() == []


def test_one_bad_account_does_not_stop_the_import(db, monkeypatch):
    real = db.import_media_server_user

    def flaky(user_id, username, *a, **kw):
        if username == "bad":
            raise sqlite3.OperationalError("disk I/O error")
        return real(user_id, username, *a, **kw)

    monkeypatch.setattr(db, "import_media_server_user", flaky)
    _discover_media_server_users(db, _Server([jf_user("1", "bad"), jf_user("2", "good")]))
    assert [u["username"] for u in db.list_users()] == ["good"]


def test_discovery_survives_an_unexpected_database_error(db, monkeypatch):
    monkeypatch.setattr(db, "is_tombstoned", MagicMock(side_effect=RuntimeError("closed")))
    _discover_media_server_users(db, _Server([jf_user("1", "a")]))  # must not raise


# ----------------------------------------------------------------------------- S4: atomic settings, no mid-run close


A = {"type": "subsonic", "url": "http://a.example:4533", "username": "ua", "password": "pa"}
B = {"type": "subsonic", "url": "http://b.example:4533", "username": "ub", "password": "pb"}


def test_overlay_swap_never_exposes_a_mixture():
    cfg = Config(plex_url="", plex_token="")
    cfg.apply_media_server_overlay(A)
    valid = {("http://a.example:4533", "ua", "pa"), ("http://b.example:4533", "ub", "pb")}
    stop = threading.Event()
    seen: list[tuple[str, str, str]] = []

    def writer():
        flip = False
        while not stop.is_set():
            cfg.apply_media_server_overlay(B if flip else A)
            flip = not flip

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(20000):
            view = cfg.media_server_view()
            assert isinstance(view, MediaServerView)
            seen.append((view.subsonic_url, view.subsonic_user, view.subsonic_password))
    finally:
        stop.set()
        thread.join()
    assert set(seen) <= valid


def test_built_adapters_never_pair_a_url_with_the_other_credentials():
    cfg = Config(plex_url="", plex_token="")
    cfg.apply_media_server_overlay(A)
    valid = {("http://a.example:4533", "ua", "pa"), ("http://b.example:4533", "ub", "pb")}
    stop = threading.Event()

    def writer():
        flip = False
        while not stop.is_set():
            cfg.apply_media_server_overlay(B if flip else A)
            flip = not flip

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(80):
            adapter = build_subsonic(cfg)
            assert adapter is not None
            assert (adapter._base, adapter._user, adapter._password) in valid
    finally:
        stop.set()
        thread.join()


def test_replacing_the_adapter_does_not_close_the_one_a_sync_holds(monkeypatch):
    fake = FakeSubsonic(default_state([("Song A", "Artist 1", "Album X"), ("Song B", "Artist 1", "Album X")]))
    fake.state.users["ua"] = fake.state.users["admin"].__class__("ua", True, "pa")
    real = SubsonicMediaServer
    monkeypatch.setattr(
        "trackseerr.media_servers.SubsonicMediaServer",
        lambda *a, **kw: real(*a, transport=fake.transport(), sleep=lambda _s: None, **kw),
    )
    cfg = Config(plex_url="", plex_token="")
    cfg.apply_media_server_overlay(A)
    held = build_subsonic(cfg)

    def settings_change_mid_sync(endpoint, n):
        if endpoint == "search3" and n == 1:  # the sync is running: the admin saves new settings now
            cfg.apply_media_server_overlay(B)
            assert build_subsonic(cfg) is not held
        return None

    fake.state.respond_with = settings_change_mid_sync
    playlist = Playlist(id="p", name="Mix", tracks=[Track("Song A", "Artist 1", "Album X"), Track("Song B", "Artist 1", "Album X")])
    from trackseerr.media_servers import PlaylistSyncOptions

    (result,) = held.sync_playlist(playlist, [""], PlaylistSyncOptions())
    assert result.success and not held._http.is_closed
    http = held._http
    del held
    gc.collect()
    assert http.is_closed  # closed once nobody holds the old adapter any more


def test_clients_holder_has_a_lock_for_reconnects():
    clients = _Clients()
    with clients.lock:
        clients.plex = None


# ----------------------------------------------------------------------------- S5: run-once reads the saved choice


def _saved_subsonic_db(path: Path) -> None:
    db = Database(str(path / "sync_db.sqlite"))
    db.save_media_server_settings(
        {"type": "subsonic", "url": SUB_URL, "username": "u", "password": "p", "api_key": "", "credentials_type": "subsonic"}
    )
    db.close()


def test_run_once_loads_the_saved_media_server(tmp_path, monkeypatch):
    _saved_subsonic_db(tmp_path)
    monkeypatch.delenv("CONFIG_DIR", raising=False)
    cfg = Config(plex_url="", plex_token="", data_dir=str(tmp_path), config_dir=str(tmp_path / "nope"))
    with patch("trackseerr.cli.os.path.isdir", return_value=False):
        _apply_saved_media_server_settings(cfg)
    assert cfg.subsonic_configured and cfg.media_server_source == "settings"


def test_run_once_main_connects_with_the_saved_server(tmp_path):
    from trackseerr.cli import main

    _saved_subsonic_db(tmp_path)
    seen: dict[str, bool] = {}

    def connect(config, clients, fatal_plex):
        seen["subsonic"] = config.subsonic_configured
        return True

    env = {"RUN_ONCE": "1", "DATA_DIR": str(tmp_path)}
    with patch.dict("os.environ", env, clear=True), patch("trackseerr.cli._connect_clients", connect), patch(
        "trackseerr.cli.SyncCoordinator"
    ), patch("trackseerr.cli.os.path.isdir", return_value=False):
        assert main() == 0
    assert seen == {"subsonic": True}


def test_run_once_ignores_saved_settings_when_the_environment_names_a_server(tmp_path):
    _saved_subsonic_db(tmp_path)
    cfg = Config(plex_url="http://plex", plex_token="t", data_dir=str(tmp_path))
    with patch("trackseerr.cli.os.path.isdir", return_value=False):
        _apply_saved_media_server_settings(cfg)
    assert cfg.media_server_type == "plex" and not cfg.subsonic_configured


def test_run_once_without_a_database_creates_none_and_keeps_env_behaviour(tmp_path):
    cfg = Config(plex_url="", plex_token="", data_dir=str(tmp_path))
    with patch("trackseerr.cli.os.path.isdir", return_value=False):
        _apply_saved_media_server_settings(cfg)
    assert not (tmp_path / "sync_db.sqlite").exists() and cfg.media_server_type == "none"


# ----------------------------------------------------------------------------- S6: SSRF


@pytest.mark.parametrize(
    "resolved,expected",
    [
        (["169.254.169.254"], False),
        (["100.100.100.200"], False),
        (["0.0.0.0"], False),
        (["127.0.0.1"], False),
        (["fe80::1"], False),
        (["::ffff:169.254.169.254"], False),
        (["192.168.1.20", "169.254.169.254"], False),  # every record counts
        (["10.0.0.5"], True),
        (["192.168.1.20"], True),
        (["172.16.4.2"], True),
        (["93.184.216.34"], True),
        ([], True),  # does not resolve (yet): a container that is not up
    ],
)
def test_hostnames_are_judged_by_what_they_resolve_to(resolved, expected):
    with patch("trackseerr.security._resolve_host", return_value=resolved):
        assert is_safe_service_url("http://media.example.com:8096") is expected


@pytest.mark.parametrize(
    "url",
    [
        "http://127.1",
        "http://127.1:8096",
        "http://10.1/",
        "http://1.2.3/",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://0177.0.0.1/",
        "http://100.100.100.200/latest/meta-data",
        "http://169.254.169.254/",
        "http://[::ffff:169.254.169.254]/",
        "http://[fd00:ec2::254]/",
    ],
)
def test_numeric_shorthand_and_metadata_literals_are_refused(url):
    assert is_safe_service_url(url) is False


@pytest.mark.parametrize("url", ["http://192.168.1.20:8096", "http://10.0.0.5", "http://172.16.0.9:4533", "http://localhost:4533"])
def test_lan_addresses_stay_allowed(url):
    assert is_safe_service_url(url) is True


def test_saved_settings_url_resolving_to_metadata_is_not_connected_but_env_is_trusted(caplog):
    cfg = Config(plex_url="", plex_token="")
    cfg.apply_media_server_overlay({"type": "subsonic", "url": "http://nav.example:4533", "username": "u", "password": "p"})
    jelly = Config(plex_url="", plex_token="")
    jelly.apply_media_server_overlay({"type": "jellyfin", "url": "http://jf.example:8096", "api_key": "k"})
    with patch("trackseerr.security._resolve_host", return_value=["169.254.169.254"]):
        with caplog.at_level(logging.ERROR):
            assert build_subsonic(cfg) is None and build_jellyfin(jelly) is None
        env_cfg = Config(plex_url="", plex_token="", media_server="subsonic", subsonic_url="http://nav.example:4533",
                         subsonic_user="u", subsonic_password="p")
        assert build_subsonic(env_cfg) is not None  # operator-controlled environment values are not re-judged
    assert any("not an allowed service address" in r.getMessage() for r in caplog.records)


# ----------------------------------------------------------------------------- J6 / S3 / S7 / S8: settings API


def _headers(db, cfg, user):
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=get_or_create_secret_key(data_dir=cfg.data_dir)
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("MEDIA_SERVER", "PLEX_URL", "PLEX_TOKEN", "SUBSONIC_URL", "JELLYFIN_URL"):
        monkeypatch.delenv(name, raising=False)
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


SUB = {"type": "subsonic", "url": SUB_URL, "username": "bob", "password": "hunter2", "api_key": ""}
JF = {"type": "jellyfin", "url": "http://jellyfin:8096", "username": "", "password": "", "api_key": "jf-secret"}
MASK = "********"


@pytest.mark.parametrize(
    "change",
    [{"url": "http://elsewhere:4533"}, {"username": "mallory"}],
    ids=["new-url", "new-username"],
)
def test_masked_subsonic_password_is_not_resolved_for_a_different_url_or_user(env, change):
    assert env.tc.put("/api/settings/media-server", json=SUB, headers=env.admin).status_code == 200
    body = {**SUB, "password": MASK, **change}
    assert env.tc.put("/api/settings/media-server", json=body, headers=env.admin).status_code == 400
    assert env.tc.post("/api/settings/media-server/test", json=body, headers=env.admin).status_code == 400
    assert env.db.get_media_server_settings()["url"] == SUB_URL  # nothing was saved


def test_masked_subsonic_api_key_is_not_resolved_for_a_different_url(env):
    keyed = {**SUB, "password": "", "api_key": "sub-key"}
    assert env.tc.put("/api/settings/media-server", json=keyed, headers=env.admin).status_code == 200
    moved = {**keyed, "api_key": MASK, "url": "http://elsewhere:4533"}
    assert env.tc.put("/api/settings/media-server", json=moved, headers=env.admin).status_code == 400
    assert env.tc.post("/api/settings/media-server/test", json=moved, headers=env.admin).status_code == 400


def test_a_stored_key_is_never_merged_across_server_types(env):
    assert env.tc.put("/api/settings/media-server", json=JF, headers=env.admin).status_code == 200
    # same URL, other type: the saved Jellyfin key must not become a Subsonic API key
    as_subsonic = {**SUB, "url": JF["url"], "password": "", "api_key": MASK}
    assert env.tc.put("/api/settings/media-server", json=as_subsonic, headers=env.admin).status_code == 400
    assert env.tc.post("/api/settings/media-server/test", json=as_subsonic, headers=env.admin).status_code == 400
    assert env.tc.put("/api/settings/media-server", json={**SUB, "password": MASK}, headers=env.admin).status_code in (400, 422)
    # and the other way round
    keyed_subsonic = {**SUB, "password": "", "api_key": "sub-key"}
    assert env.tc.put("/api/settings/media-server", json=keyed_subsonic, headers=env.admin).status_code == 200
    as_jellyfin = {**JF, "url": SUB_URL, "api_key": MASK}
    assert env.tc.put("/api/settings/media-server", json=as_jellyfin, headers=env.admin).status_code == 400
    assert env.tc.post("/api/settings/media-server/test", json=as_jellyfin, headers=env.admin).status_code == 400


def test_merge_secrets_unit_rules():
    stored = {"type": "jellyfin", "credentials_type": "jellyfin", "url": "http://j", "username": "", "password": "", "api_key": "k"}
    with pytest.raises(ms_settings.SecretReuseError):
        ms_settings.merge_secrets({"type": "subsonic", "url": "http://j", "api_key": MASK, "username": "", "password": ""}, stored)
    same = ms_settings.merge_secrets({"type": "jellyfin", "url": "http://J/", "api_key": MASK, "username": "", "password": ""}, stored)
    assert same["api_key"] == "k"
    sub_stored = {"type": "subsonic", "credentials_type": "subsonic", "url": "http://s", "username": "u", "password": "p", "api_key": ""}
    sub_keyed = {**sub_stored, "api_key": "sub-key"}
    with pytest.raises(ms_settings.SecretReuseError):
        ms_settings.merge_secrets({"type": "jellyfin", "url": "http://s", "api_key": MASK, "username": "", "password": ""}, sub_keyed)
    ok = ms_settings.merge_secrets({"type": "subsonic", "url": "http://s/", "username": "u", "password": MASK, "api_key": ""}, sub_stored)
    assert ok["password"] == "p"


def test_saving_none_keeps_the_credentials_of_the_previous_type(env):
    assert env.tc.put("/api/settings/media-server", json=JF, headers=env.admin).status_code == 200
    assert env.tc.put("/api/settings/media-server", json={"type": "none"}, headers=env.admin).status_code == 200
    stored = env.db.get_media_server_settings()
    assert (stored["type"], stored["api_key"], stored["url"], stored["credentials_type"]) == ("none", "jf-secret", JF["url"], "jellyfin")
    assert not Config.from_env().jellyfin_configured
    # the kept key still only resolves for its own type and URL
    assert env.tc.put("/api/settings/media-server", json={**SUB, "url": JF["url"], "password": "", "api_key": MASK}, headers=env.admin).status_code == 400
    assert env.tc.put("/api/settings/media-server", json={**JF, "api_key": MASK}, headers=env.admin).status_code == 200
    assert env.db.get_media_server_settings()["api_key"] == "jf-secret"
    # a second "none" keeps them too
    env.tc.put("/api/settings/media-server", json={"type": "none"}, headers=env.admin)
    env.tc.put("/api/settings/media-server", json={"type": "none"}, headers=env.admin)
    assert env.db.get_media_server_settings()["api_key"] == "jf-secret"



def test_settings_get_and_test_require_the_core_tier():
    from fastapi import HTTPException

    from trackseerr.api.routes import settings as settings_routes

    wanted = {("/media-server", "GET"), ("/media-server/test", "POST")}
    found = set()
    for route in settings_routes.router.routes:
        for method in getattr(route, "methods", ()) or ():
            if (getattr(route, "path", ""), method) in wanted:
                assert require_core_tier in [d.dependency for d in route.dependencies], (route.path, method)
                found.add((route.path, method))
    assert found == wanted
    with pytest.raises(HTTPException) as refused:
        require_core_tier(Config(plex_url="", plex_token="", role="gateway"))
    assert refused.value.status_code == 403


def test_save_and_test_routes_reject_a_hostname_that_resolves_to_metadata(env):
    with patch("trackseerr.security._resolve_host", return_value=["169.254.169.254"]):
        assert env.tc.put("/api/settings/media-server", json={**SUB, "url": "http://sneaky.example:4533"}, headers=env.admin).status_code == 400
        res = env.tc.post("/api/settings/media-server/test", json={**SUB, "url": "http://sneaky.example:4533"}, headers=env.admin).json()
    assert res["ok"] is False and "SSRF" in res["message"]


# ----------------------------------------------------------------------------- J3 through the admin API


def test_user_refresh_skips_colliding_names_and_survives_a_bad_row(env):
    from trackseerr.api.dependencies import get_media_client

    env.cfg.media_server = "jellyfin"
    local_admin(env.db, "kid")  # a local account already owns the name Jellyfin also has
    server = SimpleNamespace(
        kind="jellyfin",
        capabilities=SimpleNamespace(users=True),
        list_users=lambda: [jf_user("jf-1", "KID"), jf_user("jf-2", "friend", admin=True), jf_user("jf-3", "later")],
    )
    env.app.dependency_overrides[get_media_client] = lambda: server
    with patch("trackseerr.api.routes.users.as_media_server", return_value=server):
        res = env.tc.post("/api/users/refresh", headers=env.admin)
    assert res.status_code == 200, res.text
    users = {u["username"]: u for u in res.json()}
    assert "KID" not in users and users["kid"]["auth_type"] == "local"
    assert users["friend"]["auth_type"] == "jellyfin" and users["friend"]["is_admin"] is False
    assert "later" in users

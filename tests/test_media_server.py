"""Stage 1 of media-server support: Trackseerr runs without Plex (MEDIA_SERVER=none), Plex stays the default."""

import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import local_auth, media_server
from trackseerr.admin_bootstrap import ensure_bootstrap_admin
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_plex_client
from trackseerr.api.routes.sync import SyncState
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.cli import main
from trackseerr.config import Config, ConfigError
from trackseerr.media_servers.plex import PlexMediaServer
from trackseerr.models import Playlist, Track, UserPermission
from trackseerr.storage import Database
from trackseerr.sync import SyncCoordinator

_ADMIN_PERMS = int(UserPermission.DEFAULT) | int(UserPermission.ADMIN)
SECRET = "s" * 40
NO_SERVER_BODY = {"detail": "No media server connected", "code": "media_server_unavailable"}


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    media_server.reset_probe_cache()
    yield
    media_server.reset_probe_cache()


# ----------------------------------------------------------------------------- config semantics


def test_default_is_plex_when_credentials_present():
    cfg = Config(plex_url="http://plex:32400", plex_token="tok")
    assert cfg.media_server_type == "plex" and cfg.plex_enabled


def test_default_is_none_without_credentials():
    cfg = Config(plex_url="", plex_token="")
    assert cfg.media_server_type == "none" and not cfg.plex_enabled
    cfg.validate_media_server()  # implicit none is never an error


def test_partial_credentials_default_to_none():
    assert Config(plex_url="http://plex", plex_token="").media_server_type == "none"


@pytest.mark.parametrize("url,token", [("http://plex", ""), ("", "tok")])
def test_partial_plex_config_with_media_server_unset_is_a_config_error(url, token):
    with pytest.raises(ConfigError, match="partially configured"):
        Config(plex_url=url, plex_token=token).validate_media_server()


@pytest.mark.parametrize("choice", ["none", "plex"])
def test_partial_plex_config_with_explicit_media_server_keeps_prior_behaviour(choice):
    cfg = Config(plex_url="http://plex", plex_token="", media_server=choice)
    if choice == "none":
        cfg.validate_media_server()
    else:
        with pytest.raises(ConfigError, match="PLEX_URL and PLEX_TOKEN"):
            cfg.validate_media_server()


def test_explicit_none_wins_over_credentials():
    cfg = Config(plex_url="http://plex", plex_token="tok", media_server="none")
    assert cfg.media_server_type == "none" and not cfg.plex_enabled


def test_explicit_plex_without_credentials_is_a_config_error():
    with pytest.raises(ConfigError, match="PLEX_URL and PLEX_TOKEN"):
        Config(plex_url="", plex_token="", media_server="plex").validate_media_server()


def test_unknown_media_server_is_a_config_error():
    with pytest.raises(ConfigError, match="not supported"):
        Config(plex_url="", plex_token="", media_server="emby").validate_media_server()


def test_from_env_reads_media_server():
    with patch.dict(os.environ, {"MEDIA_SERVER": " None "}, clear=True):
        assert Config.from_env().media_server_type == "none"
    with patch.dict(os.environ, {"PLEX_URL": "http://p", "PLEX_TOKEN": "t"}, clear=True):
        assert Config.from_env().media_server_type == "plex"


# ----------------------------------------------------------------------------- boot in every role / mode


def _stub_workers():
    return [
        patch("trackseerr.acquisition_worker.acquisition_worker.start"),
        patch("trackseerr.backlog_worker.backlog_worker.start"),
        patch("trackseerr.backlog_worker.rss_worker.start"),
        patch("trackseerr.artist_refresh_worker.artist_refresh_worker.start"),
        patch("trackseerr.scrobble_worker.scrobble_worker.start"),
        patch("trackseerr.mix_worker.mix_worker.start"),
        patch("trackseerr.cli._start_lidarr_trickle"),
    ]


def _run_web(env):
    patches = _stub_workers() + [patch("trackseerr.cli.uvicorn.Server"), patch("trackseerr.cli.PlexClient")]
    started = [p.start() for p in patches]
    plex_cls = started[-1]
    try:
        with patch.dict(os.environ, env, clear=True):
            return main(), plex_cls
    finally:
        for p in reversed(patches):
            p.stop()


def _base_env(tmp_path, **extra):
    env = {"PORT": "5250", "DATA_DIR": str(tmp_path), "CONFIG_DIR": str(tmp_path), "SECONDS_TO_WAIT": "0"}
    env.update(extra)
    return env


@pytest.mark.parametrize(
    "extra",
    [
        {},  # all-in-one
        {"ROLE": "core", "INTERNAL_CORE_SECRET": SECRET, "APPLICATION_URL": "https://music.example.com"},
    ],
    ids=["all-in-one", "core"],
)
def test_web_roles_boot_without_plex(tmp_path, caplog, extra):
    with caplog.at_level(logging.INFO):
        code, plex_cls = _run_web(_base_env(tmp_path, **extra))
    assert code == 0
    plex_cls.assert_not_called()
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert infos.count(media_server.NO_MEDIA_SERVER_BOOT_MESSAGE) == 1


def test_explicit_plex_without_credentials_fails_startup_loudly(tmp_path, caplog, capsys):
    with caplog.at_level(logging.ERROR):
        code, _ = _run_web(_base_env(tmp_path, MEDIA_SERVER="plex"))
    assert code == 1
    assert "MEDIA_SERVER=plex requires PLEX_URL and PLEX_TOKEN" in capsys.readouterr().err


def test_run_once_without_plex_skips_push_and_succeeds():
    with patch("trackseerr.cli.PlexClient") as plex_cls, patch("trackseerr.cli.SyncCoordinator") as coord_cls:
        with patch.dict(os.environ, {"RUN_ONCE": "1"}, clear=True):
            code = main()
    assert code == 0
    plex_cls.assert_not_called()
    assert coord_cls.call_args.kwargs["plex_client"] is None
    coord_cls.return_value.run_sync_cycle.assert_called_once()


def test_headless_without_plex_runs_cycle_and_exits_cleanly():
    with patch("trackseerr.cli.PlexClient") as plex_cls, patch("trackseerr.cli.SyncCoordinator") as coord_cls:
        with patch.dict(os.environ, {"HEADLESS": "1", "SECONDS_TO_WAIT": "1"}, clear=True):
            with patch("trackseerr.cli._shutdown_requested", True):
                code = main()
    assert code == 0
    plex_cls.assert_not_called()


def test_run_once_without_plex_does_not_fail_on_playlist_fetch_with_no_db():
    """The real coordinator, no Plex and no database: playlists are fetched, nothing is pushed, no exception."""
    pl = Playlist(id="p1", name="Mix", tracks=[Track(title="A", artist="B", album="C")])
    spotify = MagicMock()
    spotify.fetch_all_playlists.return_value = [pl]
    cfg = Config(plex_url="", plex_token="", spotify_client_id="i", spotify_client_secret="s", spotify_playlist_ids=["p1"])
    results = SyncCoordinator(config=cfg, plex_client=None, spotify_client=spotify).run_sync_cycle()
    assert [(r.playlist_name, r.success, r.matched_tracks) for r in results] == [("Mix", True, 0)]


# ----------------------------------------------------------------------------- first admin without Plex


def test_bootstrap_admin_created_from_env_and_can_log_in(tmp_path):
    db = Database(":memory:")
    with patch.dict(os.environ, {"ADMIN_USERNAME": "Ron", "ADMIN_PASSWORD": "correct horse battery"}, clear=True):
        assert ensure_bootstrap_admin(db) is True
        assert ensure_bootstrap_admin(db) is False  # an admin now exists: the env can never reset it
    user = next(u for u in db.list_users() if u["username"] == "ron")
    assert user["is_admin"] and user["auth_type"] == "local"
    cfg = Config(plex_url="", plex_token="", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    res = TestClient(app).post("/api/auth/local/login", json={"username": "ron", "password": "correct horse battery"})
    assert res.status_code == 200, res.text


def test_bootstrap_admin_without_password_logs_error_with_recovery_instructions(caplog):
    db = Database(":memory:")
    with patch.dict(os.environ, {}, clear=True), caplog.at_level(logging.WARNING):
        assert ensure_bootstrap_admin(db) is False
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "set ADMIN_PASSWORD and restart to create a local admin" in errors[0].getMessage() and "restart" in errors[0].getMessage()
    assert db.list_users() == []


def test_bootstrap_admin_skipped_when_local_admin_with_password_exists():
    db = Database(":memory:")
    db.create_local_user_with_password("owner", local_auth.hash_password("correct horse battery"), _ADMIN_PERMS)
    before = [u["id"] for u in db.list_users()]
    with patch.dict(os.environ, {"ADMIN_PASSWORD": "another good passphrase"}, clear=True):
        assert ensure_bootstrap_admin(db) is False
    assert [u["id"] for u in db.list_users()] == before


def test_bootstrap_with_plex_only_admin_creates_local_admin_and_leaves_plex_admin_untouched(tmp_path):
    db = Database(":memory:")
    db.upsert_user("plex-1", "owner", "o@x.io", is_admin=True)
    plex_before = db.get_user("plex-1")
    with patch.dict(
        os.environ, {"ADMIN_USERNAME": "localadmin", "ADMIN_PASSWORD": "correct horse battery"}, clear=True
    ):
        assert ensure_bootstrap_admin(db) is True
    assert db.get_user("plex-1") == plex_before
    local = next(u for u in db.list_users() if u["username"] == "localadmin")
    assert local["is_admin"] and local["auth_type"] == "local"
    cfg = Config(plex_url="", plex_token="", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    res = TestClient(app).post(
        "/api/auth/local/login", json={"username": "localadmin", "password": "correct horse battery"}
    )
    assert res.status_code == 200, res.text


def test_bootstrap_plex_only_admin_without_password_creates_nothing(caplog):
    db = Database(":memory:")
    db.upsert_user("plex-1", "owner", "o@x.io", is_admin=True)
    with patch.dict(os.environ, {}, clear=True), caplog.at_level(logging.ERROR):
        assert ensure_bootstrap_admin(db) is False
    assert any(r.levelno == logging.ERROR and "ADMIN_PASSWORD" in r.getMessage() for r in caplog.records)
    assert [u["username"] for u in db.list_users()] == ["owner"]


def test_bootstrap_username_collision_with_non_local_user_is_an_error_and_untouched(caplog):
    db = Database(":memory:")
    db.upsert_user("plex-1", "admin", "o@x.io", is_admin=True)
    before = db.get_user("plex-1")
    with patch.dict(os.environ, {"ADMIN_PASSWORD": "correct horse battery"}, clear=True), caplog.at_level(
        logging.ERROR
    ):
        assert ensure_bootstrap_admin(db) is False
    assert db.get_user("plex-1") == before and len(db.list_users()) == 1
    assert any("already exists" in r.getMessage() and "ADMIN_USERNAME" in r.getMessage() for r in caplog.records)


def test_bootstrap_is_atomic_failure_leaves_no_admin_row_and_no_invite():
    db = Database(":memory:")
    with patch.dict(os.environ, {"ADMIN_PASSWORD": "correct horse battery"}, clear=True), patch.object(
        local_auth, "hash_password", side_effect=ValueError("boom")
    ):
        assert ensure_bootstrap_admin(db) is False
    assert db.list_users() == []
    # failure inside the transaction itself: nothing persists
    with patch.object(db, "get_user", return_value=None):
        with pytest.raises(RuntimeError):
            db.create_local_user_with_password("zed", "hash", 34)
    db.conn.execute("DELETE FROM users WHERE username = 'zed'")
    db.conn.commit()
    assert db.conn.execute("SELECT COUNT(*) FROM user_invites").fetchone()[0] == 0


def test_bootstrap_creates_user_with_hash_and_no_invite_row():
    db = Database(":memory:")
    with patch.dict(os.environ, {"ADMIN_PASSWORD": "correct horse battery"}, clear=True):
        assert ensure_bootstrap_admin(db) is True
    assert db.conn.execute("SELECT COUNT(*) FROM user_invites").fetchone()[0] == 0
    assert db.conn.execute("SELECT password_hash FROM users").fetchone()[0]


def test_bootstrap_removes_admin_password_from_environment():
    for existing in (False, True):
        db = Database(":memory:")
        if existing:
            db.create_local_user_with_password("owner", local_auth.hash_password("correct horse battery"), _ADMIN_PERMS)
        with patch.dict(os.environ, {"ADMIN_PASSWORD": "another good passphrase"}, clear=True):
            ensure_bootstrap_admin(db)
            assert "ADMIN_PASSWORD" not in os.environ


def test_bootstrap_admin_rejects_weak_password():
    db = Database(":memory:")
    with patch.dict(os.environ, {"ADMIN_PASSWORD": "x"}, clear=True):
        assert ensure_bootstrap_admin(db) is False
    assert db.list_users() == []


# ----------------------------------------------------------------------------- app fixture: no media server


def _admin_headers(db, cfg):
    user = db.upsert_user("u-admin", "admin", "a@x.io", is_admin=True)
    secret = get_or_create_secret_key(data_dir=cfg.data_dir)
    token = create_session_token(user_id=user["id"], username="admin", is_admin=True, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def build_client(tmp_path, *, plex=None, media_server_choice="", url="", token=""):
    """An app instance for a given media-server setup; ``plex`` is what get_plex_client yields."""
    db = Database(":memory:")
    cfg = Config(plex_url=url, plex_token=token, media_server=media_server_choice, data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_plex_client] = lambda: plex
    return SimpleNamespace(tc=TestClient(app), db=db, cfg=cfg, headers=_admin_headers(db, cfg))


@pytest.fixture
def no_server(tmp_path):
    return build_client(tmp_path)


# ----------------------------------------------------------------------------- endpoint contract


def test_media_server_endpoint_none(no_server):
    res = no_server.tc.get("/api/system/media-server")  # unauthenticated on purpose: the login screen needs it
    assert res.status_code == 200
    assert res.json() == {
        "type": "none",
        "connected": False,
        "capabilities": {"playlists": False, "users": False, "mixes": False, "library_refresh": False, "file_paths": False},
    }


def test_media_server_endpoint_plex_connected(tmp_path):
    env = build_client(tmp_path, url="http://plex", token="tok", plex=MagicMock())
    with patch("trackseerr.api.routes.system.status.get_plex_client", return_value=MagicMock()):
        body = env.tc.get("/api/system/media-server").json()
    assert body == {
        "type": "plex",
        "connected": True,
        "capabilities": {"playlists": True, "users": True, "mixes": True, "library_refresh": True, "file_paths": True},
    }


def test_media_server_endpoint_plex_unreachable_is_still_plex(tmp_path):
    env = build_client(tmp_path, url="http://plex", token="tok")
    with patch("trackseerr.api.routes.system.status.get_plex_client", return_value=None):
        body = env.tc.get("/api/system/media-server").json()
    assert body["type"] == "plex" and body["connected"] is False and body["capabilities"]["playlists"] is True


def test_media_server_probe_is_cached_for_unauthenticated_callers(tmp_path):
    env = build_client(tmp_path, url="http://plex", token="tok")
    with patch("trackseerr.api.routes.system.status.get_plex_client", return_value=MagicMock()) as connect:
        for _ in range(5):
            assert env.tc.get("/api/system/media-server").json()["connected"] is True
    assert connect.call_count == 1


def test_media_server_probe_never_blocks_concurrent_callers():
    import threading
    import time

    cfg = Config(plex_url="http://plex", plex_token="tok")

    def slow_connect():
        time.sleep(2)
        return MagicMock()

    durations: list[float] = []
    results: list[bool] = []

    def call():
        t0 = time.monotonic()
        results.append(media_server.media_server_status(cfg, slow_connect)["connected"])
        durations.append(time.monotonic() - t0)

    threads = [threading.Thread(target=call) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for d in durations if d > 0.3) <= 1
    assert results.count(False) >= 19  # no cache yet: the others answer "not connected" instead of waiting
    # once the refresh lands, the cached value is served instantly
    t0 = time.monotonic()
    assert media_server.media_server_status(cfg, slow_connect)["connected"] is True
    assert time.monotonic() - t0 < 0.3


def test_media_server_endpoint_never_leaks_credentials(tmp_path):
    env = build_client(tmp_path, url="http://plex.secret:32400", token="SECRETTOKEN")
    with patch("trackseerr.api.routes.system.status.get_plex_client", return_value=MagicMock()):
        raw = env.tc.get("/api/system/media-server").text
    assert "SECRETTOKEN" not in raw and "plex.secret" not in raw


# ----------------------------------------------------------------------------- Plex-only routes: one consistent 409


PLEX_ONLY_ROUTES = [
    ("get", "/api/plex-playlists"),
    ("get", "/api/plex-playlists/users"),
    ("get", "/api/plex-playlists/mixes"),
    ("get", "/api/plex-playlists/mixes/snapshots"),
    ("post", "/api/users/refresh"),
    ("get", "/api/missing/search?query=abc"),
]


@pytest.mark.parametrize("method,path", PLEX_ONLY_ROUTES)
def test_plex_only_routes_return_consistent_409(no_server, method, path):
    kwargs = {"json": {"mix_type": "x"}} if method == "post" else {}
    res = getattr(no_server.tc, method)(path, headers=no_server.headers, **kwargs)
    assert res.status_code == 409, res.text
    assert res.json() == NO_SERVER_BODY


def test_plex_login_returns_409_without_media_server(no_server):
    assert no_server.tc.post("/api/auth/plex/pin", json={}).json() == NO_SERVER_BODY
    res = no_server.tc.post("/api/auth/plex/verify", json={"pin_id": 1})
    assert res.status_code in (400, 409)  # an unclaimed PIN is rejected before the server check


def test_configured_but_unreachable_plex_keeps_503(tmp_path):
    env = build_client(tmp_path, url="http://plex", token="tok", plex=None)
    assert env.tc.get("/api/plex-playlists", headers=env.headers).status_code == 503


def test_system_status_reports_media_server_not_configured_not_unhealthy(no_server):
    res = no_server.tc.get("/api/system/status", headers=no_server.headers)
    assert res.status_code == 200
    plex = res.json()["plex"]
    assert plex["configured"] is False and plex["message"] == "Plex not configured"
    assert no_server.tc.get("/api/health").status_code == 200


def test_core_features_work_without_plex(no_server):
    h = no_server.headers
    assert no_server.tc.get("/api/library/artists", headers=h).status_code != 409
    assert no_server.tc.get("/api/playlists", headers=h).status_code == 200
    created = no_server.tc.post(
        "/api/admin/users", headers=h, json={"username": "newuser", "permissions": 34}
    )
    assert created.status_code in (201, 503)  # 503 only when APPLICATION_URL is unset on this bare config


# ----------------------------------------------------------------------------- sync without a media server


def test_sync_with_no_media_server_records_missing_via_native_library(tmp_path):
    env = build_client(tmp_path)
    db = env.db
    pl_tracks = [
        Track(title="Known Song", artist="Known Artist", album="Known Album"),
        Track(title="Unknown Song", artist="Nobody", album="Nowhere"),
    ]
    db.upsert_playlist("imp_x", "Imported", service="spotify")
    with db._lock:
        db.conn.execute(
            "UPDATE playlists SET tracks_json = ? WHERE id = ?",
            (json.dumps([{"title": t.title, "artist": t.artist, "album": t.album} for t in pl_tracks]), "imp_x"),
        )
        db.conn.commit()
    matched_track = pl_tracks[0]
    with patch(
        "trackseerr.api.routes.sync.match_playlist_tracks_native",
        return_value=([matched_track], [pl_tracks[1]]),
    ) as native:
        state = SyncState()
        out = state.execute_sync(db=db, config=env.cfg, plex_client=None, spotify_client=None, deezer_client=None)
    native.assert_called_once()
    assert out["status"] == "success"
    assert out["stats"]["total_matched"] == 1 and out["stats"]["total_missing"] == 1
    assert out["stats"]["success_count"] == 1
    missing = db.get_missing_tracks("imp_x")
    assert [m["title"] for m in missing] == ["Unknown Song"]
    assert db.get_playlist("imp_x")["sync_status"] == "success"


def test_sync_with_no_media_server_needs_no_target_users(tmp_path):
    """Plex sync skips playlists with no target users; native matching must not."""
    env = build_client(tmp_path)
    env.db.upsert_playlist("imp_y", "NoTargets", service="spotify")
    with env.db._lock:
        env.db.conn.execute("UPDATE playlists SET tracks_json = ? WHERE id = ?", (json.dumps([{"title": "T", "artist": "A"}]), "imp_y"))
        env.db.conn.commit()
    out = SyncState().execute_sync(db=env.db, config=env.cfg, plex_client=None, spotify_client=None, deezer_client=None)
    assert out["stats"]["total_missing"] == 1
    assert [m["title"] for m in env.db.get_missing_tracks("imp_y")] == ["T"]


def _unchanged_env(tmp_path, tracks=None):
    """No-media-server env with one stored-list playlist, plus the native matcher patch target."""
    env = build_client(tmp_path)
    env.db.upsert_playlist("imp_skip", "SkipMe", service="spotify")
    _set_tracks(env.db, tracks or _TWO_TRACKS_NATIVE)
    return env


_TWO_TRACKS_NATIVE = [{"title": "Known Song", "artist": "Known Artist"}, {"title": "Unknown Song", "artist": "Nobody"}]


def _set_tracks(db, tracks):
    with db._lock:
        db.conn.execute("UPDATE playlists SET tracks_json = ? WHERE id = ?", (json.dumps(tracks), "imp_skip"))
        db.conn.commit()


def _sql(db, sql, *args):
    with db._lock:
        db.conn.execute(sql, args)
        db.conn.commit()


def _run_sync(env, state, **kwargs):
    return state.execute_sync(db=env.db, config=env.cfg, plex_client=None, spotify_client=None, deezer_client=None, **kwargs)


def _native_result():
    return (
        [Track(title="Known Song", artist="Known Artist", album="")],
        [Track(title="Unknown Song", artist="Nobody", album="")],
    )


def test_second_sync_skips_unchanged_playlist(tmp_path):
    env = _unchanged_env(tmp_path)
    state = SyncState()
    with patch("trackseerr.api.routes.sync.match_playlist_tracks_native", return_value=_native_result()) as native:
        first = _run_sync(env, state)
        second = _run_sync(env, state)
    native.assert_called_once()
    assert first["stats"]["skipped_unchanged"] == 0
    assert second["stats"]["skipped_unchanged"] == 1
    assert [m["title"] for m in env.db.get_missing_tracks("imp_skip")] == ["Unknown Song"]


def test_changed_source_tracks_rematch(tmp_path):
    env = _unchanged_env(tmp_path)
    state = SyncState()
    with patch("trackseerr.api.routes.sync.match_playlist_tracks_native", return_value=_native_result()) as native:
        _run_sync(env, state)
        _set_tracks(env.db, [{"title": "Another", "artist": "Else"}])
        out = _run_sync(env, state)
    assert native.call_count == 2
    assert out["stats"]["skipped_unchanged"] == 0


def test_library_import_since_last_sync_forces_rematch(tmp_path):
    env = _unchanged_env(tmp_path)
    state = SyncState()
    with patch("trackseerr.api.routes.sync.match_playlist_tracks_native", return_value=_native_result()) as native:
        _run_sync(env, state)
        env.db.upsert_library_artist({"id": "a1", "name": "Known Artist", "monitored": True})
        env.db.upsert_library_album({"id": "al1", "artist_id": "a1", "title": "Alb", "year": 2000})
        env.db.upsert_library_track({"id": "t1", "album_id": "al1", "artist_id": "a1", "title": "Known Song"})
        env.db.upsert_library_file(
            {"id": "f1", "track_id": "t1", "file_path": "/m/a.flac", "relative_path": "a.flac", "codec": "FLAC",
             "quality_name": "FLAC", "size_bytes": 1}
        )
        _sql(env.db, "UPDATE library_files SET updated_at = '2999-01-01 00:00:00'")
        _sql(env.db, "UPDATE playlists SET last_synced_at = '2998-01-01 00:00:00' WHERE id = 'imp_skip'")
        out = _run_sync(env, state)
    assert native.call_count == 2
    assert out["stats"]["skipped_unchanged"] == 0


def _import_file(db):
    db.upsert_library_artist({"id": "a1", "name": "Known Artist", "monitored": True})
    db.upsert_library_album({"id": "al1", "artist_id": "a1", "title": "Alb", "year": 2000})
    db.upsert_library_track({"id": "t1", "album_id": "al1", "artist_id": "a1", "title": "Known Song"})
    db.upsert_library_file(
        {"id": "f1", "track_id": "t1", "file_path": "/m/a.flac", "relative_path": "a.flac", "codec": "FLAC",
         "quality_name": "FLAC", "size_bytes": 1}
    )


def test_import_just_before_last_sync_still_rematches(tmp_path):
    env = _unchanged_env(tmp_path)
    state = SyncState()
    with patch("trackseerr.api.routes.sync.match_playlist_tracks_native", return_value=_native_result()) as native:
        _run_sync(env, state)
        _import_file(env.db)
        _sql(env.db, "UPDATE playlists SET last_synced_at = datetime('now', '-1 hours') WHERE id = 'imp_skip'")
        _sql(env.db, "UPDATE library_files SET updated_at = datetime('now', '-1 hours', '-5 minutes')")
        out = _run_sync(env, state)
    assert native.call_count == 2
    assert out["stats"]["skipped_unchanged"] == 0


def test_import_well_before_last_sync_is_skipped(tmp_path):
    env = _unchanged_env(tmp_path)
    state = SyncState()
    with patch("trackseerr.api.routes.sync.match_playlist_tracks_native", return_value=_native_result()) as native:
        _run_sync(env, state)
        _import_file(env.db)
        _sql(env.db, "UPDATE playlists SET last_synced_at = datetime('now', '-1 hours') WHERE id = 'imp_skip'")
        _sql(env.db, "UPDATE library_files SET updated_at = datetime('now', '-3 hours')")
        out = _run_sync(env, state)
    native.assert_called_once()
    assert out["stats"]["skipped_unchanged"] == 1


def test_force_rematches(tmp_path):
    env = _unchanged_env(tmp_path)
    state = SyncState()
    with patch("trackseerr.api.routes.sync.match_playlist_tracks_native", return_value=_native_result()) as native:
        _run_sync(env, state)
        out = _run_sync(env, state, force=True)
    assert native.call_count == 2
    assert out["stats"]["skipped_unchanged"] == 0


def test_failed_sync_clears_fingerprint_and_retries(tmp_path):
    env = _unchanged_env(tmp_path)
    state = SyncState()
    with patch(
        "trackseerr.api.routes.sync.match_playlist_tracks_native",
        side_effect=[RuntimeError("boom"), _native_result(), _native_result()],
    ) as native:
        _run_sync(env, state)
        assert env.db.get_playlist("imp_skip")["sync_status"] == "failed"
        assert env.db.get_playlist_sync_fingerprint("imp_skip")[0] is None
        out = _run_sync(env, state)
        assert out["stats"]["success_count"] == 1
        assert env.db.get_playlist_sync_fingerprint("imp_skip")[0]
        again = _run_sync(env, state)
    assert native.call_count == 2
    assert again["stats"]["skipped_unchanged"] == 1


def test_stale_fingerprint_older_than_a_day_rematches(tmp_path):
    env = _unchanged_env(tmp_path)
    state = SyncState()
    with patch("trackseerr.api.routes.sync.match_playlist_tracks_native", return_value=_native_result()) as native:
        _run_sync(env, state)
        _sql(env.db, "UPDATE playlists SET last_synced_at = datetime('now', '-2 days') WHERE id = 'imp_skip'")
        out = _run_sync(env, state)
    assert native.call_count == 2
    assert out["stats"]["skipped_unchanged"] == 0


def _plex_env(tmp_path, playlist_id: str, tracks: list[dict]):
    """A Plex-configured env with one spotify playlist holding ``tracks`` as its stored list."""
    plex = MagicMock()
    env = build_client(tmp_path, url="http://plex", token="tok", plex=plex)
    env.plex = plex
    env.db.upsert_playlist(playlist_id, "PlexList", service="spotify")
    with env.db._lock:
        env.db.conn.execute("UPDATE playlists SET tracks_json = ? WHERE id = ?", (json.dumps(tracks), playlist_id))
        env.db.conn.commit()
    return env


_TWO_TRACKS = [{"title": "Known Song", "artist": "Known Artist"}, {"title": "Unknown Song", "artist": "Nobody"}]


def test_sync_with_plex_and_no_targets_matches_without_pushing(tmp_path):
    env = _plex_env(tmp_path, "imp_z", _TWO_TRACKS)
    known, unknown = Track(title="Known Song", artist="Known Artist", album=""), Track(title="Unknown Song", artist="Nobody", album="")
    with (
        patch("trackseerr.api.routes.sync.refresh_mix_snapshots", return_value=0),
        patch("trackseerr.api.routes.sync.match_playlist_tracks_native", return_value=([known], [unknown])) as native,
        patch.object(PlexMediaServer, "sync_playlist") as push,
        patch.object(PlexMediaServer, "match_playlist_tracks") as server_match,
    ):
        out = SyncState().execute_sync(db=env.db, config=env.cfg, plex_client=env.plex, spotify_client=None, deezer_client=None)
    native.assert_called_once()
    push.assert_not_called()
    server_match.assert_not_called()
    assert out["status"] == "success"
    assert out["stats"]["total_missing"] == 1 and out["stats"]["success_count"] == 1
    assert [m["title"] for m in env.db.get_missing_tracks("imp_z")] == ["Unknown Song"]


def test_sync_no_targets_logs_once_per_playlist(tmp_path, caplog):
    env = _plex_env(tmp_path, "imp_log", _TWO_TRACKS)
    state = SyncState()
    with (
        patch("trackseerr.api.routes.sync.refresh_mix_snapshots", return_value=0),
        patch("trackseerr.api.routes.sync.match_playlist_tracks_native", return_value=([], [])),
        caplog.at_level(logging.INFO, logger="trackseerr"),
    ):
        for _ in range(2):
            state.execute_sync(db=env.db, config=env.cfg, plex_client=env.plex, spotify_client=None, deezer_client=None)
    assert len([r for r in caplog.records if "has no target users" in r.getMessage()]) == 1


def test_sync_with_plex_and_targets_still_pushes(tmp_path):
    env = _plex_env(tmp_path, "imp_push", _TWO_TRACKS)
    user = env.db.upsert_user("u-target", "target", "t@x.io")
    env.db.set_playlist_targets("imp_push", [user["id"]])
    with (
        patch("trackseerr.api.routes.sync.refresh_mix_snapshots", return_value=0),
        patch("trackseerr.api.routes.sync.match_playlist_tracks_native") as native,
        patch.object(PlexMediaServer, "sync_playlist", return_value=[SimpleNamespace(success=True)]) as push,
        patch.object(PlexMediaServer, "match_playlist_tracks", return_value=([], [])),
    ):
        out = SyncState().execute_sync(db=env.db, config=env.cfg, plex_client=env.plex, spotify_client=None, deezer_client=None)
    push.assert_called_once()
    assert push.call_args.args[1] == ["target"]
    native.assert_not_called()
    assert out["stats"]["success_count"] == 1


def test_coordinator_matches_natively_when_db_given_and_no_plex(tmp_path):
    db = Database(":memory:")
    db.upsert_playlist("p1", "Mix", service="spotify")
    pl = Playlist(id="p1", name="Mix", tracks=[Track(title="Gone", artist="Nobody", album="")])
    spotify = MagicMock()
    spotify.fetch_all_playlists.return_value = [pl]
    cfg = Config(plex_url="", plex_token="", spotify_client_id="i", spotify_client_secret="s", spotify_playlist_ids=["p1"])
    results = SyncCoordinator(config=cfg, plex_client=None, spotify_client=spotify, db=db).run_sync_cycle()
    assert results[0].missing_tracks == 1
    assert [m["title"] for m in db.get_missing_tracks("p1")] == ["Gone"]


def test_native_matcher_matches_indexed_library_track(tmp_path):
    from trackseerr.native_match import match_playlist_tracks_native

    db = Database(":memory:")
    found, gone = Track("Known Song", "Known Artist", "Known Album"), Track("Other", "Nobody", "X")
    fake = MagicMock()
    fake.match.side_effect = lambda entry: ("tid", "metadata") if entry["name"] == "Known Song" else None
    with patch("trackseerr.native_match.TrackMatcher", return_value=fake):
        matched, missing = match_playlist_tracks_native(db, [found, gone])
    assert matched == [found] and missing == [gone]
    assert fake.match.call_args_list[0].args[0]["artist"] == "Known Artist"


def test_direct_import_without_media_server_records_missing(tmp_path):
    env = build_client(tmp_path)
    with patch(
        "trackseerr.api.routes.playlists.match_playlist_tracks_native",
        side_effect=lambda db, tracks: ([], list(tracks)),
    ):
        res = env.tc.post(
            "/api/playlists/import",
            headers=env.headers,
            json={"name": "Direct", "service": "spotify", "tracks": [{"title": "Lost", "artist": "Band", "album": "LP"}]},
        )
    assert res.status_code == 201, res.text
    assert res.json()["missing_count"] == 1 and res.json()["matched_count"] == 0
    assert [m["title"] for m in env.db.get_missing_tracks(res.json()["id"])] == ["Lost"]

"""Tests for per-user Subsonic playlist syncing via saved credentials."""

from __future__ import annotations

from pathlib import Path

from trackseerr.config import Config
from trackseerr.media_servers.base import PlaylistSyncOptions
from trackseerr.media_servers.registry import ServerRecord
from trackseerr.media_servers.subsonic import SubsonicMediaServer
from trackseerr.media_servers.user_logins import sync_subsonic_for_users
from trackseerr.models import Playlist, Track
from trackseerr.secret_box import seal
from trackseerr.storage import Database
from tests.subsonic_fake import FakeSubsonic, FakeUser, default_state

SONGS = [
    ("Song A", "Artist 1", "Album X"),
    ("Song B", "Artist 1", "Album X"),
]


def _setup(tmp_path: Path):
    db = Database(":memory:")
    cfg = Config(plex_url="", plex_token="", data_dir=str(tmp_path))
    state = default_state(SONGS, password="pw")
    state.users["alice"] = FakeUser("alice", admin=False, password="alice-pw")
    state.users["bob"] = FakeUser("bob", admin=False, password="bob-pw")
    fake = FakeSubsonic(state)

    srv_row = db.insert_media_server_row(
        {
            "kind": "subsonic",
            "name": "Navidrome",
            "url": "http://nav:4533",
            "username": "admin",
            "password": "pw",
        }
    )
    rec = ServerRecord(
        id=srv_row["id"],
        kind="subsonic",
        name="Navidrome",
        source="settings",
        url="http://nav:4533",
        username="admin",
        password="pw",
    )
    adapter = SubsonicMediaServer(
        "http://nav:4533",
        "admin",
        "pw",
        transport=fake.transport(),
        sleep=lambda _s: None,
    )
    db.upsert_user("u-admin", "admin", "admin@x")
    db.upsert_user("u-alice", "alice", "alice@x")
    db.upsert_user("u-bob", "bob", "bob@x")
    playlist = Playlist(
        id="pl-1",
        name="Road Trip",
        tracks=[Track("Song A", "Artist 1", "Album X"), Track("Song B", "Artist 1", "Album X")],
    )
    options = PlaylistSyncOptions.from_config(cfg, db=db)
    return db, cfg, rec, adapter, fake, playlist, options


def test_own_account_target_writes_once(tmp_path: Path) -> None:
    db, cfg, rec, adapter, fake, playlist, options = _setup(tmp_path)
    try:
        results = sync_subsonic_for_users(db, cfg, rec, adapter, playlist, ["admin", "ADMIN"], options)
        assert len(results) == 2
        assert all(r.success for r in results)
        admin_playlists = [p for p in fake.state.playlists.values() if p.owner == "admin"]
        assert len(admin_playlists) == 1
        assert [ep for ep, _ in fake.state.calls].count("createPlaylist") == 1
    finally:
        adapter.close()
        db.close()


def test_empty_targets_writes_to_own_account(tmp_path: Path) -> None:
    db, cfg, rec, adapter, fake, playlist, options = _setup(tmp_path)
    try:
        results = sync_subsonic_for_users(db, cfg, rec, adapter, playlist, [], options)
        assert len(results) == 1
        assert results[0].success is True
        assert [p.owner for p in fake.state.playlists.values()] == ["admin"]
    finally:
        adapter.close()
        db.close()


def test_other_user_with_saved_login_gets_own_playlist(tmp_path: Path) -> None:
    db, cfg, rec, adapter, fake, playlist, options = _setup(tmp_path)
    try:
        db.set_server_account("u-alice", rec.id, "alice", seal("alice-pw", data_dir=cfg.data_dir))
        results = sync_subsonic_for_users(db, cfg, rec, adapter, playlist, ["ALICE"], options)
        assert len(results) == 1
        assert results[0].success is True
        assert [(p.owner, p.name, p.entries) for p in fake.state.playlists.values()] == [
            ("alice", "Road Trip", ["s1", "s2"])
        ]
    finally:
        adapter.close()
        db.close()


def test_missing_login_returns_failed_result_without_exception(tmp_path: Path) -> None:
    db, cfg, rec, adapter, fake, playlist, options = _setup(tmp_path)
    try:
        results = sync_subsonic_for_users(db, cfg, rec, adapter, playlist, ["bob"], options)
        assert len(results) == 1
        assert results[0].success is False
        assert results[0].error == "No Navidrome login saved for bob"
        assert fake.state.playlists == {}
    finally:
        adapter.close()
        db.close()


def test_unsealable_login_returns_failed_result(tmp_path: Path) -> None:
    db, cfg, rec, adapter, _fake, playlist, options = _setup(tmp_path)
    try:
        db.set_server_account("u-alice", rec.id, "alice", "not-a-valid-fernet-token")
        results = sync_subsonic_for_users(db, cfg, rec, adapter, playlist, ["alice"], options)
        assert len(results) == 1
        assert results[0].success is False
        assert results[0].error == "No Navidrome login saved for alice"
    finally:
        adapter.close()
        db.close()


def test_one_users_auth_failure_does_not_stop_another(tmp_path: Path) -> None:
    db, cfg, rec, adapter, fake, playlist, options = _setup(tmp_path)
    try:
        db.set_server_account("u-bob", rec.id, "bob", seal("wrong-pw", data_dir=cfg.data_dir))
        db.set_server_account("u-alice", rec.id, "alice", seal("alice-pw", data_dir=cfg.data_dir))

        results = sync_subsonic_for_users(db, cfg, rec, adapter, playlist, ["bob", "alice"], options)
        assert [r.success for r in results] == [False, True]
        assert "40" in results[0].error
        assert [p.owner for p in fake.state.playlists.values()] == ["alice"]
    finally:
        adapter.close()
        db.close()

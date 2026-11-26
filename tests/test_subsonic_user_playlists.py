"""Tests for per-user Subsonic playlist syncing via saved credentials."""

from __future__ import annotations

from pathlib import Path

import pytest

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
        assert results[0].error == (
            "The saved Navidrome login for alice can't be read any more; save it again on the Account page"
        )
    finally:
        adapter.close()
        db.close()


def test_unreadable_saved_login_message(tmp_path: Path) -> None:
    db, cfg, rec, adapter, _fake, playlist, options = _setup(tmp_path)
    try:
        db.set_server_account("u-bob", rec.id, "bob", "corrupted-sealed-token")
        results = sync_subsonic_for_users(db, cfg, rec, adapter, playlist, ["bob"], options)
        assert len(results) == 1
        assert results[0].success is False
        assert results[0].error == (
            "The saved Navidrome login for bob can't be read any more; save it again on the Account page"
        )
    finally:
        adapter.close()
        db.close()


def test_stored_playlist_push_uses_user_logins(tmp_path: Path) -> None:
    from trackseerr.api.routes.playlists import _push_stored_to_first_server

    db, cfg, rec, adapter, fake, playlist, _options = _setup(tmp_path)
    try:
        db.upsert_playlist(playlist.id, playlist.name, service="manual")
        db.set_server_account("u-alice", rec.id, "alice", seal("alice-pw", data_dir=cfg.data_dir))
        matched_count, missing_count = _push_stored_to_first_server(
            db, cfg, rec, adapter, playlist, ["alice"]
        )
        assert matched_count == 2
        assert missing_count == 0
        assert [(p.owner, p.name, p.entries) for p in fake.state.playlists.values()] == [
            ("alice", "Road Trip", ["s1", "s2"])
        ]
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


def test_with_login_keeps_server_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("trackseerr.media_servers.subsonic.get_shared_enricher", lambda _db: None)
    db, cfg, rec, adapter, fake, _playlist, options = _setup(tmp_path)
    try:
        adapter.server_id = rec.id
        adapter.legacy_overrides = False
        user_adapter = adapter.with_login("alice", "alice-pw")
        try:
            assert user_adapter.server_id == rec.id
            assert user_adapter.legacy_overrides is False
        finally:
            user_adapter.close()

        # Server-scoped override applies during per-user push, while legacy (unscoped) override is ignored.
        db.set_server_account("u-alice", rec.id, "alice", seal("alice-pw", data_dir=cfg.data_dir))
        db.add_server_match_override(rec.id, "Wanted Track", "Artist 1", "s1", "Song A", "Artist 1")
        db.add_match_override("Legacy Track", "Artist 1", "s2", "Song B", "Artist 1")
        pl = Playlist(
            id="pl-scoped",
            name="Scoped Mix",
            tracks=[
                Track("Wanted Track", "Artist 1", "Album X"),
                Track("Legacy Track", "Artist 1", "Album X"),
            ],
        )

        results = sync_subsonic_for_users(db, cfg, rec, adapter, pl, ["alice"], options)
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].matched_tracks == 1
        assert [t.title for t in results[0].missing] == ["Legacy Track"]
        assert [(p.owner, p.name, p.entries) for p in fake.state.playlists.values()] == [
            ("alice", "Scoped Mix", ["s1"])
        ]
    finally:
        adapter.close()
        db.close()


def test_per_user_missing_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("trackseerr.media_servers.subsonic.get_shared_enricher", lambda _db: None)
    from trackseerr.api.routes.playlists import _push_stored_to_first_server

    db, cfg, rec, adapter, _fake, _playlist, options = _setup(tmp_path)
    try:
        db.set_server_account("u-alice", rec.id, "alice", seal("alice-pw", data_dir=cfg.data_dir))
        pl = Playlist(
            id="pl-missing",
            name="Missing Mix",
            tracks=[
                Track("Song A", "Artist 1", "Album X"),
                Track("Missing Song", "Unknown Artist", "Unknown Album"),
            ],
        )
        db.upsert_playlist(pl.id, pl.name, service="manual")

        results = sync_subsonic_for_users(db, cfg, rec, adapter, pl, ["bob", "alice"], options)
        assert len(results) == 2
        assert results[0].success is False
        assert [t.title for t in results[0].missing] == ["Song A", "Missing Song"]
        assert results[1].success is True
        assert [t.title for t in results[1].missing] == ["Missing Song"]

        matched_count, missing_count = _push_stored_to_first_server(db, cfg, rec, adapter, pl, ["bob", "alice"])
        assert (matched_count, missing_count) == (1, 1)
        assert [m["title"] for m in db.get_missing_tracks(pl.id)] == ["Missing Song"]
    finally:
        adapter.close()
        db.close()


def test_saved_login_wins_over_name_match_with_server_account(tmp_path: Path) -> None:
    db = Database(":memory:")
    cfg = Config(plex_url="", plex_token="", data_dir=str(tmp_path))
    state = default_state(SONGS, password="pw")
    state.users["rontube"] = FakeUser("rontube", admin=True, password="pw")
    state.users["wdj"] = FakeUser("wdj", admin=False, password="wdj-pw")
    fake = FakeSubsonic(state)
    srv_row = db.insert_media_server_row(
        {"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533", "username": "rontube", "password": "pw"}
    )
    rec = ServerRecord(
        id=srv_row["id"],
        kind="subsonic",
        name="Navidrome",
        source="settings",
        url="http://nav:4533",
        username="rontube",
        password="pw",
    )
    adapter = SubsonicMediaServer("http://nav:4533", "rontube", "pw", transport=fake.transport(), sleep=lambda _s: None)
    try:
        db.upsert_user("u-ron", "rontube", "ron@x")
        db.set_server_account("u-ron", rec.id, "wdj", seal("wdj-pw", data_dir=cfg.data_dir))
        playlist = Playlist(
            id="pl-ron",
            name="Ron Mix",
            tracks=[Track("Song A", "Artist 1", "Album X")],
        )
        options = PlaylistSyncOptions.from_config(cfg, db=db)

        results = sync_subsonic_for_users(db, cfg, rec, adapter, playlist, ["rontube"], options)
        assert len(results) == 1
        assert results[0].success is True
        assert [(p.owner, p.name, p.entries) for p in fake.state.playlists.values()] == [
            ("wdj", "Ron Mix", ["s1"])
        ]
    finally:
        adapter.close()
        db.close()


def test_navidrome_owner_transfer_creates_and_updates_mapped_copies(tmp_path: Path) -> None:
    from tests.test_navidrome_admin import FakeNavidrome

    db = Database(":memory:")
    cfg = Config(plex_url="", plex_token="", data_dir=str(tmp_path))
    fake = FakeNavidrome(SONGS, password="pw")
    fake.add_user("nd-alice", "alice", admin=False)
    fake.add_user("nd-bob", "bob", admin=False)
    srv_row = db.insert_media_server_row(
        {"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533", "username": "admin", "password": "pw"}
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
    adapter = SubsonicMediaServer("http://nav:4533", "admin", "pw", transport=fake.transport(), sleep=lambda _s: None)
    try:
        db.upsert_user("u-alice", "alice", "alice@x")
        db.upsert_user("u-bob", "bob", "bob@x")
        db.link_server_user("u-alice", rec.id, "nd-alice", "alice", source="imported")
        db.link_server_user("u-bob", rec.id, "nd-bob", "bob", source="imported")

        pl = Playlist(
            id="pl-shared",
            name="Road Trip",
            tracks=[Track("Song A", "Artist 1", "Album X")],
        )
        options = PlaylistSyncOptions.from_config(cfg, db=db)

        results = sync_subsonic_for_users(db, cfg, rec, adapter, pl, ["alice", "bob"], options)
        assert [r.success for r in results] == [True, True]
        owners = sorted((p.owner, p.name, tuple(p.entries)) for p in fake.state.playlists.values())
        assert owners == [("alice", "Road Trip", ("s1",)), ("bob", "Road Trip", ("s1",))]

        alice_pl_id = db.get_server_playlist_id("pl-shared", rec.id, "u-alice")
        bob_pl_id = db.get_server_playlist_id("pl-shared", rec.id, "u-bob")
        assert alice_pl_id and bob_pl_id and alice_pl_id != bob_pl_id

        # Second sync updates the mapped playlists in place without creating duplicates
        pl_updated = Playlist(
            id="pl-shared",
            name="Road Trip",
            tracks=[Track("Song A", "Artist 1", "Album X"), Track("Song B", "Artist 1", "Album X")],
        )
        results2 = sync_subsonic_for_users(db, cfg, rec, adapter, pl_updated, ["alice", "bob"], options)
        assert [r.success for r in results2] == [True, True]
        assert len(fake.state.playlists) == 2
        assert fake.state.playlists[alice_pl_id].entries == ["s1", "s2"]
        assert fake.state.playlists[bob_pl_id].entries == ["s1", "s2"]
    finally:
        adapter.close()
        db.close()


def test_navidrome_owner_transfer_ignored_logs_once_and_saved_login_still_works(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    from trackseerr.media_servers import user_logins
    from tests.test_navidrome_admin import FakeNavidrome

    user_logins._WARNED_TRANSFER_SERVERS.clear()
    db = Database(":memory:")
    cfg = Config(plex_url="", plex_token="", data_dir=str(tmp_path))
    fake = FakeNavidrome(SONGS, password="pw", ignore_owner_change=True)
    fake.add_user("nd-alice", "alice", admin=False, password="alice-pw")
    fake.add_user("nd-bob", "bob", admin=False, password="bob-pw")
    srv_row = db.insert_media_server_row(
        {"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533", "username": "admin", "password": "pw"}
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
    adapter = SubsonicMediaServer("http://nav:4533", "admin", "pw", transport=fake.transport(), sleep=lambda _s: None)
    try:
        db.upsert_user("u-alice", "alice", "alice@x")
        db.upsert_user("u-bob", "bob", "bob@x")
        db.link_server_user("u-alice", rec.id, "nd-alice", "alice", source="imported")
        db.link_server_user("u-bob", rec.id, "nd-bob", "bob", source="imported")
        # Alice has a saved login (wins directly); Bob only has a link and transfer is ignored
        db.set_server_account("u-alice", rec.id, "alice", seal("alice-pw", data_dir=cfg.data_dir))

        pl = Playlist(
            id="pl-ign",
            name="Ignored Mix",
            tracks=[Track("Song A", "Artist 1", "Album X")],
        )
        options = PlaylistSyncOptions.from_config(cfg, db=db)

        with caplog.at_level(logging.WARNING, logger="trackseerr.media_servers.user_logins"):
            res1 = sync_subsonic_for_users(db, cfg, rec, adapter, pl, ["alice", "bob"], options)
            res2 = sync_subsonic_for_users(db, cfg, rec, adapter, pl, ["bob"], options)
        assert [r.success for r in res1] == [True, False]
        assert res1[1].skipped is False
        assert res2[0].success is False
        # Orphaned admin playlist copy was cleaned up and mapping removed
        assert db.get_server_playlist_id("pl-ign", rec.id, "u-bob") is None
        assert [(p.owner, p.name) for p in fake.state.playlists.values()] == [("alice", "Ignored Mix")]
        warn_msgs = [r.getMessage() for r in caplog.records if "ignored playlist owner transfer" in r.getMessage()]
        assert len(warn_msgs) == 1
    finally:
        user_logins._WARNED_TRANSFER_SERVERS.clear()
        adapter.close()
        db.close()


def test_no_account_targets_record_skipped_server_state(tmp_path: Path) -> None:
    from trackseerr.api.routes.playlists import _push_stored_to_first_server
    from trackseerr.api.routes.sync import _push_to_extra_server

    db, cfg, rec, adapter, fake, playlist, options = _setup(tmp_path)
    try:
        db.upsert_playlist(playlist.id, playlist.name, service="manual")
        db.upsert_user("u-ken", "kenhanson853", "ken@x")

        # Only target without an account -> skipped, not failed
        results = sync_subsonic_for_users(db, cfg, rec, adapter, playlist, ["kenhanson853"], options)
        assert len(results) == 1
        assert results[0].success is False
        assert results[0].skipped is True

        _push_to_extra_server(db, cfg, rec, adapter, playlist, ["kenhanson853"])
        state = db.list_playlist_server_state(playlist.id)[0]
        assert state["status"] == "skipped"
        assert state["error"] == "No Navidrome login saved for kenhanson853"

        matched_count, missing_count = _push_stored_to_first_server(db, cfg, rec, adapter, playlist, ["kenhanson853"])
        assert (matched_count, missing_count) == (0, 0)
        assert db.get_playlist(playlist.id)["sync_status"] == "skipped"
        assert db.list_playlist_server_state(playlist.id)[0]["status"] == "skipped"

        # One valid account + one skipped target -> overall server status is success
        db.set_server_account("u-alice", rec.id, "alice", seal("alice-pw", data_dir=cfg.data_dir))
        _push_to_extra_server(db, cfg, rec, adapter, playlist, ["kenhanson853", "alice"])
        state2 = db.list_playlist_server_state(playlist.id)[0]
        assert state2["status"] == "success"
        assert state2["matched"] == 2
        assert state2["missing"] == 0
        assert fake.state.playlists
    finally:
        adapter.close()
        db.close()

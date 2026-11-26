import json
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from trackseerr.api.routes.playlists import _sync_stored_playlist
from trackseerr.api.routes.sync import SyncState
from trackseerr.config import Config
from trackseerr.media_servers import registry
from trackseerr.media_servers.base import MediaServerConnectionError, PlaylistSyncOptions
from trackseerr.models import Playlist, SyncResult, Track
from trackseerr.storage import Database
from trackseerr.sync import SyncCoordinator


def test_sync_coordinator_cycle():
    cfg = Config(
        plex_url="http://localhost:32400",
        plex_token="token",
        spotify_client_id="cid",
        spotify_client_secret="csecret",
        spotify_user_id="sp_user",
        deezer_user_id="dz_user",
    )

    mock_plex = MagicMock()
    mock_spotify = MagicMock()
    mock_deezer = MagicMock()

    sp_pl = Playlist(id="sp_1", name="Spotify Top 50", tracks=[Track("Song A", "Artist A", "Album A")])
    dz_pl = Playlist(id="dz_1", name="Deezer Top 50", tracks=[Track("Song B", "Artist B", "Album B")])

    mock_spotify.fetch_all_playlists.return_value = [sp_pl]
    mock_deezer.fetch_all_playlists.return_value = [dz_pl]

    mock_plex.sync_playlist.side_effect = [
        SyncResult(playlist_name="Spotify Top 50", total_tracks=1, matched_tracks=1, missing_tracks=0, success=True),
        SyncResult(playlist_name="Deezer Top 50", total_tracks=1, matched_tracks=1, missing_tracks=0, success=True),
    ]

    coord = SyncCoordinator(
        config=cfg,
        plex_client=mock_plex,
        spotify_client=mock_spotify,
        deezer_client=mock_deezer,
    )

    results = coord.run_sync_cycle()

    assert len(results) == 2
    assert results[0].playlist_name == "Spotify Top 50"
    assert results[1].playlist_name == "Deezer Top 50"
    assert mock_plex.sync_playlist.call_count == 2


class _FakePlaylistAdapter:
    def __init__(
        self,
        *,
        matched: Optional[list[Track]] = None,
        missing: Optional[list[Track]] = None,
        success: bool = True,
        exc: Optional[Exception] = None,
    ) -> None:
        self._matched = matched if matched is not None else []
        self._missing = missing if missing is not None else []
        self._success = success
        self._exc = exc
        self.calls: list[tuple[Playlist, list[str], PlaylistSyncOptions]] = []

    def sync_playlist(
        self, playlist: Playlist, target_usernames: list[str], options: PlaylistSyncOptions
    ) -> list[SyncResult]:
        self.calls.append((playlist, list(target_usernames), options))
        if self._exc is not None:
            raise self._exc
        matched = list(self._matched)
        missing: list[Track] = []
        for track in self._missing:
            if options.db is not None and options.db.get_match_override(track.title, track.artist):
                matched.append(track)
            else:
                missing.append(track)
        return [
            SyncResult(
                playlist_name=playlist.name,
                total_tracks=len(playlist.tracks),
                matched_tracks=len(matched),
                missing_tracks=len(missing),
                success=self._success,
                missing=missing,
            )
        ]

    def match_playlist_tracks(
        self, tracks: list[Track], threshold: float = 0.9, db: Optional[Any] = None
    ) -> tuple[list[Track], list[Track]]:
        raise AssertionError("match_playlist_tracks must not be called after a push")


def _seed_fanout_db(tracks: Optional[list[dict[str, str]]] = None) -> tuple[Database, Config]:
    db = Database(":memory:")
    db.upsert_user("u1", "alice", "alice@example.com")
    db.upsert_playlist("imp_pl1", "Road Trip", service="spotify")
    payload = tracks if tracks is not None else [
        {"title": "Song 1", "artist": "Artist 1", "album": "Album 1"},
        {"title": "Song 2", "artist": "Artist 2", "album": "Album 2"},
    ]
    db.set_playlist_tracks_json("imp_pl1", json.dumps(payload))
    db.set_playlist_targets("imp_pl1", ["u1"])
    cfg = Config(plex_url="", plex_token="")
    return db, cfg


def _install_servers(
    monkeypatch: pytest.MonkeyPatch,
    db: Database,
    cfg: Config,
    specs: list[tuple[dict[str, Any], _FakePlaylistAdapter]],
) -> dict[str, str]:
    ids: dict[str, str] = {}
    adapters: dict[str, _FakePlaylistAdapter] = {}
    login_id: Optional[str] = None
    for row_data, adapter in specs:
        is_login = bool(row_data.pop("is_login", False))
        row = db.insert_media_server_row(row_data)
        ids[row["name"]] = row["id"]
        adapters[row["id"]] = adapter
        if is_login:
            login_id = row["id"]
    if login_id is not None:
        db.set_login_media_server(login_id)
    registry.reload(cfg, db)
    monkeypatch.setattr("trackseerr.media_servers.registry.build_adapter", lambda rec: adapters.get(rec.id))
    return ids


def test_pushes_to_every_playlist_server(monkeypatch: pytest.MonkeyPatch) -> None:
    db, cfg = _seed_fanout_db()
    try:
        t1 = Track("Song 1", "Artist 1", "Album 1")
        t2 = Track("Song 2", "Artist 2", "Album 2")
        plex_adapter = _FakePlaylistAdapter(matched=[t1, t2], missing=[])
        sub_adapter = _FakePlaylistAdapter(matched=[t1, t2], missing=[])
        jf_adapter = _FakePlaylistAdapter(matched=[t1, t2], missing=[])

        _install_servers(
            monkeypatch,
            db,
            cfg,
            [
                ({"kind": "plex", "name": "Home Plex", "url": "http://plex:32400", "is_login": True}, plex_adapter),
                ({"kind": "jellyfin", "name": "Jellyfin", "url": "http://jf1:8096"}, sub_adapter),
                ({"kind": "jellyfin", "name": "Jellyfin Off", "url": "http://jf2:8096", "use_playlists": False}, jf_adapter),
            ],
        )

        out = SyncState().execute_sync(db, cfg, None, None, None)
        assert out["status"] == "success"
        assert len(plex_adapter.calls) == 1
        assert len(sub_adapter.calls) == 1
        assert len(jf_adapter.calls) == 0
        assert plex_adapter.calls[0][1] == ["alice"]
        assert sub_adapter.calls[0][1] == ["alice"]
    finally:
        db.close()


def test_excluded_server_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    db, cfg = _seed_fanout_db()
    try:
        t1 = Track("Song 1", "Artist 1", "Album 1")
        plex_adapter = _FakePlaylistAdapter(matched=[t1], missing=[])
        sub_adapter = _FakePlaylistAdapter(matched=[t1], missing=[])

        ids = _install_servers(
            monkeypatch,
            db,
            cfg,
            [
                ({"kind": "plex", "name": "Plex", "url": "http://plex:32400", "is_login": True}, plex_adapter),
                ({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"}, sub_adapter),
            ],
        )
        db.set_playlist_server_excludes("imp_pl1", [ids["Navidrome"]])

        SyncState().execute_sync(db, cfg, None, None, None)
        assert len(plex_adapter.calls) == 1
        assert len(sub_adapter.calls) == 0
        states = db.list_playlist_server_state("imp_pl1")
        assert [s["server_id"] for s in states] == [ids["Plex"]]
    finally:
        db.close()


def test_extra_server_failure_does_not_change_playlist_status(monkeypatch: pytest.MonkeyPatch) -> None:
    db, cfg = _seed_fanout_db()
    try:
        t1 = Track("Song 1", "Artist 1", "Album 1")
        t2 = Track("Song 2", "Artist 2", "Album 2")
        plex_adapter = _FakePlaylistAdapter(matched=[t1, t2], missing=[])
        sub_adapter = _FakePlaylistAdapter(
            exc=MediaServerConnectionError("boom", safe_detail="connection refused")
        )

        ids = _install_servers(
            monkeypatch,
            db,
            cfg,
            [
                ({"kind": "plex", "name": "Plex", "url": "http://plex:32400", "is_login": True}, plex_adapter),
                ({"kind": "jellyfin", "name": "Jellyfin", "url": "http://jf:8096"}, sub_adapter),
            ],
        )

        out = SyncState().execute_sync(db, cfg, None, None, None)
        assert out["status"] == "success"
        assert out["stats"]["success_count"] == 1
        assert out["stats"]["total_matched"] == 2
        assert out["stats"]["total_missing"] == 0
        assert db.get_playlist("imp_pl1")["sync_status"] == "success"
        assert db.get_missing_tracks("imp_pl1") == []

        by_server = {s["server_id"]: s for s in db.list_playlist_server_state("imp_pl1")}
        assert by_server[ids["Plex"]]["status"] == "success"
        assert by_server[ids["Jellyfin"]]["status"] == "failed"
        assert by_server[ids["Jellyfin"]]["error"] == "connection refused"
    finally:
        db.close()


def test_primary_result_drives_missing_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    db, cfg = _seed_fanout_db()
    try:
        t1 = Track("Song 1", "Artist 1", "Album 1")
        t2 = Track("Song 2", "Artist 2", "Album 2")
        # Primary misses t2; secondary has both.
        plex_adapter = _FakePlaylistAdapter(matched=[t1], missing=[t2])
        sub_adapter = _FakePlaylistAdapter(matched=[t1, t2], missing=[])

        ids = _install_servers(
            monkeypatch,
            db,
            cfg,
            [
                ({"kind": "plex", "name": "Plex", "url": "http://plex:32400", "is_login": True}, plex_adapter),
                ({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"}, sub_adapter),
            ],
        )

        out = SyncState().execute_sync(db, cfg, None, None, None)
        assert out["stats"]["total_matched"] == 1
        assert out["stats"]["total_missing"] == 1
        assert [m["title"] for m in db.get_missing_tracks("imp_pl1")] == ["Song 2"]

        by_server = {s["server_id"]: s for s in db.list_playlist_server_state("imp_pl1")}
        assert (by_server[ids["Plex"]]["matched"], by_server[ids["Plex"]]["missing"]) == (1, 1)
        # Navidrome push is per-user and no account is saved/linked for the target, so it is skipped.
        assert by_server[ids["Navidrome"]]["status"] == "skipped"
        assert (by_server[ids["Navidrome"]]["matched"], by_server[ids["Navidrome"]]["missing"]) == (0, 0)
    finally:
        db.close()


def test_server_state_recorded_per_server(monkeypatch: pytest.MonkeyPatch) -> None:
    db, cfg = _seed_fanout_db()
    try:
        t1 = Track("Song 1", "Artist 1", "Album 1")
        t2 = Track("Song 2", "Artist 2", "Album 2")
        plex_adapter = _FakePlaylistAdapter(matched=[t1], missing=[t2], success=True)
        sub_adapter = _FakePlaylistAdapter(matched=[], missing=[t1, t2], success=False)

        ids = _install_servers(
            monkeypatch,
            db,
            cfg,
            [
                ({"kind": "plex", "name": "Plex", "url": "http://plex:32400", "is_login": True}, plex_adapter),
                ({"kind": "jellyfin", "name": "Jellyfin", "url": "http://jf:8096"}, sub_adapter),
            ],
        )

        SyncState().execute_sync(db, cfg, None, None, None)
        by_server = {s["server_id"]: s for s in db.list_playlist_server_state("imp_pl1")}
        assert by_server[ids["Plex"]]["status"] == "success"
        assert by_server[ids["Plex"]]["matched"] == 1
        assert by_server[ids["Plex"]]["missing"] == 1
        assert by_server[ids["Jellyfin"]]["status"] == "failed"
        assert by_server[ids["Jellyfin"]]["matched"] == 0
        assert by_server[ids["Jellyfin"]]["missing"] == 2

        # Direct-import fanout records per-server state and returns primary counts.
        matched, missing = _sync_stored_playlist(
            db, cfg, None, "imp_pl1", "Road Trip", "", "", [t1, t2], ["u1"]
        )
        assert (matched, missing) == (1, 1)
        assert len(plex_adapter.calls) == 2
        assert len(sub_adapter.calls) == 2
    finally:
        db.close()


def test_adding_server_changes_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    db, cfg = _seed_fanout_db()
    try:
        t1 = Track("Song 1", "Artist 1", "Album 1")
        plex_adapter = _FakePlaylistAdapter(matched=[t1], missing=[])
        sub_adapter = _FakePlaylistAdapter(matched=[t1], missing=[])

        plex_row = db.insert_media_server_row(
            {"kind": "plex", "name": "Plex", "url": "http://plex:32400"}
        )
        db.set_login_media_server(plex_row["id"])
        adapters = {plex_row["id"]: plex_adapter}
        registry.reload(cfg, db)
        monkeypatch.setattr("trackseerr.media_servers.registry.build_adapter", lambda rec: adapters.get(rec.id))

        state = SyncState()
        first = state.execute_sync(db, cfg, None, None, None)
        assert first["stats"]["skipped_unchanged"] == 0
        assert len(plex_adapter.calls) == 1

        second = state.execute_sync(db, cfg, None, None, None)
        assert second["stats"]["skipped_unchanged"] == 1
        assert len(plex_adapter.calls) == 1

        # Add a second playlist server: fingerprint changes, triggering a resync to both servers.
        sub_row = db.insert_media_server_row(
            {"kind": "jellyfin", "name": "Jellyfin", "url": "http://jf:8096"}
        )
        adapters[sub_row["id"]] = sub_adapter
        registry.reload(cfg, db)

        third = state.execute_sync(db, cfg, None, None, None)
        assert third["stats"]["skipped_unchanged"] == 0
        assert len(plex_adapter.calls) == 2
        assert len(sub_adapter.calls) == 1
    finally:
        db.close()


def test_not_installed_registry_uses_passed_server() -> None:
    db, cfg = _seed_fanout_db()
    try:
        assert registry.is_installed() is False
        t2 = Track("Song 2", "Artist 2", "Album 2")
        passed_plex = MagicMock()
        passed_plex.sync_playlist_to_users.return_value = [
            SyncResult("Road Trip", 2, 1, 1, True, missing=[t2])
        ]
        passed_plex.match_playlist_tracks.side_effect = AssertionError("must not be called after push")
        passed_plex.refresh_auto_mix_snapshots.return_value = 0

        out = SyncState().execute_sync(db, cfg, passed_plex, None, None)
        assert out["status"] == "success"
        assert out["stats"]["success_count"] == 1
        passed_plex.sync_playlist_to_users.assert_called_once()
        assert db.list_playlist_server_state("imp_pl1") == []
    finally:
        db.close()


def test_skip_rating_keys_only_for_login_plex(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(":memory:")
    try:
        cfg = Config(plex_url="", plex_token="")
        db.upsert_user("u1", "alice", "alice@example.com")
        db.upsert_playlist("plex_admin_42", "Adopted Mix", service="plex")
        db.set_playlist_tracks_json(
            "plex_admin_42",
            json.dumps([{"title": "Song 1", "artist": "Artist 1", "album": "Album 1"}]),
        )
        db.set_playlist_targets("plex_admin_42", ["u1"])
        db.upsert_plex_registry(
            "admin",
            "42",
            "Adopted Mix",
            "regular",
            "trackseerr",
            trackseerr_playlist_id="plex_admin_42",
        )

        t1 = Track("Song 1", "Artist 1", "Album 1")
        login_plex = _FakePlaylistAdapter(matched=[t1], missing=[])
        other_plex = _FakePlaylistAdapter(matched=[t1], missing=[])

        _install_servers(
            monkeypatch,
            db,
            cfg,
            [
                ({"kind": "plex", "name": "Other Plex", "url": "http://plex2:32400", "sort_order": 0}, other_plex),
                ({"kind": "plex", "name": "Login Plex", "url": "http://plex1:32400", "sort_order": 1, "is_login": True}, login_plex),
            ],
        )

        out = SyncState().execute_sync(db, cfg, None, None, None)
        assert out["status"] == "success"
        assert len(login_plex.calls) == 1
        assert len(other_plex.calls) == 1
        assert login_plex.calls[0][2].skip_item_ids == {"42"}
        assert other_plex.calls[0][2].skip_item_ids is None
    finally:
        db.close()


def test_subsonic_push_uses_user_logins(monkeypatch: pytest.MonkeyPatch) -> None:
    db, cfg = _seed_fanout_db()
    try:
        t1 = Track("Song 1", "Artist 1", "Album 1")
        sub_primary = _FakePlaylistAdapter(matched=[t1], missing=[])
        sub_extra = _FakePlaylistAdapter(matched=[t1], missing=[])

        ids = _install_servers(
            monkeypatch,
            db,
            cfg,
            [
                ({"kind": "subsonic", "name": "Nav Primary", "url": "http://nav1:4533", "sort_order": 0}, sub_primary),
                ({"kind": "subsonic", "name": "Nav Extra", "url": "http://nav2:4533", "sort_order": 1}, sub_extra),
            ],
        )

        routed: list[tuple[str, list[str]]] = []

        def fake_sync_subsonic(d, c, rec, adapter, playlist, target_usernames, options):
            routed.append((rec.id, list(target_usernames)))
            return [SyncResult(playlist.name, len(playlist.tracks), 1, 0, True)]

        monkeypatch.setattr("trackseerr.api.routes.sync.sync_subsonic_for_users", fake_sync_subsonic)

        out = SyncState().execute_sync(db, cfg, None, None, None)
        assert out["status"] == "success"
        assert routed == [(ids["Nav Primary"], ["alice"]), (ids["Nav Extra"], ["alice"])]
        assert sub_primary.calls == []
        assert sub_extra.calls == []
    finally:
        db.close()


def test_server_adopted_playlist_refreshes_from_source(monkeypatch: pytest.MonkeyPatch) -> None:
    from trackseerr.media_servers.subsonic import SubsonicMediaServer
    from tests.subsonic_fake import FakePlaylist, FakeSubsonic, default_state

    db = Database(":memory:")
    try:
        cfg = Config(plex_url="", plex_token="")
        db.upsert_user("u1", "alice", "alice@example.com")
        fake = FakeSubsonic(
            default_state([("Song 1", "Artist 1", "Album 1"), ("Song 2", "Artist 2", "Album 2")], password="pw")
        )
        fake.state.playlists["sub-1"] = FakePlaylist("sub-1", "Adopted Nav", "admin", ["s1", "s2"])
        sub_adapter = SubsonicMediaServer("http://nav1:4533", "admin", "pw", transport=fake.transport())

        sub_row = db.insert_media_server_row(
            {"kind": "subsonic", "name": "Nav Source", "url": "http://nav1:4533", "username": "admin", "password": "pw"}
        )
        dest_adapter = _FakePlaylistAdapter(
            matched=[Track("Song 1", "Artist 1", "Album 1"), Track("Song 2", "Artist 2", "Album 2")],
            missing=[],
        )
        dest_row = db.insert_media_server_row(
            {"kind": "jellyfin", "name": "JF Dest", "url": "http://jf:8096", "api_key": "k"}
        )
        records = registry.load_records(cfg, db)
        registry.install(records)
        rec_by_id = {r.id: r for r in records}
        monkeypatch.setattr(
            registry,
            "build_adapter",
            lambda rec: sub_adapter if rec.id == sub_row["id"] else dest_adapter if rec.id == dest_row["id"] else None,
        )

        pl_id = f"subsonic_{sub_row['id'][:8]}_sub-1"
        db.upsert_playlist(pl_id, "Adopted Nav", service="subsonic")
        db.set_playlist_tracks_json(pl_id, json.dumps([{"title": "Song 1", "artist": "Artist 1", "album": "Album 1"}]))
        db.set_playlist_targets(pl_id, ["u1"])
        db.set_server_playlist_source(pl_id, sub_row["id"], "sub-1", "")
        db.set_playlist_server_excludes(pl_id, [sub_row["id"]])

        out = SyncState().execute_sync(db, cfg, None, None, None)
        assert out["status"] == "success"
        refreshed = json.loads(db.get_playlist(pl_id)["tracks_json"])
        assert [t["title"] for t in refreshed] == ["Song 1", "Song 2"]
        assert len(dest_adapter.calls) == 1
        assert [t.title for t in dest_adapter.calls[0][0].tracks] == ["Song 1", "Song 2"]
        assert [ep for ep, _ in fake.state.calls if ep in ("createPlaylist", "updatePlaylist")] == []
        assert rec_by_id[sub_row["id"]].id == sub_row["id"]
    finally:
        registry._reset_for_tests()
        db.close()


def test_post_push_matching_receives_db(monkeypatch: pytest.MonkeyPatch) -> None:
    db, cfg = _seed_fanout_db()
    try:
        t1 = Track("Song 1", "Artist 1", "Album 1")
        t2 = Track("Song 2", "Artist 2", "Album 2")
        plex_adapter = _FakePlaylistAdapter(matched=[t1, t2], missing=[])
        jf_adapter = _FakePlaylistAdapter(matched=[t1, t2], missing=[])
        _install_servers(
            monkeypatch,
            db,
            cfg,
            [
                ({"kind": "plex", "name": "Plex", "url": "http://plex:32400", "sort_order": 0}, plex_adapter),
                ({"kind": "jellyfin", "name": "Jellyfin", "url": "http://jf:8096", "sort_order": 1}, jf_adapter),
            ],
        )

        out = SyncState().execute_sync(db, cfg, None, None, None)
        assert out["status"] == "success"
        assert [call[2].db for call in plex_adapter.calls] == [db]
        assert [call[2].db for call in jf_adapter.calls] == [db]
    finally:
        registry._reset_for_tests()
        db.close()


def test_override_matched_track_not_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    db, cfg = _seed_fanout_db()
    try:
        db.add_match_override("Song 2", "Artist 2", "999", "Song 2", "Artist 2")
        t1 = Track("Song 1", "Artist 1", "Album 1")
        t2 = Track("Song 2", "Artist 2", "Album 2")
        plex_adapter = _FakePlaylistAdapter(matched=[t1], missing=[t2])
        jf_adapter = _FakePlaylistAdapter(matched=[t1], missing=[t2])
        ids = _install_servers(
            monkeypatch,
            db,
            cfg,
            [
                ({"kind": "plex", "name": "Plex", "url": "http://plex:32400", "sort_order": 0}, plex_adapter),
                ({"kind": "jellyfin", "name": "Jellyfin", "url": "http://jf:8096", "sort_order": 1}, jf_adapter),
            ],
        )

        out = SyncState().execute_sync(db, cfg, None, None, None)
        assert out["status"] == "success"
        assert db.get_missing_tracks("imp_pl1") == []
        states = {s["server_id"]: s for s in db.list_playlist_server_state("imp_pl1")}
        assert states[ids["Plex"]]["matched"] == 2
        assert states[ids["Plex"]]["missing"] == 0
        assert states[ids["Jellyfin"]]["matched"] == 2
        assert states[ids["Jellyfin"]]["missing"] == 0
    finally:
        registry._reset_for_tests()
        db.close()


def test_skip_keys_follow_login_plex_not_position(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(":memory:")
    try:
        cfg = Config(plex_url="", plex_token="")
        db.upsert_user("u1", "alice", "alice@example.com")
        db.upsert_playlist("plex_admin_42", "Adopted Mix", service="plex")
        db.set_playlist_tracks_json(
            "plex_admin_42",
            json.dumps([{"title": "Song 1", "artist": "Artist 1", "album": "Album 1"}]),
        )
        db.set_playlist_targets("plex_admin_42", ["u1"])
        db.upsert_plex_registry(
            "admin",
            "42",
            "Adopted Mix",
            "regular",
            "trackseerr",
            trackseerr_playlist_id="plex_admin_42",
        )

        t1 = Track("Song 1", "Artist 1", "Album 1")
        primary_jf = _FakePlaylistAdapter(matched=[t1], missing=[])
        login_plex = _FakePlaylistAdapter(matched=[t1], missing=[])

        _install_servers(
            monkeypatch,
            db,
            cfg,
            [
                ({"kind": "jellyfin", "name": "Primary JF", "url": "http://jf:8096", "sort_order": 0, "is_primary": True}, primary_jf),
                ({"kind": "plex", "name": "Login Plex", "url": "http://plex1:32400", "sort_order": 1, "is_login": True}, login_plex),
            ],
        )

        out = SyncState().execute_sync(db, cfg, None, None, None)
        assert out["status"] == "success"
        assert len(primary_jf.calls) == 1
        assert len(login_plex.calls) == 1
        assert primary_jf.calls[0][2].skip_item_ids is None
        assert login_plex.calls[0][2].skip_item_ids == {"42"}
    finally:
        registry._reset_for_tests()
        db.close()



def test_server_adopted_keeps_snapshot_when_server_down(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import httpx
    from trackseerr.media_servers.subsonic import SubsonicMediaServer
    from tests.subsonic_fake import FakeSubsonic, default_state

    db = Database(":memory:")
    try:
        cfg = Config(plex_url="", plex_token="")
        db.upsert_user("u1", "alice", "alice@example.com")
        fake = FakeSubsonic(default_state([("Song 1", "Artist 1", "Album 1")], password="pw"))
        fake.state.transport_failure = lambda: httpx.ConnectError("down")
        sub_adapter = SubsonicMediaServer(
            "http://nav1:4533", "admin", "pw", transport=fake.transport(), sleep=lambda _: None
        )
        sub_row = db.insert_media_server_row(
            {"kind": "subsonic", "name": "Nav Source", "url": "http://nav1:4533", "username": "admin", "password": "pw"}
        )
        records = registry.load_records(cfg, db)
        registry.install(records)
        monkeypatch.setattr(registry, "build_adapter", lambda rec: sub_adapter if rec.id == sub_row["id"] else None)

        pl_id = f"subsonic_{sub_row['id'][:8]}_sub-1"
        snapshot = json.dumps([{"title": "Song 1", "artist": "Artist 1", "album": "Album 1"}])
        db.upsert_playlist(pl_id, "Adopted Nav", service="subsonic")
        db.set_playlist_tracks_json(pl_id, snapshot)
        db.set_server_playlist_source(pl_id, sub_row["id"], "sub-1", "")
        db.set_playlist_server_excludes(pl_id, [sub_row["id"]])

        with caplog.at_level(logging.WARNING):
            out = SyncState().execute_sync(db, cfg, None, None, None)
        assert out["status"] == "success"
        assert db.get_playlist(pl_id)["tracks_json"] == snapshot
        assert any("using stored snapshot" in r.getMessage() for r in caplog.records)
    finally:
        registry._reset_for_tests()
        db.close()


def test_missing_comes_from_push_not_second_match(monkeypatch: pytest.MonkeyPatch) -> None:
    from trackseerr.media_servers.subsonic import SubsonicMediaServer
    from tests.subsonic_fake import FakeSubsonic, default_state

    db = Database(":memory:")
    sub_adapter: Optional[SubsonicMediaServer] = None
    try:
        cfg = Config(plex_url="", plex_token="")
        db.upsert_user("u1", "admin", "admin@example.com")
        db.upsert_library_artist({"id": "art-1", "name": "Artist X"})
        db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": "Album Y"})
        db.upsert_library_track(
            {
                "id": "trk-mb",
                "album_id": "alb-1",
                "artist_id": "art-1",
                "title": "Local Title",
                "track_number": 1,
                "disc_number": 1,
                "duration_seconds": 180.0,
                "isrc": None,
                "mb_recording_id": "rec-123",
            }
        )
        db.upsert_library_file(
            {
                "id": "file-mb",
                "track_id": "trk-mb",
                "file_path": "/data/music/Artist X/Album Y/01 Local Title.flac",
                "relative_path": "01 Local Title.flac",
                "codec": "flac",
                "quality_name": "FLAC",
                "size_bytes": 123456,
            }
        )

        state = default_state([("Local Title", "Artist X", "Album Y")], password="pw")
        state.songs[0].path = "/srv/Artist X/Album Y/01 Local Title.flac"
        fake = FakeSubsonic(state)
        sub_adapter = SubsonicMediaServer("http://nav1:4533", "admin", "pw", transport=fake.transport(), sleep=lambda _: None)

        enricher = MagicMock()
        enricher.search_recordings.return_value = [
            {
                "id": "rec-123",
                "title": "Playlist Title",
                "score": 98,
                "length_ms": 180000,
                "artist_credit": "Artist X",
                "artist_names": ["Artist X"],
                "artist_ids": ["art-1"],
                "release_groups": [{"id": "rg-1", "primary_type": "Album", "secondary_types": []}],
            }
        ]
        monkeypatch.setattr("trackseerr.media_servers.subsonic.get_shared_enricher", lambda _db: enricher)

        match_calls: list[Any] = []
        orig_match = sub_adapter.match_playlist_tracks

        def tracked_match(tracks, threshold=0.9, db=None, enricher=None, budget=None):
            match_calls.append(enricher)
            return orig_match(tracks, threshold=threshold, db=db, enricher=enricher, budget=budget)

        monkeypatch.setattr(sub_adapter, "match_playlist_tracks", tracked_match)

        t_mb = Track("Playlist Title", "Artist X", "Album Y", duration_seconds=180.0)
        extra_adapter = _FakePlaylistAdapter(matched=[t_mb], missing=[])
        ids = _install_servers(
            monkeypatch,
            db,
            cfg,
            [
                ({"kind": "subsonic", "name": "Nav Primary", "url": "http://nav1:4533", "username": "admin", "password": "pw", "is_primary": True}, sub_adapter),
                ({"kind": "jellyfin", "name": "JF Extra", "url": "http://jf:8096", "sort_order": 1}, extra_adapter),
            ],
        )

        db.upsert_playlist("imp_mb", "MB Push", service="spotify")
        db.set_playlist_tracks_json(
            "imp_mb",
            json.dumps([{"title": "Playlist Title", "artist": "Artist X", "album": "Album Y"}]),
        )
        db.set_playlist_targets("imp_mb", ["u1"])

        out = SyncState().execute_sync(db, cfg, None, None, None)
        assert out["status"] == "success"
        assert out["stats"]["total_matched"] == 1
        assert out["stats"]["total_missing"] == 0
        assert db.get_missing_tracks("imp_mb") == []
        # Only the push itself ran match_playlist_tracks (with the MB enricher); no second match call.
        assert match_calls == [enricher]
        states = {s["server_id"]: s for s in db.list_playlist_server_state("imp_mb")}
        assert (states[ids["Nav Primary"]]["matched"], states[ids["Nav Primary"]]["missing"]) == (1, 0)
        assert (states[ids["JF Extra"]]["matched"], states[ids["JF Extra"]]["missing"]) == (1, 0)

        # Direct-import path (_sync_stored_playlist -> _push_stored_to_first_server) also uses push missing list.
        matched_count, missing_count = _sync_stored_playlist(
            db, cfg, None, "imp_mb", "MB Push", "", "", [t_mb], ["u1"]
        )
        assert (matched_count, missing_count) == (1, 0)
        assert len(match_calls) == 2
        assert db.get_missing_tracks("imp_mb") == []
    finally:
        if sub_adapter is not None:
            sub_adapter.close()
        registry._reset_for_tests()
        db.close()


def test_second_plex_adopted_playlist_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from trackseerr.media_servers.plex import PlexMediaServer

    db = Database(":memory:")
    try:
        cfg = Config(plex_url="", plex_token="")
        db.upsert_user("u1", "alice", "alice@example.com")

        audio_pl = SimpleNamespace(
            items=lambda: [
                SimpleNamespace(title="Song 1", grandparentTitle="Artist 1", parentTitle="Album 1"),
                SimpleNamespace(title="Song 2", grandparentTitle="Artist 2", parentTitle="Album 2"),
            ]
        )
        fake_plex_server = SimpleNamespace(fetchItem=lambda key: audio_pl if int(key) == 42 else None)
        second_plex_adapter = PlexMediaServer(SimpleNamespace(server=fake_plex_server, music_section=None))

        t1 = Track("Song 1", "Artist 1", "Album 1")
        t2 = Track("Song 2", "Artist 2", "Album 2")
        login_plex = _FakePlaylistAdapter(matched=[t1, t2], missing=[])
        dest_jf = _FakePlaylistAdapter(matched=[t1, t2], missing=[])

        login_row = db.insert_media_server_row(
            {"kind": "plex", "name": "Login Plex", "url": "http://plex1:32400", "sort_order": 0}
        )
        db.set_login_media_server(login_row["id"])
        second_row = db.insert_media_server_row(
            {"kind": "plex", "name": "Second Plex", "url": "http://plex2:32400", "sort_order": 1}
        )
        jf_row = db.insert_media_server_row(
            {"kind": "jellyfin", "name": "JF Dest", "url": "http://jf:8096", "sort_order": 2}
        )
        adapters = {
            login_row["id"]: login_plex,
            second_row["id"]: second_plex_adapter,
            jf_row["id"]: dest_jf,
        }
        registry.reload(cfg, db)
        monkeypatch.setattr("trackseerr.media_servers.registry.build_adapter", lambda rec: adapters.get(rec.id))

        pl_id = f"plex_{second_row['id'][:8]}_42"
        db.upsert_playlist(pl_id, "Second Plex Mix", service="plex")
        db.set_playlist_tracks_json(pl_id, json.dumps([{"title": "Song 1", "artist": "Artist 1", "album": "Album 1"}]))
        db.set_server_playlist_source(pl_id, second_row["id"], "42", "")
        db.set_playlist_server_excludes(pl_id, [second_row["id"]])
        db.set_playlist_targets(pl_id, ["u1"])

        out = SyncState().execute_sync(db, cfg, None, None, None)
        assert out["status"] == "success"
        refreshed = json.loads(db.get_playlist(pl_id)["tracks_json"])
        assert [t["title"] for t in refreshed] == ["Song 1", "Song 2"]
        assert len(dest_jf.calls) == 1
        assert [t.title for t in dest_jf.calls[0][0].tracks] == ["Song 1", "Song 2"]
        assert len(login_plex.calls) == 1
        assert login_plex.calls[0][2].skip_item_ids is None
    finally:
        registry._reset_for_tests()
        db.close()



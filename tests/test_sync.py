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
        return [
            SyncResult(
                playlist_name=playlist.name,
                total_tracks=len(playlist.tracks),
                matched_tracks=len(self._matched),
                missing_tracks=len(self._missing),
                success=self._success,
            )
        ]

    def match_playlist_tracks(
        self, tracks: list[Track], threshold: float = 0.9
    ) -> tuple[list[Track], list[Track]]:
        return list(self._matched), list(self._missing)


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
        assert (by_server[ids["Navidrome"]]["matched"], by_server[ids["Navidrome"]]["missing"]) == (2, 0)
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
        t1 = Track("Song 1", "Artist 1", "Album 1")
        passed_plex = MagicMock()
        passed_plex.sync_playlist_to_users.return_value = [
            SyncResult("Road Trip", 2, 1, 1, True)
        ]
        passed_plex.match_playlist_tracks.return_value = ([t1], [])
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


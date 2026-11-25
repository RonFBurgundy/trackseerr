"""Tests for Jellyfin play history polling and multi-server scrobble ingestion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from tests.jellyfin_fake import API_KEY, FakeJellyfin, FakePlay, FakeUser, default_state
from trackseerr.config import Config
from trackseerr.media_servers import registry
from trackseerr.media_servers.base import MediaServerConnectionError
from trackseerr.media_servers.jellyfin import JellyfinMediaServer
from trackseerr.scrobble_worker import ScrobbleWorker
from trackseerr.storage import Database

JF_URL = "http://jellyfin.local:8096"


@pytest.fixture(autouse=True)
def _reset_registry():
    registry._reset_for_tests()
    yield
    registry._reset_for_tests()


def _make_record(
    server_id: str,
    *,
    kind: str = "jellyfin",
    roles: frozenset[str] = frozenset(registry.ROLES),
) -> registry.ServerRecord:
    return registry.ServerRecord(
        id=server_id,
        kind=kind,
        name=server_id,
        source="db",
        url=JF_URL,
        api_key=API_KEY,
        roles=roles,
        is_login=False,
    )


def test_recent_plays_stops_at_since_and_maps_fields():
    state = default_state(
        [
            ("Song Old", "Artist Old", "Album Old"),
            ("Song Boundary", "Artist Boundary", "Album Boundary"),
            ("Song New", "Artist New", "Album New"),
        ]
    )
    state.plays = [
        FakePlay("u-admin", "s1", "2026-01-01T11:50:00Z"),
        FakePlay("u-admin", "s2", "2026-01-01T12:00:00Z"),
        FakePlay("u-admin", "s3", "2026-01-01T12:05:00Z"),
    ]
    fake = FakeJellyfin(state)
    adapter = JellyfinMediaServer(JF_URL, API_KEY, transport=fake.transport(), sleep=lambda _s: None)

    since = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    plays = adapter.recent_plays("u-admin", since=since)

    assert len(plays) == 1
    assert plays[0] == {
        "item_id": "s3",
        "artist": "Artist New",
        "title": "Song New",
        "album": "Album New",
        "duration_ms": 200_000,
        "played_at": datetime(2026, 1, 1, 12, 5, 0, tzinfo=timezone.utc),
    }


def test_recent_plays_skips_incomplete_items_and_uses_album_artist_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/u1/Items":
            return httpx.Response(
                200,
                json={
                    "Items": [
                        {
                            "Id": "missing-date",
                            "Name": "No Date",
                            "Artists": ["Artist A"],
                            "RunTimeTicks": 100_000_000,
                            "UserData": {},
                        },
                        {
                            "Id": "missing-artist",
                            "Name": "No Artist",
                            "Artists": [],
                            "AlbumArtist": "",
                            "UserData": {"LastPlayedDate": "2026-01-01T12:10:00Z"},
                        },
                        {
                            "Id": "missing-title",
                            "Name": "",
                            "Artists": ["Artist B"],
                            "UserData": {"LastPlayedDate": "2026-01-01T12:09:00Z"},
                        },
                        {
                            "Id": "album-artist-only",
                            "Name": "Valid Track",
                            "Artists": [],
                            "AlbumArtist": "Fallback Artist",
                            "Album": "",
                            "RunTimeTicks": 150_000_000,
                            "UserData": {"LastPlayedDate": "2026-01-01T12:08:00Z"},
                        },
                    ],
                    "TotalRecordCount": 4,
                },
            )
        return httpx.Response(404)

    adapter = JellyfinMediaServer(JF_URL, API_KEY, transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    plays = adapter.recent_plays("u1", since=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))

    assert len(plays) == 1
    assert plays[0]["item_id"] == "album-artist-only"
    assert plays[0]["artist"] == "Fallback Artist"
    assert plays[0]["title"] == "Valid Track"
    assert plays[0]["album"] is None
    assert plays[0]["duration_ms"] == 15_000


def test_worker_ingests_id_and_username_matches_skips_unknown_and_advances_cursor(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.set_plex_history_poll_minutes(5)
    # Imported Jellyfin user keeps Jellyfin id; username match has a different Trackseerr id.
    db.upsert_user("u-admin", "admin_other_name", "a@example.com", is_admin=True)
    db.upsert_user("plex-kid-id", "Kid", "k@example.com", is_admin=False)

    state = default_state(
        [
            ("Track One", "Artist One", "Album One"),
            ("Track Two", "Artist Two", "Album Two"),
            ("Track Three", "Artist Three", "Album Three"),
        ]
    )
    state.users.append(FakeUser("u-stranger", "stranger", False))
    now = datetime(2026, 1, 1, 12, 10, 0, tzinfo=timezone.utc)
    state.plays = [
        FakePlay("u-admin", "s1", "2026-01-01T12:08:00Z"),
        FakePlay("u-kid", "s2", "2026-01-01T12:09:00Z"),
        FakePlay("u-stranger", "s3", "2026-01-01T12:09:30Z"),
    ]
    fake = FakeJellyfin(state)
    adapter = JellyfinMediaServer(JF_URL, API_KEY, transport=fake.transport(), sleep=lambda _s: None)

    rec = _make_record("jf-1", roles=frozenset({registry.ROLE_SCROBBLES}))
    monkeypatch.setattr(registry, "build_adapter", lambda r: adapter)
    registry.install([rec])

    worker = ScrobbleWorker()
    result = worker.run_iteration(
        db, config=Config(plex_url="http://localhost:32400", plex_token="tok"), plex_client=None, now=now
    )

    assert result["ingested"] == 2
    assert db.get_scrobble_state("jellyfin_history:jf-1") == now.isoformat(timespec="seconds")

    admin_listens = db.list_listens("u-admin")
    assert len(admin_listens) == 1
    assert admin_listens[0]["source"] == "jellyfin_history"
    assert admin_listens[0]["rating_key"] == "jellyfin:s1"

    kid_listens = db.list_listens("plex-kid-id")
    assert len(kid_listens) == 1
    assert kid_listens[0]["source"] == "jellyfin_history"
    assert kid_listens[0]["rating_key"] == "jellyfin:s2"
    db.close()


def test_failing_jellyfin_server_keeps_cursor_and_does_not_stop_others(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.set_plex_history_poll_minutes(5)
    db.upsert_user("u-admin", "admin", "a@example.com", is_admin=True)
    initial_cursor = "2026-01-01T12:00:00+00:00"
    db.set_scrobble_state("jellyfin_history:jf-fail", initial_cursor)
    db.set_scrobble_state("jellyfin_history:jf-ok", initial_cursor)

    failing_adapter = MagicMock(spec=JellyfinMediaServer)
    failing_adapter.list_users.side_effect = MediaServerConnectionError("boom", safe_detail="HTTP 503")

    state = default_state([("Good Track", "Good Artist", "Good Album")])
    state.plays = [FakePlay("u-admin", "s1", "2026-01-01T12:04:00Z")]
    ok_adapter = JellyfinMediaServer(
        JF_URL, API_KEY, transport=FakeJellyfin(state).transport(), sleep=lambda _s: None
    )

    rec_fail = _make_record("jf-fail", roles=frozenset({registry.ROLE_SCROBBLES}))
    rec_ok = _make_record("jf-ok", roles=frozenset({registry.ROLE_SCROBBLES}))
    adapters = {"jf-fail": failing_adapter, "jf-ok": ok_adapter}
    monkeypatch.setattr(registry, "build_adapter", lambda r: adapters[r.id])
    registry.install([rec_fail, rec_ok])

    now = datetime(2026, 1, 1, 12, 6, 0, tzinfo=timezone.utc)
    worker = ScrobbleWorker()
    result = worker.run_iteration(
        db, config=Config(plex_url="http://localhost:32400", plex_token="tok"), plex_client=None, now=now
    )

    assert result["ingested"] == 1
    assert worker.errors == 1
    assert db.get_scrobble_state("jellyfin_history:jf-fail") == initial_cursor
    assert db.get_scrobble_state("jellyfin_history:jf-ok") == now.isoformat(timespec="seconds")
    db.close()


def test_role_off_and_subsonic_servers_are_not_polled(monkeypatch, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.set_plex_history_poll_minutes(5)

    jf_no_role = MagicMock(spec=JellyfinMediaServer)
    subsonic_with_role = MagicMock()

    rec_jf = _make_record("jf-off", kind="jellyfin", roles=frozenset({registry.ROLE_USERS}))
    rec_sub = _make_record("sub-on", kind="subsonic", roles=frozenset({registry.ROLE_SCROBBLES}))
    adapters = {"jf-off": jf_no_role, "sub-on": subsonic_with_role}
    monkeypatch.setattr(registry, "build_adapter", lambda r: adapters[r.id])
    registry.install([rec_jf, rec_sub])

    worker = ScrobbleWorker()
    now = datetime(2026, 1, 1, 12, 10, 0, tzinfo=timezone.utc)
    result = worker.run_iteration(
        db, config=Config(plex_url="http://localhost:32400", plex_token="tok"), plex_client=None, now=now, force=True
    )

    assert result["ingested"] == 0
    jf_no_role.list_users.assert_not_called()
    subsonic_with_role.list_users.assert_not_called()
    subsonic_with_role.recent_plays.assert_not_called()
    db.close()

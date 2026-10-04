"""Subsonic adapter against a REAL Navidrome (docs/INTEGRATION_TESTS.md). Opt-in: RUN_INTEGRATION=1.

The library is generated (tests/integration/navidrome/make_music.py), so nothing depends on the internet.
Assertions read the server back with raw Subsonic calls that do not go through the code under test.
"""

import hashlib
import secrets
import time
import uuid
from typing import Any, Iterator

import httpx
import pytest

from plex_playlist_sync.media_servers import (
    MediaServerAuthError,
    MediaServerError,
    PlaylistSyncOptions,
    SubsonicMediaServer,
)
from plex_playlist_sync.models import Playlist, Track
from tests.integration.navidrome.make_music import TRACKS

pytestmark = pytest.mark.integration


class Raw:
    """Raw Subsonic client for assertions and cleanup."""

    def __init__(self, url: str, user: str, password: str) -> None:
        self.url, self.user, self.password = url, user, password
        self.http = httpx.Client(timeout=30.0)

    def call(self, endpoint: str, **params: Any) -> dict[str, Any]:
        salt = secrets.token_hex(6)
        query: list[tuple[str, str]] = [
            ("u", self.user),
            ("t", hashlib.md5((self.password + salt).encode()).hexdigest()),
            ("s", salt),
            ("v", "1.16.1"),
            ("c", "it-raw"),
            ("f", "json"),
        ]
        for key, value in params.items():
            for item in value if isinstance(value, list) else [value]:
                query.append((key, str(item)))
        body = self.http.get(f"{self.url}/rest/{endpoint}", params=query).json()["subsonic-response"]
        assert body["status"] == "ok", body
        return body

    def playlist_titles(self, name: str) -> list[str]:
        for p in self.call("getPlaylists").get("playlists", {}).get("playlist", []):
            if p["name"] == name:
                return [e["title"] for e in self.call("getPlaylist", id=p["id"])["playlist"].get("entry", [])]
        raise AssertionError(f"playlist {name!r} not found")

    def playlist_id(self, name: str) -> str:
        return next(p["id"] for p in self.call("getPlaylists")["playlists"]["playlist"] if p["name"] == name)

    def delete_playlist(self, name: str) -> None:
        for p in self.call("getPlaylists").get("playlists", {}).get("playlist", []):
            if p["name"] == name:
                self.call("deletePlaylist", id=p["id"])

    def scanning(self) -> bool:
        return bool(self.call("getScanStatus")["scanStatus"]["scanning"])

    def wait_scan(self, seconds: float = 90.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.scanning():
                return
            time.sleep(0.5)
        raise AssertionError("Navidrome scan did not finish")


@pytest.fixture(scope="session")
def raw(navidrome_target) -> Raw:
    return Raw(*navidrome_target)


@pytest.fixture(scope="session")
def scanned(raw: Raw) -> Raw:
    """The generated library is scanned and all tracks are visible."""
    raw.call("startScan", fullScan="true")
    raw.wait_scan()
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        found = raw.call("search3", query="", songCount=100).get("searchResult3", {}).get("song", [])
        if len(found) >= len(TRACKS):
            return raw
        time.sleep(1.0)
    raise AssertionError("Navidrome did not index the generated tracks")


@pytest.fixture
def server(navidrome_target, scanned) -> Iterator[SubsonicMediaServer]:
    url, user, password = navidrome_target
    s = SubsonicMediaServer(url, user, password)
    yield s
    s.close()


@pytest.fixture
def name(raw: Raw) -> Iterator[str]:
    playlist_name = f"it-{uuid.uuid4().hex[:8]}"
    yield playlist_name
    raw.delete_playlist(playlist_name)


def playlist_of(playlist_name: str, *titles: str) -> Playlist:
    by_title = {t.title: t for t in TRACKS}
    tracks = [Track(by_title[t].title, by_title[t].artist, by_title[t].album) for t in titles]
    return Playlist(id="p", name=playlist_name, tracks=tracks)


T = {i: t.title for i, t in enumerate(TRACKS)}


def test_ping(server: SubsonicMediaServer) -> None:
    res = server.test_connection()
    assert res.ok and "Connected" in res.message


def test_wrong_password_maps_to_auth_error(navidrome_target) -> None:
    url, user, _ = navidrome_target
    bad = SubsonicMediaServer(url, user, "definitely-not-the-password")
    try:
        res = bad.test_connection()
        assert not res.ok and "40" in res.message
        with pytest.raises(MediaServerAuthError):
            bad.search_tracks("kettle")
    finally:
        bad.close()


def test_unreachable_server_reports_not_ok() -> None:
    dead = SubsonicMediaServer("http://127.0.0.1:1", "u", "p", timeout=2.0, sleep=lambda _s: None)
    try:
        assert dead.test_connection().ok is False
    finally:
        dead.close()


def test_search_finds_generated_tracks(server: SubsonicMediaServer) -> None:
    hits = server.search_tracks("kettle", limit=5)
    assert [h["title"] for h in hits] == ["Kettle Song"]
    assert hits[0]["artist"] == "Tessellate Orchard" and hits[0]["album"] == "Paper Lanterns"
    assert server.search_tracks("zzzz-no-such-track") == []


def test_match_track_with_remaster_noise_and_miss(server: SubsonicMediaServer) -> None:
    ref = server.match_track(Track("Harbour Lights", "Velvet Kiln", "Slow Weather"))
    assert ref is not None and ref.title == "Harbour Lights (2011 Remaster)"
    assert server.match_track(Track("Kettle Song", "Somebody Else", "")) is None
    matched, missing = server.match_playlist_tracks(
        [Track("Orange Static", "Velvet Kiln", ""), Track("Nope", "Nobody", ""), Track("Kettle Song", "Tessellate Orchard", "")]
    )
    assert [m.title for m in matched] == ["Orange Static", "Kettle Song"] and [t.title for t in missing] == ["Nope"]


def test_playlist_create_update_reorder_remove_and_idempotent(server: SubsonicMediaServer, raw: Raw, name: str) -> None:
    a, b, c, d = T[0], T[1], T[2], T[3]
    opts = PlaylistSyncOptions()
    (res,) = server.sync_playlist(playlist_of(name, a, b, c), [], opts)
    assert res.success and res.matched_tracks == 3
    assert raw.playlist_titles(name) == [a, b, c]
    playlist_id = raw.playlist_id(name)

    # reorder + drop + add in one sync
    (res,) = server.sync_playlist(playlist_of(name, c, a, d), [], opts)
    assert res.success
    assert raw.playlist_titles(name) == [c, a, d]
    assert raw.playlist_id(name) == playlist_id  # updated in place, not recreated

    # idempotent: a re-sync changes nothing
    server.sync_playlist(playlist_of(name, c, a, d), [], opts)
    assert raw.playlist_titles(name) == [c, a, d] and raw.playlist_id(name) == playlist_id

    # removal only
    server.sync_playlist(playlist_of(name, a), [], opts)
    assert raw.playlist_titles(name) == [a]

    # append mode only adds
    server.sync_playlist(playlist_of(name, d, b), [], PlaylistSyncOptions(append=True))
    assert raw.playlist_titles(name) == [a, d, b]


def test_description_and_other_account_target(server: SubsonicMediaServer, raw: Raw, name: str, navidrome_target) -> None:
    pl = playlist_of(name, T[0])
    pl.description = "Generated by the integration test"
    results = server.sync_playlist(pl, [navidrome_target[1], "someone-else"], PlaylistSyncOptions())
    assert [r.success for r in results] == [True, False]
    found = next(p for p in raw.call("getPlaylists")["playlists"]["playlist"] if p["name"] == name)
    assert found.get("comment") == "Generated by the integration test"


def test_large_playlist_round_trip_is_chunked(server: SubsonicMediaServer, raw: Raw, name: str) -> None:
    # 150 entries (the 6 tracks repeated) exceed the 100-id request chunk
    titles = [T[i % len(TRACKS)] for i in range(150)]
    (res,) = server.sync_playlist(playlist_of(name, *titles), [], PlaylistSyncOptions())
    assert res.success and res.matched_tracks == 150
    assert raw.playlist_titles(name) == titles


def test_refresh_library_triggers_a_scan(server: SubsonicMediaServer, raw: Raw) -> None:
    before = raw.call("getScanStatus")["scanStatus"]
    assert server.refresh_library() is True
    deadline = time.monotonic() + 30.0
    seen_change = False
    while time.monotonic() < deadline:
        after = raw.call("getScanStatus")["scanStatus"]
        if after.get("scanning") or after.get("lastScan") != before.get("lastScan"):
            seen_change = True
            break
        time.sleep(0.2)
    assert seen_change, "startScan did not start a scan"
    raw.wait_scan()


def test_list_users_is_supported_or_cleanly_unsupported(server: SubsonicMediaServer) -> None:
    try:
        users = server.list_users()
    except MediaServerError as exc:  # Navidrome does not implement getUsers on every release
        assert "Subsonic" in exc.safe_detail
    else:
        assert any(u.is_admin for u in users)

"""Jellyfin adapter against a REAL Jellyfin (docs/INTEGRATION_TESTS.md). Opt-in: RUN_INTEGRATION=1.

The library is generated (tests/integration/navidrome/make_music.py), so nothing depends on the internet. Assertions
read the server back with raw REST calls that do not go through the code under test.
"""

import time
import uuid
from typing import Any, Iterator

import httpx
import pytest

from plex_playlist_sync.media_servers import (
    JellyfinMediaServer,
    MediaServerAuthError,
    PlaylistSyncOptions,
)
from plex_playlist_sync.models import Playlist, Track
from tests.integration.conftest import JellyfinTarget
from tests.integration.navidrome.make_music import TRACKS

pytestmark = pytest.mark.integration


class Raw:
    """Raw Jellyfin client for assertions and cleanup."""

    def __init__(self, target: JellyfinTarget) -> None:
        self.t = target
        self.http = httpx.Client(
            base_url=target.url, timeout=60.0, headers={"Authorization": f'MediaBrowser Token="{target.api_key}"'}
        )

    def get(self, path: str, **params: Any) -> Any:
        resp = self.http.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    def playlists(self, user_id: str, name: str) -> list[dict[str, Any]]:
        items = self.get("/Items", IncludeItemTypes="Playlist", Recursive="true", UserId=user_id)["Items"]
        return [p for p in items if p["Name"] == name]

    def playlist_id(self, user_id: str, name: str) -> str:
        (found,) = self.playlists(user_id, name)
        return found["Id"]

    def titles(self, user_id: str, name: str) -> list[str]:
        pid = self.playlist_id(user_id, name)
        return [e["Name"] for e in self.get(f"/Playlists/{pid}/Items", UserId=user_id)["Items"]]

    def delete_playlists(self, name: str) -> None:
        for user_id in (self.t.admin_id, self.t.kid_id):
            for p in self.playlists(user_id, name):
                self.http.delete(f"/Items/{p['Id']}")


@pytest.fixture(scope="session")
def raw(jellyfin_target: JellyfinTarget) -> Raw:
    return Raw(jellyfin_target)


@pytest.fixture(scope="session")
def scanned(raw: Raw) -> Raw:
    """The generated library is scanned and all tracks are visible."""
    raw.http.post("/Library/Refresh")
    wanted = {t.title for t in TRACKS}
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        # Tracks first appear named after their file; the tag read (real titles) follows in the metadata pass.
        names = {i["Name"] for i in raw.get("/Items", IncludeItemTypes="Audio", Recursive="true")["Items"]}
        if names == wanted:
            return raw
        time.sleep(1.0)
    raise AssertionError("Jellyfin did not index the generated tracks")


@pytest.fixture
def server(jellyfin_target: JellyfinTarget, scanned: Raw) -> Iterator[JellyfinMediaServer]:
    s = JellyfinMediaServer(jellyfin_target.url, jellyfin_target.api_key, jellyfin_target.admin_name)
    yield s
    s.close()


@pytest.fixture
def name(raw: Raw) -> Iterator[str]:
    playlist_name = f"it-{uuid.uuid4().hex[:8]}"
    yield playlist_name
    raw.delete_playlists(playlist_name)


def playlist_of(playlist_name: str, *titles: str) -> Playlist:
    by_title = {t.title: t for t in TRACKS}
    return Playlist(
        id="p",
        name=playlist_name,
        tracks=[Track(by_title[t].title, by_title[t].artist, by_title[t].album) for t in titles],
    )


T = {i: t.title for i, t in enumerate(TRACKS)}


def test_ping(server: JellyfinMediaServer) -> None:
    res = server.test_connection()
    assert res.ok and "Connected" in res.message


def test_bad_key_maps_to_auth_error(jellyfin_target: JellyfinTarget) -> None:
    bad = JellyfinMediaServer(jellyfin_target.url, "definitely-not-a-key")
    try:
        res = bad.test_connection()
        assert not res.ok and "401" in res.message
        with pytest.raises(MediaServerAuthError):
            bad.search_tracks("kettle")
    finally:
        bad.close()


def test_unreachable_server_reports_not_ok() -> None:
    dead = JellyfinMediaServer("http://127.0.0.1:1", "k", timeout=2.0, sleep=lambda _s: None)
    try:
        assert dead.test_connection().ok is False
    finally:
        dead.close()


def test_scan_then_search_finds_generated_tracks(server: JellyfinMediaServer) -> None:
    hits = server.search_tracks("kettle", limit=5)
    assert [h["title"] for h in hits] == ["Kettle Song"]
    assert hits[0]["artist"] == "Tessellate Orchard" and hits[0]["album"] == "Paper Lanterns"
    assert hits[0]["duration"] and 0.5 < hits[0]["duration"] < 3.0  # 40 silent frames, about one second
    assert server.search_tracks("zzzz-no-such-track") == []


def test_match_track_with_remaster_noise_and_miss(server: JellyfinMediaServer) -> None:
    ref = server.match_track(Track("Harbour Lights", "Velvet Kiln", "Slow Weather"))
    assert ref is not None and ref.title == "Harbour Lights (2011 Remaster)"
    assert server.match_track(Track("Kettle Song", "Somebody Else", "")) is None


def test_playlist_create_reorder_remove_idempotent_and_append(
    server: JellyfinMediaServer, raw: Raw, name: str, jellyfin_target: JellyfinTarget
) -> None:
    uid = jellyfin_target.admin_id
    a, b, c, d = T[0], T[1], T[2], T[3]
    opts = PlaylistSyncOptions()
    (res,) = server.sync_playlist(playlist_of(name, a, b, c), [], opts)
    assert res.success and res.matched_tracks == 3
    assert raw.titles(uid, name) == [a, b, c]
    playlist_id = raw.playlist_id(uid, name)

    (res,) = server.sync_playlist(playlist_of(name, c, a, d), [], opts)  # reorder + drop + add
    assert res.success
    assert raw.titles(uid, name) == [c, a, d]
    assert raw.playlist_id(uid, name) == playlist_id  # updated in place, not recreated

    server.sync_playlist(playlist_of(name, d, c, a), [], opts)  # pure reorder (remove + append)
    assert raw.titles(uid, name) == [d, c, a]

    server.sync_playlist(playlist_of(name, d, c, a), [], opts)  # idempotent
    assert raw.titles(uid, name) == [d, c, a] and raw.playlist_id(uid, name) == playlist_id

    server.sync_playlist(playlist_of(name, a), [], opts)  # removal only
    assert raw.titles(uid, name) == [a]

    server.sync_playlist(playlist_of(name, d, b), [], PlaylistSyncOptions(append=True))
    assert raw.titles(uid, name) == [a, d, b]


def test_second_user_gets_its_own_playlist(
    server: JellyfinMediaServer, raw: Raw, name: str, jellyfin_target: JellyfinTarget
) -> None:
    results = server.sync_playlist(
        playlist_of(name, T[0], T[2]), [jellyfin_target.admin_name, jellyfin_target.kid_name, "nobody-here"], PlaylistSyncOptions()
    )
    assert [r.success for r in results] == [True, True, False]
    assert raw.titles(jellyfin_target.admin_id, name) == [T[0], T[2]]
    assert raw.titles(jellyfin_target.kid_id, name) == [T[0], T[2]]
    assert raw.playlist_id(jellyfin_target.admin_id, name) != raw.playlist_id(jellyfin_target.kid_id, name)


def test_another_users_public_playlist_with_the_same_name_is_never_modified(
    server: JellyfinMediaServer, raw: Raw, name: str, jellyfin_target: JellyfinTarget
) -> None:
    """Jellyfin lists other users' PUBLIC playlists for a user and lets an API key remove entries from them, so the
    adapter must prove ownership (an empty add authorised as the target: 403 for someone else's playlist)."""
    t = jellyfin_target
    created = raw.http.post("/Playlists", json={"Name": name, "Ids": [], "UserId": t.kid_id, "MediaType": "Audio", "IsPublic": True})
    created.raise_for_status()
    kid_playlist = created.json()["Id"]
    kid_songs = [i["Id"] for i in raw.get("/Items", IncludeItemTypes="Audio", Recursive="true")["Items"]][:2]
    raw.http.post(f"/Playlists/{kid_playlist}/Items", params={"Ids": ",".join(kid_songs), "UserId": t.kid_id}).raise_for_status()
    before = [e["PlaylistItemId"] for e in raw.get(f"/Playlists/{kid_playlist}/Items", UserId=t.kid_id)["Items"]]

    (result,) = server.sync_playlist(playlist_of(name, T[3]), [t.admin_name], PlaylistSyncOptions())

    assert result.success
    assert [e["PlaylistItemId"] for e in raw.get(f"/Playlists/{kid_playlist}/Items", UserId=t.kid_id)["Items"]] == before
    own = [p["Id"] for p in raw.playlists(t.admin_id, name) if p["Id"] != kid_playlist]
    assert len(own) == 1
    assert [e["Name"] for e in raw.get(f"/Playlists/{own[0]}/Items", UserId=t.admin_id)["Items"]] == [T[3]]


def test_full_reversal_and_repeated_tracks_collapse(
    server: JellyfinMediaServer, raw: Raw, name: str, jellyfin_target: JellyfinTarget
) -> None:
    uid = jellyfin_target.admin_id
    order = [T[i] for i in range(len(TRACKS))]
    (res,) = server.sync_playlist(playlist_of(name, *order), [], PlaylistSyncOptions())
    assert res.success and raw.titles(uid, name) == order
    pid = raw.playlist_id(uid, name)
    server.sync_playlist(playlist_of(name, *reversed(order)), [], PlaylistSyncOptions())  # full reversal
    assert raw.titles(uid, name) == list(reversed(order)) and raw.playlist_id(uid, name) == pid
    # Jellyfin refuses repeats in an update, so a track listed twice is synced once (and stays idempotent)
    repeated = playlist_of(name, order[0], order[1], order[0])
    server.sync_playlist(repeated, [], PlaylistSyncOptions())
    server.sync_playlist(repeated, [], PlaylistSyncOptions())
    assert raw.titles(uid, name) == [order[0], order[1]]


def test_list_users_includes_both_accounts(server: JellyfinMediaServer, jellyfin_target: JellyfinTarget) -> None:
    users = {u.name: u for u in server.list_users()}
    assert users[jellyfin_target.admin_name].is_admin and users[jellyfin_target.admin_name].id == jellyfin_target.admin_id
    assert not users[jellyfin_target.kid_name].is_admin


def test_refresh_library_is_accepted(server: JellyfinMediaServer) -> None:
    assert server.refresh_library() is True

"""Jellyfin adapter behaviour beyond the shared contract: auth header, retry, paging, ordering, multi-user."""

import logging
import random

import httpx
import pytest

from plex_playlist_sync.media_servers import (
    MediaServerAuthError,
    MediaServerConnectionError,
    MediaServerError,
    PlaylistSyncOptions,
)
from plex_playlist_sync.media_servers.jellyfin import (
    JellyfinMediaServer,
    plan_entry_changes,
)
from plex_playlist_sync.models import Playlist, Track
from plex_playlist_sync.redaction import redact_text
from tests.jellyfin_fake import API_KEY, FakeAudio, FakeJellyfin, default_state

SONGS = [
    ("Song A", "Artist 1", "Album X"),
    ("Song B", "Artist 1", "Album X"),
    ("Song C", "Artist 2", "Album Y"),
    ("Song D", "Artist 3", "Album Z"),
]


def make(songs=SONGS, user="admin", key=API_KEY, **kwargs):
    fake = FakeJellyfin(default_state(songs))
    sleeps: list[float] = []
    server = JellyfinMediaServer("http://srv/", key, user, transport=fake.transport(), sleep=sleeps.append, **kwargs)
    return server, fake, sleeps


def pl(*titles, name="Mix"):
    by = {t[0]: Track(*t) for t in SONGS}
    return Playlist(id="p", name=name, tracks=[by.get(t, Track(t, "Nobody", "")) for t in titles])


def order(fake: FakeJellyfin, name="Mix", owner="u-admin"):
    (p,) = [x for x in fake.state.playlists.values() if x.name == name and x.owner_id == owner]
    return [fake.state.song(e.item_id).title for e in p.entries]


# ----------------------------------------------------------------------------- construction & auth


def test_requires_url_and_key():
    with pytest.raises(MediaServerError):
        JellyfinMediaServer("", "k")
    with pytest.raises(MediaServerAuthError):
        JellyfinMediaServer("http://x", "  ")


def test_key_travels_in_the_authorization_header_never_the_url():
    server, fake, _ = make()
    server.test_connection()
    server.search_tracks("song")
    server.sync_playlist(pl("Song A"), ["admin"], PlaylistSyncOptions())
    assert fake.state.headers_seen and all(h.startswith("MediaBrowser ") and f'Token="{API_KEY}"' in h for h in fake.state.headers_seen)
    assert all(API_KEY not in url for url in fake.state.urls_seen)


def test_bad_key_is_an_auth_error_and_test_connection_reports_it():
    server, _, _ = make(key="wrong")
    res = server.test_connection()
    assert res.ok is False and "wrong" not in res.message
    with pytest.raises(MediaServerAuthError) as info:
        server.refresh_library()
    assert "wrong" not in info.value.safe_detail


def test_test_connection_reports_server_and_version():
    server, _, _ = make()
    res = server.test_connection()
    assert res.ok and "fake-jellyfin" in res.message and "10.10.0" in res.message


def test_forbidden_is_reported_as_missing_permission():
    server, fake, _ = make()
    fake.state.respond_with = lambda m, p, n: httpx.Response(403)
    with pytest.raises(MediaServerAuthError, match="permission"):
        server.list_users()


def test_non_json_answer_is_a_clear_error():
    server, fake, _ = make()
    fake.state.respond_with = lambda m, p, n: httpx.Response(200, content=b"<html>proxy</html>")
    with pytest.raises(MediaServerError, match="Jellyfin server"):
        server.list_users()


def test_header_token_is_redacted_from_log_text():
    assert "SECRET" not in redact_text('Authorization: MediaBrowser Client="T", Token="SECRET"')


def test_httpx_logger_never_shows_the_key(caplog):
    server, _, _ = make()
    with caplog.at_level(logging.INFO, logger="httpx"):
        server.search_tracks("song")
    assert API_KEY not in caplog.text


# ----------------------------------------------------------------------------- retry


def test_reads_retry_on_5xx_and_429_with_backoff():
    server, fake, sleeps = make()
    fake.state.respond_with = lambda m, p, n: httpx.Response(503) if n == 1 else httpx.Response(429, headers={"Retry-After": "2"}) if n == 2 else None
    assert server.test_connection().ok
    assert len(sleeps) == 2 and sleeps[1] >= 2


def test_read_gives_up_after_three_attempts():
    server, fake, sleeps = make()
    fake.state.respond_with = lambda m, p, n: httpx.Response(503)
    with pytest.raises(MediaServerConnectionError, match="503"):
        server.list_users()
    assert fake.state.request_counts["GET /Users"] == 3


def test_read_retries_transport_failures():
    server, fake, _ = make()
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        return httpx.ConnectError("down")

    fake.state.transport_failure = boom
    with pytest.raises(MediaServerConnectionError):
        server.list_users()
    assert calls["n"] == 3


def test_write_is_not_replayed_on_5xx_or_transport_failure():
    server, fake, _ = make()
    fake.state.respond_with = lambda m, p, n: httpx.Response(502) if m == "POST" else None
    with pytest.raises(MediaServerConnectionError):
        server.refresh_library()
    assert fake.state.request_counts["POST /Library/Refresh"] == 1
    fake.state.respond_with = None
    attempts = []

    def slow():
        attempts.append(1)
        return httpx.ReadTimeout("slow")

    fake.state.transport_failure = slow
    with pytest.raises(MediaServerConnectionError):
        server.refresh_library()
    assert len(attempts) == 1


def test_write_is_retried_on_429_only():
    server, fake, sleeps = make()
    fake.state.respond_with = lambda m, p, n: httpx.Response(429) if m == "POST" and n == 1 else None
    assert server.refresh_library() is True
    assert fake.state.scans == 1 and len(sleeps) == 1


# ----------------------------------------------------------------------------- matching


def test_search_term_is_the_title_so_artist_decides_between_candidates():
    songs = [("Intro", f"Artist {i}", "A") for i in range(1, 6)] + [("Intro", "Wanted", "A")]
    server, _, _ = make(songs)
    ref = server.match_track(Track("Intro", "Wanted", "A"))
    assert ref is not None and ref.artist == "Wanted"


def test_match_pages_past_the_server_page_cap():
    songs = [("Common", f"Other {i}", "A") for i in range(120)] + [("Common", "Target", "A")]
    server, fake, _ = make(songs)
    fake.state.max_page = 30
    ref = server.match_track(Track("Common", "Target", "A"))
    assert ref is not None and ref.artist == "Target"
    assert fake.state.request_counts["GET /Items"] > 1


def test_duration_beyond_tolerance_costs_a_match():
    server, fake, _ = make([("Song A", "Artist 1", "Album X")])
    fake.state.songs[0].seconds = 200
    near = Track("Song A", "Artist 1", "Album X", duration_seconds=202)
    far = Track("Song A", "Artist 1", "Album X", duration_seconds=260)
    assert server.match_track(near, threshold=0.95) is not None
    assert server.match_track(far, threshold=0.95) is None


def test_pinned_match_override_wins():
    server, _, _ = make()

    class DB:
        def get_match_override(self, title, artist):
            return {"plex_rating_key": "s3"}

    ref = server.match_track(Track("Totally different", "Nobody", ""), db=DB())
    assert ref is not None and ref.id == "s3"


def test_search_tracks_shape_and_limit():
    server, _, _ = make()
    hits = server.search_tracks("song", limit=2)
    assert len(hits) == 2
    assert set(hits[0]) == {"id", "rating_key", "title", "artist", "album", "duration"} and hits[0]["duration"] == 200
    assert server.search_tracks("  ") == []


# ----------------------------------------------------------------------------- ordering


def entries(fake, name="Mix"):
    p = next(x for x in fake.state.playlists.values() if x.name == name)
    return [(e.entry_id, e.item_id) for e in p.entries]


@pytest.mark.parametrize("seed", range(60))
def test_plan_entry_changes_always_yields_the_desired_order(seed):
    rng = random.Random(seed)
    pool = [f"i{k}" for k in range(8)]
    current = [(f"e{k}", item) for k, item in enumerate(rng.sample(pool, rng.randint(0, 8)))]
    desired = rng.sample(pool, rng.randint(0, 8))
    remove, add = plan_entry_changes(current, desired)
    result = [item for entry, item in current if entry not in remove] + add
    assert result == desired


def test_plan_entry_changes_keeps_the_matching_prefix_untouched():
    cur = [("e1", "a"), ("e2", "x"), ("e3", "b"), ("e4", "c")]
    assert plan_entry_changes(cur, ["a", "b", "c"]) == (["e2"], [])
    assert plan_entry_changes(cur, ["a", "b", "c", "d"]) == (["e2"], ["d"])
    assert plan_entry_changes(cur, ["c", "a"]) == (["e1", "e2", "e3"], ["a"])


def test_plan_entry_changes_handles_duplicates():
    cur = [("e1", "a"), ("e2", "b"), ("e3", "a")]
    assert plan_entry_changes(cur, ["a", "b", "a", "a"]) == ([], ["a"])
    assert plan_entry_changes(cur, ["b", "a", "a"]) == (["e1"], ["a"])


def test_resync_keeps_playlist_id_and_issues_no_writes_when_unchanged():
    server, fake, _ = make()
    server.sync_playlist(pl("Song A", "Song B"), ["admin"], PlaylistSyncOptions())
    (pid,) = fake.state.playlists
    fake.state.calls.clear()
    server.sync_playlist(pl("Song A", "Song B"), ["admin"], PlaylistSyncOptions())
    assert list(fake.state.playlists) == [pid]
    assert [c for c in fake.state.calls if c[0] != "GET"] == []


def test_update_removes_adds_and_reorders_in_place():
    server, fake, _ = make()
    server.sync_playlist(pl("Song A", "Song B", "Song C"), ["admin"], PlaylistSyncOptions())
    (pid,) = fake.state.playlists
    before = {i: e for e, i in entries(fake)}
    server.sync_playlist(pl("Song A", "Song C", "Song D"), ["admin"], PlaylistSyncOptions())
    after = {i: e for e, i in entries(fake)}
    assert list(fake.state.playlists) == [pid] and order(fake) == ["Song A", "Song C", "Song D"]
    assert after["s1"] == before["s1"] and after["s3"] == before["s3"]  # untouched entries keep their entry id
    server.sync_playlist(pl("Song D", "Song C", "Song A"), ["admin"], PlaylistSyncOptions())  # full reversal
    assert list(fake.state.playlists) == [pid] and order(fake) == ["Song D", "Song C", "Song A"]
    assert not [c for c in fake.state.calls if "Move" in c[1]]  # Move needs a user session; never used


def test_large_playlist_is_created_whole_and_extended_in_chunks():
    songs = [(f"T{i:03d}", "A", "B") for i in range(130)]
    server, fake, _ = make(songs)
    pl130 = Playlist(id="p", name="Big", tracks=[Track(t, "A", "B") for t, _, _ in songs])
    server.sync_playlist(pl130, ["admin"], PlaylistSyncOptions())
    assert len(order(fake, "Big")) == 130
    fake.state.max_page = 40  # reading the playlist back needs several pages
    server.sync_playlist(Playlist(id="p", name="Big", tracks=list(reversed(pl130.tracks))), ["admin"], PlaylistSyncOptions())
    assert order(fake, "Big") == [t for t, _, _ in reversed(songs)]


def test_a_track_listed_twice_is_synced_once():
    server, fake, _ = make()
    (r,) = server.sync_playlist(pl("Song A", "Song B", "Song A"), ["admin"], PlaylistSyncOptions())
    assert r.success and r.matched_tracks == 3 and order(fake) == ["Song A", "Song B"]
    server.sync_playlist(pl("Song A", "Song B", "Song A"), ["admin"], PlaylistSyncOptions())
    assert order(fake) == ["Song A", "Song B"]


def test_append_mode_never_removes_or_reorders():
    server, fake, _ = make()
    server.sync_playlist(pl("Song A", "Song B"), ["admin"], PlaylistSyncOptions())
    server.sync_playlist(pl("Song D", "Song A"), ["admin"], PlaylistSyncOptions(append=True))
    assert order(fake) == ["Song A", "Song B", "Song D"]
    assert not [c for c in fake.state.calls if c[0] == "DELETE"]


# ----------------------------------------------------------------------------- users


def test_each_target_gets_its_own_playlist_for_its_own_user():
    server, fake, _ = make()
    results = server.sync_playlist(pl("Song A", "Song C"), ["admin", "KID"], PlaylistSyncOptions())
    assert [r.success for r in results] == [True, True]
    assert order(fake, owner="u-admin") == order(fake, owner="u-kid") == ["Song A", "Song C"]
    assert len(fake.state.playlists) == 2


def test_target_can_be_a_user_id_and_duplicates_are_pushed_once():
    server, fake, _ = make()
    results = server.sync_playlist(pl("Song A"), ["u-kid", "kid"], PlaylistSyncOptions())
    assert len(results) == 2 and all(r.success for r in results) and len(fake.state.playlists) == 1


def test_unknown_target_is_reported_not_written():
    server, fake, _ = make()
    results = server.sync_playlist(pl("Song A"), ["admin", "ghost"], PlaylistSyncOptions())
    assert results[0].success and not results[1].success and "ghost" in results[1].error
    assert len(fake.state.playlists) == 1


def test_default_account_is_the_configured_user_else_first_admin():
    server, fake, _ = make(user="kid")
    server.sync_playlist(pl("Song A"), [], PlaylistSyncOptions())
    assert list(p.owner_id for p in fake.state.playlists.values()) == ["u-kid"]
    server2, fake2, _ = make(user="")
    server2.sync_playlist(pl("Song A"), [], PlaylistSyncOptions())
    assert list(p.owner_id for p in fake2.state.playlists.values()) == ["u-admin"]


def test_unknown_default_user_is_an_unsuccessful_result():
    server, fake, _ = make(user="nobody")
    (r,) = server.sync_playlist(pl("Song A"), [], PlaylistSyncOptions())
    assert not r.success and "JELLYFIN_USER" in r.error and not fake.state.playlists


def test_list_users_maps_id_name_admin():
    server, _, _ = make()
    users = server.list_users()
    assert [(u.id, u.name, u.is_admin) for u in users] == [("u-admin", "admin", True), ("u-kid", "kid", False)]


def test_playlists_are_created_private():
    server, fake, _ = make()
    server.sync_playlist(pl("Song A"), ["admin"], PlaylistSyncOptions())
    (playlist,) = fake.state.playlists.values()
    assert playlist.public is False


def test_a_playlist_of_another_user_with_the_same_name_is_not_touched():
    server, fake, _ = make()
    server.sync_playlist(pl("Song A"), ["kid"], PlaylistSyncOptions())
    server.sync_playlist(pl("Song B"), ["admin"], PlaylistSyncOptions())
    assert order(fake, owner="u-kid") == ["Song A"] and order(fake, owner="u-admin") == ["Song B"]


def test_zero_matches_writes_nothing():
    server, fake, _ = make()
    results = server.sync_playlist(pl("Ghost"), ["admin", "kid"], PlaylistSyncOptions())
    assert [r.success for r in results] == [False, False] and not fake.state.playlists


def test_missing_csv_written_when_requested(tmp_path):
    server, _, _ = make()
    server.sync_playlist(pl("Song A", "Ghost"), ["admin"], PlaylistSyncOptions(write_missing_as_csv=True, data_dir=str(tmp_path)))
    assert any(tmp_path.rglob("*.csv"))


def test_song_without_runtime_has_no_duration():
    server, fake, _ = make([("Song A", "Artist 1", "Album X")])
    fake.state.songs.append(FakeAudio("s9", "Song Z", "Artist 1", "Album X", seconds=0))
    (hit,) = server.search_tracks("song z")
    assert hit["duration"] is None

"""Subsonic adapter behaviour beyond the shared contract: auth, retry/backoff, matching, ordering, secrets."""

import hashlib
import logging
import random
import re

import httpx
import pytest

from plex_playlist_sync.media_servers import (
    MediaServerAuthError,
    MediaServerConnectionError,
    MediaServerError,
    MediaServerUnsupported,
    PlaylistSyncOptions,
)
from plex_playlist_sync.media_servers.subsonic import (
    SubsonicMediaServer,
    _Song,
    plan_playlist_update,
    score_candidate,
)
from plex_playlist_sync.models import Playlist, Track
from tests.subsonic_fake import FakeSubsonic, default_state, raw_json

SONGS = [
    ("Song A", "Artist 1", "Album X"),
    ("Song B", "Artist 1", "Album X"),
    ("Song C", "Artist 2", "Album Y"),
    ("Song D", "Artist 3", "Album Z"),
]


def make(songs=SONGS, user="admin", password="pw", **kwargs):
    fake = FakeSubsonic(default_state(songs))
    sleeps: list[float] = []
    server = SubsonicMediaServer(
        "http://srv/", user, password, transport=fake.transport(), sleep=sleeps.append, **kwargs
    )
    return server, fake, sleeps


def pl(*titles, name="Mix", description=""):
    by = {t[0]: Track(*t) for t in SONGS}
    return Playlist(id="p", name=name, description=description, tracks=[by.get(t, Track(t, "Nobody", "")) for t in titles])


def titles(fake: FakeSubsonic, name="Mix"):
    st = fake.state
    (p,) = [p for p in st.playlists.values() if p.name == name]
    return [st.song(i).title for i in p.entries]


class TestAuth:
    def test_token_auth_is_salted_md5_and_password_never_sent(self) -> None:
        seen: list[httpx.Request] = []
        fake = FakeSubsonic(default_state(SONGS))

        def spy(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return fake.handle(request)

        server = SubsonicMediaServer("http://srv", "admin", "pw", transport=httpx.MockTransport(spy))
        assert server.test_connection().ok
        server.test_connection()
        q = [dict(httpx.QueryParams(r.url.query)) for r in seen]
        assert q[0]["u"] == "admin" and q[0]["v"] == "1.16.1" and q[0]["f"] == "json" and q[0]["c"]
        assert q[0]["t"] == hashlib.md5(("pw" + q[0]["s"]).encode()).hexdigest()
        assert "p" not in q[0] and "pw" not in str(seen[0].url)
        assert q[0]["s"] != q[1]["s"]  # fresh salt per request

    def test_wrong_password_is_auth_error(self) -> None:
        server, _, _ = make(password="nope")
        res = server.test_connection()
        assert not res.ok and "40" in res.message
        with pytest.raises(MediaServerAuthError):
            server.refresh_library()

    def test_requires_credentials_or_key(self) -> None:
        with pytest.raises(MediaServerAuthError):
            SubsonicMediaServer("http://srv", "admin", "")
        with pytest.raises(MediaServerError):
            SubsonicMediaServer("", "a", "b")

    def test_url_with_rest_suffix_and_slash(self) -> None:
        server = SubsonicMediaServer("http://srv/navidrome/rest/", "a", "b", transport=FakeSubsonic().transport())
        assert server._base == "http://srv/navidrome"

    def test_api_key_used_when_advertised(self) -> None:
        fake = FakeSubsonic(default_state(SONGS))
        fake.state.api_keys["KEY123"] = "admin"
        server = SubsonicMediaServer("http://srv", api_key="KEY123", transport=fake.transport())
        assert server.test_connection().ok
        assert server.search_tracks("song a")
        assert all("u" not in dict(params) for _, params in fake.state.calls)

    def test_api_key_refused_when_not_advertised(self) -> None:
        fake = FakeSubsonic(default_state(SONGS))
        fake.state.advertise_api_key = False
        fake.state.api_keys["KEY123"] = "admin"
        server = SubsonicMediaServer("http://srv", api_key="KEY123", transport=fake.transport())
        res = server.test_connection()
        assert not res.ok and "apiKeyAuthentication" in res.message
        assert fake.state.calls == []  # no authenticated call was attempted

    def test_bad_api_key(self) -> None:
        fake = FakeSubsonic(default_state(SONGS))
        server = SubsonicMediaServer("http://srv", api_key="WRONG", transport=fake.transport())
        with pytest.raises(MediaServerAuthError):
            server.refresh_library()


class TestSecretsNeverLeak:
    def test_logs_and_errors_hold_no_secret(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        caplog.set_level(logging.INFO, logger="httpx")  # cli.setup_logging (other tests) raises it to WARNING process-wide
        server, fake, _ = make(password="hunter2-secret")
        fake.state.users["admin"].password = "different"
        with pytest.raises(MediaServerAuthError) as info:
            server.search_tracks("x")
        fake.state.users["admin"].password = "hunter2-secret"
        fake.state.transport_failure = lambda: httpx.ConnectError("http://srv/rest/ping?t=TOKEN&s=SALT&u=admin")
        res = server.test_connection()
        blob = caplog.text + str(info.value) + info.value.safe_detail + res.message
        for secret in ("hunter2-secret", "TOKEN", "SALT"):
            assert secret not in blob
        # httpx logs request URLs at INFO: the auth query parameters must be redacted there
        auth_values = re.findall(r"[?&](?:t|s|u)=([^&\s]+)", caplog.text)
        assert auth_values and set(auth_values) == {"REDACTED"}

    def test_transport_error_detail_is_type_name_only(self) -> None:
        server, fake, _ = make()
        fake.state.transport_failure = lambda: httpx.ReadTimeout("http://srv/rest/ping?t=TOKEN")
        with pytest.raises(MediaServerConnectionError) as info:
            server.refresh_library()
        assert "ReadTimeout" in info.value.safe_detail and "TOKEN" not in info.value.safe_detail


class TestRetry:
    def test_429_then_ok_honours_retry_after(self) -> None:
        server, fake, sleeps = make()
        fake.state.respond_with = lambda ep, n: raw_json(429, {}, {"Retry-After": "2"}) if (ep == "ping" and n == 1) else None
        assert server.test_connection().ok
        assert fake.state.request_counts["ping"] == 2
        assert sleeps == [2.0]

    @pytest.mark.parametrize("code", [502, 503, 504])
    def test_5xx_retried_with_exponential_backoff_then_gives_up(self, code: int) -> None:
        server, fake, sleeps = make()
        fake.state.respond_with = lambda ep, n: raw_json(code, {}) if ep == "search3" else None
        with pytest.raises(MediaServerConnectionError):
            server.search_tracks("song")
        assert fake.state.request_counts["search3"] == 3
        assert sleeps == [0.5, 1.0]

    def test_500_is_not_retried(self) -> None:
        server, fake, sleeps = make()
        fake.state.respond_with = lambda ep, n: raw_json(500, {}) if ep == "search3" else None
        with pytest.raises(MediaServerConnectionError):
            server.search_tracks("song")
        assert fake.state.request_counts["search3"] == 1 and sleeps == []

    def test_read_retries_transport_failure_but_mutation_does_not(self) -> None:
        server, fake, sleeps = make()
        calls = {"n": 0}

        def flaky() -> Exception:
            calls["n"] += 1
            return httpx.ConnectError("boom")

        fake.state.transport_failure = flaky
        with pytest.raises(MediaServerConnectionError):
            server.search_tracks("song")
        assert calls["n"] == 3
        calls["n"] = 0
        with pytest.raises(MediaServerConnectionError):
            server.refresh_library()
        assert calls["n"] == 1  # a startScan / playlist write is never replayed after a transport failure

    @pytest.mark.parametrize("code", [502, 503, 504])
    @pytest.mark.parametrize("endpoint", ["createPlaylist", "updatePlaylist"])
    def test_playlist_writes_are_not_replayed_on_5xx(self, code: int, endpoint: str) -> None:
        server, fake, sleeps = make()
        if endpoint == "updatePlaylist":
            server.sync_playlist(pl("Song A"), [""], PlaylistSyncOptions())
        fake.state.respond_with = lambda ep, n: raw_json(code, {}) if ep == endpoint else None
        fake.state.request_counts.clear()
        with pytest.raises(MediaServerConnectionError):
            server.sync_playlist(pl("Song A", "Song B") if endpoint == "updatePlaylist" else pl("Song B", name="New"), [""], PlaylistSyncOptions())
        assert fake.state.request_counts[endpoint] == 1 and sleeps == []

    def test_playlist_write_is_retried_on_429(self) -> None:
        server, fake, sleeps = make()
        fake.state.respond_with = lambda ep, n: raw_json(429, {}) if (ep == "createPlaylist" and n == 1) else None
        server.sync_playlist(pl("Song A"), [""], PlaylistSyncOptions())
        assert fake.state.request_counts["createPlaylist"] == 2 and sleeps == [0.5]

    def test_html_response_is_a_clear_error(self) -> None:
        server, fake, _ = make()
        fake.state.respond_with = lambda ep, n: httpx.Response(200, text="<html>login</html>")
        res = server.test_connection()
        assert not res.ok and "Subsonic" in res.message

    def test_http_401_maps_to_auth(self) -> None:
        server, fake, _ = make()
        fake.state.respond_with = lambda ep, n: httpx.Response(401)
        with pytest.raises(MediaServerAuthError):
            server.refresh_library()


class TestMatching:
    def lib(self, *rows):
        return make(list(rows))[0]

    def test_exact_match(self) -> None:
        s = self.lib(("Hello", "Adele", "25"))
        ref = s.match_track(Track("Hello", "Adele", "25"))
        assert ref and ref.title == "Hello" and ref.id == "s1"

    def test_remaster_and_feat_noise_is_ignored(self) -> None:
        s = self.lib(("Heroes (2017 Remastered Version)", "David Bowie", "Heroes"))
        assert s.match_track(Track("Heroes", "David Bowie", "Heroes"))
        s2 = self.lib(("Get Lucky", "Daft Punk", "RAM"))
        assert s2.match_track(Track("Get Lucky (feat. Pharrell Williams)", "Daft Punk, Pharrell Williams", "Random Access Memories"))

    def test_different_album_still_matches(self) -> None:
        s = self.lib(("Hello", "Adele", "25"))
        assert s.match_track(Track("Hello", "Adele", "Greatest Hits"))

    def test_different_artist_does_not(self) -> None:
        s = self.lib(("Hello", "Lionel Richie", "Can't Slow Down"))
        assert s.match_track(Track("Hello", "Adele", "25")) is None

    def test_duration_outside_tolerance_penalised(self) -> None:
        song = _Song.from_api({"id": "1", "title": "Hello", "artist": "Adele", "album": "25", "duration": 295})
        base = Track("Hello", "Adele", "25")
        assert score_candidate(base, song) == pytest.approx(1.0)
        within = Track("Hello", "Adele", "25", duration_seconds=297.5)
        beyond = Track("Hello", "Adele", "25", duration_seconds=240.0)
        assert score_candidate(within, song) == pytest.approx(1.0)
        assert score_candidate(beyond, song) < 0.9
        s = self.lib(("Hello", "Adele", "25"))
        s.search_tracks("x")  # smoke
        assert s.match_track(Track("Hello", "Adele", "25", duration_seconds=240.0)) is None

    def test_threshold_is_honoured(self) -> None:
        s = self.lib(("Hello", "Adele", "25"))
        wanted = Track("Hello", "Adele", "Some Compilation")  # album differs: scores 0.95-ish, not 1.0
        assert s.match_track(wanted, threshold=0.99) is None
        assert s.match_track(wanted, threshold=0.9) is not None

    def test_best_candidate_wins(self) -> None:
        s = self.lib(("Hello (Live)", "Adele", "Live"), ("Hello", "Adele", "25"))
        ref = s.match_track(Track("Hello", "Adele", "25"))
        assert ref and ref.id == "s2"

    def test_match_override_pins_song(self) -> None:
        s = self.lib(("Totally Different", "Someone", "X"))

        class DB:
            def get_match_override(self, title, artist):
                return {"plex_rating_key": "s1"}

        ref = s.match_track(Track("Unfindable", "Nobody", ""), db=DB())
        assert ref and ref.id == "s1"

    def test_stale_override_falls_back_to_search(self) -> None:
        s = self.lib(("Hello", "Adele", "25"))

        class DB:
            def get_match_override(self, title, artist):
                return {"plex_rating_key": "gone"}

        ref = s.match_track(Track("Hello", "Adele", "25"), db=DB())
        assert ref and ref.id == "s1"

    def test_search_results_shape(self) -> None:
        s = self.lib(("Hello", "Adele", "25"))
        (hit,) = s.search_tracks("hello")
        assert hit["title"] == "Hello" and hit["artist"] == "Adele" and hit["rating_key"] == hit["id"] == "s1"
        assert s.search_tracks("   ") == []


class TestPlanPlaylistUpdate:
    def apply(self, current, desired):
        removals, adds = plan_playlist_update(current, desired)
        kept = [c for i, c in enumerate(current) if i not in set(removals)]
        return kept + adds

    @pytest.mark.parametrize(
        "current,desired",
        [
            ([], ["a"]),
            (["a", "b"], ["a", "b"]),
            (["a", "b", "c"], ["c", "a"]),
            (["a", "b", "c"], ["a", "c"]),
            (["a", "b"], ["a", "b", "c"]),
            (["a", "a", "b"], ["a", "b", "a"]),
            (["a", "b"], []),
        ],
    )
    def test_examples(self, current, desired) -> None:
        assert self.apply(current, desired) == desired

    def test_identical_is_a_noop(self) -> None:
        assert plan_playlist_update(["a", "b"], ["a", "b"]) == ([], [])

    def test_random_always_reaches_target(self) -> None:
        rng = random.Random(7)
        for _ in range(300):
            current = [rng.choice("abcde") for _ in range(rng.randint(0, 8))]
            desired = [rng.choice("abcdef") for _ in range(rng.randint(0, 8))]
            assert self.apply(current, desired) == desired

    def test_prefix_extension_only_appends(self) -> None:
        assert plan_playlist_update(["a", "b"], ["a", "b", "c"]) == ([], ["c"])


class TestSyncPlaylist:
    def test_update_uses_updatePlaylist_never_recreates(self) -> None:
        server, fake, _ = make()
        server.sync_playlist(pl("Song A", "Song B", "Song C"), ["admin"], PlaylistSyncOptions())
        (created,) = fake.state.playlists.values()
        server.sync_playlist(pl("Song C", "Song A", "Song D"), [], PlaylistSyncOptions())
        assert titles(fake) == ["Song C", "Song A", "Song D"]
        assert list(fake.state.playlists) == [created.id]  # same playlist id: not deleted / recreated
        endpoints = [e for e, _ in fake.state.calls]
        assert endpoints.count("createPlaylist") == 1 and "deletePlaylist" not in endpoints

    def test_resync_makes_no_write(self) -> None:
        server, fake, _ = make()
        server.sync_playlist(pl("Song A", "Song B"), [], PlaylistSyncOptions())
        before = len(fake.state.calls)
        server.sync_playlist(pl("Song A", "Song B"), [], PlaylistSyncOptions())
        new = [e for e, _ in fake.state.calls[before:]]
        assert "updatePlaylist" not in new and "createPlaylist" not in new

    def test_large_playlist_is_chunked_and_ordered(self) -> None:
        rows = [(f"Track {i:03d}", "Band", "Album") for i in range(250)]
        server, fake, _ = make(rows)
        playlist = Playlist("p", "Big", tracks=[Track(*r) for r in rows])
        (res,) = server.sync_playlist(playlist, [], PlaylistSyncOptions())
        assert res.success and res.matched_tracks == 250
        assert titles(fake, "Big") == [r[0] for r in rows]
        writes = [(e, p) for e, p in fake.state.calls if e in ("createPlaylist", "updatePlaylist")]
        assert all(len(p) <= 105 for _, p in writes)  # bounded GET size
        # reversing re-orders without exceeding the bound
        rev = Playlist("p", "Big", tracks=[Track(*r) for r in reversed(rows)])
        server.sync_playlist(rev, [], PlaylistSyncOptions())
        assert titles(fake, "Big") == [r[0] for r in reversed(rows)]

    def test_description_written_as_comment_when_enabled(self) -> None:
        server, fake, _ = make()
        server.sync_playlist(pl("Song A", description="Hello there"), [], PlaylistSyncOptions())
        (p,) = fake.state.playlists.values()
        assert p.comment == "Hello there"
        p.comment = ""
        server.sync_playlist(pl("Song A", description="Hello there"), [], PlaylistSyncOptions(add_description=False))
        assert p.comment == ""

    def test_other_users_playlists_are_not_touched(self) -> None:
        server, fake, _ = make()
        from tests.subsonic_fake import FakePlaylist

        fake.state.playlists["theirs"] = FakePlaylist("theirs", "Mix", "kid", ["s1"])
        server.sync_playlist(pl("Song B"), [], PlaylistSyncOptions())
        assert fake.state.playlists["theirs"].entries == ["s1"]
        mine = [p for p in fake.state.playlists.values() if p.owner == "admin"]
        assert len(mine) == 1 and [fake.state.song(i).title for i in mine[0].entries] == ["Song B"]

    def test_duplicate_source_tracks_keep_duplicates(self) -> None:
        server, fake, _ = make()
        server.sync_playlist(pl("Song A", "Song B", "Song A"), [], PlaylistSyncOptions())
        assert titles(fake) == ["Song A", "Song B", "Song A"]
        server.sync_playlist(pl("Song A", "Song B", "Song A"), [], PlaylistSyncOptions())
        assert titles(fake) == ["Song A", "Song B", "Song A"]

    def test_missing_csv_written_and_removed(self, tmp_path) -> None:
        server, fake, _ = make()
        opts = PlaylistSyncOptions(write_missing_as_csv=True, data_dir=str(tmp_path))
        server.sync_playlist(pl("Song A", "Ghost"), [], opts)
        csv_file = tmp_path / "Mix.csv"
        assert csv_file.exists() and "Ghost" in csv_file.read_text()
        server.sync_playlist(pl("Song A"), [], opts)
        assert not csv_file.exists()

    def test_target_matching_is_case_insensitive_and_other_account_rejected(self) -> None:
        server, fake, _ = make()
        results = server.sync_playlist(pl("Song A"), ["ADMIN", "kid"], PlaylistSyncOptions())
        assert [r.success for r in results] == [True, False]
        assert len([p for p in fake.state.playlists.values()]) == 1


class TestUsersAndLibrary:
    def test_list_users_admin(self) -> None:
        server, _, _ = make()
        assert [(u.name, u.is_admin) for u in server.list_users()] == [("admin", True), ("kid", False)]

    def test_list_users_non_admin_is_unsupported(self) -> None:
        server, _, _ = make(user="kid")
        with pytest.raises(MediaServerUnsupported):
            server.list_users()

    def test_capabilities_are_static_and_documented(self) -> None:
        server, _, _ = make()
        caps = server.capabilities
        assert caps.playlists and caps.library_refresh and caps.search
        assert not caps.users and not caps.mixes

    def test_refresh_true(self) -> None:
        server, fake, _ = make()
        assert server.refresh_library() is True and fake.state.scans == 1


def test_song_from_api_collects_opensubsonic_artists() -> None:
    song = _Song.from_api(
        {"id": "1", "title": "T", "artist": "A feat. B", "artists": [{"name": "A"}, {"name": "B"}], "duration": 10}
    )
    assert song.artists == ("A feat. B", "A", "B") and song.duration == 10.0


@pytest.mark.parametrize("sequential", [False, True])
def test_index_removals_are_sent_highest_first_and_correct_under_both_server_semantics(sequential: bool) -> None:
    server, fake, _ = make()
    fake.state.sequential_removal = sequential
    server.sync_playlist(pl("Song A", "Song B", "Song C", "Song D"), [""], PlaylistSyncOptions())
    fake.state.calls.clear()
    server.sync_playlist(pl("Song A", "Song C"), [""], PlaylistSyncOptions())  # drops indexes 1 and 3
    (update,) = [params for ep, params in fake.state.calls if ep == "updatePlaylist"]
    sent = [int(v) for k, v in update if k == "songIndexToRemove"]
    assert sent == sorted(sent, reverse=True) == [3, 1]
    assert titles(fake) == ["Song A", "Song C"]


def test_stale_override_lookup_failure_falls_back_to_search(caplog) -> None:
    server, fake, _ = make()

    class DB:
        def get_match_override(self, title, artist):
            return {"plex_rating_key": "gone"}

    fake.state.respond_with = lambda ep, n: raw_json(500, {}) if ep == "getSong" else None
    with caplog.at_level(logging.WARNING):
        ref = server.match_track(Track("Song A", "Artist 1", "Album X"), db=DB())
    assert ref is not None and ref.id == "s1"
    assert any("Pinned match" in r.getMessage() for r in caplog.records)

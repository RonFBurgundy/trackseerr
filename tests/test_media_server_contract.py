"""Behavioural contract every MediaServer implementation must satisfy.

Each implementation is driven through a small harness over one shared in-memory backend, so the same scenarios run
against ``FakeMediaServer`` (a direct implementation of the interface) and ``PlexMediaServer`` (the real adapter over a
duck-typed PlexClient that raises genuine plexapi/requests exceptions). Stage 3/4 adapters (Subsonic, Jellyfin) join by
adding a harness to ``HARNESSES``; the scenarios themselves do not change.

The Plex harness verifies the adapter's plumbing (delegation, ref mapping, option mapping, exception translation).
PlexClient's own matching and playlist logic is covered by tests/test_plex.py and tests/test_multi_user_sync.py.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

import httpx
import pytest
import requests
from plexapi.exceptions import BadRequest, NotFound, Unauthorized

from plex_playlist_sync.media_servers import (
    ConnectionTest,
    MediaServer,
    MediaServerAuthError,
    MediaServerConnectionError,
    MediaServerError,
    MediaServerNotFound,
    MediaServerUnsupported,
    PlaylistSyncOptions,
    PlexMediaServer,
    ServerCapabilities,
    ServerTrackRef,
    as_media_server,
    describe_error,
    plex_extras,
)
from plex_playlist_sync.media_servers.plex import refresh_mix_snapshots
from plex_playlist_sync.media_servers.subsonic import SubsonicMediaServer
from plex_playlist_sync.models import Playlist, SyncResult, Track
from tests.subsonic_fake import Fault as SubsonicFault
from tests.subsonic_fake import FakeSubsonic, default_state

LIBRARY = [
    Track("Song A", "Artist 1", "Album X"),
    Track("Song B", "Artist 1", "Album X"),
    Track("Song C", "Artist 2", "Album Y"),
    Track("Song D", "Artist 3", "Album Z"),
]
ADMIN = "admin"


def _key(track: Track) -> tuple[str, str]:
    return track.title.lower(), track.artist.lower()


@dataclass
class Backend:
    """Shared in-memory server state."""

    library: list[Track] = field(default_factory=lambda: list(LIBRARY))
    playlists: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    users: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"id": "1", "username": ADMIN, "email": "a@x", "thumb": "", "is_admin": True},
            {"id": "2", "username": "kid", "email": "", "thumb": "", "is_admin": False},
        ]
    )
    refreshes: int = 0
    reachable: bool = True
    failure: Optional[Callable[[], Exception]] = None

    def check(self) -> None:
        if self.failure is not None:
            raise self.failure()

    def find(self, track: Track) -> Optional[Track]:
        for t in self.library:
            if _key(t) == _key(track):
                return t
        return None

    def sync(self, playlist: Playlist, targets: Sequence[str], options: PlaylistSyncOptions) -> list[SyncResult]:
        self.check()
        matched = [t for t in (self.find(x) for x in playlist.tracks) if t is not None]
        missing = len(playlist.tracks) - len(matched)
        if not matched:
            return [
                SyncResult(playlist.name, len(playlist.tracks), 0, missing, False, "Zero tracks matched")
                for _ in (targets or [ADMIN])
            ]
        results = []
        for user in targets or [ADMIN]:
            titles = [t.title for t in matched]
            existing = self.playlists.get((user, playlist.name))
            if existing is not None and options.append:
                titles = existing + [t for t in titles if t not in existing]
            self.playlists[(user, playlist.name)] = titles
            results.append(SyncResult(playlist.name, len(playlist.tracks), len(matched), missing, True))
        return results


class FakeMediaServer(MediaServer):
    """Direct in-memory implementation of the interface (what a Subsonic/Jellyfin adapter must look like)."""

    kind = "fake"

    def __init__(self, backend: Backend) -> None:
        self.b = backend

    @property
    def capabilities(self) -> ServerCapabilities:
        return ServerCapabilities(playlists=True, users=True, library_refresh=True, search=True)

    def test_connection(self) -> ConnectionTest:
        return ConnectionTest(self.b.reachable, "ok" if self.b.reachable else "down")

    def match_track(self, track: Track, threshold: float = 0.9, db: Optional[Any] = None) -> Optional[ServerTrackRef]:
        self.b.check()
        t = self.b.find(track)
        return None if t is None else ServerTrackRef(id=t.title, title=t.title, artist=t.artist, album=t.album)

    def match_playlist_tracks(
        self, tracks: Sequence[Track], threshold: float = 0.9, db: Optional[Any] = None
    ) -> tuple[list[ServerTrackRef], list[Track]]:
        matched, missing = [], []
        for t in tracks:
            ref = self.match_track(t, threshold, db)
            (matched if ref else missing).append(ref or t)
        return matched, missing

    def sync_playlist(
        self, playlist: Playlist, targets: Sequence[str], options: PlaylistSyncOptions
    ) -> list[SyncResult]:
        return self.b.sync(playlist, targets, options)

    def refresh_library(self) -> bool:
        self.b.check()
        self.b.refreshes += 1
        return True

    def search_tracks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        self.b.check()
        hits = [t for t in self.b.library if query.lower() in t.title.lower()]
        return [{"title": t.title, "artist": t.artist, "album": t.album} for t in hits][:limit]

    def list_users(self):  # type: ignore[no-untyped-def]
        from plex_playlist_sync.media_servers import ServerUser

        self.b.check()
        return [ServerUser(id=u["id"], name=u["username"], is_admin=u["is_admin"]) for u in self.b.users]


class _PlexItem:
    def __init__(self, t: Track) -> None:
        self.ratingKey = abs(hash(t.title)) % 10_000
        self.title, self.grandparentTitle, self.parentTitle = t.title, t.artist, t.album


class FakePlexClient:
    """Duck-typed PlexClient over the shared backend; raises real plexapi/requests exceptions on failure."""

    def __init__(self, backend: Backend) -> None:
        self.b = backend
        self.music_section = "Music"

    def test_connection(self) -> tuple[bool, str]:
        return (self.b.reachable, "Connected to Fake (v1)" if self.b.reachable else "ConnectionError")

    def match_track(self, track: Track, threshold: float = 0.9, db: Optional[Any] = None) -> Optional[_PlexItem]:
        self.b.check()
        t = self.b.find(track)
        return None if t is None else _PlexItem(t)

    def match_playlist_tracks(self, tracks, threshold=0.9, db=None):  # type: ignore[no-untyped-def]
        matched, missing = [], []
        for t in tracks:
            item = self.match_track(t, threshold, db)
            (matched if item else missing).append(item or t)
        return matched, missing

    def sync_playlist(self, playlist, append=False, add_description=True, add_poster=True,  # type: ignore[no-untyped-def]
                      write_missing_as_csv=False, data_dir="/data", threshold=0.9):
        return self.b.sync(playlist, [], PlaylistSyncOptions(append=append))[0]

    def sync_playlist_to_users(self, playlist, target_usernames, append=False, add_description=True,  # type: ignore[no-untyped-def]
                               add_poster=True, write_missing_as_csv=False, data_dir="/data", threshold=0.9,
                               db=None, skip_rating_keys=None):
        return self.b.sync(playlist, target_usernames, PlaylistSyncOptions(append=append))

    def refresh_music_library(self) -> bool:
        self.b.check()
        self.b.refreshes += 1
        return True

    def search_library_tracks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        self.b.check()
        return [
            {"title": t.title, "artist": t.artist, "album": t.album}
            for t in self.b.library
            if query.lower() in t.title.lower()
        ][:limit]

    def get_home_users(self) -> list[dict[str, Any]]:
        self.b.check()
        return list(self.b.users)


@dataclass
class Harness:
    name: str
    backend: Backend
    server: MediaServer
    errors: dict[str, Callable[[], Exception]]
    multi_user: bool = True  # False: the adapter can only write to the configured account (Subsonic)

    def playlist(self, name: str, user: str = ADMIN) -> Optional[list[str]]:
        return self.backend.playlists.get((user, name))


def _fake() -> Harness:
    b = Backend()
    return Harness(
        "fake",
        b,
        FakeMediaServer(b),
        {
            "auth": lambda: MediaServerAuthError("nope"),
            "notfound": lambda: MediaServerNotFound("gone"),
            "connection": lambda: MediaServerConnectionError("down"),
        },
    )


def _plex() -> Harness:
    b = Backend()
    return Harness(
        "plex",
        b,
        PlexMediaServer(FakePlexClient(b)),  # type: ignore[arg-type]
        {
            "auth": lambda: Unauthorized("401 http://plex/?X-Plex-Token=SECRET"),
            "notfound": lambda: NotFound("404 missing"),
            "connection": lambda: requests.ConnectionError("http://plex/?X-Plex-Token=SECRET"),
        },
    )


class SubsonicBackend:
    """The Backend surface the scenarios use, over a fake Subsonic server's state."""

    def __init__(self, fake: FakeSubsonic) -> None:
        self.fake = fake
        self._reachable = True
        self._failure: Optional[Callable[[], Exception]] = None

    def _sync_hooks(self) -> None:
        st = self.fake.state
        failure = self._failure
        probe = failure() if failure is not None else None
        st.transport_failure = None
        st.fault = None
        if not self._reachable:
            st.transport_failure = lambda: httpx.ConnectError("http://srv/rest/ping?u=admin&t=SECRET&s=SECRET")
        elif isinstance(probe, SubsonicFault):
            st.fault = lambda: failure()  # type: ignore[misc,return-value]
        elif probe is not None:
            st.transport_failure = lambda: failure()  # type: ignore[misc,return-value]

    @property
    def reachable(self) -> bool:
        return self._reachable

    @reachable.setter
    def reachable(self, value: bool) -> None:
        self._reachable = value
        self._sync_hooks()

    @property
    def failure(self) -> Optional[Callable[[], Exception]]:
        return self._failure

    @failure.setter
    def failure(self, value: Optional[Callable[[], Exception]]) -> None:
        self._failure = value
        self._sync_hooks()

    @property
    def playlists(self) -> dict[tuple[str, str], list[str]]:
        st = self.fake.state
        out: dict[tuple[str, str], list[str]] = {}
        for p in st.playlists.values():
            out[(p.owner, p.name)] = [st.song(i).title for i in p.entries]  # type: ignore[union-attr]
        return out

    @property
    def refreshes(self) -> int:
        return self.fake.state.scans


def _subsonic() -> Harness:
    fake = FakeSubsonic(default_state([(t.title, t.artist, t.album) for t in LIBRARY]))
    backend = SubsonicBackend(fake)
    server = SubsonicMediaServer(
        "http://srv", ADMIN, "pw", transport=fake.transport(), sleep=lambda _s: None
    )
    return Harness(
        "subsonic",
        backend,  # type: ignore[arg-type]
        server,
        {
            "auth": lambda: SubsonicFault(40, "Wrong username or password"),
            "notfound": lambda: SubsonicFault(70, "Data not found"),
            "connection": lambda: httpx.ConnectError("http://srv/rest/ping?u=admin&t=SECRET&s=SECRET"),
        },
        multi_user=False,
    )


HARNESSES = {"fake": _fake, "plex": _plex, "subsonic": _subsonic}  # Stage 4: add a "jellyfin" harness here


@pytest.fixture(params=sorted(HARNESSES))
def h(request: pytest.FixtureRequest) -> Harness:
    return HARNESSES[request.param]()


def _pl(*titles: str, name: str = "Mix") -> Playlist:
    by_title = {t.title: t for t in LIBRARY}
    tracks = [by_title.get(t, Track(t, "Nobody", "None")) for t in titles]
    return Playlist(id="p1", name=name, tracks=tracks)


class TestIdentityAndCapabilities:
    def test_kind_and_capabilities(self, h: Harness) -> None:
        assert h.server.kind
        caps = h.server.capabilities
        assert isinstance(caps, ServerCapabilities)
        assert caps.playlists and caps.library_refresh
        assert set(caps.to_dict()) == {"playlists", "users", "mixes", "library_refresh"}

    def test_as_media_server_is_idempotent(self, h: Harness) -> None:
        assert as_media_server(h.server) is h.server
        assert as_media_server(None) is None


class TestMatching:
    def test_match_track_hit_and_miss(self, h: Harness) -> None:
        ref = h.server.match_track(Track("Song C", "Artist 2", "Album Y"))
        assert isinstance(ref, ServerTrackRef) and ref.title == "Song C"
        assert h.server.match_track(Track("Unknown", "Nobody", "")) is None

    def test_match_playlist_tracks_splits_and_keeps_order(self, h: Harness) -> None:
        pl = _pl("Song D", "Ghost", "Song A")
        matched, missing = h.server.match_playlist_tracks(pl.tracks)
        assert [m.title for m in matched] == ["Song D", "Song A"]
        assert [t.title for t in missing] == ["Ghost"]
        assert all(isinstance(m, ServerTrackRef) for m in matched)

    def test_search_tracks(self, h: Harness) -> None:
        hits = h.server.search_tracks("song b", limit=5)
        assert [x["title"] for x in hits] == ["Song B"]
        assert h.server.search_tracks("zzz") == []


class TestPlaylistSync:
    def test_create_keeps_source_order(self, h: Harness) -> None:
        results = h.server.sync_playlist(_pl("Song C", "Song A", "Song D"), [ADMIN], PlaylistSyncOptions())
        assert [r.success for r in results] == [True]
        assert h.playlist("Mix") == ["Song C", "Song A", "Song D"]
        assert results[0].matched_tracks == 3 and results[0].missing_tracks == 0

    def test_resync_is_idempotent(self, h: Harness) -> None:
        pl = _pl("Song A", "Song B")
        h.server.sync_playlist(pl, [ADMIN], PlaylistSyncOptions())
        h.server.sync_playlist(pl, [ADMIN], PlaylistSyncOptions())
        assert h.playlist("Mix") == ["Song A", "Song B"]

    def test_update_reorders_and_removes_dropped_tracks(self, h: Harness) -> None:
        h.server.sync_playlist(_pl("Song A", "Song B", "Song C"), [ADMIN], PlaylistSyncOptions())
        h.server.sync_playlist(_pl("Song C", "Song A"), [ADMIN], PlaylistSyncOptions())
        assert h.playlist("Mix") == ["Song C", "Song A"]

    def test_append_mode_only_adds(self, h: Harness) -> None:
        h.server.sync_playlist(_pl("Song A", "Song B"), [ADMIN], PlaylistSyncOptions())
        h.server.sync_playlist(_pl("Song B", "Song D"), [ADMIN], PlaylistSyncOptions(append=True))
        assert h.playlist("Mix") == ["Song A", "Song B", "Song D"]

    def test_missing_tracks_reported_and_skipped(self, h: Harness) -> None:
        (r,) = h.server.sync_playlist(_pl("Song A", "Ghost"), [ADMIN], PlaylistSyncOptions())
        assert r.success and r.matched_tracks == 1 and r.missing_tracks == 1
        assert h.playlist("Mix") == ["Song A"]

    def test_zero_matches_is_unsuccessful_not_an_exception(self, h: Harness) -> None:
        (r,) = h.server.sync_playlist(_pl("Ghost"), [ADMIN], PlaylistSyncOptions())
        assert r.success is False and r.matched_tracks == 0
        assert h.playlist("Mix") is None

    def test_multiple_targets_one_result_each(self, h: Harness) -> None:
        results = h.server.sync_playlist(_pl("Song A"), [ADMIN, "kid"], PlaylistSyncOptions())
        assert len(results) == 2
        assert results[0].success
        if h.multi_user:
            assert results[1].success
            assert h.playlist("Mix", "kid") == ["Song A"]
        else:  # one account only: the other target is reported, never silently written to the wrong account
            assert not results[1].success and "configured account" in results[1].error
            assert h.playlist("Mix", "kid") is None

    def test_no_targets_means_default_account(self, h: Harness) -> None:
        results = h.server.sync_playlist(_pl("Song A"), [], PlaylistSyncOptions())
        assert len(results) == 1 and results[0].success
        assert h.playlist("Mix") == ["Song A"]


class TestLibraryAndUsers:
    def test_refresh_library(self, h: Harness) -> None:
        assert h.server.refresh_library() is True
        assert h.backend.refreshes == 1

    def test_list_users(self, h: Harness) -> None:
        users = h.server.list_users()
        assert [u.name for u in users] == [ADMIN, "kid"]
        assert [u.is_admin for u in users] == [True, False]

    def test_list_users_unsupported_by_default(self) -> None:
        class NoUsers(FakeMediaServer):
            list_users = MediaServer.list_users  # type: ignore[assignment]

        with pytest.raises(MediaServerUnsupported):
            NoUsers(Backend()).list_users()


class TestConnection:
    def test_reachable(self, h: Harness) -> None:
        res = h.server.test_connection()
        assert res.ok is True and res.message

    def test_unreachable_reports_not_ok(self, h: Harness) -> None:
        h.backend.reachable = False
        res = h.server.test_connection()
        assert res.ok is False


OPERATIONS: dict[str, Callable[[MediaServer], Any]] = {
    "match_track": lambda s: s.match_track(Track("Song A", "Artist 1", "")),
    "match_playlist_tracks": lambda s: s.match_playlist_tracks([Track("Song A", "Artist 1", "")]),
    "sync_playlist": lambda s: s.sync_playlist(_pl("Song A"), [ADMIN], PlaylistSyncOptions()),
    "refresh_library": lambda s: s.refresh_library(),
    "search_tracks": lambda s: s.search_tracks("song"),
    "list_users": lambda s: s.list_users(),
}


class TestExceptionTranslation:
    @pytest.mark.parametrize("op", sorted(OPERATIONS))
    @pytest.mark.parametrize(
        "kind,expected",
        [("auth", MediaServerAuthError), ("notfound", MediaServerNotFound), ("connection", MediaServerConnectionError)],
    )
    def test_failures_surface_as_generic_errors(self, h: Harness, op: str, kind: str, expected: type) -> None:
        h.backend.failure = h.errors[kind]
        with pytest.raises(expected) as info:
            OPERATIONS[op](h.server)
        assert isinstance(info.value, MediaServerError)
        assert "SECRET" not in str(info.value) and "SECRET" not in info.value.safe_detail

    def test_describe_error_is_redacted(self, h: Harness) -> None:
        h.backend.failure = h.errors["connection"]
        with pytest.raises(MediaServerError) as info:
            h.server.refresh_library()
        assert "SECRET" not in describe_error(info.value)


class TestPlexSpecific:
    """Plex-only behaviour of the adapter: translation detail, extras gating, option mapping."""

    def test_translation_table(self) -> None:
        from plex_playlist_sync.media_servers.plex import translate_plex_error

        assert type(translate_plex_error(Unauthorized("x"))) is MediaServerAuthError
        assert type(translate_plex_error(NotFound("x"))) is MediaServerNotFound
        assert type(translate_plex_error(BadRequest("x"))) is MediaServerError
        assert type(translate_plex_error(requests.ReadTimeout("x"))) is MediaServerConnectionError

    def test_requests_error_detail_keeps_type_name_only(self) -> None:
        from plex_playlist_sync.media_servers.plex import translate_plex_error

        assert translate_plex_error(requests.ReadTimeout("http://p/?X-Plex-Token=SECRET")).safe_detail == "ReadTimeout"

    def test_unrelated_errors_are_not_swallowed(self) -> None:
        b = Backend(failure=lambda: ValueError("bug"))
        with pytest.raises(ValueError):
            PlexMediaServer(FakePlexClient(b)).refresh_library()  # type: ignore[arg-type]

    def test_extras_only_for_plex(self) -> None:
        client = FakePlexClient(Backend())
        plex = PlexMediaServer(client)  # type: ignore[arg-type]
        assert plex_extras(plex) is client and plex.plex_extras() is client
        assert plex_extras(FakeMediaServer(Backend())) is None
        assert plex_extras(None) is None
        assert refresh_mix_snapshots(FakeMediaServer(Backend()), object()) == 0

    def test_sync_options_map_to_plex_kwargs(self) -> None:
        from unittest.mock import MagicMock

        client = MagicMock()
        client.sync_playlist_to_users.return_value = []
        opts = PlaylistSyncOptions(append=True, add_poster=False, threshold=0.7, db="DB", skip_item_ids={"9"})
        PlexMediaServer(client).sync_playlist(_pl("Song A"), ["kid"], opts)
        kw = client.sync_playlist_to_users.call_args.kwargs
        assert kw["target_usernames"] == ["kid"] and kw["append"] is True and kw["add_poster"] is False
        assert kw["threshold"] == 0.7 and kw["db"] == "DB" and kw["skip_rating_keys"] == {"9"}

    def test_refresh_without_client_support_is_false(self) -> None:
        assert PlexMediaServer(object()).refresh_library() is False  # type: ignore[arg-type]

    def test_test_connection_normalises_legacy_return_shapes(self) -> None:
        from unittest.mock import MagicMock

        client = MagicMock()
        client.test_connection.return_value = {"online": True, "message": "hi"}
        assert PlexMediaServer(client).test_connection() == ConnectionTest(True, "hi")
        client.test_connection.return_value = False
        assert PlexMediaServer(client).test_connection() == ConnectionTest(False, "Plex unreachable")

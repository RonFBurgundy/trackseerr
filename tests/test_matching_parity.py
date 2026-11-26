"""Matching parity tests across Subsonic and Jellyfin adapters (mirroring Plex matching fixes)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.jellyfin_fake import API_KEY as JELLYFIN_KEY
from tests.jellyfin_fake import FakeAudio, FakeJellyfin, JellyfinState
from tests.jellyfin_fake import FakeUser as JfUser
from tests.subsonic_fake import FakeSong, FakeSubsonic, SubsonicState
from tests.subsonic_fake import FakeUser as SubUser
from trackseerr.media_servers.base import MediaServer, PlaylistSyncOptions
from trackseerr.media_servers.jellyfin import JellyfinMediaServer
from trackseerr.media_servers.subsonic import SubsonicMediaServer
from trackseerr.models import Playlist, Track
from trackseerr.storage import Database


def _build_subsonic(songs: list[tuple[str, str, str, str, tuple[str, ...]]]) -> tuple[MediaServer, FakeSubsonic]:
    state = SubsonicState(
        songs=[
            FakeSong(id=f"s{i}", title=t, artist=a, album=al, path=p, artists=arts)
            for i, (t, a, al, p, arts) in enumerate(songs, start=1)
        ],
        users={"admin": SubUser("admin", admin=True, password="pw")},
    )
    fake = FakeSubsonic(state)
    server = SubsonicMediaServer("http://srv/", "admin", "pw", transport=fake.transport(), sleep=lambda _s: None)
    return server, fake


def _build_jellyfin(songs: list[tuple[str, str, str, str, tuple[str, ...]]]) -> tuple[MediaServer, FakeJellyfin]:
    state = JellyfinState(
        songs=[
            FakeAudio(id=f"s{i}", title=t, artist=a, album=al, path=p, artists=arts)
            for i, (t, a, al, p, arts) in enumerate(songs, start=1)
        ],
        users=[JfUser("u-admin", "admin", admin=True)],
    )
    fake = FakeJellyfin(state)
    server = JellyfinMediaServer("http://srv/", JELLYFIN_KEY, "admin", transport=fake.transport(), sleep=lambda _s: None)
    return server, fake


def _make_server(
    adapter: str,
    songs: list[tuple[str, str, str, str, tuple[str, ...]]],
) -> tuple[MediaServer, object]:
    if adapter == "subsonic":
        return _build_subsonic(songs)
    return _build_jellyfin(songs)


def _seed_library_track(
    db: Database,
    *,
    track_id: str = "trk-1",
    title: str = "Karma Police",
    artist: str = "Radiohead",
    album: str = "OK Computer",
    isrc: str | None = None,
    mb_recording_id: str | None = None,
    file_path: str = "/music/Radiohead/OK Computer/06 Karma Police.flac",
) -> None:
    db.upsert_library_artist({"id": f"art-{track_id}", "name": artist})
    db.upsert_library_album({"id": f"alb-{track_id}", "artist_id": f"art-{track_id}", "title": album})
    db.upsert_library_track(
        {
            "id": track_id,
            "album_id": f"alb-{track_id}",
            "artist_id": f"art-{track_id}",
            "title": title,
            "track_number": 1,
            "disc_number": 1,
            "duration_seconds": 180.0,
            "isrc": isrc,
            "mb_recording_id": mb_recording_id,
        }
    )
    db.upsert_library_file(
        {
            "id": f"file-{track_id}",
            "track_id": track_id,
            "file_path": file_path,
            "relative_path": Path(file_path).name,
            "codec": "flac",
            "quality_name": "FLAC",
            "size_bytes": 123456,
        }
    )


@pytest.mark.parametrize("adapter", ["subsonic", "jellyfin"])
def test_dollar_sign_and_leading_the(adapter: str) -> None:
    server, _ = _make_server(
        adapter,
        [
            ("TiK ToK", "Ke$ha", "Animal", "", ()),
            ("I Gotta Feeling", "The Black Eyed Peas", "The E.N.D.", "", ()),
            ("TiK ToK", "HappyHappy", "Other", "", ()),
        ],
    )
    hit_dollar = server.match_track(Track("TiK ToK", "Kesha", "Animal"))
    assert hit_dollar is not None and hit_dollar.id == "s1"

    hit_the = server.match_track(Track("I Gotta Feeling", "Black Eyed Peas", "The E.N.D."))
    assert hit_the is not None and hit_the.id == "s2"

    server_rev, _ = _make_server(
        adapter,
        [
            ("TiK ToK", "Kesha", "Animal", "", ()),
            ("I Gotta Feeling", "Black Eyed Peas", "The E.N.D.", "", ()),
        ],
    )
    assert server_rev.match_track(Track("TiK ToK", "Ke$ha", "Animal")) is not None
    assert server_rev.match_track(Track("I Gotta Feeling", "The Black Eyed Peas", "The E.N.D.")) is not None
    assert server_rev.match_track(Track("TiK ToK", "HappyHappy", "Animal")) is None


@pytest.mark.parametrize("adapter", ["subsonic", "jellyfin"])
def test_second_credited_artist_matches(adapter: str) -> None:
    server, _ = _make_server(
        adapter,
        [
            ("Tataki", "Argy;Alok;Adam Beyer", "Tataki", "", ("Argy", "Alok", "Adam Beyer")),
            ("Alone", "Michael Bibi", "Alone", "", ()),
        ],
    )
    hit_joined = server.match_track(Track("Tataki", "Alok", "Tataki"))
    assert hit_joined is not None and hit_joined.id == "s1"

    hit_split = server.match_track(Track("Alone", "Wu-Tang Clan, Michael Bibi, Kettama", "Alone"))
    assert hit_split is not None and hit_split.id == "s2"


@pytest.mark.parametrize("adapter", ["subsonic", "jellyfin"])
def test_remix_does_not_match_original(adapter: str) -> None:
    server_remix_only, _ = _make_server(
        adapter,
        [
            ("A Very Long Song Title Here (Fred Falke Club Remix)", "M83", "Album", "", ()),
            ("Another Long Song Title Here (Live)", "M83", "Album", "", ()),
            ("Third Long Song Title Here (Acoustic)", "M83", "Album", "", ()),
            ("Studio Only Long Song Title", "M83", "Album", "", ()),
        ],
    )
    assert server_remix_only.match_track(Track("A Very Long Song Title Here", "M83", "Album")) is None
    assert server_remix_only.match_track(Track("Another Long Song Title Here", "M83", "Album")) is None
    assert server_remix_only.match_track(Track("Third Long Song Title Here", "M83", "Album")) is None
    assert server_remix_only.match_track(Track("Studio Only Long Song Title (Acoustic)", "M83", "Album")) is None

    server_both, _ = _make_server(
        adapter,
        [
            ("TiK ToK (Fred Falke club remix)", "Kesha", "Animal", "", ()),
            ("TiK ToK", "Kesha", "Animal", "", ()),
        ],
    )
    picked = server_both.match_track(Track("TiK ToK", "Kesha", "Animal"))
    assert picked is not None and picked.id == "s2"


@pytest.mark.parametrize("adapter", ["subsonic", "jellyfin"])
def test_censored_title_matches(adapter: str) -> None:
    server, _ = _make_server(
        adapter,
        [
            ("Fun Times", "Lily Allen", "Alright, Still", "", ()),
            ("Fuck You", "Lily Allen", "Alright, Still", "", ()),
        ],
    )
    matched = server.match_track(Track("F**k You", "Lily Allen", "Alright, Still"))
    assert matched is not None and matched.id == "s2"
    assert server.match_track(Track("F**k Off", "Lily Allen", "Alright, Still")) is None
    assert server.match_track(Track("F**k You", "Wrong Artist", "Alright, Still")) is None


@pytest.mark.parametrize("adapter", ["subsonic", "jellyfin"])
def test_common_title_found_via_artist_scope(adapter: str) -> None:
    songs = [("Common", f"Other {i}", "Other Album", "", ()) for i in range(160)]
    songs.append(("Common", "Target", "Target Album", "", ()))
    server, _ = _make_server(adapter, songs)

    hit = server.match_track(Track("Common", "Target", "Target Album"))
    assert hit is not None and hit.artist == "Target"

    hit_second_credit = server.match_track(Track("Common", "CoArtist, Target", "Target Album"))
    assert hit_second_credit is not None and hit_second_credit.artist == "Target"


@pytest.mark.parametrize("adapter", ["subsonic", "jellyfin"])
def test_isrc_hit_maps_by_path_tail(adapter: str, tmp_path: Path) -> None:
    db = Database(str(tmp_path / "isrc.db"))
    try:
        _seed_library_track(
            db,
            title="Karma Police",
            artist="Radiohead",
            album="OK Computer",
            isrc="GBAYE9701377",
            file_path="/data/music/Radiohead/OK Computer/06 Karma Police.flac",
        )
        server, _ = _make_server(
            adapter,
            [
                ("Karma Police", "Radiohead", "Greatest Hits", "/plex/Radiohead/Greatest Hits/06 Karma Police.flac", ()),
                ("Karma Police", "Radiohead", "OK Computer", r"D:\Music\Radiohead\OK Computer\06 Karma Police.flac", ()),
            ],
        )
        hit = server.match_track(Track("Karma Police", "Radiohead", "OK Computer", isrc="GBAYE9701377"), db=db)
        assert hit is not None and hit.id == "s2"
    finally:
        db.close()


@pytest.mark.parametrize("adapter", ["subsonic", "jellyfin"])
def test_musicbrainz_only_during_sync(adapter: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(str(tmp_path / "mb.db"))
    try:
        _seed_library_track(
            db,
            track_id="trk-mb",
            title="Local Title",
            artist="Artist X",
            album="Album Y",
            mb_recording_id="rec-123",
            file_path="/data/music/Artist X/Album Y/01 Local Title.flac",
        )
        server, _ = _make_server(
            adapter,
            [("Local Title", "Artist X", "Album Y", "/srv/Artist X/Album Y/01 Local Title.flac", ())],
        )
        enricher = MagicMock()
        enricher.search_recordings.side_effect = [
            [
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
            ],
            [],
            [],
        ]
        monkeypatch.setattr("trackseerr.media_servers.subsonic.get_shared_enricher", lambda _db: enricher)
        monkeypatch.setattr("trackseerr.media_servers.jellyfin.get_shared_enricher", lambda _db: enricher)
        monkeypatch.setattr("trackseerr.playlist_identity.DEFAULT_SYNC_MB_LOOKUPS", 2)
        monkeypatch.setattr("trackseerr.media_servers.subsonic.MusicBrainzBudget", lambda: __import__("trackseerr.playlist_identity", fromlist=["MusicBrainzBudget"]).MusicBrainzBudget(2))
        monkeypatch.setattr("trackseerr.media_servers.jellyfin.MusicBrainzBudget", lambda: __import__("trackseerr.playlist_identity", fromlist=["MusicBrainzBudget"]).MusicBrainzBudget(2))

        # Interactive route path (match_track without enricher/budget): never calls MusicBrainz.
        assert server.match_track(Track("Playlist Title", "Artist X", "Album Y", duration_seconds=180.0), db=db) is None
        enricher.search_recordings.assert_not_called()

        # Sync path resolves via MusicBrainz and respects the per-sync budget (2 of 3 tracks).
        pl = Playlist(
            id="p1",
            name="MB Mix",
            tracks=[
                Track("Playlist Title", "Artist X", "Album Y", duration_seconds=180.0),
                Track("Missing 1", "Nobody 1", "Nowhere 1"),
                Track("Missing 2", "Nobody 2", "Nowhere 2"),
            ],
        )
        results = server.sync_playlist(pl, ["admin"], PlaylistSyncOptions(db=db))
        assert len(results) == 1 and results[0].success and results[0].matched_tracks == 1 and results[0].missing_tracks == 2
        assert enricher.search_recordings.call_count == 2
    finally:
        db.close()


@pytest.mark.parametrize("adapter", ["subsonic", "jellyfin"])
def test_search_artist_title_query(adapter: str) -> None:
    server, _ = _make_server(
        adapter,
        [
            ("Song A", "Artist 1", "Album X", "", ()),
            ("Song C", "Artist 2", "Album Y", "", ()),
            ("Boom Boom Pow", "The Black Eyed Peas", "The E.N.D.", "", ()),
        ],
    )
    hits = server.search_tracks("Artist 2 Song C")
    assert [h["id"] for h in hits] == ["s2"]

    hits_multi = server.search_tracks("The Black Eyed Peas Boom Pow")
    assert [h["id"] for h in hits_multi] == ["s3"]

    hits_artist_only = server.search_tracks("Artist 2")
    assert [h["id"] for h in hits_artist_only] == ["s2"]


@pytest.mark.parametrize("adapter", ["subsonic", "jellyfin", "plex"])
def test_sync_result_lists_missing(adapter: str, tmp_path: Path) -> None:
    from types import SimpleNamespace
    from unittest.mock import patch
    from plexapi.exceptions import NotFound
    from trackseerr.clients.plex import PlexClient
    from trackseerr.media_servers.plex import PlexMediaServer

    pl = Playlist(
        id="pl-miss",
        name="Parity Mix",
        tracks=[
            Track("Song A", "Artist 1", "Album X"),
            Track("Not In Library", "Artist 2", "Album Y"),
        ],
    )
    if adapter == "plex":
        plex_track = SimpleNamespace(
            ratingKey="101",
            title="Song A",
            grandparentTitle="Artist 1",
            parentTitle="Album X",
            duration=180000,
        )
        admin_server = MagicMock()
        admin_server.myPlexAccount.return_value = SimpleNamespace(username="admin", id=1, email="", thumb="", users=lambda: [])
        admin_server.search.side_effect = lambda title, **kwargs: [plex_track] if title == "Song A" else []
        admin_server.playlist.side_effect = [NotFound("missing"), MagicMock()]
        with patch("trackseerr.clients.plex.PlexServer"):
            client = PlexClient("http://localhost:32400", "token")
        client.server = admin_server
        server: MediaServer = PlexMediaServer(client)
        single = server.sync_playlist(pl, [], PlaylistSyncOptions(data_dir=str(tmp_path)))
        assert len(single) == 1
        assert single[0].success is True
        assert single[0].matched_tracks == 1
        assert single[0].missing_tracks == 1
        assert [t.title for t in single[0].missing] == ["Not In Library"]

        admin_server.playlist.side_effect = [NotFound("missing"), MagicMock()]
        results = server.sync_playlist(pl, ["admin"], PlaylistSyncOptions(data_dir=str(tmp_path)))
    else:
        server, _ = _make_server(adapter, [("Song A", "Artist 1", "Album X", "", ())])
        results = server.sync_playlist(pl, ["admin"], PlaylistSyncOptions(data_dir=str(tmp_path)))

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].matched_tracks == 1
    assert results[0].missing_tracks == 1
    assert [t.title for t in results[0].missing] == ["Not In Library"]


@pytest.mark.parametrize("adapter", ["subsonic", "jellyfin", "plex"])
def test_unowned_song_gets_single_search(adapter: str, tmp_path: Path) -> None:
    """Indexes in ``library_missing`` get exactly one plain search; server-only songs still match."""
    from types import SimpleNamespace
    from unittest.mock import patch
    from trackseerr.clients.plex import PlexClient
    from trackseerr.media_servers.plex import PlexMediaServer

    pl = Playlist(
        id="pl-skip",
        name="Skip Unowned",
        tracks=[Track("Unowned Track", "No Artist", "No Album")],
    )
    opts = PlaylistSyncOptions(data_dir=str(tmp_path), library_missing={0})
    if adapter == "plex":
        admin_server = MagicMock()
        admin_server.myPlexAccount.return_value = SimpleNamespace(username="admin", id=1, email="", thumb="", users=lambda: [])
        admin_server.search.return_value = []
        with patch("trackseerr.clients.plex.PlexServer"):
            client = PlexClient("http://localhost:32400", "token")
        client.server = admin_server
        server: MediaServer = PlexMediaServer(client)
        results = server.sync_playlist(pl, [], opts)
        assert admin_server.search.call_count == 1
        assert len(results) == 1
        assert results[0].matched_tracks == 0
        assert results[0].missing_tracks == 1

        # Server-only song (present on Plex, absent from native library) still matches in 1 search.
        admin_server.search.reset_mock()
        admin_server.search.return_value = [
            SimpleNamespace(
                ratingKey=42,
                title="Unowned Track",
                grandparentTitle="No Artist",
                parentTitle="No Album",
                originalTitle="",
                type="track",
                duration=200000,
                media=[],
            )
        ]
        results_hit = server.sync_playlist(pl, [], opts)
        assert admin_server.search.call_count == 1
        assert results_hit[0].matched_tracks == 1
        assert results_hit[0].missing_tracks == 0
    elif adapter == "subsonic":
        server, fake = _make_server(adapter, [("Other Song", "Other Artist", "Other Album", "", ())])
        results = server.sync_playlist(pl, ["admin"], opts)
        assert len([ep for ep, _ in fake.state.calls if ep == "search3"]) == 1
        assert len(results) == 1
        assert results[0].matched_tracks == 0
        assert results[0].missing_tracks == 1

        server_hit, fake_hit = _make_server(adapter, [("Unowned Track", "No Artist", "No Album", "", ())])
        results_hit = server_hit.sync_playlist(pl, ["admin"], opts)
        assert len([ep for ep, _ in fake_hit.state.calls if ep == "search3"]) == 1
        assert results_hit[0].matched_tracks == 1
        assert results_hit[0].missing_tracks == 0
    else:
        server, fake = _make_server(adapter, [("Other Song", "Other Artist", "Other Album", "", ())])
        results = server.sync_playlist(pl, ["admin"], opts)
        assert len([p for _, p, q in fake.state.calls if p == "/Items" and q.get("IncludeItemTypes") == "Audio"]) == 1
        assert len(results) == 1
        assert results[0].matched_tracks == 0
        assert results[0].missing_tracks == 1

        server_hit, fake_hit = _make_server(adapter, [("Unowned Track", "No Artist", "No Album", "", ())])
        results_hit = server_hit.sync_playlist(pl, ["admin"], opts)
        assert len([p for _, p, q in fake_hit.state.calls if p == "/Items" and q.get("IncludeItemTypes") == "Audio"]) == 1
        assert results_hit[0].matched_tracks == 1
        assert results_hit[0].missing_tracks == 0


@pytest.mark.parametrize("adapter", ["subsonic", "jellyfin", "plex"])
def test_unowned_song_skips_musicbrainz(adapter: str) -> None:
    """Indexes in ``library_missing`` (``skip_search=True``) never invoke MusicBrainz resolution."""
    from types import SimpleNamespace
    from unittest.mock import patch
    from trackseerr.clients.plex import PlexClient

    track = Track("Unowned Track", "No Artist", "No Album")
    with patch("trackseerr.playlist_identity.resolve_with_musicbrainz") as mb_mock:
        if adapter == "plex":
            admin_server = MagicMock()
            admin_server.myPlexAccount.return_value = SimpleNamespace(
                username="admin", id=1, email="", thumb="", users=lambda: []
            )
            admin_server.search.return_value = []
            with patch("trackseerr.clients.plex.PlexServer"):
                client = PlexClient("http://localhost:32400", "token")
            client.server = admin_server
            client.match_track(track, db=MagicMock(), enricher=MagicMock(), budget=MagicMock(), skip_search=True)
        else:
            server, _ = _make_server(adapter, [("Other Song", "Other Artist", "Other Album", "", ())])
            server.match_track(track, db=MagicMock(), enricher=MagicMock(), budget=MagicMock(), skip_search=True)
        assert mb_mock.call_count == 0


@pytest.mark.parametrize("adapter", ["subsonic", "jellyfin", "plex"])
def test_library_hit_maps_by_path_without_fuzzy_search(adapter: str, tmp_path: Path) -> None:
    """A ``LibraryHit`` resolves via path tail without fuzzy searching; falls back when path mapping fails."""
    from types import SimpleNamespace
    from unittest.mock import patch
    from plexapi.exceptions import NotFound
    from trackseerr.clients.plex import PlexClient
    from trackseerr.media_servers.plex import PlexMediaServer
    from trackseerr.playlist_identity import LibraryHit

    hit = LibraryHit(
        track_id="trk-1",
        title="Local Name",
        artist="Artist 1",
        album="Album X",
        file_path="/music/Artist 1/Album X/01 Local Name.flac",
        via="metadata",
    )
    pl = Playlist(
        id="pl-hit",
        name="Hit Mix",
        tracks=[Track("Playlist Name", "Artist 1", "Album X")],
    )
    opts = PlaylistSyncOptions(data_dir=str(tmp_path), library_hits={0: hit})

    if adapter == "plex":
        part = SimpleNamespace(file="/srv/music/Artist 1/Album X/01 Local Name.flac")
        plex_track = SimpleNamespace(
            ratingKey="101",
            title="Local Name",
            grandparentTitle="Artist 1",
            parentTitle="Album X",
            duration=180000,
            media=[SimpleNamespace(parts=[part])],
        )
        admin_server = MagicMock()
        admin_server.myPlexAccount.return_value = SimpleNamespace(username="admin", id=1, email="", thumb="", users=lambda: [])
        admin_server.search.side_effect = lambda title, **kwargs: [plex_track] if title == "Local Name" else []
        admin_server.playlist.side_effect = [NotFound("missing"), MagicMock()]
        with patch("trackseerr.clients.plex.PlexServer"):
            client = PlexClient("http://localhost:32400", "token")
        client.server = admin_server
        server: MediaServer = PlexMediaServer(client)
        results = server.sync_playlist(pl, [], opts)
        searched_titles = [c.args[0] for c in admin_server.search.call_args_list]
        assert "Playlist Name" not in searched_titles
    else:
        server, fake = _make_server(
            adapter,
            [("Local Name", "Artist 1", "Album X", "/srv/music/Artist 1/Album X/01 Local Name.flac", ())],
        )
        results = server.sync_playlist(pl, ["admin"], opts)
        if adapter == "subsonic":
            queries = [dict(p).get("query") for ep, p in fake.state.calls if ep == "search3"]
        else:
            queries = [dict(p).get("SearchTerm") for _, pth, p in fake.state.calls if pth == "/Items" and dict(p).get("SearchTerm")]
        assert "Playlist Name" not in queries

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].matched_tracks == 1
    assert results[0].missing_tracks == 0


@pytest.mark.parametrize("adapter", ["subsonic", "jellyfin"])
def test_search_cache_avoids_duplicate_requests_and_clears_per_sync(adapter: str, tmp_path: Path) -> None:
    """Repeated identical queries within one sync hit cache; a new ``sync_playlist`` clears it."""
    server, fake = _make_server(
        adapter,
        [("Song A", "Artist 1", "Album X", "/music/Artist 1/Album X/01 Song A.flac", ())],
    )
    pl = Playlist(
        id="pl-cache",
        name="Cache Mix",
        tracks=[
            Track("Song A", "Artist 1", "Album X"),
            Track("Song A", "Artist 1", "Album X"),
        ],
    )
    opts = PlaylistSyncOptions(data_dir=str(tmp_path))

    def _search_count() -> int:
        if adapter == "subsonic":
            return sum(1 for ep, _ in fake.state.calls if ep == "search3")
        return sum(1 for _, pth, p in fake.state.calls if pth == "/Items" and dict(p).get("IncludeItemTypes") == "Audio")

    res1 = server.sync_playlist(pl, ["admin"], opts)
    assert res1[0].matched_tracks == 2
    first_run_searches = _search_count()
    assert first_run_searches == 1

    res2 = server.sync_playlist(pl, ["admin"], opts)
    assert res2[0].matched_tracks == 2
    assert _search_count() == first_run_searches * 2


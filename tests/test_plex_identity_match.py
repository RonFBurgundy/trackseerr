"""Tests for Plex playlist matching with ISRC and MusicBrainz identity resolution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from trackseerr.album_track_hydration import clear_negative_cache
from trackseerr.clients.plex import PlexClient
from trackseerr.models import Track
from trackseerr.playlist_identity import LibraryHit, MusicBrainzBudget
from trackseerr.storage import Database


class MockArtist:
    def __init__(self, title: str) -> None:
        self.title = title


class MockAlbum:
    def __init__(self, title: str) -> None:
        self.title = title


class MockPlexTrack:
    def __init__(
        self,
        title: str,
        artist: str,
        album: str,
        file_path: str | None = None,
    ) -> None:
        self.title = title
        self._artist = MockArtist(artist)
        self._album = MockAlbum(album)
        self.originalTitle = None
        self.grandparentTitle = None
        if file_path is not None:
            self.media = [SimpleNamespace(parts=[SimpleNamespace(file=file_path)])]
        else:
            self.media = []

    def artist(self) -> MockArtist:
        return self._artist

    def album(self) -> MockAlbum:
        return self._album


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "test_plex_identity.db"))
    clear_negative_cache()
    yield database
    database.close()


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
    db.upsert_library_artist({"id": "art-1", "name": artist})
    db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": album})
    db.upsert_library_track(
        {
            "id": track_id,
            "album_id": "alb-1",
            "artist_id": "art-1",
            "title": title,
            "track_number": 1,
            "disc_number": 1,
            "duration_seconds": 261.0,
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


@patch("trackseerr.clients.plex.PlexServer")
def test_isrc_match_beats_fuzzy(mock_server, db: Database) -> None:
    _seed_library_track(
        db,
        title="Karma Police",
        artist="Radiohead",
        album="OK Computer",
        isrc="GBAYE9701377",
        file_path="/music/Radiohead/OK Computer/06 Karma Police.flac",
    )
    client = PlexClient("http://localhost:32400", "token")
    wrong_cand = MockPlexTrack(
        "Karma Police",
        "Radiohead",
        "OK Computer",
        file_path="/plex/Radiohead/Greatest Hits/06 Karma Police.flac",
    )
    right_cand = MockPlexTrack(
        "Karma Police",
        "Radiohead",
        "OK Computer",
        file_path=r"D:\Plex\Radiohead\OK Computer\06 Karma Police.flac",
    )
    client.server.search.return_value = [wrong_cand, right_cand]

    t = Track(title="Karma Police", artist="Radiohead", album="OK Computer", isrc="GBAYE9701377")
    matched = client.match_track(t, db=db)
    assert matched is right_cand


@patch("trackseerr.clients.plex.PlexServer")
def test_isrc_hit_without_plex_item_falls_back_to_fuzzy(mock_server, db: Database) -> None:
    _seed_library_track(
        db,
        title="Easy Skanking",
        artist="Bob Marley & The Wailers",
        album="Kaya",
        isrc="USABC1234567",
        file_path="/music/Bob Marley/Kaya/01 Easy Skanking.flac",
    )
    client = PlexClient("http://localhost:32400", "token")
    fuzzy_cand = MockPlexTrack(
        "Easy Skanking",
        "Bob Marley & The Wailers",
        "Kaya",
        file_path="/plex/Other/Other.flac",
    )
    client.server.search.side_effect = [[], [fuzzy_cand]]

    t = Track(title="Easy Skanking", artist="Bob Marley & The Wailers", album="Kaya", isrc="USABC1234567")
    matched = client.match_track(t, db=db)
    assert matched is fuzzy_cand
    assert client.server.search.call_count == 2


@patch("trackseerr.clients.plex.PlexServer")
def test_musicbrainz_resolve_used_only_after_fuzzy_fails(mock_server, db: Database) -> None:
    client = PlexClient("http://localhost:32400", "token")
    fuzzy_cand = MockPlexTrack("Karma Police", "Radiohead", "OK Computer")
    client.server.search.return_value = [fuzzy_cand]
    enricher = MagicMock()
    budget = MusicBrainzBudget(5)

    matched = client.match_track(
        Track(title="Karma Police", artist="Radiohead", album="OK Computer"),
        db=db,
        enricher=enricher,
        budget=budget,
    )
    assert matched is fuzzy_cand
    enricher.lookup_track_mbids.assert_not_called()
    enricher.search_recordings.assert_not_called()
    assert budget.used == 0

    _seed_library_track(
        db,
        track_id="trk-mb",
        title="Local Title",
        artist="Artist X",
        album="Album Y",
        mb_recording_id="rec-123",
        file_path="/music/Artist X/Album Y/01 Local Title.flac",
    )
    resolved_cand = MockPlexTrack(
        "Local Title",
        "Artist X",
        "Album Y",
        file_path="/plex/Artist X/Album Y/01 Local Title.flac",
    )
    client.server.search.side_effect = [[], [resolved_cand]]
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

    matched_mb = client.match_track(
        Track(title="Playlist Title", artist="Artist X", album="Album Y", duration_seconds=180.0),
        db=db,
        enricher=enricher,
        budget=budget,
    )
    assert matched_mb is resolved_cand
    enricher.search_recordings.assert_called_once()
    assert budget.used == 1


@patch("trackseerr.playlist_identity.DEFAULT_SYNC_MB_LOOKUPS", 2)
@patch("trackseerr.clients.plex.PlexServer")
def test_budget_shared_across_playlist(mock_server, db: Database) -> None:
    client = PlexClient("http://localhost:32400", "token")
    client.server.search.return_value = []
    enricher = MagicMock()
    enricher.search_recordings.return_value = []

    tracks = [
        Track(title="Song 1", artist="Artist 1", album="Album 1"),
        Track(title="Song 2", artist="Artist 2", album="Album 2"),
        Track(title="Song 3", artist="Artist 3", album="Album 3"),
    ]
    matched, missing = client.match_playlist_tracks(tracks, db=db, enricher=enricher)
    assert matched == []
    assert len(missing) == 3
    assert enricher.search_recordings.call_count == 2


@patch("trackseerr.clients.plex.PlexServer")
def test_no_db_keeps_old_behaviour(mock_server) -> None:
    client = PlexClient("http://localhost:32400", "token")
    client.server.search.return_value = []
    enricher = MagicMock()
    budget = MusicBrainzBudget(5)

    matched = client.match_track(
        Track(title="Missing", artist="Nobody", album="Nowhere", isrc="USABC1234567"),
        enricher=enricher,
        budget=budget,
    )
    assert matched is None
    enricher.lookup_track_mbids.assert_not_called()
    enricher.search_recordings.assert_not_called()
    assert budget.used == 0


@patch("trackseerr.clients.plex.PlexServer")
def test_plex_item_for_file_finds_track_via_album(mock_server) -> None:
    client = PlexClient("http://localhost:32400", "token")
    client.server.search.return_value = [
        MockPlexTrack("Harvest", f"Other {i}", f"Other Album {i}", file_path=f"/plex/Other{i}/Album/01 Harvest.flac")
        for i in range(10)
    ]
    right_track = MockPlexTrack(
        "Harvest",
        "Neil Young",
        "Harvest",
        file_path="/plex/Neil Young/Harvest (1972)/02 - Harvest.flac",
    )
    wrong_album = MagicMock(parentTitle="Other Artist")
    wrong_album.tracks.return_value = [
        MockPlexTrack("Harvest", "Other Artist", "Harvest", file_path="/plex/Other/Harvest/01 Harvest.flac")
    ]
    right_album = MagicMock(parentTitle="Neil Young")
    right_album.tracks.return_value = [right_track]

    section = MagicMock(title="Music", type="artist")
    section.searchAlbums.return_value = [wrong_album, right_album]
    client.server.library.sections.return_value = [section]

    hit = LibraryHit(
        track_id="trk-harvest",
        title="Harvest",
        artist="Neil Young",
        album="Harvest",
        file_path="/data/media/music/Neil Young/Harvest (1972)/02 - Harvest.flac",
        via="musicbrainz",
    )
    assert client._plex_item_for_file(hit) is right_track
    section.searchAlbums.assert_called_once_with(title="Harvest", maxresults=10)
    wrong_album.tracks.assert_not_called()
    client.server.search.assert_not_called()


@patch("trackseerr.clients.plex.PlexServer")
def test_plex_item_for_file_artist_scoped_fallback(mock_server) -> None:
    client = PlexClient("http://localhost:32400", "token")
    client.server.search.return_value = []
    right_track = MockPlexTrack(
        "Harvest",
        "Neil Young",
        "Harvest",
        file_path="/plex/Neil Young/Harvest (1972)/02 - Harvest.flac",
    )
    section = MagicMock(title="Music", type="artist")
    section.searchAlbums.return_value = []
    section.searchTracks.return_value = [right_track]
    client.server.library.sections.return_value = [section]

    hit = LibraryHit(
        track_id="trk-harvest",
        title="Harvest",
        artist="Neil Young",
        album="Harvest",
        file_path="/data/media/music/Neil Young/Harvest (1972)/02 - Harvest.flac",
        via="musicbrainz",
    )
    assert client._plex_item_for_file(hit) is right_track
    section.searchTracks.assert_called_once_with(
        title="Harvest",
        filters={"artist.title": "Neil Young"},
        maxresults=25,
    )

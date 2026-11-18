"""Tests for playlist identity resolution (ISRC DB lookup and MusicBrainz fallback)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trackseerr.album_track_hydration import clear_negative_cache
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.models import Track
from trackseerr.playlist_identity import (
    LibraryHit,
    MusicBrainzBudget,
    find_by_isrc,
    resolve_with_musicbrainz,
)
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "test_playlist_identity.db"))
    clear_negative_cache()
    yield database
    database.close()


def _seed_library_track(
    db: Database,
    *,
    track_id: str = "trk-1",
    title: str = "Easy Skanking",
    artist: str = "Bob Marley & The Wailers",
    album: str = "Kaya",
    isrc: str | None = None,
    mb_recording_id: str | None = None,
    file_path: str | None = "/music/Bob Marley/Kaya/01 Easy Skanking.flac",
    duration_seconds: float = 178.0,
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
            "duration_seconds": duration_seconds,
            "isrc": isrc,
            "mb_recording_id": mb_recording_id,
        }
    )
    if file_path is not None:
        db.upsert_library_file(
            {
                "id": f"file-{track_id}",
                "track_id": track_id,
                "file_path": file_path,
                "relative_path": "01 Easy Skanking.flac",
                "codec": "flac",
                "quality_name": "FLAC",
                "size_bytes": 123456,
            }
        )


def test_find_by_isrc_returns_file(db: Database):
    _seed_library_track(db, isrc="USABC1234567")

    hit = find_by_isrc(db, "USABC1234567")
    assert hit == LibraryHit(
        track_id="trk-1",
        title="Easy Skanking",
        artist="Bob Marley & The Wailers",
        album="Kaya",
        file_path="/music/Bob Marley/Kaya/01 Easy Skanking.flac",
        via="isrc",
    )

    lower_hit = find_by_isrc(db, "usabc1234567")
    assert lower_hit == hit


def test_find_by_isrc_ignores_tracks_without_file(db: Database):
    _seed_library_track(db, isrc="USABC1234567", file_path=None)

    assert find_by_isrc(db, "USABC1234567") is None


def test_resolve_isrc_through_musicbrainz(db: Database):
    _seed_library_track(db, isrc=None, mb_recording_id="rec-mb-1")
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.lookup_track_mbids.return_value = {
        "musicbrainz_artistid": "art-mb-1",
        "musicbrainz_albumid": "rel-mb-1",
        "musicbrainz_releasegroupid": "rg-mb-1",
        "musicbrainz_trackid": "rec-mb-1",
    }

    track = Track(
        title="Easy Skanking",
        artist="Bob Marley & The Wailers",
        album="Kaya",
        duration_seconds=178.0,
        isrc="USABC1234567",
    )
    hit = resolve_with_musicbrainz(db, enricher, track, MusicBrainzBudget(5))

    assert hit is not None
    assert hit.track_id == "trk-1"
    assert hit.file_path == "/music/Bob Marley/Kaya/01 Easy Skanking.flac"
    assert hit.via == "isrc_musicbrainz"
    enricher.lookup_track_mbids.assert_called_once_with(
        "Bob Marley & The Wailers", "Kaya", "Easy Skanking", isrc="USABC1234567"
    )
    enricher.search_recordings.assert_not_called()


def test_resolve_by_search_requires_safe_pick(db: Database):
    _seed_library_track(db, isrc=None, mb_recording_id="rec-search-1", duration_seconds=200.0)
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.search_recordings.return_value = [
        {
            "id": "rec-search-1",
            "title": "Easy Skanking",
            "score": 98,
            "length_ms": 201000,
            "artist_credit": "Bob Marley & The Wailers",
            "artist_names": ["Bob Marley & The Wailers"],
            "artist_ids": ["art-1"],
            "release_groups": [{"id": "rg-1", "primary_type": "Album", "secondary_types": []}],
        }
    ]

    track = Track(
        title="Easy Skanking",
        artist="Bob Marley & The Wailers",
        album="Kaya",
        duration_seconds=200.0,
    )
    hit = resolve_with_musicbrainz(db, enricher, track, MusicBrainzBudget(5))
    assert hit is not None
    assert hit.via == "musicbrainz_search"
    assert hit.track_id == "trk-1"

    # Candidate with duration gap over the limit should be rejected -> None
    enricher.search_recordings.return_value = [
        {
            "id": "rec-search-1",
            "title": "Easy Skanking",
            "score": 98,
            "length_ms": 260000,
            "artist_credit": "Bob Marley & The Wailers",
            "artist_names": ["Bob Marley & The Wailers"],
            "artist_ids": ["art-1"],
            "release_groups": [{"id": "rg-1", "primary_type": "Album", "secondary_types": []}],
        }
    ]
    assert resolve_with_musicbrainz(db, enricher, track, MusicBrainzBudget(5)) is None


def test_resolve_stops_when_budget_spent(db: Database):
    _seed_library_track(db, isrc=None, mb_recording_id="rec-mb-1")
    enricher = MagicMock(spec=MbidEnricherClient)
    track = Track(
        title="Easy Skanking",
        artist="Bob Marley & The Wailers",
        album="Kaya",
        isrc="USABC1234567",
    )

    hit = resolve_with_musicbrainz(db, enricher, track, MusicBrainzBudget(0))
    assert hit is None
    enricher.lookup_track_mbids.assert_not_called()
    enricher.search_recordings.assert_not_called()


def test_resolve_unavailable_returns_none(db: Database):
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.search_recordings.return_value = None
    track = Track(
        title="Easy Skanking",
        artist="Bob Marley & The Wailers",
        album="Kaya",
    )

    assert resolve_with_musicbrainz(db, enricher, track, MusicBrainzBudget(5)) is None

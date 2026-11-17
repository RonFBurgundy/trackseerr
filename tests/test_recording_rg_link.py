"""Tests for linking albums to MusicBrainz release groups through their tracks' recordings."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from trackseerr.clients.mbid_enricher import VARIOUS_ARTISTS_MBID, MbidEnricherClient
from trackseerr.models import LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack
from trackseerr.release_matching import link_albums_by_recording
from trackseerr.storage import Database


def _rg(rg_id: str, title: str, primary: str = "Album", secondary: Optional[list[str]] = None, date: str = "1976") -> dict[str, Any]:
    return {
        "id": rg_id,
        "title": title,
        "primary_type": primary,
        "secondary_types": secondary or [],
        "first_release_date": date,
    }


class FakeEnricher:
    def __init__(self, by_recording: dict[str, list[dict[str, Any]]]) -> None:
        self.by_recording = by_recording
        self.calls: list[str] = []

    def get_recording_release_groups(self, recording_id: str) -> list[dict[str, Any]]:
        self.calls.append(recording_id)
        return self.by_recording.get(recording_id, [])


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "link.db"))
    database.upsert_library_artist(LibraryArtist(id="ar1", name="Eagles", monitored=False))
    yield database
    database.close()


def _album(db: Database, tmp_path: Path, alb: str, title: str, rec: Optional[str], rg: Optional[str] = None) -> None:
    db.upsert_library_album(LibraryAlbum(id=alb, artist_id="ar1", title=title, mb_release_group_id=rg))
    tid = f"{alb}-t1"
    db.upsert_library_track(
        LibraryTrack(id=tid, album_id=alb, artist_id="ar1", title="Song", track_number=1, mb_recording_id=rec)
    )
    f = tmp_path / f"{tid}.flac"
    f.write_bytes(b"x")
    db.upsert_library_file(LibraryFile(id=f"file-{tid}", track_id=tid, file_path=str(f), relative_path=f.name, codec="FLAC"))


def _rg_of(db: Database, alb: str) -> Optional[str]:
    return db.get_library_album(alb).get("mb_release_group_id") or None


def test_links_single_track_remaster_album(db: Database, tmp_path: Path) -> None:
    _album(db, tmp_path, "a1", "Hotel California - 2013 Remaster", "rec1")
    enricher = FakeEnricher(
        {"rec1": [_rg("rg-best", "The Very Best Of", "Album", ["Compilation"]), _rg("rg-hc", "Hotel California")]}
    )
    counts = link_albums_by_recording(db, enricher)
    assert counts["linked"] == 1
    assert counts["albums"] == 1
    assert _rg_of(db, "a1") == "rg-hc"


def test_prefers_album_over_compilation_with_same_title(db: Database, tmp_path: Path) -> None:
    _album(db, tmp_path, "a1", "Greatest Hits", "rec1")
    enricher = FakeEnricher(
        {
            "rec1": [
                _rg("rg-comp", "Greatest Hits", "Album", ["Compilation"], "1970"),
                _rg("rg-album", "Greatest Hits", "Album", [], "1980"),
            ]
        }
    )
    link_albums_by_recording(db, enricher)
    assert _rg_of(db, "a1") == "rg-album"


def test_no_link_without_title_match(db: Database, tmp_path: Path) -> None:
    # Albums of 3 tracks or fewer are filed under the recording's home release instead, so use 4+ tracks.
    _album(db, tmp_path, "a1", "Hotel California", "rec1")
    for n in range(2, 5):
        db.upsert_library_track(
            LibraryTrack(id=f"a1-t{n}", album_id="a1", artist_id="ar1", title=f"Song {n}", track_number=n)
        )
    counts = link_albums_by_recording(db, FakeEnricher({"rec1": [_rg("rg-x", "Desperado")]}))
    assert counts["no_title_match"] == 1
    assert counts["linked"] == 0
    assert _rg_of(db, "a1") is None


def _held_and_four_track_album(db: Database, tmp_path: Path, title: str) -> None:
    _album(db, tmp_path, "held", "Hotel California", "recA", rg="rg-hc")
    _album(db, tmp_path, "a2", title, "recB")
    for n in range(2, 5):
        db.upsert_library_track(
            LibraryTrack(id=f"a2-t{n}", album_id="a2", artist_id="ar1", title=f"Song {n}", track_number=n)
        )


def test_large_album_with_matching_title_folds_into_holder(db: Database, tmp_path: Path) -> None:
    _held_and_four_track_album(db, tmp_path, "Hotel California (Hi-Res Version)")
    counts = link_albums_by_recording(db, FakeEnricher({"recB": [_rg("rg-hc", "Hotel California")]}))
    assert counts["rg_taken"] == 0
    assert counts["linked"] == 0
    # Both albums have a file for track 1, so the fold reports a conflict and leaves both albums untouched.
    assert counts["merged"] == 0
    assert counts["merge_conflict"] == 1
    assert _rg_of(db, "a2") is None
    assert _rg_of(db, "held") == "rg-hc"
    assert db.get_library_album("a2") is not None


def test_skips_rg_already_held_by_same_artist_with_other_title(db: Database, tmp_path: Path) -> None:
    _held_and_four_track_album(db, tmp_path, "Hotel California Sessions")
    counts = link_albums_by_recording(db, FakeEnricher({"recB": [_rg("rg-hc", "Hotel California Sessions")]}))
    assert counts["rg_taken"] == 1
    assert counts["linked"] == 0
    assert counts["merged"] == 0
    assert counts["merge_conflict"] == 0
    assert _rg_of(db, "a2") is None
    assert db.get_library_album("a2") is not None


def test_album_without_recording_ids_is_counted_not_linked(db: Database, tmp_path: Path) -> None:
    _album(db, tmp_path, "a1", "Hotel California", None)
    enricher = FakeEnricher({})
    counts = link_albums_by_recording(db, enricher)
    assert counts["no_recordings"] == 1
    assert counts["linked"] == 0
    assert enricher.calls == []


def test_get_recording_release_groups_parses_and_dedupes() -> None:
    client = MbidEnricherClient(base_url="https://api.brainzmash.cc")
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "releases": [
            {"id": "r1", "release-group": {"id": "rg1", "title": "Hotel California", "primary-type": "Album",
                                           "secondary-types": [], "first-release-date": "1976-12-08"},
             "artist-credit": [{"artist": {"id": "ar-eagles"}}]},
            {"id": "r2", "release-group": {"id": "rg1", "title": "Hotel California", "primary-type": "Album"}},
            {"id": "r3", "release-group": {"id": "rg2", "title": "Best Of", "primary-type": "Album",
                                           "secondary-types": ["Compilation"]},
             "artist-credit": [{"artist": {"id": VARIOUS_ARTISTS_MBID}}]},
            {"id": "r4"},
        ]
    }
    client._request = MagicMock(return_value=resp)  # type: ignore[method-assign]
    groups = client.get_recording_release_groups("rec-1")
    assert [g["id"] for g in groups] == ["rg1", "rg2"]
    assert groups[0] == {
        "id": "rg1",
        "title": "Hotel California",
        "primary_type": "Album",
        "secondary_types": [],
        "first_release_date": "1976-12-08",
        "artist_ids": ["ar-eagles"],
        "is_various": False,
    }
    assert groups[1]["secondary_types"] == ["Compilation"]
    assert groups[1]["artist_ids"] == [VARIOUS_ARTISTS_MBID] and groups[1]["is_various"] is True
    params = client._request.call_args.kwargs["params"]
    assert params["recording"] == "rec-1" and params["inc"] == "release-groups+artist-credits" and params["limit"] == 100
    client.get_recording_release_groups("rec-1")
    assert client._request.call_count == 1

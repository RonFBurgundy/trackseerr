"""Tests for Deezer fallback when filing compilation-only songs out of Various Artists."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trackseerr.album_track_hydration import clear_negative_cache
from trackseerr.clients.discovery import DiscoveryClient
from trackseerr.clients.mbid_enricher import VARIOUS_ARTISTS_MBID, MbidEnricherClient
from trackseerr.compilation_filing import refile_out_of_various, various_artists_id
from trackseerr.models import LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack
from trackseerr.storage import Database

BOB_MBID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "test_deezer_home.db"))
    clear_negative_cache()
    yield database
    database.close()


def _group(
    gid: str,
    title: str,
    primary: str = "Album",
    secondary: list[str] | None = None,
    date: str = "2000-01-01",
    artist_ids: list[str] | None = None,
    various: bool = False,
) -> dict[str, Any]:
    return {
        "id": gid,
        "title": title,
        "primary_type": primary,
        "secondary_types": secondary or [],
        "first_release_date": date,
        "artist_ids": artist_ids if artist_ids is not None else ([VARIOUS_ARTISTS_MBID] if various else [BOB_MBID]),
        "is_various": various,
    }


NOW_44 = _group(
    "rg-now44",
    "Now That's What I Call Music! 44",
    secondary=["Compilation"],
    date="1999-11-01",
    various=True,
)


def _enricher(
    groups_by_rec: dict[str, list[dict[str, Any]]],
    search_results: dict[tuple[str, str], list[dict[str, Any]] | None] | None = None,
) -> MagicMock:
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_recording_release_groups.side_effect = lambda rec: groups_by_rec.get(rec, [])
    enricher.get_release_group_tracks.side_effect = lambda rg, force=False: []
    enricher.search_recordings.side_effect = lambda artist, title, limit=10: (search_results or {}).get(
        (artist, title), []
    )
    return enricher


def _seed_album(
    db: Database,
    tmp_path: Path,
    album_id: str,
    title: str,
    tracks: list[dict[str, Any]],
    rg_id: str | None = None,
    artist_id: str = "artist-bob",
    artist_name: str = "Bob Marley",
    artist_mbid: str | None = BOB_MBID,
    monitored: bool = False,
) -> None:
    if db.get_library_artist(artist_id) is None:
        db.upsert_library_artist(LibraryArtist(id=artist_id, name=artist_name, mbid=artist_mbid, monitored=False))
    db.upsert_library_album(
        LibraryAlbum(
            id=album_id,
            artist_id=artist_id,
            title=title,
            mb_release_group_id=rg_id,
            monitored=monitored,
            total_tracks=len(tracks),
        )
    )
    for t in tracks:
        tid = f"{album_id}-{t['num']}"
        db.upsert_library_track(
            LibraryTrack(
                id=tid,
                album_id=album_id,
                artist_id=artist_id,
                title=t["title"],
                track_number=t["num"],
                mb_recording_id=t.get("rec_id"),
                duration_seconds=t.get("duration_seconds"),
                monitored=False,
            )
        )
        if t.get("file", True):
            path = tmp_path / f"{tid}.flac"
            path.write_bytes(b"dummy audio")
            db.upsert_library_file(
                LibraryFile(id=f"file-{tid}", track_id=tid, file_path=str(path), relative_path=path.name, codec="FLAC")
            )


def _seed_va_song(
    db: Database,
    tmp_path: Path,
    *,
    discovery_id: str | None = "deezer:artist:1",
    duration_seconds: float | None = 210.0,
) -> None:
    va_id = various_artists_id(db)
    _seed_album(
        db,
        tmp_path,
        "alb-va",
        "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only", "duration_seconds": duration_seconds}],
        rg_id="rg-now44",
        artist_id=va_id,
        artist_name="Various Artists",
        artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.upsert_library_artist(
        LibraryArtist(
            id="artist-bob", name="Bob Marley", mbid=BOB_MBID, foreign_artist_id=discovery_id, monitored=True
        )
    )
    db.replace_track_artists(
        "alb-va-1",
        [{"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}],
    )


def _discovery(
    *,
    albums: list[dict[str, Any]] | None = None,
    singles_eps: list[dict[str, Any]] | None = None,
    compilations: list[dict[str, Any]] | None = None,
    album_details: dict[str, dict[str, Any]] | None = None,
    search_artist_result: dict[str, Any] | None = None,
) -> MagicMock:
    client = MagicMock(spec=DiscoveryClient)
    client.search_artist.return_value = search_artist_result
    client.get_artist_details.return_value = {
        "id": "deezer:artist:1",
        "name": "Bob Marley",
        "albums": albums or [],
        "singles_eps": singles_eps or [],
        "compilations": compilations or [],
    }
    details_map = album_details or {}
    client.get_album_details.side_effect = lambda aid, force=False: details_map.get(aid)
    return client


def test_deezer_home_files_song_under_single(db: Database, tmp_path: Path):
    _seed_va_song(db, tmp_path)
    enricher = _enricher({"rec-va-only": [NOW_44]}, search_results={("Bob Marley", "Could I Be In Love"): []})
    discovery = _discovery(
        singles_eps=[{"id": "deezer:album:10", "title": "Could I Be In Love", "record_type": "single", "cover_url": None}],
        album_details={
            "deezer:album:10": {
                "id": "deezer:album:10",
                "title": "Could I Be In Love",
                "release_date": "1999-05-01",
                "tracks": [{"title": "Could I Be In Love", "duration_seconds": 210}],
            }
        },
    )

    counts = refile_out_of_various(db, enricher, discovery=discovery)

    assert counts["deezer_home"] == 1
    assert counts["filed_home"] == 1
    moved = db.get_library_track("alb-va-1")
    assert moved is not None
    assert moved["mb_recording_id"] == "rec-va-only"
    new_album = db.get_library_album_by_foreign_id("deezer:album:10")
    assert new_album is not None
    assert new_album["artist_id"] == "artist-bob"
    assert not new_album["monitored"]
    assert new_album["album_type"] == "single"
    assert moved["album_id"] == new_album["id"]


def test_deezer_home_skips_compilations_and_current_album(db: Database, tmp_path: Path):
    _seed_va_song(db, tmp_path)
    enricher = _enricher({"rec-va-only": [NOW_44]}, search_results={("Bob Marley", "Could I Be In Love"): []})

    comp_only = _discovery(
        compilations=[{"id": "deezer:album:20", "title": "Greatest Hits", "record_type": "compile", "cover_url": None}],
        album_details={
            "deezer:album:20": {
                "id": "deezer:album:20",
                "title": "Greatest Hits",
                "release_date": "2005-01-01",
                "tracks": [{"title": "Could I Be In Love", "duration_seconds": 210}],
            }
        },
    )
    counts = refile_out_of_various(db, enricher, discovery=comp_only)
    assert counts["deezer_home"] == 0
    assert counts["left_search_no_results"] == 1

    same_title = _discovery(
        albums=[
            {
                "id": "deezer:album:21",
                "title": "Now That's What I Call Music! 44",
                "record_type": "album",
                "cover_url": None,
            }
        ],
        album_details={
            "deezer:album:21": {
                "id": "deezer:album:21",
                "title": "Now That's What I Call Music! 44",
                "release_date": "1999-11-01",
                "tracks": [{"title": "Could I Be In Love", "duration_seconds": 210}],
            }
        },
    )
    counts_same = refile_out_of_various(db, enricher, discovery=same_title)
    assert counts_same["deezer_home"] == 0
    assert counts_same["left_search_no_results"] == 1


def test_deezer_home_duration_and_version_guard(db: Database, tmp_path: Path):
    _seed_va_song(db, tmp_path, duration_seconds=210.0)
    enricher = _enricher({"rec-va-only": [NOW_44]}, search_results={("Bob Marley", "Could I Be In Love"): []})

    live_only = _discovery(
        singles_eps=[{"id": "deezer:album:30", "title": "Could I Be In Love", "record_type": "single", "cover_url": None}],
        album_details={
            "deezer:album:30": {
                "id": "deezer:album:30",
                "title": "Could I Be In Love",
                "release_date": "2001-01-01",
                "tracks": [{"title": "Could I Be In Love (Live)", "duration_seconds": 210}],
            }
        },
    )
    assert refile_out_of_various(db, enricher, discovery=live_only)["deezer_home"] == 0

    bad_duration = _discovery(
        singles_eps=[{"id": "deezer:album:31", "title": "Could I Be In Love", "record_type": "single", "cover_url": None}],
        album_details={
            "deezer:album:31": {
                "id": "deezer:album:31",
                "title": "Could I Be In Love",
                "release_date": "2001-01-01",
                "tracks": [{"title": "Could I Be In Love", "duration_seconds": 250}],
            }
        },
    )
    assert refile_out_of_various(db, enricher, discovery=bad_duration)["deezer_home"] == 0


def test_deezer_home_searches_artist_by_exact_name(db: Database, tmp_path: Path):
    _seed_va_song(db, tmp_path, discovery_id=None)
    enricher = _enricher({"rec-va-only": [NOW_44]}, search_results={("Bob Marley", "Could I Be In Love"): []})

    tribute = _discovery(
        search_artist_result={"id": "deezer:artist:99", "name": "Bob Marley Tribute Band"},
        singles_eps=[{"id": "deezer:album:40", "title": "Could I Be In Love", "record_type": "single", "cover_url": None}],
        album_details={
            "deezer:album:40": {
                "id": "deezer:album:40",
                "title": "Could I Be In Love",
                "release_date": "2001-01-01",
                "tracks": [{"title": "Could I Be In Love", "duration_seconds": 210}],
            }
        },
    )
    assert refile_out_of_various(db, enricher, discovery=tribute)["deezer_home"] == 0
    tribute.get_artist_details.assert_not_called()

    exact = _discovery(
        search_artist_result={"id": "deezer:artist:1", "name": "Bob Marley"},
        singles_eps=[{"id": "deezer:album:41", "title": "Could I Be In Love", "record_type": "single", "cover_url": None}],
        album_details={
            "deezer:album:41": {
                "id": "deezer:album:41",
                "title": "Could I Be In Love",
                "release_date": "2001-01-01",
                "tracks": [{"title": "Could I Be In Love", "duration_seconds": 210}],
            }
        },
    )
    assert refile_out_of_various(db, enricher, discovery=exact)["deezer_home"] == 1
    exact.get_artist_details.assert_called_once_with("deezer:artist:1")


def test_deezer_home_reuses_existing_album(db: Database, tmp_path: Path):
    _seed_va_song(db, tmp_path)
    existing = db.upsert_library_album(
        LibraryAlbum(
            id="alb-existing-dz",
            artist_id="artist-bob",
            title="Could I Be In Love",
            foreign_album_id="deezer:album:10",
            album_type="single",
            monitored=False,
        )
    )
    enricher = _enricher({"rec-va-only": [NOW_44]}, search_results={("Bob Marley", "Could I Be In Love"): []})
    discovery = _discovery(
        singles_eps=[{"id": "deezer:album:10", "title": "Could I Be In Love", "record_type": "single", "cover_url": None}],
        album_details={
            "deezer:album:10": {
                "id": "deezer:album:10",
                "title": "Could I Be In Love",
                "release_date": "1999-05-01",
                "tracks": [{"title": "Could I Be In Love", "duration_seconds": 210}],
            }
        },
    )

    counts = refile_out_of_various(db, enricher, discovery=discovery)

    assert counts["deezer_home"] == 1
    moved = db.get_library_track("alb-va-1")
    assert moved is not None
    assert moved["album_id"] == existing["id"]
    assert len(db.list_library_albums(artist_id="artist-bob")) == 1


def test_deezer_home_caps_album_lookups(db: Database, tmp_path: Path):
    _seed_va_song(db, tmp_path)
    enricher = _enricher({"rec-va-only": [NOW_44]}, search_results={("Bob Marley", "Could I Be In Love"): []})
    releases = [
        {"id": f"deezer:album:{i}", "title": f"Unrelated Album {i}", "record_type": "album", "cover_url": None}
        for i in range(12)
    ]
    details = {
        f"deezer:album:{i}": {
            "id": f"deezer:album:{i}",
            "title": f"Unrelated Album {i}",
            "release_date": "2000-01-01",
            "tracks": [{"title": f"Other Track {i}", "duration_seconds": 200}],
        }
        for i in range(12)
    }
    discovery = _discovery(albums=releases, album_details=details)

    counts = refile_out_of_various(db, enricher, discovery=discovery)

    assert counts["deezer_home"] == 0
    assert discovery.get_album_details.call_count == 8


def test_deezer_home_after_search_before_own_compilation(db: Database, tmp_path: Path):
    _seed_va_song(db, tmp_path)
    best_of = _group("rg-best", "Legend", secondary=["Compilation"], artist_ids=[BOB_MBID])
    enricher = _enricher(
        {"rec-va-only": [NOW_44, best_of]},
        search_results={("Bob Marley", "Could I Be In Love"): []},
    )
    discovery = _discovery(
        singles_eps=[{"id": "deezer:album:10", "title": "Could I Be In Love", "record_type": "single", "cover_url": None}],
        album_details={
            "deezer:album:10": {
                "id": "deezer:album:10",
                "title": "Could I Be In Love",
                "release_date": "1999-05-01",
                "tracks": [{"title": "Could I Be In Love", "duration_seconds": 210}],
            }
        },
    )

    counts = refile_out_of_various(db, enricher, discovery=discovery)

    assert counts["deezer_home"] == 1
    assert db.get_library_album_by_foreign_id("deezer:album:10") is not None
    assert db.album_holding_release_group("artist-bob", "rg-best", "") is None


def test_out_of_various_without_discovery_unchanged(db: Database, tmp_path: Path):
    _seed_va_song(db, tmp_path)
    best_of = _group("rg-best", "Legend", secondary=["Compilation"], artist_ids=[BOB_MBID])
    enricher = _enricher(
        {"rec-va-only": [NOW_44, best_of]},
        search_results={("Bob Marley", "Could I Be In Love"): []},
    )

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 1
    assert counts["deezer_home"] == 0
    assert db.album_holding_release_group("artist-bob", "rg-best", "") is not None

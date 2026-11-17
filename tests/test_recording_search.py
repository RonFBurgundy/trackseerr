"""Tests for MusicBrainz recording text search (enricher + matcher + Self-Heal fill step)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest
import requests

from trackseerr.clients.mbid_enricher import VARIOUS_ARTISTS_MBID, MbidEnricherClient
from trackseerr.models import LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack
from trackseerr.recording_search import (
    fill_recordings_from_search,
    pick_search_recording,
    search_title,
)
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "test_rec_search.db"))
    yield database
    database.close()


def _cand(
    rec_id: str = "rec-1",
    title: str = "Easy Skanking",
    artist: str = "Bob Marley & The Wailers",
    score: int = 100,
    length_ms: Optional[int] = 200000,
    release_groups: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    return {
        "id": rec_id,
        "title": title,
        "score": score,
        "length_ms": length_ms,
        "artist_credit": artist,
        "artist_names": [artist],
        "artist_ids": ["art-1"],
        "release_groups": release_groups
        if release_groups is not None
        else [{"id": "rg-1", "title": "Kaya", "primary_type": "Album", "secondary_types": [], "is_various": False}],
    }


# ---------------------------------------------------------------- search_title


def test_search_title_cleans_real_examples():
    assert (
        search_title('I Don\'t Want to Miss a Thing - From the Touchstone film, "Armageddon"', "Aerosmith")
        == "I Don't Want to Miss a Thing"
    )
    assert search_title("Easy Skanking", "Bob Marley & The Wailers") == "Easy Skanking"
    assert search_title("Singles - 01 - No Diggity ft. Dr. Dre, Queen Pen", "Blackstreet") == "No Diggity"
    assert search_title("Sun Is Shining (Radio De Luxe edit)") == "Sun Is Shining (Radio De Luxe edit)"
    assert search_title("Daft Punk - One More Time", "Daft Punk") == "One More Time"
    assert search_title("1-03 Song (feat. Someone)") == "Song"
    assert search_title("7 Rings") == "7 Rings"


# ---------------------------------------------------------------- pick


def test_pick_rejects_low_score():
    assert pick_search_recording([_cand(score=89)], "Easy Skanking", "Bob Marley & The Wailers", None, set()) is None


def test_pick_rejects_other_artist():
    cands = [_cand(artist="Some Cover Band")]
    assert pick_search_recording(cands, "Easy Skanking", "Bob Marley & The Wailers", None, set()) is None


def test_pick_rejects_version_mismatch():
    cands = [_cand(title="Easy Skanking (live)")]
    assert pick_search_recording(cands, "Easy Skanking", "Bob Marley & The Wailers", None, set()) is None


def test_pick_rejects_duration_gap():
    cands = [_cand(length_ms=200000)]
    assert pick_search_recording(cands, "Easy Skanking", "Bob Marley & The Wailers", 215.0, set()) is None


def test_pick_closest_duration_wins():
    cands = [_cand("rec-far", length_ms=204000), _cand("rec-near", length_ms=200500)]
    chosen = pick_search_recording(cands, "Easy Skanking", "Bob Marley & The Wailers", 200.0, set())
    assert chosen is not None and chosen["id"] == "rec-near"


def test_pick_without_duration_prefers_album_or_single():
    various = [{"id": "rg-v", "title": "Hits", "primary_type": "Album", "secondary_types": [], "is_various": True}]
    cands = [
        _cand("rec-va", score=100, release_groups=various),
        _cand("rec-album", score=95),
    ]
    chosen = pick_search_recording(cands, "Easy Skanking", "Bob Marley & The Wailers", None, set())
    assert chosen is not None and chosen["id"] == "rec-album"


def test_pick_skips_used_recording():
    cands = [_cand("rec-used"), _cand("rec-free")]
    chosen = pick_search_recording(cands, "Easy Skanking", "Bob Marley & The Wailers", None, {"rec-used"})
    assert chosen is not None and chosen["id"] == "rec-free"
    assert pick_search_recording(cands[:1], "Easy Skanking", "Bob Marley & The Wailers", None, {"rec-used"}) is None


# ---------------------------------------------------------------- enricher


def _resp(status: int, body: Any) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    return resp


def test_search_recordings_parses_and_caches():
    enricher = MbidEnricherClient(base_url="https://api.brainzmash.cc")
    body = {
        "recordings": [
            {
                "id": "rec-1",
                "title": "Easy Skanking",
                "score": 100,
                "length": 200000,
                "artist-credit": [
                    {"name": "Bob Marley", "joinphrase": " & ", "artist": {"id": "a1", "name": "Bob Marley"}},
                    {"name": "The Wailers", "artist": {"id": "a2", "name": "The Wailers"}},
                ],
                "releases": [
                    {"id": "r1", "release-group": {"id": "rg-1", "title": "Kaya", "primary-type": "Album"}},
                    {
                        "id": "r2",
                        "artist-credit": [{"name": "Various Artists", "artist": {"id": VARIOUS_ARTISTS_MBID}}],
                        "release-group": {
                            "id": "rg-2",
                            "title": "Reggae Hits",
                            "primary-type": "Album",
                            "secondary-types": ["Compilation"],
                        },
                    },
                    {"id": "r3", "release-group": {"id": "rg-1", "title": "Kaya", "primary-type": "Album"}},
                ],
            }
        ]
    }
    enricher._request = MagicMock(return_value=_resp(200, body))

    first = enricher.search_recordings("Bob Marley", 'Easy "Skanking"')
    second = enricher.search_recordings("Bob Marley", 'Easy "Skanking"')

    assert enricher._request.call_count == 1
    assert first == second and first is not None and len(first) == 1
    rec = first[0]
    assert rec["id"] == "rec-1" and rec["score"] == 100 and rec["length_ms"] == 200000
    assert rec["artist_credit"] == "Bob Marley & The Wailers"
    assert rec["artist_names"] == ["Bob Marley", "The Wailers"] and rec["artist_ids"] == ["a1", "a2"]
    groups = {g["id"]: g for g in rec["release_groups"]}
    assert set(groups) == {"rg-1", "rg-2"}
    assert groups["rg-1"]["is_various"] is False and groups["rg-2"]["is_various"] is True
    params = enricher._request.call_args[1]["params"]
    assert params["query"] == 'recording:"Easy  Skanking" AND artist:"Bob Marley"'
    assert params["fmt"] == "json" and params["limit"] == 10


def test_search_recordings_transient_failure_returns_none_uncached():
    enricher = MbidEnricherClient(base_url="https://api.brainzmash.cc")
    enricher._request = MagicMock(
        side_effect=[requests.ConnectionError("boom"), _resp(503, {}), _resp(200, {"recordings": []})]
    )

    assert enricher.search_recordings("A", "T") is None
    enricher._cache.clear()  # only the in-process memo held the failure; nothing was persisted
    assert enricher.search_recordings("A", "T") is None
    enricher._cache.clear()
    assert enricher.search_recordings("A", "T") == []
    assert enricher._request.call_count == 3
    # The empty answer is cached.
    assert enricher.search_recordings("A", "T") == []
    assert enricher._request.call_count == 3


def test_recording_release_groups_flags_various_artists():
    enricher = MbidEnricherClient(base_url="https://api.brainzmash.cc")
    body = {
        "releases": [
            {
                "id": "r1",
                "artist-credit": [{"artist": {"id": "a1"}}],
                "release-group": {"id": "rg-1", "title": "Album", "primary-type": "Album"},
            },
            {
                "id": "r2",
                "artist-credit": [{"artist": {"id": VARIOUS_ARTISTS_MBID}}],
                "release-group": {"id": "rg-2", "title": "Comp", "primary-type": "Album"},
            },
        ]
    }
    enricher._request = MagicMock(return_value=_resp(200, body))

    groups = {g["id"]: g for g in enricher.get_recording_release_groups("rec-x")}

    assert groups["rg-1"]["is_various"] is False and groups["rg-1"]["artist_ids"] == ["a1"]
    assert groups["rg-2"]["is_various"] is True
    assert enricher._request.call_args[1]["params"]["inc"] == "release-groups+artist-credits"


# ---------------------------------------------------------------- fill


def _seed(
    db: Database,
    tmp_path: Path,
    titles: list[str],
    album_artist: str = "Bob Marley & The Wailers",
) -> list[str]:
    db.upsert_library_artist(LibraryArtist(id="artist-1", name=album_artist, monitored=True))
    db.upsert_library_album(
        LibraryAlbum(id="alb-1", artist_id="artist-1", title="Album", total_tracks=len(titles))
    )
    ids = []
    for i, title in enumerate(titles, start=1):
        tid = f"alb-1-{i}"
        ids.append(tid)
        db.upsert_library_track(
            LibraryTrack(
                id=tid,
                album_id="alb-1",
                artist_id="artist-1",
                title=title,
                track_number=i,
                disc_number=1,
                duration_seconds=200.0,
            )
        )
        path = tmp_path / f"{tid}.flac"
        path.write_bytes(b"dummy")
        db.upsert_library_file(
            LibraryFile(id=f"file-{tid}", track_id=tid, file_path=str(path), relative_path=path.name, codec="FLAC")
        )
    return ids


def _track_rec(db: Database, track_id: str) -> str:
    return str(next(t for t in db.list_library_tracks(album_id="alb-1", limit=100) if t["id"] == track_id).get("mb_recording_id") or "")


def test_fill_from_search_fills_and_counts(db: Database, tmp_path: Path):
    ids = _seed(db, tmp_path, ["Easy Skanking", "Unknown Song"])
    enricher = MagicMock()
    enricher.search_recordings.side_effect = lambda artist, title: (
        [_cand("rec-easy")] if title == "Easy Skanking" else []
    )

    counts = fill_recordings_from_search(db, enricher)

    assert counts == {"tracks": 2, "filled": 1, "no_match": 1, "unavailable": 0, "errors": 0}
    assert _track_rec(db, ids[0]) == "rec-easy"
    assert _track_rec(db, ids[1]) == ""


def test_fill_from_search_stops_after_three_unavailable(db: Database, tmp_path: Path):
    _seed(db, tmp_path, ["One", "Two", "Three", "Four", "Five"])
    enricher = MagicMock()
    enricher.search_recordings.return_value = None

    counts = fill_recordings_from_search(db, enricher)

    assert counts["unavailable"] == 3 and counts["filled"] == 0
    assert enricher.search_recordings.call_count == 3


def test_fill_from_search_uses_primary_credit(db: Database, tmp_path: Path):
    ids = _seed(db, tmp_path, ["Easy Skanking"], album_artist="Various Artists")
    db.replace_track_artists(ids[0], [{"artist_name": "Bob Marley & The Wailers", "role": "primary"}])
    enricher = MagicMock()
    enricher.search_recordings.return_value = [_cand("rec-easy")]

    counts = fill_recordings_from_search(db, enricher)

    searched_artist = enricher.search_recordings.call_args[0][0]
    assert searched_artist == "Bob Marley & The Wailers"
    assert counts["filled"] == 1

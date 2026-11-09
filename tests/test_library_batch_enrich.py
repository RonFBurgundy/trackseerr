"""Tests for batched library enrichment (_enrich_tracks, _enrich_albums, _enrich_artists)."""

import copy
from typing import Any

import pytest

from trackseerr.api.routes.library._shared import (
    _chunked,
    _enrich_albums,
    _enrich_artists,
    _enrich_tracks,
)
from trackseerr.storage import Database
from trackseerr.track_matching import match_owned_equivalent


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


def _reference_enrich_tracks(db: Database, tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reference implementation matching the pre-batching per-row behavior."""
    tracks_copy = [dict(t) for t in tracks]
    artist_cache: dict[str, str] = {}
    album_cache: dict[str, str] = {}
    filed_tracks_cache: dict[str, list[dict[str, Any]]] = {}
    for t in tracks_copy:
        art_id = t.get("artist_id")
        if art_id:
            if art_id not in artist_cache:
                art = db.get_library_artist(art_id)
                artist_cache[art_id] = art["name"] if art else "Unknown Artist"
            t["artist_name"] = artist_cache[art_id]
        else:
            t["artist_name"] = "Unknown Artist"

        alb_id = t.get("album_id")
        if alb_id:
            if alb_id not in album_cache:
                alb = db.get_library_album(alb_id)
                album_cache[alb_id] = alb["title"] if alb else "Unknown Album"
            t["album_title"] = album_cache[alb_id]
        else:
            t["album_title"] = "Unknown Album"

        t["file"] = db.get_library_file_for_track(t["id"])
        if t["file"] is None and art_id:
            if art_id not in filed_tracks_cache:
                filed_tracks_cache[art_id] = db.list_filed_tracks_for_artist(str(art_id))
            equiv = match_owned_equivalent(
                candidates=filed_tracks_cache[art_id],
                title=t.get("title") or "",
                duration_seconds=t.get("duration_seconds"),
                exclude_track_id=t["id"],
            )
            if equiv:
                eq_alb_id = equiv.get("album_id")
                eq_alb_title: str | None = None
                if eq_alb_id:
                    if eq_alb_id not in album_cache:
                        alb = db.get_library_album(eq_alb_id)
                        album_cache[eq_alb_id] = alb["title"] if alb else "Unknown Album"
                    eq_alb_title = album_cache[eq_alb_id]
                t["owned_elsewhere"] = {
                    "track_id": equiv["id"],
                    "album_id": eq_alb_id,
                    "album_title": eq_alb_title,
                }
    return tracks_copy


def test_enrich_tracks_matches_per_row_lookups(test_db):
    """Seed artists, albums, tracks (some with files, one with two files, unknown artist, no album)

    and assert _enrich_tracks matches reference per-row output exactly.
    """
    test_db.conn.execute("PRAGMA foreign_keys = OFF;")
    ar1 = test_db.upsert_library_artist({"id": "ar-1", "name": "Radiohead"})
    ar2 = test_db.upsert_library_artist({"id": "ar-2", "name": "Daft Punk"})

    al1 = test_db.upsert_library_album({"id": "al-1", "artist_id": ar1["id"], "title": "OK Computer"})
    al2 = test_db.upsert_library_album({"id": "al-2", "artist_id": ar2["id"], "title": "Discovery"})

    # Track 1: normal with file
    t1 = test_db.upsert_library_track({"id": "tr-1", "artist_id": ar1["id"], "album_id": al1["id"], "title": "Airbag"})
    test_db.upsert_library_file({
        "id": "f-1", "track_id": t1["id"], "file_path": "/music/airbag.flac",
        "relative_path": "airbag.flac", "codec": "FLAC", "quality_name": "FLAC",
    })

    # Track 2: two file rows (replacement scenario; lowest rowid should be kept)
    t2 = test_db.upsert_library_track({"id": "tr-2", "artist_id": ar1["id"], "album_id": al1["id"], "title": "Paranoid Android"})
    test_db.upsert_library_file({
        "id": "f-2a", "track_id": t2["id"], "file_path": "/music/android_old.flac",
        "relative_path": "android_old.flac", "codec": "FLAC", "quality_name": "FLAC",
    })
    test_db.upsert_library_file({
        "id": "f-2b", "track_id": t2["id"], "file_path": "/music/android_new.flac",
        "relative_path": "android_new.flac", "codec": "FLAC", "quality_name": "FLAC",
    })

    # Track 3: without file
    test_db.upsert_library_track({"id": "tr-3", "artist_id": ar2["id"], "album_id": al2["id"], "title": "One More Time"})

    # Track 4: unknown artist ID
    t4 = test_db.upsert_library_track({"id": "tr-4", "artist_id": "ghost-artist", "album_id": al1["id"], "title": "Subterranean"})
    test_db.upsert_library_file({
        "id": "f-4", "track_id": t4["id"], "file_path": "/music/sub.flac",
        "relative_path": "sub.flac", "codec": "FLAC", "quality_name": "FLAC",
    })

    # Track 5: without album (album_id is empty/None)
    test_db.conn.execute(
        "INSERT INTO library_tracks (id, artist_id, album_id, title, clean_title) VALUES (?, ?, ?, ?, ?)",
        ("tr-5", ar1["id"], "", "Non-Album Track", "non album track"),
    )
    test_db.upsert_library_file({
        "id": "f-5", "track_id": "tr-5", "file_path": "/music/single.flac",
        "relative_path": "single.flac", "codec": "FLAC", "quality_name": "FLAC",
    })

    # Track 6: neither artist nor album
    test_db.conn.execute(
        "INSERT INTO library_tracks (id, artist_id, album_id, title, clean_title) VALUES (?, ?, ?, ?, ?)",
        ("tr-6", "", "", "Orphan Track", "orphan track"),
    )

    tracks = [
        test_db.get_library_track("tr-1"),
        test_db.get_library_track("tr-2"),
        test_db.get_library_track("tr-3"),
        test_db.get_library_track("tr-4"),
        test_db.get_library_track("tr-5"),
        test_db.get_library_track("tr-6"),
    ]

    ref_output = _reference_enrich_tracks(test_db, copy.deepcopy(tracks))
    batched_output = _enrich_tracks(test_db, copy.deepcopy(tracks))

    assert len(batched_output) == len(ref_output)
    for batched, ref in zip(batched_output, ref_output, strict=True):
        assert batched["id"] == ref["id"]
        assert batched["artist_name"] == ref["artist_name"]
        assert batched["album_title"] == ref["album_title"]
        assert batched["file"] == ref["file"]
        assert batched.get("owned_elsewhere") == ref.get("owned_elsewhere")


def test_enrich_tracks_query_count_is_constant(test_db):
    """Assert a 100-track page with files runs at most 10 statements."""
    for a_idx in range(5):
        test_db.upsert_library_artist({"id": f"ar-{a_idx}", "name": f"Artist {a_idx}"})
    for alb_idx in range(10):
        test_db.upsert_library_album({
            "id": f"al-{alb_idx}", "artist_id": f"ar-{alb_idx % 5}", "title": f"Album {alb_idx}"
        })

    for t_idx in range(100):
        tid = f"tr-{t_idx:03d}"
        test_db.upsert_library_track({
            "id": tid, "artist_id": f"ar-{t_idx % 5}", "album_id": f"al-{t_idx % 10}", "title": f"Track {t_idx}"
        })
        test_db.upsert_library_file({
            "id": f"f-{tid}", "track_id": tid, "file_path": f"/music/{tid}.flac",
            "relative_path": f"{tid}.flac", "codec": "FLAC", "quality_name": "FLAC",
        })

    tracks = [test_db.get_library_track(f"tr-{i:03d}") for i in range(100)]

    statements: list[str] = []
    test_db.conn.set_trace_callback(statements.append)
    try:
        enriched = _enrich_tracks(test_db, copy.deepcopy(tracks))
        assert len(enriched) == 100
        # Batched queries: 1 for artists, 1 for albums, 1 for files -> 3 total statements
        assert len(statements) <= 10
        assert any("FROM library_artists" in s for s in statements)
        assert any("FROM library_albums" in s for s in statements)
        assert any("FROM library_files" in s for s in statements)
    finally:
        test_db.conn.set_trace_callback(None)


def test_enrich_albums_batches_artist_names(test_db):
    """Albums from 3 artists + one with a missing artist -> names correct, Unknown Artist for missing."""
    test_db.conn.execute("PRAGMA foreign_keys = OFF;")
    ar1 = test_db.upsert_library_artist({"id": "ar-1", "name": "Artist One"})
    ar2 = test_db.upsert_library_artist({"id": "ar-2", "name": "Artist Two"})
    ar3 = test_db.upsert_library_artist({"id": "ar-3", "name": "Artist Three"})

    al1 = test_db.upsert_library_album({"id": "al-1", "artist_id": ar1["id"], "title": "Album 1"})
    test_db.upsert_library_album({"id": "al-2", "artist_id": ar2["id"], "title": "Album 2"})
    test_db.upsert_library_album({"id": "al-3", "artist_id": ar3["id"], "title": "Album 3"})
    test_db.conn.execute(
        "INSERT INTO library_albums (id, artist_id, title, clean_title) VALUES (?, ?, ?, ?)",
        ("al-4", "ghost-artist", "Album 4", "album 4"),
    )
    test_db.conn.execute(
        "INSERT INTO library_albums (id, artist_id, title, clean_title) VALUES (?, ?, ?, ?)",
        ("al-5", "", "Album 5", "album 5"),
    )

    test_db.upsert_library_track({"id": "tr-1", "album_id": al1["id"], "title": "T1"})
    test_db.upsert_library_track({"id": "tr-2", "album_id": al1["id"], "title": "T2"})

    albums = [test_db.get_library_album(aid) for aid in ["al-1", "al-2", "al-3", "al-4", "al-5"]]

    statements: list[str] = []
    test_db.conn.set_trace_callback(statements.append)
    try:
        enriched = _enrich_albums(test_db, copy.deepcopy(albums))
    finally:
        test_db.conn.set_trace_callback(None)

    assert len(enriched) == 5
    assert enriched[0]["artist_name"] == "Artist One"
    assert enriched[0]["track_count"] == 2
    assert enriched[1]["artist_name"] == "Artist Two"
    assert enriched[2]["artist_name"] == "Artist Three"
    assert enriched[3]["artist_name"] == "Unknown Artist"
    assert enriched[4]["artist_name"] == "Unknown Artist"
    # Batch statement count: 1 for track counts, 1 for artist names
    assert len(statements) <= 5


def test_enrich_handles_empty_page(test_db):
    """Empty lists return [] without error and without executing queries."""
    statements: list[str] = []
    test_db.conn.set_trace_callback(statements.append)
    try:
        assert _enrich_tracks(test_db, []) == []
        assert _enrich_albums(test_db, []) == []
        assert _enrich_artists(test_db, []) == []
        assert len(statements) == 0
    finally:
        test_db.conn.set_trace_callback(None)


def test_owned_elsewhere_still_reported(test_db):
    """Fileless track whose title/duration matches filed track on another album of same artist gets owned_elsewhere."""
    ar = test_db.upsert_library_artist({"id": "ar-floyd", "name": "Pink Floyd"})

    al1 = test_db.upsert_library_album({"id": "al-darkside", "artist_id": ar["id"], "title": "The Dark Side of the Moon"})
    al2 = test_db.upsert_library_album({"id": "al-echoes", "artist_id": ar["id"], "title": "Echoes: The Best of Pink Floyd"})

    # Track 1 on Dark Side: filed
    t1 = test_db.upsert_library_track({
        "id": "tr-time-darkside", "artist_id": ar["id"], "album_id": al1["id"],
        "title": "Time", "duration_seconds": 420.0,
    })
    test_db.upsert_library_file({
        "id": "f-time", "track_id": t1["id"], "file_path": "/music/time.flac",
        "relative_path": "time.flac", "codec": "FLAC", "quality_name": "FLAC",
    })

    # Track 2 on Echoes: unfiled
    t2 = test_db.upsert_library_track({
        "id": "tr-time-echoes", "artist_id": ar["id"], "album_id": al2["id"],
        "title": "Time", "duration_seconds": 420.0,
    })

    track_echoes = test_db.get_library_track(t2["id"])

    # Case A: Only track 2 is enriched; album 1 is not in the page's batched albums,
    # so owned_elsewhere falls back to db.get_library_album(eq_alb_id)
    enriched = _enrich_tracks(test_db, [copy.deepcopy(track_echoes)])
    assert len(enriched) == 1
    assert enriched[0]["file"] is None
    assert enriched[0].get("owned_elsewhere") == {
        "track_id": t1["id"],
        "album_id": al1["id"],
        "album_title": "The Dark Side of the Moon",
    }

    # Case B: Both tracks enriched together; album 1 is already in batched album_cache
    track_darkside = test_db.get_library_track(t1["id"])
    enriched_both = _enrich_tracks(test_db, [copy.deepcopy(track_darkside), copy.deepcopy(track_echoes)])
    assert len(enriched_both) == 2
    assert enriched_both[0]["file"] is not None
    assert "owned_elsewhere" not in enriched_both[0]
    assert enriched_both[1]["owned_elsewhere"] == {
        "track_id": t1["id"],
        "album_id": al1["id"],
        "album_title": "The Dark Side of the Moon",
    }


def test_chunked_helper():
    """Verify _chunked handles empty lists, small lists, and large sequences correctly."""
    assert list(_chunked([])) == []
    assert list(_chunked([1, 2, 3], chunk_size=2)) == [[1, 2], [3]]
    items = list(range(1250))
    chunks = list(_chunked(items, chunk_size=500))
    assert len(chunks) == 3
    assert len(chunks[0]) == 500
    assert len(chunks[1]) == 500
    assert len(chunks[2]) == 250
    assert [x for c in chunks for x in c] == items


def test_enrich_tracks_chunking_beyond_500(test_db):
    """Verify _enrich_tracks functions correctly with more than 500 items, exercising chunking."""
    test_db.upsert_library_artist({"id": "ar-big", "name": "Big Artist"})
    test_db.upsert_library_album({"id": "al-big", "artist_id": "ar-big", "title": "Big Album"})

    tracks = []
    for i in range(600):
        tid = f"tr-chunk-{i}"
        tracks.append({
            "id": tid,
            "artist_id": "ar-big",
            "album_id": "al-big",
            "title": f"Big Track {i}",
        })

    enriched = _enrich_tracks(test_db, tracks)
    assert len(enriched) == 600
    assert all(t["artist_name"] == "Big Artist" for t in enriched)
    assert all(t["album_title"] == "Big Album" for t in enriched)
    assert all(t["file"] is None for t in enriched)


def test_enrich_artists_batches_and_counts(test_db):
    """Verify _enrich_artists attaches album/track counts and cover URL correctly via db.reader()."""
    ar1 = test_db.upsert_library_artist({"id": "ar-1", "name": "Artist 1"})
    ar2 = test_db.upsert_library_artist({"id": "ar-2", "name": "Artist 2"})

    test_db.upsert_library_album({"id": "al-1", "artist_id": ar1["id"], "title": "Album 1", "cover_url": "http://art/1.jpg"})
    test_db.upsert_library_album({"id": "al-2", "artist_id": ar1["id"], "title": "Album 2"})
    test_db.upsert_library_album({"id": "al-3", "artist_id": ar2["id"], "title": "Album 3"})

    test_db.upsert_library_track({"id": "tr-1", "artist_id": ar1["id"], "album_id": "al-1", "title": "T1"})
    test_db.upsert_library_file({
        "id": "f-1", "track_id": "tr-1", "file_path": "/m/1.flac", "relative_path": "1.flac",
        "codec": "FLAC", "quality_name": "FLAC",
    })

    artists = [test_db.get_library_artist(ar1["id"]), test_db.get_library_artist(ar2["id"])]
    enriched = _enrich_artists(test_db, artists)

    assert len(enriched) == 2
    assert enriched[0]["id"] == "ar-1"
    assert enriched[0]["album_count"] == 2
    assert enriched[0]["total_track_count"] == 1
    assert enriched[0]["track_file_count"] == 1
    assert "image_url" in enriched[0]

    assert enriched[1]["id"] == "ar-2"
    assert enriched[1]["album_count"] == 1
    assert enriched[1]["total_track_count"] == 0


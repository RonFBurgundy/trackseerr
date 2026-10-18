"""Tests for finding owned equivalent tracks across album editions."""

from pathlib import Path
import pytest

from trackseerr.storage import Database
from trackseerr.track_matching import find_owned_equivalent


@pytest.fixture
def test_db(tmp_path: Path):
    """Provides an isolated Database instance."""
    db = Database(str(tmp_path / "test_owned_equiv.db"))
    try:
        yield db
    finally:
        db.close()


def test_remaster_suffix_matches_owned_track(test_db: Database) -> None:
    test_db.upsert_library_artist({"id": "art-1", "name": "Prince", "monitored": True})
    test_db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": "Sign O' The Times", "monitored": True})
    test_db.upsert_library_track({
        "id": "trk-1",
        "album_id": "alb-1",
        "artist_id": "art-1",
        "title": "Play in the Sunshine",
        "duration_seconds": 265.0,
        "monitored": True,
    })
    test_db.upsert_library_file({
        "track_id": "trk-1",
        "file_path": "/music/Prince/Sign O The Times/02 Play in the Sunshine.flac",
        "cutoff_met": True,
    })

    match = find_owned_equivalent(test_db, "art-1", "Play In The Sunshine - 2020 Remaster")
    assert match is not None
    assert match["id"] == "trk-1"
    assert match["title"] == "Play in the Sunshine"


def test_curly_apostrophe_and_bracket_remaster(test_db: Database) -> None:
    # 1. Curly apostrophe + dash remaster
    test_db.upsert_library_artist({"id": "art-neil", "name": "Neil Young", "monitored": True})
    test_db.upsert_library_album({"id": "alb-harvest", "artist_id": "art-neil", "title": "Harvest", "monitored": True})
    test_db.upsert_library_track({
        "id": "trk-world",
        "album_id": "alb-harvest",
        "artist_id": "art-neil",
        "title": "There’s a World",
        "monitored": True,
    })
    test_db.upsert_library_file({
        "track_id": "trk-world",
        "file_path": "/music/Neil Young/Harvest/There's a World.flac",
        "cutoff_met": True,
    })

    match1 = find_owned_equivalent(test_db, "art-neil", "There's a World - 2009 Remaster")
    assert match1 is not None
    assert match1["id"] == "trk-world"

    # 2. Bracketed remaster
    test_db.upsert_library_artist({"id": "art-trex", "name": "T. Rex", "monitored": True})
    test_db.upsert_library_album({"id": "alb-slider", "artist_id": "art-trex", "title": "Electric Warrior", "monitored": True})
    test_db.upsert_library_track({
        "id": "trk-gong",
        "album_id": "alb-slider",
        "artist_id": "art-trex",
        "title": "Bang a Gong (Get It On)",
        "monitored": True,
    })
    test_db.upsert_library_file({
        "track_id": "trk-gong",
        "file_path": "/music/T. Rex/Electric Warrior/Bang a Gong.flac",
        "cutoff_met": True,
    })

    match2 = find_owned_equivalent(test_db, "art-trex", "Bang a Gong (Get It On) [2003 Remaster]")
    assert match2 is not None
    assert match2["id"] == "trk-gong"


def test_identity_qualifier_does_not_match(test_db: Database) -> None:
    test_db.upsert_library_artist({"id": "art-omam", "name": "Of Monsters and Men", "monitored": True})
    test_db.upsert_library_album({"id": "alb-omam", "artist_id": "art-omam", "title": "My Head Is an Animal", "monitored": True})

    test_db.upsert_library_track({
        "id": "trk-ms",
        "album_id": "alb-omam",
        "artist_id": "art-omam",
        "title": "Mountain Sound",
        "monitored": True,
    })
    test_db.upsert_library_file({
        "track_id": "trk-ms",
        "file_path": "/music/OMAM/Mountain Sound.flac",
        "cutoff_met": True,
    })

    # Unknown bracket qualifier is identity-bearing and should not match
    match1 = find_owned_equivalent(test_db, "art-omam", "Mountain Sound (The Cabin Sessions)")
    assert match1 is None

    test_db.upsert_library_track({
        "id": "trk-wait",
        "album_id": "alb-omam",
        "artist_id": "art-omam",
        "title": "I'll Be Waiting",
        "monitored": True,
    })
    test_db.upsert_library_file({
        "track_id": "trk-wait",
        "file_path": "/music/OMAM/Waiting.flac",
        "cutoff_met": True,
    })

    match2 = find_owned_equivalent(test_db, "art-omam", "I'll Be Waiting (Instrumental)")
    assert match2 is None


def test_track_without_file_is_ignored(test_db: Database) -> None:
    test_db.upsert_library_artist({"id": "art-1", "name": "Artist", "monitored": True})
    test_db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": "Album", "monitored": True})
    test_db.upsert_library_track({
        "id": "trk-nofile",
        "album_id": "alb-1",
        "artist_id": "art-1",
        "title": "Some Song",
        "monitored": True,
    })

    match = find_owned_equivalent(test_db, "art-1", "Some Song")
    assert match is None


def test_duration_mismatch_rejects(test_db: Database) -> None:
    test_db.upsert_library_artist({"id": "art-1", "name": "Artist", "monitored": True})
    test_db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": "Album", "monitored": True})
    test_db.upsert_library_track({
        "id": "trk-dur",
        "album_id": "alb-1",
        "artist_id": "art-1",
        "title": "Timed Song",
        "duration_seconds": 200.0,
        "monitored": True,
    })
    test_db.upsert_library_file({
        "track_id": "trk-dur",
        "file_path": "/music/Timed Song.flac",
        "cutoff_met": True,
    })

    # Duration mismatch: 260s vs 200s (diff 60s > 5.0s)
    match_fail = find_owned_equivalent(test_db, "art-1", "Timed Song", duration_seconds=260.0)
    assert match_fail is None

    # Duration match: 203s vs 200s (diff 3s <= 5.0s)
    match_ok = find_owned_equivalent(test_db, "art-1", "Timed Song", duration_seconds=203.0)
    assert match_ok is not None
    assert match_ok["id"] == "trk-dur"


def test_excluded_track_and_other_artist_ignored(test_db: Database) -> None:
    test_db.upsert_library_artist({"id": "art-1", "name": "Artist 1", "monitored": True})
    test_db.upsert_library_artist({"id": "art-2", "name": "Artist 2", "monitored": True})
    test_db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": "Album", "monitored": True})
    test_db.upsert_library_track({
        "id": "trk-ex",
        "album_id": "alb-1",
        "artist_id": "art-1",
        "title": "Shared Title",
        "monitored": True,
    })
    test_db.upsert_library_file({
        "track_id": "trk-ex",
        "file_path": "/music/Shared.flac",
        "cutoff_met": True,
    })

    # Excluded track
    assert find_owned_equivalent(test_db, "art-1", "Shared Title", exclude_track_id="trk-ex") is None

    # Different artist
    assert find_owned_equivalent(test_db, "art-2", "Shared Title") is None

    # None artist
    assert find_owned_equivalent(test_db, None, "Shared Title") is None

    # Empty title
    assert find_owned_equivalent(test_db, "art-1", "") is None

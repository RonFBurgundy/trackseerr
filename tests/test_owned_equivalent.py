"""Tests for finding owned equivalent tracks across album editions."""

from pathlib import Path
import pytest

from trackseerr.models import MusicRequest
from trackseerr.storage import Database
from trackseerr.track_matching import find_owned_equivalent, match_owned_equivalent, wanted_song


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


def test_match_owned_equivalent_pure() -> None:
    candidates = [
        {"id": "c1", "title": "Paranoid Android (Remastered)", "duration_seconds": 387.0, "cutoff_met": False},
        {"id": "c2", "title": "Paranoid Android", "duration_seconds": 386.0, "cutoff_met": True},
        {"id": "c3", "title": "Karma Police", "duration_seconds": 264.0, "cutoff_met": True},
    ]

    # Matches Paranoid Android and prefers cutoff_met (c2 over c1)
    res = match_owned_equivalent(candidates, "Paranoid Android - 2017 Remaster", duration_seconds=385.0)
    assert res is not None
    assert res["id"] == "c2"

    # When c2 is excluded, matches c1
    res_ex = match_owned_equivalent(candidates, "Paranoid Android", duration_seconds=387.0, exclude_track_id="c2")
    assert res_ex is not None
    assert res_ex["id"] == "c1"

    # Duration mismatch (> 5.0 s)
    res_dur = match_owned_equivalent(candidates, "Paranoid Android", duration_seconds=400.0)
    assert res_dur is None

    # Unknown duration matches candidate
    res_no_dur = match_owned_equivalent(candidates, "Karma Police")
    assert res_no_dur is not None
    assert res_no_dur["id"] == "c3"

    # Empty candidate list or unmatching title
    assert match_owned_equivalent([], "Paranoid Android") is None
    assert match_owned_equivalent(candidates, "Creep") is None
    assert match_owned_equivalent(candidates, "") is None


def test_zero_duration_is_unknown() -> None:
    candidates_zero = [
        {"id": "c1", "title": "Song", "duration_seconds": 0.0, "cutoff_met": True},
    ]
    # Candidate with 0.0s matches wanted duration of 200.0s (treated as unknown length)
    assert match_owned_equivalent(candidates_zero, "Song", duration_seconds=200.0) is not None

    candidates_known = [
        {"id": "c2", "title": "Song", "duration_seconds": 200.0, "cutoff_met": True},
    ]
    # Wanted duration of 0.0s matches candidate with 200.0s (treated as unknown length)
    assert match_owned_equivalent(candidates_known, "Song", duration_seconds=0.0) is not None

    # Candidate with None or <= 0 duration
    candidates_neg = [
        {"id": "c3", "title": "Song", "duration_seconds": -1.0, "cutoff_met": True},
    ]
    assert match_owned_equivalent(candidates_neg, "Song", duration_seconds=200.0) is not None

    # Wanted duration of -5.0s matches known candidate
    assert match_owned_equivalent(candidates_known, "Song", duration_seconds=-5.0) is not None

    # Both known positive durations that differ > 5.0s do not match
    assert match_owned_equivalent(candidates_known, "Song", duration_seconds=260.0) is None


def test_wanted_song_prefers_track_then_request_never_release_title(test_db: Database) -> None:
    test_db.upsert_user("u1", "user", "u@example.com", is_admin=True)
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
    test_db.create_request(
        MusicRequest(
            id="req-1",
            user_id="u1",
            item_type="track",
            title="Play In The Sunshine - 2020 Remaster",
            artist="Prince",
        )
    )

    # 1. Prefers track when item["track_id"] resolves via db.get_library_track
    item_track = {
        "title": "Prince - Play In The Sunshine (2020 Remaster) [FLAC 24bit 44.1kHz]",
        "track_id": "trk-1",
        "request_id": "req-1",
    }
    title, dur = wanted_song(test_db, item_track)
    assert title == "Play in the Sunshine"
    assert dur == 265.0

    # 2. Falls back to request when track_id is absent or unresolvable
    item_req = {
        "title": "Prince - Play In The Sunshine (2020 Remaster) [FLAC 24bit 44.1kHz]",
        "track_id": None,
        "request_id": "req-1",
    }
    title, dur = wanted_song(test_db, item_req)
    assert title == "Play In The Sunshine - 2020 Remaster"
    assert dur is None

    item_unresolved_track = {
        "title": "Prince - Play In The Sunshine (2020 Remaster) [FLAC 24bit 44.1kHz]",
        "track_id": "nonexistent-track-id",
        "request_id": "req-1",
    }
    title, dur = wanted_song(test_db, item_unresolved_track)
    assert title == "Play In The Sunshine - 2020 Remaster"
    assert dur is None

    # 3. Never uses item["title"] — returns (None, None) when neither track nor request resolves
    item_neither = {
        "title": "Prince - Play In The Sunshine (2020 Remaster) [FLAC 24bit 44.1kHz]",
        "track_id": None,
        "request_id": None,
    }
    title, dur = wanted_song(test_db, item_neither)
    assert title is None
    assert dur is None


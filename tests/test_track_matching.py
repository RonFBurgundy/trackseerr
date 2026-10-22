"""Tests for track matching rules."""
from unittest.mock import MagicMock

from trackseerr.track_matching import find_fileless_equivalent, reconcile_audio_file_to_track_scored, titles_match


def test_album_version_matches_plain_title() -> None:
    assert titles_match("Won't Get Fooled Again (Original Album Version)", "Won’t Get Fooled Again")
    assert not titles_match("Beat Goes On (Extended Mix)", "Beat Goes On")
    assert not titles_match("Mountain Sound (The Cabin Sessions)", "Mountain Sound")


def test_number_match_rejected_when_title_differs() -> None:
    candidates = [{"id": "t11", "title": "Butterflies", "track_number": 11, "disc_number": 1}]
    meta = {"title": "Grand", "track_number": 11, "disc_number": 1}
    track, _ = reconcile_audio_file_to_track_scored(meta, candidates)
    assert track is None or track["title"] != "Butterflies"


def test_instrumental_file_not_matched_to_vocal_by_number() -> None:
    candidates = [
        {"id": "t3", "title": "Legend Has It", "track_number": 3, "disc_number": 1},
        {"id": "t13", "title": "Legend Has It (instrumental)", "track_number": 13, "disc_number": 1},
    ]
    meta = {
        "title": "Legend Has It",
        "track_number": 3,
        "disc_number": 1,
        "file_path": "/dl/03 Legend Has It (Instrumental).flac",
    }
    track, _ = reconcile_audio_file_to_track_scored(meta, candidates)
    assert track is not None and track["id"] == "t13"


def test_number_match_still_strong_when_titles_agree() -> None:
    candidates = [{"id": "t1", "title": "Song", "track_number": 1, "disc_number": 1}]
    meta = {"title": "Song", "track_number": 1, "disc_number": 1}
    track, strength = reconcile_audio_file_to_track_scored(meta, candidates)
    assert track is not None and track["id"] == "t1"
    assert strength == "strong"


def _fileless_db(*tracks: dict) -> MagicMock:
    db = MagicMock()
    db.list_fileless_tracks_for_artist.return_value = list(tracks)
    return db


def test_fileless_equivalent_isrc_match_ignores_duration() -> None:
    track = {"id": "t1", "title": "This Year's Girl", "duration_seconds": 230.0, "isrc": "GBASQ7800024"}
    db = _fileless_db(track)
    assert find_fileless_equivalent(db, ["a1"], "This Year's Girl", 200.0, isrc=" gbasq7800024 ") == track
    assert find_fileless_equivalent(db, ["a1"], "This Year's Girl", None, isrc="GBASQ7800024") == track


def test_fileless_equivalent_isrc_mismatch_falls_back_to_duration() -> None:
    track = {"id": "t1", "title": "This Year's Girl", "duration_seconds": 230.0, "isrc": "GBASQ7800024"}
    db = _fileless_db(track)
    assert find_fileless_equivalent(db, ["a1"], "This Year's Girl", 200.0, isrc="USXXX0000001") is None


def test_fileless_equivalent_two_candidates_still_none() -> None:
    t1 = {"id": "t1", "title": "This Year's Girl", "duration_seconds": 204.0}
    t2 = {"id": "t2", "title": "This Year's Girl", "duration_seconds": 202.0}
    assert find_fileless_equivalent(_fileless_db(t1, t2), ["a1"], "This Year's Girl", 203.0) is None

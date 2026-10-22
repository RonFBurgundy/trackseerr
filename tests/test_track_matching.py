"""Tests for track matching rules."""
from trackseerr.track_matching import reconcile_audio_file_to_track_scored, titles_match


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

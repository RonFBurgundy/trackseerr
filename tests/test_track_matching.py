"""Tests for track matching rules."""
from trackseerr.track_matching import titles_match


def test_album_version_matches_plain_title() -> None:
    assert titles_match("Won't Get Fooled Again (Original Album Version)", "Won’t Get Fooled Again")
    assert not titles_match("Beat Goes On (Extended Mix)", "Beat Goes On")
    assert not titles_match("Mountain Sound (The Cabin Sessions)", "Mountain Sound")

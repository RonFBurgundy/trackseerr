"""Tests for release and album title normalization."""
from trackseerr.lidarr_release import norm_title, titles_match


def test_norm_title_drops_platinum_edition() -> None:
    assert norm_title("BEYONCÉ [Platinum Edition]") == norm_title("BEYONCÉ")


def test_norm_title_drops_anniversary_edition() -> None:
    assert norm_title("Thriller (25th Anniversary Edition)") == norm_title("Thriller")
    assert norm_title("(25th Anniversary Edition)") == ""


def test_norm_title_keeps_live() -> None:
    assert norm_title("Partition (Live)") != norm_title("Partition")


def test_norm_title_drops_streaming_qualifiers() -> None:
    base = norm_title("Hotel California")
    assert norm_title("Hotel California (Hi-Res Version)") == base
    assert norm_title("Hotel California (Hi Res)") == base
    assert norm_title("Hotel California - 2013 Remaster") == base
    assert norm_title("Hotel California (Remastered 2011)") == base
    assert norm_title("Hotel California (24 bit Remaster)") == base
    assert norm_title("Hotel California (Deluxe)") == base
    assert norm_title("Hotel California (Expanded Edition)") == base


def test_mixed_bracket_drops_noise_part() -> None:
    assert titles_match("Happiest Girl (Jack mix)", "Happiest Girl (Jack Mix; 2006 Remaster)")


def test_mixed_bracket_keeps_identity_part() -> None:
    assert not titles_match("Happiest Girl", "Happiest Girl (Jack Mix; 2006 Remaster)")


def test_mixed_bracket_live_and_remaster() -> None:
    assert titles_match("Song (Live)", "Song (Live, 2011 Remaster)")
    assert not titles_match("Song", "Song (Live, 2011 Remaster)")


def test_all_noise_bracket_still_dropped() -> None:
    assert titles_match("Song", "Song (2011 Remaster; Explicit)")

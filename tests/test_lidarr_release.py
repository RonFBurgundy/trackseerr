"""Tests for release and album title normalization."""
from trackseerr.lidarr_release import norm_title


def test_norm_title_drops_platinum_edition() -> None:
    assert norm_title("BEYONCÉ [Platinum Edition]") == norm_title("BEYONCÉ")


def test_norm_title_drops_anniversary_edition() -> None:
    assert norm_title("Thriller (25th Anniversary Edition)") == norm_title("Thriller")
    assert norm_title("(25th Anniversary Edition)") == ""


def test_norm_title_keeps_live() -> None:
    assert norm_title("Partition (Live)") != norm_title("Partition")

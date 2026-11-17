"""Tests for album-specific title cleaning and comparison rules."""

from __future__ import annotations

import pytest

from trackseerr.album_titles import (
    album_titles_match,
    clean_new_album_title,
    strip_album_junk,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("The E•N•D (2009)", "The E•N•D"),
        (
            "Legend – The Best Of Bob Marley & The Wailers (1984)",
            "Legend – The Best Of Bob Marley & The Wailers",
        ),
        ("Long Jacket - Single (2026)", "Long Jacket"),
        ("Long Jacket - EP", "Long Jacket"),
        ("Long Jacket (Single)", "Long Jacket"),
        ("Long Jacket (EP)", "Long Jacket"),
        ("Those Were the Days (Disc 2)", "Those Were the Days"),
        ("Those Were the Days (CD 1)", "Those Were the Days"),
        ("Those Were the Days [Disc 3]", "Those Were the Days"),
        ("In the Dark [2013 Remaster] (1987)", "In the Dark"),
        ("Disraeli Gears (2013 SHM-SACD) (1967)", "Disraeli Gears"),
        ("Kind of Blue (24bit)", "Kind of Blue"),
        ("Kind of Blue [FLAC]", "Kind of Blue"),
        ("Kind of Blue (Vinyl Rip)", "Kind of Blue"),
        ("Synchronicity [1983]", "Synchronicity"),
        # Negatives
        ("1984", "1984"),
        ("(1984)", "(1984)"),
        ("Live at Wembley (Live)", "Live at Wembley (Live)"),
        ("Disc Jockey", "Disc Jockey"),
        ("Single Ladies", "Single Ladies"),
        ("Blink-182 (Deluxe)", "Blink-182 (Deluxe)"),
        ("[FLAC] Kind of Blue", "[FLAC] Kind of Blue"),
        ("Kind of [FLAC] Blue", "Kind of [FLAC] Blue"),
    ],
)
def test_strip_album_junk(raw: str, expected: str) -> None:
    assert strip_album_junk(raw) == expected


def test_album_titles_match() -> None:
    assert album_titles_match("The E•N•D (2009)", "The E.N.D.")
    assert album_titles_match("Long Jacket - Single (2026)", "Long Jacket")
    assert album_titles_match("Blink-182 (Deluxe)", "Blink-182")
    assert album_titles_match("In the Dark [2013 Remaster] (1987)", "In the Dark")
    assert album_titles_match("Disraeli Gears (2013 SHM-SACD) (1967)", "Disraeli Gears")
    assert album_titles_match("Those Were the Days (Disc 2)", "Those Were the Days")
    assert not album_titles_match("Live at Wembley (Live)", "Live at Wembley")


def test_clean_new_album_title() -> None:
    assert clean_new_album_title("The E•N•D (2009)", 2009) == "The E•N•D"
    assert clean_new_album_title("1999 (1999)", 1999) == "1999"
    assert clean_new_album_title("Abbey Road (1970)", 1969) == "Abbey Road (1970)"
    assert clean_new_album_title("Long Jacket - Single (2026)", 2026) == "Long Jacket"
    assert clean_new_album_title("Long Jacket - EP", None) == "Long Jacket"
    assert (
        clean_new_album_title("In the Dark [2013 Remaster] (1987)", 1987)
        == "In the Dark [2013 Remaster]"
    )
    assert clean_new_album_title("1999", 1999) == "1999"
    assert clean_new_album_title("(1999)", 1999) == "(1999)"

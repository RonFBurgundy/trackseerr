"""Tests for trackseerr.search_terms query simplification and relevance gating."""

from trackseerr.search_terms import (
    build_query_ladder,
    matches_request,
    primary_artist,
    strip_featuring,
    title_core,
    version_descriptor,
)

MP_ARTIST = "Morgan Page, BT, Ned Shepard & Sultan"
MP_TITLE = "In the Air (Mord Fustang Remix) [feat. Angela McCluskey]"


def test_primary_artist_splits_credit_list():
    assert primary_artist(MP_ARTIST) == "Morgan Page"
    assert primary_artist("A feat. B") == "A"
    assert primary_artist("A x B") == "A"
    assert primary_artist("A vs. B") == "A"
    assert primary_artist("  Radiohead ") == "Radiohead"
    assert primary_artist("") == ""


def test_strip_featuring_bracket_and_trailing():
    assert strip_featuring(MP_TITLE) == "In the Air (Mord Fustang Remix)"
    assert strip_featuring("Song (ft. Someone)") == "Song"
    assert strip_featuring("Song (featuring Someone)") == "Song"
    assert strip_featuring("Song feat. Someone Else") == "Song"
    assert strip_featuring("Plain Song") == "Plain Song"


def test_version_descriptor_detects_remix_and_none_for_plain():
    assert version_descriptor(MP_TITLE) == "Mord Fustang Remix"
    assert version_descriptor("Song [Live at Wembley]") == "Live at Wembley"
    assert version_descriptor("Plain Song") is None
    assert version_descriptor("Song (feat. X)") is None


def test_title_core_removes_groups():
    assert title_core(MP_TITLE) == "In the Air"
    assert title_core("Song - 2011 Remaster") == "Song"
    assert title_core("Song (Live) [Deluxe]") == "Song"


def test_build_query_ladder_morgan_page():
    assert build_query_ladder(MP_ARTIST, MP_TITLE, None) == [
        (MP_ARTIST, MP_TITLE, None),
        ("Morgan Page", "In the Air (Mord Fustang Remix)", None),
        ("Morgan Page", "In the Air Mord Fustang Remix", None),
    ]


def test_build_query_ladder_dedupes_simple_title():
    assert build_query_ladder("Daft Punk", "One More Time", None) == [("Daft Punk", "One More Time", None)]


def test_build_query_ladder_album_request():
    ladder = build_query_ladder("Artist, Other", None, "Great Album (Deluxe Edition)")
    assert ladder[0] == ("Artist, Other", None, "Great Album (Deluxe Edition)")
    assert ladder[1] == ("Artist", None, "Great Album (Deluxe Edition)")
    assert ladder[-1] == ("Artist", None, "Great Album")
    assert all(r[1] is None for r in ladder)


def test_matches_request_requires_remixer():
    assert matches_request("Morgan Page - In The Air (Mord Fustang Remix) [2011] FLAC", MP_TITLE, None)
    assert not matches_request("Morgan Page - In The Air (Original Mix)", MP_TITLE, None)
    assert not matches_request("Completely Different Song Name", MP_TITLE, None)


def test_matches_request_original_rejects_remix():
    assert matches_request("Morgan Page - In The Air [FLAC]", "In the Air", None)
    assert not matches_request("Morgan Page - In The Air (Someone Remix)", "In the Air", None)

import pytest

from trackseerr.search_terms import (
    banned_term_queries,
    build_query_ladder,
    matches_request,
    primary_artist,
    soulseek_file_matches_track,
    soulseek_file_titles,
    strip_featuring,
    title_core,
    version_descriptor,
    wildcard_word,
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
    assert matches_request("Morgan Page - In The Air (Mord Fustang Remix) [2011] FLAC", "Morgan Page", MP_TITLE, None)
    assert not matches_request("Morgan Page - In The Air (Original Mix)", "Morgan Page", MP_TITLE, None)
    assert not matches_request("Completely Different Song Name", "Morgan Page", MP_TITLE, None)


def test_matches_request_original_rejects_remix():
    assert matches_request("Morgan Page - In The Air [FLAC]", "Morgan Page", "In the Air", None)
    assert not matches_request("Morgan Page - In The Air (Someone Remix)", "Morgan Page", "In the Air", None)


def test_matches_request_rejects_other_artist():
    artist = "Earth, Wind & Fire"
    assert not matches_request("Taylor Swift - September (2023) FLAC", artist, "September", None)
    assert matches_request("Earth Wind & Fire - September [1978] FLAC", artist, "September", None)


_ALEX_PATH = "Music\\ALEX\\ALEX - AKUMA (2019)\\"


def test_matches_request_title_text_ignores_album_folder():
    text = "01 - 05 - Danger City [FLAC 24bit 48kHz] " + _ALEX_PATH + "01 - 05 - Danger City.flac"
    assert not matches_request(text, "ALEX", "AKUMA", "AKUMA", title_text="01 - 05 - Danger City [FLAC 24bit 48kHz]")
    assert matches_request(text, "ALEX", "AKUMA", "AKUMA")


def test_matches_request_title_text_accepts_real_track():
    title_text = "03 - AKUMA [FLAC 16bit 44.1kHz]"
    text = title_text + " " + _ALEX_PATH + "03 - AKUMA.flac"
    assert matches_request(text, "ALEX", "AKUMA", "AKUMA", title_text=title_text)


def test_matches_request_empty_artist_skips_artist_check():
    assert matches_request("Taylor Swift - September (2023) FLAC", "", "September", None)


@pytest.mark.parametrize(
    ("wanted_title", "file_name", "expected"),
    [
        # Rejects from Context
        ("Time", "03 - All of the Time.flac", False),
        ("Under Pressure", "Logic - Under Pressure - 06 - Bounce", False),
        ("Copperhead Road", "Steve Earle - Copperhead Road - 10 - Nothing but a Child", False),
        ("Overload", "Kayzo - Overload - 06 - Feel the Power", False),
        ("Wait", "12 - Wait It Out", False),
        ("Down (instrumental)", "07 Run The Jewels - No Come Down (Instrumental)", False),
        ("Everything Has Changed", "14 - Everything Has Changed (Taylor’s version)", False),
        ("Beat Goes On", "Beat Goes On (Extended Mix)", False),
        ("Mountain Sound", "Of Monsters and Men - My Head Is An Animal (The Cabin Sessions) - 04 - Mountain Sound (The Cabin Sessions)", False),
        # Accepts from Context
        ("Lipstick Vogue - 2021 Remaster", "Elvis-Costello-This-Year’s-Model-11-Lipstick-Vogue", True),
        ("Pump It Up - 2021 Remaster", "04-elvis_costello-pump_it_up_(2021_remaster)-0d2e49e8", True),
        ("Life on Mars? - 2015 Remaster", "04 - Life on Mars？ (2015 Remaster)", True),
        ("There's a World - 2009 Remaster", "Neil Young - Harvest (2009 Remaster) - There's a World - 2009 Remaster", True),
        ("It's Gonna Be A Beautiful Night - 2020 Remaster", "15 - It’s Gonna Be a Beautiful Night (2020 remaster)", True),
        ("Play In The Sunshine - 2020 Remaster", "Prince - Play In The Sunshine (2020 Remaster)", True),
        ("Lean Woman Blues - 2003 Remaster", "05-Lean_Woman_Blues", True),
        ("Monolith - 2003 Remaster", "1-04 - Monolith", True),
    ],
)
def test_soulseek_file_titles_and_match(wanted_title: str, file_name: str, expected: bool):
    titles = soulseek_file_titles(file_name)
    assert len(titles) > 0
    assert soulseek_file_matches_track(file_name, wanted_title) is expected


def test_wildcard_word_longest_word():
    assert wildcard_word("Franz Ferdinand") == "Franz *erdinand"
    assert wildcard_word("Coexist") == "*oexist"
    assert wildcard_word("XO") is None


def test_banned_term_queries_order():
    queries = banned_term_queries("Franz Ferdinand", "Darts of Pleasure", None)
    assert queries == [
        "Darts of Pleasure",
        "Franz *erdinand Darts of Pleasure",
        "Franz Ferdinand Darts of *leasure",
    ]


def test_banned_term_queries_album_only_and_short_title():
    album_queries = banned_term_queries("The xx", None, "Coexist")
    assert album_queries == ["Coexist", "The xx *oexist"]

    short_title_queries = banned_term_queries("The Beatles", "XO", None)
    assert "XO" not in short_title_queries
    assert short_title_queries == ["The *eatles XO"]



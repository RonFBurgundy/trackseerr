from trackseerr.censored import censored_pattern, is_censored, search_text
from trackseerr.storage_common import clean_library_name

CENSORED = "Grab The Devil By The Horns And **** Him Up The ***"
REAL = "Grab The Devil By The Horns And Fuck Him Up The Ass"


def test_pattern_matches_uncensored_title():
    pattern = censored_pattern(CENSORED)
    assert pattern is not None
    assert pattern.fullmatch(clean_library_name(REAL))


def test_pattern_rejects_word_count_mismatch():
    pattern = censored_pattern(CENSORED)
    assert pattern is not None
    assert not pattern.fullmatch(clean_library_name("Grab The Devil By The Horns"))


def test_in_word_run_matches():
    pattern = censored_pattern("F**k You")
    assert pattern is not None
    assert pattern.fullmatch(clean_library_name("Fuck You"))
    assert not pattern.fullmatch(clean_library_name("Fun Times"))


def test_uncensored_and_all_asterisk_titles():
    assert censored_pattern("Plain Title") is None
    assert not is_censored("Single * star")
    assert censored_pattern("****") is None


def test_search_text_longest_uncensored_segment():
    assert is_censored(CENSORED)
    assert search_text(CENSORED) == "Grab The Devil By The Horns And"


def test_search_text_drops_masked_tokens():
    assert search_text("F**k You") == "You"
    assert search_text("****") == ""

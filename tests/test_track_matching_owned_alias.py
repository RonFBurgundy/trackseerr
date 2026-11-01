"""Tests for loose owned-song alias matching (combined titles and leading articles)."""

from trackseerr.track_matching import match_owned_equivalent, same_song_title


def test_combined_mb_title_matches_first_segment():
    owned = "Blue Dress / Interlude #3"
    wanted = "Blue Dress - 2006 Remaster"
    assert same_song_title(owned, wanted) is True

    candidates = [{"id": "t1", "title": owned, "duration_seconds": 341.0, "cutoff_met": True}]
    res = match_owned_equivalent(candidates, wanted, duration_seconds=341.0)
    assert res is not None
    assert res["id"] == "t1"


def test_combined_title_with_colon_segment():
    owned = "Enjoy the Silence / Interlude #2: Crucified"
    wanted = "Enjoy the Silence - 2006 Remaster"
    assert same_song_title(owned, wanted) is True

    candidates = [{"id": "t1", "title": owned, "duration_seconds": 373.0, "cutoff_met": True}]
    res = match_owned_equivalent(candidates, wanted, duration_seconds=372.0)
    assert res is not None
    assert res["id"] == "t1"


def test_later_segment_never_matches():
    owned = "Blue Dress / Interlude #3"
    wanted = "Interlude #3"
    assert same_song_title(owned, wanted) is False

    candidates = [{"id": "t1", "title": owned, "duration_seconds": 341.0, "cutoff_met": True}]
    res = match_owned_equivalent(candidates, wanted, duration_seconds=341.0)
    assert res is None


def test_leading_article_optional():
    owned = "Same Situation"
    wanted = "The Same Situation"
    assert same_song_title(owned, wanted) is True
    # Reverse check as well (owned has "The", wanted does not)
    assert same_song_title("The Same Situation", "Same Situation") is True

    candidates = [{"id": "t1", "title": owned, "duration_seconds": 177.0, "cutoff_met": True}]
    res = match_owned_equivalent(candidates, wanted, duration_seconds=178.0)
    assert res is not None
    assert res["id"] == "t1"


def test_loose_rule_needs_durations():
    owned = "Same Situation"
    wanted = "The Same Situation"
    # Duration None on owned
    candidates_no_dur = [{"id": "t1", "title": owned, "duration_seconds": None, "cutoff_met": True}]
    assert match_owned_equivalent(candidates_no_dur, wanted, duration_seconds=178.0) is None

    # Duration None on wanted
    candidates_with_dur = [{"id": "t1", "title": owned, "duration_seconds": 177.0, "cutoff_met": True}]
    assert match_owned_equivalent(candidates_with_dur, wanted, duration_seconds=None) is None

    # Both known but 30 s apart
    assert match_owned_equivalent(candidates_with_dur, wanted, duration_seconds=207.0) is None


def test_substring_is_not_a_match():
    owned = "All of the Time"
    wanted = "Time"
    assert same_song_title(owned, wanted) is False

    candidates = [{"id": "t1", "title": owned, "duration_seconds": 180.0, "cutoff_met": True}]
    assert match_owned_equivalent(candidates, wanted, duration_seconds=180.0) is None
    assert match_owned_equivalent(candidates, wanted, duration_seconds=None) is None


def test_exact_match_preferred_over_loose():
    candidates = [
        {"id": "loose", "title": "Blue Dress / Interlude #3", "duration_seconds": 341.0, "cutoff_met": True},
        {"id": "exact", "title": "Blue Dress", "duration_seconds": 341.0, "cutoff_met": True},
    ]
    res = match_owned_equivalent(candidates, "Blue Dress", duration_seconds=341.0)
    assert res is not None
    assert res["id"] == "exact"

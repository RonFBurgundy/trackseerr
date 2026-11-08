"""Tests for acquisition import title cleaning, prefixes, and fallbacks."""
from __future__ import annotations

from trackseerr.acquisition_import import _clean_pack_title, _strip_artist_prefix


def test_strip_artist_prefix_drops_feat_credit() -> None:
    assert (
        _strip_artist_prefix("Coolio Feat. L.V. - Gangsta's Paradise", "Coolio")
        == "Gangsta's Paradise"
    )
    assert (
        _strip_artist_prefix("Coolio ft. L.V. - Gangsta's Paradise", "Coolio")
        == "Gangsta's Paradise"
    )
    assert (
        _strip_artist_prefix("Coolio feat L.V. - Gangsta's Paradise", "Coolio")
        == "Gangsta's Paradise"
    )
    assert (
        _strip_artist_prefix("Coolio featuring L.V. - Gangsta's Paradise", "Coolio")
        == "Gangsta's Paradise"
    )


def test_strip_artist_prefix_feat_without_separator_unchanged() -> None:
    assert (
        _strip_artist_prefix("Coolio Feat. L.V. Gangsta's Paradise", "Coolio")
        == "Feat. L.V. Gangsta's Paradise"
    )


def test_clean_pack_title_drops_bpm() -> None:
    assert _clean_pack_title("Gotta Be Cool 124", None) == "Gotta Be Cool"
    assert _clean_pack_title("Rafael - Gotta Be Cool 124", None) == "Rafael - Gotta Be Cool"


def test_clean_pack_title_keeps_summer_of_69() -> None:
    assert _clean_pack_title("Summer Of 69", None) == "Summer Of 69"


def test_clean_pack_title_keeps_number_in_wanted_title() -> None:
    assert _clean_pack_title("Club 120", "Club 120") == "Club 120"
    assert _clean_pack_title("Club 120", "Club") == "Club"


def test_clean_pack_title_strips_clean_dirty_tags() -> None:
    assert (
        _clean_pack_title("All The Small Things (Clean) 148", None)
        == "All The Small Things"
    )
    assert _clean_pack_title("Song Title (Dirty) 130", None) == "Song Title"

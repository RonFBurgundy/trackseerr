"""Tests for acquisition import title cleaning, prefixes, and fallbacks."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from trackseerr.acquisition_import import (
    _clean_pack_title,
    _match_files_for_track,
    _strip_any_artist_prefix,
    _strip_artist_prefix,
)


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


def test_strip_any_artist_prefix() -> None:
    assert _strip_any_artist_prefix("Silkroad Ensemble - Green") == "Green"
    assert _strip_any_artist_prefix("A - 01 - Song") == "Song"
    assert _strip_any_artist_prefix("Jay-Z") == "Jay-Z"
    assert _strip_any_artist_prefix("Re-Up") == "Re-Up"


def test_match_files_other_artist_prefix_unique(tmp_path: Path) -> None:
    green = tmp_path / "01. Silkroad Ensemble - Green (Vincent's Tune).flac"
    ethiopia = tmp_path / "02. Silkroad Ensemble - Ethiopia.flac"
    green.touch()
    ethiopia.touch()

    with patch("trackseerr.acquisition_import.inspect_audio_file", return_value={}):
        matches = _match_files_for_track(
            [green, ethiopia],
            "Green (Vincent's Tune) (feat. Roomful of Teeth)",
            "Yo-Yo Ma",
        )

    assert len(matches) == 1
    assert matches[0][0] == green


def test_match_files_other_artist_prefix_not_unique(tmp_path: Path) -> None:
    first = tmp_path / "01. A - Green.flac"
    second = tmp_path / "05. B - Green.flac"
    first.touch()
    second.touch()

    with patch("trackseerr.acquisition_import.inspect_audio_file", return_value={}):
        matches = _match_files_for_track([first, second], "Green", "Yo-Yo Ma")

    assert matches == []

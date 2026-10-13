"""Pure helpers that keep an album's original release year."""

from datetime import datetime

import pytest

from trackseerr.album_years import merge_album_year, merge_release_date, plausible_year

NOW = datetime.now().year


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1971, 1971),
        ("1971", 1971),
        (" 1971 ", 1971),
        ("1971-05-02", 1971),
        (1900, 1900),
        (1884, None),
        ("1884-01-01", None),
        (NOW, NOW),
        (NOW + 1, NOW + 1),
        (NOW + 2, None),
        (None, None),
        ("", None),
        ("abc", None),
        ("19", None),
        ("19711", None),
        (True, None),
    ],
)
def test_plausible_year(value, expected):
    assert plausible_year(value) == expected


@pytest.mark.parametrize(
    ("existing", "incoming", "expected"),
    [
        (1971, 2011, 1971),
        (2011, 1971, 1971),
        (1971, 1971, 1971),
        (None, 1975, 1975),
        (1975, None, 1975),
        (1884, 1975, 1975),
        (1975, 1884, 1975),
        (1884, 1885, None),
        (None, None, None),
        ("1999-03-01", "2005", 1999),
        (NOW + 2, 2001, 2001),
    ],
)
def test_merge_album_year(existing, incoming, expected):
    assert merge_album_year(existing, incoming) == expected


@pytest.mark.parametrize(
    ("existing", "incoming", "expected"),
    [
        ("1971-05-02", "2011-05-02", "1971-05-02"),
        ("2005-01-01", "1999-03-01", "1999-03-01"),
        ("1999-03-01", "1999-06-01", "1999-03-01"),
        (None, "1999-03-01", "1999-03-01"),
        ("1999-03-01", None, "1999-03-01"),
        ("1884-01-01", "1975-01-01", "1975-01-01"),
        ("1975-01-01", "1884-01-01", "1975-01-01"),
        ("1884-01-01", "1885-01-01", "1884-01-01"),
        (None, None, None),
    ],
)
def test_merge_release_date(existing, incoming, expected):
    assert merge_release_date(existing, incoming) == expected

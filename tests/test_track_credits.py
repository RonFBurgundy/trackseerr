"""Pure track-credit parsing: MusicBrainz artist-credit and remixers from titles."""

import pytest

from trackseerr.track_credits import build_track_credits, credits_from_mb_artist_credit, remixers_from_title


def _ac(*parts: tuple[str, str, str]) -> list[dict]:
    return [{"name": n, "joinphrase": j, "artist": {"id": i, "name": n}} for n, i, j in parts]


def test_single_artist_credit() -> None:
    out = credits_from_mb_artist_credit(_ac(("Bee Gees", "mb-bg", "")))
    assert out == [
        {"artist_name": "Bee Gees", "artist_mbid": "mb-bg", "role": "primary", "join_phrase": "", "source": "musicbrainz"}
    ]


def test_feat_credit_marks_later_entries_featured() -> None:
    out = credits_from_mb_artist_credit(_ac(("A", "a", " feat. "), ("B", "b", " & "), ("C", "c", "")))
    assert [(c["artist_name"], c["role"]) for c in out] == [("A", "primary"), ("B", "featured"), ("C", "featured")]
    assert out[0]["join_phrase"] == " feat. "


@pytest.mark.parametrize("join", [" & ", " x ", ", ", " vs. "])
def test_plain_joins_stay_primary(join: str) -> None:
    out = credits_from_mb_artist_credit(_ac(("A", "a", join), ("B", "b", "")))
    assert [c["role"] for c in out] == ["primary", "primary"]


def test_ft_and_featuring_join_phrases() -> None:
    for join in (" ft. ", " Featuring "):
        out = credits_from_mb_artist_credit(_ac(("A", "a", join), ("B", "b", "")))
        assert out[1]["role"] == "featured"


def test_invalid_credit_input() -> None:
    assert credits_from_mb_artist_credit(None) == []
    assert credits_from_mb_artist_credit([{"name": ""}, "x"]) == []


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Closer (Adriatique remix)", ["Adriatique"]),
        ("Song (A & B Remix)", ["A", "B"]),
        ("Song (A x B Remix)", ["A", "B"]),
        ("Song (A, B Remix)", ["A", "B"]),
        ("Song [Foo Bootleg]", ["Foo"]),
        ("Song (Foo Flip)", ["Foo"]),
        ("Song (Foo Rework)", ["Foo"]),
        ("Song (Foo Refix)", ["Foo"]),
        ("Song (Foo Re-Edit)", ["Foo"]),
        ("Song (Foo Edit)", ["Foo"]),
        ("Song (Foo Dub)", ["Foo"]),
        ("Song (Tim Fuchs Make Out remix)", ["Tim Fuchs Make Out"]),
        ("Song (Live) (X Remix)", ["X"]),
        ("Song (X Remix) (Live)", []),
        ("Song", []),
        ("Song (Live)", []),
        ("Song (Original Mix)", []),
        ("Song (Extended Mix)", []),
        ("Song (Radio Edit)", []),
        ("Song (Club Mix)", []),
        ("Song (Dub Mix)", []),
        ("Song (Instrumental)", []),
        ("Song (Remastered)", []),
        ("Song (VIP)", []),
        ("Song (Foo VIP Mix)", []),
        ("Song (Remix)", []),
        ("Song (Vocal Mix)", []),
        ("Song (Todd Edwards main vocalized remix)", []),
        ("", []),
    ],
)
def test_remixers_from_title(title: str, expected: list[str]) -> None:
    assert remixers_from_title(title) == expected


def test_build_with_mb_and_title_remixer() -> None:
    out = build_track_credits(_ac(("Orig", "o", "")), "Song (Remi Remix)")
    assert [(c["artist_name"], c["role"], c["source"]) for c in out] == [
        ("Orig", "primary", "musicbrainz"),
        ("Remi", "remixer", "title"),
    ]


def test_build_tag_fallback() -> None:
    out = build_track_credits(None, "Song", tag_artist=" Tagged ", tag_artist_mbid="t-id")
    assert out == [
        {"artist_name": "Tagged", "artist_mbid": "t-id", "role": "primary", "join_phrase": "", "source": "tags"}
    ]


def test_build_skips_remixer_already_credited() -> None:
    out = build_track_credits(_ac(("Remi", "r", "")), "Song (remi Remix)")
    assert len(out) == 1 and out[0]["role"] == "primary"


def test_build_no_credit_at_all() -> None:
    assert build_track_credits(None, "Song") == []
    assert build_track_credits([], "Song", tag_artist="  ") == []

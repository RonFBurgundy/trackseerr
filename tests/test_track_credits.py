"""Pure track-credit parsing: MusicBrainz artist-credit and remixers from titles."""

import pytest

from trackseerr.track_credits import (
    apply_credit_style,
    build_track_credits,
    credits_from_mb_artist_credit,
    naming_credits,
    remixers_from_title,
    tag_artist_from_credits,
)

from tests.test_library_api import test_db  # noqa: F401


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


def test_apply_credit_style_original_joins_primaries() -> None:
    credits = [
        {"artist_name": "Bee Gees", "role": "primary", "join_phrase": " & ", "position": 1},
        {"artist_name": "Yvonne Elliman", "role": "primary", "join_phrase": "", "position": 2},
    ]
    artist, title = apply_credit_style(credits, "Stayin' Alive", "original", "in_title")
    assert artist == "Bee Gees & Yvonne Elliman"
    assert title == "Stayin' Alive"


def test_apply_credit_style_remixer_mode_uses_remixers() -> None:
    credits = [
        {"artist_name": "Original Artist", "role": "primary", "join_phrase": "", "position": 1},
        {"artist_name": "Remix Master", "role": "remixer", "join_phrase": "", "position": 2},
    ]
    artist, title = apply_credit_style(credits, "Song (Remix Master Remix)", "remixer", "in_title")
    assert artist == "Remix Master"
    assert title == "Song (Remix Master Remix)"


def test_apply_credit_style_remixer_mode_without_remixer_falls_back() -> None:
    credits = [
        {"artist_name": "Original Artist", "role": "primary", "join_phrase": "", "position": 1},
    ]
    artist, title = apply_credit_style(credits, "Song", "remixer", "in_title")
    assert artist == "Original Artist"
    assert title == "Song"


def test_apply_credit_style_as_credited_keeps_join_phrases() -> None:
    credits = [
        {"artist_name": "Artist A", "role": "primary", "join_phrase": " feat. ", "position": 1},
        {"artist_name": "Artist B", "role": "featured", "join_phrase": "", "position": 2},
    ]
    artist, title = apply_credit_style(credits, "Song", "as_credited", "drop")
    assert artist == "Artist A feat. Artist B"
    assert title == "Song"


def test_featured_in_title_leaves_title() -> None:
    credits = [
        {"artist_name": "Main Artist", "role": "primary", "join_phrase": " feat. ", "position": 1},
        {"artist_name": "Guest Artist", "role": "featured", "join_phrase": "", "position": 2},
    ]
    artist, title = apply_credit_style(credits, "Hit Song (feat. Guest Artist)", "original", "in_title")
    assert artist == "Main Artist"
    assert title == "Hit Song (feat. Guest Artist)"


def test_featured_in_artist_moves_feat_and_strips_title() -> None:
    credits = [
        {"artist_name": "Main Artist", "role": "primary", "join_phrase": " feat. ", "position": 1},
        {"artist_name": "Guest Artist", "role": "featured", "join_phrase": "", "position": 2},
    ]
    artist, title = apply_credit_style(credits, "Hit Song (feat. Guest Artist)", "original", "in_artist")
    assert artist == "Main Artist feat. Guest Artist"
    assert title == "Hit Song"


def test_featured_drop_strips_title() -> None:
    credits = [
        {"artist_name": "Main Artist", "role": "primary", "join_phrase": " feat. ", "position": 1},
        {"artist_name": "Guest Artist", "role": "featured", "join_phrase": "", "position": 2},
    ]
    artist, title = apply_credit_style(credits, "Hit Song (feat. Guest Artist)", "original", "drop")
    assert artist == "Main Artist"
    assert title == "Hit Song"


def test_no_primary_returns_none() -> None:
    credits = [
        {"artist_name": "Remixer Only", "role": "remixer", "join_phrase": "", "position": 1},
    ]
    artist, title = apply_credit_style(credits, "Some Track", "original", "in_title")
    assert artist is None
    assert title == "Some Track"


def test_tag_artist_from_credits_uses_primary_mbid() -> None:
    credits = [
        {"artist_name": "Bee Gees", "artist_mbid": "bg-mbid", "role": "primary", "position": 1},
        {"artist_name": "Robin Gibb", "artist_mbid": "rg-mbid", "role": "featured", "position": 2},
    ]
    artist, mbid = tag_artist_from_credits(
        credits, "Stayin' Alive", {"remix_credit": "original", "featured_artists": "in_title"}
    )
    assert artist == "Bee Gees"
    assert mbid == "bg-mbid"


def test_tag_artist_from_credits_featured_in_artist_mode() -> None:
    credits = [
        {"artist_name": "Main Artist", "artist_mbid": "main-mbid", "role": "primary", "position": 1},
        {"artist_name": "Guest Artist", "artist_mbid": "guest-mbid", "role": "featured", "position": 2},
    ]
    artist, mbid = tag_artist_from_credits(
        credits, "Song (feat. Guest Artist)", {"featured_artists": "in_artist"}
    )
    assert artist == "Main Artist feat. Guest Artist"
    assert mbid == "main-mbid"


def test_tag_artist_from_credits_empty() -> None:
    assert tag_artist_from_credits([], "Song", {}) == (None, None)
    assert tag_artist_from_credits(None, "Song", {}) == (None, None)
def test_naming_credits_prefers_stored_credits(test_db) -> None:
    test_db.upsert_library_artist({"id": "art-1", "name": "Artist 1", "monitored": True})
    test_db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": "Album 1", "monitored": True})
    test_db.upsert_library_track({"id": "trk-1", "album_id": "alb-1", "artist_id": "art-1", "title": "My Title", "track_number": 1, "disc_number": 1, "monitored": True})
    stored = [
        {"artist_name": "Stored Artist", "artist_mbid": "mbid-stored", "role": "primary", "join_phrase": "", "source": "musicbrainz"}
    ]
    test_db.replace_track_artists("trk-1", stored)
    out = naming_credits(test_db, "trk-1", "My Title", tag_artist="Tag Artist", tag_artist_mbid="mbid-tag")
    assert [(c["artist_name"], c["role"], c["source"]) for c in out] == [("Stored Artist", "primary", "musicbrainz")]


def test_naming_credits_falls_back_to_tag_artist(test_db) -> None:
    out = naming_credits(test_db, "trk-empty", "My Title", tag_artist="Tag Artist", tag_artist_mbid="mbid-tag")
    assert [(c["artist_name"], c["artist_mbid"], c["role"], c["source"]) for c in out] == [
        ("Tag Artist", "mbid-tag", "primary", "tags")
    ]


def test_naming_credits_ignores_various_artists_tag(test_db) -> None:
    for va in ("Various Artists", "Various", "VA", "various artists", "va"):
        assert naming_credits(test_db, "trk-none", "My Title", tag_artist=va) == []


def test_naming_credits_no_track_id_uses_tags(test_db) -> None:
    out = naming_credits(test_db, None, "My Title", tag_artist="Tag Performer", tag_artist_mbid="mb-p")
    assert [(c["artist_name"], c["role"], c["source"]) for c in out] == [
        ("Tag Performer", "primary", "tags")
    ]


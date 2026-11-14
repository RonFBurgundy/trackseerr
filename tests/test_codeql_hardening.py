"""Tests for CodeQL ReDoS and path traversal hardening."""

from pathlib import Path
import time
from unittest.mock import MagicMock

import pytest

from trackseerr.artist_ingest import ingest_artist_native
from trackseerr.lidarr_release import _strip_album_version
from trackseerr.m3u import parse_m3u
from trackseerr.media_match import split_artist_credits
from trackseerr.mediacover import MediaCoverService
from trackseerr.pack_index import (
    _extract_featured,
    _extract_trailing_bpm_key,
    _strip_trailing_bracket_tags,
    clean_pool_title,
    strip_trailing_bpm,
)
from trackseerr.quality import parse_release_title
from trackseerr.search_terms import (
    _version_groups,
    primary_artist,
    strip_featuring,
    title_core,
)
from trackseerr.storage import Database
from trackseerr.storage_common import _has_extra_bracket_suffix
from trackseerr.text_limits import MAX_TITLE_CHARS, cap


def test_cap_truncates_and_handles_none() -> None:
    assert cap(None) == ""
    assert cap("") == ""
    assert cap("short") == "short"
    long_str = "x" * 1000
    assert len(cap(long_str)) == MAX_TITLE_CHARS
    assert cap(long_str, limit=10) == "x" * 10


def test_long_titles_return_quickly() -> None:
    pathological_1 = "(" * 50000 + "a"
    pathological_2 = "a " * 50000 + "feat."

    funcs = [
        lambda s: _strip_album_version(s),
        lambda s: split_artist_credits(s),
        lambda s: strip_trailing_bpm(s),
        lambda s: _extract_trailing_bpm_key(s, {}),
        lambda s: _extract_featured(s, {}),
        lambda s: clean_pool_title(s),
        lambda s: _strip_trailing_bracket_tags(s),
        lambda s: parse_release_title(s),
        lambda s: primary_artist(s),
        lambda s: strip_featuring(s),
        lambda s: _version_groups(s),
        lambda s: title_core(s),
        lambda s: _has_extra_bracket_suffix(s, "normal"),
        lambda s: parse_m3u(f"#EXTINF:123,{s}\npath.mp3"),
    ]

    for f in funcs:
        for text in (pathological_1, pathological_2):
            t0 = time.perf_counter()
            f(text)
            elapsed = time.perf_counter() - t0
            assert elapsed < 1.0


def test_normal_titles_unchanged() -> None:
    title1 = "Summer of 69"
    cleaned, extras = clean_pool_title(title1)
    assert cleaned == "Summer of 69"
    assert extras.get("bpm_stripped") is False

    title2 = "Titanium Ft Sia (Alesso Remix) (Clean) 126"
    cleaned2, extras2 = clean_pool_title(title2)
    assert cleaned2 == "Titanium (Alesso Remix)"
    assert extras2.get("bpm") == 126
    assert extras2.get("edit_kind") == "Clean"
    assert extras2.get("featured") == "Sia"

    parsed = parse_release_title("Artist - Song (2020) [FLAC 24bit]")
    assert parsed.quality == "FLAC 24bit"

    core = title_core("Track (Club Mix) [feat. Singer] - Bonus")
    assert core == "Track"

    feat_stripped = strip_featuring("Song [feat. Singer]")
    assert feat_stripped == "Song"


def test_artwork_path_rejects_traversal(tmp_path: Path) -> None:
    service = MediaCoverService(str(tmp_path))

    with pytest.raises(ValueError, match="Unsafe artwork id"):
        service.get_album_cover_path("../x")

    with pytest.raises(ValueError, match="Unsafe artwork id"):
        service.get_artist_poster_path("foo/bar")

    with pytest.raises(ValueError, match="Unsafe artwork id"):
        service.get_artist_banner_path("foo\\bar")

    with pytest.raises(ValueError, match="Unsafe artwork id"):
        service.get_album_cover_path("foo\x00bar")

    with pytest.raises(ValueError, match="Unsafe artwork id"):
        service.get_album_cover_path("")

    res = service.ensure_artwork("album_cover", "../x", "https://e.com/a.jpg")
    assert res is None


def test_artist_folder_cannot_escape_root(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "test.db"))
    root_dir = tmp_path / "music"
    root_dir.mkdir(parents=True, exist_ok=True)
    db.update_media_management_settings({"root_folder_path": str(root_dir)})

    discovery_client = MagicMock()
    discovery_client.get_artist.return_value = {
        "id": "ext-1",
        "name": "Artist Name",
        "albums": [],
    }
    enricher = MagicMock()

    res = ingest_artist_native(
        db,
        discovery_client,
        enricher,
        foreign_artist_id="ext-1",
        artist_name="../../etc",
        root_folder=str(root_dir),
    )
    folder = Path(res["path"])
    assert folder.resolve().is_relative_to(root_dir.resolve())

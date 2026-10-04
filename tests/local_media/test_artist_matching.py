"""Artist-name normalisation and collaboration-folder handling.

Real folders: 'Daft Punk_ Pharrell Williams' (iTunes turns ':' into '_'), 'Daft Punk feat Pharrell',
'Daft Punk ft Pharrell', 'Daft Punk & Rihanna & Suzzanne Vega & JT', and the truncated
'Get Lucky (Radio Edit) [feat. Pharrell W' album folder. The only normalisation in the code base is
``storage.clean_library_name`` + ``Database.get_library_artist_by_name`` (no feat/ft/& splitting exists).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mutagen import id3

from plex_playlist_sync.library import inspect_audio_file
from plex_playlist_sync.storage import clean_library_name

from .conftest import run_scan, snapshot

pytestmark = pytest.mark.local_media

TAGGED_COLLAB = "Daft Punk_ Pharrell Williams/Random Access Memories"   # TPE1 'Daft Punk; Pharrell Williams', TPE2 'Daft Punk'
FEAT = "Daft Punk feat Pharrell/Unknown Album"                         # no album / album-artist tags
FT = "Daft Punk ft Pharrell/Unknown Album"
AMP = "Daft Punk & Rihanna & Suzzanne Vega & JT/Unknown Album"
TRUNC = "Daft Punk/Get Lucky (Radio Edit) [feat. Pharrell W"           # .m4a, AAC 256, album artist 'Daft Punk'


def _first_audio(media_root: Path, rel_dir: str, suffixes=(".mp3", ".m4a")) -> Path:
    d = media_root / rel_dir
    if not d.is_dir():
        pytest.skip(f"{d} not in this library")
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in suffixes:
            return p
    pytest.skip(f"no audio in {d}")


def _stage(media_root: Path, root: Path, *rel_dirs: str) -> list[Path]:
    out = []
    for rel in rel_dirs:
        src = _first_audio(media_root, rel)
        dst = root / rel / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        out.append(dst)
    return out


# --------------------------------------------------------------------------------------------------
# clean_library_name / get_library_artist_by_name
# --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["daft punk", "DAFT PUNK", "Daft  Punk", " Daft Punk ", "Daft Punk."])
def test_clean_name_collapses_case_spacing_punctuation(variant):
    assert clean_library_name(variant) == clean_library_name("Daft Punk") == "daft punk"


@pytest.mark.parametrize(
    "variant",
    ["Daft Punk feat Pharrell", "Daft Punk ft Pharrell", "Daft Punk & Rihanna & Suzzanne Vega & JT", "Daft Punk; Pharrell Williams"],
)
def test_collaboration_strings_never_collapse_onto_the_primary_artist_key(variant):
    assert clean_library_name(variant) != "daft punk"


def test_lookup_by_name_is_case_and_punctuation_insensitive(db, library_copy):
    run_scan(db, library_copy)
    assert db.get_library_artist_by_name("DAFT PUNK")["name"] == "Daft Punk"
    assert db.get_library_artist_by_name("daft punk.")["name"] == "Daft Punk"
    assert db.get_library_artist_by_name("Daft Punk feat Pharrell") is None


def test_underscore_substitute_matches_punctuation_form():
    assert clean_library_name("Daft Punk_ Pharrell Williams") == clean_library_name("Daft Punk; Pharrell Williams")


# --------------------------------------------------------------------------------------------------
# Scanning the collaboration folders
# --------------------------------------------------------------------------------------------------

@pytest.fixture
def collab_root(media_root, tmp_path):
    root = tmp_path / "music"
    _stage(media_root, root, TAGGED_COLLAB, TRUNC)
    return root


def test_source_tags_are_what_we_think_they_are(media_root):
    meta = inspect_audio_file(_first_audio(media_root, TAGGED_COLLAB))
    assert meta["artist"] == "Daft Punk; Pharrell Williams" and meta["album_artist"] == "Daft Punk"


def test_semicolon_collab_tag_resolves_to_album_artist(db, collab_root):
    run_scan(db, collab_root)
    assert [a["name"] for a in snapshot(db)["artists"]] == ["Daft Punk"]


def test_truncated_album_folder_does_not_truncate_the_album_title(db, collab_root):
    run_scan(db, collab_root)
    titles = [a["title"] for a in snapshot(db)["albums"]]
    assert "Get Lucky (Radio Edit) [feat. Pharrell Williams] - Single" in titles, titles


def test_truncated_folder_m4a_is_scanned_with_aac_quality(db, collab_root):
    run_scan(db, collab_root)
    (f,) = [f for f in snapshot(db)["files"] if f["file_path"].endswith(".m4a")]
    assert f["codec"] == "AAC" and f["quality_name"] == "AAC 256"
    (album,) = [a for a in snapshot(db)["albums"] if a["title"].startswith("Get Lucky")]
    assert album["path"].endswith("[feat. Pharrell W"), "album path is the (truncated) folder actually on disk"


@pytest.fixture
def untagged_root(media_root, tmp_path):
    """Feat / ft / & folders (these files have no album or album-artist tag) plus a fully stripped copy."""
    root = tmp_path / "music"
    _stage(media_root, root, FEAT, FT, AMP)
    return root


def test_feat_and_ft_variants_resolve_to_primary_artist(db, untagged_root):
    run_scan(db, untagged_root)
    names = {a["name"] for a in snapshot(db)["artists"] if "Rihanna" not in a["name"]}
    assert names == {"Daft Punk"}, names


def test_ampersand_group_name_is_kept_verbatim(db, untagged_root):
    run_scan(db, untagged_root)
    names = {a["name"] for a in snapshot(db)["artists"]}
    assert "Daft Punk & Rihanna & Suzzanne Vega & JT" in names


def test_tagless_file_falls_back_to_folder_names(db, tmp_path, media_root):
    root = tmp_path / "music"
    (mp3,) = _stage(media_root, root, TAGGED_COLLAB)[:1]
    id3.ID3(str(mp3)).delete()
    run_scan(db, root)
    snap = snapshot(db)
    assert [a["name"] for a in snap["artists"]] == ["Daft Punk_ Pharrell Williams"]
    assert [a["title"] for a in snap["albums"]] == ["Random Access Memories"]


def test_tagless_file_title_and_track_number_come_from_filename(db, tmp_path, media_root):
    root = tmp_path / "music"
    src = media_root / TAGGED_COLLAB / "08 Get Lucky.mp3"
    if not src.is_file():
        pytest.skip("fixture file missing")
    dst = root / TAGGED_COLLAB / src.name
    dst.parent.mkdir(parents=True)
    shutil.copyfile(src, dst)
    id3.ID3(str(dst)).delete()
    run_scan(db, root)
    (t,) = snapshot(db)["tracks"]
    assert (t["title"], t["track_number"]) == ("Get Lucky", 8)

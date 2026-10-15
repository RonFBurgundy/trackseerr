"""Rename preview reasons: classify_rename, collisions and the default-selection rule."""

import pytest

from trackseerr.naming import build_track_path, existing_artist_folder
from trackseerr.rename_reasons import classify_rename, flag_collisions, is_default_selected

CASES = [
    ("artist_folder", "Mumford & Sons/Babel (2012)/01 - Babel.flac", "Mumford and Sons/Babel (2012)/01 - Babel.flac",
     ["artist_folder"], True),
    ("loose_file", "Lord Huron/02 - Mine Forever.mp3", "Lord Huron/Long Lost (2021)/02 - Mine Forever.mp3",
     ["loose_file"], True),
    ("album_year", "Rod Stewart/Every Picture Tells a Story (1971)/01 - Maggie May.flac",
     "Rod Stewart/Every Picture Tells a Story (1884)/01 - Maggie May.flac", ["album_year"], False),
    ("album_title", "Foo/Bar Baz (2000)/01 - X.flac", "Foo/Bar Qux (2000)/01 - X.flac", ["album_title"], False),
    ("album_folder_format", "Foo/Bar: Baz (2000)/01 - X.flac", "Foo/Bar - Baz (2000)/01 - X.flac",
     ["album_folder_format"], True),
    ("disc_layout_and_number", "Baby Keem/The Melodic Blue (2021)/Disc 01/08 - lost souls.flac",
     "Baby Keem/The Melodic Blue (2021)/Disc 02/01 - lost souls.flac", ["disc_layout", "track_number"], False),
    ("disc_appears", "Foo/Bar (2000)/01 - X.flac", "Foo/Bar (2000)/Disc 01/01 - X.flac", ["disc_layout"], False),
    ("file_name_format", "Sleep/Album (2000)/05 - How Do You Sleep.flac", "Sleep/Album (2000)/05 - How Do You Sleep!.flac",
     ["file_name_format"], True),
    ("disc_and_file_title", "Miami Horror/Illumination (2010)/Disc 01/02 - I Look to You.flac",
     "Miami Horror/Illumination (2010)/Disc 03/02 - I Look to You (Tim Fuchs Make Out remix).flac",
     ["disc_layout", "file_title"], False),
    ("file_title", "Foo/Bar (2000)/01 - Old Name.flac", "Foo/Bar (2000)/01 - New Name.flac", ["file_title"], False),
    ("track_number", "Foo/Bar (2000)/01 - X.flac", "Foo/Bar (2000)/02 - X.flac", ["track_number"], False),
    ("no_change", "Foo/Bar (2000)/01 - X.flac", "Foo/Bar (2000)/01 - X.flac", [], True),
    ("prefixed_number_parsed", "Foo/Bar (2000)/Foo - Bar - 03 - X.flac", "Foo/Bar (2000)/03 - X.flac", [], True),
    ("compilation_link_from_va", "Various Artists/Now 79 (2011)/01 - Song.flac",
     "LMFAO/Now 79 (2011)/01 - Song.flac", ["artist_folder", "compilation_link"], False),
    ("compilation_link_to_va", "Toots & The Maytals/Funky Kingston (1973)/01 - Song.flac",
     "Various Artists/Funky Kingston (1973)/01 - Song.flac", ["artist_folder", "compilation_link"], False),
]


@pytest.mark.parametrize(("name", "current", "proposed", "reasons", "selected"), CASES, ids=[c[0] for c in CASES])
def test_classify_rename(name, current, proposed, reasons, selected):
    got = classify_rename(current, proposed)
    assert got == reasons
    assert is_default_selected(got) is selected


def test_collisions_flag_every_row_sharing_a_destination_and_unselect_it():
    assert flag_collisions(["a/b.flac", "a/c.flac", "a/b.flac"]) == [True, False, True]
    assert is_default_selected(["artist_folder", "collision"]) is False


def test_existing_artist_folder_requires_direct_child_of_root_and_flag_off():
    settings = {"root_folder_path": "/music", "rename_artist_folders": False}
    assert existing_artist_folder({"path": "/music/Mumford & Sons"}, settings) == "Mumford & Sons"
    assert existing_artist_folder({"path": "/music/Mumford & Sons/"}, settings) == "Mumford & Sons"
    assert existing_artist_folder({"path": "/music/A/B"}, settings) is None
    assert existing_artist_folder({"path": "/elsewhere/B"}, settings) is None
    assert existing_artist_folder({"path": None}, settings) is None
    assert existing_artist_folder(None, settings) is None
    assert existing_artist_folder({"path": "/music/B"}, {**settings, "rename_artist_folders": True}) is None


def test_build_track_path_uses_artist_folder_verbatim_but_rejects_unsafe_names():
    settings = {"root_folder_path": "/music", "artist_folder_format": "{Artist CleanName}"}
    meta = {"artist": "Mumford & Sons", "album": "Babel", "title": "Babel", "track_number": 1, "year": 2012,
            "extension": ".flac"}
    assert build_track_path({**meta, "artist_folder": "Mumford & Sons"}, settings).startswith("/music/Mumford & Sons/")
    assert build_track_path(meta, settings).startswith("/music/Mumford and Sons/")
    for bad in ("../evil", "a/b", "a\\b", ".."):
        assert build_track_path({**meta, "artist_folder": bad}, settings).startswith("/music/Mumford and Sons/")


def test_compilation_link_not_added_for_normal_artist_moves():
    got = classify_rename("Crosby, Stills & Nash/Album/01.flac", "Crosby Stills and Nash/Album/01.flac")
    assert got == ["artist_folder"]
    assert is_default_selected(got) is True


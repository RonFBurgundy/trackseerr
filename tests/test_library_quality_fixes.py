"""CI coverage for the library-quality fixes that the opt-in ``tests/local_media`` suite found.

Every test here builds tiny synthetic audio (mutagen-tagged MPEG frames) or exercises a pure function, so the
behaviour is covered without a real music library.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mutagen import id3

from plex_playlist_sync import library as library_mod
from plex_playlist_sync.library import (
    fingerprint_audio_file,
    find_folder_art,
    inspect_audio_file,
    parse_filename_track,
    primary_artist,
    resolve_album_artist,
)
from plex_playlist_sync.library_monitoring import hydrated_track_monitored
from plex_playlist_sync.library_scanner import LibraryScanner
from plex_playlist_sync.m3u import parse_m3u
from plex_playlist_sync.models import LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack
from plex_playlist_sync.naming import sanitize_component
from plex_playlist_sync.quality import parse_release_title
from plex_playlist_sync.storage import Database, clean_library_name

REPO = Path(__file__).resolve().parents[1]

# One MPEG-1 Layer III 128 kbps / 44.1 kHz frame (417 bytes); a dozen of them make a parseable MP3 stream.
_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413
# The same at 320 kbps (1044-byte frames): the quality parser turns "MP3 320kbps" into the catalog name "MP3 320".
_FRAME_320 = b"\xff\xfb\xe0\x00" + b"\x00" * 1040


def make_mp3(path: Path, *, kbps320: bool = False, **frames: str) -> Path:
    """Writes a tiny valid MP3 and tags it, e.g. ``make_mp3(p, TIT2="Song", TPE1="Artist", TRCK="3")``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((_FRAME_320 if kbps320 else _FRAME) * 12)
    tags = id3.ID3()
    for frame_id, value in frames.items():
        tags.add(getattr(id3, frame_id)(encoding=3, text=[value]))
    if frames:
        tags.save(str(path))
    return path


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "quality_fixes.db"))
    yield database
    database.close()


def scan(db: Database, root: Path) -> dict:
    from plex_playlist_sync.artist_refresh_worker import artist_refresh_worker

    with patch.object(artist_refresh_worker, "refresh_once", lambda *a, **k: {}):
        return LibraryScanner().scan(db, root_folder=str(root))


def names(rows: list[dict], key: str) -> list[str]:
    return sorted(r[key] for r in rows)


# ------------------------------------------------------------------ bugs 1+2: artist / album keying


@pytest.mark.parametrize(
    "credit, expected",
    [
        ("Daft Punk / Romanthony", "Daft Punk"),
        ("Artist A / Artist B", "Artist A"),
        ("Daft Punk/Romanthony", "Daft Punk/Romanthony"),
        ("f/x", "f/x"),
        ("Daft Punk; Pharrell Williams", "Daft Punk"),
        ("Daft Punk feat Pharrell", "Daft Punk"),
        ("Daft Punk ft Pharrell", "Daft Punk"),
        ("Daft Punk ft. Pharrell", "Daft Punk"),
        ("Daft Punk featuring Pharrell Williams", "Daft Punk"),
        ("Daft Punk (feat. Pharrell Williams)", "Daft Punk"),
        ("Simon & Garfunkel", "Simon & Garfunkel"),
        ("Daft Punk & Rihanna", "Daft Punk & Rihanna"),
        ("AC/DC", "AC/DC"),
        ("Soft Cell", "Soft Cell"),
        ("", ""),
    ],
)
def test_primary_artist_splits_only_slash_semicolon_and_feat_credits(credit, expected):
    assert primary_artist(credit) == expected


def test_album_artist_wins_over_track_artist_and_falls_back_to_primary_artist():
    assert resolve_album_artist({"artist": "Daft Punk/Romanthony", "album_artist": "Daft Punk"}) == "Daft Punk"
    assert resolve_album_artist({"artist": "Daft Punk / Romanthony", "album_artist": None}) == "Daft Punk"
    assert resolve_album_artist({"artist": None, "album_artist": "Various Artists"}) == "Various Artists"
    assert resolve_album_artist({}, fallback="Unknown Artist") == "Unknown Artist"


def test_bare_slash_splits_only_when_the_left_part_is_a_known_artist():
    lowered = lambda n: n.casefold() in {"daft punk"}  # noqa: E731
    assert primary_artist("Daft Punk/Romanthony", lowered) == "Daft Punk"
    assert primary_artist("Daft Punk/Romanthony", lambda n: False) == "Daft Punk/Romanthony"
    assert primary_artist("Daft Punk/Romanthony") == "Daft Punk/Romanthony"


def test_bare_slash_stays_whole_when_the_whole_string_is_itself_a_known_artist():
    assert primary_artist("AC/DC", lambda n: n.casefold() in {"ac", "ac/dc"}) == "AC/DC"
    assert primary_artist("AC/DC") == "AC/DC"
    assert primary_artist("f/x", lambda n: False) == "f/x"


def test_ampersand_is_never_split_even_with_a_known_artist():
    assert primary_artist("Simon & Garfunkel", lambda n: True) == "Simon & Garfunkel"
    assert resolve_album_artist({"artist": "Simon & Garfunkel"}, known_artist=lambda n: True) == "Simon & Garfunkel"


def test_artists_tag_wins_over_the_artist_string_but_album_artist_wins_over_all():
    meta = {"artist": "Daft Punk / Romanthony", "artists": ["Romanthony", "Daft Punk"]}
    assert resolve_album_artist(meta) == "Romanthony"
    assert resolve_album_artist({**meta, "album_artist": "Various Artists"}) == "Various Artists"


def test_single_musicbrainz_artist_id_keeps_a_bare_slash_whole():
    mbid = "4d3dc8ec-3d17-4a36-a4f7-3f6a3b7f2c11"
    meta = {"artist": "Daft Punk/Romanthony", "musicbrainz_artistid": mbid}
    assert resolve_album_artist(meta, known_artist=lambda n: n == "Daft Punk") == "Daft Punk/Romanthony"
    two = {"artist": "Daft Punk/Romanthony", "musicbrainz_artistid": f"{mbid}; 9d3dc8ec-3d17-4a36-a4f7-3f6a3b7f2c12"}
    assert resolve_album_artist(two, known_artist=lambda n: n == "Daft Punk") == "Daft Punk"


def test_inspect_audio_file_reads_the_multi_valued_artists_tag(tmp_path):
    path = make_mp3(tmp_path / "a.mp3", TIT2="X", TPE1="Daft Punk/Romanthony")
    tags = id3.ID3(str(path))
    tags.add(id3.TXXX(encoding=3, desc="ARTISTS", text=["Daft Punk", "Romanthony"]))
    tags.save(str(path))
    meta = inspect_audio_file(path)
    assert meta["artists"] == ["Daft Punk", "Romanthony"]
    assert resolve_album_artist(meta) == "Daft Punk"


def test_scan_keeps_ac_dc_whole_and_splits_a_bare_slash_credit_of_a_known_artist(db, tmp_path):
    root = tmp_path / "music"
    make_mp3(root / "AC_DC" / "Back" / "01.mp3", TIT2="Hells Bells", TPE1="AC/DC", TALB="Back in Black", TRCK="1")
    make_mp3(root / "DP" / "Disc" / "00.mp3", TIT2="Aerodynamic", TPE1="Daft Punk", TALB="Discovery", TRCK="0")
    make_mp3(root / "DP" / "Disc" / "01.mp3", TIT2="One More Time", TPE1="Daft Punk/Romanthony", TALB="Discovery", TRCK="1")
    make_mp3(root / "X" / "Y" / "01.mp3", TIT2="Song", TPE1="Nobody/Else", TALB="Y", TRCK="1")
    scan(db, root)
    assert names(db.list_library_artists(limit=100), "name") == ["AC/DC", "Daft Punk", "Nobody/Else"]


def test_combined_artist_tags_collapse_into_one_artist_and_one_album(db, tmp_path):
    album = tmp_path / "music" / "Daft Punk" / "Discovery"
    make_mp3(album / "00.mp3", TIT2="Aerodynamic", TPE1="Daft Punk", TALB="Discovery", TRCK="2")
    make_mp3(album / "01.mp3", TIT2="One More Time", TPE1="Daft Punk/Romanthony", TALB="Discovery", TRCK="1")
    make_mp3(album / "02.mp3", TIT2="Face to Face", TPE1="Daft Punk/Todd Edwards", TALB="Discovery", TRCK="13")
    make_mp3(album / "03.mp3", TIT2="Get Lucky", TPE1="Daft Punk; Pharrell Williams", TALB="Discovery", TRCK="3")
    scan(db, tmp_path / "music")
    assert names(db.list_library_artists(limit=100), "name") == ["Daft Punk"]
    assert names(db.list_library_albums(limit=100), "title") == ["Discovery"]
    assert len(db.list_library_tracks(limit=100)) == 4


def test_album_artist_tag_keys_artist_and_compilation_stays_one_album(db, tmp_path):
    album = tmp_path / "music" / "Various Artists" / "Now 1"
    make_mp3(album / "01.mp3", TIT2="A", TPE1="Alpha", TPE2="Various Artists", TALB="Now 1", TRCK="1")
    make_mp3(album / "02.mp3", TIT2="B", TPE1="Beta/Gamma", TPE2="Various Artists", TALB="Now 1", TRCK="2")
    scan(db, tmp_path / "music")
    assert names(db.list_library_artists(limit=100), "name") == ["Various Artists"]
    assert names(db.list_library_albums(limit=100), "title") == ["Now 1"]


def test_feat_ft_folder_fallbacks_resolve_to_the_primary_artist(db, tmp_path):
    root = tmp_path / "music"
    make_mp3(root / "A" / "Alb" / "01 X.mp3", TIT2="X", TPE1="Daft Punk feat Pharrell", TALB="Alb")
    make_mp3(root / "B" / "Alb" / "01 Y.mp3", TIT2="Y", TPE1="Daft Punk ft Pharrell", TALB="Alb")
    scan(db, root)
    assert names(db.list_library_artists(limit=100), "name") == ["Daft Punk"]


# ------------------------------------------------------------------ bugs 3+17: untagged track numbers


def test_missing_track_number_is_not_defaulted(tmp_path):
    f = make_mp3(tmp_path / "a.mp3", TIT2="Around The World", TPE1="Daft Punk")
    assert inspect_audio_file(f)["track_number"] is None
    g = make_mp3(tmp_path / "b.mp3", TIT2="One", TPE1="Daft Punk", TRCK="4/9")
    assert inspect_audio_file(g)["track_number"] == 4


def test_untagged_number_does_not_merge_into_track_one(db, tmp_path):
    album = tmp_path / "music" / "Daft Punk" / "Discovery"
    make_mp3(album / "01 One More Time.mp3", TIT2="One More Time", TPE1="Daft Punk", TALB="Discovery", TRCK="1")
    make_mp3(album / "Around The World.mp3", TIT2="Around The World", TPE1="Daft Punk", TALB="Discovery")
    scan(db, tmp_path / "music")
    assert names(db.list_library_tracks(limit=100), "title") == ["Around The World", "One More Time"]


def _seed_album(db: Database) -> str:
    db.upsert_library_artist(LibraryArtist(id="ar", name="Daft Punk", monitored=True))
    db.upsert_library_album(LibraryAlbum(id="al", artist_id="ar", title="Discovery", monitored=True))
    return "al"


def _seed_track(db: Database, track_id: str, title: str, number: int) -> None:
    db.upsert_library_track(
        LibraryTrack(id=track_id, album_id="al", artist_id="ar", title=title, track_number=number)
    )


def test_track_lookup_matches_title_first_and_never_by_number_alone(db):
    album_id = _seed_album(db)
    _seed_track(db, "t1", "One More Time", 1)
    # A different title that merely shares the number is a different track.
    assert db.get_library_track_by_title(album_id, "Around The World", track_number=1) is None
    assert db.get_library_track_by_title(album_id, "Around The World") is None
    # Title wins even when the number disagrees.
    assert db.get_library_track_by_title(album_id, "One More Time", track_number=9)["id"] == "t1"


def test_track_lookup_number_is_a_tiebreaker_for_near_equal_titles(db):
    album_id = _seed_album(db)
    _seed_track(db, "t6", "Night Vision", 6)
    assert db.get_library_track_by_title(album_id, "Night Vision (Radio Edit)", track_number=6)["id"] == "t6"
    assert db.get_library_track_by_title(album_id, "Night Vision (Radio Edit)") is None


def test_track_lookup_titles_differing_only_in_digits_stay_separate(db):
    album_id = _seed_album(db)
    _seed_track(db, "i2", "Intro 2", 1)
    assert db.get_library_track_by_title(album_id, "Intro 3", track_number=1) is None


def test_track_lookup_is_space_insensitive_without_a_track_number(db):
    album_id = _seed_album(db)
    _seed_track(db, "t6", "Night Vision", 6)
    assert db.get_library_track_by_title(album_id, "Nightvision")["id"] == "t6"
    assert db.get_library_track_by_title(album_id, "Nightvision", track_number=6)["id"] == "t6"
    assert db.get_library_track_by_title(album_id, "Night  Vision")["id"] == "t6"


def test_two_spellings_of_one_track_scan_into_one_track_with_two_files(db, tmp_path):
    album = tmp_path / "music" / "Daft Punk" / "Discovery"
    make_mp3(album / "06 Night Vision.mp3", TIT2="Night Vision", TPE1="Daft Punk", TALB="Discovery", TRCK="6")
    make_mp3(album / "06 Nightvision.mp3", TIT2="Nightvision", TPE1="Daft Punk", TALB="Discovery", TRCK="6")
    scan(db, tmp_path / "music")
    assert len(db.list_library_tracks(limit=100)) == 1
    assert len(db.list_library_files(limit=100)) == 2


# ------------------------------------------------------------------ bug 10: tagless filenames


@pytest.mark.parametrize(
    "stem, title, number",
    [
        ("08 Get Lucky", "Get Lucky", 8),
        ("08 - Get Lucky", "Get Lucky", 8),
        ("08. Get Lucky", "Get Lucky", 8),
        ("08-Get Lucky", "Get Lucky", 8),
        ("Get Lucky", "Get Lucky", None),
        ("1999", "1999", None),
        ("2112 Overture", "2112 Overture", None),
        ("", "", None),
    ],
)
def test_parse_filename_track(stem, title, number):
    assert parse_filename_track(stem) == (title, number)


def test_tagless_file_takes_title_and_number_from_its_name(db, tmp_path):
    make_mp3(tmp_path / "music" / "Daft Punk" / "RAM" / "08 Get Lucky.mp3")  # no tags at all
    scan(db, tmp_path / "music")
    (track,) = db.list_library_tracks(limit=10)
    assert (track["title"], track["track_number"]) == ("Get Lucky", 8)


# ------------------------------------------------------------------ bug 7 / 9: names


def test_sanitize_component_keeps_words_apart_across_a_slash():
    assert sanitize_component("Daft Punk/Romanthony") == "Daft Punk-Romanthony"
    assert sanitize_component("AC/DC") == "AC-DC"
    assert "/" not in sanitize_component("a / b")


def test_clean_library_name_treats_underscore_as_whitespace():
    assert clean_library_name("Daft Punk_ Pharrell Williams") == clean_library_name("Daft Punk; Pharrell Williams")
    assert clean_library_name("Daft Punk_ Pharrell Williams") == "daft punk pharrell williams"
    assert clean_library_name("snake_case") == "snake case"


def test_migration_v39_recomputes_underscore_keys_and_is_idempotent(tmp_path):
    path = str(tmp_path / "m39.db")
    database = Database(path)
    database.upsert_library_artist(LibraryArtist(id="ar", name="Daft Punk; Pharrell Williams", monitored=True))
    database.upsert_library_album(LibraryAlbum(id="al", artist_id="ar", title="Get_Lucky", monitored=True))
    database.upsert_library_track(LibraryTrack(id="t", album_id="al", artist_id="ar", title="Get_Lucky"))
    # Simulate rows written before the fix: stale underscore keys and a v38 schema.
    database.conn.execute("UPDATE library_artists SET clean_name = 'daft punk_ pharrell williams'")
    database.conn.execute("UPDATE library_albums SET clean_title = 'get_lucky', search_clean = 'get_lucky'")
    database.conn.execute("UPDATE library_tracks SET clean_title = 'get_lucky', search_clean = 'get_lucky'")
    database.conn.execute("DELETE FROM schema_migrations WHERE version >= 39")
    database.conn.commit()
    database.close()

    for _ in range(2):  # the second open runs v39 again over already-clean rows
        database = Database(path)
        try:
            assert database.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 39
            assert database.get_library_artist("ar")["clean_name"] == "daft punk pharrell williams"
            assert database.get_library_album("al")["clean_title"] == "get lucky"
            assert database.get_library_track("t")["clean_title"] == "get lucky"
            assert database.conn.execute("SELECT search_clean FROM library_tracks").fetchone()[0] == "get lucky"
            assert database.get_library_artist_by_name("Daft Punk_ Pharrell Williams")["id"] == "ar"
            database.conn.execute("DELETE FROM schema_migrations WHERE version = 39")
            database.conn.commit()
        finally:
            database.close()
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM library_artists").fetchone()[0] == 1


# ------------------------------------------------------------------ bug 11: folder art


@pytest.mark.parametrize(
    "files, expected",
    [
        (["AlbumArt_{E0B5F6EB-9E7A-4290-A301-FD4994D78C20}_Large.jpg"], "AlbumArt_{E0B5F6EB-9E7A-4290-A301-FD4994D78C20}_Large.jpg"),
        (["AlbumArtSmall.jpg", "AlbumArt_{E0B5F6EB-9E7A-4290-A301-FD4994D78C20}_Large.jpg"], "AlbumArt_{E0B5F6EB-9E7A-4290-A301-FD4994D78C20}_Large.jpg"),
        (["AlbumArt_{E0B5F6EB-9E7A-4290-A301-FD4994D78C20}_Small.jpg", "AlbumArt_{E0B5F6EB-9E7A-4290-A301-FD4994D78C20}_Large.jpg"], "AlbumArt_{E0B5F6EB-9E7A-4290-A301-FD4994D78C20}_Large.jpg"),
        (["AlbumArtSmall.jpg"], "AlbumArtSmall.jpg"),
        (["Folder.jpg"], "Folder.jpg"),
        (["FOLDER.JPG", "AlbumArtSmall.jpg"], "FOLDER.JPG"),
        (["Cover.PNG", "Folder.jpg"], "Cover.PNG"),
    ],
)
def test_find_folder_art_recognises_itunes_and_wmp_names(tmp_path, files, expected):
    for name in files:
        (tmp_path / name).write_bytes(b"\xff\xd8\xff\xd9")
    found = find_folder_art(tmp_path)
    assert found is not None and found.name == expected


def test_find_folder_art_ignores_unrelated_files_and_missing_dirs(tmp_path):
    (tmp_path / "notes.jpg").write_bytes(b"x")
    (tmp_path / "AlbumArt.txt").write_bytes(b"x")
    assert find_folder_art(tmp_path) is None
    assert find_folder_art(tmp_path / "nope") is None


def test_scanner_marks_itunes_albumart_as_album_cover(db, tmp_path):
    album = tmp_path / "music" / "Daft Punk" / "Discovery"
    make_mp3(album / "02 Aerodynamic.mp3", TIT2="Aerodynamic", TPE1="Daft Punk", TALB="Discovery", TRCK="2")
    (album / "AlbumArt_{E0B5F6EB-9E7A-4290-A301-FD4994D78C20}_Large.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    scan(db, tmp_path / "music")
    (row,) = db.list_library_albums(limit=10)
    assert row["cover_url"]


# ------------------------------------------------------------------ bug 14: fingerprinting


def test_acoustid_and_chromaprint_are_declared_for_the_shipped_image():
    reqs = (REPO / "requirements.txt").read_text().lower()
    docker = (REPO / "Dockerfile").read_text().lower()
    assert re.search(r"^pyacoustid\b", reqs, re.MULTILINE)
    assert "libchromaprint-tools" in docker
    assert "--no-install-recommends" in docker and "rm -rf /var/lib/apt/lists/*" in docker


@pytest.fixture
def fresh_fp_warning():
    library_mod._fingerprint_warned = False
    yield
    library_mod._fingerprint_warned = False


def test_missing_pyacoustid_warns_once_instead_of_failing_silently(tmp_path, caplog, monkeypatch, fresh_fp_warning):
    audio = make_mp3(tmp_path / "a.mp3", TIT2="A")
    monkeypatch.setitem(sys.modules, "acoustid", None)  # makes ``import acoustid`` raise ImportError
    with caplog.at_level(logging.WARNING, logger=library_mod.logger.name):
        assert fingerprint_audio_file(audio, api_key="k") is None
        assert fingerprint_audio_file(audio, api_key="k") is None
    warnings = [r for r in caplog.records if "fingerprinting is unavailable" in r.getMessage()]
    assert len(warnings) == 1 and "pyacoustid" in warnings[0].getMessage()


def test_missing_fpcalc_backend_warns_once(tmp_path, caplog, monkeypatch, fresh_fp_warning):
    audio = make_mp3(tmp_path / "a.mp3", TIT2="A")
    fake = types.ModuleType("acoustid")

    class NoBackendError(Exception):
        pass

    def match(key, path, **kwargs):
        raise NoBackendError("fpcalc not found")

    fake.match = match
    monkeypatch.setitem(sys.modules, "acoustid", fake)
    with caplog.at_level(logging.WARNING, logger=library_mod.logger.name):
        assert fingerprint_audio_file(audio, api_key="k") is None
        assert fingerprint_audio_file(audio, api_key="k") is None
    assert len([r for r in caplog.records if "fingerprinting is unavailable" in r.getMessage()]) == 1


def test_fingerprinting_forces_the_fpcalc_backend(tmp_path, monkeypatch, fresh_fp_warning):
    audio = make_mp3(tmp_path / "a.mp3", TIT2="A")
    fake = types.ModuleType("acoustid")
    seen: dict = {}

    def match(key, path, **kwargs):
        seen.update(kwargs)
        yield (0.9, "rec-1", "A", "B")

    fake.match = match
    monkeypatch.setitem(sys.modules, "acoustid", fake)
    out = fingerprint_audio_file(audio, api_key="k")
    assert out and out["recording_id"] == "rec-1"
    assert seen == {"force_fpcalc": True}


# ------------------------------------------------------------------ bug 15: AAC classification


@pytest.mark.parametrize(
    "text, expected",
    [
        ("AAC 128kbps", "Unknown"),
        ("AAC 192kbps", "Unknown"),
        ("M4A 160kbps", "Unknown"),
        ("AAC 256kbps", "AAC 256"),
        ("AAC 320kbps", "AAC 256"),
        ("AAC", "AAC 256"),  # no bitrate stated: nothing to downgrade on
        ("Artist - Album [M4A]", "AAC 256"),
        ("Artist - Album [FLAC] m4a", "FLAC 16bit"),
        ("MP3 320kbps", "MP3 320"),
    ],
)
def test_aac_is_classified_by_bitrate(text, expected):
    assert parse_release_title(text).quality == expected


def test_low_bitrate_aac_does_not_meet_an_aac_256_cutoff():
    from plex_playlist_sync.models import QualityProfile, QualityProfileItem
    from plex_playlist_sync.quality import evaluate_release

    profile = QualityProfile(
        id="p",
        name="aac",
        cutoff="AAC 256",
        items=[QualityProfileItem(quality="AAC 256", allowed=True, weight=700)],
    )
    assert evaluate_release(parse_release_title("AAC 256kbps"), profile).meets_cutoff
    assert not evaluate_release(parse_release_title("AAC 128kbps"), profile).meets_cutoff


# ------------------------------------------------------------------ bug 16: iTunes EXTINF orientation


ITUNES = (
    "#EXTM3U\r\n"
    "#EXTINF:210,The Crash - Kavinsky\r\n"
    "C:\\Users\\me\\Music\\iTunes\\iTunes Media\\Music\\Kavinsky\\Teddy Boy EP\\01 The Crash.mp3\r\n"
)


def test_itunes_title_dash_artist_is_oriented_by_the_artist_folder():
    (t,) = parse_m3u(ITUNES)
    assert (t["artist"], t["title"], t["album"]) == ("Kavinsky", "The Crash", "")
    assert "alt" not in t


def test_artist_dash_title_is_still_read_as_written_when_the_folder_agrees():
    content = "#EXTINF:211,Daft Punk - Aerodynamic\n../music/Daft Punk/Discovery/02 Aerodynamic.mp3\n"
    (t,) = parse_m3u(content)
    assert (t["artist"], t["title"]) == ("Daft Punk", "Aerodynamic")
    assert "alt" not in t


def test_file_name_decides_orientation_when_there_is_no_artist_folder():
    (t,) = parse_m3u("#EXTINF:1,The Crash - Kavinsky\n01 The Crash.mp3\n")
    assert (t["artist"], t["title"]) == ("Kavinsky", "The Crash")


def test_ambiguous_display_name_keeps_artist_first_and_offers_the_swapped_reading():
    (t,) = parse_m3u("#EXTINF:1,Foo - Bar\nsomewhere/else/Unrelated.mp3\n")
    assert (t["artist"], t["title"]) == ("Foo", "Bar")
    assert t["alt"] == {"artist": "Bar", "title": "Foo"}


def test_explicit_artist_attribute_is_never_reoriented():
    (t,) = parse_m3u('#EXTINF:1 artist="Kavinsky",The Crash - Kavinsky\nKavinsky/Teddy Boy EP/01 The Crash.mp3\n')
    assert t["artist"] == "Kavinsky"


def test_consecutive_extinf_entries_do_not_leak_orientation_state():
    content = (
        "#EXTINF:1,The Crash - Kavinsky\nKavinsky/Teddy Boy EP/01 The Crash.mp3\n"
        "#EXTINF:1,Daft Punk - Aerodynamic\nDaft Punk/Discovery/02 Aerodynamic.mp3\n"
    )
    first, second = parse_m3u(content)
    assert (first["artist"], second["artist"]) == ("Kavinsky", "Daft Punk")


@pytest.fixture
def api(db, tmp_path: Path):
    from fastapi.testclient import TestClient

    from plex_playlist_sync.api.app import create_app
    from plex_playlist_sync.api.dependencies import get_config, get_db, get_plex_client
    from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
    from plex_playlist_sync.config import Config

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    config = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(cfg_dir))
    admin = db.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_plex_client] = lambda: MagicMock()
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=admin["id"], username="admin_user", is_admin=True, secret_key=secret)
    db.create_session(token, admin["id"], {"auth": "test"})
    client, headers = TestClient(app), {"Authorization": f"Bearer {token}"}
    return types.SimpleNamespace(
        db=db,
        post=lambda path, json=None: client.post(path, json=json, headers=headers),
    )


def test_m3u_import_swaps_to_the_reading_whose_artist_the_library_knows(api):
    api.db.upsert_library_artist(LibraryArtist(id="kv", name="Kavinsky", monitored=True))
    content = "#EXTINF:1,The Crash - Kavinsky\nsomewhere/else/Unrelated.mp3\n"
    with patch("plex_playlist_sync.api.routes.playlists.import_playlist_tracks") as imp:
        imp.return_value = {"status": "imported"}
        resp = api.post("/api/playlists/import/m3u", {"content": content})
    assert resp.status_code == 201, resp.text
    (item,) = imp.call_args.kwargs["req"].tracks
    assert (item.artist, item.title) == ("Kavinsky", "The Crash")


# ------------------------------------------------------------------ bugs 4,5,6,8: manual import


@pytest.fixture
def env(api, tmp_path):
    root, staging = tmp_path / "library", tmp_path / "staging"
    root.mkdir()
    staging.mkdir()
    api.db.update_media_management_settings({"root_folder_path": str(root), "staging_folder_path": str(staging)})
    api.root, api.staging = root, staging
    return api


def _commit(env, src: Path, **kw) -> dict:
    item = {"source_path": str(src), "album_title": "Discovery", "year": 2001, **kw}
    body = env.post("/api/library/manual-import/commit", {"items": [item]}).json()
    assert body["failed_count"] == 0, body
    return body["results"][0]


def test_manual_scan_proposes_the_album_artist_not_the_raw_track_artist(env):
    env.db.upsert_library_artist(LibraryArtist(id="dp", name="Daft Punk", monitored=True))
    make_mp3(env.staging / "a.mp3", TIT2="One More Time", TPE1="Daft Punk/Romanthony", TALB="Discovery", TRCK="1")
    make_mp3(
        env.staging / "b.mp3",
        TIT2="Face to Face",
        TPE1="Daft Punk/Todd Edwards",
        TPE2="Daft Punk",
        TALB="Discovery",
        TRCK="13",
    )
    cands = env.post("/api/library/manual-import/scan", {"folder_path": str(env.staging)}).json()
    assert {c["matched_artist_name"] for c in cands} == {"Daft Punk"}


def test_commit_files_a_combined_artist_tag_under_the_primary_artist(env):
    env.db.upsert_library_artist(LibraryArtist(id="dp", name="Daft Punk", monitored=True))
    src = make_mp3(env.staging / "a.mp3", TIT2="One More Time", TPE1="Daft Punk/Romanthony", TALB="Discovery", TRCK="1")
    result = _commit(env, src, write_tags=False)
    dest = Path(result["destination_path"])
    assert dest.relative_to(env.root).parts[0] == "Daft Punk"
    assert names(env.db.list_library_artists(limit=10), "name") == ["Daft Punk"]


def test_commit_without_track_number_uses_the_files_tag(env):
    src = make_mp3(env.staging / "x.mp3", TIT2="Aerodynamic", TPE1="Daft Punk", TALB="Discovery", TRCK="2")
    dest = Path(_commit(env, src, write_tags=False)["destination_path"])
    assert dest.name.startswith("02 - Aerodynamic")
    assert env.db.list_library_tracks(limit=10)[0]["track_number"] == 2


def test_explicit_track_number_still_overrides_the_tag(env):
    src = make_mp3(env.staging / "x.mp3", TIT2="Aerodynamic", TPE1="Daft Punk", TALB="Discovery", TRCK="2")
    dest = Path(_commit(env, src, write_tags=False, track_number=7)["destination_path"])
    assert dest.name.startswith("07 - Aerodynamic")


def test_commit_uses_filename_number_for_an_untagged_file(env):
    src = make_mp3(env.staging / "08 Get Lucky.mp3")
    dest = Path(_commit(env, src, write_tags=False, artist_name="Daft Punk")["destination_path"])
    assert dest.name.startswith("08 - Get Lucky")


@pytest.mark.parametrize("setting, rewritten", [(False, False), (True, True)])
def test_commit_defaults_write_tags_to_the_global_setting(env, setting, rewritten):
    env.db.update_media_management_settings({"write_audio_tags": setting})
    src = make_mp3(env.staging / "a.mp3", TIT2="One More Time", TPE1="Daft Punk/Romanthony", TALB="Discovery", TRCK="1")
    dest = Path(_commit(env, src, artist_name="Daft Punk")["destination_path"])
    artist_tag = inspect_audio_file(dest)["artist"]
    assert (artist_tag == "Daft Punk") is rewritten
    if not rewritten:
        assert artist_tag == "Daft Punk/Romanthony"


def test_explicit_write_tags_overrides_the_global_setting(env):
    env.db.update_media_management_settings({"write_audio_tags": False})
    src = make_mp3(env.staging / "a.mp3", TIT2="One More Time", TPE1="Daft Punk/Romanthony", TALB="Discovery", TRCK="1")
    dest = Path(_commit(env, src, artist_name="Daft Punk", write_tags=True)["destination_path"])
    assert inspect_audio_file(dest)["artist"] == "Daft Punk"


def test_freshly_imported_file_needs_no_rename(env):
    src = make_mp3(env.staging / "x.mp3", kbps320=True, TIT2="Aerodynamic", TPE1="Daft Punk", TALB="Discovery", TRCK="2")
    assert inspect_audio_file(src)["quality_full"] == "MP3 320kbps"  # what the tag reader reports
    dest = Path(_commit(env, src, write_tags=False)["destination_path"])
    # {Quality Full} is rendered from the catalog name, exactly as rename preview/apply do.
    assert dest.name == "02 - Aerodynamic (MP3 320).mp3"
    (row,) = env.post("/api/library/rename/preview", {}).json()
    assert row["needs_rename"] is False


# ------------------------------------------------------------------ bug 13: hydration under "existing"


def test_hydrated_track_monitored_rule():
    assert hydrated_track_monitored("existing") is False
    for option in ("all", "albums", "singles_eps", "future", "none", None):
        assert hydrated_track_monitored(option) is True


def _refresh(db: Database, option: str) -> list[dict]:
    from plex_playlist_sync.api.routes.library import refresh_single_artist
    from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
    from plex_playlist_sync.mediacover import mediacover_service

    db.upsert_library_artist(LibraryArtist(id="ar", name="Daft Punk", mbid="mb-dp", monitored=True, monitor_option=option))
    db.upsert_library_album(
        LibraryAlbum(id="al", artist_id="ar", title="Discovery", monitored=True, mb_release_group_id="rg-1")
    )
    db.upsert_library_track(
        LibraryTrack(id="owned", album_id="al", artist_id="ar", title="Aerodynamic", track_number=2, monitored=True)
    )
    db.upsert_library_file(
        LibraryFile(
            id="f1", track_id="owned", file_path="/music/a.mp3", relative_path="a.mp3", codec="MP3", quality_name="MP3 320"
        )
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_artist_details.return_value = {"id": "mb-dp"}
    enricher.get_artist_discography.return_value = [
        {"id": "rg-1", "title": "Discovery", "album_type": "album", "year": 2001}
    ]
    enricher.get_release_group_tracks.return_value = [
        {"track_number": 1, "disc_number": 1, "title": "One More Time", "mb_recording_id": "r1"},
        {"track_number": 2, "disc_number": 1, "title": "Aerodynamic", "mb_recording_id": "r2"},
        {"track_number": 3, "disc_number": 1, "title": "Digital Love", "mb_recording_id": "r3"},
    ]
    with patch.object(mediacover_service, "ensure_artwork", return_value=None):
        assert refresh_single_artist(artist_id="ar", db=db, enricher=enricher)["success"] is True
    return db.list_library_tracks(album_id="al", limit=50)


def test_refresh_under_existing_leaves_hydrated_missing_tracks_unmonitored(db):
    tracks = {t["title"]: bool(t["monitored"]) for t in _refresh(db, "existing")}
    assert tracks == {"Aerodynamic": True, "One More Time": False, "Digital Love": False}


def test_refresh_under_all_still_monitors_hydrated_tracks(db):
    tracks = {t["title"]: bool(t["monitored"]) for t in _refresh(db, "all")}
    assert tracks == {"Aerodynamic": True, "One More Time": True, "Digital Love": True}

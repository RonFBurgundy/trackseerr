"""M3U import: playlists that reference the copied Discovery files by absolute path, relative path and EXTINF.

The importer is a pure text parser (m3u.py): it never opens the referenced files, so artist/album/title come
from path segments or EXTINF text only. We therefore also check that what it extracts resolves against a
library scanned from the same files.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from trackseerr.m3u import parse_m3u

from .conftest import audio_files, copy_media, run_scan, set_id3

pytestmark = pytest.mark.local_media

NAMES = ["02 Aerodynamic.mp3", "13 Face to Face.mp3", "06 Night Vision.mp3"]


@pytest.fixture
def lib(tmp_path, copy_discovery):
    root = tmp_path / "music"
    for p in copy_discovery(NAMES, root / "Daft Punk" / "Discovery"):
        # Normalise the combined artist tag ("Daft Punk/Todd Edwards") so this file isolates M3U behaviour from
        # the artist-split scanner bug covered in test_scan.py.
        set_id3(p, TPE1="Daft Punk")
    return root


def _abs_m3u(root: Path) -> str:
    return "#EXTM3U\n" + "\n".join(str(p) for p in audio_files(root)) + "\n"


def test_absolute_paths_parse_to_artist_album_title(lib):
    tracks = parse_m3u(_abs_m3u(lib))
    assert sorted(t["title"] for t in tracks) == ["Aerodynamic", "Face to Face", "Night Vision"]
    assert {(t["artist"], t["album"]) for t in tracks} == {("Daft Punk", "Discovery")}


def test_relative_paths_and_windows_separators(lib):
    content = "Daft Punk/Discovery/02 Aerodynamic.mp3\r\nDaft Punk\\Discovery\\13 Face to Face.mp3\r\n"
    tracks = parse_m3u(content)
    assert [(t["artist"], t["album"], t["title"]) for t in tracks] == [
        ("Daft Punk", "Discovery", "Aerodynamic"),
        ("Daft Punk", "Discovery", "Face to Face"),
    ]


def test_extinf_takes_precedence_over_path(lib):
    content = "#EXTM3U\n#EXTINF:211,Daft Punk - Aerodynamic\n../music/Daft Punk/Discovery/02 Aerodynamic.mp3\n"
    (t,) = parse_m3u(content)
    assert (t["artist"], t["title"]) == ("Daft Punk", "Aerodynamic")


def test_parsed_entries_resolve_against_scanned_library(db, lib):
    run_scan(db, lib)
    for t in parse_m3u(_abs_m3u(lib)):
        artist = db.get_library_artist_by_name(t["artist"])
        assert artist, t
        album = db.get_library_album_by_title(artist["id"], t["album"])
        assert album, t
        assert db.get_library_track_by_title(album["id"], t["title"]), t


def test_m3u_title_variant_resolves_to_existing_track(db, lib):
    run_scan(db, lib)
    artist = db.get_library_artist_by_name("Daft Punk")
    album = db.get_library_album_by_title(artist["id"], "Discovery")
    (t,) = parse_m3u("Daft Punk/Discovery/06 Nightvision.mp3")
    assert db.get_library_track_by_title(album["id"], t["title"]) is not None


def test_import_route_passes_parsed_tracks_through(api, lib):
    captured = {}

    def fake_import(req, **kwargs):
        captured["req"] = req
        return {"name": req.name, "service": req.service, "track_count": len(req.tracks)}

    with patch("trackseerr.api.routes.playlists.import_playlist_tracks", side_effect=fake_import):
        resp = api.post(
            "/api/playlists/import/m3u",
            {"name": "Discovery mix", "content": _abs_m3u(lib), "targets": ["admin-1"]},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["track_count"] == 3
    req = captured["req"]
    assert req.service == "m3u"
    assert sorted(t.title for t in req.tracks) == ["Aerodynamic", "Face to Face", "Night Vision"]
    assert {t.artist for t in req.tracks} == {"Daft Punk"}


def test_m3u_with_only_comments_is_rejected(api):
    resp = api.post("/api/playlists/import/m3u", {"name": "x", "content": "#EXTM3U\n# nothing here\n", "targets": []})
    assert resp.status_code == 400


# --------------------------------------------------------------------------------------------------
# Real iTunes playlist: /iTunes/Playlists/Kavinsky.m3u (CRLF, Windows drive-letter paths, 'Title - Artist' EXTINF)
# --------------------------------------------------------------------------------------------------

PLAYLIST = "Kavinsky.m3u"
WIN_PREFIX = "Music\\"
# Smallest referenced files, copied for the library side of the test.
COPIED = [
    "Kavinsky/1986/03 Flashback.mp3",
    "Kavinsky/Teddy Boy EP/03 Transistor.mp3",
    "Kavinsky/Teddy Boy EP/04 The Crash.mp3",
    "Kavinsky/Outrun/01 Prélude.mp3",
    "Kavinsky/Outrun/05 Rampage.mp3",
]


@pytest.fixture(scope="module")
def real_m3u(media_root) -> str:
    p = media_root.parents[1] / "Playlists" / PLAYLIST
    if not p.is_file():
        pytest.skip(f"{p} not found")
    return p.read_bytes().decode("utf-8", errors="replace")


def _file_lines(content: str) -> list[str]:
    return [ln for ln in content.splitlines() if ln.strip() and not ln.startswith("#")]


def _to_media_path(win: str) -> str:
    """C:\\Users\\Aaron\\iTunes\\iTunes Media\\Music\\A\\B\\c.mp3 -> A/B/c.mp3 (relative to the Music folder)."""
    return win.split(WIN_PREFIX, 1)[1].replace("\\", "/")


def test_real_playlist_uses_crlf_and_windows_drive_paths(real_m3u):
    assert "\r\n" in real_m3u
    files = _file_lines(real_m3u)
    assert len(files) == 29 and all(ln.startswith("C:\\Users\\") for ln in files)


def test_real_playlist_parses_every_entry(real_m3u):
    tracks = parse_m3u(real_m3u)
    assert len(tracks) == 29
    assert all(t["title"] and t["artist"] for t in tracks)


def test_real_playlist_referenced_files_exist_except_one(real_m3u, media_root):
    missing = [_to_media_path(ln) for ln in _file_lines(real_m3u) if not (media_root / _to_media_path(ln)).is_file()]
    assert missing == ["Kavinsky/Unknown Album/Odd Look (ft. The Weeknd).mp3"]


def test_real_playlist_extinf_title_artist_orientation(real_m3u):
    crash = next(t for t in parse_m3u(real_m3u) if t["title"] == "The Crash" or t["artist"] == "The Crash")
    assert (crash["artist"], crash["title"]) == ("Kavinsky", "The Crash")


def test_real_playlist_path_only_fallback_reads_windows_artist_album_track(real_m3u):
    """Without EXTINF the Artist\\Album\\NN Title.mp3 path layout yields the right fields (and strips the NN prefix)."""
    paths_only = "\r\n".join(_file_lines(real_m3u))
    tracks = parse_m3u(paths_only)
    crash = next(t for t in tracks if t["title"] == "The Crash")
    assert (crash["artist"], crash["album"]) == ("Kavinsky", "Teddy Boy EP")
    assert len(tracks) == 29


def test_real_playlist_matches_copied_files_and_misses_the_rest(db, tmp_path, media_root, real_m3u):
    music = tmp_path / "music"
    copy_media_flat = []
    for rel in COPIED:
        src = media_root / rel
        if not src.is_file():
            pytest.skip(f"{src} missing")
        dst = music / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copyfile(src, dst)
        copy_media_flat.append(dst)
    run_scan(db, music)

    entries = _file_lines(real_m3u)
    present = [e for e in entries if (music / _to_media_path(e)).is_file()]
    absent = [e for e in entries if not (music / _to_media_path(e)).is_file()]
    assert len(present) == len(COPIED) and len(absent) == 29 - len(COPIED)

    # Library side: every copied playlist entry resolves by its path-derived artist/album/title.
    paths_only = parse_m3u("\r\n".join(present))
    for t in paths_only:
        artist = db.get_library_artist_by_name(t["artist"])
        assert artist, t
        album = db.get_library_album_by_title(artist["id"], t["album"])
        assert album, t
        assert db.get_library_track_by_title(album["id"], t["title"]), t
    # The entry whose file does not exist on the drive can never match.
    odd = parse_m3u("\r\n".join(e for e in absent if "Odd Look (ft" in e))[0]
    assert db.get_library_artist_by_name(odd["artist"]) is not None  # artist exists...
    album = db.get_library_album_by_title(db.get_library_artist_by_name(odd["artist"])["id"], odd["album"])
    assert album is None, "...but the 'Unknown Album' it points at does not"

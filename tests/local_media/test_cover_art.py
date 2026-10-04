"""Cover art: embedded-artwork detection/extraction, scanner cover_url, and the cover route.

The real Discovery MP3s carry NO embedded art; the folder holds an iTunes/WMP ``AlbumArt_{GUID}_Large.jpg``.
Embedded art is therefore synthesised onto COPIES with the library's own ``embed_album_artwork``.

Note: ``save_cover_art_file`` / ``embed_artwork`` are only consulted by the acquisition worker
(acquisition_worker.py:510), never by manual import or the scanner, so they have no local-media entry point.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import mutagen
import pytest

from plex_playlist_sync.library import embed_album_artwork, inspect_audio_file
from plex_playlist_sync.library_scanner import extract_embedded_cover_art

from .conftest import configure_roots, run_scan, snapshot

pytestmark = pytest.mark.local_media

# Not a decodable image: every code path here treats artwork as opaque bytes.
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"TRACKSEERR-TEST-ART" * 40 + b"\xff\xd9"
ALBUMART = "AlbumArt_{E0B5F6EB-9E7A-4290-A301-FD4994D78C20}_Large.jpg"


def test_real_mp3_has_no_embedded_art_and_extraction_writes_nothing(tmp_path, copy_discovery):
    (mp3,) = copy_discovery(["02 Aerodynamic.mp3"], tmp_path / "a")
    assert not mutagen.File(str(mp3)).tags.getall("APIC")
    assert extract_embedded_cover_art(mp3, mp3.parent) is None
    assert not (mp3.parent / "cover.jpg").exists()


def test_embedded_art_is_detected_and_extracted_byte_for_byte(tmp_path, copy_discovery):
    (mp3,) = copy_discovery(["02 Aerodynamic.mp3"], tmp_path / "a")
    assert embed_album_artwork(mp3, FAKE_JPEG) is True
    assert len(mutagen.File(str(mp3)).tags.getall("APIC")) == 1

    out = extract_embedded_cover_art(mp3, mp3.parent)
    assert out == mp3.parent / "cover.jpg"
    assert out.read_bytes() == FAKE_JPEG


def test_embedding_art_preserves_existing_tags_and_stream(tmp_path, copy_discovery):
    (mp3,) = copy_discovery(["01 One More Time.mp3"], tmp_path / "a")
    before = inspect_audio_file(mp3)
    embed_album_artwork(mp3, FAKE_JPEG)
    after = inspect_audio_file(mp3)
    for key in ("title", "artist", "album_artist", "album", "year", "track_number", "bitrate", "sample_rate"):
        assert after[key] == before[key], key


def test_embed_rejects_empty_image(tmp_path, copy_discovery):
    (mp3,) = copy_discovery(["02 Aerodynamic.mp3"], tmp_path / "a")
    assert embed_album_artwork(mp3, b"") is False


def test_scan_extracts_embedded_art_and_sets_album_cover_url(db, tmp_path, copy_discovery):
    root = tmp_path / "music"
    album_dir = root / "Daft Punk" / "Discovery"
    (mp3,) = copy_discovery(["02 Aerodynamic.mp3"], album_dir)
    embed_album_artwork(mp3, FAKE_JPEG)

    run_scan(db, root)
    (album,) = snapshot(db)["albums"]
    assert album["cover_url"] == f"/api/library/albums/{album['id']}/cover"
    assert (album_dir / "cover.jpg").read_bytes() == FAKE_JPEG, "scanner materialises embedded art next to the audio"


def test_scan_without_any_art_leaves_cover_url_empty(db, tmp_path, copy_discovery):
    root = tmp_path / "music"
    copy_discovery(["02 Aerodynamic.mp3"], root / "Daft Punk" / "Discovery")
    run_scan(db, root)
    (album,) = snapshot(db)["albums"]
    assert album["cover_url"] is None


def test_scan_uses_existing_folder_jpg(db, tmp_path, copy_discovery):
    root = tmp_path / "music"
    album_dir = root / "Daft Punk" / "Discovery"
    copy_discovery(["02 Aerodynamic.mp3"], album_dir)
    (album_dir / "folder.jpg").write_bytes(FAKE_JPEG)
    run_scan(db, root)
    (album,) = snapshot(db)["albums"]
    assert album["cover_url"] == f"/api/library/albums/{album['id']}/cover"


def test_cover_route_serves_local_cover_file(api, tmp_path, copy_discovery):
    root = tmp_path / "music"
    album_dir = root / "Daft Punk" / "Discovery"
    copy_discovery(["02 Aerodynamic.mp3"], album_dir)
    (album_dir / "cover.jpg").write_bytes(FAKE_JPEG)
    configure_roots(api.db, root)
    run_scan(api.db, root)
    (album,) = snapshot(api.db)["albums"]

    resp = api.get(f"/api/library/albums/{album['id']}/cover")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == FAKE_JPEG


def test_itunes_albumart_jpg_is_recognised_as_folder_art(db, tmp_path, copy_discovery, discovery_src):
    src_art = discovery_src / ALBUMART
    if not src_art.is_file():
        pytest.skip("iTunes AlbumArt jpg not present in this library")
    root = tmp_path / "music"
    album_dir = root / "Daft Punk" / "Discovery"
    copy_discovery(["02 Aerodynamic.mp3"], album_dir)
    shutil.copyfile(src_art, album_dir / ALBUMART)
    run_scan(db, root)
    (album,) = snapshot(db)["albums"]
    assert album["cover_url"] is not None

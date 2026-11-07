"""Tests for multi-file track grab imports and single-track selection."""
from __future__ import annotations

import logging
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trackseerr.acquisition_worker import AcquisitionWorker
from trackseerr.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    LibraryAlbum,
    LibraryArtist,
    LibraryTrack,
    MusicRequest,
    RequestStatus,
)
from trackseerr.storage import Database


def _flac(path: Path, payload: bytes = b"") -> None:
    sr = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    streaminfo = struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00") + sr + b"\x00" * 16
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC\x80\x00\x00\x22" + streaminfo + payload)


def _mp3(path: Path, payload: bytes = b"") -> None:
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frame * 2 + payload)


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "multifile.db"))
    yield d
    d.close()


def _seed_bryan_adams(
    db: Database,
    tmp_path: Path,
    *,
    with_track_id: bool = True,
    with_request: bool = False,
    request_track_id: str | None = None,
    quality_profile_id: str | None = None,
) -> tuple[Path, Path, Path]:
    music, staging = tmp_path / "music", tmp_path / "staging"
    music.mkdir(exist_ok=True)
    staging.mkdir(exist_ok=True)
    db.upsert_user("u1", "user", "u@example.com", is_admin=True)
    db.update_media_management_settings(
        {
            "root_folder_path": str(music),
            "staging_folder_path": str(staging),
            "library_mode": "native",
            "enrich_mbids": False,
            "write_audio_tags": False,
        }
    )
    db.upsert_library_artist(
        LibraryArtist(
            id="art-ba",
            name="Bryan Adams",
            clean_name="bryan adams",
            path=str(music / "Bryan Adams"),
            quality_profile_id=quality_profile_id,
        )
    )
    db.upsert_library_album(
        LibraryAlbum(
            id="alb-waking",
            artist_id="art-ba",
            title="Waking Up the Neighbours",
            clean_title="waking up the neighbours",
            year=1991,
        )
    )
    db.upsert_library_track(
        LibraryTrack(
            id="trk-everything",
            album_id="alb-waking",
            artist_id="art-ba",
            title="(Everything I Do) I Do It for You",
            clean_title="(everything i do) i do it for you",
            duration_seconds=394.0,
            track_number=1,
            disc_number=1,
            monitored=True,
            mb_recording_id="b0449348-1111-2222-3333-444455556666",
        )
    )

    req_id = None
    if with_request:
        req = db.create_request(
            MusicRequest(
                id="req-1",
                user_id="u1",
                artist="Bryan Adams",
                title="(Everything I Do) I Do It for You",
                item_type="track",
                quality_profile_id=quality_profile_id,
                status=RequestStatus.PENDING,
            )
        )
        req_id = req["id"]

    db.create_download_client(
        DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    db.create_active_download(
        ActiveDownload(
            id="dl-1",
            client_id="c1",
            title="Bryan Adams - (Everything I Do) I Do It For You (1991) (CDM) [FLAC] [MP3]",
            artist="Bryan Adams",
            item_type="track",
            status=DownloadStatus.DOWNLOADING.value,
            download_hash="h-ba",
            album_id="alb-waking" if with_track_id else None,
            track_id="trk-everything" if with_track_id else None,
            request_id=req_id,
        )
    )
    dl = staging / "Bryan Adams - (Everything I Do) I Do It For You (1991) (CDM) [FLAC] [MP3]"
    dl.mkdir(parents=True, exist_ok=True)
    return music, staging, dl


def _run_worker(db: Database, dl: Path, staging: Path) -> dict[str, int]:
    driver = MagicMock()
    driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value,
        "progress": 100.0,
        "size_bytes": 1,
        "speed_bps": 0,
        "eta_seconds": 0,
        "source_path": str(dl),
        "error_message": None,
    }
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        return AcquisitionWorker().poll_once(db=db, staging_dir=str(staging))


def test_track_grab_cdm_imports_only_wanted_song_flac(tmp_path: Path, db: Database):
    """Exact live evidence: 4 songs x FLAC+MP3, untagged, wanted plain title.

    One FLAC placed on existing catalog track, 7 skipped, no new tracks/albums created.
    """
    music, staging, dl = _seed_bryan_adams(db, tmp_path)

    # 4 songs x FLAC + MP3 (8 files total), untagged
    _flac(dl / "01 (Everything I Do) I Do It For You (Single Version).flac", b"FLAC-SONG-1")
    _mp3(dl / "01 (Everything I Do) I Do It For You (Single Version).mp3", b"MP3-SONG-1")
    _flac(dl / "02 There Will Never Be Another Tonight (Live).flac", b"FLAC-SONG-2")
    _mp3(dl / "02 There Will Never Be Another Tonight (Live).mp3", b"MP3-SONG-2")
    _flac(dl / "03 Cuts Like A Knife (Live).flac", b"FLAC-SONG-3")
    _mp3(dl / "03 Cuts Like A Knife (Live).mp3", b"MP3-SONG-3")
    _flac(dl / "04 She's Only Happy When She's Dancin' (Live).flac", b"FLAC-SONG-4")
    _mp3(dl / "04 She's Only Happy When She's Dancin' (Live).mp3", b"MP3-SONG-4")

    stats = _run_worker(db, dl, staging)
    assert stats["imported"] == 1
    assert stats["failed"] == 0

    placed_files = list(music.rglob("*.flac")) + list(music.rglob("*.mp3"))
    assert len(placed_files) == 1, f"Expected exactly 1 placed file, got {len(placed_files)}"
    assert placed_files[0].suffix.lower() == ".flac"
    assert placed_files[0].read_bytes().endswith(b"FLAC-SONG-1")

    # Catalog tracks count must still be 1 (no new tracks created)
    tracks = db.list_library_tracks(album_id="alb-waking")
    assert len(tracks) == 1
    assert tracks[0]["id"] == "trk-everything"

    # Catalog albums count must still be 1 (no new album created)
    albums = db.list_library_albums(artist_id="art-ba")
    assert len(albums) == 1
    assert albums[0]["id"] == "alb-waking"

    # The file is linked to the existing catalog track
    linked = db.get_library_file_for_track("trk-everything")
    assert linked is not None
    assert linked["codec"].lower() == "flac"

    # Other 7 files still exist in the download folder
    assert (dl / "01 (Everything I Do) I Do It For You (Single Version).mp3").exists()
    assert (dl / "02 There Will Never Be Another Tonight (Live).flac").exists()
    assert (dl / "03 Cuts Like A Knife (Live).flac").exists()
    assert (dl / "04 She's Only Happy When She's Dancin' (Live).flac").exists()


def test_track_grab_prefers_mp3_when_profile_disallows_flac(tmp_path: Path, db: Database):
    """When the active profile disallows FLAC (e.g. Standard MP3), MP3 is picked over FLAC."""
    db.set_default_quality_profile("profile-standard-mp3")
    music, staging, dl = _seed_bryan_adams(db, tmp_path, quality_profile_id="profile-standard-mp3")

    _flac(dl / "01 (Everything I Do) I Do It For You.flac", b"FLAC-DISALLOWED")
    _mp3(dl / "01 (Everything I Do) I Do It For You.mp3", b"MP3-PREFERRED")

    stats = _run_worker(db, dl, staging)
    assert stats["imported"] == 1
    assert stats["failed"] == 0

    placed_files = list(music.rglob("*.flac")) + list(music.rglob("*.mp3"))
    assert len(placed_files) == 1
    assert placed_files[0].suffix.lower() == ".mp3"
    assert placed_files[0].read_bytes().endswith(b"MP3-PREFERRED")

    linked = db.get_library_file_for_track("trk-everything")
    assert linked is not None
    assert linked["codec"].lower() == "mp3"


def test_track_grab_live_variant_not_chosen_when_plain_exists(tmp_path: Path, db: Database):
    """When a plain-title file exists alongside a live variant, plain file is chosen."""
    music, staging, dl = _seed_bryan_adams(db, tmp_path)

    _flac(dl / "01 (Everything I Do) I Do It For You.flac", b"PLAIN-TITLE")
    _flac(dl / "02 (Everything I Do) I Do It For You (Live).flac", b"LIVE-VARIANT")

    stats = _run_worker(db, dl, staging)
    assert stats["imported"] == 1
    assert stats["failed"] == 0

    placed_files = list(music.rglob("*.flac"))
    assert len(placed_files) == 1
    assert placed_files[0].read_bytes().endswith(b"PLAIN-TITLE")
    assert (dl / "02 (Everything I Do) I Do It For You (Live).flac").exists()


def test_track_grab_single_version_used_when_only_variant(tmp_path: Path, db: Database):
    """When no plain-title file exists, the closest single variant is used."""
    music, staging, dl = _seed_bryan_adams(db, tmp_path)

    _flac(dl / "01 (Everything I Do) I Do It For You (Single Version).flac", b"SINGLE-VERSION")

    stats = _run_worker(db, dl, staging)
    assert stats["imported"] == 1
    assert stats["failed"] == 0

    placed_files = list(music.rglob("*.flac"))
    assert len(placed_files) == 1
    assert placed_files[0].read_bytes().endswith(b"SINGLE-VERSION")
    assert db.get_library_file_for_track("trk-everything") is not None


def test_track_grab_no_matching_file_goes_to_manual_import(tmp_path: Path, db: Database):
    """When no files in download match wanted title, marks for manual import without failure/blocklist."""
    music, staging, dl = _seed_bryan_adams(db, tmp_path)

    _flac(dl / "01 Cuts Like A Knife.flac", b"DIFFERENT-SONG-1")
    _flac(dl / "02 Summer of 69.flac", b"DIFFERENT-SONG-2")

    stats = _run_worker(db, dl, staging)
    assert stats["imported"] == 0
    assert stats["failed"] == 0

    assert len(list(music.rglob("*.flac"))) == 0

    row = db.get_active_download("dl-1")
    assert row["status"] == DownloadStatus.WARNING.value
    assert row["error_message"] == 'No file in the download matches "(Everything I Do) I Do It for You"'
    assert len(row["unmatched_files"]) == 2
    assert not db.is_blocklisted(release_title="Bryan Adams - (Everything I Do) I Do It For You (1991) (CDM) [FLAC] [MP3]")


def test_track_grab_request_without_track_id_resolves_catalog_track(tmp_path: Path, db: Database):
    """A track grab with request but no download track_id resolves existing catalog track."""
    music, staging, dl = _seed_bryan_adams(
        db, tmp_path, with_track_id=False, with_request=True
    )

    _flac(dl / "01 (Everything I Do) I Do It For You.flac", b"FLAC-PAYLOAD")

    stats = _run_worker(db, dl, staging)
    assert stats["imported"] == 1
    assert stats["failed"] == 0

    placed_files = list(music.rglob("*.flac"))
    assert len(placed_files) == 1
    assert placed_files[0].read_bytes().endswith(b"FLAC-PAYLOAD")

    # Catalog track linked without creating duplicate rows
    tracks = db.list_library_tracks(album_id="alb-waking")
    assert len(tracks) == 1
    assert tracks[0]["id"] == "trk-everything"

    linked = db.get_library_file_for_track("trk-everything")
    assert linked is not None


def test_same_destination_never_overwrites(tmp_path: Path, db: Database, caplog: pytest.LogCaptureFixture):
    """Two files mapping to same destination path: first kept, second skipped, warning logged."""
    music, staging, dl = _seed_bryan_adams(db, tmp_path, with_track_id=False)

    # In an album grab with two files with identical name/target
    db.conn.execute("UPDATE active_downloads SET item_type = 'album' WHERE id = 'dl-1'")
    db.conn.commit()

    f1 = dl / "folder1" / "01 - (Everything I Do) I Do It for You.flac"
    f2 = dl / "folder2" / "01 - (Everything I Do) I Do It for You.flac"
    _flac(f1, b"FIRST-FILE")
    _flac(f2, b"SECOND-FILE")

    with caplog.at_level(logging.WARNING):
        stats = _run_worker(db, dl, staging)
    assert stats["imported"] == 1
    assert stats["failed"] == 0

    placed_files = list(music.rglob("*.flac"))
    assert len(placed_files) == 1
    assert placed_files[0].read_bytes().endswith(b"FIRST-FILE")
    assert any("Two files map to" in r.message and "kept the first" in r.message for r in caplog.records)


def test_album_grab_dual_format_keeps_best_per_track(tmp_path: Path, db: Database):
    """Album grab with same track in two formats keeps only best per track."""
    music, staging = tmp_path / "music", tmp_path / "staging"
    music.mkdir(exist_ok=True)
    staging.mkdir(exist_ok=True)
    db.upsert_user("u1", "user", "u@example.com", is_admin=True)
    db.update_media_management_settings(
        {
            "root_folder_path": str(music),
            "staging_folder_path": str(staging),
            "library_mode": "native",
            "enrich_mbids": False,
            "write_audio_tags": False,
        }
    )
    db.upsert_library_artist(
        LibraryArtist(id="art-daft", name="Daft Punk", clean_name="daft punk", path=str(music / "Daft Punk"))
    )
    db.upsert_library_album(
        LibraryAlbum(id="alb-disc", artist_id="art-daft", title="Discovery", clean_title="discovery", year=2001)
    )
    db.upsert_library_track(
        LibraryTrack(id="trk-d1", album_id="alb-disc", artist_id="art-daft", title="One More Time", track_number=1, disc_number=1)
    )
    db.upsert_library_track(
        LibraryTrack(id="trk-d2", album_id="alb-disc", artist_id="art-daft", title="Aerodynamic", track_number=2, disc_number=1)
    )

    db.create_download_client(
        DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    db.create_active_download(
        ActiveDownload(
            id="dl-album",
            client_id="c1",
            title="Daft Punk - Discovery (2001) [FLAC] [MP3]",
            artist="Daft Punk",
            item_type="album",
            status=DownloadStatus.DOWNLOADING.value,
            download_hash="h-dp",
            album_id="alb-disc",
        )
    )
    dl = staging / "Daft Punk - Discovery (2001) [FLAC] [MP3]"
    dl.mkdir(parents=True, exist_ok=True)

    _flac(dl / "01 One More Time.flac", b"FLAC-T1")
    _mp3(dl / "01 One More Time.mp3", b"MP3-T1")
    _flac(dl / "02 Aerodynamic.flac", b"FLAC-T2")
    _mp3(dl / "02 Aerodynamic.mp3", b"MP3-T2")

    stats = _run_worker(db, dl, staging)
    assert stats["imported"] == 1
    assert stats["failed"] == 0

    placed_flacs = list(music.rglob("*.flac"))
    placed_mp3s = list(music.rglob("*.mp3"))
    assert len(placed_flacs) == 2
    assert len(placed_mp3s) == 0

    assert db.get_library_file_for_track("trk-d1") is not None
    assert db.get_library_file_for_track("trk-d2") is not None


def test_single_file_track_grab_unchanged(tmp_path: Path, db: Database):
    """Soulseek-style single file still imports as before."""
    music, staging, dl = _seed_bryan_adams(db, tmp_path)

    _flac(dl / "01 - Bryan Adams - (Everything I Do) I Do It for You.flac", b"SOULSEEK-SINGLE")

    stats = _run_worker(db, dl, staging)
    assert stats["imported"] == 1
    assert stats["failed"] == 0

    placed_files = list(music.rglob("*.flac"))
    assert len(placed_files) == 1
    assert placed_files[0].read_bytes().endswith(b"SOULSEEK-SINGLE")
    assert db.get_library_file_for_track("trk-everything") is not None


def test_track_grab_multifile_with_dangerous_file_rejects_whole_release(tmp_path: Path, db: Database):
    """A release with a dangerous file must be rejected as a whole even if wanted song matches cleanly."""
    music, staging, dl = _seed_bryan_adams(db, tmp_path)

    _flac(dl / "01 (Everything I Do) I Do It For You.flac", b"FLAC-SONG-1")
    bad_file = dl / "02 Malicious.mp3"
    bad_file.write_bytes(b"<html>not an audio file</html>")

    stats = _run_worker(db, dl, staging)
    assert stats["failed"] == 1
    assert stats["imported"] == 0

    # Nothing imported to library (quarantined files are in .trackseerr-quarantine)
    assert not any(p for p in music.rglob("*") if ".trackseerr-quarantine" not in p.parts)

    # Download marked failed
    row = db.get_active_download("dl-1")
    assert row["status"] == DownloadStatus.FAILED.value
    assert "Security check failed" in (row.get("error_message") or "")


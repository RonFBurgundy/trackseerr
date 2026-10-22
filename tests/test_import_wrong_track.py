"""Imports must never replace another track's file, nor a same-or-better file on the wanted track."""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Any
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
    LibraryFile,
    LibraryTrack,
)
from trackseerr.storage import Database


def _flac(path: Path, payload: bytes = b"") -> None:
    sr = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    streaminfo = struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00") + sr + b"\x00" * 16
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC\x80\x00\x00\x22" + streaminfo + payload)


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "wrong_track.db"))
    yield d
    d.close()


def _seed(
    db: Database, tmp_path: Path, *, wanted_filed_quality: str | None = None
) -> tuple[Path, Path, Path, Path]:
    """Album with track 1 'Lie, Cheat, Steal' (filed FLAC 24bit) and wanted track 2 '... (instrumental)'.

    When ``wanted_filed_quality`` is set, the wanted track 2 is filed instead of being file-less.
    """
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
        LibraryArtist(id="art-1", name="Run the Jewels", clean_name="run the jewels", path=str(music / "RTJ"))
    )
    db.upsert_library_album(
        LibraryAlbum(id="alb-1", artist_id="art-1", title="RTJ2", clean_title="rtj2", year=2014)
    )
    folder = music / "RTJ" / "RTJ2"
    vocal = folder / "01 Lie, Cheat, Steal.flac"
    _flac(vocal, b"VOCAL-ORIGINAL")
    instr = folder / "02 Lie, Cheat, Steal (instrumental).flac"
    for tid, title, num in (
        ("trk-1", "Lie, Cheat, Steal", 1),
        ("trk-2", "Lie, Cheat, Steal (instrumental)", 2),
    ):
        db.upsert_library_track(
            LibraryTrack(
                id=tid,
                album_id="alb-1",
                artist_id="art-1",
                title=title,
                clean_title=title.lower(),
                duration_seconds=200.0,
                track_number=num,
                disc_number=1,
                monitored=True,
            )
        )
    db.upsert_library_file(
        LibraryFile(
            id="fil-1",
            track_id="trk-1",
            file_path=str(vocal),
            relative_path="RTJ/RTJ2/01 Lie, Cheat, Steal.flac",
            codec="flac",
            bits_per_sample=24,
            quality_name="FLAC 24bit",
            cutoff_met=True,
        )
    )
    if wanted_filed_quality:
        _flac(instr, b"INSTR-ORIGINAL")
        db.upsert_library_file(
            LibraryFile(
                id="fil-2",
                track_id="trk-2",
                file_path=str(instr),
                relative_path="RTJ/RTJ2/02 Lie, Cheat, Steal (instrumental).flac",
                codec="flac",
                bits_per_sample=24 if "24" in wanted_filed_quality else 16,
                quality_name=wanted_filed_quality,
                cutoff_met=True,
            )
        )
    db.create_download_client(
        DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    db.create_active_download(
        ActiveDownload(
            id="dl-1",
            client_id="c1",
            title="Run the Jewels - Lie, Cheat, Steal (instrumental)",
            artist="Run the Jewels",
            item_type="track",
            status=DownloadStatus.DOWNLOADING.value,
            download_hash="h1",
            album_id="alb-1",
            track_id="trk-2",
        )
    )
    dl = staging / "dl"
    dl.mkdir(parents=True, exist_ok=True)
    return music, staging, dl, vocal


def _run(db: Database, dl: Path, staging: Path, bits: int, *, number: int = 1) -> None:
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

    def inspect(path: Any) -> dict[str, Any]:
        return {
            "artist": "Run the Jewels",
            "album": "RTJ2",
            "title": "Lie, Cheat, Steal",  # the tag lacks the qualifier the file name carries
            "track_number": number,
            "disc_number": 1,
            "duration": 200.0,
            "codec": "FLAC",
            "bits_per_sample": bits,
            "bitrate": 900,
            "sample_rate": 44100,
            "extension": ".flac",
            "file_path": str(path),
        }

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.acquisition_import.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_catalog.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_worker.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        AcquisitionWorker().poll_once(db=db, staging_dir=str(staging))


def _events(db: Database, track_id: str) -> set[str]:
    return {e["event"] for e in db.list_item_events("track", track_id)}


def test_import_never_replaces_other_tracks_file(tmp_path: Path, db: Database):
    _, staging, dl, vocal = _seed(db, tmp_path)
    _flac(dl / "02 Lie, Cheat, Steal (Instrumental).flac", b"INSTRUMENTAL-NEW")
    _run(db, dl, staging, bits=24)

    assert vocal.read_bytes().endswith(b"VOCAL-ORIGINAL")
    assert not ({"file_replaced", "upgraded"} & _events(db, "trk-1"))
    landed = db.get_library_file_for_track("trk-2")
    assert landed is not None
    assert Path(landed["file_path"]).read_bytes().endswith(b"INSTRUMENTAL-NEW")
    assert db.get_library_file_for_track("trk-1")["id"] == "fil-1"


def test_equal_quality_does_not_replace(tmp_path: Path, db: Database):
    _, staging, dl, _ = _seed(db, tmp_path, wanted_filed_quality="FLAC 24bit")
    _flac(dl / "02 Lie, Cheat, Steal (Instrumental).flac", b"INSTRUMENTAL-NEW")
    _run(db, dl, staging, bits=24, number=2)

    existing = db.get_library_file_for_track("trk-2")
    assert Path(existing["file_path"]).read_bytes().endswith(b"INSTR-ORIGINAL")
    assert "file_replaced" not in _events(db, "trk-2")
    row = db.get_active_download("dl-1")
    assert row["status"] == DownloadStatus.IMPORTED.value
    assert "duplicate" in (row.get("error_message") or "").lower()


def test_lower_quality_does_not_replace(tmp_path: Path, db: Database):
    _, staging, dl, _ = _seed(db, tmp_path, wanted_filed_quality="FLAC 24bit")
    _flac(dl / "02 Lie, Cheat, Steal (Instrumental).flac", b"INSTRUMENTAL-NEW")
    _run(db, dl, staging, bits=16, number=2)

    existing = db.get_library_file_for_track("trk-2")
    assert Path(existing["file_path"]).read_bytes().endswith(b"INSTR-ORIGINAL")
    assert "file_replaced" not in _events(db, "trk-2")
    assert db.get_active_download("dl-1")["status"] == DownloadStatus.IMPORTED.value


def test_duplicate_discard_history_has_message(tmp_path: Path, db: Database):
    _, staging, dl, _ = _seed(db, tmp_path, wanted_filed_quality="FLAC 24bit")
    _flac(dl / "02 Lie, Cheat, Steal (Instrumental).flac", b"INSTRUMENTAL-NEW")
    _run(db, dl, staging, bits=24, number=2)

    rows, _ = db.list_download_history(1, 50, "desc", event="imported")
    assert rows, "the discard must be recorded in download history"
    assert "duplicate discarded" in (rows[0].get("message") or "").lower()


def test_owned_elsewhere_discard_blocklists_release(tmp_path: Path, db: Database):
    _, staging, dl, _ = _seed(db, tmp_path)
    # The file is the vocal song, not the wanted instrumental: nothing on the wanted track, owned elsewhere.
    _flac(dl / "01 Lie, Cheat, Steal.flac", b"VOCAL-AGAIN")
    _run(db, dl, staging, bits=24)

    assert db.get_library_file_for_track("trk-2") is None
    assert db.is_blocklisted(release_guid="h1") or db.is_blocklisted(info_hash="h1")

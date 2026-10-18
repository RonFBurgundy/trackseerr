"""Tests for importing held files onto matching file-less tracks on other albums."""
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
    LibraryTrack,
)
from trackseerr.storage import Database


def _flac(path: Path) -> None:
    sr = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    streaminfo = struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00") + sr + b"\x00" * 16
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC\x80\x00\x00\x22" + streaminfo)


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "held_cross_album.db"))
    yield d
    d.close()


def _seed_costello(
    db: Database,
    tmp_path: Path,
    *,
    with_download: bool = True,
    download_status: str = DownloadStatus.DOWNLOADING.value,
    held_file_path: str | None = None,
    track_duration: float | None = 157.0,
    add_second_candidate: bool = False,
):
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

    # Artist 1: "Elvis Costello" with file-less "Hand in Hand" on "This Year's Model"
    art_ec = db.upsert_library_artist(
        LibraryArtist(id="art-ec", name="Elvis Costello", path=str(music / "Elvis Costello"))
    )
    alb_tym = db.upsert_library_album(
        LibraryAlbum(id="alb-tym", artist_id="art-ec", title="This Year's Model", year=1978)
    )
    trk_hih = db.upsert_library_track(
        LibraryTrack(
            id="trk-hih",
            album_id="alb-tym",
            artist_id="art-ec",
            title="Hand in Hand",
            duration_seconds=track_duration,
            track_number=7,
            disc_number=1,
            monitored=True,
        )
    )

    if add_second_candidate:
        alb_live = db.upsert_library_album(
            LibraryAlbum(id="alb-live", artist_id="art-ec", title="Live at the El Mocambo", year=1978)
        )
        db.upsert_library_track(
            LibraryTrack(
                id="trk-hih-live",
                album_id="alb-live",
                artist_id="art-ec",
                title="Hand in Hand",
                duration_seconds=track_duration,
                track_number=3,
                disc_number=1,
                monitored=True,
            )
        )

    # Artist 2: "Elvis Costello & The Attractions" with target album "Armed Forces"
    art_ecta = db.upsert_library_artist(
        LibraryArtist(
            id="art-ecta",
            name="Elvis Costello & The Attractions",
            path=str(music / "Elvis Costello & The Attractions"),
        )
    )
    alb_af = db.upsert_library_album(
        LibraryAlbum(id="alb-af", artist_id="art-ecta", title="Armed Forces", year=1979)
    )
    trk_awh = db.upsert_library_track(
        LibraryTrack(
            id="trk-awh",
            album_id="alb-af",
            artist_id="art-ecta",
            title="Accidents Will Happen",
            duration_seconds=180.0,
            track_number=1,
            disc_number=1,
            monitored=True,
        )
    )
    trk_oa = db.upsert_library_track(
        LibraryTrack(
            id="trk-oa",
            album_id="alb-af",
            artist_id="art-ecta",
            title="Oliver's Army",
            duration_seconds=178.0,
            track_number=2,
            disc_number=1,
            monitored=True,
        )
    )

    dl = None
    if with_download:
        db.create_download_client(
            DownloadClientConfig(
                id="c1",
                name="C",
                driver_type=DownloadDriverType.SLSKD,
                host_url="http://slskd:5030",
            )
        )
        dl = db.create_active_download(
            ActiveDownload(
                id="dl-1",
                client_id="c1",
                title="Elvis Costello & The Attractions - Armed Forces",
                artist="Elvis Costello & The Attractions",
                item_type="album",
                status=download_status,
                download_hash="h1",
                album_id="alb-af",
            )
        )
        if held_file_path:
            db.set_download_unmatched_files("dl-1", [held_file_path])
            db.update_download_status(
                "dl-1",
                status=DownloadStatus.WARNING.value,
                error_message="1 file(s) couldn't be matched — manual import required",
            )

    return music, staging, art_ec, alb_tym, trk_hih, art_ecta, alb_af, trk_awh, trk_oa


def _run_worker(
    db: Database,
    dl: Path,
    staging: Path,
    metas: dict[str, dict[str, Any]],
    driver: MagicMock | None = None,
):
    driver = driver or MagicMock()
    driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value,
        "progress": 100.0,
        "size_bytes": 1,
        "speed_bps": 0,
        "eta_seconds": 0,
        "source_path": str(dl),
        "error_message": None,
    }

    def inspect(path):
        base = {
            "artist": "Elvis Costello & The Attractions",
            "album": "Armed Forces",
            "disc_number": 1,
            "codec": "FLAC",
            "bits_per_sample": 16,
            "bitrate": 900,
            "sample_rate": 44100,
            "extension": ".flac",
        }
        return {**base, **metas.get(Path(path).name, {})}

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.acquisition_import.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_catalog.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_worker.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        stats = AcquisitionWorker().poll_once(db=db, staging_dir=str(staging))
    return stats, driver


def test_file_imported_onto_matching_track_of_related_artist(tmp_path: Path, db: Database):
    """File tagged Hand In Hand (2021 Remaster), 158s placed under Elvis Costello / This Year's Model."""
    music, staging, art_ec, alb_tym, trk_hih, art_ecta, alb_af, _, _ = _seed_costello(db, tmp_path)
    dl = staging / "Elvis Costello & The Attractions - Armed Forces"
    f = dl / "07_Hand In Hand (2021 Remaster).flac"
    _flac(f)
    metas = {
        "07_Hand In Hand (2021 Remaster).flac": {
            "title": "Hand In Hand (2021 Remaster)",
            "artist": "Elvis Costello & The Attractions",
            "duration": 158.0,
            "duration_seconds": 158.0,
            "track_number": 7,
            "disc_number": 1,
        }
    }

    stats, driver = _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    assert dl_row["unmatched_files"] == []

    # File must be linked to trk-hih on alb-tym (Elvis Costello), not alb-af
    files = db.list_library_files_for_track("trk-hih")
    assert len(files) == 1
    placed_file = files[0]
    assert placed_file["track_id"] == "trk-hih"
    placed_path = Path(placed_file["file_path"])
    assert placed_path.exists()
    assert "This Year's Model" in str(placed_path)
    assert "Elvis Costello" in str(placed_path)


def test_duration_mismatch_still_holds(tmp_path: Path, db: Database):
    """200 s vs 157 s exceeds 3.0 s tolerance and leaves file held."""
    music, staging, _, _, _, _, _, _, _ = _seed_costello(db, tmp_path)
    dl = staging / "Elvis Costello & The Attractions - Armed Forces"
    f = dl / "07_Hand In Hand (2021 Remaster).flac"
    _flac(f)
    metas = {
        "07_Hand In Hand (2021 Remaster).flac": {
            "title": "Hand In Hand (2021 Remaster)",
            "artist": "Elvis Costello & The Attractions",
            "duration": 200.0,
            "duration_seconds": 200.0,
            "track_number": 7,
            "disc_number": 1,
        }
    }

    stats, driver = _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert "manual import required" in (dl_row.get("error_message") or "")
    assert str(f) in dl_row["unmatched_files"]
    assert f.exists()
    assert db.list_library_files_for_track("trk-hih") == []


def test_unknown_duration_still_holds(tmp_path: Path, db: Database):
    """Unknown duration (None) leaves file held."""
    music, staging, _, _, _, _, _, _, _ = _seed_costello(db, tmp_path)
    dl = staging / "Elvis Costello & The Attractions - Armed Forces"
    f = dl / "07_Hand In Hand (2021 Remaster).flac"
    _flac(f)
    metas = {
        "07_Hand In Hand (2021 Remaster).flac": {
            "title": "Hand In Hand (2021 Remaster)",
            "artist": "Elvis Costello & The Attractions",
            "duration": None,
            "duration_seconds": None,
            "track_number": 7,
            "disc_number": 1,
        }
    }

    stats, driver = _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert "manual import required" in (dl_row.get("error_message") or "")
    assert str(f) in dl_row["unmatched_files"]
    assert f.exists()
    assert db.list_library_files_for_track("trk-hih") == []


def test_two_candidates_still_holds(tmp_path: Path, db: Database):
    """Two file-less tracks matching length is ambiguous and leaves file held."""
    music, staging, _, _, _, _, _, _, _ = _seed_costello(db, tmp_path, add_second_candidate=True)
    dl = staging / "Elvis Costello & The Attractions - Armed Forces"
    f = dl / "07_Hand In Hand (2021 Remaster).flac"
    _flac(f)
    metas = {
        "07_Hand In Hand (2021 Remaster).flac": {
            "title": "Hand In Hand (2021 Remaster)",
            "artist": "Elvis Costello & The Attractions",
            "duration": 158.0,
            "duration_seconds": 158.0,
            "track_number": 7,
            "disc_number": 1,
        }
    }

    stats, driver = _run_worker(db, dl, staging, metas)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert "manual import required" in (dl_row.get("error_message") or "")
    assert str(f) in dl_row["unmatched_files"]
    assert f.exists()
    assert db.list_library_files_for_track("trk-hih") == []


def test_parked_row_requeued_once_then_imported(tmp_path: Path, db: Database):
    """WARNING row with that held file -> after one _poll_once it is COMPLETED with retry marker; second run where import still holds leaves it WARNING and does not requeue again."""
    dl_dir = tmp_path / "staging" / "Elvis Costello & The Attractions - Armed Forces"
    f = dl_dir / "07_Hand In Hand (2021 Remaster).flac"
    _flac(f)
    music, staging, _, _, trk_hih, _, _, _, _ = _seed_costello(
        db,
        tmp_path,
        download_status=DownloadStatus.WARNING.value,
        held_file_path=str(f),
    )

    driver = MagicMock()
    driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value,
        "progress": 100.0,
        "size_bytes": 1,
        "speed_bps": 0,
        "eta_seconds": 0,
        "source_path": str(dl_dir),
        "error_message": None,
    }

    metas = {
        "07_Hand In Hand (2021 Remaster).flac": {
            "title": "Hand In Hand (2021 Remaster)",
            "artist": "Elvis Costello & The Attractions",
            "duration": 158.0,
            "duration_seconds": 158.0,
            "track_number": 7,
            "disc_number": 1,
        }
    }

    def inspect(path):
        base = {
            "artist": "Elvis Costello & The Attractions",
            "album": "Armed Forces",
            "disc_number": 1,
            "codec": "FLAC",
            "bits_per_sample": 16,
            "bitrate": 900,
            "sample_rate": 44100,
            "extension": ".flac",
        }
        return {**base, **metas.get(Path(path).name, {})}

    # Run 1: sweep detects fileless match, requeues to COMPLETED with retry marker
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.acquisition_import.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_catalog.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_worker.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        AcquisitionWorker()._poll_once(db=db, staging_dir=str(staging))

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.COMPLETED.value
    assert dl_row["error_message"] == "Retried import onto a matching track"

    # Second run where import still holds (e.g. candidate track became unavailable)
    # The import will re-hold the file as WARNING.
    # The subsequent resolution check must NOT requeue again.
    metas["07_Hand In Hand (2021 Remaster).flac"]["duration"] = 500.0
    metas["07_Hand In Hand (2021 Remaster).flac"]["duration_seconds"] = 500.0

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.acquisition_import.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_catalog.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_worker.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        AcquisitionWorker()._poll_once(db=db, staging_dir=str(staging))

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert "Retried import" in str(dl_row.get("error_message"))

    # Third run: verify _resolve_parked_duplicates does not requeue it again even though duration matches again
    metas["07_Hand In Hand (2021 Remaster).flac"]["duration"] = 158.0
    metas["07_Hand In Hand (2021 Remaster).flac"]["duration_seconds"] = 158.0

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.acquisition_import.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_catalog.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_worker.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        AcquisitionWorker()._poll_once(db=db, staging_dir=str(staging))

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value


def test_wrong_song_not_requeued(tmp_path: Path, db: Database):
    """Held All of the Time with no file-less match stays WARNING."""
    dl_dir = tmp_path / "staging" / "Elvis Costello & The Attractions - Armed Forces"
    f = dl_dir / "All of the Time.flac"
    _flac(f)
    music, staging, _, _, _, _, _, _, _ = _seed_costello(
        db,
        tmp_path,
        download_status=DownloadStatus.WARNING.value,
        held_file_path=str(f),
    )

    driver = MagicMock()
    metas = {
        "All of the Time.flac": {
            "title": "All of the Time",
            "artist": "Elvis Costello & The Attractions",
            "duration": 180.0,
            "duration_seconds": 180.0,
            "track_number": 1,
            "disc_number": 1,
        }
    }

    def inspect(path):
        base = {
            "artist": "Elvis Costello & The Attractions",
            "album": "Armed Forces",
            "disc_number": 1,
            "codec": "FLAC",
            "bits_per_sample": 16,
            "bitrate": 900,
            "sample_rate": 44100,
            "extension": ".flac",
        }
        return {**base, **metas.get(Path(path).name, {})}

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.acquisition_import.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_catalog.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_worker.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        AcquisitionWorker()._poll_once(db=db, staging_dir=str(staging))

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert "manual import required" in str(dl_row.get("error_message"))


def test_twin_rows_requeue_only_one(tmp_path: Path, db: Database):
    """Two WARNING rows with the same held path: exactly one is requeued to COMPLETED in one sweep pass."""
    dl_dir = tmp_path / "staging" / "Elvis Costello & The Attractions - Armed Forces"
    f = dl_dir / "07_Hand In Hand (2021 Remaster).flac"
    _flac(f)
    music, staging, _, _, _, _, _, _, _ = _seed_costello(
        db,
        tmp_path,
        download_status=DownloadStatus.WARNING.value,
        held_file_path=str(f),
    )

    # Seed the second twin active download pointing to the same held file
    db.create_active_download(
        ActiveDownload(
            id="dl-2",
            client_id="c1",
            title="Elvis Costello & The Attractions - Armed Forces (Twin)",
            artist="Elvis Costello & The Attractions",
            item_type="album",
            status=DownloadStatus.WARNING.value,
            download_hash="h2",
            album_id="alb-af",
        )
    )
    db.set_download_unmatched_files("dl-2", [str(f)])
    db.update_download_status(
        "dl-2",
        status=DownloadStatus.WARNING.value,
        error_message="1 file(s) couldn't be matched — manual import required",
    )

    metas = {
        "07_Hand In Hand (2021 Remaster).flac": {
            "title": "Hand In Hand",
            "artist": "Elvis Costello",
            "duration": 157.0,
            "duration_seconds": 157.0,
            "track_number": 7,
            "disc_number": 1,
        }
    }

    def inspect(path):
        base = {
            "artist": "Elvis Costello",
            "album": "Armed Forces",
            "disc_number": 1,
            "codec": "FLAC",
            "bits_per_sample": 16,
            "bitrate": 900,
            "sample_rate": 44100,
            "extension": ".flac",
        }
        return {**base, **metas.get(Path(path).name, {})}

    driver = MagicMock()
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.acquisition_import.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_catalog.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.acquisition_worker.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        AcquisitionWorker()._resolve_parked_duplicates(
            db=db,
            ctx=MagicMock(media_settings=db.get_media_management_settings()),
        )

    row1 = db.get_active_download("dl-1")
    row2 = db.get_active_download("dl-2")
    statuses = [row1["status"], row2["status"]]
    assert statuses.count(DownloadStatus.COMPLETED.value) == 1
    assert statuses.count(DownloadStatus.WARNING.value) == 1


def test_retried_import_with_missing_file_reparks_without_blocklist(tmp_path: Path, db: Database):
    """Row with 'Retried import…' message whose held path is deleted reparks as WARNING without blocklist or fallback scan."""
    dl_dir = tmp_path / "staging" / "Elvis Costello & The Attractions - Armed Forces"
    f = dl_dir / "07_Hand In Hand (2021 Remaster).flac"
    # Note: do not create f on disk, or delete it
    music, staging, _, _, _, _, _, _, _ = _seed_costello(
        db,
        tmp_path,
        download_status=DownloadStatus.COMPLETED.value,
        held_file_path=None,
    )
    db.set_download_unmatched_files("dl-1", [str(f)])
    db.update_download_status(
        "dl-1",
        status=DownloadStatus.COMPLETED.value,
        error_message="Retried import onto a matching track",
    )

    worker = AcquisitionWorker()
    stats = {"polled": 0, "completed": 0, "failed": 0, "imported": 0}

    driver = MagicMock()
    driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value,
        "progress": 100.0,
        "size_bytes": 1,
        "speed_bps": 0,
        "eta_seconds": 0,
        "source_path": str(dl_dir),
        "error_message": None,
    }

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch.object(worker, "_find_audio_files") as mock_find:
        mock_find.side_effect = AssertionError("_find_audio_files should not be called for retried import")
        worker._poll_once(db=db, staging_dir=str(staging))

    row = db.get_active_download("dl-1")
    assert row["status"] == DownloadStatus.WARNING.value
    assert row["error_message"] == "Retried import: file no longer on disk"
    assert row["unmatched_files"] == [str(f)]

    # Verify no blocklist entries were created
    blocklist = db.list_blocklist()
    assert len(blocklist) == 0


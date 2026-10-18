"""Tests for auto-clearing downloads whose files are already owned in the library."""
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


def _flac(path: Path) -> None:
    sr = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    streaminfo = struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00") + sr + b"\x00" * 16
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC\x80\x00\x00\x22" + streaminfo)


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "held_cleanup.db"))
    yield d
    d.close()


def _as_torrent(db: Database) -> None:
    """Turns the seeded download client into a qBittorrent one."""
    db.conn.execute("UPDATE download_clients SET driver_type = 'qbittorrent' WHERE id = 'c1'")
    db.conn.commit()


def _seed_prince(db: Database, tmp_path: Path, *, cutoff_met: bool = True, with_download: bool = True):
    music, staging = tmp_path / "music", tmp_path / "staging"
    music.mkdir(exist_ok=True)
    staging.mkdir(exist_ok=True)
    db.update_media_management_settings(
        {
            "root_folder_path": str(music),
            "staging_folder_path": str(staging),
            "library_mode": "native",
            "enrich_mbids": False,
            "write_audio_tags": False,
            "seed_complete_action": "remove",
        }
    )
    artist = db.upsert_library_artist(LibraryArtist(id="art-p", name="Prince", path=str(music / "Prince")))
    album_a = db.upsert_library_album(LibraryAlbum(id="alb-a", artist_id="art-p", title="Sign O' The Times (Live)", year=1987))
    trk_live = db.upsert_library_track(LibraryTrack(
        id="trk-live",
        album_id="alb-a",
        artist_id="art-p",
        title="Play in the Sunshine (Live)",
        track_number=1,
        disc_number=1,
        monitored=True,
    ))

    album_b = db.upsert_library_album(LibraryAlbum(id="alb-b", artist_id="art-p", title="Sign O' The Times", year=1987))
    trk_studio = db.upsert_library_track(LibraryTrack(
        id="trk-studio",
        album_id="alb-b",
        artist_id="art-p",
        title="Play in the Sunshine",
        track_number=1,
        disc_number=1,
        monitored=True,
    ))
    db.upsert_library_file(LibraryFile(
        id="f-studio",
        track_id="trk-studio",
        file_path=str(music / "Prince" / "Sign O The Times" / "01.flac"),
        relative_path="01.flac",
        codec="FLAC",
        size_bytes=1,
        cutoff_met=cutoff_met,
    ))

    if with_download:
        db.create_download_client(
            DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
        )
        db.create_active_download(
            ActiveDownload(
                id="dl-1",
                client_id="c1",
                title="Prince.Sign.O.The.Times.Live.FLAC",
                artist="Prince",
                item_type="album",
                status=DownloadStatus.DOWNLOADING.value,
                download_hash="h1",
                album_id="alb-a",
                track_id="trk-live",
            )
        )
    return music, staging, artist, album_a, album_b, trk_live, trk_studio


def _run_worker(db: Database, dl: Path, staging: Path, metas: dict[str, dict[str, Any]], driver: MagicMock | None = None):
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
            "artist": "Prince",
            "album": "Sign O' The Times (Live)",
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


def test_unmatched_file_owned_elsewhere_is_discarded_not_held(tmp_path: Path, db: Database):
    music, staging, artist, album_a, album_b, trk_live, trk_studio = _seed_prince(db, tmp_path)
    dl = staging / "Prince.Sign.O.The.Times.Live.FLAC"
    f = dl / "track1.flac"
    _flac(f)
    driver = MagicMock()
    metas = {"track1.flac": {"title": "Play In The Sunshine - 2020 Remaster"}}

    _run_worker(db, dl, staging, metas, driver=driver)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    assert "Already in library" in (dl_row.get("error_message") or "")
    assert dl_row["unmatched_files"] == []
    assert not f.exists()
    driver.cleanup_completed.assert_called_once()


def test_owned_but_cutoff_unmet_still_holds(tmp_path: Path, db: Database):
    music, staging, artist, album_a, album_b, trk_live, trk_studio = _seed_prince(db, tmp_path, cutoff_met=False)
    dl = staging / "Prince.Sign.O.The.Times.Live.FLAC"
    f = dl / "track1.flac"
    _flac(f)
    driver = MagicMock()
    metas = {"track1.flac": {"title": "Play In The Sunshine - 2020 Remaster"}}

    _run_worker(db, dl, staging, metas, driver=driver)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert "manual import required" in (dl_row.get("error_message") or "")
    assert str(f) in dl_row["unmatched_files"]
    assert f.exists()
    driver.cleanup_completed.assert_not_called()


def test_wrong_song_still_holds(tmp_path: Path, db: Database):
    music, staging, artist, album_a, album_b, trk_live, trk_studio = _seed_prince(db, tmp_path, cutoff_met=True)
    dl = staging / "Prince.Sign.O.The.Times.Live.FLAC"
    f = dl / "track1.flac"
    _flac(f)
    driver = MagicMock()
    metas = {"track1.flac": {"title": "All of the Time"}}

    _run_worker(db, dl, staging, metas, driver=driver)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert "manual import required" in (dl_row.get("error_message") or "")
    assert str(f) in dl_row["unmatched_files"]
    assert f.exists()
    driver.cleanup_completed.assert_not_called()


def test_torrent_copy_mode_duplicate_keeps_file(tmp_path: Path, db: Database):
    music, staging, artist, album_a, album_b, trk_live, trk_studio = _seed_prince(db, tmp_path, cutoff_met=True)
    _as_torrent(db)
    db.update_media_management_settings({"import_mode": "copy"})
    dl = staging / "Prince.Sign.O.The.Times.Live.FLAC"
    f = dl / "track1.flac"
    _flac(f)
    driver = MagicMock()
    metas = {"track1.flac": {"title": "Play In The Sunshine - 2020 Remaster"}}

    _run_worker(db, dl, staging, metas, driver=driver)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    assert "Already in library" in (dl_row.get("error_message") or "")
    assert f.exists()


def test_parked_warning_with_duplicate_file_is_cleared_on_poll(tmp_path: Path, db: Database):
    music, staging, artist, album_a, album_b, trk_live, trk_studio = _seed_prince(db, tmp_path, cutoff_met=True, with_download=False)
    db.create_download_client(
        DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    dl = staging / "Prince.Sign.O.The.Times.Live.FLAC"
    f = dl / "held.flac"
    _flac(f)
    db.create_active_download(
        ActiveDownload(
            id="dl-1",
            client_id="c1",
            title="Prince.Sign.O.The.Times.Live.FLAC",
            artist="Prince",
            item_type="album",
            status=DownloadStatus.WARNING.value,
            download_hash="h1",
            album_id="alb-a",
            track_id="trk-live",
        )
    )
    db.set_download_unmatched_files("dl-1", [str(f)])
    db.update_download_status("dl-1", status=DownloadStatus.WARNING.value, error_message="1 file(s) couldn't be matched — manual import required")

    driver = MagicMock()
    metas = {"held.flac": {"title": "Play In The Sunshine - 2020 Remaster"}}

    def inspect(path):
        base = {
            "artist": "Prince",
            "album": "Sign O' The Times (Live)",
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
         patch("trackseerr.acquisition_worker.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        AcquisitionWorker().poll_once(db=db, staging_dir=str(staging))

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    assert "Already in library" in (dl_row.get("error_message") or "")
    assert dl_row["unmatched_files"] == []
    assert not f.exists()


def test_parked_warning_with_missing_file_and_owned_track_is_cleared(tmp_path: Path, db: Database):
    music, staging, artist, album_a, album_b, trk_live, trk_studio = _seed_prince(db, tmp_path, cutoff_met=True, with_download=False)
    db.create_download_client(
        DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    # Target track trk-live now has a library file
    db.upsert_library_file(LibraryFile(
        id="f-live",
        track_id="trk-live",
        file_path=str(music / "Prince" / "Sign O The Times Live" / "01.flac"),
        relative_path="01.flac",
        codec="FLAC",
        size_bytes=1,
        cutoff_met=True,
    ))
    db.create_active_download(
        ActiveDownload(
            id="dl-1",
            client_id="c1",
            title="Prince.Sign.O.The.Times.Live.FLAC",
            artist="Prince",
            item_type="album",
            status=DownloadStatus.WARNING.value,
            download_hash="h1",
            album_id="alb-a",
            track_id="trk-live",
        )
    )
    absent = staging / "absent.flac"
    db.set_download_unmatched_files("dl-1", [str(absent)])
    db.update_download_status("dl-1", status=DownloadStatus.WARNING.value, error_message="1 file(s) couldn't be matched — manual import required")

    driver = MagicMock()
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        AcquisitionWorker().poll_once(db=db, staging_dir=str(staging))

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    assert "Already in library" in (dl_row.get("error_message") or "")
    assert dl_row["unmatched_files"] == []


def test_parked_warning_with_missing_file_and_no_owned_track_stays(tmp_path: Path, db: Database):
    music, staging, artist, album_a, album_b, trk_live, trk_studio = _seed_prince(db, tmp_path, cutoff_met=True, with_download=False)
    db.create_download_client(
        DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    db.create_active_download(
        ActiveDownload(
            id="dl-1",
            client_id="c1",
            title="NonExistentSong",
            artist="Prince",
            item_type="album",
            status=DownloadStatus.WARNING.value,
            download_hash="h1",
            album_id="alb-a",
            track_id="trk-live",
        )
    )
    absent = staging / "absent.flac"
    db.set_download_unmatched_files("dl-1", [str(absent)])
    db.update_download_status("dl-1", status=DownloadStatus.WARNING.value, error_message="1 file(s) couldn't be matched — manual import required")

    driver = MagicMock()
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        AcquisitionWorker().poll_once(db=db, staging_dir=str(staging))

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert str(absent) in dl_row["unmatched_files"]


def test_duplicate_unmonitors_fileless_target_track(tmp_path: Path, db: Database):
    music, staging, artist, album_a, album_b, trk_live, trk_studio = _seed_prince(db, tmp_path, cutoff_met=True)
    assert db.get_library_track("trk-live")["monitored"] is True
    assert db.get_library_track("trk-studio")["monitored"] is True
    dl = staging / "Prince.Sign.O.The.Times.Live.FLAC"
    f = dl / "track1.flac"
    _flac(f)
    driver = MagicMock()
    metas = {"track1.flac": {"title": "Play In The Sunshine - 2020 Remaster"}}

    _run_worker(db, dl, staging, metas, driver=driver)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.IMPORTED.value
    assert db.get_library_track("trk-live")["monitored"] is False
    assert db.get_library_track("trk-studio")["monitored"] is True


def test_allow_multiple_editions_on_keeps_holding(tmp_path: Path, db: Database):
    music, staging, artist, album_a, album_b, trk_live, trk_studio = _seed_prince(db, tmp_path, cutoff_met=True)
    db.update_media_management_settings({"allow_multiple_editions": True})
    dl = staging / "Prince.Sign.O.The.Times.Live.FLAC"
    f = dl / "track1.flac"
    _flac(f)
    driver = MagicMock()
    metas = {"track1.flac": {"title": "Play In The Sunshine - 2020 Remaster"}}

    _run_worker(db, dl, staging, metas, driver=driver)

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert "manual import required" in (dl_row.get("error_message") or "")
    assert str(f) in dl_row["unmatched_files"]
    assert f.exists()
    driver.cleanup_completed.assert_not_called()


def test_parked_partial_resolution_updates_remaining(tmp_path: Path, db: Database):
    music, staging, artist, album_a, album_b, trk_live, trk_studio = _seed_prince(db, tmp_path, cutoff_met=True, with_download=False)
    db.create_download_client(
        DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    dl = staging / "Prince.Sign.O.The.Times.Live.FLAC"
    f_dup = dl / "dup.flac"
    f_wrong = dl / "wrong.flac"
    _flac(f_dup)
    _flac(f_wrong)
    db.create_active_download(
        ActiveDownload(
            id="dl-1",
            client_id="c1",
            title="Prince.Sign.O.The.Times.Live.FLAC",
            artist="Prince",
            item_type="album",
            status=DownloadStatus.WARNING.value,
            download_hash="h1",
            album_id="alb-a",
            track_id="trk-live",
        )
    )
    db.set_download_unmatched_files("dl-1", [str(f_dup), str(f_wrong)])
    db.update_download_status("dl-1", status=DownloadStatus.WARNING.value, error_message="2 file(s) couldn't be matched — manual import required")

    driver = MagicMock()
    metas = {
        "dup.flac": {"title": "Play In The Sunshine - 2020 Remaster"},
        "wrong.flac": {"title": "All of the Time"},
    }

    def inspect(path):
        base = {
            "artist": "Prince",
            "album": "Sign O' The Times (Live)",
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
         patch("trackseerr.acquisition_worker.inspect_audio_file", side_effect=inspect), \
         patch("trackseerr.track_matching.fingerprint_audio_file", return_value=None):
        AcquisitionWorker().poll_once(db=db, staging_dir=str(staging))

    dl_row = db.get_active_download("dl-1")
    assert dl_row["status"] == DownloadStatus.WARNING.value
    assert dl_row["unmatched_files"] == [str(f_wrong)]
    assert dl_row["error_message"] == "1 file(s) couldn't be matched — manual import required"
    assert not f_dup.exists()
    assert f_wrong.exists()

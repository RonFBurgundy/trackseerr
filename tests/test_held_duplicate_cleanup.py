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
    MusicRequest,
    RequestStatus,
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
    db.upsert_user("u1", "user", "u@example.com", is_admin=True)
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
        title="Play in the Sunshine",
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


def test_gone_file_not_owned_fails_row(tmp_path: Path, db: Database):
    music, staging, artist, album_a, album_b, trk_live, trk_studio = _seed_prince(db, tmp_path, cutoff_met=True, with_download=False)
    db.create_download_client(
        DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    db.create_request(
        MusicRequest(
            id="req-missing",
            user_id="u1",
            item_type="track",
            title="Starfish and Coffee",
            artist="Prince",
            status=RequestStatus.PENDING,
        )
    )
    trk_missing = db.upsert_library_track(LibraryTrack(
        id="trk-missing",
        album_id="alb-a",
        artist_id="art-p",
        title="Starfish and Coffee",
        track_number=2,
        disc_number=1,
        monitored=True,
    ))
    db.create_active_download(
        ActiveDownload(
            id="dl-1",
            client_id="c1",
            title="Prince.Sign.O.The.Times.Live.FLAC",
            artist="Prince",
            item_type="track",
            status=DownloadStatus.WARNING.value,
            download_hash="h1",
            album_id="alb-a",
            track_id="trk-missing",
            request_id="req-missing",
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
    assert dl_row["status"] == DownloadStatus.FAILED.value
    assert dl_row["error_message"] == "The downloaded file is no longer on disk"
    assert dl_row["unmatched_files"] == []
    req_after = db.get_request("req-missing")
    assert req_after["status"] == "pending"
    assert not db.is_blocklisted(release_title="Prince.Sign.O.The.Times.Live.FLAC")


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


def test_wrong_song_owned_elsewhere_clears_row_but_keeps_wanted_track(tmp_path: Path, db: Database):
    music, staging, artist, album_a, album_b, trk_live, trk_studio = _seed_prince(db, tmp_path, cutoff_met=True, with_download=False)
    db.create_download_client(
        DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    db.create_request(
        MusicRequest(
            id="req-time",
            user_id="u1",
            item_type="track",
            title="Time",
            artist="Prince",
            status=RequestStatus.PENDING,
        )
    )
    trk_time = db.upsert_library_track(LibraryTrack(
        id="trk-time",
        album_id="alb-a",
        artist_id="art-p",
        title="Time",
        track_number=2,
        disc_number=1,
        monitored=True,
    ))
    trk_all = db.upsert_library_track(LibraryTrack(
        id="trk-all",
        album_id="alb-b",
        artist_id="art-p",
        title="All of the Time",
        track_number=2,
        disc_number=1,
        monitored=False,
    ))
    db.upsert_library_file(LibraryFile(
        id="f-all",
        track_id="trk-all",
        file_path=str(music / "Prince" / "Sign O The Times" / "AllOfTheTime.flac"),
        relative_path="AllOfTheTime.flac",
        codec="FLAC",
        size_bytes=1,
        cutoff_met=True,
    ))

    dl = staging / "Prince.Time.FLAC"
    f = dl / "held.flac"
    _flac(f)

    db.create_active_download(
        ActiveDownload(
            id="dl-1",
            client_id="c1",
            title="Prince.Time.FLAC",
            artist="Prince",
            item_type="track",
            status=DownloadStatus.WARNING.value,
            download_hash="h1",
            album_id="alb-a",
            track_id="trk-time",
            request_id="req-time",
        )
    )
    db.set_download_unmatched_files("dl-1", [str(f)])
    db.update_download_status("dl-1", status=DownloadStatus.WARNING.value, error_message="1 file(s) couldn't be matched — manual import required")

    driver = MagicMock()
    metas = {"held.flac": {"artist": "Prince", "title": "All of the Time"}}

    def inspect(path):
        base = {
            "artist": "Prince",
            "album": "Sign O' The Times",
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
    assert dl_row["error_message"] == "Discarded a file that is a different song you already own"
    assert dl_row["unmatched_files"] == []
    assert not f.exists()
    assert db.get_library_track("trk-time")["monitored"] is True
    assert db.get_request("req-time")["status"] == "pending"


def test_gone_file_resolved_by_request_title(tmp_path: Path, db: Database):
    music, staging, artist, album_a, album_b, trk_live, trk_studio = _seed_prince(db, tmp_path, cutoff_met=True, with_download=False)
    db.create_download_client(
        DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    db.create_request(
        MusicRequest(
            id="req-prince",
            user_id="u1",
            item_type="track",
            title="Play In The Sunshine - 2020 Remaster",
            artist="Prince",
            status=RequestStatus.PENDING,
        )
    )
    absent = staging / "absent.flac"
    db.create_active_download(
        ActiveDownload(
            id="dl-1",
            client_id="c1",
            title="Prince - Play In The Sunshine (2020 Remaster) [FLAC 24bit 44.1kHz]",
            artist="Prince",
            item_type="track",
            status=DownloadStatus.WARNING.value,
            download_hash="h1",
            track_id=None,
            request_id="req-prince",
        )
    )
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
    req_after = db.get_request("req-prince")
    assert req_after["status"] == "available"


def test_unmonitor_respects_duration(tmp_path: Path, db: Database):
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
        }
    )
    artist = db.upsert_library_artist(LibraryArtist(id="art-1", name="Artist", path=str(music / "Artist")))
    alb_a = db.upsert_library_album(LibraryAlbum(id="alb-a", artist_id="art-1", title="Album A", year=2020))
    alb_b = db.upsert_library_album(LibraryAlbum(id="alb-b", artist_id="art-1", title="Album B", year=2021))

    trk_wanted = db.upsert_library_track(LibraryTrack(
        id="trk-wanted",
        album_id="alb-a",
        artist_id="art-1",
        title="Time",
        duration_seconds=251.0,
        monitored=True,
    ))
    trk_owned = db.upsert_library_track(LibraryTrack(
        id="trk-owned",
        album_id="alb-b",
        artist_id="art-1",
        title="Time",
        duration_seconds=213.0,
        monitored=True,
    ))
    db.upsert_library_file(LibraryFile(
        id="f-owned",
        track_id="trk-owned",
        file_path=str(music / "Artist" / "Album B" / "Time.flac"),
        relative_path="Time.flac",
        codec="FLAC",
        size_bytes=1,
        cutoff_met=True,
    ))

    db.create_download_client(
        DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    dl = staging / "Artist.Time.FLAC"
    f = dl / "Time.flac"
    _flac(f)

    db.create_active_download(
        ActiveDownload(
            id="dl-1",
            client_id="c1",
            title="Artist.Time.FLAC",
            artist="Artist",
            item_type="track",
            status=DownloadStatus.WARNING.value,
            download_hash="h1",
            album_id="alb-a",
            track_id="trk-wanted",
        )
    )
    db.set_download_unmatched_files("dl-1", [str(f)])
    db.update_download_status("dl-1", status=DownloadStatus.WARNING.value, error_message="1 file(s) couldn't be matched — manual import required")

    driver = MagicMock()
    metas = {"Time.flac": {"artist": "Artist", "title": "Time", "duration_seconds": 213.0}}

    def inspect(path):
        base = {
            "artist": "Artist",
            "album": "Album B",
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
    assert dl_row["error_message"] == "Discarded a file that is a different song you already own"
    assert db.get_library_track("trk-wanted")["monitored"] is True


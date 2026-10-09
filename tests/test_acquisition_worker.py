"""Integration and unit tests for AcquisitionWorker and safe atomic library placement."""

from tests.audio_fixtures import write_flac, write_mp3
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from trackseerr.acquisition_worker import (
    AcquisitionWorker,
)
from trackseerr.import_files import (
    place_audio_file,
    safe_atomic_move,
)
from trackseerr.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    MediaIssue,
    MusicRequest,
    RequestStatus,
)
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def workspace_dirs(tmp_path):
    downloads_dir = tmp_path / "downloads"
    music_dir = tmp_path / "music"
    downloads_dir.mkdir()
    music_dir.mkdir()
    return downloads_dir, music_dir


# ---------------------------------------------------------------------------
# safe_atomic_move & place_audio_file Tests
# ---------------------------------------------------------------------------
def test_safe_atomic_move_success(tmp_path):
    src = tmp_path / "temp_download.flac"
    src.write_text("audio sample binary content")

    dst = tmp_path / "library" / "Artist" / "Album" / "track.flac"

    result_path = safe_atomic_move(src, dst)
    assert result_path == dst
    assert dst.exists()
    assert not src.exists()
    assert dst.read_text() == "audio sample binary content"


def test_safe_atomic_move_missing_src(tmp_path):
    src = tmp_path / "nonexistent.mp3"
    dst = tmp_path / "library" / "track.mp3"
    with pytest.raises(FileNotFoundError):
        safe_atomic_move(src, dst)


def test_place_audio_file_hardlink(tmp_path):
    src = tmp_path / "downloads" / "track.flac"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("audio stream data")

    dst = tmp_path / "media" / "music" / "track.flac"
    res = place_audio_file(src, dst, mode="hardlink")

    assert res == dst
    assert dst.exists()
    assert src.exists()  # Crucial for torrent seeding!
    assert os.stat(src).st_ino == os.stat(dst).st_ino
    assert dst.read_text() == "audio stream data"


def test_place_audio_file_move(tmp_path):
    src = tmp_path / "downloads" / "track.flac"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("audio stream data")

    dst = tmp_path / "media" / "music" / "track.flac"
    res = place_audio_file(src, dst, mode="move")

    assert res == dst
    assert dst.exists()
    assert not src.exists()
    assert dst.read_text() == "audio stream data"


def test_place_audio_file_hardlink_fallback_on_oserror(tmp_path):
    src = tmp_path / "downloads" / "track.flac"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("fallback stream content")

    dst = tmp_path / "media" / "music" / "track.flac"
    with patch("os.link", side_effect=OSError("Cross-device link")):
        res = place_audio_file(src, dst, mode="hardlink")

    assert res == dst
    assert dst.exists()
    assert src.exists()
    assert dst.read_text() == "fallback stream content"


# ---------------------------------------------------------------------------
# AcquisitionWorker Tests
# ---------------------------------------------------------------------------
def test_worker_poll_once_downloading(test_db, workspace_dirs):
    downloads_dir, music_dir = workspace_dirs

    # Seed client
    client = test_db.create_download_client(
        DownloadClientConfig(
            id="client-slskd-1",
            name="Test Slskd",
            driver_type=DownloadDriverType.SLSKD,
            host_url="http://slskd:5030",
        )
    )

    # Seed active download
    download = test_db.create_active_download(
        ActiveDownload(
            id="dl-active-1",
            title="Daft Punk - One More Time",
            artist="Daft Punk",
            client_id="client-slskd-1",
            download_hash="dl-hash-123",
            status=DownloadStatus.DOWNLOADING.value,
        )
    )

    worker = AcquisitionWorker()

    mock_driver = MagicMock()
    mock_driver.get_status.return_value = {
        "status": DownloadStatus.DOWNLOADING.value,
        "progress": 55.0,
        "size_bytes": 10000000,
        "speed_bps": 250000,
        "eta_seconds": 30,
        "source_path": None,
        "error_message": None,
    }

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
        stats = worker.poll_once(db=test_db, staging_dir=str(downloads_dir))
        assert stats["polled"] == 1

    updated = test_db.get_active_download("dl-active-1")
    assert updated is not None
    assert updated["progress"] == 55.0


def test_worker_poll_once_completed_and_organizes(test_db, workspace_dirs):
    downloads_dir, music_dir = workspace_dirs

    # Seed client
    client = test_db.create_download_client(
        DownloadClientConfig(
            id="client-sab-1",
            name="Test SABnzbd",
            driver_type=DownloadDriverType.SABNZBD,
            host_url="http://sabnzbd:8080",
            api_key="secret",
        )
    )

    # Seed user and request
    user = test_db.upsert_user("user-1", "dj_bob", "bob@example.com")
    req = test_db.create_request(
        MusicRequest(
            id="req-101",
            user_id="user-1",
            item_type="track",
            title="Get Lucky",
            artist="Daft Punk",
            album="Random Access Memories",
            status=RequestStatus.PROCESSING,
        )
    )

    # Seed active download linked to request
    download = test_db.create_active_download(
        ActiveDownload(
            id="dl-sab-101",
            title="Get Lucky",
            artist="Daft Punk",
            client_id="client-sab-1",
            download_hash="nzo-999",
            status=DownloadStatus.DOWNLOADING.value,
            request_id="req-101",
        )
    )

    # Create dummy downloaded audio file in downloads directory
    dl_file = downloads_dir / "03 - Get Lucky.mp3"
    write_mp3(dl_file)

    mock_driver = MagicMock()
    mock_driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value,
        "progress": 100.0,
        "size_bytes": dl_file.stat().st_size,
        "speed_bps": 0,
        "eta_seconds": 0,
        "source_path": str(dl_file),
        "error_message": None,
    }

    mock_plex = MagicMock()
    worker = AcquisitionWorker()

    mock_meta = {
        "artist": "Daft Punk",
        "title": "Get Lucky",
        "album": "Random Access Memories",
        "file_path": str(dl_file),
        "extension": ".mp3",
        "track_number": 3,
        "year": 2013,
        "disc_number": 1,
        "total_discs": 1,
    }

    # Set media management root path to music_dir
    settings = test_db.get_media_management_settings()
    settings["root_folder_path"] = str(music_dir)
    test_db.update_media_management_settings(settings)

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
        with patch("trackseerr.acquisition_worker.inspect_audio_file", return_value=mock_meta):
            stats = worker.poll_once(db=test_db, plex_client=mock_plex, staging_dir=str(downloads_dir))
            assert stats["completed"] == 1
            assert stats["imported"] == 1

    # Verify download record marked IMPORTED
    updated_dl = test_db.get_active_download("dl-sab-101")
    assert updated_dl is not None
    assert updated_dl["status"] == DownloadStatus.IMPORTED.value
    assert updated_dl["target_path"] is not None
    assert os.path.exists(updated_dl["target_path"])

    # Verify linked request marked AVAILABLE
    updated_req = test_db.get_request("req-101")
    assert updated_req is not None
    assert updated_req["status"] == RequestStatus.AVAILABLE.value

    # Verify Plex library refresh pinged
    mock_plex.refresh_music_library.assert_called_once()


def test_import_of_replacement_grab_comments_on_issue(test_db, workspace_dirs):
    downloads_dir, music_dir = workspace_dirs

    # Seed client
    client = test_db.create_download_client(
        DownloadClientConfig(
            id="client-sab-1",
            name="Test SABnzbd",
            driver_type=DownloadDriverType.SABNZBD,
            host_url="http://sabnzbd:8080",
            api_key="secret",
        )
    )

    # Seed user and request
    user = test_db.upsert_user("user-1", "dj_bob", "bob@example.com")
    req = test_db.create_request(
        MusicRequest(
            id="req-101",
            user_id="user-1",
            item_type="track",
            title="Get Lucky",
            artist="Daft Punk",
            album="Random Access Memories",
            status=RequestStatus.PROCESSING,
        )
    )

    # Seed active download linked to request
    download = test_db.create_active_download(
        ActiveDownload(
            id="dl-sab-101",
            title="Get Lucky",
            artist="Daft Punk",
            client_id="client-sab-1",
            download_hash="nzo-999",
            status=DownloadStatus.DOWNLOADING.value,
            request_id="req-101",
        )
    )

    test_db.create_issue(
        MediaIssue(
            id="iss-1", user_id="user-1", media_title="Get Lucky", artist="Daft Punk",
            issue_type="corrupted_file", problem_details="skips",
        )
    )
    test_db.record_download_grab("dl-sab-101", replacement_issue_id="iss-1")
    # Create dummy downloaded audio file in downloads directory
    dl_file = downloads_dir / "03 - Get Lucky.mp3"
    write_mp3(dl_file)

    mock_driver = MagicMock()
    mock_driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value,
        "progress": 100.0,
        "size_bytes": dl_file.stat().st_size,
        "speed_bps": 0,
        "eta_seconds": 0,
        "source_path": str(dl_file),
        "error_message": None,
    }

    mock_plex = MagicMock()
    worker = AcquisitionWorker()

    mock_meta = {
        "artist": "Daft Punk",
        "title": "Get Lucky",
        "album": "Random Access Memories",
        "file_path": str(dl_file),
        "extension": ".mp3",
        "track_number": 3,
        "year": 2013,
        "disc_number": 1,
        "total_discs": 1,
    }

    # Set media management root path to music_dir
    settings = test_db.get_media_management_settings()
    settings["root_folder_path"] = str(music_dir)
    test_db.update_media_management_settings(settings)

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
        with patch("trackseerr.acquisition_worker.inspect_audio_file", return_value=mock_meta):
            stats = worker.poll_once(db=test_db, plex_client=mock_plex, staging_dir=str(downloads_dir))
            assert stats["completed"] == 1
            assert stats["imported"] == 1

    assert test_db.get_issue("iss-1")["status"] == "open"  # the admin decides; nothing auto-resolves
    comments = test_db.list_issue_comments("iss-1")
    assert [c["body"] for c in comments] == ["Replacement imported"] and comments[0]["is_system"]


def test_worker_poll_once_failed(test_db, workspace_dirs):
    downloads_dir, music_dir = workspace_dirs

    client = test_db.create_download_client(
        DownloadClientConfig(
            id="client-qbit-1",
            name="Test Qbit",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://qbit:8080",
        )
    )

    download = test_db.create_active_download(
        ActiveDownload(
            id="dl-bad-1",
            title="Corrupted Torrent",
            artist="Unknown Artist",
            client_id="client-qbit-1",
            download_hash="hash-bad",
            status=DownloadStatus.DOWNLOADING.value,
        )
    )

    mock_driver = MagicMock()
    mock_driver.get_status.return_value = {
        "status": DownloadStatus.FAILED.value,
        "progress": 0.0,
        "error_message": "CRC check failed",
    }

    worker = AcquisitionWorker()

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
        stats = worker.poll_once(db=test_db, staging_dir=str(downloads_dir))
        assert stats["failed"] == 1

    updated = test_db.get_active_download("dl-bad-1")
    assert updated["status"] == DownloadStatus.FAILED.value
    assert updated["error_message"] == "CRC check failed"


def test_worker_lifecycle(test_db, workspace_dirs):
    downloads_dir, _ = workspace_dirs
    worker = AcquisitionWorker()

    started = worker.start(
        db=test_db,
        poll_interval=0.1,
        staging_dir=str(downloads_dir),
    )
    assert started is True
    assert worker.is_running() is True

    worker.stop(timeout=1.0)
    assert worker.is_running() is False


def test_worker_rejects_candidate_src_outside_staging(test_db, workspace_dirs, tmp_path):
    downloads_dir, music_dir = workspace_dirs

    # Create dummy audio file outside the staging directory
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    evil_file = outside_dir / "secret.mp3"
    write_mp3(evil_file)

    client = test_db.create_download_client(
        DownloadClientConfig(
            id="client-test-1",
            name="Test Client",
            driver_type=DownloadDriverType.SLSKD,
            host_url="http://slskd:5030",
        )
    )

    download = test_db.create_active_download(
        ActiveDownload(
            id="dl-traversal-src",
            title="Sneaky Track",
            artist="Attacker",
            client_id="client-test-1",
            download_hash="hash-traversal",
            status=DownloadStatus.COMPLETED.value,
            source_path=str(evil_file),
        )
    )

    mock_driver = MagicMock()
    mock_driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value,
        "progress": 100.0,
        "source_path": str(evil_file),
        "error_message": None,
    }

    worker = AcquisitionWorker()

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
        stats = worker.poll_once(db=test_db, staging_dir=str(downloads_dir))
        assert stats["failed"] == 1
        assert stats["imported"] == 0

    updated = test_db.get_active_download("dl-traversal-src")
    assert updated["status"] == DownloadStatus.FAILED.value
    assert "No audio files found" in updated["error_message"]
    # Ensure source was not moved
    assert evil_file.exists()


def test_worker_rejects_target_escaping_root_folder(test_db, workspace_dirs):
    downloads_dir, music_dir = workspace_dirs

    # Valid audio file in staging
    audio_file = downloads_dir / "valid_track.mp3"
    write_mp3(audio_file)

    client = test_db.create_download_client(
        DownloadClientConfig(
            id="client-test-2",
            name="Test Client",
            driver_type=DownloadDriverType.SLSKD,
            host_url="http://slskd:5030",
        )
    )

    download = test_db.create_active_download(
        ActiveDownload(
            id="dl-traversal-dst",
            title="Escape Root",
            artist="Attacker",
            client_id="client-test-2",
            download_hash="hash-esc",
            status=DownloadStatus.COMPLETED.value,
            source_path=str(audio_file),
        )
    )

    mock_driver = MagicMock()
    mock_driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value,
        "progress": 100.0,
        "source_path": str(audio_file),
        "error_message": None,
    }

    settings = test_db.get_media_management_settings()
    settings["root_folder_path"] = str(music_dir)
    test_db.update_media_management_settings(settings)

    worker = AcquisitionWorker()

    # Mock build_track_path to return a path outside music_dir (e.g. /etc/cron.d/evil.mp3)
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
        with patch("trackseerr.acquisition_worker.build_track_path", return_value="/etc/cron.d/evil.mp3"):
            stats = worker.poll_once(db=test_db, staging_dir=str(downloads_dir))
            assert stats["failed"] == 1
            assert stats["imported"] == 0

    updated = test_db.get_active_download("dl-traversal-dst")
    assert updated["status"] == DownloadStatus.FAILED.value
    assert "Destination escaped music root" in updated["error_message"]
    # Source file remains safe
    assert audio_file.exists()


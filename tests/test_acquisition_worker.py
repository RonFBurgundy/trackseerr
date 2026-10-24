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
from trackseerr.library import build_tags_to_write
from trackseerr.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    MediaIssue,
    MusicRequest,
    RequestStatus,
)
from trackseerr.media_servers import refresh_scheduler
from trackseerr.storage import Database


@pytest.fixture(autouse=True)
def _reset_refresh_scheduler():
    refresh_scheduler.reset_for_tests()
    yield
    refresh_scheduler.reset_for_tests()


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
    mock_sec = MagicMock(type="artist", locations=[str(music_dir)], title="Music")
    mock_plex.server.library.sections.return_value = [mock_sec]
    mock_plex.refresh_music_paths.return_value = 1
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
        with patch("trackseerr.acquisition_import.inspect_audio_file", return_value=mock_meta), \
             patch("trackseerr.acquisition_catalog.inspect_audio_file", return_value=mock_meta):
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

    # Verify Plex library refresh pinged with album folder
    mock_plex.refresh_music_library.assert_not_called()
    assert refresh_scheduler.flush_now() is True
    expected_album_dir = str(Path(updated_dl["target_path"]).parent)
    mock_plex.refresh_music_paths.assert_called_once_with([expected_album_dir])
    mock_plex.refresh_music_library.assert_not_called()


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
        with patch("trackseerr.acquisition_import.inspect_audio_file", return_value=mock_meta), \
             patch("trackseerr.acquisition_catalog.inspect_audio_file", return_value=mock_meta):
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
        with patch("trackseerr.acquisition_import.build_track_path", return_value="/etc/cron.d/evil.mp3"):
            stats = worker.poll_once(db=test_db, staging_dir=str(downloads_dir))
            assert stats["failed"] == 1
            assert stats["imported"] == 0

    updated = test_db.get_active_download("dl-traversal-dst")
    assert updated["status"] == DownloadStatus.FAILED.value
    assert "Destination escaped music root" in updated["error_message"]
    # Source file remains safe
    assert audio_file.exists()



# ---------------------------------------------------------------------------
# slskd stall detection
# ---------------------------------------------------------------------------
def _seed_slskd_row(
    db, driver_type, created_min_ago, progress=0.0, moved_min_ago=None, status="queued", extra_settings_json=None
):
    db.create_download_client(
        DownloadClientConfig(
            id="cl-1", name="Client", driver_type=driver_type, host_url="http://slskd:5030",
            extra_settings_json=extra_settings_json,
        )
    )
    db.create_active_download(
        ActiveDownload(
            id="dl-s1",
            title="Artist - Song",
            artist="Artist",
            client_id="cl-1",
            download_hash="user1::Music%2Fsong.flac",
            status=status,
        )
    )
    moved = moved_min_ago if moved_min_ago is not None else created_min_ago
    db.conn.execute(
        "UPDATE active_downloads SET progress = ?, "
        "created_at = datetime('now', ?), progress_updated_at = datetime('now', ?) WHERE id = 'dl-s1'",
        (progress, f"-{created_min_ago} minutes", f"-{moved} minutes"),
    )
    db.conn.commit()


def _poll_slskd(db, tmp_path, status_dict):
    driver = MagicMock()
    driver.get_status.return_value = {"size_bytes": 1000, "error_message": None, **status_dict}
    driver.cancel.return_value = True
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver):
        AcquisitionWorker().poll_once(db=db, staging_dir=str(tmp_path))
    return driver, db.get_active_download("dl-s1")


def test_slskd_stalled_queued_download_fails_and_cancels(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.SLSKD, 45)
    driver, row = _poll_slskd(
        test_db, tmp_path, {"status": "queued", "progress": 0.0, "remote_state": "Queued, Remotely"}
    )
    assert row["status"] == DownloadStatus.FAILED.value
    assert "Peer never started" in row["error_message"]
    driver.cancel.assert_called_once_with("user1::Music%2Fsong.flac")
    entries = test_db.list_blocklist()
    assert [e["release_guid"] for e in entries] == ["user1::Music%2Fsong.flac"]


def _poll_slskd_research(test_db, tmp_path, status_dict, track_id):
    _seed_slskd_row(test_db, DownloadDriverType.SLSKD, 45)
    if track_id:
        test_db.conn.execute("PRAGMA foreign_keys = OFF")
        test_db.conn.execute("UPDATE active_downloads SET track_id = ? WHERE id = 'dl-s1'", (track_id,))
        test_db.conn.commit()
    target = {"track_id": track_id or "t1"}
    with patch("trackseerr.backlog_worker.backlog_worker.queue_wanted_search", return_value={"queued": 1}) as q, \
            patch.object(Database, "list_wanted_search_targets", return_value=[target]):
        _poll_slskd(test_db, tmp_path, status_dict)
    return q, target


def test_slskd_stall_queues_research_for_track(test_db, tmp_path):
    q, target = _poll_slskd_research(
        test_db, tmp_path, {"status": "queued", "progress": 0.0, "remote_state": "Queued, Remotely"}, "trk-1"
    )
    q.assert_called_once()
    assert q.call_args.args[1] == [target]


def test_slskd_stall_without_track_id_no_research(test_db, tmp_path):
    q, _ = _poll_slskd_research(test_db, tmp_path, {"status": "queued", "progress": 0.0}, None)
    q.assert_not_called()


def test_slskd_plain_failure_also_researches(test_db, tmp_path):
    q, _ = _poll_slskd_research(
        test_db, tmp_path, {"status": "failed", "progress": 0.0, "error_message": "refused"}, "trk-1"
    )
    q.assert_called_once()


def test_slskd_queue_timeout_default_15(test_db, tmp_path, monkeypatch):
    monkeypatch.delenv("SLSKD_QUEUE_TIMEOUT_MINUTES", raising=False)
    _seed_slskd_row(test_db, DownloadDriverType.SLSKD, 20)
    _, row = _poll_slskd(test_db, tmp_path, {"status": "queued", "progress": 0.0})
    assert row["status"] == DownloadStatus.FAILED.value
    assert "after 15 min" in row["error_message"]


def test_slskd_missing_transfer_fails_after_grace(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.SLSKD, 20)
    _, row = _poll_slskd(test_db, tmp_path, {"status": "queued", "progress": 0.0, "missing": True})
    assert row["status"] == DownloadStatus.FAILED.value
    assert "disappeared" in row["error_message"]


def test_slskd_recent_queued_download_untouched(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.SLSKD, 5)
    driver, row = _poll_slskd(test_db, tmp_path, {"status": "queued", "progress": 0.0, "missing": True})
    assert row["status"] != DownloadStatus.FAILED.value
    driver.cancel.assert_not_called()


def test_slskd_in_progress_without_movement_fails(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.SLSKD, 60, progress=40.0, moved_min_ago=20, status="downloading")
    _, row = _poll_slskd(test_db, tmp_path, {"status": "downloading", "progress": 40.0})
    assert row["status"] == DownloadStatus.FAILED.value
    assert "stalled at 40%" in row["error_message"]


def test_slskd_in_progress_moving_untouched(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.SLSKD, 60, progress=40.0, moved_min_ago=20, status="downloading")
    driver, row = _poll_slskd(test_db, tmp_path, {"status": "downloading", "progress": 55.0})
    assert row["status"] == DownloadStatus.DOWNLOADING.value
    driver.cancel.assert_not_called()


def test_slskd_queue_timeout_env_override(test_db, tmp_path, monkeypatch):
    monkeypatch.setenv("SLSKD_QUEUE_TIMEOUT_MINUTES", "60")
    _seed_slskd_row(test_db, DownloadDriverType.SLSKD, 45)
    driver, row = _poll_slskd(test_db, tmp_path, {"status": "queued", "progress": 0.0})
    assert row["status"] != DownloadStatus.FAILED.value
    driver.cancel.assert_not_called()


def test_torrent_queued_under_dead_limit_untouched(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.QBITTORRENT, 60)
    driver, row = _poll_slskd(test_db, tmp_path, {"status": "queued", "progress": 0.0, "missing": True})
    assert row["status"] != DownloadStatus.FAILED.value
    driver.cancel.assert_not_called()


def test_slskd_failed_folder_cancels_remaining(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.SLSKD, 5, progress=30.0, status="downloading")
    driver, row = _poll_slskd(
        test_db, tmp_path, {"status": "failed", "progress": 30.0, "error_message": "1 of 3 files failed: x"}
    )
    assert row["status"] == DownloadStatus.FAILED.value
    driver.cancel.assert_called_once_with("user1::Music%2Fsong.flac")
    assert len(test_db.list_blocklist()) == 1


def test_slskd_stalled_not_cancelled_twice(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.SLSKD, 45)
    driver, row = _poll_slskd(test_db, tmp_path, {"status": "queued", "progress": 0.0})
    assert row["status"] == DownloadStatus.FAILED.value
    assert driver.cancel.call_count == 1


def test_slskd_remote_state_persisted(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.SLSKD, 5)
    _, row = _poll_slskd(
        test_db, tmp_path, {"status": "queued", "progress": 0.0, "remote_state": "Queued, Remotely"}
    )
    assert row["remote_state"] == "Queued, Remotely"


def test_slskd_client_timeout_overrides_env(test_db, tmp_path):
    _seed_slskd_row(
        test_db, DownloadDriverType.SLSKD, 45, extra_settings_json='{"queue_timeout_minutes": 60}'
    )
    driver, row = _poll_slskd(test_db, tmp_path, {"status": "queued", "progress": 0.0})
    assert row["status"] != DownloadStatus.FAILED.value
    driver.cancel.assert_not_called()


@pytest.mark.parametrize("extra", ["not json", '{"queue_timeout_minutes": -5}', '{"queue_timeout_minutes": "x"}', "[1]"])
def test_slskd_invalid_client_timeout_falls_back(test_db, tmp_path, extra):
    _seed_slskd_row(test_db, DownloadDriverType.SLSKD, 45, extra_settings_json=extra)
    driver, row = _poll_slskd(test_db, tmp_path, {"status": "queued", "progress": 0.0})
    assert row["status"] == DownloadStatus.FAILED.value
    driver.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# Dead torrents and re-search after any failed download
# ---------------------------------------------------------------------------
def _set_row(db, **cols):
    db.conn.execute("PRAGMA foreign_keys = OFF")
    for k, v in cols.items():
        db.conn.execute(f"UPDATE active_downloads SET {k} = ? WHERE id = 'dl-s1'", (v,))
    db.conn.commit()


def test_dead_torrent_zero_progress_fails_cancels_and_researches(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.QBITTORRENT, 150)
    _set_row(test_db, track_id="trk-1")
    with patch("trackseerr.backlog_worker.backlog_worker.queue_wanted_search", return_value={"queued": 1}) as q, \
            patch.object(Database, "list_wanted_search_targets", return_value=[{"track_id": "trk-1"}]):
        driver, row = _poll_slskd(test_db, tmp_path, {"status": "queued", "progress": 0.0})
    assert row["status"] == DownloadStatus.FAILED.value
    assert "never started" in row["error_message"]
    driver.cancel.assert_called_once()
    q.assert_called_once()


def test_torrent_with_progress_never_cancelled(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.QBITTORRENT, 600, progress=0.5, status="downloading")
    driver, row = _poll_slskd(test_db, tmp_path, {"status": "downloading", "progress": 0.5})
    assert row["status"] != DownloadStatus.FAILED.value
    driver.cancel.assert_not_called()


def test_torrent_seeding_state_never_cancelled(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.QBITTORRENT, 600)
    driver, row = _poll_slskd(
        test_db, tmp_path, {"status": "downloading", "progress": 0.0, "ratio": 1.2, "seeding_time_seconds": 50}
    )
    assert row["status"] != DownloadStatus.FAILED.value
    driver.cancel.assert_not_called()


def test_torrent_dead_minutes_client_override(test_db, tmp_path):
    _seed_slskd_row(
        test_db, DownloadDriverType.QBITTORRENT, 45, extra_settings_json='{"dead_torrent_minutes": 30}'
    )
    _, row = _poll_slskd(test_db, tmp_path, {"status": "queued", "progress": 0.0})
    assert row["status"] == DownloadStatus.FAILED.value


def test_usenet_failure_researches_track(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.SABNZBD, 5)
    _set_row(test_db, track_id="trk-1")
    with patch("trackseerr.backlog_worker.backlog_worker.queue_wanted_search", return_value={"queued": 1}) as q, \
            patch.object(Database, "list_wanted_search_targets", return_value=[{"track_id": "trk-1"}]):
        driver, row = _poll_slskd(test_db, tmp_path, {"status": "failed", "error_message": "Unpack failed"})
    assert row["status"] == DownloadStatus.FAILED.value
    q.assert_called_once()
    driver.cancel.assert_not_called()


def test_failed_album_download_researches_album_tracks(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.SABNZBD, 5)
    _set_row(test_db, album_id="alb-1")
    with patch("trackseerr.backlog_worker.backlog_worker.queue_wanted_search", return_value={"queued": 2}), \
            patch.object(Database, "list_library_tracks", return_value=[{"id": "t1"}, {"id": "t2"}]), \
            patch.object(Database, "list_wanted_search_targets", return_value=[{"track_id": "t1"}]) as lt:
        _poll_slskd(test_db, tmp_path, {"status": "failed", "error_message": "Unpack failed"})
    lt.assert_called_once_with(track_ids=["t1", "t2"])


def test_build_tags_to_write_with_album_artist():
    tags = build_tags_to_write(
        artist="Bee Gees",
        album_artist="Various Artists",
        album="Saturday Night Fever",
        title="Stayin' Alive",
    )
    assert tags["artist"] == "Bee Gees"
    assert tags["albumartist"] == "Various Artists"
    assert tags["album"] == "Saturday Night Fever"
    assert tags["title"] == "Stayin' Alive"


# ---------------------------------------------------------------------------
# slskd folder cleanup
# ---------------------------------------------------------------------------
def test_cancelled_slskd_failure_removes_transfer(test_db, tmp_path):
    _seed_slskd_row(test_db, DownloadDriverType.SLSKD, 5, progress=30.0, status="downloading")
    driver = MagicMock()
    driver.get_status.return_value = {
        "size_bytes": 1000, "status": "failed", "progress": 30.0, "cancelled": True, "error_message": "cancelled",
    }
    driver.cleanup_completed.return_value = True
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver):
        AcquisitionWorker().poll_once(db=test_db, staging_dir=str(tmp_path))
    assert test_db.get_active_download("dl-s1")["status"] == DownloadStatus.FAILED.value
    driver.cleanup_completed.assert_called_once_with("user1::Music%2Fsong.flac")
    driver.cancel.assert_not_called()


def test_move_import_prunes_emptied_source_dirs(test_db, workspace_dirs):
    from trackseerr import download_roots

    download_roots.clear_roots_cache()
    downloads_dir, music_dir = workspace_dirs
    test_db.create_download_client(
        DownloadClientConfig(
            id="client-slskd-1", name="slskd", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030"
        )
    )
    test_db.upsert_user("user-1", "dj_bob", "bob@example.com")
    test_db.create_request(
        MusicRequest(
            id="req-101", user_id="user-1", item_type="track", title="Get Lucky", artist="Daft Punk",
            album="Random Access Memories", status=RequestStatus.PROCESSING,
        )
    )
    test_db.create_active_download(
        ActiveDownload(
            id="dl-1", title="Get Lucky", artist="Daft Punk", client_id="client-slskd-1",
            download_hash="user1::Album", status=DownloadStatus.DOWNLOADING.value, request_id="req-101",
        )
    )
    album_dir = downloads_dir / "Album"
    album_dir.mkdir()
    dl_file = album_dir / "03 - Get Lucky.mp3"
    write_mp3(dl_file)
    driver = MagicMock()
    driver.get_download_roots.return_value = [str(downloads_dir)]
    driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value, "progress": 100.0, "size_bytes": dl_file.stat().st_size,
        "source_path": str(dl_file), "error_message": None,
    }
    meta = {
        "artist": "Daft Punk", "title": "Get Lucky", "album": "Random Access Memories",
        "file_path": str(dl_file), "extension": ".mp3", "track_number": 3, "year": 2013,
        "disc_number": 1, "total_discs": 1,
    }
    settings = test_db.get_media_management_settings()
    settings["root_folder_path"] = str(music_dir)
    test_db.update_media_management_settings(settings)
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
            patch("trackseerr.acquisition_import.inspect_audio_file", return_value=meta), \
            patch("trackseerr.acquisition_catalog.inspect_audio_file", return_value=meta):
        stats = AcquisitionWorker().poll_once(db=test_db, staging_dir=str(downloads_dir))
    download_roots.clear_roots_cache()
    assert stats["imported"] == 1
    row = test_db.get_active_download("dl-1")
    assert row["status"] == DownloadStatus.IMPORTED.value
    assert os.path.exists(row["target_path"])
    assert not dl_file.exists()
    assert not album_dir.exists()
    assert downloads_dir.is_dir()


def test_slskd_sweep_throttled_and_skips_library_overlap(test_db, workspace_dirs, tmp_path):
    downloads_dir, music_dir = workspace_dirs
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    test_db.create_download_client(
        DownloadClientConfig(id="cl-1", name="slskd", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
    )
    settings = test_db.get_media_management_settings()
    settings["root_folder_path"] = str(music_dir)
    test_db.update_media_management_settings(settings)
    driver = MagicMock()
    driver.get_incomplete_root.return_value = str(incomplete)
    worker = AcquisitionWorker()
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver), \
            patch("trackseerr.acquisition_worker.fetch_client_roots", return_value=([str(downloads_dir), str(music_dir)], None)), \
            patch("trackseerr.acquisition_worker.sweep_empty_dirs", return_value=0) as sweep:
        worker._poll_once(test_db)
        worker._poll_once(test_db)
    swept = sorted(call.args[0] for call in sweep.call_args_list)
    assert swept == sorted([downloads_dir.resolve(), incomplete.resolve()])
    assert all(Path(p) != music_dir.resolve() for p in swept)

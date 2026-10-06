"""Comprehensive unit and integration test suite for TrackSeerr Arr Lifecycle (Phase 5).

Covers:
1. Safe archive extraction (extract_archive, is_archive_file, path traversal defense).
2. Download client cleanup_completed implementation across QbittorrentDriver,
   SabnzbdDriver, SlskdDriver, and base AcquisitionDriver.
3. Database migration v14 schema fields (quality_profile_id, current_quality, cutoff_met,
   delete_completed_transfers, enable_quality_upgrades), cutoff-unmet queries, and updates.
4. Cutoff-unmet tracking and quality upgrades across AcquisitionWorker,
   WantedBacklogWorker, and RSSSyncWorker.
5. Media management settings API GET/POST for delete_completed_transfers and
   enable_quality_upgrades with RBAC enforcement.
"""

from __future__ import annotations

import io
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.acquisition_coordinator import (
    acquisition_coordinator,
    _to_quality_profile,
)
from plex_playlist_sync.acquisition_worker import AcquisitionWorker
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.backlog_worker import WantedBacklogWorker, RSSSyncWorker
from plex_playlist_sync.clients.acquisition.base import AcquisitionDriver
from plex_playlist_sync.clients.acquisition.qbittorrent import QbittorrentDriver
from plex_playlist_sync.clients.acquisition.sabnzbd import SabnzbdDriver
from plex_playlist_sync.clients.acquisition.slskd import SlskdDriver
from plex_playlist_sync.config import Config
from tests.audio_fixtures import write_flac, write_mp3
from plex_playlist_sync.library import extract_archive, is_archive_file
from plex_playlist_sync.models import (
    AcquisitionSearchResult,
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    MusicRequest,
    RequestStatus,
)
from plex_playlist_sync.storage import Database


# ---------------------------------------------------------------------------
# Test Fixtures and Helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def test_db() -> Database:
    """Provides an isolated in-memory Database instance."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path: Path) -> Config:
    """Provides a test Config pointing to tmp_path."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        data_dir=str(tmp_path),
        user_request_quota=10,
        auto_approve_requests=False,
    )


@pytest.fixture
def seeded_users(test_db: Database) -> dict[str, dict[str, Any]]:
    """Seeds admin and regular user accounts."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice}


@pytest.fixture
def app_and_client(test_db: Database, test_config: Config) -> tuple[Any, TestClient]:
    """Creates a FastAPI test client with injected test database and config."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    client = TestClient(app)
    return app, client


def _auth_headers(user: dict[str, Any], test_db: Database, config: Config) -> dict[str, str]:
    """Generates valid bearer authentication headers with an active session in test_db."""
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def _create_minimal_flac(path: Path) -> None:
    """Writes a valid minimal FLAC (STREAMINFO) that mutagen parses and the import security gate accepts."""
    write_flac(path)


def _create_minimal_mp3(path: Path) -> None:
    """Writes a few valid MPEG-1 Layer 3 frames that mutagen parses and the import security gate accepts."""
    write_mp3(path)


# ---------------------------------------------------------------------------
# 1. TestArchiveExtractor
# ---------------------------------------------------------------------------
class TestArchiveExtractor:
    """Unit tests for archive extraction and path traversal defenses in library.py."""

    def test_is_archive_file(self) -> None:
        """Verifies is_archive_file identifies supported archive extensions."""
        assert is_archive_file("music.zip") is True
        assert is_archive_file("album.tar.gz") is True
        assert is_archive_file("disc.tgz") is True
        assert is_archive_file("release.tar.bz2") is True
        assert is_archive_file("archive.tar") is True
        assert is_archive_file("song.flac") is False
        assert is_archive_file("track.mp3") is False
        assert is_archive_file("cover.jpg") is False

    def test_valid_zip_containing_flac_extracts_successfully(self, tmp_path: Path) -> None:
        """Verifies extracting a valid .zip containing audio files discovers only audio files."""
        zip_path = tmp_path / "album.zip"
        target_dir = tmp_path / "extracted_zip"
        target_dir.mkdir()

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("disc1/01_intro.flac", b"flac_mock_data_1")
            zf.writestr("disc1/02_song.flac", b"flac_mock_data_2")
            zf.writestr("disc1/artwork.jpg", b"image_mock_data")
            zf.writestr("release_notes.txt", b"txt_mock_data")

        audio_files = extract_archive(zip_path, target_dir)

        assert len(audio_files) == 2
        file_names = [f.name for f in audio_files]
        assert "01_intro.flac" in file_names
        assert "02_song.flac" in file_names
        assert (target_dir / "disc1" / "artwork.jpg").exists()
        assert (target_dir / "release_notes.txt").exists()

    def test_valid_tar_gz_containing_mp3_extracts_successfully(self, tmp_path: Path) -> None:
        """Verifies extracting a valid .tar.gz containing audio files discovers only audio files."""
        tar_path = tmp_path / "album.tar.gz"
        target_dir = tmp_path / "extracted_tar"
        target_dir.mkdir()

        with tarfile.open(tar_path, "w:gz") as tf:
            data_mp3 = b"mp3_mock_data_1"
            ti_mp3 = tarfile.TarInfo(name="album_dir/track.mp3")
            ti_mp3.size = len(data_mp3)
            tf.addfile(ti_mp3, io.BytesIO(data_mp3))

            data_txt = b"liner notes"
            ti_txt = tarfile.TarInfo(name="album_dir/info.nfo")
            ti_txt.size = len(data_txt)
            tf.addfile(ti_txt, io.BytesIO(data_txt))

        audio_files = extract_archive(tar_path, target_dir)

        assert len(audio_files) == 1
        assert audio_files[0].name == "track.mp3"
        assert (target_dir / "album_dir" / "info.nfo").exists()

    def test_path_traversal_in_zip_raises_value_error(self, tmp_path: Path) -> None:
        """Verifies path traversal attempt in zip archive raises ValueError."""
        zip_path = tmp_path / "evil.zip"
        target_dir = tmp_path / "extracted_evil_zip"
        target_dir.mkdir()

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../evil.flac", b"malicious payload")

        with pytest.raises(ValueError, match="Path traversal detected in zip archive"):
            extract_archive(zip_path, target_dir)

    def test_path_traversal_in_tar_raises_value_error(self, tmp_path: Path) -> None:
        """Verifies path traversal attempt in tar archive raises ValueError."""
        tar_path = tmp_path / "evil.tar.gz"
        target_dir = tmp_path / "extracted_evil_tar"
        target_dir.mkdir()

        with tarfile.open(tar_path, "w:gz") as tf:
            data = b"malicious tar payload"
            ti = tarfile.TarInfo(name="../evil.mp3")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))

        with pytest.raises(ValueError, match="Path traversal detected in tar archive"):
            extract_archive(tar_path, target_dir)

    def test_archive_extractor_error_handling(self, tmp_path: Path) -> None:
        """Verifies input validation: missing archive, missing target dir, non-directory target, unsupported format."""
        target_dir = tmp_path / "target"
        target_dir.mkdir()

        # Nonexistent archive
        with pytest.raises(FileNotFoundError, match="Archive file not found"):
            extract_archive(tmp_path / "ghost.zip", target_dir)

        # Nonexistent target directory
        dummy_zip = tmp_path / "test.zip"
        with zipfile.ZipFile(dummy_zip, "w") as zf:
            zf.writestr("test.flac", b"dummy")
        with pytest.raises(FileNotFoundError, match="Target directory does not exist"):
            extract_archive(dummy_zip, tmp_path / "nonexistent_dir")

        # Target is a file, not directory
        file_target = tmp_path / "file_not_dir.txt"
        file_target.touch()
        with pytest.raises(NotADirectoryError, match="Target path is not a directory"):
            extract_archive(dummy_zip, file_target)

        # Unsupported format
        unsupported = tmp_path / "archive.7z"
        unsupported.touch()
        with pytest.raises(ValueError, match="Unsupported archive format"):
            extract_archive(unsupported, target_dir)


# ---------------------------------------------------------------------------
# 2. TestClientCleanupCompleted
# ---------------------------------------------------------------------------
class TestClientCleanupCompleted:
    """Unit tests for cleanup_completed across acquisition drivers."""

    def test_base_driver_default_cleanup_completed(self) -> None:
        """Verifies base AcquisitionDriver provides default cleanup_completed returning False."""
        class MinimalDriver(AcquisitionDriver):
            def test_connection(self) -> tuple[bool, str]:
                return True, "ok"

            def search(self, query: str) -> list[AcquisitionSearchResult]:
                return []

            def download(self, item: AcquisitionSearchResult) -> str:
                return "id-123"

            def get_status(self, download_id: str) -> dict[str, Any]:
                return {}

            def cancel(self, download_id: str) -> bool:
                return True

        driver = MinimalDriver()
        assert driver.cleanup_completed("any-id", delete_files=False) is False

    def test_qbittorrent_cleanup_completed_success(self) -> None:
        """Verifies QbittorrentDriver.cleanup_completed posts to /api/v2/torrents/delete."""
        driver = QbittorrentDriver("http://qbittorrent.local:8080", "admin", "adminadmin")

        mock_login = MagicMock(status_code=200, text="Ok.", cookies={"SID": "session_sid"})
        mock_del = MagicMock(status_code=200)

        with patch("httpx.Client.post", side_effect=[mock_login, mock_del]) as mock_post:
            result = driver.cleanup_completed("HASH123ABC", delete_files=False)
            assert result is True
            assert mock_post.call_count == 2
            # Login was first call, delete was second call
            del_call = mock_post.call_args_list[1]
            assert del_call[0][0] == "http://qbittorrent.local:8080/api/v2/torrents/delete"
            assert del_call[1]["data"] == {"hashes": "hash123abc", "deleteFiles": "false"}

    def test_qbittorrent_cleanup_completed_delete_files_true(self) -> None:
        """Verifies QbittorrentDriver.cleanup_completed sets deleteFiles to true when requested."""
        driver = QbittorrentDriver("http://qbittorrent.local:8080", "admin", "adminadmin")
        driver._cookie = "SID=existing_cookie"

        mock_del = MagicMock(status_code=200)
        with patch("httpx.Client.post", return_value=mock_del) as mock_post:
            result = driver.cleanup_completed("hash456", delete_files=True)
            assert result is True
            mock_post.assert_called_once()
            assert mock_post.call_args[1]["data"] == {"hashes": "hash456", "deleteFiles": "true"}

    def test_qbittorrent_cleanup_completed_reauth_on_403(self) -> None:
        """Verifies QbittorrentDriver.cleanup_completed reauthenticates and retries on 403."""
        driver = QbittorrentDriver("http://qbittorrent.local:8080", "admin", "adminadmin")
        driver._cookie = "SID=expired_cookie"

        mock_403 = MagicMock(status_code=403)
        mock_login = MagicMock(status_code=200, text="Ok.", cookies={"SID": "new_cookie"})
        mock_200 = MagicMock(status_code=200)

        with patch("httpx.Client.post", side_effect=[mock_403, mock_login, mock_200]):
            result = driver.cleanup_completed("hash789", delete_files=False)
            assert result is True

    def test_qbittorrent_cleanup_completed_error_and_ssrf(self) -> None:
        """Verifies QbittorrentDriver.cleanup_completed handles exceptions and SSRF rejection."""
        # SSRF unsafe host
        unsafe_driver = QbittorrentDriver("http://169.254.169.254:8080", "admin", "pass")
        assert unsafe_driver.cleanup_completed("hash123") is False

        # Network error
        safe_driver = QbittorrentDriver("http://qbittorrent.local:8080", "admin", "pass")
        with patch("httpx.Client.post", side_effect=httpx.ConnectError("Connection refused")):
            assert safe_driver.cleanup_completed("hash123") is False

    def test_sabnzbd_cleanup_completed_success(self) -> None:
        """Verifies SabnzbdDriver.cleanup_completed calls history delete endpoint."""
        driver = SabnzbdDriver("http://sabnzbd.local:8080", api_key="secretkey")

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"status": True}

        with patch("httpx.Client.get", return_value=mock_resp) as mock_get:
            result = driver.cleanup_completed("SABnzbd_nzo_abc123", delete_files=False)
            assert result is True
            mock_get.assert_called_once()
            called_url = mock_get.call_args[0][0]
            assert "mode=history" in called_url
            assert "name=delete" in called_url
            assert "val=SABnzbd_nzo_abc123" in called_url
            assert "del_files=0" in called_url

    def test_sabnzbd_cleanup_completed_delete_files_true(self) -> None:
        """Verifies SabnzbdDriver.cleanup_completed passes del_files=1 when delete_files=True."""
        driver = SabnzbdDriver("http://sabnzbd.local:8080", api_key="secretkey")

        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"status": True}

        with patch("httpx.Client.get", return_value=mock_resp) as mock_get:
            result = driver.cleanup_completed("SABnzbd_nzo_abc123", delete_files=True)
            assert result is True
            called_url = mock_get.call_args[0][0]
            assert "del_files=1" in called_url

    def test_sabnzbd_cleanup_completed_status_false_and_error(self) -> None:
        """Verifies SabnzbdDriver.cleanup_completed handles status=False, HTTP 500, and SSRF."""
        driver = SabnzbdDriver("http://sabnzbd.local:8080", api_key="secretkey")

        # SABnzbd returns status: False
        mock_fail = MagicMock(status_code=200)
        mock_fail.json.return_value = {"status": False}
        with patch("httpx.Client.get", return_value=mock_fail):
            assert driver.cleanup_completed("nzo_fail") is False

        # SABnzbd HTTP 500
        mock_500 = MagicMock(status_code=500)
        with patch("httpx.Client.get", return_value=mock_500):
            assert driver.cleanup_completed("nzo_500") is False

        # SSRF unsafe host
        unsafe_driver = SabnzbdDriver("http://169.254.169.254:8080", api_key="secretkey")
        assert unsafe_driver.cleanup_completed("nzo_ssrf") is False

    def test_slskd_cleanup_completed_user_and_file(self) -> None:
        """Verifies SlskdDriver.cleanup_completed deletes download by user and path."""
        driver = SlskdDriver("http://slskd.local:5030", api_key="slskdkey")

        mock_resp = MagicMock(status_code=200)
        with patch("httpx.Client.request", return_value=mock_resp) as mock_req:
            res = driver.cleanup_completed("peeruser::music/album/song.flac", delete_files=False)
            assert res is True
            mock_req.assert_called_once()
            args, kwargs = mock_req.call_args
            assert args[0] == "DELETE"
            assert args[1] == "http://slskd.local:5030/api/v0/transfers/downloads/peeruser"
            assert kwargs["json"] == [{"filename": "music/album/song.flac"}]

    def test_slskd_cleanup_completed_no_separator_returns_true(self) -> None:
        """Verifies SlskdDriver.cleanup_completed returns True without request when '::' is absent."""
        driver = SlskdDriver("http://slskd.local:5030", api_key="slskdkey")
        with patch("httpx.Client.request") as mock_req:
            res = driver.cleanup_completed("hash_without_delimiter")
            assert res is True
            mock_req.assert_not_called()

    def test_slskd_cleanup_completed_exception_and_ssrf(self) -> None:
        """Verifies SlskdDriver.cleanup_completed returns True on request error and False on SSRF."""
        driver = SlskdDriver("http://slskd.local:5030", api_key="slskdkey")
        with patch("httpx.Client.request", side_effect=httpx.ConnectError("Transfer gone")):
            assert driver.cleanup_completed("user::file.mp3") is True

        unsafe_driver = SlskdDriver("http://169.254.169.254:5030", api_key="slskdkey")
        assert unsafe_driver.cleanup_completed("user::file.mp3") is False


# ---------------------------------------------------------------------------
# 3. TestStorageMigrationV14
# ---------------------------------------------------------------------------
class TestStorageMigrationV14:
    """Unit tests for database migration v14 schema additions and query methods."""

    def test_music_requests_v14_columns_and_defaults(self, test_db: Database, seeded_users: dict[str, Any]) -> None:
        """Verifies quality_profile_id, current_quality, and cutoff_met columns exist in music_requests."""
        cur = test_db.conn.execute("PRAGMA table_info(music_requests)")
        cols = {row[1]: row for row in cur.fetchall()}

        assert "quality_profile_id" in cols
        assert "current_quality" in cols
        assert "cutoff_met" in cols
        # cutoff_met default is 1
        assert cols["cutoff_met"][4] == "1"

        # Check index exists
        idx_cur = test_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_music_requests_cutoff'"
        )
        assert idx_cur.fetchone() is not None

        # Verify creating request stores the new fields
        req = MusicRequest(
            id="req-v14-test",
            user_id=seeded_users["alice"]["id"],
            item_type="album",
            title="Discovery",
            artist="Daft Punk",
            album="Discovery",
            quality_profile_id="profile-high-quality",
            current_quality="MP3 320",
            cutoff_met=0,
        )
        created = test_db.create_request(req)
        assert created["quality_profile_id"] == "profile-high-quality"
        assert created["current_quality"] == "MP3 320"
        assert created["cutoff_met"] == 0

        # Verify get_request retrieves the new fields
        retrieved = test_db.get_request("req-v14-test")
        assert retrieved is not None
        assert retrieved["quality_profile_id"] == "profile-high-quality"
        assert retrieved["current_quality"] == "MP3 320"
        assert retrieved["cutoff_met"] == 0

    def test_media_management_settings_v14_columns(self, test_db: Database) -> None:
        """Verifies delete_completed_transfers and enable_quality_upgrades exist with defaults."""
        cur = test_db.conn.execute("PRAGMA table_info(media_management_settings)")
        cols = {row[1]: row for row in cur.fetchall()}

        assert "delete_completed_transfers" in cols  # legacy column stays readable
        assert "seed_complete_action" in cols
        assert "enable_quality_upgrades" in cols

        # Check default settings values
        settings = test_db.get_media_management_settings()
        assert settings["seed_complete_action"] == "keep"
        assert settings["enable_quality_upgrades"] is True

        # Check updating settings
        updated = test_db.update_media_management_settings(
            {"seed_complete_action": "remove", "enable_quality_upgrades": False}
        )
        assert updated["seed_complete_action"] == "remove"
        assert updated["enable_quality_upgrades"] is False

        # Re-fetch from DB to verify persistence
        re_fetched = test_db.get_media_management_settings()
        assert re_fetched["seed_complete_action"] == "remove"
        assert re_fetched["enable_quality_upgrades"] is False

    def test_get_cutoff_unmet_requests_query(self, test_db: Database, seeded_users: dict[str, Any]) -> None:
        """Verifies get_cutoff_unmet_requests returns only status='available' AND cutoff_met=0."""
        user_id = seeded_users["alice"]["id"]

        # 1. Available and cutoff unmet -> should be returned
        r1 = MusicRequest(
            id="req-unmet-avail",
            user_id=user_id,
            item_type="album",
            title="Album 1",
            artist="Artist 1",
            album="Album 1",
            status=RequestStatus.AVAILABLE,
            cutoff_met=0,
            current_quality="MP3 192",
        )
        test_db.create_request(r1)

        # 2. Available and cutoff met -> should NOT be returned
        r2 = MusicRequest(
            id="req-met-avail",
            user_id=user_id,
            item_type="album",
            title="Album 2",
            artist="Artist 2",
            album="Album 2",
            status=RequestStatus.AVAILABLE,
            cutoff_met=1,
            current_quality="FLAC 16bit",
        )
        test_db.create_request(r2)

        # 3. Pending and cutoff unmet -> should NOT be returned
        r3 = MusicRequest(
            id="req-unmet-pending",
            user_id=user_id,
            item_type="album",
            title="Album 3",
            artist="Artist 3",
            album="Album 3",
            status=RequestStatus.PENDING,
            cutoff_met=0,
            current_quality=None,
        )
        test_db.create_request(r3)

        # 4. Rejected and cutoff unmet -> should NOT be returned
        r4 = MusicRequest(
            id="req-unmet-rejected",
            user_id=user_id,
            item_type="album",
            title="Album 4",
            artist="Artist 4",
            album="Album 4",
            status=RequestStatus.REJECTED,
            cutoff_met=0,
            current_quality=None,
        )
        test_db.create_request(r4)

        unmet = test_db.get_cutoff_unmet_requests()
        assert len(unmet) == 1
        assert unmet[0]["id"] == "req-unmet-avail"
        assert unmet[0]["cutoff_met"] == 0
        assert unmet[0]["current_quality"] == "MP3 192"

    def test_update_request_quality(self, test_db: Database, seeded_users: dict[str, Any]) -> None:
        """Verifies update_request_quality updates current_quality and cutoff_met cleanly."""
        user_id = seeded_users["alice"]["id"]
        req = MusicRequest(
            id="req-quality-update",
            user_id=user_id,
            item_type="album",
            title="Homework",
            artist="Daft Punk",
            album="Homework",
            status=RequestStatus.AVAILABLE,
            cutoff_met=1,
            current_quality="FLAC 16bit",
        )
        test_db.create_request(req)

        # Update to lower quality, cutoff not met
        ok = test_db.update_request_quality("req-quality-update", current_quality="MP3 320", cutoff_met=0)
        assert ok is True

        fetched = test_db.get_request("req-quality-update")
        assert fetched is not None
        assert fetched["current_quality"] == "MP3 320"
        assert fetched["cutoff_met"] == 0

        # Update to higher quality, cutoff met
        ok = test_db.update_request_quality("req-quality-update", current_quality="FLAC 24bit", cutoff_met=1)
        assert ok is True

        fetched = test_db.get_request("req-quality-update")
        assert fetched is not None
        assert fetched["current_quality"] == "FLAC 24bit"
        assert fetched["cutoff_met"] == 1

        # Nonexistent request returns False
        assert test_db.update_request_quality("nonexistent-id", "MP3 320", 0) is False


# ---------------------------------------------------------------------------
# 4. TestAcquisitionWorkerCutoffAndCleanup
# ---------------------------------------------------------------------------
class TestAcquisitionWorkerCutoffAndCleanup:
    """Integration tests for AcquisitionWorker cutoff determination, archive extraction, and client cleanup."""

    @pytest.fixture
    def staging_and_music(self, tmp_path: Path, test_db: Database) -> tuple[Path, Path]:
        staging = tmp_path / "downloads"
        music = tmp_path / "music"
        staging.mkdir()
        music.mkdir()

        settings = test_db.get_media_management_settings()
        settings["staging_folder_path"] = str(staging)
        settings["root_folder_path"] = str(music)
        test_db.update_media_management_settings(settings)
        return staging, music

    def test_import_with_lower_quality_sets_cutoff_met_zero(
        self,
        test_db: Database,
        seeded_users: dict[str, Any],
        staging_and_music: tuple[Path, Path],
    ) -> None:
        """Verifies importing lower quality (MP3 320) against cutoff (FLAC 16bit) sets cutoff_met = 0."""
        staging, music = staging_and_music

        # 1. Setup client
        test_db.create_download_client(
            DownloadClientConfig(
                id="c-qbit-cutoff",
                name="Qbit Cutoff",
                driver_type=DownloadDriverType.QBITTORRENT,
                host_url="http://127.0.0.1:8080",
                enabled=True,
            )
        )

        # 2. Setup request with profile-high-quality (cutoff = FLAC 16bit, MP3 320 allowed but below cutoff)
        req = MusicRequest(
            id="req-cutoff-test",
            user_id=seeded_users["alice"]["id"],
            item_type="album",
            title="Random Access Memories",
            artist="Daft Punk",
            album="Random Access Memories",
            status=RequestStatus.PROCESSING,
            quality_profile_id="profile-high-quality",
        )
        test_db.create_request(req)

        # 3. Create MP3 file in staging
        audio_file = staging / "track01.mp3"
        _create_minimal_mp3(audio_file)

        # 4. Create active download referencing request with MP3 320 in title
        test_db.create_active_download(
            ActiveDownload(
                id="dl-cutoff-mp3",
                title="Daft Punk - Random Access Memories (2013) [MP3 320]",
                artist="Daft Punk",
                client_id="c-qbit-cutoff",
                download_hash="hash-cutoff-1",
                status=DownloadStatus.COMPLETED.value,
                source_path=str(audio_file),
                request_id="req-cutoff-test",
            )
        )

        mock_driver = MagicMock()
        mock_driver.get_status.return_value = {
            "status": DownloadStatus.COMPLETED.value,
            "progress": 100.0,
            "source_path": str(audio_file),
            "error_message": None,
        }

        worker = AcquisitionWorker()
        with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
            stats = worker.poll_once(db=test_db, staging_dir=str(staging))

            assert stats["imported"] == 1
            assert stats["failed"] == 0

        # Verify request updated: status available, current_quality MP3 320, cutoff_met = 0
        updated_req = test_db.get_request("req-cutoff-test")
        assert updated_req is not None
        assert updated_req["status"] == RequestStatus.AVAILABLE.value
        assert updated_req["current_quality"] == "MP3 320"
        assert updated_req["cutoff_met"] == 0

    def test_import_with_cutoff_meeting_quality_sets_cutoff_met_one(
        self,
        test_db: Database,
        seeded_users: dict[str, Any],
        staging_and_music: tuple[Path, Path],
    ) -> None:
        """Verifies importing quality meeting cutoff (FLAC 16bit) sets cutoff_met = 1."""
        staging, music = staging_and_music

        test_db.create_download_client(
            DownloadClientConfig(
                id="c-qbit-meet",
                name="Qbit Meet",
                driver_type=DownloadDriverType.QBITTORRENT,
                host_url="http://127.0.0.1:8080",
                enabled=True,
            )
        )

        req = MusicRequest(
            id="req-meet-test",
            user_id=seeded_users["alice"]["id"],
            item_type="album",
            title="Discovery",
            artist="Daft Punk",
            album="Discovery",
            status=RequestStatus.PROCESSING,
            quality_profile_id="profile-high-quality",
        )
        test_db.create_request(req)

        audio_file = staging / "track01.flac"
        _create_minimal_flac(audio_file)

        test_db.create_active_download(
            ActiveDownload(
                id="dl-meet-flac",
                title="Daft Punk - Discovery (2001) [FLAC 16bit]",
                artist="Daft Punk",
                client_id="c-qbit-meet",
                download_hash="hash-meet-1",
                status=DownloadStatus.COMPLETED.value,
                source_path=str(audio_file),
                request_id="req-meet-test",
            )
        )

        mock_driver = MagicMock()
        mock_driver.get_status.return_value = {
            "status": DownloadStatus.COMPLETED.value,
            "progress": 100.0,
            "source_path": str(audio_file),
            "error_message": None,
        }

        worker = AcquisitionWorker()
        with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
            stats = worker.poll_once(db=test_db, staging_dir=str(staging))
            assert stats["imported"] == 1

        updated_req = test_db.get_request("req-meet-test")
        assert updated_req is not None
        assert updated_req["status"] == RequestStatus.AVAILABLE.value
        assert updated_req["current_quality"] == "FLAC 16bit"
        assert updated_req["cutoff_met"] == 1

    def test_import_with_delete_completed_transfers_invokes_cleanup(
        self,
        test_db: Database,
        seeded_users: dict[str, Any],
        staging_and_music: tuple[Path, Path],
    ) -> None:
        """Verifies delete_completed_transfers=True triggers driver.cleanup_completed."""
        staging, music = staging_and_music

        # Enable delete_completed_transfers in settings
        settings = test_db.get_media_management_settings()
        settings["seed_complete_action"] = "remove"
        test_db.update_media_management_settings(settings)

        test_db.create_download_client(
            DownloadClientConfig(
                id="c-cleanup-client",
                name="Cleanup Client",
                driver_type=DownloadDriverType.QBITTORRENT,
                host_url="http://127.0.0.1:8080",
                enabled=True,
            )
        )

        audio_file = staging / "cleanup_track.flac"
        _create_minimal_flac(audio_file)

        test_db.create_active_download(
            ActiveDownload(
                id="dl-cleanup-run",
                title="Daft Punk - Aerodynamic [FLAC 16bit]",
                artist="Daft Punk",
                client_id="c-cleanup-client",
                download_hash="hash-cleanup-123",
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
        mock_driver.cleanup_completed.return_value = True

        worker = AcquisitionWorker()
        with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
            stats = worker.poll_once(db=test_db, staging_dir=str(staging))
            assert stats["imported"] == 1

            # Assert driver.cleanup_completed was called with the download hash
            mock_driver.cleanup_completed.assert_called_once_with("hash-cleanup-123", delete_files=False)

    @pytest.mark.parametrize(
        "import_mode,expected_delete", [("copy", True), ("move", False)]
    )
    def test_import_remove_and_delete_deletes_files_only_when_safe(
        self,
        test_db: Database,
        seeded_users: dict[str, Any],
        staging_and_music: tuple[Path, Path],
        import_mode: str,
        expected_delete: bool,
    ) -> None:
        """remove_and_delete passes delete_files=True after a copy import (placed files recorded), never after a move."""
        staging, music = staging_and_music
        settings = test_db.get_media_management_settings()
        settings["seed_complete_action"] = "remove_and_delete"
        settings["import_mode"] = import_mode
        test_db.update_media_management_settings(settings)
        test_db.create_download_client(
            DownloadClientConfig(
                id="c-del", name="Del Client", driver_type=DownloadDriverType.QBITTORRENT,
                host_url="http://127.0.0.1:8080", enabled=True,
            )
        )
        audio_file = staging / "del_track.flac"
        _create_minimal_flac(audio_file)
        test_db.create_active_download(
            ActiveDownload(
                id="dl-del", title="Daft Punk - Aerodynamic [FLAC 16bit]", artist="Daft Punk",
                client_id="c-del", download_hash="hash-del", status=DownloadStatus.COMPLETED.value,
                source_path=str(audio_file),
            )
        )
        mock_driver = MagicMock()
        mock_driver.get_status.return_value = {
            "status": DownloadStatus.COMPLETED.value, "progress": 100.0, "source_path": str(audio_file),
            "content_path": str(audio_file), "error_message": None,
        }
        mock_driver.cleanup_completed.return_value = True
        with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
            stats = AcquisitionWorker().poll_once(db=test_db, staging_dir=str(staging))
        assert stats["imported"] == 1
        mock_driver.cleanup_completed.assert_called_once_with("hash-del", delete_files=expected_delete)
        row = test_db.get_active_download("dl-del")
        assert row["placed_mode"] == import_mode and len(row["placed_files"]) == 1
        assert Path(row["placed_files"][0]).is_file() and str(music) in row["placed_files"][0]

    def test_import_with_delete_completed_transfers_false_skips_cleanup(
        self,
        test_db: Database,
        staging_and_music: tuple[Path, Path],
    ) -> None:
        """Verifies delete_completed_transfers=False skips driver.cleanup_completed."""
        staging, music = staging_and_music

        settings = test_db.get_media_management_settings()
        settings["seed_complete_action"] = "keep"
        test_db.update_media_management_settings(settings)

        test_db.create_download_client(
            DownloadClientConfig(
                id="c-no-cleanup-client",
                name="No Cleanup Client",
                driver_type=DownloadDriverType.QBITTORRENT,
                host_url="http://127.0.0.1:8080",
                enabled=True,
            )
        )

        audio_file = staging / "keep_track.flac"
        _create_minimal_flac(audio_file)

        test_db.create_active_download(
            ActiveDownload(
                id="dl-no-cleanup-run",
                title="Daft Punk - One More Time [FLAC 16bit]",
                artist="Daft Punk",
                client_id="c-no-cleanup-client",
                download_hash="hash-no-cleanup",
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

        worker = AcquisitionWorker()
        with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
            stats = worker.poll_once(db=test_db, staging_dir=str(staging))
            assert stats["imported"] == 1
            mock_driver.cleanup_completed.assert_not_called()

    def test_archive_extraction_during_worker_import(
        self,
        test_db: Database,
        staging_and_music: tuple[Path, Path],
    ) -> None:
        """Verifies candidate archive (.zip) in staging is extracted and audio files are imported."""
        staging, music = staging_and_music

        test_db.create_download_client(
            DownloadClientConfig(
                id="c-arc-client",
                name="Archive Client",
                driver_type=DownloadDriverType.QBITTORRENT,
                host_url="http://127.0.0.1:8080",
                enabled=True,
            )
        )

        # Create zip archive in staging
        archive_path = staging / "daft_punk_live.zip"
        _create_minimal_flac(staging / "_temp.flac")
        raw_flac = (staging / "_temp.flac").read_bytes()
        (staging / "_temp.flac").unlink()

        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("album/track01.flac", raw_flac)

        test_db.create_active_download(
            ActiveDownload(
                id="dl-archive-import",
                title="Daft Punk - Alive 2007 [FLAC 16bit]",
                artist="Daft Punk",
                client_id="c-arc-client",
                download_hash="hash-arc-1",
                status=DownloadStatus.COMPLETED.value,
                source_path=str(archive_path),
            )
        )

        mock_driver = MagicMock()
        mock_driver.get_status.return_value = {
            "status": DownloadStatus.COMPLETED.value,
            "progress": 100.0,
            "source_path": str(archive_path),
            "error_message": None,
        }

        worker = AcquisitionWorker()
        with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
            stats = worker.poll_once(db=test_db, staging_dir=str(staging))
            assert stats["imported"] == 1

        updated_dl = test_db.get_active_download("dl-archive-import")
        assert updated_dl is not None
        assert updated_dl["status"] == DownloadStatus.IMPORTED.value


# ---------------------------------------------------------------------------
# 5. TestBacklogAndRSSQualityUpgrades
# ---------------------------------------------------------------------------
class TestBacklogAndRSSQualityUpgrades:
    """Integration tests for WantedBacklogWorker and RSSSyncWorker quality upgrades."""

    def test_backlog_worker_includes_cutoff_unmet_requests(
        self,
        test_db: Database,
        seeded_users: dict[str, Any],
    ) -> None:
        """Verifies WantedBacklogWorker scans cutoff-unmet requests when enable_quality_upgrades=True."""
        # 1. Enable quality upgrades in settings
        settings = test_db.get_media_management_settings()
        settings["enable_quality_upgrades"] = True
        test_db.update_media_management_settings(settings)

        # 2. Seed available request with cutoff_met=0 and quality_profile_id
        req = MusicRequest(
            id="req-upgrade-backlog",
            user_id=seeded_users["alice"]["id"],
            item_type="album",
            title="Random Access Memories",
            artist="Daft Punk",
            album="Random Access Memories",
            status=RequestStatus.AVAILABLE,
            cutoff_met=0,
            current_quality="MP3 192",
            quality_profile_id="profile-high-quality",
        )
        test_db.create_request(req)

        worker = WantedBacklogWorker()
        with patch.object(acquisition_coordinator, "search_and_grab") as mock_search_grab:
            mock_search_grab.return_value = {"success": True, "result": None}

            stats = worker.poll_once(test_db)
            assert stats["items_checked"] >= 1
            assert stats["items_grabbed"] == 1

            # Assert search_and_grab was called with min_score and quality_profile_id
            mock_search_grab.assert_called_once()
            _, kwargs = mock_search_grab.call_args
            assert kwargs["artist"] == "Daft Punk"
            assert kwargs["title"] == "Random Access Memories"
            assert kwargs["quality_profile_id"] == "profile-high-quality"
            # min_score must be computed based on current_quality
            assert kwargs["min_score"] is not None

    def test_backlog_worker_skips_cutoff_unmet_when_upgrades_disabled(
        self,
        test_db: Database,
        seeded_users: dict[str, Any],
    ) -> None:
        """Verifies WantedBacklogWorker skips cutoff-unmet requests when enable_quality_upgrades=False."""
        settings = test_db.get_media_management_settings()
        settings["enable_quality_upgrades"] = False
        test_db.update_media_management_settings(settings)

        req = MusicRequest(
            id="req-upgrade-disabled",
            user_id=seeded_users["alice"]["id"],
            item_type="album",
            title="Discovery",
            artist="Daft Punk",
            album="Discovery",
            status=RequestStatus.AVAILABLE,
            cutoff_met=0,
            current_quality="MP3 192",
            quality_profile_id="profile-high-quality",
        )
        test_db.create_request(req)

        worker = WantedBacklogWorker()
        with patch.object(acquisition_coordinator, "search_and_grab") as mock_search_grab:
            stats = worker.poll_once(test_db)
            assert stats["items_checked"] == 0
            mock_search_grab.assert_not_called()

    def test_backlog_worker_deduplicates_in_flight_upgrades(
        self,
        test_db: Database,
        seeded_users: dict[str, Any],
    ) -> None:
        """Verifies WantedBacklogWorker skips cutoff-unmet requests that already have active downloads."""
        test_db.create_download_client(
            DownloadClientConfig(
                id="c-client-any",
                name="Any Client",
                driver_type=DownloadDriverType.QBITTORRENT,
                host_url="http://127.0.0.1:8080",
                enabled=True,
            )
        )

        settings = test_db.get_media_management_settings()
        settings["enable_quality_upgrades"] = True
        test_db.update_media_management_settings(settings)

        req = MusicRequest(
            id="req-in-flight",
            user_id=seeded_users["alice"]["id"],
            item_type="album",
            title="Tron Legacy",
            artist="Daft Punk",
            album="Tron Legacy",
            status=RequestStatus.AVAILABLE,
            cutoff_met=0,
            current_quality="MP3 192",
            quality_profile_id="profile-high-quality",
        )
        test_db.create_request(req)

        # Active download already downloading for this request
        test_db.create_active_download(
            ActiveDownload(
                id="dl-upgrade-in-flight",
                title="Daft Punk - Tron Legacy [FLAC 16bit]",
                artist="Daft Punk",
                client_id="c-client-any",
                download_hash="hash-in-flight",
                status=DownloadStatus.DOWNLOADING.value,
                request_id="req-in-flight",
            )
        )

        worker = WantedBacklogWorker()
        with patch.object(acquisition_coordinator, "search_and_grab") as mock_search_grab:
            stats = worker.poll_once(test_db)
            assert stats["items_checked"] == 0
            mock_search_grab.assert_not_called()

    def test_rss_worker_upgrade_candidate_grabbed_only_if_score_exceeds_current(
        self,
        test_db: Database,
        seeded_users: dict[str, Any],
    ) -> None:
        """Verifies RSSSyncWorker only grabs candidate if candidate score > current score."""
        worker = RSSSyncWorker()

        # Enable quality upgrades in settings
        settings = test_db.get_media_management_settings()
        settings["enable_quality_upgrades"] = True
        test_db.update_media_management_settings(settings)

        # Setup indexer and download client
        test_db.create_indexer(
            {
                "id": "idx-rss-upg",
                "name": "RSS Indexer",
                "indexer_type": "torznab",
                "host_url": "http://127.0.0.1:9696",
                "enabled": True,
            }
        )
        test_db.create_download_client(
            {
                "id": "c-qbit-rss-upg",
                "name": "Qbit RSS",
                "driver_type": "qbittorrent",
                "host_url": "http://127.0.0.1:8080",
                "enabled": True,
                "priority": 1,
            }
        )

        # Seed request with available status, cutoff_met=0, current quality MP3 320
        # In profile-high-quality: MP3 320 score is 800, FLAC 16bit score is 900, MP3 192 score is 500
        req = MusicRequest(
            id="req-rss-upgrade-eval",
            user_id=seeded_users["alice"]["id"],
            item_type="album",
            title="Random Access Memories",
            artist="Daft Punk",
            album="Random Access Memories",
            status=RequestStatus.AVAILABLE,
            cutoff_met=0,
            current_quality="MP3 320",
            quality_profile_id="profile-high-quality",
        )
        test_db.create_request(req)

        # Candidate 1: Same or lower score (MP3 320 or MP3 192) -> score <= current_score -> Rejected
        cand_lower = AcquisitionSearchResult(
            download_id="dl-low",
            title="Daft Punk - Random Access Memories (2013) [MP3 192]",
            artist="Daft Punk",
            album="Random Access Memories",
            size_bytes=100000000,
            protocol="torrent",
            download_url="magnet:?xt=urn:btih:low123",
        )

        mock_idx = MagicMock()
        mock_idx.fetch_recent.return_value = [cand_lower]
        mock_client = MagicMock()
        mock_client.download.return_value = "grabbed_hash_1"

        with patch("plex_playlist_sync.backlog_worker.get_indexer_driver", return_value=mock_idx), \
             patch("plex_playlist_sync.backlog_worker.get_acquisition_driver", return_value=mock_client):

            stats = worker.poll_once(test_db)
            assert stats["releases_scanned"] == 1
            assert stats["grabs_triggered"] == 0
            mock_client.download.assert_not_called()

        # Candidate 2: Higher score (FLAC 16bit, score 900 > 800) -> Accepted and Grabbed
        cand_higher = AcquisitionSearchResult(
            download_id="dl-high",
            title="Daft Punk - Random Access Memories (2013) [FLAC 16bit]",
            artist="Daft Punk",
            album="Random Access Memories",
            size_bytes=400000000,
            protocol="torrent",
            download_url="magnet:?xt=urn:btih:high123",
        )
        mock_idx.fetch_recent.return_value = [cand_higher]

        with patch("plex_playlist_sync.backlog_worker.get_indexer_driver", return_value=mock_idx), \
             patch("plex_playlist_sync.backlog_worker.get_acquisition_driver", return_value=mock_client):

            stats = worker.poll_once(test_db)
            assert stats["releases_scanned"] == 1
            assert stats["grabs_triggered"] == 1
            mock_client.download.assert_called_once()

    def test_rss_worker_skips_upgrades_when_disabled(
        self,
        test_db: Database,
        seeded_users: dict[str, Any],
    ) -> None:
        """Verifies RSSSyncWorker skips cutoff-unmet requests when enable_quality_upgrades=False."""
        worker = RSSSyncWorker()

        settings = test_db.get_media_management_settings()
        settings["enable_quality_upgrades"] = False
        test_db.update_media_management_settings(settings)

        test_db.create_indexer(
            {
                "id": "idx-rss-dis",
                "name": "RSS Indexer",
                "indexer_type": "torznab",
                "host_url": "http://127.0.0.1:9696",
                "enabled": True,
            }
        )

        req = MusicRequest(
            id="req-rss-upg-disabled",
            user_id=seeded_users["alice"]["id"],
            item_type="album",
            title="Discovery",
            artist="Daft Punk",
            album="Discovery",
            status=RequestStatus.AVAILABLE,
            cutoff_met=0,
            current_quality="MP3 192",
            quality_profile_id="profile-high-quality",
        )
        test_db.create_request(req)

        cand_higher = AcquisitionSearchResult(
            download_id="dl-high-dis",
            title="Daft Punk - Discovery (2001) [FLAC 16bit]",
            artist="Daft Punk",
            album="Discovery",
            size_bytes=350000000,
            protocol="torrent",
            download_url="magnet:?xt=urn:btih:highdis",
        )
        mock_idx = MagicMock()
        mock_idx.fetch_recent.return_value = [cand_higher]

        with patch("plex_playlist_sync.backlog_worker.get_indexer_driver", return_value=mock_idx):
            stats = worker.poll_once(test_db)
            # When enable_quality_upgrades is False, wanted_requests is empty so no scan needed
            assert stats["releases_scanned"] == 0
            assert stats["grabs_triggered"] == 0


# ---------------------------------------------------------------------------
# 6. TestMediaManagementSettingsAPI
# ---------------------------------------------------------------------------
class TestMediaManagementSettingsAPI:
    """API endpoint tests for media management settings with v14 upgrade fields."""

    def test_get_media_management_settings_returns_v14_fields(
        self,
        app_and_client: tuple[Any, TestClient],
        seeded_users: dict[str, Any],
        test_db: Database,
        test_config: Config,
    ) -> None:
        """Verifies GET /api/settings/media-management returns delete_completed_transfers & enable_quality_upgrades."""
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        res = client.get("/api/settings/media-management", headers=admin_headers)
        assert res.status_code == 200
        data = res.json()
        assert "settings" in data
        settings = data["settings"]
        assert "seed_complete_action" in settings
        assert "delete_completed_transfers" not in settings
        assert "enable_quality_upgrades" in settings
        assert settings["seed_complete_action"] == "keep"
        assert settings["enable_quality_upgrades"] is True

    def test_post_media_management_settings_updates_v14_fields(
        self,
        app_and_client: tuple[Any, TestClient],
        seeded_users: dict[str, Any],
        test_db: Database,
        test_config: Config,
    ) -> None:
        """Verifies POST /api/settings/media-management updates and persists v14 fields."""
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        payload = {
            "seed_complete_action": "remove_and_delete",
            "enable_quality_upgrades": False,
        }
        res = client.post("/api/settings/media-management", json=payload, headers=admin_headers)
        assert res.status_code == 200
        updated = res.json()
        assert updated["seed_complete_action"] == "remove_and_delete"
        assert updated["enable_quality_upgrades"] is False

        # Verify DB directly
        db_settings = test_db.get_media_management_settings()
        assert db_settings["seed_complete_action"] == "remove_and_delete"
        assert db_settings["enable_quality_upgrades"] is False

        # Subsequent GET confirms persistence
        get_res = client.get("/api/settings/media-management", headers=admin_headers)
        assert get_res.status_code == 200
        assert get_res.json()["settings"]["seed_complete_action"] == "remove_and_delete"
        assert get_res.json()["settings"]["enable_quality_upgrades"] is False

    def test_media_management_settings_non_admin_forbidden(
        self,
        app_and_client: tuple[Any, TestClient],
        seeded_users: dict[str, Any],
        test_db: Database,
        test_config: Config,
    ) -> None:
        """Verifies non-admin users cannot access or modify media management settings."""
        _, client = app_and_client
        alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)

        # GET forbidden
        res = client.get("/api/settings/media-management", headers=alice_headers)
        assert res.status_code == 403

        # POST forbidden
        res = client.post(
            "/api/settings/media-management",
            json={"delete_completed_transfers": True},
            headers=alice_headers,
        )
        assert res.status_code == 403

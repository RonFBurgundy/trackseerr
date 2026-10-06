"""Unit and integration tests for Phase 4: Scalability, Custom Formats & Seeding Governance.

Covers:
1. Migration v19: custom_formats_json, min_score, seed_ratio_limit, seed_time_limit_minutes.
2. Custom Formats (CF) regex scoring engine with bonuses, penalties, and negate flags.
3. min_score cutoff enforcement rejecting candidate releases below threshold.
4. LibraryScanner multi-threaded worker pool, file-size caching skipping unchanged files, and batched writes.
5. Seeding Governance preserving hardlink torrents in download client until ratio/time limits are reached.
"""

from tests.audio_fixtures import write_flac
import sqlite3
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plex_playlist_sync.acquisition_worker import AcquisitionWorker
from plex_playlist_sync.clients.acquisition.qbittorrent import QbittorrentDriver
from plex_playlist_sync.library_scanner import LibraryScanner
from plex_playlist_sync.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    ParsedRelease,
    QualityProfile,
    QualityProfileItem,
)
from plex_playlist_sync.quality import evaluate_release
from plex_playlist_sync.storage import Database


@pytest.fixture
def test_db(tmp_path: Path):
    """Provides an isolated disk-backed SQLite database with migrations applied."""
    db_file = tmp_path / "test_phase4.db"
    database = Database(db_file)
    yield database
    database.close()


@pytest.fixture
def test_scanner():
    """Provides a fresh LibraryScanner instance."""
    return LibraryScanner()


# ---------------------------------------------------------------------------
# Test 1: Migration v19 Schema and CRUD
# ---------------------------------------------------------------------------


def test_migration_v19_schema_and_crud(test_db: Database):
    """Verifies Migration v19 adds custom_formats_json, min_score, seed_ratio_limit, and seed_time_limit_minutes."""
    # 1. Verify schema_migrations contains version 19
    with test_db._lock:
        cur = test_db.conn.cursor()
        cur.execute("SELECT MAX(version) FROM schema_migrations")
        max_v = cur.fetchone()[0]
        assert max_v >= 19

        # Check quality_profiles columns
        cur.execute("PRAGMA table_info(quality_profiles)")
        qp_cols = {row[1]: row[2] for row in cur.fetchall()}
        assert "custom_formats_json" in qp_cols
        assert "min_score" in qp_cols

        # Check media_management_settings columns
        cur.execute("PRAGMA table_info(media_management_settings)")
        mm_cols = {row[1]: row[2] for row in cur.fetchall()}
        assert "seed_ratio_limit" in mm_cols
        assert "seed_time_limit_minutes" in mm_cols

    # 2. Test Quality Profile CRUD with custom_formats and min_score
    custom_formats_data = [
        {"name": "Remaster", "pattern": r"\bremaster\b", "score": 150, "negate": False},
        {"name": "Vinyl", "pattern": r"\bvinyl\b", "score": 75, "negate": False},
    ]
    profile = QualityProfile(
        id="profile-custom-cf",
        name="Lossless with CF",
        cutoff="FLAC 16bit",
        items=[
            QualityProfileItem(quality="FLAC 24bit", allowed=True, weight=1000),
            QualityProfileItem(quality="FLAC 16bit", allowed=True, weight=900),
        ],
        custom_formats=custom_formats_data,
        min_score=850,
    )

    created = test_db.create_quality_profile(profile)
    assert created["id"] == "profile-custom-cf"
    assert created["custom_formats"] == custom_formats_data
    assert created["min_score"] == 850

    fetched = test_db.get_quality_profile("profile-custom-cf")
    assert fetched is not None
    assert fetched["custom_formats"] == custom_formats_data
    assert fetched["min_score"] == 850

    # Test update_quality_profile
    updated = test_db.update_quality_profile(
        "profile-custom-cf",
        {"min_score": 920, "custom_formats": [{"name": "Web", "pattern": r"\bweb\b", "score": 50}]},
    )
    assert updated is not None
    assert updated["min_score"] == 920
    assert len(updated["custom_formats"]) == 1
    assert updated["custom_formats"][0]["name"] == "Web"

    # 3. Test Media Management Settings CRUD with seeding limits
    mm_settings = test_db.get_media_management_settings()
    assert "seed_ratio_limit" in mm_settings
    assert "seed_time_limit_minutes" in mm_settings
    assert mm_settings["seed_ratio_limit"] is None
    assert mm_settings["seed_time_limit_minutes"] is None

    updated_mm = test_db.update_media_management_settings(
        {
            "seed_ratio_limit": 2.5,
            "seed_time_limit_minutes": 180,
            "import_mode": "hardlink",
        }
    )
    assert updated_mm["seed_ratio_limit"] == 2.5
    assert updated_mm["seed_time_limit_minutes"] == 180
    assert updated_mm["import_mode"] == "hardlink"

    # Retrieve again from clean DB fetch
    fetched_mm = test_db.get_media_management_settings()
    assert fetched_mm["seed_ratio_limit"] == 2.5
    assert fetched_mm["seed_time_limit_minutes"] == 180


# ---------------------------------------------------------------------------
# Test 2: Custom Formats Regex Scoring Engine
# ---------------------------------------------------------------------------


def test_custom_formats_scoring_bonuses_and_penalties():
    """Verifies that CF regex scoring engine applies bonuses, penalties, and negate logic."""
    profile = QualityProfile(
        id="profile-cf-test",
        name="CF Scoring Test Profile",
        cutoff="FLAC 16bit",
        items=[
            QualityProfileItem(quality="FLAC 16bit", allowed=True, weight=1000),
            QualityProfileItem(quality="MP3 320", allowed=True, weight=700),
        ],
        preferred_tags=[],
        custom_formats=[
            {"name": "Remaster Bonus", "pattern": r"\bremaster(?:ed)?\b", "score": 150, "negate": False},
            {"name": "Vinyl Bonus", "pattern": r"\bvinyl\b", "score": 80, "negate": False},
            {"name": "Live Penalty", "pattern": r"\blive\b", "score": -200, "negate": False},
            {"name": "Censored Penalty", "pattern": r"\bcensored\b", "score": -350, "negate": False},
            {"name": "Uncensored Bonus", "pattern": r"\bcensored\b", "score": 60, "negate": True},
        ],
    )

    # 1. Base release: FLAC 16bit + Remaster + Vinyl (Not Censored -> +60)
    # Expected: 1000 + 150 + 80 + 60 = 1290
    rel1 = ParsedRelease(
        raw_title="Pink Floyd - The Dark Side of the Moon (1973) [FLAC 16bit] Remaster Vinyl",
        quality="FLAC 16bit",
    )
    res1 = evaluate_release(rel1, profile)
    assert res1.is_acceptable is True
    assert res1.format_score == 290

    # 2. Live release: FLAC 16bit + Live (-200) (Not Censored -> +60)
    # Expected: 1000 - 200 + 60 = 860
    rel2 = ParsedRelease(
        raw_title="Pink Floyd - Live at Pompeii [FLAC 16bit]",
        quality="FLAC 16bit",
    )
    res2 = evaluate_release(rel2, profile)
    assert res2.is_acceptable is True
    assert res2.format_score == -140

    # 3. Censored release: MP3 320 (700) + Censored (-350) + Negate matched (+0)
    # Expected: 700 - 350 = 350
    rel3 = ParsedRelease(
        raw_title="Eminem - The Eminem Show [MP3 320] Censored Edit",
        quality="MP3 320",
    )
    res3 = evaluate_release(rel3, profile)
    assert res3.is_acceptable is True
    assert res3.format_score == -350


# ---------------------------------------------------------------------------
# Test 3: min_score Cutoff Enforcement
# ---------------------------------------------------------------------------


def test_min_score_cutoff_rejects_below_threshold():
    """Verifies that min_score rejects releases whose calculated score falls below profile threshold."""
    profile = QualityProfile(
        id="profile-min-score",
        name="Strict Threshold Profile",
        cutoff="FLAC 16bit",
        items=[
            QualityProfileItem(quality="FLAC 16bit", allowed=True, weight=1000),
            QualityProfileItem(quality="MP3 320", allowed=True, weight=700),
        ],
        custom_formats=[
            {"name": "Live Penalty", "pattern": r"\blive\b", "score": -200, "negate": False},
        ],
        min_score=900,
    )

    # 1. Score 1000 >= 900: Acceptable
    rel_good = ParsedRelease(
        raw_title="Radiohead - OK Computer [FLAC 16bit]",
        quality="FLAC 16bit",
    )
    res_good = evaluate_release(rel_good, profile)
    assert res_good.is_acceptable is True
    assert res_good.format_score == 0

    # 2. Score 800 (1000 - 200 for Live) < 900: Rejected
    rel_live = ParsedRelease(
        raw_title="Radiohead - OK Computer Live in Oxford [FLAC 16bit]",
        quality="FLAC 16bit",
    )
    res_live = evaluate_release(rel_live, profile)
    assert res_live.is_acceptable is False
    assert res_live.format_score == -200
    assert any("below profile minimum 900" in r for r in res_live.rejection_reasons)

    # 3. Base score 700 < 900: Rejected
    rel_mp3 = ParsedRelease(
        raw_title="Radiohead - OK Computer [MP3 320]",
        quality="MP3 320",
    )
    res_mp3 = evaluate_release(rel_mp3, profile)
    assert res_mp3.is_acceptable is False
    assert res_mp3.format_score == 0
    assert any("below profile minimum 900" in r for r in res_mp3.rejection_reasons)


# ---------------------------------------------------------------------------
# Test 4: LibraryScanner Multi-Threaded Worker Pool & Size-Based Caching
# ---------------------------------------------------------------------------


def test_library_scanner_skips_unchanged_files_via_cache(test_db: Database, test_scanner: LibraryScanner, tmp_path: Path):
    """Verifies that LibraryScanner inspects files concurrently and skips Mutagen re-inspection on cached files."""
    music_dir = tmp_path / "music"
    album_dir = music_dir / "Miles Davis" / "Kind of Blue"
    album_dir.mkdir(parents=True)

    file_paths: list[Path] = []
    for i in range(1, 6):
        f = album_dir / f"0{i} - Track_{i}.flac"
        f.write_bytes(f"flac audio content for track {i}".encode())
        file_paths.append(f)

    inspect_call_count = 0

    def mock_inspect(path: Path | str) -> dict[str, Any]:
        nonlocal inspect_call_count
        inspect_call_count += 1
        p = Path(path).resolve()
        return {
            "title": p.stem,
            "artist": "Miles Davis",
            "album": "Kind of Blue",
            "track_number": int(p.stem.split(" - ")[0]),
            "disc_number": 1,
            "codec": "FLAC",
            "bitrate": 900000,
            "sample_rate": 44100,
            "bits_per_sample": 16,
            "quality_full": "FLAC 16bit",
            "file_path": str(p),
        }

    # Initial scan: inspect_audio_file must be called for all 5 files
    with patch("plex_playlist_sync.library_scanner.inspect_audio_file", side_effect=mock_inspect):
        res1 = test_scanner.scan(test_db, root_folder=str(music_dir))

    assert res1["status"] == "completed"
    assert res1["total_files_found"] == 5
    assert res1["files_indexed"] == 5
    assert inspect_call_count == 5

    # Check files in database
    files_in_db = test_db.list_library_files(limit=100)
    assert len(files_in_db) == 5

    # Second scan: Files are identical on disk with matching size_bytes
    # inspect_audio_file MUST NOT be called!
    inspect_call_count = 0
    with patch("plex_playlist_sync.library_scanner.inspect_audio_file", side_effect=mock_inspect):
        res2 = test_scanner.scan(test_db, root_folder=str(music_dir))

    assert res2["status"] == "completed"
    assert res2["total_files_found"] == 5
    assert res2["files_indexed"] == 5
    assert inspect_call_count == 0  # Zero Mutagen calls!

    # Third scan: Modify exactly 1 file on disk
    file_paths[0].write_bytes(b"modified and enlarged audio file content to change size")
    inspect_call_count = 0
    with patch("plex_playlist_sync.library_scanner.inspect_audio_file", side_effect=mock_inspect):
        res3 = test_scanner.scan(test_db, root_folder=str(music_dir))

    assert res3["status"] == "completed"
    assert res3["total_files_found"] == 5
    assert res3["files_indexed"] == 5
    assert inspect_call_count == 1  # Only the modified file was re-inspected!


def test_library_scanner_batched_database_commits(test_db: Database, test_scanner: LibraryScanner, tmp_path: Path):
    """Verifies that LibraryScanner groups file insertions in batches."""
    music_dir = tmp_path / "music"
    album_dir = music_dir / "Daft Punk" / "Homework"
    album_dir.mkdir(parents=True)

    for i in range(1, 12):
        f = album_dir / f"{i:02d} - Track {i}.flac"
        f.write_bytes(b"audio data for batch test")

    spy_batch = MagicMock(wraps=test_db.upsert_library_files_batch)
    with patch.object(test_db, "upsert_library_files_batch", spy_batch):
        res = test_scanner.scan(test_db, root_folder=str(music_dir))

    assert res["status"] == "completed"
    assert res["files_indexed"] == 11
    # upsert_library_files_batch was called
    assert spy_batch.call_count >= 1
    total_batch_files = sum(len(call.args[0]) for call in spy_batch.call_args_list)
    assert total_batch_files == 11


# ---------------------------------------------------------------------------
# Test 5: Seeding Governance in AcquisitionWorker
# ---------------------------------------------------------------------------


def test_seeding_governance_preserves_hardlink_until_ratio_and_time_limits(
    test_db: Database, tmp_path: Path
):
    """Verifies seeding governance preserves hardlink torrents in client until ratio or time limit is met."""
    staging = tmp_path / "staging"
    music = tmp_path / "music"
    staging.mkdir(parents=True)
    music.mkdir(parents=True)

    # Configure media management settings: hardlink import mode, delete_completed_transfers=True
    test_db.update_media_management_settings(
        {
            "import_mode": "hardlink",
            "delete_completed_transfers": True,
            "seed_ratio_limit": 2.0,
            "seed_time_limit_minutes": 60,
            "root_folder_path": str(music),
            "staging_folder_path": str(staging),
        }
    )

    test_db.create_download_client(
        DownloadClientConfig(
            id="c-qb-seeder",
            name="qBittorrent Seeder",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://127.0.0.1:8080",
            enabled=True,
        )
    )

    torrent_file = staging / "01 - Around the World.flac"
    write_flac(torrent_file)

    # Capture torrent file's inode and bytes BEFORE import
    torrent_ino_before = torrent_file.stat().st_ino
    torrent_bytes_before = torrent_file.read_bytes()

    test_db.create_active_download(
        ActiveDownload(
            id="dl-seeding-test",
            title="Daft Punk - Around the World [FLAC 16bit]",
            artist="Daft Punk",
            client_id="c-qb-seeder",
            download_hash="hash-seed-12345",
            status=DownloadStatus.COMPLETED.value,
            source_path=str(torrent_file),
        )
    )

    mock_driver = MagicMock()
    mock_driver.cleanup_completed.return_value = True

    worker = AcquisitionWorker()

    with patch(
        "plex_playlist_sync.acquisition_worker.get_acquisition_driver",
        return_value=mock_driver,
    ):
        # --- Tick 1: Download completed, imported as hardlink, but ratio (0.4) < 2.0 and time (10m) < 60m ---
        mock_driver.get_status.return_value = {
            "status": DownloadStatus.COMPLETED.value,
            "progress": 100.0,
            "source_path": str(torrent_file),
            "ratio": 0.4,
            "seeding_time_seconds": 600,  # 10 minutes
            "error_message": None,
        }

        stats1 = worker.poll_once(db=test_db, staging_dir=str(staging))
        assert stats1["imported"] == 1

        # Assert cleanup_completed was NOT called (hardlink must keep seeding!)
        mock_driver.cleanup_completed.assert_not_called()

        # Assert active download is kept in COMPLETED (seeding) status in DB
        dl_row1 = test_db.get_active_download("dl-seeding-test")
        assert dl_row1 is not None
        assert dl_row1["status"] == DownloadStatus.COMPLETED.value
        assert dl_row1["target_path"] is not None

        # Assert hardlink file was created in music library
        placed_path = Path(dl_row1["target_path"])
        assert placed_path.exists()
        # Verify torrent file is untouched: still exists, same inode, same bytes
        assert torrent_file.exists()
        assert torrent_file.stat().st_ino == torrent_ino_before
        assert torrent_file.read_bytes() == torrent_bytes_before

        # --- Tick 2: Subsequent poll, ratio reaches 1.5, seeding time 35 min (still below limits) ---
        mock_driver.get_status.return_value = {
            "status": DownloadStatus.COMPLETED.value,
            "progress": 100.0,
            "source_path": str(torrent_file),
            "ratio": 1.5,
            "seeding_time_seconds": 2100,  # 35 minutes
            "error_message": None,
        }

        stats2 = worker.poll_once(db=test_db, staging_dir=str(staging))
        mock_driver.cleanup_completed.assert_not_called()

        dl_row2 = test_db.get_active_download("dl-seeding-test")
        assert dl_row2["status"] == DownloadStatus.COMPLETED.value

        # --- Tick 3: Subsequent poll, ratio reaches 2.1 (>= 2.0 limit reached!) ---
        mock_driver.get_status.return_value = {
            "status": DownloadStatus.COMPLETED.value,
            "progress": 100.0,
            "source_path": str(torrent_file),
            "ratio": 2.1,
            "seeding_time_seconds": 2700,  # 45 minutes
            "error_message": None,
        }

        stats3 = worker.poll_once(db=test_db, staging_dir=str(staging))
        # Now cleanup_completed MUST be called without deleting files!
        mock_driver.cleanup_completed.assert_called_once_with(
            "hash-seed-12345", delete_files=False
        )

        dl_row3 = test_db.get_active_download("dl-seeding-test")
        assert dl_row3["status"] == DownloadStatus.IMPORTED.value
        assert placed_path.exists()  # Hardlinked file preserved!


def test_seeding_governance_time_limit_only(test_db: Database, tmp_path: Path):
    """Verifies seeding governance when only seed_time_limit_minutes is configured."""
    staging = tmp_path / "staging"
    music = tmp_path / "music"
    staging.mkdir(parents=True)
    music.mkdir(parents=True)

    test_db.update_media_management_settings(
        {
            "import_mode": "hardlink",
            "delete_completed_transfers": True,
            "seed_ratio_limit": None,
            "seed_time_limit_minutes": 30,
            "root_folder_path": str(music),
            "staging_folder_path": str(staging),
        }
    )

    test_db.create_download_client(
        DownloadClientConfig(
            id="c-time-client",
            name="Time Seeder",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://127.0.0.1:8080",
            enabled=True,
        )
    )

    tfile = staging / "time_track.flac"
    write_flac(tfile)

    test_db.create_active_download(
        ActiveDownload(
            id="dl-time-test",
            title="Artist - Track [FLAC]",
            artist="Artist",
            client_id="c-time-client",
            download_hash="hash-time-111",
            status=DownloadStatus.COMPLETED.value,
            source_path=str(tfile),
        )
    )

    mock_driver = MagicMock()
    mock_driver.cleanup_completed.return_value = True

    worker = AcquisitionWorker()

    with patch(
        "plex_playlist_sync.acquisition_worker.get_acquisition_driver",
        return_value=mock_driver,
    ):
        # 15 minutes (< 30 minutes)
        mock_driver.get_status.return_value = {
            "status": DownloadStatus.COMPLETED.value,
            "progress": 100.0,
            "source_path": str(tfile),
            "ratio": 0.2,
            "seeding_time_seconds": 900,
            "error_message": None,
        }
        worker.poll_once(db=test_db, staging_dir=str(staging))
        mock_driver.cleanup_completed.assert_not_called()

        # 31 minutes (>= 30 minutes)
        mock_driver.get_status.return_value = {
            "status": DownloadStatus.COMPLETED.value,
            "progress": 100.0,
            "source_path": str(tfile),
            "ratio": 0.5,
            "seeding_time_seconds": 1860,
            "error_message": None,
        }
        worker.poll_once(db=test_db, staging_dir=str(staging))
        mock_driver.cleanup_completed.assert_called_once_with(
            "hash-time-111", delete_files=False
        )


def test_qbittorrent_driver_get_status_ratio_and_time():
    """Verifies QbittorrentDriver.get_status extracts ratio and seeding_time_seconds."""
    driver = QbittorrentDriver(host_url="http://127.0.0.1:8080")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {
            "hash": "abc123hash",
            "name": "Radiohead - In Rainbows",
            "state": "uploading",
            "progress": 1.0,
            "size": 50000000,
            "dlspeed": 0,
            "eta": 0,
            "content_path": "/downloads/Radiohead - In Rainbows",
            "ratio": 1.85,
            "seeding_time": 4500,
        }
    ]

    with patch("httpx.Client.get", return_value=mock_resp):
        status = driver.get_status("abc123hash")

    assert status["status"] == DownloadStatus.COMPLETED.value
    assert status["progress"] == 100.0
    assert status["ratio"] == 1.85
    assert status["seeding_time_seconds"] == 4500

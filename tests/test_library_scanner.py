"""Unit tests for LibraryScanner recursive filesystem scanning and ingestion engine."""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plex_playlist_sync.library_scanner import LibraryScanner
from plex_playlist_sync.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    """Provides a fresh disk-backed Database for isolated testing."""
    db_file = tmp_path / "test_scanner.db"
    database = Database(db_file)
    yield database
    database.close()


@pytest.fixture
def scanner():
    """Provides a fresh LibraryScanner instance for each test."""
    return LibraryScanner()


def test_scan_populates_library_hierarchy(db: Database, scanner: LibraryScanner, tmp_path: Path):
    """Verifies scanning populates artist, album, track, and file hierarchy."""
    music_dir = tmp_path / "music"
    album_dir = music_dir / "Pink Floyd" / "The Wall"
    album_dir.mkdir(parents=True)

    track1_file = album_dir / "01 - In the Flesh.flac"
    track2_file = album_dir / "02 - The Thin Ice.mp3"
    track1_file.write_bytes(b"dummy flac content")
    track2_file.write_bytes(b"dummy mp3 content")

    mock_metadata = {
        str(track1_file.resolve()): {
            "title": "In the Flesh?",
            "artist": "Pink Floyd",
            "album": "The Wall",
            "track_number": 1,
            "disc_number": 1,
            "year": 1979,
            "total_tracks": 26,
            "duration": 196.0,
            "codec": "FLAC",
            "bitrate": 900000,
            "sample_rate": 44100,
            "bits_per_sample": 16,
            "quality_full": "FLAC 16bit 44.1kHz",
            "file_path": str(track1_file.resolve()),
        },
        str(track2_file.resolve()): {
            "title": "The Thin Ice",
            "artist": "Pink Floyd",
            "album": "The Wall",
            "track_number": 2,
            "disc_number": 1,
            "year": 1979,
            "total_tracks": 26,
            "duration": 149.0,
            "codec": "MP3",
            "bitrate": 320000,
            "sample_rate": 44100,
            "bits_per_sample": 16,
            "quality_full": "MP3 320kbps",
            "file_path": str(track2_file.resolve()),
        },
    }

    def fake_inspect(path: Path | str):
        resolved = str(Path(path).resolve())
        if resolved in mock_metadata:
            return mock_metadata[resolved]
        raise ValueError(f"Unknown file: {path}")

    with patch("plex_playlist_sync.library_scanner.inspect_audio_file", side_effect=fake_inspect):
        status = scanner.scan(db, root_folder=str(music_dir))

    assert status["status"] == "completed"
    assert status["total_files_found"] == 2
    assert status["files_indexed"] == 2
    assert status["artists_created"] == 1
    assert status["albums_created"] == 1
    assert status["tracks_created"] == 2

    # Check Artist
    artist = db.get_library_artist_by_name("Pink Floyd")
    assert artist is not None
    assert artist["name"] == "Pink Floyd"
    artist_id = artist["id"]

    # Check Album via lookup helper
    album = db.get_library_album_by_title(artist_id, "The Wall")
    assert album is not None
    assert album["title"] == "The Wall"
    assert album["year"] == 1979
    album_id = album["id"]

    # Check Tracks via lookup helper
    track1 = db.get_library_track_by_title(album_id, "In the Flesh?", track_number=1)
    assert track1 is not None
    assert track1["track_number"] == 1
    assert track1["artist_id"] == artist_id

    track2 = db.get_library_track_by_title(album_id, "The Thin Ice", track_number=2)
    assert track2 is not None
    assert track2["track_number"] == 2

    # Check Files via lookup helper
    file1 = db.get_library_file_by_path(str(track1_file.resolve()))
    assert file1 is not None
    assert file1["track_id"] == track1["id"]
    assert file1["codec"] == "FLAC"
    assert file1["relative_path"] == "Pink Floyd/The Wall/01 - In the Flesh.flac"

    file2 = db.get_library_file_by_path(str(track2_file.resolve()))
    assert file2 is not None
    assert file2["track_id"] == track2["id"]
    assert file2["codec"] == "MP3"


def test_scan_skips_in_lidarr_mode(db: Database, scanner: LibraryScanner, tmp_path: Path):
    """Verifies that scanning returns 'skipped' early when library_mode is 'lidarr'."""
    db.update_media_management_settings({"library_mode": "lidarr"})
    settings = db.get_media_management_settings()
    assert settings["library_mode"] == "lidarr"

    music_dir = tmp_path / "music"
    album_dir = music_dir / "Daft Punk" / "Discovery"
    album_dir.mkdir(parents=True)
    (album_dir / "01 - One More Time.flac").write_bytes(b"dummy")

    status = scanner.scan(db, root_folder=str(music_dir))

    assert status["status"] == "skipped"
    assert status["is_scanning"] is False
    assert status["files_indexed"] == 0

    stats = db.get_library_stats()
    assert stats["artist_count"] == 0
    assert stats["album_count"] == 0
    assert stats["track_count"] == 0
    assert stats["file_count"] == 0


def test_prune_missing_files(db: Database, scanner: LibraryScanner, tmp_path: Path):
    """Verifies that prune_missing deletes database rows for missing disk files."""
    music_dir = tmp_path / "music"
    album_dir = music_dir / "Radiohead" / "OK Computer"
    album_dir.mkdir(parents=True)

    track1_file = album_dir / "01 - Airbag.flac"
    track2_file = album_dir / "02 - Paranoid Android.flac"
    track1_file.write_bytes(b"flac data 1")
    track2_file.write_bytes(b"flac data 2")

    # Initial scan indexes both files
    status1 = scanner.scan(db, root_folder=str(music_dir))
    assert status1["status"] == "completed"
    assert status1["files_indexed"] == 2

    file1 = db.get_library_file_by_path(str(track1_file.resolve()))
    file2 = db.get_library_file_by_path(str(track2_file.resolve()))
    assert file1 is not None
    assert file2 is not None

    # Delete track 2 from disk
    track2_file.unlink()
    assert not track2_file.exists()

    # Re-scan with prune_missing=True
    status2 = scanner.scan(db, root_folder=str(music_dir), prune_missing=True)
    assert status2["status"] == "completed"
    assert status2["files_pruned"] == 1

    # File 1 remains in DB, File 2 is pruned
    assert db.get_library_file_by_path(str(track1_file.resolve())) is not None
    assert db.get_library_file_by_path(str(track2_file.resolve())) is None


def test_scan_cancellation(db: Database, scanner: LibraryScanner, tmp_path: Path):
    """Verifies that cancel_scan halts execution and transitions status to 'cancelled'."""
    music_dir = tmp_path / "music"
    music_dir.mkdir(parents=True)

    # Populate directory with 20 dummy files
    for i in range(20):
        track_dir = music_dir / f"Artist {i}" / "Album"
        track_dir.mkdir(parents=True)
        (track_dir / f"0{i} - Track.flac").write_bytes(b"audio data")

    # Hook inspect_audio_file to simulate slow extraction and trigger cancel
    original_inspect = scanner.scan

    def slow_inspect(file_path):
        scanner.cancel_scan()
        time.sleep(0.01)
        return {
            "title": "Track",
            "artist": "Artist",
            "album": "Album",
            "track_number": 1,
            "disc_number": 1,
            "codec": "FLAC",
            "quality_full": "FLAC 16bit",
            "file_path": str(file_path),
        }

    with patch("plex_playlist_sync.library_scanner.inspect_audio_file", side_effect=slow_inspect):
        result = scanner.scan(db, root_folder=str(music_dir))

    assert result["status"] == "cancelled"
    assert result["is_scanning"] is False
    assert scanner.is_running() is False
    assert result["processed_files"] < 20


def test_cutoff_evaluation_during_scan(db: Database, scanner: LibraryScanner, tmp_path: Path):
    """Verifies cutoff_met evaluation distinguishes formats meeting cutoff vs below cutoff."""
    music_dir = tmp_path / "music"
    album_dir = music_dir / "Miles Davis" / "Kind of Blue"
    album_dir.mkdir(parents=True)

    high_q_file = album_dir / "01 - So What.flac"
    low_q_file = album_dir / "02 - Freddie Freeloader.mp3"
    high_q_file.write_bytes(b"high quality flac")
    low_q_file.write_bytes(b"low quality mp3")

    mock_metadata = {
        str(high_q_file.resolve()): {
            "title": "So What",
            "artist": "Miles Davis",
            "album": "Kind of Blue",
            "track_number": 1,
            "disc_number": 1,
            "codec": "FLAC",
            "bitrate": 900000,
            "sample_rate": 44100,
            "bits_per_sample": 16,
            "quality_full": "FLAC 16bit 44.1kHz",
            "file_path": str(high_q_file.resolve()),
        },
        str(low_q_file.resolve()): {
            "title": "Freddie Freeloader",
            "artist": "Miles Davis",
            "album": "Kind of Blue",
            "track_number": 2,
            "disc_number": 1,
            "codec": "MP3",
            "bitrate": 128000,
            "sample_rate": 44100,
            "bits_per_sample": 16,
            "quality_full": "MP3 128kbps",
            "file_path": str(low_q_file.resolve()),
        },
    }

    def fake_inspect(path):
        return mock_metadata[str(Path(path).resolve())]

    with patch("plex_playlist_sync.library_scanner.inspect_audio_file", side_effect=fake_inspect):
        res = scanner.scan(db, root_folder=str(music_dir))

    assert res["status"] == "completed"

    file_high = db.get_library_file_by_path(str(high_q_file.resolve()))
    assert file_high is not None
    assert file_high["cutoff_met"] is True

    file_low = db.get_library_file_by_path(str(low_q_file.resolve()))
    assert file_low is not None
    assert file_low["cutoff_met"] is False


def test_scan_path_derived_fallback_on_corrupt_files(db: Database, scanner: LibraryScanner, tmp_path: Path):
    """Verifies that Mutagen exceptions fall back cleanly to path-derived artist/album/title tags."""
    music_dir = tmp_path / "music"
    album_dir = music_dir / "Led Zeppelin" / "IV"
    album_dir.mkdir(parents=True)

    corrupt_file = album_dir / "04 - Stairway to Heaven.flac"
    corrupt_file.write_bytes(b"corrupt non-audio junk")

    # Let inspect_audio_file raise a realistic Mutagen parsing failure
    with patch(
        "plex_playlist_sync.library_scanner.inspect_audio_file",
        side_effect=ValueError("Corrupt flac header"),
    ):
        status = scanner.scan(db, root_folder=str(music_dir))

    assert status["status"] == "completed"
    assert status["files_indexed"] == 1

    artist = db.get_library_artist_by_name("Led Zeppelin")
    assert artist is not None

    album = db.get_library_album_by_title(artist["id"], "IV")
    assert album is not None

    track = db.get_library_track_by_title(album["id"], "Stairway to Heaven", track_number=4)  # name parsed from "04 - ..."
    assert track is not None

    fl = db.get_library_file_by_path(str(corrupt_file.resolve()))
    assert fl is not None
    assert fl["track_id"] == track["id"]


def test_scan_nonexistent_root_fails_gracefully(db: Database, scanner: LibraryScanner, tmp_path: Path):
    """Verifies that a missing root folder logs an error and returns status 'failed'."""
    missing_dir = tmp_path / "does_not_exist"
    status = scanner.scan(db, root_folder=str(missing_dir))

    assert status["status"] == "failed"
    assert "Root folder does not exist" in status["error"]
    assert status["is_scanning"] is False


def test_start_scan_background_execution(db: Database, scanner: LibraryScanner, tmp_path: Path):
    """Verifies that start_scan spawns a background thread and updates status asynchronously."""
    music_dir = tmp_path / "music"
    album_dir = music_dir / "Tool" / "Lateralus"
    album_dir.mkdir(parents=True)
    (album_dir / "01 - The Grudge.flac").write_bytes(b"flac data")

    mock_metadata = {
        "title": "The Grudge",
        "artist": "Tool",
        "album": "Lateralus",
        "track_number": 1,
        "codec": "FLAC",
        "quality_full": "FLAC 16bit",
        "file_path": str((album_dir / "01 - The Grudge.flac").resolve()),
    }

    with patch("plex_playlist_sync.library_scanner.inspect_audio_file", return_value=mock_metadata):
        started = scanner.start_scan(db, root_folder=str(music_dir))
        assert started is True

        # Second start while running should return False
        started_again = scanner.start_scan(db, root_folder=str(music_dir))
        assert started_again is False

        # Wait for thread completion
        if scanner._thread:
            scanner._thread.join(timeout=5.0)

    assert scanner.is_running() is False
    status = scanner.get_status()
    assert status["status"] == "completed"
    assert status["files_indexed"] == 1


def test_plex_client_refresh_called_when_provided(db: Database, scanner: LibraryScanner, tmp_path: Path):
    """Verifies that refresh_music_library is called on the provided plex_client."""
    music_dir = tmp_path / "music"
    music_dir.mkdir(parents=True)

    mock_plex = MagicMock()

    status = scanner.scan(db, root_folder=str(music_dir), plex_client=mock_plex)
    assert status["status"] == "completed"
    mock_plex.refresh_music_library.assert_called_once()

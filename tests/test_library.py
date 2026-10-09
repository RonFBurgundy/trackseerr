"""Unit tests for audio metadata inspector and collision detection in library.py."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from trackseerr.library import (
    _extract_year,
    _parse_num_total,
    detect_path_collision,
    inspect_audio_file,
    resolve_collision,
)


class TestLibraryHelpers:
    """Validates low-level parsing helpers."""

    def test_parse_num_total_variations(self):
        assert _parse_num_total("5/12") == (5, 12)
        assert _parse_num_total("3") == (3, None)
        assert _parse_num_total((2, 10)) == (2, 10)
        assert _parse_num_total([1]) == (1, None)
        assert _parse_num_total(4) == (4, None)
        assert _parse_num_total(None) == (None, None)

    def test_extract_year_variations(self):
        assert _extract_year("1997-05-21") == 1997
        assert _extract_year("2023") == 2023
        assert _extract_year("Released in 1984 somewhere") == 1984
        assert _extract_year("") is None
        assert _extract_year(None) is None


class TestCollisionDetection:
    """Validates collision detection and incremental path renaming."""

    def test_detect_path_collision(self, tmp_path):
        real_file = tmp_path / "song.flac"
        real_file.touch()

        assert detect_path_collision(real_file) is True
        assert detect_path_collision(tmp_path / "nonexistent.flac") is False

        # In-memory set detection
        in_memory_paths = {"/music/Pink Floyd/track.flac"}
        assert detect_path_collision("/music/Pink Floyd/track.flac", in_memory_paths) is True
        assert detect_path_collision("/music/Pink Floyd/other.flac", in_memory_paths) is False

    def test_resolve_collision(self, tmp_path):
        f1 = tmp_path / "track.flac"
        f1.touch()
        f2 = tmp_path / "track (1).flac"
        f2.touch()

        # Should increment to track (2).flac
        resolved = resolve_collision(f1)
        assert resolved.name == "track (2).flac"

        # In-memory collision resolution
        simulated = {"/music/song.mp3", "/music/song (1).mp3"}
        resolved_mem = resolve_collision("/music/song.mp3", existing_paths=simulated)
        assert resolved_mem == Path("/music/song (2).mp3")


class TestInspectAudioFile:
    """Validates audio tag extraction across container formats."""

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            inspect_audio_file(tmp_path / "ghost.flac")

    def test_unsupported_file_raises_value_error(self, tmp_path):
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("not audio")
        with pytest.raises(ValueError, match="Unsupported audio"):
            inspect_audio_file(txt_file)

    def test_flac_inspection_mock(self, tmp_path):
        dummy = tmp_path / "test.flac"
        dummy.touch()

        mock_flac = MagicMock(spec=FLAC)
        mock_flac.tags = {
            "title": ["Comfortably Numb"],
            "artist": ["Pink Floyd"],
            "album": ["The Wall"],
            "albumartist": ["Pink Floyd"],
            "date": ["1979-11-30"],
            "tracknumber": ["6/13"],
            "discnumber": ["2/2"],
        }
        mock_flac.info = MagicMock()
        mock_flac.info.sample_rate = 96000
        mock_flac.info.bits_per_sample = 24
        mock_flac.info.bitrate = 1411000
        mock_flac.info.length = 382.5

        with patch("mutagen.File", return_value=mock_flac):
            meta = inspect_audio_file(dummy)

            assert meta["title"] == "Comfortably Numb"
            assert meta["artist"] == "Pink Floyd"
            assert meta["album"] == "The Wall"
            assert meta["album_artist"] == "Pink Floyd"
            assert meta["year"] == 1979
            assert meta["track_number"] == 6
            assert meta["total_tracks"] == 13
            assert meta["disc_number"] == 2
            assert meta["total_discs"] == 2
            assert meta["codec"] == "FLAC"
            assert meta["sample_rate"] == 96000
            assert meta["bits_per_sample"] == 24
            assert meta["duration"] == 382.5
            assert meta["quality_full"] == "FLAC 24bit 96kHz"

    def test_mp3_inspection_mock(self, tmp_path):
        dummy = tmp_path / "song.mp3"
        dummy.touch()

        mock_mp3 = MagicMock(spec=MP3)
        mock_tags = {}

        def make_frame(val):
            f = MagicMock()
            f.text = [val]
            return f

        mock_tags["TIT2"] = make_frame("Paranoid Android")
        mock_tags["TPE1"] = make_frame("Radiohead")
        mock_tags["TALB"] = make_frame("OK Computer")
        mock_tags["TPE2"] = make_frame("Radiohead")
        mock_tags["TYER"] = make_frame("1997")
        mock_tags["TRCK"] = make_frame("2/12")
        mock_tags["TPOS"] = make_frame("1/1")

        mock_mp3.tags = mock_tags
        mock_mp3.info = MagicMock()
        mock_mp3.info.sample_rate = 44100
        mock_mp3.info.bitrate = 320000
        mock_mp3.info.bits_per_sample = None
        mock_mp3.info.length = 387.0

        with patch("mutagen.File", return_value=mock_mp3):
            meta = inspect_audio_file(dummy)

            assert meta["title"] == "Paranoid Android"
            assert meta["artist"] == "Radiohead"
            assert meta["album"] == "OK Computer"
            assert meta["year"] == 1997
            assert meta["track_number"] == 2
            assert meta["total_tracks"] == 12
            assert meta["codec"] == "MP3"
            assert meta["quality_full"] == "MP3 320kbps"

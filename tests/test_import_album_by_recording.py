"""Tests for filing imported and scanned tracks under an existing album by recording release group."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from tests.audio_fixtures import write_flac
from trackseerr.acquisition_worker import AcquisitionWorker
from trackseerr.library_scanner import LibraryScanner
from trackseerr.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    LibraryAlbum,
    LibraryArtist,
)
from trackseerr.release_matching import album_for_recording
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    """Provides an isolated disk-backed Database instance."""
    db_file = tmp_path / "test_import_rec.db"
    database = Database(db_file)
    yield database
    database.close()


class _FakeEnricher:
    def __init__(self, groups_by_rec: dict[str, list[dict[str, Any]]] | None = None, error: Exception | None = None):
        self.groups_by_rec = groups_by_rec or {}
        self.error = error
        self.calls: list[str] = []

    def get_recording_release_groups(self, recording_id: str) -> list[dict[str, Any]]:
        self.calls.append(recording_id)
        if self.error is not None:
            raise self.error
        return list(self.groups_by_rec.get(recording_id, []))


def _patch_enricher(monkeypatch: pytest.MonkeyPatch, enricher: _FakeEnricher) -> None:
    monkeypatch.setattr("trackseerr.mb_metadata_store.get_shared_enricher", lambda _db: enricher)


def test_album_for_recording_prefers_studio_album_over_compilation(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    artist = db.upsert_library_artist(LibraryArtist(id="art-beatles", name="The Beatles"))
    comp_album = db.upsert_library_album(
        LibraryAlbum(
            id="alb-comp",
            artist_id=artist["id"],
            title="1",
            year=2000,
            album_type="compilation",
            mb_release_group_id="rg-comp",
        )
    )
    studio_album = db.upsert_library_album(
        LibraryAlbum(
            id="alb-abbey",
            artist_id=artist["id"],
            title="Abbey Road",
            year=1969,
            album_type="album",
            mb_release_group_id="rg-abbey",
        )
    )

    enricher = _FakeEnricher(
        {
            "rec-something": [
                {
                    "id": "rg-comp",
                    "title": "1",
                    "primary_type": "Album",
                    "secondary_types": ["Compilation"],
                    "first_release_date": "2000-11-13",
                },
                {
                    "id": "rg-abbey",
                    "title": "Abbey Road",
                    "primary_type": "Album",
                    "secondary_types": [],
                    "first_release_date": "1969-09-26",
                },
            ]
        }
    )
    _patch_enricher(monkeypatch, enricher)

    matched = album_for_recording(db, artist["id"], "rec-something")
    assert matched is not None
    assert matched["id"] == studio_album["id"]
    assert matched["title"] == "Abbey Road"

    # When only the compilation is held by the artist, it is used as fallback.
    db.delete_library_album(studio_album["id"])
    fallback = album_for_recording(db, artist["id"], "rec-something")
    assert fallback is not None
    assert fallback["id"] == comp_album["id"]


def test_album_for_recording_none_without_holder(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    artist = db.upsert_library_artist(LibraryArtist(id="art-beatles", name="The Beatles"))
    other_artist = db.upsert_library_artist(LibraryArtist(id="art-other", name="Other Artist"))
    db.upsert_library_album(
        LibraryAlbum(
            id="alb-other",
            artist_id=other_artist["id"],
            title="Abbey Road",
            mb_release_group_id="rg-abbey",
        )
    )

    enricher = _FakeEnricher(
        {
            "rec-something": [
                {
                    "id": "rg-abbey",
                    "title": "Abbey Road",
                    "primary_type": "Album",
                    "secondary_types": [],
                }
            ]
        }
    )
    _patch_enricher(monkeypatch, enricher)

    assert album_for_recording(db, "", "rec-something") is None
    assert album_for_recording(db, artist["id"], "") is None
    assert enricher.calls == []

    assert album_for_recording(db, artist["id"], "rec-something") is None
    assert enricher.calls == ["rec-something"]


def test_album_for_recording_none_on_lookup_error(
    db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    artist = db.upsert_library_artist(LibraryArtist(id="art-beatles", name="The Beatles"))
    enricher = _FakeEnricher(error=requests.RequestException("MB mirror timeout"))
    _patch_enricher(monkeypatch, enricher)

    with caplog.at_level(logging.WARNING, logger="trackseerr.release_matching"):
        result = album_for_recording(db, artist["id"], "rec-something")

    assert result is None
    assert any("rec-something" in rec.message for rec in caplog.records)


def test_import_files_single_under_existing_album(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    downloads_dir = tmp_path / "downloads"
    music_dir = tmp_path / "music"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    music_dir.mkdir(parents=True, exist_ok=True)

    settings = db.get_media_management_settings()
    settings.update(
        {
            "root_folder_path": str(music_dir),
            "library_mode": "native",
            "staging_folder_path": str(downloads_dir),
            "enrich_mbids": False,
            "write_audio_tags": False,
        }
    )
    db.update_media_management_settings(settings)

    db.create_download_client(
        DownloadClientConfig(
            id="client-qbit-1",
            name="Test Qbit",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://qbit:8080",
        )
    )

    artist = db.upsert_library_artist(
        LibraryArtist(id="art-beatles", name="The Beatles", path=str(music_dir / "The Beatles"))
    )
    abbey_road = db.upsert_library_album(
        LibraryAlbum(
            id="alb-abbey-road",
            artist_id=artist["id"],
            title="Abbey Road",
            year=1969,
            album_type="album",
            mb_release_group_id="rg-abbey-road",
            path=str(music_dir / "The Beatles" / "Abbey Road"),
        )
    )

    enricher = _FakeEnricher(
        {
            "rec-something": [
                {
                    "id": "rg-abbey-road",
                    "title": "Abbey Road",
                    "primary_type": "Album",
                    "secondary_types": [],
                    "first_release_date": "1969-09-26",
                }
            ]
        }
    )
    _patch_enricher(monkeypatch, enricher)

    download_folder = downloads_dir / "The_Beatles_Something"
    download_folder.mkdir(parents=True, exist_ok=True)
    audio_file = download_folder / "02_Something.flac"
    write_flac(audio_file)

    db.create_active_download(
        ActiveDownload(
            id="dl-beatles-something",
            client_id="client-qbit-1",
            title="The Beatles - Something [FLAC]",
            artist="The Beatles",
            item_type="track",
            status=DownloadStatus.COMPLETED.value,
            download_hash="hash-something",
            size_bytes=audio_file.stat().st_size,
        )
    )

    mock_driver = MagicMock()
    mock_driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value,
        "progress": 100.0,
        "size_bytes": audio_file.stat().st_size,
        "source_path": str(audio_file),
    }

    mock_meta = {
        "artist": "The Beatles",
        "title": "Something",
        "album": "Something - Remastered 2009",
        "year": 2009,
        "codec": "FLAC",
        "bitrate": 900,
        "sample_rate": 44100,
        "bits_per_sample": 16,
        "duration": 182.0,
        "track_number": 1,
        "disc_number": 1,
        "total_discs": 1,
        "total_tracks": 1,
        "musicbrainz_trackid": "rec-something",
        "file_path": str(audio_file),
    }

    worker = AcquisitionWorker()
    worker.staging_dir = str(downloads_dir)

    with (
        caplog.at_level(logging.INFO, logger="trackseerr.acquisition_catalog"),
        patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver),
        patch("trackseerr.acquisition_import.inspect_audio_file", return_value=dict(mock_meta)),
        patch("trackseerr.acquisition_catalog.inspect_audio_file", return_value=dict(mock_meta)),
    ):
        stats = worker.poll_once(db=db, staging_dir=str(downloads_dir))

    assert stats["imported"] == 1
    albums = db.list_library_albums(artist_id=artist["id"])
    assert [a["id"] for a in albums] == [abbey_road["id"]]

    tracks = db.list_library_tracks(album_id=abbey_road["id"])
    assert len(tracks) == 1
    assert tracks[0]["title"] == "Something"
    assert db.get_library_file_for_track(tracks[0]["id"]) is not None
    assert any("Filed Something under existing album Abbey Road" in r.message for r in caplog.records)


def test_scan_files_single_under_existing_album(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    music_dir = tmp_path / "music"
    single_dir = music_dir / "The Beatles" / "Something - Remastered 2009"
    single_dir.mkdir(parents=True)

    track1_file = single_dir / "01 - Something.flac"
    track2_file = single_dir / "02 - Come Together.flac"
    track1_file.write_bytes(b"dummy flac content 1")
    track2_file.write_bytes(b"dummy flac content 2")

    artist = db.upsert_library_artist(
        LibraryArtist(id="art-beatles", name="The Beatles", path=str(music_dir / "The Beatles"))
    )
    abbey_road = db.upsert_library_album(
        LibraryAlbum(
            id="alb-abbey-road",
            artist_id=artist["id"],
            title="Abbey Road",
            year=1969,
            album_type="album",
            mb_release_group_id="rg-abbey-road",
            path=str(music_dir / "The Beatles" / "Abbey Road"),
        )
    )

    enricher = _FakeEnricher(
        {
            "rec-something": [
                {
                    "id": "rg-abbey-road",
                    "title": "Abbey Road",
                    "primary_type": "Album",
                    "secondary_types": [],
                    "first_release_date": "1969-09-26",
                }
            ]
        }
    )
    _patch_enricher(monkeypatch, enricher)

    mock_metadata = {
        str(track1_file.resolve()): {
            "title": "Something",
            "artist": "The Beatles",
            "album": "Something - Remastered 2009",
            "track_number": 2,
            "disc_number": 1,
            "year": 2009,
            "total_tracks": 2,
            "duration": 182.0,
            "codec": "FLAC",
            "bitrate": 900000,
            "sample_rate": 44100,
            "bits_per_sample": 16,
            "quality_full": "FLAC 16bit 44.1kHz",
            "musicbrainz_trackid": "rec-something",
            "file_path": str(track1_file.resolve()),
        },
        str(track2_file.resolve()): {
            "title": "Come Together",
            "artist": "The Beatles",
            "album": "Something - Remastered 2009",
            "track_number": 1,
            "disc_number": 1,
            "year": 2009,
            "total_tracks": 2,
            "duration": 259.0,
            "codec": "FLAC",
            "bitrate": 900000,
            "sample_rate": 44100,
            "bits_per_sample": 16,
            "quality_full": "FLAC 16bit 44.1kHz",
            "musicbrainz_trackid": "rec-come-together",
            "file_path": str(track2_file.resolve()),
        },
    }

    def fake_inspect(path: Path | str) -> dict[str, Any]:
        resolved = str(Path(path).resolve())
        if resolved in mock_metadata:
            return mock_metadata[resolved]
        raise ValueError(f"Unknown file: {path}")

    scanner = LibraryScanner()
    with patch("trackseerr.library_scanner.inspect_audio_file", side_effect=fake_inspect):
        status = scanner.scan(db, root_folder=str(music_dir))

    assert status["status"] == "completed"
    assert status["files_indexed"] == 2
    assert status["albums_created"] == 0

    albums = db.list_library_albums(artist_id=artist["id"])
    assert [a["id"] for a in albums] == [abbey_road["id"]]

    tracks = db.list_library_tracks(album_id=abbey_road["id"])
    assert {t["title"] for t in tracks} == {"Something", "Come Together"}
    # Second file in same folder hits run.album_cache without repeating the network call.
    assert enricher.calls == ["rec-something"]

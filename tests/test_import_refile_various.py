"""Tests for post-import Various Artists album refiling in acquisition_catalog."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trackseerr.acquisition_catalog import _refile_imported_various_albums
from trackseerr.compilation_filing import various_artists_id
from trackseerr.models import LibraryAlbum, LibraryFile, LibraryTrack
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "test_import_refile_various.db"))
    yield database
    database.close()


def _seed_va_files(db: Database, tmp_path: Path, count: int = 2) -> list[str]:
    va_id = various_artists_id(db)
    db.upsert_library_album(
        LibraryAlbum(
            id="alb-va",
            artist_id=va_id,
            title="Now That's What I Call Music! 44",
            mb_release_group_id="rg-now44",
            monitored=False,
            total_tracks=count,
        )
    )
    paths: list[str] = []
    for idx in range(1, count + 1):
        p = tmp_path / f"track_{idx}.flac"
        p.write_bytes(b"fake")
        track_id = f"alb-va-{idx}"
        db.upsert_library_track(
            LibraryTrack(
                id=track_id,
                album_id="alb-va",
                artist_id=va_id,
                track_number=idx,
                title=f"Track {idx}",
                monitored=False,
            )
        )
        db.upsert_library_file(
            LibraryFile(
                id=f"file-{idx}",
                track_id=track_id,
                file_path=str(p),
                relative_path=p.name,
                codec="FLAC",
            )
        )
        paths.append(str(p))
    return paths


def test_import_refiles_touched_va_album_once(db: Database, tmp_path: Path):
    paths = _seed_va_files(db, tmp_path, count=2)
    fake_enricher = MagicMock()
    fake_discovery = MagicMock()

    with (
        patch("trackseerr.mb_metadata_store.get_shared_enricher", return_value=fake_enricher),
        patch("trackseerr.mb_metadata_store.get_shared_discovery_client", return_value=fake_discovery),
        patch("trackseerr.acquisition_catalog.refile_various_album") as mock_refile,
    ):
        _refile_imported_various_albums(db, paths)

    mock_refile.assert_called_once_with(db, fake_enricher, "alb-va", discovery=fake_discovery)


def test_import_refile_failure_does_not_break_import(db: Database, tmp_path: Path, caplog):
    paths = _seed_va_files(db, tmp_path, count=1)
    fake_enricher = MagicMock()
    fake_discovery = MagicMock()

    with (
        caplog.at_level(logging.WARNING),
        patch("trackseerr.mb_metadata_store.get_shared_enricher", return_value=fake_enricher),
        patch("trackseerr.mb_metadata_store.get_shared_discovery_client", return_value=fake_discovery),
        patch(
            "trackseerr.acquisition_catalog.refile_various_album",
            side_effect=sqlite3.OperationalError("database is locked"),
        ),
    ):
        _refile_imported_various_albums(db, paths)

    assert any("Post-import Various Artists refile failed" in rec.message for rec in caplog.records)

"""The Wanted sweep must not search Missing playlist rows whose song is already owned."""

from pathlib import Path

import pytest

from trackseerr.backlog_worker import WantedBacklogWorker, _SweepCollect
from trackseerr.storage import Database


@pytest.fixture
def test_db(tmp_path: Path):
    db = Database(str(tmp_path / "missing_owned.db"))
    try:
        yield db
    finally:
        db.close()


def _own(db: Database, title: str) -> None:
    db.upsert_library_artist({"id": "art-dd", "name": "Dr. Dog", "monitored": True})
    db.upsert_library_album({"id": "alb-dd", "artist_id": "art-dd", "title": "Album", "monitored": True})
    db.upsert_library_track(
        {"id": "trk-dd", "album_id": "alb-dd", "artist_id": "art-dd", "title": title, "monitored": True}
    )
    db.upsert_library_file({"track_id": "trk-dd", "file_path": "/music/Dr. Dog/x.flac", "cutoff_met": True})


def _collect(db: Database, rows: list[dict]) -> list[dict]:
    worker = WantedBacklogWorker.__new__(WantedBacklogWorker)
    db.get_missing_tracks = lambda: rows  # type: ignore[method-assign]
    return worker._collect_unfulfilled_missing(_SweepCollect(db=db))


def _row(title: str, artist: str = "Dr. Dog") -> dict:
    return {"id": 1, "playlist_id": "p1", "title": title, "artist": artist, "lidarr_status": "unmonitored"}


def test_owned_missing_row_not_searched(test_db: Database) -> None:
    _own(test_db, "Heart It Races (Cover Version)")
    assert _collect(test_db, [_row("Heart It Races - Cover Version")]) == []


def test_unowned_missing_row_still_searched(test_db: Database) -> None:
    _own(test_db, "Heart It Races (Cover Version)")
    rows = [_row("Some Other Song")]
    assert _collect(test_db, rows) == rows


def test_instrumental_missing_row_not_satisfied_by_vocal(test_db: Database) -> None:
    _own(test_db, "Legend Has It")
    rows = [_row("Legend Has It (instrumental)")]
    assert _collect(test_db, rows) == rows

"""Tests for fast library stats queries, PRAGMA tuning, and library_albums.monitored index."""

from pathlib import Path
from typing import Any

import pytest

from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    """Provides a fresh disk-backed Database for isolated testing."""
    db_file = tmp_path / "test_library_stats_speed.db"
    database = Database(db_file)
    yield database
    database.close()


def _insert_artist(
    db: Database,
    artist_id: str,
    name: str = "Artist",
    *,
    monitored: bool = True,
    monitor_option: str = "all",
    **kw: Any,
) -> dict[str, Any]:
    return db.upsert_library_artist(
        {"id": artist_id, "name": name, "monitored": monitored, "monitor_option": monitor_option, **kw}
    )


def _insert_album(
    db: Database,
    album_id: str,
    artist_id: str,
    title: str = "Album",
    *,
    monitored: bool = True,
    **kw: Any,
) -> dict[str, Any]:
    return db.upsert_library_album(
        {"id": album_id, "artist_id": artist_id, "title": title, "monitored": monitored, **kw}
    )


def _insert_track(
    db: Database,
    track_id: str,
    album_id: str,
    artist_id: str,
    title: str = "Track",
    *,
    monitored: bool = True,
    **kw: Any,
) -> dict[str, Any]:
    return db.upsert_library_track(
        {
            "id": track_id,
            "album_id": album_id,
            "artist_id": artist_id,
            "title": title,
            "monitored": monitored,
            **kw,
        }
    )


def _insert_file(
    db: Database,
    file_id: str,
    track_id: str,
    *,
    size_bytes: int = 1024,
    **kw: Any,
) -> dict[str, Any]:
    return db.upsert_library_file(
        {
            "id": file_id,
            "track_id": track_id,
            "file_path": f"/music/{file_id}.flac",
            "relative_path": f"{file_id}.flac",
            "codec": "FLAC",
            "quality_name": "FLAC",
            "size_bytes": size_bytes,
            **kw,
        }
    )


def test_track_count_matches_lidarr_semantics(db: Database) -> None:
    """One monitored album with 3 tracks (1 with a file), one unmonitored album

    with 4 tracks (2 with files), one unmonitored album with 2 tracks and no files
    -> track_count == 5, total_track_count == 9, track_file_count == 3.
    """
    _insert_artist(db, "art-1", "Artist 1", monitored=True)

    # Monitored album with 3 tracks (1 has file)
    _insert_album(db, "alb-1", "art-1", "Monitored Album", monitored=True)
    _insert_track(db, "t1-1", "alb-1", "art-1", "Track 1-1", monitored=True)
    _insert_track(db, "t1-2", "alb-1", "art-1", "Track 1-2", monitored=True)
    _insert_track(db, "t1-3", "alb-1", "art-1", "Track 1-3", monitored=True)
    _insert_file(db, "f1-1", "t1-1")

    # Unmonitored album with 4 tracks (2 have files)
    _insert_album(db, "alb-2", "art-1", "Unmonitored Album 1", monitored=False)
    _insert_track(db, "t2-1", "alb-2", "art-1", "Track 2-1", monitored=True)
    _insert_track(db, "t2-2", "alb-2", "art-1", "Track 2-2", monitored=True)
    _insert_track(db, "t2-3", "alb-2", "art-1", "Track 2-3", monitored=True)
    _insert_track(db, "t2-4", "alb-2", "art-1", "Track 2-4", monitored=True)
    _insert_file(db, "f2-1", "t2-1")
    _insert_file(db, "f2-2", "t2-2")

    # Unmonitored album with 2 tracks and no files
    _insert_album(db, "alb-3", "art-1", "Unmonitored Album 2", monitored=False)
    _insert_track(db, "t3-1", "alb-3", "art-1", "Track 3-1", monitored=True)
    _insert_track(db, "t3-2", "alb-3", "art-1", "Track 3-2", monitored=True)

    stats = db.get_library_stats()
    assert stats["track_count"] == 5
    assert stats["total_track_count"] == 9
    assert stats["track_file_count"] == 3


def test_missing_track_count_uses_has_file(db: Database) -> None:
    """Monitored artist + monitored album, 3 monitored tracks, one with a file

    -> missing_track_count == 2; delete that file row -> missing_track_count == 3.
    """
    _insert_artist(db, "art-1", "Artist 1", monitored=True)
    _insert_album(db, "alb-1", "art-1", "Album 1", monitored=True)
    _insert_track(db, "t-1", "alb-1", "art-1", "Track 1", monitored=True)
    _insert_track(db, "t-2", "alb-1", "art-1", "Track 2", monitored=True)
    _insert_track(db, "t-3", "alb-1", "art-1", "Track 3", monitored=True)
    _insert_file(db, "f-1", "t-1")

    # Initially: 3 monitored tracks, 1 has a file -> 2 missing
    stats = db.get_library_stats()
    assert stats["missing_track_count"] == 2

    # Delete the file row; the trg_lib_files_ad trigger sets t-1 has_file = 0
    deleted = db.delete_library_file("f-1")
    assert deleted is True

    has_file = db.conn.execute("SELECT has_file FROM library_tracks WHERE id = 't-1'").fetchone()[0]
    assert has_file == 0

    stats_after = db.get_library_stats()
    assert stats_after["missing_track_count"] == 3


def test_monitored_album_index_exists(db: Database) -> None:
    """PRAGMA index_list(library_albums) contains idx_lib_albums_monitored."""
    indices = {row[1] for row in db.conn.execute("PRAGMA index_list(library_albums)").fetchall()}
    assert "idx_lib_albums_monitored" in indices


def test_connection_pragmas(db: Database) -> None:
    """PRAGMA cache_size returns -65536 and PRAGMA temp_store returns 2."""
    cache_size = db.conn.execute("PRAGMA cache_size").fetchone()[0]
    temp_store = db.conn.execute("PRAGMA temp_store").fetchone()[0]
    assert cache_size == -65536
    assert temp_store == 2

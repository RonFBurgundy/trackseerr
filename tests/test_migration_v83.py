"""v83 migration: Clear Lidarr's implausible album year and date sentinels."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def test_schema_version_is_at_least_83() -> None:
    assert SCHEMA_VERSION >= 83


def test_migration_v83_clears_sentinel_dates_and_leaves_real_dates(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "v83.db"))
    try:
        artist = db.upsert_library_artist({"name": "Test Artist"})
        # Insert rows directly to simulate legacy / pre-migration rows
        db.conn.execute(
            """
            INSERT INTO library_albums (
                id, artist_id, title, clean_title, sort_title, search_text, search_clean,
                year, release_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "alb-sentinel-1",
                artist["id"],
                "Sentinel Year 1",
                "sentinel year 1",
                "sentinel year 1",
                "sentinel year 1",
                "sentinel year 1",
                1,
                "0001-01-01T00:00:00Z",
            ),
        )
        db.conn.execute(
            """
            INSERT INTO library_albums (
                id, artist_id, title, clean_title, sort_title, search_text, search_clean,
                year, release_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "alb-sentinel-0",
                artist["id"],
                "Sentinel Year 0",
                "sentinel year 0",
                "sentinel year 0",
                "sentinel year 0",
                "sentinel year 0",
                0,
                "0000-00-00",
            ),
        )
        db.conn.execute(
            """
            INSERT INTO library_albums (
                id, artist_id, title, clean_title, sort_title, search_text, search_clean,
                year, release_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "alb-valid",
                artist["id"],
                "Valid Album",
                "valid album",
                "valid album",
                "valid album",
                "valid album",
                1971,
                "1971-05-18",
            ),
        )
        # Add tracks to verify library_tracks.album_date is also cleared
        db.conn.execute(
            """
            INSERT INTO library_tracks (
                id, album_id, artist_id, title, clean_title, sort_title, search_text, search_clean,
                album_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "trk-sentinel",
                "alb-sentinel-1",
                artist["id"],
                "Sentinel Track",
                "sentinel track",
                "sentinel track",
                "sentinel track",
                "sentinel track",
                "0001-01-01T00:00:00Z",
            ),
        )
        db.conn.execute(
            """
            INSERT INTO library_tracks (
                id, album_id, artist_id, title, clean_title, sort_title, search_text, search_clean,
                album_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "trk-valid",
                "alb-valid",
                artist["id"],
                "Valid Track",
                "valid track",
                "valid track",
                "valid track",
                "valid track",
                "1971-05-18",
            ),
        )
        db.conn.commit()

        # Run migration v83 (and verify idempotence)
        db._migration_v83(db.conn.cursor())
        db._migration_v83(db.conn.cursor())
        db.conn.commit()

        a1 = db.get_library_album("alb-sentinel-1")
        assert a1["year"] is None
        assert a1["release_date"] is None

        a0 = db.get_library_album("alb-sentinel-0")
        assert a0["year"] is None
        assert a0["release_date"] is None

        valid = db.get_library_album("alb-valid")
        assert valid["year"] == 1971
        assert valid["release_date"] == "1971-05-18"

        t_sentinel_date = db.conn.execute(
            "SELECT album_date FROM library_tracks WHERE id = ?", ("trk-sentinel",)
        ).fetchone()[0]
        assert t_sentinel_date is None

        t_valid_date = db.conn.execute(
            "SELECT album_date FROM library_tracks WHERE id = ?", ("trk-valid",)
        ).fetchone()[0]
        assert t_valid_date == "1971-05-18"
    finally:
        db.close()

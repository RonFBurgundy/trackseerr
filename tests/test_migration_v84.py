"""v84 migration: Add catalog tracking columns to music_requests."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def test_schema_version_is_at_least_84() -> None:
    assert SCHEMA_VERSION >= 84


def test_migration_v84_adds_catalog_columns_and_index(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "v84.db"))
    try:
        cur = db.conn.cursor()
        cur.execute("PRAGMA table_info(music_requests)")
        cols = {row[1] for row in cur.fetchall()}
        assert "catalog_track_id" in cols
        assert "catalog_album_id" in cols
        assert "catalog_seeded_at" in cols
        assert "catalog_seed_status" in cols

        cur.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_music_requests_status_catalog_seeded_at'"
        )
        assert cur.fetchone() is not None

        # Verify idempotence by running migration directly twice more
        db._migration_v84(db.conn.cursor())
        db._migration_v84(db.conn.cursor())
        db.conn.commit()

        cur.execute("PRAGMA table_info(music_requests)")
        cols_after = {row[1] for row in cur.fetchall()}
        assert "catalog_track_id" in cols_after
        assert "catalog_album_id" in cols_after
        assert "catalog_seeded_at" in cols_after
        assert "catalog_seed_status" in cols_after
    finally:
        db.close()

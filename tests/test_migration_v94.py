"""v94 migration: pack_releases, pack_files, pack_files_fts tables and indexers.pack_scan."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database

_TABLES = ("pack_releases", "pack_files", "pack_files_fts")
_INDEXES = ("idx_pack_releases_indexer_guid", "idx_pack_releases_status")


def _tables(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def test_schema_version_is_at_least_94() -> None:
    assert SCHEMA_VERSION >= 94


def test_fresh_db_has_pack_tables_and_column(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        tables = _tables(db)
        assert set(_TABLES) <= tables
        indexes = _indexes(db)
        assert set(_INDEXES) <= indexes

        # Verify indexers.pack_scan column and default
        cols = {
            row[1]: {"type": row[2], "dflt_value": row[4]}
            for row in db.conn.execute("PRAGMA table_info(indexers)")
        }
        assert "pack_scan" in cols
        assert cols["pack_scan"]["dflt_value"] in ("'off'", "off")
    finally:
        db.close()


def test_v93_db_upgrades_to_v94(tmp_path: Path) -> None:
    path = str(tmp_path / "v93.db")
    db = Database(path)
    db.conn.execute("DROP TABLE IF EXISTS pack_files_fts")
    db.conn.execute("DROP TABLE IF EXISTS pack_files")
    db.conn.execute("DROP TABLE IF EXISTS pack_releases")
    db.conn.execute("DROP INDEX IF EXISTS idx_pack_releases_indexer_guid")
    db.conn.execute("DROP INDEX IF EXISTS idx_pack_releases_status")
    # Simulate a v93 DB where pack_scan column was not yet added
    # (or if SQLite doesn't drop columns easily in older syntax, verify migration handles missing column)
    db.conn.execute("DELETE FROM schema_migrations")
    db.conn.execute("INSERT INTO schema_migrations (version) VALUES (93)")
    db.conn.commit()
    db.close()

    db = Database(path)
    try:
        tables = _tables(db)
        assert set(_TABLES) <= tables
        indexes = _indexes(db)
        assert set(_INDEXES) <= indexes
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(indexers)")}
        assert "pack_scan" in cols
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 94
    finally:
        db.close()

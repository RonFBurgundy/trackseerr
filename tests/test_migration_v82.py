"""v82 migration: playlists.sync_fingerprint."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def _playlist_columns(db: Database) -> set[str]:
    return {r[1] for r in db.conn.execute("PRAGMA table_info(playlists)")}


def test_schema_version_is_at_least_82() -> None:
    assert SCHEMA_VERSION >= 82


def test_fresh_db_has_sync_fingerprint(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert "sync_fingerprint" in _playlist_columns(db)
    finally:
        db.close()


def test_v81_database_upgrades_and_migration_is_idempotent(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "v81.db"))
    try:
        db.upsert_playlist("p1", "Mix", service="spotify")
        db.conn.execute("ALTER TABLE playlists DROP COLUMN sync_fingerprint")
        db.conn.commit()
        assert "sync_fingerprint" not in _playlist_columns(db)
        db._migration_v82(db.conn.cursor())
        db._migration_v82(db.conn.cursor())
        db.conn.commit()
        assert "sync_fingerprint" in _playlist_columns(db)
        assert db.get_playlist_sync_fingerprint("p1") == (None, None)
    finally:
        db.close()

"""v93 migration: exclusions table and indexes."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database

_TABLES = ("exclusions",)
_INDEXES = ("idx_exclusions_scope_key", "idx_exclusions_scope_mbid")


def _tables(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def test_schema_version_is_at_least_93() -> None:
    assert SCHEMA_VERSION >= 93


def test_fresh_db_has_exclusions(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert set(_TABLES) <= _tables(db)
        assert set(_INDEXES) <= _indexes(db)
    finally:
        db.close()


def test_v92_db_upgrades_to_v93(tmp_path: Path) -> None:
    path = str(tmp_path / "v92.db")
    db = Database(path)
    db.conn.execute("DROP TABLE exclusions")
    db.conn.execute("DELETE FROM schema_migrations")
    db.conn.execute("INSERT INTO schema_migrations (version) VALUES (92)")
    db.conn.commit()
    db.close()

    db = Database(path)
    try:
        assert set(_TABLES) <= _tables(db)
        assert set(_INDEXES) <= _indexes(db)
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 93
    finally:
        db.close()

"""v92 migration: health_checks + health_item_alerts."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database

_TABLES = ("health_checks", "health_item_alerts")


def _tables(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_schema_version_is_at_least_92() -> None:
    assert SCHEMA_VERSION >= 92


def test_fresh_db_has_health_tables(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert set(_TABLES) <= _tables(db)
    finally:
        db.close()


def test_v91_db_upgrades_to_v92(tmp_path: Path) -> None:
    path = str(tmp_path / "v91.db")
    db = Database(path)
    db.conn.execute("DROP TABLE health_checks")
    db.conn.execute("DROP TABLE health_item_alerts")
    db.conn.execute("DELETE FROM schema_migrations")
    db.conn.execute("INSERT INTO schema_migrations (version) VALUES (91)")
    db.conn.commit()
    db.close()

    db = Database(path)
    try:
        assert set(_TABLES) <= _tables(db)
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 92
    finally:
        db.close()

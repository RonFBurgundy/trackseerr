"""v102 migration: delay_windows_served table."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def test_schema_version_is_at_least_102() -> None:
    assert SCHEMA_VERSION >= 102


def test_fresh_db_has_delay_windows_served(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(delay_windows_served)")}
        assert {"item_key", "protocol", "served_at"}.issubset(cols)
    finally:
        db.close()


def test_v101_db_upgrades_to_v102(tmp_path: Path) -> None:
    path = str(tmp_path / "v101.db")
    db = Database(path)
    db.conn.execute("DROP TABLE delay_windows_served")
    db.conn.execute("DELETE FROM schema_migrations")
    db.conn.execute("INSERT INTO schema_migrations (version) VALUES (101)")
    db.conn.commit()
    db.close()

    db = Database(path)
    try:
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(delay_windows_served)")}
        assert {"item_key", "protocol", "served_at"}.issubset(cols)
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 102
    finally:
        db.close()

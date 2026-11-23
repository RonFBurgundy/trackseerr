"""v103 migration: monitored_by_hand column on library_albums."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def test_schema_version_is_at_least_103() -> None:
    assert SCHEMA_VERSION >= 103


def test_fresh_db_has_monitored_by_hand(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(library_albums)")}
        assert "monitored_by_hand" in cols
    finally:
        db.close()


def test_v102_db_upgrades_to_v103_idempotently(tmp_path: Path) -> None:
    path = str(tmp_path / "v102.db")
    db = Database(path)
    db.conn.execute("DELETE FROM schema_migrations")
    db.conn.execute("INSERT INTO schema_migrations (version) VALUES (102)")
    db.conn.commit()
    db.close()

    db = Database(path)
    try:
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(library_albums)")}
        assert "monitored_by_hand" in cols
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 103
    finally:
        db.close()

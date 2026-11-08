"""v95 migration: active_downloads.selected_files."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def test_schema_version_is_at_least_95() -> None:
    assert SCHEMA_VERSION >= 95


def test_fresh_db_has_selected_files_column(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        cols = {
            row[1]: {"type": row[2], "dflt_value": row[4]}
            for row in db.conn.execute("PRAGMA table_info(active_downloads)")
        }
        assert "selected_files" in cols
    finally:
        db.close()


def test_v94_db_upgrades_to_v95(tmp_path: Path) -> None:
    path = str(tmp_path / "v94.db")
    db = Database(path)
    # Simulate a v94 DB by dropping selected_files column or setting migration version to 94
    # SQLite 3.35.0+ supports ALTER TABLE DROP COLUMN
    try:
        db.conn.execute("ALTER TABLE active_downloads DROP COLUMN selected_files")
    except Exception:
        pass
    db.conn.execute("DELETE FROM schema_migrations")
    db.conn.execute("INSERT INTO schema_migrations (version) VALUES (94)")
    db.conn.commit()
    db.close()

    db = Database(path)
    try:
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(active_downloads)")}
        assert "selected_files" in cols
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 95
    finally:
        db.close()

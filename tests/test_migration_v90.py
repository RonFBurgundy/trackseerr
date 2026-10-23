"""v90 migration: active_downloads.hidden_from_queue."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def _cols(db: Database) -> dict[str, tuple]:
    return {row[1]: tuple(row) for row in db.conn.execute("PRAGMA table_info(active_downloads)")}


def test_schema_version_is_at_least_90() -> None:
    assert SCHEMA_VERSION >= 90


def test_fresh_db_has_hidden_from_queue(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        col = _cols(db)["hidden_from_queue"]
        assert col[3] == 1 and str(col[4]) == "0"  # NOT NULL DEFAULT 0
    finally:
        db.close()


def test_reopened_db_keeps_column_and_v90_is_idempotent(tmp_path: Path) -> None:
    path = str(tmp_path / "v90.db")
    Database(path).close()
    db = Database(path)
    try:
        cur = db.conn.cursor()
        db._migration_v90(cur)
        db._migration_v90(cur)
        db.conn.commit()
        assert "hidden_from_queue" in _cols(db)
    finally:
        db.close()

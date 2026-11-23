"""v104 migration: keep_compilations_together column on media_management_settings."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def test_schema_version_is_at_least_104() -> None:
    assert SCHEMA_VERSION >= 104


def test_fresh_db_has_keep_compilations_together(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(media_management_settings)")}
        assert "keep_compilations_together" in cols
        assert db.get_media_management_settings()["keep_compilations_together"] is False
    finally:
        db.close()


def test_v103_db_upgrades_to_v104_idempotently(tmp_path: Path) -> None:
    path = str(tmp_path / "v103.db")
    db = Database(path)
    db.conn.execute("DELETE FROM schema_migrations")
    db.conn.execute("INSERT INTO schema_migrations (version) VALUES (103)")
    db.conn.commit()
    db.close()

    db = Database(path)
    try:
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(media_management_settings)")}
        assert "keep_compilations_together" in cols
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 104
    finally:
        db.close()

"""v76 migration: media_management_settings.replace_illegal_characters."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def test_schema_version_is_at_least_76() -> None:
    assert SCHEMA_VERSION >= 76


def test_v76_adds_column_idempotently(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "v76.db"))
    try:
        conn = db.conn
        assert db.get_media_management_settings()["replace_illegal_characters"] is True
        # Simulate a pre-v76 table, then run the migration (twice: it must be idempotent).
        conn.execute("ALTER TABLE media_management_settings DROP COLUMN replace_illegal_characters")
        conn.commit()
        cur = conn.cursor()
        db._migration_v76(cur)
        db._migration_v76(cur)
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(media_management_settings)")}
        assert "replace_illegal_characters" in cols
        assert db.get_media_management_settings()["replace_illegal_characters"] is True
    finally:
        db.close()

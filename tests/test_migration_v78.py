"""v78 migration: media_management_settings.clean_artist_names is dropped."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def _cols(db: Database) -> set[str]:
    return {r[1] for r in db.conn.execute("PRAGMA table_info(media_management_settings)")}


def test_schema_version_is_at_least_78() -> None:
    assert SCHEMA_VERSION >= 78


def test_fresh_db_has_no_clean_artist_names(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert "clean_artist_names" not in _cols(db)
    finally:
        db.close()


def test_v78_drops_column_idempotently_and_preserves_settings(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "m.db"))
    try:
        db.update_media_management_settings({"root_folder_path": "/data/x", "artist_folder_format": "{Artist Name}"})
        db.conn.execute("ALTER TABLE media_management_settings ADD COLUMN clean_artist_names INTEGER NOT NULL DEFAULT 1")
        db.conn.commit()
        assert "clean_artist_names" in _cols(db)
        cur = db.conn.cursor()
        db._migration_v78(cur)
        db._migration_v78(cur)
        db.conn.commit()
        assert "clean_artist_names" not in _cols(db)
        settings = db.get_media_management_settings()
        assert settings["root_folder_path"] == "/data/x"
        assert settings["artist_folder_format"] == "{Artist Name}"
    finally:
        db.close()

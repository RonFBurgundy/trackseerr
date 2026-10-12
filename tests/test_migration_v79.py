"""v79 migration: media_management_settings.rename_artist_folders."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def test_schema_version_is_at_least_79() -> None:
    assert SCHEMA_VERSION >= 79


def test_v79_adds_column_idempotently_default_off(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "v79.db"))
    try:
        conn = db.conn
        assert db.get_media_management_settings()["rename_artist_folders"] is False
        conn.execute("ALTER TABLE media_management_settings DROP COLUMN rename_artist_folders")
        conn.commit()
        cur = conn.cursor()
        db._migration_v79(cur)
        db._migration_v79(cur)
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(media_management_settings)")}
        assert "rename_artist_folders" in cols
        assert db.get_media_management_settings()["rename_artist_folders"] is False
    finally:
        db.close()


def test_rename_artist_folders_round_trips(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "rt.db"))
    try:
        db.update_media_management_settings({"rename_artist_folders": True})
        assert db.get_media_management_settings()["rename_artist_folders"] is True
        db.update_media_management_settings({"rename_artist_folders": False})
        assert db.get_media_management_settings()["rename_artist_folders"] is False
    finally:
        db.close()

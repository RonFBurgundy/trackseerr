"""v105 migration: compilation_album_artist setting and library_albums.compilation_artist."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def test_schema_version_is_at_least_105() -> None:
    assert SCHEMA_VERSION >= 105


def test_fresh_db_has_v105_columns(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        mm_cols = {row[1] for row in db.conn.execute("PRAGMA table_info(media_management_settings)")}
        album_cols = {row[1] for row in db.conn.execute("PRAGMA table_info(library_albums)")}
        assert "compilation_album_artist" in mm_cols
        assert "compilation_artist" in album_cols
        assert db.get_media_management_settings()["compilation_album_artist"] == "various"
    finally:
        db.close()


def test_v104_db_upgrades_to_v105_idempotently(tmp_path: Path) -> None:
    path = str(tmp_path / "v104.db")
    db = Database(path)
    db.conn.execute("DELETE FROM schema_migrations")
    db.conn.execute("INSERT INTO schema_migrations (version) VALUES (104)")
    db.conn.commit()
    db.close()

    db = Database(path)
    try:
        mm_cols = {row[1] for row in db.conn.execute("PRAGMA table_info(media_management_settings)")}
        album_cols = {row[1] for row in db.conn.execute("PRAGMA table_info(library_albums)")}
        assert "compilation_album_artist" in mm_cols
        assert "compilation_artist" in album_cols
        assert db.get_media_management_settings()["compilation_album_artist"] == "various"
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 105
    finally:
        db.close()

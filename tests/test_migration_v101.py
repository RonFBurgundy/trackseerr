"""v101 migration: media_server_settings.plex_account_token and plex_account_username."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def test_schema_version_is_at_least_101() -> None:
    assert SCHEMA_VERSION >= 101


def test_fresh_db_has_plex_account_columns(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        cols = {row[1]: row[4] for row in db.conn.execute("PRAGMA table_info(media_server_settings)")}
        assert "plex_account_token" in cols
        assert "plex_account_username" in cols
        assert db.get_plex_account() == {"token": "", "username": ""}
    finally:
        db.close()


def test_v100_db_upgrades_to_v101(tmp_path: Path) -> None:
    path = str(tmp_path / "v100.db")
    db = Database(path)
    db.conn.execute("ALTER TABLE media_server_settings DROP COLUMN plex_account_token")
    db.conn.execute("ALTER TABLE media_server_settings DROP COLUMN plex_account_username")
    db.conn.execute("DELETE FROM schema_migrations")
    db.conn.execute("INSERT INTO schema_migrations (version) VALUES (100)")
    db.conn.commit()
    db.close()

    db = Database(path)
    try:
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(media_server_settings)")}
        assert "plex_account_token" in cols
        assert "plex_account_username" in cols
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 101
        assert db.get_plex_account() == {"token": "", "username": ""}
    finally:
        db.close()

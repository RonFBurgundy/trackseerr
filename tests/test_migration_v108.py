"""v108 migration: blank legacy media_server_settings credentials while keeping the Plex account token."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def test_schema_version_is_at_least_108() -> None:
    assert SCHEMA_VERSION >= 108


def test_v108_blanks_old_settings_row(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "blank.db"))
    try:
        db.conn.execute(
            "INSERT INTO media_server_settings (id, type, url, username, password, api_key, credentials_type) "
            "VALUES (1, 'subsonic', 'http://navidrome:4533', 'u', 'p', 'k', 'subsonic') "
            "ON CONFLICT(id) DO UPDATE SET type = excluded.type, url = excluded.url, username = excluded.username, "
            "password = excluded.password, api_key = excluded.api_key, credentials_type = excluded.credentials_type"
        )
        db.conn.commit()

        cur = db.conn.cursor()
        db._migration_v108(cur)
        db.conn.commit()

        row = db.conn.execute(
            "SELECT type, url, username, password, api_key, credentials_type FROM media_server_settings WHERE id = 1"
        ).fetchone()
        assert tuple(row) == ("", "", "", "", "", "")
    finally:
        db.close()


def test_v108_keeps_plex_account_token(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "plex_token.db"))
    try:
        db.conn.execute(
            "INSERT INTO media_server_settings (id, type, url, username, password, api_key, credentials_type) "
            "VALUES (1, 'jellyfin', 'http://jellyfin:8096', 'bob', '', 'jf-key', 'jellyfin') "
            "ON CONFLICT(id) DO UPDATE SET type = excluded.type, url = excluded.url, username = excluded.username, "
            "password = excluded.password, api_key = excluded.api_key, credentials_type = excluded.credentials_type"
        )
        db.conn.commit()
        db.set_plex_account("acct-tok-123", "plex_owner")

        cur = db.conn.cursor()
        db._migration_v108(cur)
        db.conn.commit()

        assert db.get_plex_account() == {"token": "acct-tok-123", "username": "plex_owner"}
    finally:
        db.close()

"""v107 migration: media_servers table + copy saved Subsonic/Jellyfin server."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def _tables(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _insert_legacy_settings(db: Database, settings: dict[str, str]) -> None:
    keys = ("type", "url", "username", "password", "api_key", "credentials_type")
    values = [str(settings.get(k) or "") for k in keys]
    db.conn.execute(
        "INSERT INTO media_server_settings (id, type, url, username, password, api_key, credentials_type) "
        "VALUES (1, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET type = excluded.type, url = excluded.url, username = excluded.username, "
        "password = excluded.password, api_key = excluded.api_key, credentials_type = excluded.credentials_type",
        values,
    )
    db.conn.commit()


def test_schema_version_is_at_least_107() -> None:
    assert SCHEMA_VERSION >= 107


def test_fresh_db_has_media_servers_table(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert "media_servers" in _tables(db)
        cur = db.conn.cursor()
        cur.execute("PRAGMA table_info(media_servers)")
        cols = {row[1] for row in cur.fetchall()}
        assert cols == {
            "id",
            "kind",
            "source",
            "name",
            "url",
            "username",
            "password",
            "api_key",
            "credentials_type",
            "music_section",
            "verify_ssl",
            "enabled",
            "use_refresh",
            "use_playlists",
            "use_users",
            "use_scrobbles",
            "is_login",
            "path_mapping_json",
            "sort_order",
            "created_at",
            "updated_at",
        }
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_media_servers_login'"
        )
        assert cur.fetchone() is not None
    finally:
        db.close()


def test_v107_copies_saved_subsonic_server(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "copy.db"))
    try:
        _insert_legacy_settings(
            db,
            {
                "type": "subsonic",
                "url": "http://navidrome:4533",
                "username": "u",
                "password": "p",
            },
        )
        cur = db.conn.cursor()
        cur.execute("DELETE FROM media_servers")
        db._migration_v107(cur)
        db.conn.commit()

        rows = db.list_media_server_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "subsonic"
        assert row["name"] == "Subsonic"
        assert row["url"] == "http://navidrome:4533"
        assert row["username"] == "u"
        assert row["password"] == "p"
    finally:
        db.close()


def test_v107_skips_plex_or_empty_settings(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "skip.db"))
    try:
        cur = db.conn.cursor()
        for settings in (
            {"type": "", "url": ""},
            {"type": "plex", "url": "http://plex:32400", "api_key": "tok"},
        ):
            _insert_legacy_settings(db, settings)
            cur.execute("DELETE FROM media_servers")
            db._migration_v107(cur)
            db.conn.commit()
            assert db.list_media_server_rows() == []
    finally:
        db.close()


def test_v107_is_idempotent(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "idempotent.db"))
    try:
        _insert_legacy_settings(
            db,
            {
                "type": "jellyfin",
                "url": "http://jellyfin:8096",
                "api_key": "jf-key",
            },
        )
        cur = db.conn.cursor()
        cur.execute("DELETE FROM media_servers")
        db._migration_v107(cur)
        db._migration_v107(cur)
        db.conn.commit()

        rows = db.list_media_server_rows()
        assert len(rows) == 1
        assert rows[0]["kind"] == "jellyfin"
        assert rows[0]["name"] == "Jellyfin"
        assert rows[0]["api_key"] == "jf-key"
    finally:
        db.close()

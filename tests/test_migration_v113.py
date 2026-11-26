"""v113 migration: server_user_links and server_playlist_ids tables."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def _tables(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_schema_version_is_at_least_113() -> None:
    assert SCHEMA_VERSION >= 113


def test_fresh_db_has_v113_tables(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        tables = _tables(db)
        assert "server_user_links" in tables
        assert "server_playlist_ids" in tables
        link_cols = {row[1] for row in db.conn.execute("PRAGMA table_info(server_user_links)")}
        assert link_cols == {"user_id", "server_id", "server_user_id", "server_username", "source"}
        pl_cols = {row[1] for row in db.conn.execute("PRAGMA table_info(server_playlist_ids)")}
        assert pl_cols == {"playlist_id", "server_id", "user_id", "server_playlist_id"}
    finally:
        db.close()


def test_migrated_db_is_idempotent(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "migrate.db"))
    try:
        cur = db.conn.cursor()
        cur.execute("DROP TABLE server_user_links")
        cur.execute("DROP TABLE server_playlist_ids")
        db.conn.commit()
        assert "server_user_links" not in _tables(db)
        assert "server_playlist_ids" not in _tables(db)

        db._migration_v113(cur)
        db._migration_v113(cur)
        db.conn.commit()

        tables = _tables(db)
        assert "server_user_links" in tables
        assert "server_playlist_ids" in tables
    finally:
        db.close()


def test_deleting_user_or_server_cascades_links_and_playlist_ids(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "cascade.db"))
    try:
        db.upsert_user("u1", "alice", "a@x")
        db.upsert_user("u2", "bob", "b@x")
        srv = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})

        db.link_server_user("u1", srv["id"], "nd-1", "alice", source="imported")
        db.link_server_user("u2", srv["id"], "nd-2", "bob", source="manual")
        db.set_server_playlist_id("pl-1", srv["id"], "u1", "srv-pl-1")
        db.set_server_playlist_id("pl-1", srv["id"], "u2", "srv-pl-2")

        db.delete_user("u1")
        assert db.get_server_user_link("u1", srv["id"]) is None
        assert db.get_server_playlist_id("pl-1", srv["id"], "u1") is None
        assert db.get_server_user_link("u2", srv["id"]) is not None
        assert db.get_server_playlist_id("pl-1", srv["id"], "u2") == "srv-pl-2"

        assert db.delete_media_server_row(srv["id"]) is True
        assert db.get_server_user_link("u2", srv["id"]) is None
        assert db.get_server_playlist_id("pl-1", srv["id"], "u2") is None
    finally:
        db.close()

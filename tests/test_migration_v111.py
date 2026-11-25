"""v111 migration: server_playlist_sources table."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def _tables(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_schema_version_is_at_least_111() -> None:
    assert SCHEMA_VERSION >= 111


def test_fresh_db_has_server_playlist_sources_table(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert "server_playlist_sources" in _tables(db)
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(server_playlist_sources)")}
        assert cols == {"playlist_id", "server_id", "server_playlist_id", "server_user"}
    finally:
        db.close()


def test_migrated_db_is_idempotent(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "migrate.db"))
    try:
        cur = db.conn.cursor()
        cur.execute("DROP TABLE server_playlist_sources")
        db.conn.commit()
        assert "server_playlist_sources" not in _tables(db)

        db._migration_v111(cur)
        db._migration_v111(cur)
        db.conn.commit()

        assert "server_playlist_sources" in _tables(db)
    finally:
        db.close()


def test_deleting_playlist_cascades(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "cascade_playlist.db"))
    try:
        db.upsert_playlist("sub_srv1_p1", name="Adopted", service="subsonic")
        srv = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})
        db.set_server_playlist_source("sub_srv1_p1", srv["id"], "p1", "alice")

        assert db.get_server_playlist_source("sub_srv1_p1") == {
            "playlist_id": "sub_srv1_p1",
            "server_id": srv["id"],
            "server_playlist_id": "p1",
            "server_user": "alice",
        }
        db.delete_playlist("sub_srv1_p1")
        assert db.get_server_playlist_source("sub_srv1_p1") is None
        assert db.list_server_playlist_sources(srv["id"]) == []
    finally:
        db.close()


def test_deleting_media_server_cascades(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "cascade_server.db"))
    try:
        db.upsert_playlist("jf_srv1_p1", name="Adopted", service="jellyfin")
        srv = db.insert_media_server_row({"kind": "jellyfin", "name": "Jellyfin", "url": "http://jf:8096"})
        db.set_server_playlist_source("jf_srv1_p1", srv["id"], "p1", "bob")

        assert db.delete_media_server_row(srv["id"]) is True
        assert db.get_server_playlist_source("jf_srv1_p1") is None
        assert db.list_server_playlist_sources(srv["id"]) == []
    finally:
        db.close()

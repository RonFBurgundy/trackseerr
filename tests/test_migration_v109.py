"""v109 migration: playlist_server_excludes and playlist_server_state tables."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def _tables(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_schema_version_is_at_least_109() -> None:
    assert SCHEMA_VERSION >= 109


def test_fresh_db_has_playlist_server_tables(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        tables = _tables(db)
        assert "playlist_server_excludes" in tables
        assert "playlist_server_state" in tables

        excludes_cols = {row[1] for row in db.conn.execute("PRAGMA table_info(playlist_server_excludes)")}
        assert excludes_cols == {"playlist_id", "server_id"}

        state_cols = {row[1] for row in db.conn.execute("PRAGMA table_info(playlist_server_state)")}
        assert state_cols == {
            "playlist_id",
            "server_id",
            "status",
            "matched",
            "missing",
            "error",
            "synced_at",
        }
    finally:
        db.close()


def test_migrated_db_is_idempotent(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "migrate.db"))
    try:
        cur = db.conn.cursor()
        cur.execute("DROP TABLE playlist_server_excludes")
        cur.execute("DROP TABLE playlist_server_state")
        db.conn.commit()
        assert "playlist_server_excludes" not in _tables(db)
        assert "playlist_server_state" not in _tables(db)

        db._migration_v109(cur)
        db._migration_v109(cur)
        db.conn.commit()

        tables = _tables(db)
        assert "playlist_server_excludes" in tables
        assert "playlist_server_state" in tables
    finally:
        db.close()


def test_deleting_playlist_cascades(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "cascade_playlist.db"))
    try:
        db.upsert_playlist("pl_1", "Playlist 1", service="spotify")
        srv = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})
        db.set_playlist_server_excludes("pl_1", [srv["id"]])
        db.record_playlist_server_state("pl_1", srv["id"], "success", matched=3, missing=1)

        assert db.delete_playlist("pl_1") is True
        assert db.get_playlist_server_excludes("pl_1") == set()
        assert db.list_playlist_server_state("pl_1") == []
    finally:
        db.close()


def test_deleting_media_server_cascades(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "cascade_server.db"))
    try:
        db.upsert_playlist("pl_1", "Playlist 1", service="spotify")
        srv = db.insert_media_server_row({"kind": "jellyfin", "name": "Jellyfin", "url": "http://jf:8096"})
        db.set_playlist_server_excludes("pl_1", [srv["id"]])
        db.record_playlist_server_state("pl_1", srv["id"], "failed", error="unreachable")

        assert db.delete_media_server_row(srv["id"]) is True
        assert db.get_playlist_server_excludes("pl_1") == set()
        assert db.list_playlist_server_state("pl_1") == []
    finally:
        db.close()

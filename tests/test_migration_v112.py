"""v112 migration: server_match_overrides table."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def _tables(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_schema_version_is_at_least_112() -> None:
    assert SCHEMA_VERSION >= 112


def test_fresh_db_has_server_match_overrides_table(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert "server_match_overrides" in _tables(db)
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(server_match_overrides)")}
        assert cols == {
            "id",
            "server_id",
            "source_title",
            "source_artist",
            "item_id",
            "item_title",
            "item_artist",
            "created_by",
            "created_at",
        }
    finally:
        db.close()


def test_migrated_db_is_idempotent(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "migrate.db"))
    try:
        cur = db.conn.cursor()
        cur.execute("DROP TABLE server_match_overrides")
        db.conn.commit()
        assert "server_match_overrides" not in _tables(db)

        db._migration_v112(cur)
        db._migration_v112(cur)
        db.conn.commit()

        assert "server_match_overrides" in _tables(db)
    finally:
        db.close()


def test_deleting_media_server_cascades_overrides(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "cascade.db"))
    try:
        srv = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})
        db.add_server_match_override(
            server_id=srv["id"],
            source_title="Song A",
            source_artist="Artist 1",
            item_id="nav-1",
            item_title="Song A",
            item_artist="Artist 1",
        )
        assert db.get_match_override("Song A", "Artist 1", server_id=srv["id"], use_legacy=False) is not None

        assert db.delete_media_server_row(srv["id"]) is True
        assert db.get_match_override("Song A", "Artist 1", server_id=srv["id"], use_legacy=False) is None
    finally:
        db.close()

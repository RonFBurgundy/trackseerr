"""v110 migration: user_server_accounts table."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def _tables(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_schema_version_is_at_least_110() -> None:
    assert SCHEMA_VERSION >= 110


def test_fresh_db_has_user_server_accounts_table(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert "user_server_accounts" in _tables(db)
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(user_server_accounts)")}
        assert cols == {"user_id", "server_id", "username", "secret_sealed", "updated_at"}
    finally:
        db.close()


def test_migrated_db_is_idempotent(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "migrate.db"))
    try:
        cur = db.conn.cursor()
        cur.execute("DROP TABLE user_server_accounts")
        db.conn.commit()
        assert "user_server_accounts" not in _tables(db)

        db._migration_v110(cur)
        db._migration_v110(cur)
        db.conn.commit()

        assert "user_server_accounts" in _tables(db)
    finally:
        db.close()


def test_deleting_user_cascades(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "cascade_user.db"))
    try:
        db.upsert_user("u1", "alice", "a@x")
        srv = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})
        db.set_server_account("u1", srv["id"], "alice", "sealed-secret")

        db.delete_user("u1")
        assert db.get_server_account("u1", srv["id"]) is None
    finally:
        db.close()


def test_deleting_media_server_cascades(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "cascade_server.db"))
    try:
        db.upsert_user("u1", "alice", "a@x")
        srv = db.insert_media_server_row({"kind": "subsonic", "name": "Navidrome", "url": "http://nav:4533"})
        db.set_server_account("u1", srv["id"], "alice", "sealed-secret")

        assert db.delete_media_server_row(srv["id"]) is True
        assert db.get_server_account("u1", srv["id"]) is None
        assert db.list_server_accounts_for_user("u1") == []
    finally:
        db.close()

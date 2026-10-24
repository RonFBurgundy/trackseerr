"""v91 migration: slskd_challenges + slskd_gated_peers."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database

_TABLES = ("slskd_challenges", "slskd_gated_peers")


def _tables(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_schema_version_is_at_least_91() -> None:
    assert SCHEMA_VERSION >= 91


def test_fresh_db_has_challenge_tables(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert set(_TABLES) <= _tables(db)
    finally:
        db.close()


def test_v90_db_upgrades_to_v91(tmp_path: Path) -> None:
    path = str(tmp_path / "v90.db")
    db = Database(path)
    db.conn.execute("DROP TABLE slskd_challenges")
    db.conn.execute("DROP TABLE slskd_gated_peers")
    db.conn.execute("DELETE FROM schema_migrations")
    db.conn.execute("INSERT INTO schema_migrations (version) VALUES (90)")
    db.conn.commit()
    db.close()
    db = Database(path)
    try:
        assert set(_TABLES) <= _tables(db)
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 91
    finally:
        db.close()

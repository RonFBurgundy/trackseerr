"""v98 migration: general_settings.default_user_permissions."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def test_schema_version_is_at_least_98() -> None:
    assert SCHEMA_VERSION >= 98


def test_fresh_db_has_default_user_permissions_column(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        cols = {row[1]: row[4] for row in db.conn.execute("PRAGMA table_info(general_settings)")}
        assert "default_user_permissions" in cols
        assert str(cols["default_user_permissions"]) == "34"
        assert db.get_account_settings()["default_user_permissions"] == 34
    finally:
        db.close()


def test_v97_db_upgrades_to_v98(tmp_path: Path) -> None:
    path = str(tmp_path / "v97.db")
    db = Database(path)
    db.conn.execute("ALTER TABLE general_settings DROP COLUMN default_user_permissions")
    db.conn.execute("DELETE FROM schema_migrations")
    db.conn.execute("INSERT INTO schema_migrations (version) VALUES (97)")
    db.conn.commit()
    db.close()

    db = Database(path)
    try:
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(general_settings)")}
        assert "default_user_permissions" in cols
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 98
        assert db.default_user_permissions() == 34
    finally:
        db.close()

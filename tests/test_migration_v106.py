"""v106 migration: clear cached library_albums.compilation_artist."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def test_schema_version_is_at_least_106() -> None:
    assert SCHEMA_VERSION >= 106


def test_v105_db_upgrades_and_clears_compilation_artist(tmp_path: Path) -> None:
    path = str(tmp_path / "v105.db")
    db = Database(path)
    db.conn.execute("PRAGMA foreign_keys = OFF")
    db.conn.execute(
        "INSERT INTO library_albums (artist_id, title, clean_title, mb_release_group_id, compilation_artist) "
        "VALUES (1, 'Some Album', 'some album', 'rg-1', 'X')"
    )
    db.conn.execute("DELETE FROM schema_migrations")
    db.conn.execute("INSERT INTO schema_migrations (version) VALUES (105)")
    db.conn.commit()
    db.close()

    db = Database(path)
    try:
        row = db.conn.execute(
            "SELECT compilation_artist FROM library_albums WHERE mb_release_group_id = 'rg-1'"
        ).fetchone()
        assert row is not None
        assert row[0] is None
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 106
    finally:
        db.close()

    # Running migrate again is a no-op
    db = Database(path)
    try:
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 106
    finally:
        db.close()

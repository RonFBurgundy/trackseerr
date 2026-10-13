"""v80 migration: file_fingerprints table."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def _tables(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_schema_version_is_at_least_80() -> None:
    assert SCHEMA_VERSION >= 80


def test_fresh_db_has_file_fingerprints(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert "file_fingerprints" in _tables(db)
    finally:
        db.close()


def test_v80_is_idempotent(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "v80.db"))
    try:
        db.conn.execute("DROP TABLE file_fingerprints")
        db.conn.commit()
        cur = db.conn.cursor()
        db._migration_v80(cur)
        db._migration_v80(cur)
        db.conn.commit()
        assert "file_fingerprints" in _tables(db)
    finally:
        db.close()


def test_deleting_library_file_cascades_to_fingerprint(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "cascade.db"))
    try:
        db.upsert_library_artist({"id": "a1", "name": "A", "monitored": True})
        db.upsert_library_album({"id": "al1", "artist_id": "a1", "title": "Al", "year": 2000, "monitored": True})
        db.upsert_library_track({"id": "t1", "album_id": "al1", "artist_id": "a1", "title": "T", "track_number": 1})
        db.upsert_library_file({"id": "f1", "track_id": "t1", "file_path": "/x/a.flac", "relative_path": "a.flac"})
        db.upsert_file_fingerprint(
            {"file_id": "f1", "file_path": "/x/a.flac", "size_bytes": 1, "mtime_ns": 2, "status": "ok", "matches": [{"recording_id": "r"}]}
        )
        assert db.get_file_fingerprint("f1")["matches"] == [{"recording_id": "r"}]
        db.conn.execute("DELETE FROM library_files WHERE id = 'f1'")
        db.conn.commit()
        assert db.get_file_fingerprint("f1") is None
        assert db.get_file_fingerprints(["f1"]) == {}
    finally:
        db.close()

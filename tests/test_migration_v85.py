"""v85 migration: AcoustID user key setting + submissions table."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def _tables(db: Database) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_schema_version_is_at_least_85() -> None:
    assert SCHEMA_VERSION >= 85


def test_fresh_db_has_v85_schema(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert "acoustid_submissions" in _tables(db)
        cur = db.conn.cursor()
        cur.execute("PRAGMA table_info(media_management_settings)")
        cols = {row[1] for row in cur.fetchall()}
        assert "acoustid_user_key" in cols

        cur.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_acoustid_submissions_status'"
        )
        assert cur.fetchone() is not None
    finally:
        db.close()


def test_v85_is_idempotent(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "v85.db"))
    try:
        cur = db.conn.cursor()
        db._migration_v85(cur)
        db._migration_v85(cur)
        db.conn.commit()

        cur.execute("PRAGMA table_info(media_management_settings)")
        cols = {row[1] for row in cur.fetchall()}
        assert "acoustid_user_key" in cols
        assert "acoustid_submissions" in _tables(db)
    finally:
        db.close()


def test_deleting_library_file_cascades_to_submission(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "cascade.db"))
    try:
        db.upsert_library_artist({"id": "a1", "name": "A", "monitored": True})
        db.upsert_library_album({"id": "al1", "artist_id": "a1", "title": "Al", "year": 2000, "monitored": True})
        db.upsert_library_track({"id": "t1", "album_id": "al1", "artist_id": "a1", "title": "T", "track_number": 1})
        db.upsert_library_file({"id": "f1", "track_id": "t1", "file_path": "/x/a.flac", "relative_path": "a.flac"})

        db.upsert_acoustid_submission(
            {
                "file_id": "f1",
                "recording_mbid": "rec-mbid-1",
                "fingerprint_hash": "hash123",
                "submission_id": 42,
                "status": "pending",
                "reason": None,
                "submitted_at": "2026-10-15T00:00:00Z",
                "checked_at": None,
            }
        )
        sub = db.get_acoustid_submission("f1")
        assert sub is not None
        assert sub["file_id"] == "f1"
        assert sub["recording_mbid"] == "rec-mbid-1"
        assert sub["submission_id"] == 42
        assert sub["status"] == "pending"

        db.conn.execute("DELETE FROM library_files WHERE id = 'f1'")
        db.conn.commit()
        assert db.get_acoustid_submission("f1") is None
    finally:
        db.close()


def test_acoustid_submissions_store_operations(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "submissions.db"))
    try:
        db.upsert_library_artist({"id": "a1", "name": "A", "monitored": True})
        db.upsert_library_album({"id": "al1", "artist_id": "a1", "title": "Al", "year": 2000, "monitored": True})
        db.upsert_library_track({"id": "t1", "album_id": "al1", "artist_id": "a1", "title": "T", "track_number": 1})
        db.upsert_library_track({"id": "t2", "album_id": "al1", "artist_id": "a1", "title": "T2", "track_number": 2})
        db.upsert_library_file({"id": "f1", "track_id": "t1", "file_path": "/x/1.flac", "relative_path": "1.flac"})
        db.upsert_library_file({"id": "f2", "track_id": "t2", "file_path": "/x/2.flac", "relative_path": "2.flac"})

        assert db.get_acoustid_submission("f1") is None
        assert db.count_acoustid_submissions() == {}
        assert db.list_acoustid_submissions("pending") == []

        # Upsert f1
        db.upsert_acoustid_submission(
            {
                "file_id": "f1",
                "recording_mbid": "mbid-1",
                "fingerprint_hash": "hash-1",
                "submission_id": 100,
                "status": "pending",
                "reason": None,
                "submitted_at": "2026-10-15T01:00:00Z",
                "checked_at": None,
            }
        )
        # Upsert f2
        db.upsert_acoustid_submission(
            {
                "file_id": "f2",
                "recording_mbid": "mbid-2",
                "fingerprint_hash": "hash-2",
                "submission_id": 101,
                "status": "imported",
                "reason": "already imported",
                "submitted_at": "2026-10-15T02:00:00Z",
                "checked_at": "2026-10-15T02:05:00Z",
            }
        )

        counts = db.count_acoustid_submissions()
        assert counts == {"pending": 1, "imported": 1}

        pending = db.list_acoustid_submissions("pending")
        assert len(pending) == 1
        assert pending[0]["file_id"] == "f1"
        assert pending[0]["recording_mbid"] == "mbid-1"
        assert pending[0]["submission_id"] == 100

        imported = db.list_acoustid_submissions("imported")
        assert len(imported) == 1
        assert imported[0]["file_id"] == "f2"
        assert imported[0]["reason"] == "already imported"

        # Update f1 status on conflict
        db.upsert_acoustid_submission(
            {
                "file_id": "f1",
                "recording_mbid": "mbid-1",
                "fingerprint_hash": "hash-1",
                "submission_id": 100,
                "status": "imported",
                "reason": "success",
                "submitted_at": "2026-10-15T01:00:00Z",
                "checked_at": "2026-10-15T03:00:00Z",
            }
        )
        sub1 = db.get_acoustid_submission("f1")
        assert sub1 is not None
        assert sub1["status"] == "imported"
        assert sub1["reason"] == "success"
        assert sub1["checked_at"] == "2026-10-15T03:00:00Z"

        assert db.count_acoustid_submissions() == {"imported": 2}
    finally:
        db.close()

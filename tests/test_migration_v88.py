"""v88 migration: library_health_findings accepts kind 'long_name'."""

from pathlib import Path

from trackseerr.storage import SCHEMA_VERSION, Database


def test_schema_version_is_at_least_88() -> None:
    assert SCHEMA_VERSION >= 88


def test_v88_rebuilds_old_check_and_keeps_rows(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "v88.db"))
    try:
        conn = db.conn
        # Recreate library_health_findings with the OLD CHECK constraint (without 'long_name')
        conn.execute("DROP TABLE library_health_findings")
        conn.execute("""
            CREATE TABLE library_health_findings (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('server_unindexed', 'server_stale', 'weak_match', 'orphan_torrent', 'cleanup_failed')),
                server_kind TEXT,
                cause TEXT NOT NULL,
                group_key TEXT NOT NULL,
                path TEXT NOT NULL,
                detail_json TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                dismissed INTEGER NOT NULL DEFAULT 0,
                UNIQUE (kind, path)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lib_health_findings_group ON library_health_findings(group_key)")

        # Insert one server_stale row
        old_row = (
            "finding-1",
            "server_stale",
            "plex",
            "metadata_missing",
            "group-1",
            "/music/stale.mp3",
            '{"reason": "stale"}',
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            0,
        )
        conn.execute(
            """
            INSERT INTO library_health_findings
                (id, kind, server_kind, cause, group_key, path, detail_json, first_seen, last_seen, dismissed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            old_row,
        )
        conn.commit()

        # Run migration twice (idempotency check)
        cur = conn.cursor()
        db._migration_v88(cur)
        db._migration_v88(cur)
        conn.commit()

        # The old row survives with every column intact
        cur.execute(
            "SELECT id, kind, server_kind, cause, group_key, path, detail_json, first_seen, last_seen, dismissed "
            "FROM library_health_findings WHERE id = 'finding-1'"
        )
        fetched = cur.fetchone()
        assert tuple(fetched) == old_row

        # The index exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_lib_health_findings_group'")
        assert cur.fetchone() is not None

        # And a long_name row upserts through db.upsert_library_health_findings without error
        upsert_res = db.upsert_library_health_findings(
            [
                {
                    "kind": "long_name",
                    "server_kind": None,
                    "cause": "path_length_exceeded",
                    "group_key": "long_names",
                    "path": "/music/" + "a" * 250 + ".mp3",
                    "detail": {"length": 256},
                }
            ],
            seen_at="2026-01-02T00:00:00Z",
        )
        assert upsert_res == 1

        cur.execute("SELECT kind, cause, path FROM library_health_findings WHERE kind = 'long_name'")
        res = cur.fetchone()
        assert res is not None
        assert res[0] == "long_name"
    finally:
        db.close()


def test_v88_noop_on_fresh_db(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        cur = db.conn.cursor()
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='library_health_findings'")
        row = cur.fetchone()
        assert row is not None
        assert "'long_name'" in row[0]

        # Insert a row and ensure migration leaves row count unchanged
        db.upsert_library_health_findings(
            [
                {
                    "kind": "server_unindexed",
                    "cause": "unindexed",
                    "group_key": "grp",
                    "path": "/music/track.mp3",
                }
            ],
            seen_at="2026-01-01T00:00:00Z",
        )
        cur.execute("SELECT COUNT(*) FROM library_health_findings")
        count_before = cur.fetchone()[0]
        assert count_before == 1

        db._migration_v88(cur)
        db.conn.commit()

        cur.execute("SELECT COUNT(*) FROM library_health_findings")
        count_after = cur.fetchone()[0]
        assert count_after == count_before
    finally:
        db.close()

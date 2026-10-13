"""v81 migration: has_file + denormalized sort columns, composite indexes, trigram table and sync triggers."""

from pathlib import Path

import pytest

from trackseerr import library_track_index as lti
from trackseerr.storage import SCHEMA_VERSION, Database


def _objects(db: Database, kind: str) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type = ?", (kind,))}


def _columns(db: Database) -> set[str]:
    return {r[1] for r in db.conn.execute("PRAGMA table_info(library_tracks)")}


def _seed(db: Database) -> None:
    db.upsert_library_artist({"id": "a1", "name": "The Band", "monitored": True})
    db.upsert_library_album(
        {"id": "al1", "artist_id": "a1", "title": "Éclat", "year": 2000, "release_date": "2000-05-01"}
    )
    for i in range(5):
        db.upsert_library_track({"id": f"t{i}", "album_id": "al1", "artist_id": "a1", "title": f"Song {i}"})
    db.upsert_library_file(
        {"id": "f1", "track_id": "t2", "file_path": "/m/a.flac", "relative_path": "a.flac", "codec": "FLAC",
         "quality_name": "FLAC", "size_bytes": 4242}
    )


def _downgrade(db: Database) -> None:
    """Puts the database back to its v80 shape: no triggers, indexes, FTS table or new columns, stale state."""
    cur = db.conn.cursor()
    lti.drop_track_triggers(cur)
    for sql in lti.INDEX_DDL:
        cur.execute("DROP INDEX IF EXISTS " + sql.split(" ON ")[0].split()[-1])
    cur.execute(f"DROP TABLE IF EXISTS {lti.FTS_TABLE}")
    for name, _decl in lti.TRACK_COLUMNS:
        cur.execute(f"ALTER TABLE library_tracks DROP COLUMN {name}")
    db.conn.commit()


def test_schema_version_is_at_least_81() -> None:
    assert SCHEMA_VERSION >= 81


def test_fresh_db_has_columns_indexes_fts_and_triggers(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert {name for name, _d in lti.TRACK_COLUMNS} <= _columns(db)
        assert {sql.split(" ON ")[0].split()[-1] for sql in lti.INDEX_DDL} <= _objects(db, "index")
        assert lti.FTS_TABLE in _objects(db, "table")
        assert set(lti.TRIGGERS) <= _objects(db, "trigger")
    finally:
        db.close()


def test_v81_backfills_a_v80_shaped_database(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "v80.db"))
    try:
        _seed(db)
        _downgrade(db)
        assert "has_file" not in _columns(db)
        db._migration_v81(db.conn.cursor())
        db.conn.commit()
        rows = {r["id"]: r for r in db.conn.execute("SELECT * FROM library_tracks")}
        assert [i for i, r in rows.items() if r["has_file"]] == ["t2"]
        assert rows["t2"]["max_size_bytes"] == 4242 and rows["t0"]["max_size_bytes"] is None
        assert rows["t0"]["artist_sort"] == "BAND" and rows["t0"]["album_sort"] == "ECLAT"
        assert rows["t0"]["album_date"] == "2000-05-01"
        hits = db.conn.execute(
            f"SELECT COUNT(*) FROM {lti.FTS_TABLE} WHERE {lti.FTS_TABLE} MATCH ?", ('raw : "eclat"',)
        ).fetchone()[0]
        assert hits == 5
        assert set(lti.TRIGGERS) <= _objects(db, "trigger")
    finally:
        db.close()


def test_v81_backfill_runs_in_batches(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "batch.db"))
    try:
        _seed(db)
        cur = db.conn.cursor()
        cur.execute("UPDATE library_tracks SET has_file = 0, max_size_bytes = NULL, artist_sort = '', album_sort = ''")
        lti.backfill_track_denorm(cur, batch_size=2)
        lti.rebuild_track_fts(cur, batch_size=2)
        assert cur.execute("SELECT COUNT(*) FROM library_tracks WHERE artist_sort = 'BAND'").fetchone()[0] == 5
        assert cur.execute("SELECT has_file FROM library_tracks WHERE id = 't2'").fetchone()[0] == 1
        assert cur.execute(f"SELECT COUNT(*) FROM {lti.FTS_TABLE}").fetchone()[0] == 5
    finally:
        db.close()


def test_v81_is_idempotent(tmp_path: Path) -> None:
    db = Database(str(tmp_path / "again.db"))
    try:
        _seed(db)
        cur = db.conn.cursor()
        db._migration_v81(cur)
        db._migration_v81(cur)
        db.conn.commit()
        assert db.conn.execute(f"SELECT COUNT(*) FROM {lti.FTS_TABLE}").fetchone()[0] == 5
        assert db.conn.execute("SELECT COUNT(*) FROM library_tracks WHERE has_file = 1").fetchone()[0] == 1
    finally:
        db.close()


def test_migrated_schema_equals_fresh_schema(tmp_path: Path) -> None:
    fresh = Database(str(tmp_path / "fresh.db"))
    old = Database(str(tmp_path / "old.db"))
    try:
        _downgrade(old)
        old._migration_v81(old.conn.cursor())
        old.conn.commit()

        def shape(db: Database) -> dict[str, str]:
            rows = db.conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE name LIKE 'idx_lib_tracks_hf%' "
                "OR name LIKE 'idx_lib_tracks_created_id' OR name LIKE 'trg_lib_%'"
            ).fetchall()
            return {r[0]: " ".join(str(r[1]).replace("IF NOT EXISTS ", "").split()) for r in rows}

        assert shape(fresh) == shape(old) and len(shape(fresh)) == len(lti.TRIGGERS) + len(lti.INDEX_DDL)
        cols = lambda db: [(r[1], r[2]) for r in db.conn.execute("PRAGMA table_info(library_tracks)") if r[1] in dict(lti.TRACK_COLUMNS)]  # noqa: E731
        assert sorted(cols(fresh)) == sorted(cols(old))
    finally:
        fresh.close()
        old.close()


@pytest.mark.parametrize("name", ["library_tracks_fts"])
def test_fts_is_trigram_substring_search(tmp_path: Path, name: str) -> None:
    db = Database(str(tmp_path / "fts.db"))
    try:
        _seed(db)
        count = lambda expr: db.conn.execute(  # noqa: E731
            f"SELECT COUNT(*) FROM {name} WHERE {name} MATCH ?", (expr,)
        ).fetchone()[0]
        assert count('raw : "ong"') == 5
        assert count('raw : "song 3"') == 1
        assert count('clean : "zzz"') == 0
    finally:
        db.close()

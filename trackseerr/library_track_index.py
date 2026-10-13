"""Denormalized sort columns, the has-file flag and the trigram search index behind the native Tracks list.

The Tracks list used to sort on joined expressions (``ar.sort_name``, ``al.sort_title``, the album date and a correlated
``MAX(size_bytes)`` subquery) and to search with ``LIKE '%x%'`` over six columns of three tables, so every request
scanned and sorted the whole catalog. This module keeps, on ``library_tracks`` itself:

* ``has_file`` / ``max_size_bytes``: maintained from ``library_files``;
* ``artist_sort`` / ``album_sort`` / ``album_date``: copies of the joined sort keys;
* ``library_tracks_fts``: a contentless FTS5 trigram table (rowid = the track's rowid) over the folded cleaned and raw
  search text of the track, its album and its artist, with ``\\x1f`` between the three parts so a needle never
  matches across a boundary.

Everything is maintained by SQLite triggers rather than by each Python write path, so the scanner, the Lidarr import,
artist refresh, relink and any raw ``DELETE``/``UPDATE`` stay consistent without having to remember to call anything.
The same DDL strings feed the fresh-install schema check, migration v81 and the benchmark fixture.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

FTS_TABLE = "library_tracks_fts"
BACKFILL_BATCH = 20000
FTS_MIN_NEEDLE = 3  # the trigram tokenizer cannot match fewer characters; shorter needles use LIKE

TRACK_COLUMNS: tuple[tuple[str, str], ...] = (
    ("has_file", "INTEGER NOT NULL DEFAULT 0"),
    ("max_size_bytes", "INTEGER"),
    ("artist_sort", "TEXT NOT NULL DEFAULT ''"),
    ("album_sort", "TEXT NOT NULL DEFAULT ''"),
    ("album_date", "TEXT"),
)

INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_lib_tracks_hf_title ON library_tracks(has_file, sort_title, id)",
    "CREATE INDEX IF NOT EXISTS idx_lib_tracks_hf_created ON library_tracks(has_file, created_at, id)",
    "CREATE INDEX IF NOT EXISTS idx_lib_tracks_hf_artist ON library_tracks(has_file, artist_sort, id)",
    "CREATE INDEX IF NOT EXISTS idx_lib_tracks_hf_album ON library_tracks(has_file, album_sort, id)",
    "CREATE INDEX IF NOT EXISTS idx_lib_tracks_hf_date ON library_tracks(has_file, album_date, id)",
    "CREATE INDEX IF NOT EXISTS idx_lib_tracks_hf_size ON library_tracks(has_file, max_size_bytes, id)",
    "CREATE INDEX IF NOT EXISTS idx_lib_tracks_created_id ON library_tracks(created_at, id)",
)

FTS_DDL = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
    "clean, raw, content='', contentless_delete=1, tokenize='trigram')"
)

# SELECT-list that builds one FTS row for the track ``t`` joined to its album ``al`` and artist ``ar``.
_FTS_FROM = (
    "FROM library_tracks t LEFT JOIN library_albums al ON al.id = t.album_id "
    "LEFT JOIN library_artists ar ON ar.id = t.artist_id"
)
_FTS_SELECT = (
    "SELECT t.rowid, "
    "t.search_clean || char(31) || COALESCE(al.search_clean, '') || char(31) || COALESCE(ar.search_clean, ''), "
    "t.search_text || char(31) || COALESCE(al.search_text, '') || char(31) || COALESCE(ar.search_text, '')"
)


def _fts_insert(where: str) -> str:
    return f"INSERT INTO {FTS_TABLE}(rowid, clean, raw) {_FTS_SELECT} {_FTS_FROM} WHERE {where}"


def _denorm_set(track: str) -> str:
    """``SET`` list recomputing the joined sort columns of the ``library_tracks`` row ``track`` (a table alias)."""
    return (
        f"artist_sort = COALESCE((SELECT sort_name FROM library_artists WHERE id = {track}.artist_id), ''), "
        f"album_sort = COALESCE((SELECT sort_title FROM library_albums WHERE id = {track}.album_id), ''), "
        f"album_date = (SELECT COALESCE(NULLIF(release_date, ''), CAST(year AS TEXT)) "
        f"FROM library_albums WHERE id = {track}.album_id)"
    )


def _file_set(track_id: str) -> str:
    return (
        f"has_file = EXISTS (SELECT 1 FROM library_files WHERE track_id = {track_id}), "
        f"max_size_bytes = (SELECT MAX(size_bytes) FROM library_files WHERE track_id = {track_id})"
    )


def _refresh_file(track_id: str) -> str:
    return f"UPDATE library_tracks SET {_file_set(track_id)} WHERE id = {track_id};"


_TRACK_TEXT_CHANGED = (
    "OLD.album_id IS NOT NEW.album_id OR OLD.artist_id IS NOT NEW.artist_id OR "
    "OLD.search_text IS NOT NEW.search_text OR OLD.search_clean IS NOT NEW.search_clean"
)

TRIGGERS: dict[str, str] = {
    "trg_lib_tracks_ai": f"""CREATE TRIGGER IF NOT EXISTS trg_lib_tracks_ai AFTER INSERT ON library_tracks
BEGIN
    UPDATE library_tracks SET {_denorm_set("library_tracks")} WHERE rowid = NEW.rowid;
    {_fts_insert("t.rowid = NEW.rowid")};
END""",
    "trg_lib_tracks_au": f"""CREATE TRIGGER IF NOT EXISTS trg_lib_tracks_au
AFTER UPDATE OF album_id, artist_id, search_text, search_clean ON library_tracks
WHEN {_TRACK_TEXT_CHANGED}
BEGIN
    UPDATE library_tracks SET {_denorm_set("library_tracks")} WHERE rowid = NEW.rowid;
    DELETE FROM {FTS_TABLE} WHERE rowid = NEW.rowid;
    {_fts_insert("t.rowid = NEW.rowid")};
END""",
    "trg_lib_tracks_ad": f"""CREATE TRIGGER IF NOT EXISTS trg_lib_tracks_ad AFTER DELETE ON library_tracks
BEGIN
    DELETE FROM {FTS_TABLE} WHERE rowid = OLD.rowid;
END""",
    "trg_lib_albums_au_sort": """CREATE TRIGGER IF NOT EXISTS trg_lib_albums_au_sort
AFTER UPDATE OF sort_title, release_date, year ON library_albums
WHEN OLD.sort_title IS NOT NEW.sort_title OR OLD.release_date IS NOT NEW.release_date OR OLD.year IS NOT NEW.year
BEGIN
    UPDATE library_tracks SET album_sort = COALESCE(NEW.sort_title, ''),
        album_date = COALESCE(NULLIF(NEW.release_date, ''), CAST(NEW.year AS TEXT))
    WHERE album_id = NEW.id;
END""",
    "trg_lib_albums_au_text": f"""CREATE TRIGGER IF NOT EXISTS trg_lib_albums_au_text
AFTER UPDATE OF search_text, search_clean ON library_albums
WHEN OLD.search_text IS NOT NEW.search_text OR OLD.search_clean IS NOT NEW.search_clean
BEGIN
    DELETE FROM {FTS_TABLE} WHERE rowid IN (SELECT rowid FROM library_tracks WHERE album_id = NEW.id);
    {_fts_insert("t.album_id = NEW.id")};
END""",
    "trg_lib_artists_au_sort": """CREATE TRIGGER IF NOT EXISTS trg_lib_artists_au_sort
AFTER UPDATE OF sort_name ON library_artists
WHEN OLD.sort_name IS NOT NEW.sort_name
BEGIN
    UPDATE library_tracks SET artist_sort = COALESCE(NEW.sort_name, '') WHERE artist_id = NEW.id;
END""",
    "trg_lib_artists_au_text": f"""CREATE TRIGGER IF NOT EXISTS trg_lib_artists_au_text
AFTER UPDATE OF search_text, search_clean ON library_artists
WHEN OLD.search_text IS NOT NEW.search_text OR OLD.search_clean IS NOT NEW.search_clean
BEGIN
    DELETE FROM {FTS_TABLE} WHERE rowid IN (SELECT rowid FROM library_tracks WHERE artist_id = NEW.id);
    {_fts_insert("t.artist_id = NEW.id")};
END""",
    "trg_lib_files_ai": """CREATE TRIGGER IF NOT EXISTS trg_lib_files_ai AFTER INSERT ON library_files
BEGIN
    UPDATE library_tracks SET has_file = 1,
        max_size_bytes = (SELECT MAX(size_bytes) FROM library_files WHERE track_id = NEW.track_id)
    WHERE id = NEW.track_id;
END""",
    "trg_lib_files_ad": f"""CREATE TRIGGER IF NOT EXISTS trg_lib_files_ad AFTER DELETE ON library_files
BEGIN
    {_refresh_file("OLD.track_id")}
END""",
    "trg_lib_files_au": f"""CREATE TRIGGER IF NOT EXISTS trg_lib_files_au
AFTER UPDATE OF track_id, size_bytes ON library_files
WHEN OLD.track_id IS NOT NEW.track_id OR OLD.size_bytes IS NOT NEW.size_bytes
BEGIN
    {_refresh_file("NEW.track_id")}
    {_refresh_file("OLD.track_id")}
END""",
}


def drop_track_triggers(cur: sqlite3.Cursor) -> None:
    for name in TRIGGERS:
        cur.execute(f"DROP TRIGGER IF EXISTS {name}")


def create_track_triggers(cur: sqlite3.Cursor) -> None:
    for sql in TRIGGERS.values():
        cur.execute(sql)


def create_track_indexes(cur: sqlite3.Cursor) -> None:
    for sql in INDEX_DDL:
        cur.execute(sql)


def add_track_columns(cur: sqlite3.Cursor) -> None:
    existing = {row[1] for row in cur.execute("PRAGMA table_info(library_tracks)").fetchall()}
    for name, decl in TRACK_COLUMNS:
        if name not in existing:
            cur.execute(f"ALTER TABLE library_tracks ADD COLUMN {name} {decl}")


def _rowid_batches(cur: sqlite3.Cursor, batch_size: int) -> list[tuple[int, int]]:
    low, high = cur.execute("SELECT MIN(rowid), MAX(rowid) FROM library_tracks").fetchone()
    if low is None:
        return []
    return [(start, min(start + batch_size - 1, high)) for start in range(low, high + 1, batch_size)]


def backfill_track_denorm(cur: sqlite3.Cursor, batch_size: int = BACKFILL_BATCH) -> None:
    """Recomputes ``has_file``/``max_size_bytes`` and the joined sort columns for every track, in rowid batches."""
    sql = (
        f"UPDATE library_tracks SET {_denorm_set('library_tracks')}, {_file_set('library_tracks.id')} "
        "WHERE rowid BETWEEN ? AND ?"
    )
    for start, end in _rowid_batches(cur, batch_size):
        cur.execute(sql, (start, end))


def rebuild_track_fts(cur: sqlite3.Cursor, batch_size: int = BACKFILL_BATCH) -> None:
    """Drops and repopulates the trigram table from the current track/album/artist search text."""
    cur.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
    cur.execute(FTS_DDL)
    for start, end in _rowid_batches(cur, batch_size):
        cur.execute(_fts_insert("t.rowid BETWEEN ? AND ?"), (start, end))


def create_track_index(cur: sqlite3.Cursor, batch_size: int = BACKFILL_BATCH) -> None:
    """Full (idempotent) setup on a database that has the base tables: columns, backfill, FTS, indexes, triggers."""
    drop_track_triggers(cur)
    add_track_columns(cur)
    backfill_track_denorm(cur, batch_size)
    rebuild_track_fts(cur, batch_size)
    create_track_indexes(cur)
    create_track_triggers(cur)


def fts_match_expression(clean_needle: Optional[str], raw_needle: str) -> Optional[str]:
    """The FTS5 MATCH string equal to the LIKE search, or None when LIKE has to be used.

    ``clean_needle`` / ``raw_needle`` are already ``fold_search_text`` output. Each needle becomes a quoted phrase,
    which the trigram tokenizer evaluates as a substring match, restricted to the column the LIKE clauses read.
    """
    parts: list[tuple[str, str]] = []
    if clean_needle:
        parts.append(("clean", clean_needle))
    parts.append(("raw", raw_needle))
    for _column, needle in parts:
        if len(needle) < FTS_MIN_NEEDLE or "\x1f" in needle or "\x00" in needle:
            return None
    return " OR ".join('%s : "%s"' % (column, needle.replace('"', '""')) for column, needle in parts)

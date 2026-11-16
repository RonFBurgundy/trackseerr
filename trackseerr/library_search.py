"""Library-wide search: best matches across artists, albums and tracks, plus a "did you mean" suggestion."""

import difflib
import threading
import weakref
from typing import Any, Optional

from trackseerr.storage import Database
from trackseerr.storage_common import clean_library_name

_SUGGEST_CUTOFF = 0.75

_cache_lock = threading.Lock()
_candidate_cache: "weakref.WeakKeyDictionary[Database, tuple[tuple[Any, ...], dict[str, str]]]" = weakref.WeakKeyDictionary()


def _rank_sql(column: str) -> str:
    """ORDER BY fragment: exact (0) > prefix (1) > substring (2), then shorter, then alphabetical."""
    return (
        f"CASE WHEN {column} = :q THEN 0 WHEN {column} LIKE :prefix THEN 1 ELSE 2 END, "
        f"LENGTH({column}), {column}"
    )


def _token_params(tokens: list[str]) -> dict[str, str]:
    return {f"t{i}": f"%{tok}%" for i, tok in enumerate(tokens)}


def _artist_rows(conn: Any, tokens: list[str], params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    where = " AND ".join(f"ar.clean_name LIKE :t{i}" for i in range(len(tokens)))
    rows = conn.execute(
        f"""
        SELECT ar.id, ar.name, ar.clean_name,
               (SELECT COUNT(*) FROM library_albums al WHERE al.artist_id = ar.id) AS album_count
        FROM library_artists ar
        WHERE {where}
        ORDER BY {_rank_sql("ar.clean_name")}
        LIMIT :limit
        """,  # noqa: S608 - only placeholder names are interpolated
        {**params, "limit": limit},
    ).fetchall()
    return [
        {"id": r[0], "name": r[1], "album_count": int(r[3] or 0), "_clean": r[2]} for r in rows
    ]


def _album_rows(conn: Any, tokens: list[str], params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    where = " AND ".join(f"(al.clean_title LIKE :t{i} OR ar.clean_name LIKE :t{i})" for i in range(len(tokens)))
    rows = conn.execute(
        f"""
        SELECT al.id, al.title, al.clean_title, al.artist_id, ar.name, al.year
        FROM library_albums al
        JOIN library_artists ar ON ar.id = al.artist_id
        WHERE {where}
        ORDER BY {_rank_sql("al.clean_title")}
        LIMIT :limit
        """,  # noqa: S608 - only placeholder names are interpolated
        {**params, "limit": limit},
    ).fetchall()
    return [
        {"id": r[0], "title": r[1], "artist_id": r[3], "artist_name": r[4], "year": r[5], "_clean": r[2]}
        for r in rows
    ]


def _track_rows(conn: Any, tokens: list[str], params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    where = " AND ".join(f"(t.clean_title LIKE :t{i} OR ar.clean_name LIKE :t{i})" for i in range(len(tokens)))
    rows = conn.execute(
        f"""
        SELECT t.id, t.title, t.clean_title, t.artist_id, ar.name, t.album_id, al.title
        FROM library_tracks t
        JOIN library_artists ar ON ar.id = t.artist_id
        JOIN library_albums al ON al.id = t.album_id
        WHERE {where}
          AND (t.has_file = 1 OR EXISTS (SELECT 1 FROM library_files f WHERE f.track_id = t.id))
        ORDER BY {_rank_sql("t.clean_title")}
        LIMIT :limit
        """,  # noqa: S608 - only placeholder names are interpolated
        {**params, "limit": limit},
    ).fetchall()
    return [
        {
            "id": r[0], "title": r[1], "artist_id": r[3], "artist_name": r[4],
            "album_id": r[5], "album_title": r[6], "_clean": r[2],
        }
        for r in rows
    ]


def _suggestion_candidates(conn: Any, db: Database) -> dict[str, str]:
    """Map of cleaned name -> display name over artists and albums, rebuilt only when the library changes."""
    stamp = tuple(conn.execute("SELECT COUNT(*), MAX(updated_at) FROM library_artists").fetchone()) + tuple(
        conn.execute("SELECT COUNT(*), MAX(updated_at) FROM library_albums").fetchone()
    )
    with _cache_lock:
        cached = _candidate_cache.get(db)
        if cached and cached[0] == stamp:
            return cached[1]
    candidates: dict[str, str] = {}
    for clean, display in conn.execute("SELECT clean_name, name FROM library_artists"):
        if clean:
            candidates.setdefault(clean, display)
    for clean, display in conn.execute("SELECT clean_title, title FROM library_albums"):
        if clean:
            candidates.setdefault(clean, display)
    with _cache_lock:
        _candidate_cache[db] = (stamp, candidates)
    return candidates


def _is_good_hit(cleaned: str, results: dict[str, list[dict[str, Any]]]) -> bool:
    for rows in results.values():
        if rows and str(rows[0]["_clean"]).startswith(cleaned):
            return True
    return False


def _strip(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in row.items() if k != "_clean"} for row in rows]


def search_library(db: Database, query: str, limit: int = 8) -> dict[str, Any]:
    """Best matches of every kind for ``query`` and, when nothing matches well, a close-name suggestion."""
    cleaned = clean_library_name(query)
    out: dict[str, Any] = {"query": query, "artists": [], "albums": [], "tracks": [], "suggestion": None}
    if not cleaned:
        return out
    tokens = cleaned.split()
    params: dict[str, Any] = {"q": cleaned, "prefix": f"{cleaned}%", **_token_params(tokens)}
    with db.reader() as conn:
        results = {
            "artists": _artist_rows(conn, tokens, params, limit),
            "albums": _album_rows(conn, tokens, params, limit),
            "tracks": _track_rows(conn, tokens, params, limit),
        }
        suggestion: Optional[str] = None
        if not _is_good_hit(cleaned, results):
            candidates = _suggestion_candidates(conn, db)
            match = difflib.get_close_matches(cleaned, list(candidates), n=1, cutoff=_SUGGEST_CUTOFF)
            if match:
                suggestion = candidates[match[0]]
    for kind, rows in results.items():
        out[kind] = _strip(rows)
    out["suggestion"] = suggestion
    return out

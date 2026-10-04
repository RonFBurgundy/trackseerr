"""Server-side paging, search and group index for the native library lists (artists, albums, tracks).

Each list is described once (``FROM`` clause, whitelisted sorts, search columns, row mapper). The paged query and the
index share the same ``FROM ... WHERE`` fragment and the same sort expression, so the index offsets are exact for
whatever filters were applied. Sort keys come only from the whitelists below; every value is a bound parameter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from plex_playlist_sync.list_index import SortDef, build_index, escape_like, fold_search_text, order_clause
from plex_playlist_sync.storage import Database, clean_library_name

_TRACK_SIZE = "(SELECT MAX(f.size_bytes) FROM library_files f WHERE f.track_id = t.id)"
_ALBUM_DATE = "COALESCE(NULLIF(al.release_date, ''), CAST(al.year AS TEXT))"


@dataclass(frozen=True)
class LibraryListSpec:
    select: str
    frm: str
    tiebreak: str
    sorts: dict[str, SortDef]
    default_sort: str
    clean_columns: tuple[str, ...]  # persisted folded cleaned-name columns, matched against the cleaned query
    raw_columns: tuple[str, ...]  # persisted folded raw-name columns, matched against the query as typed
    monitored_column: str
    filter_columns: dict[str, str]  # filter name -> column
    mapper: Callable[[Database], Callable[[Any], dict[str, Any]]]


LIBRARY_LISTS: dict[str, LibraryListSpec] = {
    "artists": LibraryListSpec(
        select="SELECT a.*",
        frm="FROM library_artists a",
        tiebreak="a.id",
        sorts={
            "name": SortDef("a.sort_name", "name"),
            "added_at": SortDef("a.created_at", "date"),
            "album_count": SortDef(
                "(SELECT COUNT(*) FROM library_albums x WHERE x.artist_id = a.id)", "count", "albums"
            ),
        },
        default_sort="name",
        clean_columns=("a.search_clean",),
        raw_columns=("a.search_text",),
        monitored_column="a.monitored",
        filter_columns={},
        mapper=lambda db: db._map_library_artist,
    ),
    "albums": LibraryListSpec(
        select="SELECT al.*",
        frm="FROM library_albums al LEFT JOIN library_artists ar ON ar.id = al.artist_id",
        tiebreak="al.id",
        sorts={
            "title": SortDef("al.sort_title", "name"),
            "artist": SortDef("COALESCE(ar.sort_name, '')", "name"),
            "release_date": SortDef(_ALBUM_DATE, "date"),
            "added_at": SortDef("al.created_at", "date"),
        },
        default_sort="title",
        clean_columns=("al.search_clean", "ar.search_clean"),
        raw_columns=("al.search_text", "ar.search_text"),
        monitored_column="al.monitored",
        filter_columns={"artist_id": "al.artist_id"},
        mapper=lambda db: db._map_library_album,
    ),
    "tracks": LibraryListSpec(
        select="SELECT t.*",
        frm=(
            "FROM library_tracks t LEFT JOIN library_albums al ON al.id = t.album_id "
            "LEFT JOIN library_artists ar ON ar.id = t.artist_id"
        ),
        tiebreak="t.id",
        sorts={
            "title": SortDef("t.sort_title", "name"),
            "artist": SortDef("COALESCE(ar.sort_name, '')", "name"),
            "album": SortDef("COALESCE(al.sort_title, '')", "name"),
            "added_at": SortDef("t.created_at", "date"),
            "size_bytes": SortDef(_TRACK_SIZE, "size"),
        },
        default_sort="title",
        clean_columns=("t.search_clean", "al.search_clean", "ar.search_clean"),
        raw_columns=("t.search_text", "al.search_text", "ar.search_text"),
        monitored_column="t.monitored",
        filter_columns={"artist_id": "t.artist_id", "album_id": "t.album_id"},
        mapper=lambda db: db._map_library_track,
    ),
}


def sort_keys(kind: str) -> tuple[str, ...]:
    return tuple(LIBRARY_LISTS[kind].sorts)


def default_sort_key(kind: str) -> str:
    return LIBRARY_LISTS[kind].default_sort


def _where(
    spec: LibraryListSpec,
    query: Optional[str],
    monitored_only: bool,
    filters: dict[str, Optional[str]],
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if monitored_only:
        clauses.append(f"{spec.monitored_column} = 1")
    for name, value in filters.items():
        column = spec.filter_columns.get(name)
        if column is not None and value:
            clauses.append(f"{column} = ?")
            params.append(str(value))
    text = (query or "").strip()
    if text:
        likes: list[str] = []
        # Matching is accent- and case-insensitive: the columns hold ``fold_search_text`` output persisted at write
        # time (migration v35 / the upsert paths), so only the needle is folded here and there is no per-row UDF. The cleaned form (punctuation stripped) widens a
        # search; skip it when the user typed a LIKE wildcard character, which must match literally and would
        # otherwise vanish from the cleaned text.
        clean = "" if any(ch in text for ch in "%_\\") else clean_library_name(text)
        if clean:
            for column in spec.clean_columns:
                likes.append(f"{column} LIKE ? ESCAPE '\\'")
                params.append(f"%{escape_like(fold_search_text(clean))}%")
        for column in spec.raw_columns:
            likes.append(f"{column} LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(fold_search_text(text))}%")
        clauses.append("(" + " OR ".join(likes) + ")")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def page_queries(
    kind: str,
    page: int,
    page_size: int,
    sort_key: str,
    sort_dir: str,
    query: Optional[str] = None,
    monitored_only: bool = False,
    filters: Optional[dict[str, Optional[str]]] = None,
) -> tuple[tuple[str, list[Any]], tuple[str, list[Any]]]:
    """``((page_sql, params), (count_sql, params))`` for one page of ``kind``."""
    spec = LIBRARY_LISTS[kind]
    sort = spec.sorts[sort_key]  # KeyError for an unknown key: the route validates against the whitelist first
    where, params = _where(spec, query, monitored_only, filters or {})
    order = order_clause(sort, sort_dir, spec.tiebreak)
    offset = (int(page) - 1) * int(page_size)
    page_sql = f"{spec.select} {spec.frm}{where} {order} LIMIT ? OFFSET ?"
    return (page_sql, [*params, int(page_size), offset]), (f"SELECT COUNT(*) {spec.frm}{where}", params)


def page_library(
    db: Database,
    kind: str,
    page: int,
    page_size: int,
    sort_key: str,
    sort_dir: str,
    query: Optional[str] = None,
    monitored_only: bool = False,
    filters: Optional[dict[str, Optional[str]]] = None,
) -> tuple[list[dict[str, Any]], int]:
    """One page of ``kind`` (``artists``/``albums``/``tracks``) plus the filtered total."""
    (page_sql, page_params), (count_sql, count_params) = page_queries(
        kind, page, page_size, sort_key, sort_dir, query, monitored_only, filters
    )
    mapper = LIBRARY_LISTS[kind].mapper(db)
    with db._lock:
        total = db.conn.execute(count_sql, count_params).fetchone()[0]
        rows = db.conn.execute(page_sql, page_params).fetchall()
        return [mapper(row) for row in rows], int(total)


def index_library(
    db: Database,
    kind: str,
    sort_key: str,
    sort_dir: str,
    query: Optional[str] = None,
    monitored_only: bool = False,
    filters: Optional[dict[str, Optional[str]]] = None,
) -> tuple[int, list[dict[str, Any]]]:
    """``(total, groups)`` for the same ordering and filters ``page_library`` uses."""
    spec = LIBRARY_LISTS[kind]
    sort = spec.sorts[sort_key]
    where, params = _where(spec, query, monitored_only, filters or {})
    with db._lock:
        return build_index(db.conn, f"{spec.frm}{where}", params, sort, sort_dir)

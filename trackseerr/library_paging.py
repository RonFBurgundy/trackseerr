"""Server-side paging, search and group index for the native library lists (artists, albums, tracks).

Each list is described once (``FROM`` clause, whitelisted sorts, search columns, row mapper). The paged query and the
index share the same ``FROM ... WHERE`` fragment and the same sort expression, so the index offsets are exact for
whatever filters were applied. Sort keys come only from the whitelists below; every value is a bound parameter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trackseerr.library_filters import LibraryFacetFilter, facet_clauses
from trackseerr.library_track_index import FTS_TABLE, fts_match_expression
from trackseerr.list_index import SortDef, build_index, escape_like, fold_search_text, order_clause
from trackseerr.storage import Database, clean_library_name

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
    has_file_column: Optional[str] = None  # persisted 0/1 flag behind the ``has_file`` filter (tracks only)
    fts_table: Optional[str] = None  # trigram search table keyed by the list's rowid (tracks only)
    base_frm: Optional[str] = None  # ``frm`` without its joins; each join below is added only when the query reads it
    joins: tuple[tuple[str, str], ...] = ()  # (alias, LEFT JOIN clause), looked for in WHERE and ORDER BY


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
            "formed": SortDef("COALESCE(CAST(a.begin_year AS TEXT), '')", "date"),
            "popularity": SortDef("COALESCE(a.popularity, 0)", "count", "fans"),
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
            "popularity": SortDef("COALESCE(ar.popularity, 0)", "count", "fans"),
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
            "artist": SortDef("t.artist_sort", "name"),
            "album": SortDef("t.album_sort", "name"),
            "added_at": SortDef("t.created_at", "date"),
            "size_bytes": SortDef("t.max_size_bytes", "size"),
            "year": SortDef("t.album_date", "date"),
            "popularity": SortDef("COALESCE(ar.popularity, 0)", "count", "fans"),
        },
        default_sort="title",
        clean_columns=("t.search_clean", "al.search_clean", "ar.search_clean"),
        raw_columns=("t.search_text", "al.search_text", "ar.search_text"),
        monitored_column="t.monitored",
        filter_columns={"artist_id": "t.artist_id", "album_id": "t.album_id"},
        mapper=lambda db: db._map_library_track,
        has_file_column="t.has_file",
        fts_table=FTS_TABLE,
        base_frm="FROM library_tracks t",
        joins=(
            ("al", "LEFT JOIN library_albums al ON al.id = t.album_id"),
            ("ar", "LEFT JOIN library_artists ar ON ar.id = t.artist_id"),
        ),
    ),
}


def sort_keys(kind: str) -> tuple[str, ...]:
    return tuple(LIBRARY_LISTS[kind].sorts)


def default_sort_key(kind: str) -> str:
    return LIBRARY_LISTS[kind].default_sort


def _search_clause(spec: LibraryListSpec, text: str) -> tuple[str, list[Any]]:
    """The ``(sql, params)`` matching ``text`` against the list's folded search columns.

    Matching is accent- and case-insensitive: the columns hold ``fold_search_text`` output persisted at write time
    (migration v35 / the upsert paths), so only the needle is folded here and there is no per-row UDF. The cleaned form
    (punctuation stripped) widens a search; skip it when the user typed a LIKE wildcard character, which must match
    literally and would otherwise vanish from the cleaned text. Lists with a trigram table (tracks) answer needles of
    three or more characters from it, which returns exactly the rows the LIKE form would.
    """
    clean = "" if any(ch in text for ch in "%_\\") else clean_library_name(text)
    clean_needle = fold_search_text(clean) if clean else None
    raw_needle = fold_search_text(text)
    if spec.fts_table is not None:
        match = fts_match_expression(clean_needle, raw_needle)
        if match is not None:
            return f"t.rowid IN (SELECT rowid FROM {spec.fts_table} WHERE {spec.fts_table} MATCH ?)", [match]
    likes: list[str] = []
    params: list[Any] = []
    if clean_needle:
        for column in spec.clean_columns:
            likes.append(f"{column} LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(clean_needle)}%")
    for column in spec.raw_columns:
        likes.append(f"{column} LIKE ? ESCAPE '\\'")
        params.append(f"%{escape_like(raw_needle)}%")
    return "(" + " OR ".join(likes) + ")", params


def _where(
    spec: LibraryListSpec,
    query: Optional[str],
    monitored_only: bool,
    filters: dict[str, Optional[str]],
    facets: Optional[LibraryFacetFilter] = None,
    kind: Optional[str] = None,
    has_file: Optional[bool] = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if monitored_only:
        clauses.append(f"{spec.monitored_column} = 1")
    if has_file is not None and spec.has_file_column is not None:
        # Inlined literal (not a bound parameter) so the planner can match the (has_file, <sort>, id) indexes.
        clauses.append(f"{spec.has_file_column} = {1 if has_file else 0}")
    for name, value in filters.items():
        column = spec.filter_columns.get(name)
        if column is not None and value:
            clauses.append(f"{column} = ?")
            params.append(str(value))
    if facets is not None and not facets.is_empty() and kind is not None:
        facet_c, facet_p = facet_clauses(facets, kind)
        clauses.extend(facet_c)
        params.extend(facet_p)
    text = (query or "").strip()
    if text:
        search_sql, search_params = _search_clause(spec, text)
        clauses.append(search_sql)
        params.extend(search_params)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _from(spec: LibraryListSpec, where: str, sort: SortDef) -> str:
    """The ``FROM`` clause, joining only the tables a filter, the search or the sort actually reads.

    SQLite does not eliminate the unused LEFT JOINs of the tracks list, and carrying them made every count and page
    walk three tables for no reason.
    """
    if spec.base_frm is None:
        return spec.frm
    touched = where + " " + sort.expr
    needed = [clause for alias, clause in spec.joins if re.search(rf"\b{alias}\.", touched)]
    return " ".join([spec.base_frm, *needed])


def page_queries(
    kind: str,
    page: int,
    page_size: int,
    sort_key: str,
    sort_dir: str,
    query: Optional[str] = None,
    monitored_only: bool = False,
    filters: Optional[dict[str, Optional[str]]] = None,
    facets: Optional[LibraryFacetFilter] = None,
    has_file: Optional[bool] = None,
) -> tuple[tuple[str, list[Any]], tuple[str, list[Any]]]:
    """``((page_sql, params), (count_sql, params))`` for one page of ``kind``."""
    spec = LIBRARY_LISTS[kind]
    sort = spec.sorts[sort_key]  # KeyError for an unknown key: the route validates against the whitelist first
    where, params = _where(
        spec, query, monitored_only, filters or {}, facets=facets, kind=kind, has_file=has_file
    )
    order = order_clause(sort, sort_dir, spec.tiebreak)
    offset = (int(page) - 1) * int(page_size)
    frm = _from(spec, where, sort)
    page_sql = f"{spec.select} {frm}{where} {order} LIMIT ? OFFSET ?"
    return (page_sql, [*params, int(page_size), offset]), (f"SELECT COUNT(*) {frm}{where}", params)


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
    facets: Optional[LibraryFacetFilter] = None,
    has_file: Optional[bool] = None,
) -> tuple[list[dict[str, Any]], int]:
    """One page of ``kind`` (``artists``/``albums``/``tracks``) plus the filtered total."""
    (page_sql, page_params), (count_sql, count_params) = page_queries(
        kind, page, page_size, sort_key, sort_dir, query, monitored_only, filters, facets=facets, has_file=has_file
    )
    mapper = LIBRARY_LISTS[kind].mapper(db)
    # The lock covers only the two statements; row mapping (and everything after) runs without blocking other users.
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
    facets: Optional[LibraryFacetFilter] = None,
    has_file: Optional[bool] = None,
) -> tuple[int, list[dict[str, Any]]]:
    """``(total, groups)`` for the same ordering and filters ``page_library`` uses."""
    spec = LIBRARY_LISTS[kind]
    sort = spec.sorts[sort_key]
    where, params = _where(
        spec, query, monitored_only, filters or {}, facets=facets, kind=kind, has_file=has_file
    )
    with db._lock:
        return build_index(db.conn, f"{_from(spec, where, sort)}{where}", params, sort, sort_dir)

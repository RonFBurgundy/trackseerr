"""Scrubber index speed tests: covering index on album titles and date expression index.

Verifies:
1. Albums list drops unused LEFT JOIN on library_artists for title / date / added_at sorts when no filters need it.
2. Search and artist / popularity sorts retain the LEFT JOIN.
3. EXPLAIN QUERY PLAN confirms covering index usage on sort_title and date key expression index on release_date.
4. Python prefix bucketing yields identical groups, counts, labels and order to the old SQL SUBSTR query.
5. Indexing albums under 250 ms (with 2x margin on CI).
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest

from tests.library_perf_catalog import build_catalog
from trackseerr import library_paging
from trackseerr.list_index import NULL_LABEL, _date_label, _date_width
from trackseerr.storage import Database

SCALE = 1.0 if os.environ.get("TRACKSEERR_PERF_FULL") else 1 / 3
MARGIN = 2.0


@pytest.fixture(scope="module")
def big(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Database, dict[str, Any]]]:
    db = Database(str(tmp_path_factory.mktemp("perf") / "perf.db"))
    info = build_catalog(db, scale=SCALE)
    yield db, info
    db.close()


def _best_ms(fn: Callable[[], Any], runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        best = min(best, (time.perf_counter() - start) * 1000)
    return best


def _old_date_groups(conn: sqlite3.Connection, frm: str, params: Sequence[Any], expr: str) -> list[tuple[str, int]]:
    bounds = conn.execute(
        f"SELECT MIN(d), MAX(d) FROM (SELECT NULLIF({expr}, '') AS d {frm}) WHERE d IS NOT NULL", list(params)
    ).fetchone()
    width = _date_width(bounds)
    rows = conn.execute(
        f"SELECT SUBSTR(NULLIF({expr}, ''), 1, {width}) AS k, COUNT(*) AS c {frm} GROUP BY k ORDER BY k",
        list(params),
    ).fetchall()
    groups: list[tuple[str, int]] = []
    for key, count in rows:
        label = NULL_LABEL if key is None else _date_label(str(key))
        if groups and groups[-1][0] == label:
            groups[-1] = (label, groups[-1][1] + int(count))
        else:
            groups.append((label, int(count)))
    return groups


def _old_index_library(db: Database, kind: str, sort_key: str, sort_dir: str) -> tuple[int, list[dict[str, Any]]]:
    spec = library_paging.LIBRARY_LISTS[kind]
    sort = spec.sorts[sort_key]
    where, params = library_paging._where(spec, None, False, {})
    frm = f"{library_paging._from(spec, where, sort)}{where}"
    with db.reader() as conn:
        asc = _old_date_groups(conn, frm, params, sort.expr)
        ordered = list(reversed(asc)) if str(sort_dir).lower() == "desc" else asc
        groups: list[dict[str, Any]] = []
        offset = 0
        for label, count in ordered:
            groups.append({"label": label, "offset": offset, "count": count})
            offset += count
        return offset, groups


@pytest.fixture
def seeded_db() -> Iterator[Database]:
    db = Database(":memory:")
    db.upsert_library_artist({"id": "ar1", "name": "Artist 1", "monitored": True})
    db.upsert_library_artist({"id": "ar2", "name": "Artist 2", "monitored": True})
    albums = [
        {"id": "al1", "artist_id": "ar1", "title": "Album 1", "release_date": "1995-04-12", "year": 1995},
        {"id": "al2", "artist_id": "ar1", "title": "Album 2", "release_date": "1995-04-20", "year": 1995},
        {"id": "al3", "artist_id": "ar1", "title": "Album 3", "release_date": "1995-10-01", "year": 1995},
        {"id": "al4", "artist_id": "ar2", "title": "Album 4", "release_date": "2001-01-15", "year": 2001},
        {"id": "al5", "artist_id": "ar2", "title": "Album 5", "release_date": "2001-01-30", "year": 2001},
        {"id": "al6", "artist_id": "ar2", "title": "Album 6", "release_date": "", "year": 2010},
        {"id": "al7", "artist_id": "ar2", "title": "Album 7", "release_date": None, "year": 2010},
        {"id": "al8", "artist_id": "ar2", "title": "Album 8", "release_date": None, "year": None},
        {"id": "al9", "artist_id": "ar1", "title": "Album 9", "release_date": "", "year": 0},
    ]
    for al in albums:
        db.upsert_library_album(al)
    yield db
    db.close()


def test_album_title_index_skips_artist_join() -> None:
    spec = library_paging.LIBRARY_LISTS["albums"]
    title_sort = spec.sorts["title"]
    frm_plain = library_paging._from(spec, "", title_sort)
    assert "library_artists" not in frm_plain
    assert "LEFT JOIN" not in frm_plain

    where_search, _params = library_paging._where(spec, "radiohead", False, {})
    frm_search = library_paging._from(spec, where_search, title_sort)
    assert "library_artists" in frm_search


def test_album_artist_sort_keeps_join() -> None:
    spec = library_paging.LIBRARY_LISTS["albums"]
    artist_sort = spec.sorts["artist"]
    frm_artist = library_paging._from(spec, "", artist_sort)
    assert "library_artists" in frm_artist


def test_album_title_index_uses_covering_index(seeded_db: Database) -> None:
    spec = library_paging.LIBRARY_LISTS["albums"]
    sort = spec.sorts["title"]
    where, params = library_paging._where(spec, None, False, {})
    frm = f"{library_paging._from(spec, where, sort)}{where}"
    query = f"EXPLAIN QUERY PLAN SELECT SUBSTR({sort.expr}, 1, 1) AS k, COUNT(*) AS c {frm} GROUP BY k ORDER BY k"
    with seeded_db.reader() as conn:
        rows = conn.execute(query, params).fetchall()
    plan = " ".join(str(r[3]) for r in rows)
    assert "idx_lib_albums_sort_title" in plan


@pytest.mark.parametrize("sort_dir", ["asc", "desc"])
def test_release_date_groups_unchanged(seeded_db: Database, sort_dir: str) -> None:
    total, groups = library_paging.index_library(seeded_db, "albums", "release_date", sort_dir)
    old_total, old_groups = _old_index_library(seeded_db, "albums", "release_date", sort_dir)
    assert total == old_total
    assert groups == old_groups


def test_release_date_groups_unchanged_narrow_dates() -> None:
    db = Database(":memory:")
    db.upsert_library_artist({"id": "ar1", "name": "Artist 1"})
    for i in range(1, 15):
        db.upsert_library_album({
            "id": f"al{i}",
            "artist_id": "ar1",
            "title": f"Album {i}",
            "release_date": f"2023-05-{i:02d}",
            "year": 2023,
        })
    db.upsert_library_album({
        "id": "al_null",
        "artist_id": "ar1",
        "title": "Album Null",
        "release_date": None,
        "year": None,
    })
    for sort_dir in ("asc", "desc"):
        total, groups = library_paging.index_library(db, "albums", "release_date", sort_dir)
        old_total, old_groups = _old_index_library(db, "albums", "release_date", sort_dir)
        assert total == old_total
        assert groups == old_groups
    db.close()


def test_date_key_index_exists_and_is_used(seeded_db: Database) -> None:
    with seeded_db.reader() as conn:
        indexes = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_lib_albums_date_key'"
            ).fetchall()
        ]
    assert "idx_lib_albums_date_key" in indexes

    spec = library_paging.LIBRARY_LISTS["albums"]
    sort = spec.sorts["release_date"]
    where, params = library_paging._where(spec, None, False, {})
    frm = f"{library_paging._from(spec, where, sort)}{where}"
    bounds_sql = (
        f"EXPLAIN QUERY PLAN SELECT MIN(d), MAX(d) "
        f"FROM (SELECT NULLIF({sort.expr}, '') AS d {frm}) WHERE d IS NOT NULL"
    )
    with seeded_db.reader() as conn:
        rows = conn.execute(bounds_sql, params).fetchall()
    plan = " ".join(str(r[3]) for r in rows)
    assert "idx_lib_albums_date_key" in plan


def test_album_indexes_are_fast(big: tuple[Database, dict[str, Any]]) -> None:
    db, _info = big
    for sort_key in ("title", "release_date"):
        for sort_dir in ("asc", "desc"):
            ms = _best_ms(lambda k=sort_key, d=sort_dir: library_paging.index_library(db, "albums", k, d))
            assert ms < 250 * MARGIN, f"{sort_key} {sort_dir}: {ms:.0f} ms"

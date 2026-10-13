"""Tracks list performance: has_file filter, denormalized sorts and the trigram search, on a synthetic catalog.

The big catalog is built once per module. It defaults to a third of the real library (317k catalog tracks, 30k
files) so the default run stays quick; ``TRACKSEERR_PERF_FULL=1`` builds the full-size one. Timing assertions use the
spec targets with a 2x margin for noisy CI; the benchmark table in the PR comes from the full-size run.
"""

from __future__ import annotations

import dataclasses
import os
import time
from typing import Any, Callable, Iterator

import pytest
from fastapi.testclient import TestClient

from tests.library_perf_catalog import build_catalog
from trackseerr import library_paging
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
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


def _ids(db: Database, sql: str, params: list[Any]) -> list[str]:
    with db._lock:
        return [r[0] for r in db.conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------------- correctness on the big catalog


def test_has_file_filter_matches_files(big: tuple[Database, dict[str, Any]]) -> None:
    db, info = big
    _rows, with_file = library_paging.page_library(db, "tracks", 1, 1, "title", "asc", has_file=True)
    _rows, without = library_paging.page_library(db, "tracks", 1, 1, "title", "asc", has_file=False)
    _rows, everything = library_paging.page_library(db, "tracks", 1, 1, "title", "asc")
    assert with_file == info["file_tracks"]
    assert with_file + without == everything == info["tracks"]
    exact = db.conn.execute("SELECT COUNT(DISTINCT track_id) FROM library_files").fetchone()[0]
    assert with_file == exact


# The sort expressions the list used before the denormalized columns, evaluated on the same data.
_OLD_SORTS = {
    "title": "t.sort_title",
    "artist": "COALESCE(ar.sort_name, '')",
    "album": "COALESCE(al.sort_title, '')",
    "added_at": "t.created_at",
    "size_bytes": "(SELECT MAX(f.size_bytes) FROM library_files f WHERE f.track_id = t.id)",
    "year": "COALESCE(NULLIF(al.release_date, ''), CAST(al.year AS TEXT))",
    "popularity": "COALESCE(ar.popularity, 0)",
}


@pytest.mark.parametrize("sort_key", sorted(_OLD_SORTS))
@pytest.mark.parametrize("sort_dir", ["asc", "desc"])
def test_sorted_pages_equal_the_old_join_sort(big: tuple[Database, dict[str, Any]], sort_key: str, sort_dir: str) -> None:
    db, _info = big
    direction = sort_dir.upper()
    for has_file, clause in ((True, "WHERE t.has_file = 1"), (None, "")):
        old = _ids(
            db,
            "SELECT t.id FROM library_tracks t LEFT JOIN library_albums al ON al.id = t.album_id "
            f"LEFT JOIN library_artists ar ON ar.id = t.artist_id {clause} "
            f"ORDER BY {_OLD_SORTS[sort_key]} {direction}, t.id {direction} LIMIT 40 OFFSET 20",
            [],
        )
        rows, _total = library_paging.page_library(db, "tracks", 2, 20, sort_key, sort_dir, has_file=has_file)
        assert [r["id"] for r in rows] == old[:20]  # page 2 of size 20 is rows 20..39
        rows, _total = library_paging.page_library(db, "tracks", 1, 40, sort_key, sort_dir, has_file=has_file)
        assert [r["id"] for r in rows][20:] == old[:20]


@pytest.mark.parametrize("needle_key", ["word3", "word10"])
def test_fts_search_equals_like_search(big: tuple[Database, dict[str, Any]], needle_key: str) -> None:
    db, info = big
    spec = library_paging.LIBRARY_LISTS["tracks"]
    like_spec = dataclasses.replace(spec, fts_table=None)
    needle = info[needle_key]
    fts_where, fts_params = library_paging._where(spec, needle, False, {})
    like_where, like_params = library_paging._where(like_spec, needle, False, {})
    assert "MATCH" in fts_where and "LIKE" in like_where
    frm = spec.frm
    got = _ids(db, f"SELECT t.id {frm}{fts_where} ORDER BY t.id", fts_params)
    want = _ids(db, f"SELECT t.id {frm}{like_where} ORDER BY t.id", like_params)
    assert got == want and got


# ---------------------------------------------------------------------------------------------------- timings


@pytest.mark.parametrize("sort_key", library_paging.sort_keys("tracks"))
def test_has_file_page_one_is_fast(big: tuple[Database, dict[str, Any]], sort_key: str) -> None:
    db, _info = big
    ms = _best_ms(lambda: library_paging.page_library(db, "tracks", 1, 50, sort_key, "asc", has_file=True))
    assert ms < 150 * MARGIN, f"{sort_key}: {ms:.0f} ms"


def test_has_file_count_and_index_are_fast(big: tuple[Database, dict[str, Any]]) -> None:
    db, _info = big
    count_ms = _best_ms(lambda: library_paging.page_library(db, "tracks", 1, 1, "title", "asc", has_file=True))
    assert count_ms < 50 * MARGIN
    for sort_key in ("title", "artist", "size_bytes", "year"):
        ms = _best_ms(lambda: library_paging.index_library(db, "tracks", sort_key, "asc", has_file=True))
        assert ms < 300 * MARGIN, f"index {sort_key}: {ms:.0f} ms"


def test_whole_catalog_default_page_is_fast(big: tuple[Database, dict[str, Any]]) -> None:
    db, _info = big
    assert _best_ms(lambda: library_paging.page_library(db, "tracks", 1, 50, "title", "asc")) < 300 * MARGIN
    assert _best_ms(lambda: library_paging.page_library(db, "tracks", 200, 50, "title", "desc")) < 300 * MARGIN


@pytest.mark.parametrize("needle_key", ["word3", "word10"])
def test_search_is_fast(big: tuple[Database, dict[str, Any]], needle_key: str) -> None:
    db, info = big
    needle = info[needle_key]
    whole = _best_ms(lambda: library_paging.page_library(db, "tracks", 1, 50, "title", "asc", needle))
    filtered = _best_ms(lambda: library_paging.page_library(db, "tracks", 1, 50, "title", "asc", needle, has_file=True))
    assert whole < 500 * MARGIN, f"whole catalog: {whole:.0f} ms"
    assert filtered < 200 * MARGIN, f"has_file: {filtered:.0f} ms"


# ------------------------------------------------------------------------- trigger maintenance on a small database


@pytest.fixture
def small() -> Iterator[Database]:
    db = Database(":memory:")
    db.upsert_library_artist({"id": "a1", "name": "Éric Clapton", "monitored": True})
    db.upsert_library_artist({"id": "a2", "name": "The Who", "monitored": True})
    db.upsert_library_album({"id": "al1", "artist_id": "a1", "title": "Unplugged", "year": 1992, "release_date": "1992-08-25"})
    db.upsert_library_album({"id": "al2", "artist_id": "a2", "title": "Tommy", "year": 1969})
    for tid, album, artist, title in (
        ("t1", "al1", "a1", "Tears in Heaven"),
        ("t2", "al1", "a1", "Layla (Acoustic)"),
        ("t3", "al2", "a2", "Pinball Wizard"),
    ):
        db.upsert_library_track({"id": tid, "album_id": album, "artist_id": artist, "title": title})
    yield db
    db.close()


def _track(db: Database, track_id: str) -> Any:
    return db.conn.execute(
        "SELECT has_file, max_size_bytes, artist_sort, album_sort, album_date "
        "FROM library_tracks WHERE id = ?",
        (track_id,),
    ).fetchone()


def _file(db: Database, fid: str, track_id: str, size: int) -> None:
    db.upsert_library_file(
        {"id": fid, "track_id": track_id, "file_path": f"/m/{fid}.flac", "relative_path": f"{fid}.flac",
         "codec": "FLAC", "quality_name": "FLAC", "size_bytes": size}
    )


def _search(db: Database, q: str, **kw: Any) -> list[str]:
    rows, total = library_paging.page_library(db, "tracks", 1, 50, "title", "asc", q, **kw)
    assert total == len(rows)
    return sorted(r["id"] for r in rows)


def test_insert_fills_denormalized_columns(small: Database) -> None:
    row = _track(small, "t1")
    assert (row["has_file"], row["max_size_bytes"]) == (0, None)
    assert row["artist_sort"] == "ERIC CLAPTON"
    assert row["album_sort"] == "UNPLUGGED"
    assert row["album_date"] == "1992-08-25"
    assert _track(small, "t3")["album_date"] == "1969"


def test_file_upsert_delete_and_relink_maintain_has_file(small: Database) -> None:
    _file(small, "f1", "t1", 100)
    _file(small, "f2", "t1", 900)
    assert tuple(_track(small, "t1"))[:2] == (1, 900)
    _file(small, "f2", "t1", 50)  # size change on an existing row
    assert tuple(_track(small, "t1"))[:2] == (1, 100)
    assert small.relink_library_file("f1", "t2")
    assert tuple(_track(small, "t1"))[:2] == (1, 50)
    assert tuple(_track(small, "t2"))[:2] == (1, 100)
    assert small.delete_library_file("f1")
    assert tuple(_track(small, "t2"))[:2] == (0, None)
    small.conn.execute("DELETE FROM library_files WHERE id = 'f2'")  # raw delete is covered too
    small.conn.commit()
    assert tuple(_track(small, "t1"))[:2] == (0, None)
    rows, total = library_paging.page_library(small, "tracks", 1, 50, "title", "asc", has_file=False)
    assert total == 3 and len(rows) == 3


def test_has_file_filter_splits_the_list(small: Database) -> None:
    _file(small, "f1", "t3", 10)
    assert [r["id"] for r in library_paging.page_library(small, "tracks", 1, 50, "title", "asc", has_file=True)[0]] == ["t3"]
    assert _search(small, "", has_file=False) == ["t1", "t2"]
    assert _search(small, "", ) == ["t1", "t2", "t3"]


def test_album_and_artist_changes_propagate(small: Database) -> None:
    small.upsert_library_album({"id": "al1", "artist_id": "a1", "title": "Reptile", "year": 2001, "release_date": "2001-03-05"})
    row = _track(small, "t1")
    assert (row["album_sort"], row["album_date"]) == ("REPTILE", "2001-03-05")
    assert _search(small, "reptile") == ["t1", "t2"]
    assert _search(small, "unplugged") == []
    small.upsert_library_artist({"id": "a1", "name": "Slowhand", "monitored": True})
    row = _track(small, "t1")
    assert row["artist_sort"] == "SLOWHAND"
    assert _search(small, "slowhand") == ["t1", "t2"]
    assert _search(small, "clapton") == []


def test_track_rename_and_delete_update_search(small: Database) -> None:
    small.upsert_library_track({"id": "t3", "album_id": "al2", "artist_id": "a2", "title": "Sally Simpson"})
    assert _search(small, "pinball") == []
    assert _search(small, "simpson") == ["t3"]
    assert small.delete_library_track("t3")
    assert _search(small, "simpson") == []
    fts_rows = small.conn.execute("SELECT COUNT(*) FROM library_tracks_fts").fetchone()[0]
    assert fts_rows == 2


@pytest.mark.parametrize(
    "needle",
    ["clapton", "ERIC", "éric", "eric cl", "tears in", "layla (acou", "(acoustic)", "acoustic", "layla acoustic",
     "who", "the who", "tommy", "wiz", "zz", "e", "%", "_", "a_c", "tears%", "50%", "unplug", "heaven", "ERIC CLAPTON UNPLUGGED"],
)
def test_search_matches_like_semantics(small: Database, needle: str) -> None:
    spec = library_paging.LIBRARY_LISTS["tracks"]
    like_spec = dataclasses.replace(spec, fts_table=None)
    fts_where, fts_params = library_paging._where(spec, needle, False, {})
    like_where, like_params = library_paging._where(like_spec, needle, False, {})
    got = _ids(small, f"SELECT t.id {spec.frm}{fts_where} ORDER BY t.id", fts_params)
    want = _ids(small, f"SELECT t.id {spec.frm}{like_where} ORDER BY t.id", like_params)
    assert got == want


def test_short_needle_uses_like_and_long_uses_fts() -> None:
    spec = library_paging.LIBRARY_LISTS["tracks"]
    assert "MATCH" not in library_paging._where(spec, "ab", False, {})[0]
    assert "MATCH" in library_paging._where(spec, "abc", False, {})[0]
    assert "MATCH" not in library_paging._where(library_paging.LIBRARY_LISTS["albums"], "abcdef", False, {})[0]


def test_needle_with_quote_does_not_break_the_match(small: Database) -> None:
    assert _search(small, 'say "hi"') == []
    assert _search(small, "tears in heaven") == ["t1"]


def test_joins_are_dropped_only_when_unused() -> None:
    spec = library_paging.LIBRARY_LISTS["tracks"]
    (plain, _p), (count, _c) = library_paging.page_queries("tracks", 1, 50, "artist", "asc", has_file=True)
    assert "JOIN" not in plain and "JOIN" not in count
    (searched, _p), _ = library_paging.page_queries("tracks", 1, 50, "title", "asc", query="ab")
    assert "JOIN library_albums" in searched and "JOIN library_artists" in searched
    (popular, _p), _ = library_paging.page_queries("tracks", 1, 50, "popularity", "asc")
    assert "JOIN library_artists" in popular and "JOIN library_albums" not in popular
    assert spec.base_frm is not None


# ----------------------------------------------------------------------------------------------------- the route


@pytest.fixture
def api_small(small: Database, tmp_path: Any) -> Iterator[tuple[TestClient, dict[str, str]]]:
    config = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    admin = small.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    app = create_app(db=small, config=config)
    app.dependency_overrides[get_db] = lambda: small
    app.dependency_overrides[get_config] = lambda: config
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=admin["id"], username=admin["username"], is_admin=True, secret_key=secret)
    small.create_session(token, admin["id"], {"auth": "test"})
    yield TestClient(app), {"Authorization": f"Bearer {token}"}


def test_route_has_file_param(small: Database, api_small: tuple[TestClient, dict[str, str]]) -> None:
    client, headers = api_small
    _file(small, "f1", "t2", 10)
    on = client.get("/api/library/tracks/paged?has_file=true", headers=headers).json()
    off = client.get("/api/library/tracks/paged?has_file=false", headers=headers).json()
    both = client.get("/api/library/tracks/paged", headers=headers).json()
    assert [r["id"] for r in on["records"]] == ["t2"] and on["total"] == 1
    assert off["total"] == 2 and both["total"] == 3
    idx = client.get("/api/library/tracks/index?has_file=true", headers=headers).json()
    assert idx["total"] == 1
    assert client.get("/api/library/tracks/paged?has_file=maybe", headers=headers).status_code == 422

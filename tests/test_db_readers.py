"""Tests for concurrent SQLite read-only connections and db.reader()."""

import sqlite3
import threading
from pathlib import Path

import pytest

from trackseerr.library_paging import page_library
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    """Provides an isolated file-backed Database instance."""
    database = Database(tmp_path / "t.sqlite")
    yield database
    database.close()


def test_reader_sees_committed_writes(db: Database) -> None:
    """A reader sees committed writes from the primary connection."""
    db.set_kv("k", "v")
    with db.reader() as c:
        row = c.execute("SELECT value FROM kv_store WHERE key='k'").fetchone()
    assert row is not None
    assert row["value"] == "v"


def test_reader_is_read_only(db: Database) -> None:
    """An INSERT attempted through db.reader() raises sqlite3.OperationalError."""
    with db.reader() as c, pytest.raises(sqlite3.OperationalError):
        c.execute("INSERT INTO kv_store (key, value) VALUES ('k', 'v')")


def test_reader_does_not_take_write_lock(db: Database) -> None:
    """A reader in another thread does not block when db._lock is held."""
    result = None
    thread_error = None

    def worker() -> None:
        nonlocal result, thread_error
        try:
            with db.reader() as c:
                row = c.execute("SELECT 1").fetchone()
                result = row[0]
        except Exception as exc:  # noqa: BLE001
            thread_error = exc

    with db._lock:
        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=1.0)
        assert not t.is_alive(), "Worker thread stalled on db._lock"

    assert thread_error is None
    assert result == 1


def test_reader_per_thread(db: Database) -> None:
    """Each thread receives its own reader connection and reuses it."""
    conn_t1_first = None
    conn_t1_second = None
    conn_t2 = None

    def worker1() -> None:
        nonlocal conn_t1_first, conn_t1_second
        with db.reader() as c1:
            conn_t1_first = c1
        with db.reader() as c2:
            conn_t1_second = c2

    def worker2() -> None:
        nonlocal conn_t2
        with db.reader() as c:
            conn_t2 = c

    t1 = threading.Thread(target=worker1)
    t2 = threading.Thread(target=worker2)
    t1.start()
    t2.start()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert conn_t1_first is not None
    assert conn_t1_first is conn_t1_second
    assert conn_t2 is not None
    assert conn_t1_first is not conn_t2


def test_close_reopens_readers(db: Database) -> None:
    """db.close() closes reader connections and subsequent reader() calls open fresh ones."""
    db.set_kv("before", "1")
    first_conn = None
    with db.reader() as c:
        first_conn = c
        val = c.execute("SELECT value FROM kv_store WHERE key='before'").fetchone()[0]
        assert val == "1"

    db.close()

    # The old reader connection is closed
    with pytest.raises(sqlite3.ProgrammingError):
        first_conn.execute("SELECT 1")

    second_conn = None
    with db.reader() as c:
        second_conn = c
        val = c.execute("SELECT value FROM kv_store WHERE key='before'").fetchone()[0]
        assert val == "1"

    assert second_conn is not first_conn
    # Primary writer connection also reopens lazily via conn
    db.set_kv("after", "2")
    assert db.get_kv("after") == "2"


def test_memory_db_reader_falls_back_to_shared_connection() -> None:
    """In-memory database reader falls back to the shared connection under db._lock."""
    mem_db = Database(":memory:")
    try:
        mem_db.set_kv("mem_k", "mem_v")
        with mem_db.reader() as c:
            assert c is mem_db.conn
            val = c.execute("SELECT value FROM kv_store WHERE key='mem_k'").fetchone()[0]
            assert val == "mem_v"
    finally:
        mem_db.close()


def test_page_library_runs_while_writer_lock_held(db: Database) -> None:
    """get_library_stats and page_library run in a worker thread while db._lock is held."""
    db.upsert_library_artist({"id": "A1", "name": "Artist 1", "monitored": True})
    db.upsert_library_artist({"id": "A2", "name": "Artist 2", "monitored": False})
    db.upsert_library_artist({"id": "A3", "name": "Artist 3", "monitored": True})

    stats_result = None
    page_result = None
    worker_error = None

    def worker() -> None:
        nonlocal stats_result, page_result, worker_error
        try:
            stats_result = db.get_library_stats()
            items, total = page_library(db, "artists", page=1, page_size=10, sort_key="name", sort_dir="asc")
            page_result = (items, total)
        except Exception as exc:  # noqa: BLE001
            worker_error = exc

    with db._lock:
        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive(), "Worker thread timed out while writer lock was held"

    assert worker_error is None
    assert stats_result is not None
    assert stats_result["artist_count"] == 3
    assert page_result is not None
    assert page_result[1] == 3
    assert len(page_result[0]) == 3


def test_reader_closed_when_thread_ends(db: Database) -> None:
    """Reader connections are garbage-collected and pruned when their thread ends."""
    import gc

    def worker() -> None:
        with db.reader() as c:
            c.execute("SELECT 1").fetchone()

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    gc.collect()
    assert len(db._readers) == 0


def test_artist_profile_threads_do_not_leak_readers(db: Database) -> None:
    """ThreadPoolExecutor threads do not permanently leak reader connections."""
    import gc
    from concurrent.futures import ThreadPoolExecutor

    def worker() -> None:
        with db.reader() as c:
            c.execute("SELECT 1").fetchone()

    for _ in range(20):
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: worker(), range(2)))

    gc.collect()
    assert len(db._readers) <= 2


def test_close_does_not_raise_without_readers(tmp_path: Path) -> None:
    """Closing a database that never opened a reader succeeds without NameError."""
    database = Database(tmp_path / "unused.sqlite")
    database.close()


def test_reader_inside_write_lock_sees_uncommitted_rows(db: Database) -> None:
    """A reader called from a thread holding the write lock reads uncommitted rows via the writer connection."""
    with db._lock:
        db.conn.execute(
            "INSERT INTO library_artists (id, name, clean_name) VALUES (?, ?, ?)",
            ("uncommitted-artist-1", "Uncommitted Artist", "uncommitted artist"),
        )
        artist = db.get_library_artist("uncommitted-artist-1")
        assert artist is not None
        assert artist["id"] == "uncommitted-artist-1"
        assert artist["name"] == "Uncommitted Artist"
        db.conn.rollback()

    assert db.get_library_artist("uncommitted-artist-1") is None


def test_library_getters_do_not_wait_for_writer(db: Database) -> None:
    """Library getter and list methods run in a worker thread without waiting for a held write lock."""
    db.upsert_library_artist({"id": "artist-1", "name": "Artist 1", "monitored": True})
    db.upsert_library_album({
        "id": "album-1",
        "artist_id": "artist-1",
        "title": "Album 1",
        "monitored": True,
    })

    artist_result = None
    album_result = None
    albums_list_result = None
    worker_error = None

    def worker() -> None:
        nonlocal artist_result, album_result, albums_list_result, worker_error
        try:
            artist_result = db.get_library_artist("artist-1")
            albums_list_result = db.list_library_albums(artist_id="artist-1")
            album_result = db.get_library_album("album-1")
        except Exception as exc:  # noqa: BLE001
            worker_error = exc

    with db._lock:
        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive(), "Worker thread timed out waiting on db._lock"

    assert worker_error is None
    assert artist_result is not None
    assert artist_result["id"] == "artist-1"
    assert album_result is not None
    assert album_result["id"] == "album-1"
    assert albums_list_result is not None
    assert len(albums_list_result) == 1
    assert albums_list_result[0]["id"] == "album-1"



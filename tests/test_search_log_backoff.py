"""Tests for search log backoff statistics and pacing."""

from datetime import datetime, timedelta, timezone
import pytest

from trackseerr.backlog_worker import backoff_wait, is_backed_off
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


def _rec(db, **kw):
    base = dict(
        source_name="Prowlarr",
        source_type="torznab",
        query_artist="Artist",
        query_title="Track",
        result_count=2,
    )
    base.update(kw)
    db.record_search(**base)


def test_recent_search_stats_groups_by_item(test_db):
    for _ in range(3):
        _rec(test_db, track_id="t1")
    for _ in range(2):
        _rec(test_db, request_id="r1")
    # Ignored: error set
    _rec(test_db, track_id="t1", error="boom")
    # Ignored: rung = 2
    _rec(test_db, track_id="t1", rung=2)

    stats = test_db.recent_search_stats(days=14)
    assert set(stats.keys()) == {"t1", "r1"}
    assert stats["t1"][0] == 3
    assert isinstance(stats["t1"][1], str)
    assert stats["r1"][0] == 2
    assert isinstance(stats["r1"][1], str)


def test_recent_search_stats_keys_missing_trigger_ref(test_db):
    for _ in range(3):
        _rec(test_db, trigger_ref="missing:artist a|track a")
    for _ in range(2):
        _rec(test_db, trigger_ref="issue-abc")

    stats = test_db.recent_search_stats(days=14)
    assert set(stats.keys()) == {"missing:artist a|track a"}
    assert stats["missing:artist a|track a"][0] == 3
    assert isinstance(stats["missing:artist a|track a"][1], str)


def test_recent_search_stats_ignores_old_rows(test_db):
    _rec(test_db, track_id="t1")
    _rec(test_db, track_id="t2")
    # Update t2's created_at to 20 days ago via direct SQL UPDATE
    test_db.conn.execute("UPDATE search_log SET created_at = datetime('now', '-20 days') WHERE track_id = 't2'")
    test_db.conn.commit()

    stats = test_db.recent_search_stats(days=14)
    assert "t1" in stats
    assert "t2" not in stats
    assert stats["t1"][0] == 1


def test_backoff_wait_steps():
    assert backoff_wait(0) is None
    assert backoff_wait(7) is None
    assert backoff_wait(8) == timedelta(hours=6)
    assert backoff_wait(15) == timedelta(hours=6)
    assert backoff_wait(16) == timedelta(hours=12)
    assert backoff_wait(23) == timedelta(hours=12)
    assert backoff_wait(24) == timedelta(hours=24)
    assert backoff_wait(100) == timedelta(hours=24)


def test_is_backed_off():
    now = datetime.now(timezone.utc)
    # count 10, last 2 h ago -> wait is 6h, elapsed is 2h < 6h -> True
    last_2h = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    stats_2h = {"t1": (10, last_2h)}
    assert is_backed_off(stats_2h, "t1", now) is True

    # count 10, last 7 h ago -> wait is 6h, elapsed is 7h >= 6h -> False
    last_7h = (now - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")
    stats_7h = {"t1": (10, last_7h)}
    assert is_backed_off(stats_7h, "t1", now) is False

    # key missing -> False
    assert is_backed_off(stats_2h, "missing_key", now) is False

    # key None -> False
    assert is_backed_off(stats_2h, None, now) is False

    # Unparseable created_at -> False
    stats_bad = {"t1": (10, "not-a-timestamp")}
    assert is_backed_off(stats_bad, "t1", now) is False

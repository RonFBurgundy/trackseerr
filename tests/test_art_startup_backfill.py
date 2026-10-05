"""Startup art backfill: runs once per art pipeline version, resumes when interrupted, never in the gateway tier."""

import threading
from unittest.mock import patch

import pytest

from plex_playlist_sync import art_pipeline, art_thumbs
from plex_playlist_sync.cli import _start_local_workers
from plex_playlist_sync.config import Config
from tests.test_library_api import test_db  # noqa: F401


@pytest.fixture
def real_startup(real_art_pipeline):
    yield
    art_pipeline.stop_startup_backfill()


def _join():
    t = art_pipeline._startup_thread
    if t is not None:
        t.join(timeout=10)
        assert not t.is_alive()


STATS = {"scanned": 0, "generated": 0, "versioned": 0}


@pytest.mark.real_art_pipeline
def test_runs_once_sets_marker_and_does_not_run_again(test_db, real_startup):
    with patch.object(art_pipeline, "backfill", return_value=STATS) as bf:
        assert art_pipeline.backfill_needed(test_db)
        assert art_pipeline.start_startup_backfill(test_db) is not None
        _join()
        assert bf.call_count == 1
        assert test_db.get_kv(art_pipeline.BACKFILL_MARKER_KEY) == str(art_pipeline.ART_PIPELINE_VERSION)
        assert art_pipeline.start_startup_backfill(test_db) is None
        assert bf.call_count == 1


@pytest.mark.real_art_pipeline
def test_marker_for_older_pipeline_version_reruns(test_db, real_startup):
    test_db.set_kv(art_pipeline.BACKFILL_MARKER_KEY, "0")
    with patch.object(art_pipeline, "backfill", return_value=STATS) as bf:
        art_pipeline.start_startup_backfill(test_db)
        _join()
    assert bf.call_count == 1


@pytest.mark.real_art_pipeline
def test_interrupted_run_sets_no_marker_and_resumes_next_start(test_db, real_startup):
    def interrupted(db, should_stop=None, **kw):
        art_pipeline.stop_startup_backfill()  # shutdown arrives mid-pass
        assert should_stop()
        return dict(STATS)

    with patch.object(art_pipeline, "backfill", side_effect=interrupted):
        art_pipeline.start_startup_backfill(test_db)
        _join()
    assert test_db.get_kv(art_pipeline.BACKFILL_MARKER_KEY) is None

    with patch.object(art_pipeline, "backfill", return_value=STATS) as bf:
        art_pipeline.start_startup_backfill(test_db)  # next boot
        _join()
    assert bf.call_count == 1
    assert test_db.get_kv(art_pipeline.BACKFILL_MARKER_KEY) == str(art_pipeline.ART_PIPELINE_VERSION)


@pytest.mark.real_art_pipeline
def test_crashed_run_sets_no_marker(test_db, real_startup):
    with patch.object(art_pipeline, "backfill", side_effect=OSError("disk gone")):
        art_pipeline.start_startup_backfill(test_db)
        _join()
    assert art_pipeline.backfill_needed(test_db)


@pytest.mark.real_art_pipeline
def test_start_does_not_block_the_caller(test_db, real_startup):
    gate = threading.Event()
    with patch.object(art_pipeline, "backfill", side_effect=lambda *a, **k: gate.wait(10) and STATS):
        t = art_pipeline.start_startup_backfill(test_db)
        assert t is not None and t.is_alive()  # returned while the pass is still running
        assert art_pipeline.start_startup_backfill(test_db) is None  # no second concurrent run
        gate.set()
        _join()


def test_local_workers_start_backfill_and_gateway_tier_does_not(test_db, art_scheduler_calls):
    """_start_local_workers is the non-gateway boot path; the gateway branch of cli.main never calls it."""
    import inspect

    from plex_playlist_sync import cli

    src = inspect.getsource(cli._background_init)
    gateway_branch, _, rest = src.partition('if role == "gateway":')
    gw_body = rest.split("_start_local_workers", 1)[0]
    assert "_start_local_workers" in rest  # the call exists, and only after the gateway branch has returned
    assert "return" in gw_body
    assert "start_startup_backfill" not in src


def test_start_local_workers_invokes_backfill_for_core(test_db, art_scheduler_calls):
    cfg = Config.from_env()
    with patch("plex_playlist_sync.backlog_worker.backlog_worker"), patch(
        "plex_playlist_sync.backlog_worker.rss_worker"
    ), patch("plex_playlist_sync.artist_refresh_worker.artist_refresh_worker"), patch(
        "plex_playlist_sync.scrobble_worker.scrobble_worker"
    ), patch("plex_playlist_sync.mix_worker.mix_worker"), patch(
        "plex_playlist_sync.import_list_worker.import_list_worker"
    ):
        _start_local_workers(test_db, cfg)
    assert len(art_scheduler_calls.startup_backfill) == 1
    assert art_scheduler_calls.startup_backfill[0][0] == (test_db,)


def test_conftest_stubs_record_calls_and_start_no_threads(test_db, tmp_path, art_scheduler_calls):
    assert art_pipeline.schedule_precache(test_db, "a1") is None
    assert art_thumbs.schedule_pregenerate(tmp_path / "x.jpg", tmp_path) is False
    assert art_pipeline.start_startup_backfill(test_db) is None
    assert art_scheduler_calls.precache[0][0] == (test_db, "a1")
    assert art_scheduler_calls.pregenerate[0][0] == (tmp_path / "x.jpg", tmp_path)
    assert art_scheduler_calls.startup_backfill == [((test_db,), {})]
    assert art_pipeline._precache_executor is None or not art_pipeline._precache_pending
    assert test_db.get_kv(art_pipeline.BACKFILL_MARKER_KEY) is None

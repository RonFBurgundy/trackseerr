"""Background scheduler threads must not spin when ``time.sleep`` is patched, and tests must not leak them.

Regression for xdist workers growing to GBs and dying (cgroup OOM) in full runs: ``cli.main`` leaves the sync
scheduler / Lidarr trickle daemon threads running; a later test patching the process-global ``time.sleep`` with a
no-op made those loops hot, and each pass allocated (log records, mock call records).
"""

import threading
from unittest.mock import MagicMock, patch

from trackseerr import cli
from tests.lidarr_fake import FastClock


def _scheduler_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name in ("ScheduledSyncWorker", "ScheduledLidarrTrickleWorker")]


def test_scheduler_threads_do_not_spin_when_time_sleep_is_a_noop():
    config = MagicMock(wait_seconds=3600)
    with patch("time.sleep") as fake_sleep:
        cli._start_sync_scheduler(MagicMock(), config, MagicMock(), None)
        cli._start_lidarr_trickle(MagicMock(), config)
        threading.Event().wait(0.3)
        spun = fake_sleep.call_count
        cli._shutdown_event.set()
        try:
            for thread in _scheduler_threads():
                thread.join(timeout=5)
            assert _scheduler_threads() == [], "scheduler threads must exit promptly once shutdown is signalled"
        finally:
            cli._shutdown_event.clear()
    assert spun == 0, (
        f"scheduler threads called the patched time.sleep {spun} times in 0.3s; "
        f"live threads: {sorted(t.name for t in threading.enumerate())}"
    )


def test_conftest_stops_scheduler_threads_a_test_leaves_behind():
    """Leaves both threads running on purpose; the autouse fixture's teardown must reap them (checked next test)."""
    cli._start_sync_scheduler(MagicMock(), MagicMock(wait_seconds=3600), MagicMock(), None)
    cli._start_lidarr_trickle(MagicMock(), MagicMock())
    assert len(_scheduler_threads()) == 2


def test_no_scheduler_threads_survive_the_previous_test():
    assert _scheduler_threads() == []


def test_fast_clock_finishes_a_cooldown_loop_without_waiting():
    clock = FastClock()
    until = clock.time() + 60
    calls = 0
    while clock.time() < until:
        clock.sleep(1.0)
        calls += 1
    assert calls <= 61


def test_interval_workers_wait_on_their_stop_event_not_time_sleep():
    """Worker loops that sleep between ticks must use ``Event.wait``; a no-op ``time.sleep`` would make them hot."""
    from trackseerr.backlog_worker import RSSSyncWorker
    from trackseerr.pending_worker import PendingReleaseWorker

    workers = [PendingReleaseWorker(), RSSSyncWorker()]
    with patch("time.sleep") as fake_sleep, patch("trackseerr.pending_worker.release_due") as rd:
        rd.return_value = {"released": 0}
        for w in workers:
            w.start(MagicMock(), 3600)
        threading.Event().wait(0.3)
        spun = fake_sleep.call_count
        for w in workers:
            w.stop()
    assert spun == 0, f"worker loops called the patched time.sleep {spun} times in 0.3s"

"""Task manager backend: run history, editable schedules, registry completeness, activity/resources endpoints and the
event-triggered artwork backfill."""

import ast
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import trackseerr
from trackseerr import art_pipeline, process_stats, task_manager
from trackseerr.backlog_worker import RSSSyncWorker
from trackseerr.clients.lidarr import LidarrClient
from trackseerr.config import Config
from trackseerr.lidarr_migration import LidarrMigrationJob
from trackseerr.library_scanner import LibraryScanner
from trackseerr.storage import SCHEMA_VERSION, Database
from tests.test_system_status import (  # noqa: F401
    app_and_client,
    create_auth_cookies,
    secret_key,
    seeded_users,
    test_config,
    test_db,
)


def _cfg(**overrides) -> Config:
    return Config(plex_url="http://plex.invalid:32400", plex_token="t", **overrides)


def _iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "tm.db")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _reset_prune_throttle(monkeypatch):
    monkeypatch.setattr(task_manager, "_last_prune_monotonic", None)


# ----------------------------------------------------------------------------- migration


def test_migration_creates_task_runs_table_and_index(db):
    assert SCHEMA_VERSION >= 67
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(task_runs)").fetchall()}
    assert cols == {"id", "task_id", "trigger", "started_at", "finished_at", "status", "message", "duration_ms"}
    index_cols = [r[2] for r in db.conn.execute("PRAGMA index_info(idx_task_runs_task_started)").fetchall()]
    assert index_cols == ["task_id", "started_at"]
    assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION


# ----------------------------------------------------------------------------- run recording


def test_record_task_run_success(db):
    with task_manager.record_task_run(db, "indexer_rss_sync", "scheduled") as run:
        running = db.list_running_task_runs()
        assert len(running) == 1 and running[0]["task_id"] == "indexer_rss_sync" and running[0]["trigger"] == "scheduled"
        run.message = "scanned=3"
    row = db.list_task_runs("indexer_rss_sync", _iso(timedelta(days=-1)))[0]
    assert row["status"] == "success"
    assert row["message"] == "scanned=3"
    assert row["finished_at"] and row["duration_ms"] is not None and row["duration_ms"] >= 0
    assert db.list_running_task_runs() == []


def test_record_task_run_failure_logs_root_cause_and_reraises(db, caplog):
    with caplog.at_level("ERROR", logger=task_manager.logger.name):
        with pytest.raises(ValueError):
            with task_manager.record_task_run(db, "seed_cleanup", "manual"):
                raise ValueError("boom")
    row = db.list_task_runs("seed_cleanup", _iso(timedelta(days=-1)))[0]
    assert row["status"] == "failed"
    assert "ValueError" in row["message"]
    assert any("seed_cleanup" in r.getMessage() and "ValueError" in r.getMessage() for r in caplog.records)


def test_record_task_run_cancelled_by_exception_and_by_flag(db):
    with task_manager.record_task_run(db, "filesystem_scan", "manual"):
        raise task_manager.TaskCancelled()
    with task_manager.record_task_run(db, "filesystem_scan", "manual") as run:
        run.cancelled = True
    statuses = [r["status"] for r in db.list_task_runs("filesystem_scan", _iso(timedelta(days=-1)))]
    assert statuses == ["cancelled", "cancelled"]


def test_record_task_run_discard_removes_row_and_failed_flag(db):
    with task_manager.record_task_run(db, "playlist_sync", "scheduled") as run:
        run.apply_result({"status": "already_running"})
    assert db.list_task_runs("playlist_sync", _iso(timedelta(days=-1))) == []
    with task_manager.record_task_run(db, "playlist_sync", "scheduled") as run:
        run.failed = "scan failed"
    assert db.list_task_runs("playlist_sync", _iso(timedelta(days=-1)))[0]["status"] == "failed"


def test_recording_failure_never_breaks_the_work():
    broken = MagicMock()
    broken.start_task_run.side_effect = sqlite3.OperationalError("disk full")
    broken.finish_task_run.side_effect = sqlite3.OperationalError("disk full")
    ran = []
    with task_manager.record_task_run(broken, "seed_cleanup", "scheduled"):
        ran.append(True)
    assert ran == [True]


def test_record_finished_run_writes_a_complete_row(db):
    task_manager.record_finished_run(db, "download_queue_monitor", "scheduled", time.monotonic() - 0.25, "polled=2")
    row = db.list_task_runs("download_queue_monitor", _iso(timedelta(days=-1)))[0]
    assert row["status"] == "success" and row["message"] == "polled=2"
    assert row["duration_ms"] >= 250


def test_startup_housekeeping_fails_leftover_running_rows(db):
    leftover = db.start_task_run("library_health", "scheduled", _iso(timedelta(minutes=-5)))
    done = db.start_task_run("seed_cleanup", "scheduled", _iso(timedelta(minutes=-9)))
    db.finish_task_run(done, "success", _iso(timedelta(minutes=-8)))
    assert task_manager.startup_housekeeping(db) == 1
    row = db.conn.execute("SELECT * FROM task_runs WHERE id = ?", (leftover,)).fetchone()
    assert row["status"] == "failed" and row["message"] == "interrupted by restart" and row["finished_at"]
    assert db.conn.execute("SELECT status FROM task_runs WHERE id = ?", (done,)).fetchone()["status"] == "success"


def test_prune_removes_rows_older_than_seven_days_and_is_throttled(db):
    old = db.start_task_run("seed_cleanup", "scheduled", _iso(timedelta(days=-8)))
    db.finish_task_run(old, "success", _iso(timedelta(days=-8)))
    fresh = db.start_task_run("seed_cleanup", "scheduled", _iso(timedelta(days=-6)))
    db.finish_task_run(fresh, "success", _iso(timedelta(days=-6)))
    assert task_manager.maybe_prune(db, force=True) == 1
    ids = [r["id"] for r in db.list_task_runs("seed_cleanup", _iso(timedelta(days=-30)))]
    assert ids == [fresh]

    spy = MagicMock(wraps=db.prune_task_runs)
    with patch.object(db, "prune_task_runs", spy):
        # Recording a run triggers the throttled prune, but only once per day per process.
        with task_manager.record_task_run(db, "seed_cleanup", "manual"):
            pass
        with task_manager.record_task_run(db, "seed_cleanup", "manual"):
            pass
    assert spy.call_count == 0  # force=True above already used today's slot


# ----------------------------------------------------------------------------- effective interval


def test_effective_interval_precedence(db):
    cfg = _cfg(wait_seconds=7200, rss_sync_interval_minutes=30)
    # code default only (no config)
    assert task_manager.effective_interval_seconds(db, None, "seed_cleanup") == 86400
    # config/env beats the code default
    assert task_manager.effective_interval_seconds(db, cfg, "playlist_sync") == 7200
    assert task_manager.effective_interval_seconds(db, cfg, "indexer_rss_sync") == 1800
    # DB override beats config
    task_manager.set_interval_override(db, "playlist_sync", 3600)
    assert task_manager.effective_interval_seconds(db, cfg, "playlist_sync") == 3600
    assert task_manager.default_interval_seconds(db, cfg, "playlist_sync") == 7200
    # clearing returns to config
    task_manager.set_interval_override(db, "playlist_sync", None)
    assert task_manager.effective_interval_seconds(db, cfg, "playlist_sync") == 7200


def test_lidarr_trickle_interval_reads_lidarr_settings_before_env(db):
    cfg = _cfg(lidarr_auto_trickle_interval_minutes=45)
    # The Lidarr settings row (default 30 minutes) is what the auto-trickle loop has always honoured over the env value.
    db.update_lidarr_settings({"auto_trickle_interval_minutes": 120})
    assert task_manager.effective_interval_seconds(db, cfg, "lidarr_auto_trickle") == 7200
    task_manager.set_interval_override(db, "lidarr_auto_trickle", 900)
    assert task_manager.effective_interval_seconds(db, cfg, "lidarr_auto_trickle") == 900
    assert task_manager.default_interval_seconds(db, cfg, "lidarr_auto_trickle") == 7200


def test_non_interval_tasks_have_no_interval(db):
    for task_id in ("filesystem_scan", "download_queue_monitor", "scrobble_sync"):
        assert task_manager.effective_interval_seconds(db, None, task_id) is None


def test_presets_respect_per_task_minimums():
    assert min(task_manager.TASKS["indexer_rss_sync"].presets) == 15 * 60
    assert min(task_manager.TASKS["artist_metadata_refresh"].presets) == 6 * 3600
    for spec in task_manager.TASKS.values():
        assert set(spec.presets) <= set(task_manager.ALL_PRESETS)
        assert (spec.kind == task_manager.KIND_INTERVAL) == (spec.default_interval_seconds is not None)


def test_wait_for_next_cycle_picks_up_a_shorter_interval():
    stop = threading.Event()
    current = [3600.0]
    threading.Timer(0.1, lambda: current.__setitem__(0, 0.2)).start()
    started = time.monotonic()
    assert task_manager.wait_for_next_cycle(stop, lambda: current[0], step=0.05) is False
    assert time.monotonic() - started < 1.5


def test_wait_for_next_cycle_extends_for_a_longer_interval_and_stops():
    stop = threading.Event()
    current = [0.2]
    threading.Timer(0.05, lambda: current.__setitem__(0, 3600.0)).start()
    threading.Timer(0.4, stop.set).start()
    assert task_manager.wait_for_next_cycle(stop, lambda: current[0], step=0.05) is True


def test_rss_worker_picks_up_a_changed_interval_without_restart(db):
    worker = RSSSyncWorker()
    polls: list[float] = []
    interval = [3600.0]
    second = threading.Event()

    def poll(db):
        polls.append(time.monotonic())
        if len(polls) == 2:
            second.set()
        return {"releases_scanned": 0}

    with patch.object(worker, "poll_once", side_effect=poll):
        worker.start(db=db, interval_seconds=3600, interval_fn=lambda: interval[0])
        try:
            time.sleep(0.2)
            assert len(polls) == 1
            interval[0] = 0.5  # what PUT /schedule does, via the DB override
            assert second.wait(3.0), "worker kept sleeping on the old interval"
        finally:
            worker.stop()
    rows = db.list_task_runs("indexer_rss_sync", _iso(timedelta(days=-1)))
    assert [r["trigger"] for r in reversed(rows)][:2] == ["startup", "scheduled"]
    assert all(r["status"] == "success" for r in rows)


# ----------------------------------------------------------------------------- next_run_at and the task list


def test_next_run_at_is_last_finish_plus_interval():
    spec = task_manager.TASKS["indexer_rss_sync"]
    finished = "2026-01-01T10:00:00+00:00"
    assert task_manager.next_run_at(spec, 900, finished) == "2026-01-01T10:15:00+00:00"
    assert task_manager.next_run_at(spec, 900, None) is None
    assert task_manager.next_run_at(task_manager.TASKS["download_queue_monitor"], None, finished) is None
    assert task_manager.next_run_at(task_manager.TASKS["filesystem_scan"], None, finished) is None


def test_task_list_exposes_structured_fields(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    cookies = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    rid = test_db.start_task_run("indexer_rss_sync", "scheduled", _iso(timedelta(minutes=-10)))
    finished_at = _iso(timedelta(minutes=-5))
    test_db.finish_task_run(rid, "failed", finished_at, "indexer down", 1234)
    running = test_db.start_task_run("seed_cleanup", "manual", _iso(timedelta(seconds=-30)))

    resp = client.get("/api/system/tasks", cookies=cookies)
    assert resp.status_code == 200
    tasks = {t["id"]: t for t in resp.json()}
    assert set(tasks) >= set(task_manager.TASKS) - {"lidarr_request_retry", "lidarr_migration"}  # Lidarr-mode only / hidden until it has run

    rss = tasks["indexer_rss_sync"]
    assert rss["schedule_kind"] == "interval" and rss["editable"] is True
    assert rss["interval_seconds"] == 900 and rss["default_interval_seconds"] == 900
    assert rss["interval_presets"] == [900, 1800, 3600, 7200, 21600]
    assert rss["last_run_status"] == "failed" and rss["last_duration_ms"] == 1234
    assert rss["last_run_at"] == finished_at
    expected_next = (datetime.fromisoformat(finished_at) + timedelta(seconds=900)).isoformat()
    assert rss["next_run_at"] == expected_next
    assert rss["current_run_started_at"] is None and rss["progress"] is None

    seed = tasks["seed_cleanup"]
    assert seed["status"] == "running" and seed["current_run_started_at"] is not None and seed["next_run_at"] is None

    art = tasks["art_thumbnail_backfill"]
    assert art["schedule_kind"] == "interval" and art["interval_seconds"] == 86400 and art["editable"] is True

    scan = tasks["filesystem_scan"]
    assert scan["schedule_kind"] == "manual" and scan["interval_seconds"] is None and scan["interval_presets"] == []
    assert scan["editable"] is False and scan["next_run_at"] is None
    for kind_task in ("download_queue_monitor", "pending_releases", "import_list_sync", "scrobble_sync"):
        assert tasks[kind_task]["schedule_kind"] == "continuous" and tasks[kind_task]["editable"] is False
    assert tasks["pending_releases"]["can_trigger"] is False and tasks["filesystem_scan"]["can_trigger"] is True
    assert running  # row existed


# ----------------------------------------------------------------------------- schedule endpoint


def _put(client, cookies, task_id, value):
    return client.put(f"/api/system/tasks/{task_id}/schedule", json={"interval_seconds": value}, cookies=cookies)


def test_schedule_put_accepts_preset_then_resets(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    cookies = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    resp = _put(client, cookies, "indexer_rss_sync", 3600)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "indexer_rss_sync" and body["interval_seconds"] == 3600 and body["interval"] == "Every 1h"
    assert body["default_interval_seconds"] == 900
    assert test_db.get_kv(task_manager.override_key("indexer_rss_sync")) == "3600"

    reset = _put(client, cookies, "indexer_rss_sync", None)
    assert reset.status_code == 200 and reset.json()["interval_seconds"] == 900
    assert test_db.get_kv(task_manager.override_key("indexer_rss_sync")) is None


def test_schedule_put_rejections(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    admin = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    alice = create_auth_cookies(test_db, seeded_users["alice"], secret_key)
    assert _put(client, admin, "indexer_rss_sync", 1234).status_code == 400  # not a preset
    assert _put(client, admin, "indexer_rss_sync", 300).status_code == 400  # preset overall, below this task's minimum
    assert _put(client, admin, "no_such_task", 3600).status_code == 404
    assert _put(client, admin, "filesystem_scan", 3600).status_code == 400  # manual task: not editable
    assert _put(client, admin, "download_queue_monitor", 3600).status_code == 400  # continuous: not editable
    assert _put(client, alice, "indexer_rss_sync", 3600).status_code == 403
    assert test_db.get_kv(task_manager.override_key("indexer_rss_sync")) is None


# ----------------------------------------------------------------------------- history / activity / resources


def test_task_runs_endpoint_orders_filters_and_limits(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    admin = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    alice = create_auth_cookies(test_db, seeded_users["alice"], secret_key)
    for days_ago in (5, 3, 1):
        rid = test_db.start_task_run("seed_cleanup", "scheduled", _iso(timedelta(days=-days_ago)))
        test_db.finish_task_run(rid, "success", _iso(timedelta(days=-days_ago)), f"d{days_ago}", 10)
    other = test_db.start_task_run("library_health", "manual", _iso(timedelta(days=-1)))
    test_db.finish_task_run(other, "success", _iso(timedelta(days=-1)))

    resp = client.get("/api/system/tasks/seed_cleanup/runs", cookies=admin)
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["message"] for r in rows] == ["d1", "d3", "d5"]
    assert set(rows[0]) == {"id", "task_id", "trigger", "started_at", "finished_at", "status", "message", "duration_ms"}
    assert [r["message"] for r in client.get("/api/system/tasks/seed_cleanup/runs?days=4", cookies=admin).json()] == ["d1", "d3"]
    assert len(client.get("/api/system/tasks/seed_cleanup/runs?limit=1", cookies=admin).json()) == 1
    assert client.get("/api/system/tasks/nope/runs", cookies=admin).status_code == 404
    assert client.get("/api/system/tasks/seed_cleanup/runs", cookies=alice).status_code == 403


def test_activity_endpoint_shape_and_admin_only(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    admin = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    alice = create_auth_cookies(test_db, seeded_users["alice"], secret_key)
    for i in range(7):
        rid = test_db.start_task_run("seed_cleanup", "scheduled", _iso(timedelta(minutes=-60 + i)))
        test_db.finish_task_run(rid, "success" if i % 2 == 0 else "failed", _iso(timedelta(minutes=-50 + i)))
    test_db.start_task_run("indexer_rss_sync", "manual", _iso(timedelta(seconds=-5)))

    assert client.get("/api/system/activity", cookies=alice).status_code == 403
    resp = client.get("/api/system/activity", cookies=admin)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"running", "recent"}
    assert len(body["running"]) == 1
    running = body["running"][0]
    assert running["task_id"] == "indexer_rss_sync" and running["name"] == "Torznab / Newznab RSS Sync"
    assert running["started_at"] and set(running) == {"task_id", "name", "started_at", "progress"}
    assert len(body["recent"]) == 5
    assert set(body["recent"][0]) == {"task_id", "name", "status", "finished_at"}
    assert body["recent"][0]["name"] == "Seed Cleanup"
    finished = [r["finished_at"] for r in body["recent"]]
    assert finished == sorted(finished, reverse=True)


def test_activity_makes_no_external_or_heavy_status_calls(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    admin = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    with patch("trackseerr.api.routes.system.tasks.get_all_scheduled_tasks") as heavy, patch(
        "trackseerr.api.routes.system.tasks.seed_cleanup.get_status"
    ) as sc:
        assert client.get("/api/system/activity", cookies=admin).status_code == 200
    heavy.assert_not_called()
    sc.assert_not_called()


def test_resources_endpoint_shape(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    admin = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    alice = create_auth_cookies(test_db, seeded_users["alice"], secret_key)
    assert client.get("/api/system/resources", cookies=alice).status_code == 403
    resp = client.get("/api/system/resources", cookies=admin)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"cpu_percent", "rss_bytes", "thread_count", "uptime_seconds"}
    if Path("/proc/self/status").exists():
        assert body["rss_bytes"] > 0 and body["thread_count"] >= 1 and body["uptime_seconds"] >= 0


def test_process_stats_returns_nulls_without_proc(monkeypatch):
    monkeypatch.setattr(process_stats, "_PROC_STATUS", "/nonexistent/status")
    monkeypatch.setattr(process_stats, "_PROC_STAT", "/nonexistent/stat")
    snap = process_stats.sample()
    assert snap["rss_bytes"] is None and snap["thread_count"] is None
    assert snap["uptime_seconds"] is not None  # falls back to time since import


def test_process_stats_cpu_is_a_delta_since_previous_sample(monkeypatch):
    clock = iter([100.0, 102.0])
    cpu = iter([10.0, 11.0])
    monkeypatch.setattr(process_stats.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(process_stats, "_cpu_seconds", lambda: next(cpu))
    monkeypatch.setattr(process_stats, "_last_sample", (96.0, 9.0))
    assert process_stats.sample()["cpu_percent"] == 25.0  # 1 cpu-second over 4 wall-seconds
    assert process_stats.sample()["cpu_percent"] == 50.0  # 1 cpu-second over 2 wall-seconds


# ----------------------------------------------------------------------------- manual run is recorded


def test_manual_run_endpoint_records_a_manual_run(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    admin = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    with patch("trackseerr.backlog_worker.backlog_worker.poll_once", return_value={"items_checked": 4}):
        assert client.post("/api/system/tasks/wanted_backlog_sweep/run", cookies=admin).status_code == 200
        deadline = time.monotonic() + 5
        rows = []
        while time.monotonic() < deadline:
            rows = test_db.list_task_runs("wanted_backlog_sweep", _iso(timedelta(days=-1)))
            if rows and rows[0]["status"] != "running":
                break
            time.sleep(0.05)
    assert rows and rows[0]["trigger"] == "manual" and rows[0]["status"] == "success"
    assert rows[0]["message"] == "items_checked=4"


def test_manual_run_records_failure(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    admin = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    with patch("trackseerr.backlog_worker.rss_worker.poll_once", side_effect=RuntimeError("feed exploded")):
        assert client.post("/api/system/tasks/indexer_rss_sync/run", cookies=admin).status_code == 200
        deadline = time.monotonic() + 5
        rows = []
        while time.monotonic() < deadline:
            rows = test_db.list_task_runs("indexer_rss_sync", _iso(timedelta(days=-1)))
            if rows and rows[0]["status"] != "running":
                break
            time.sleep(0.05)
    assert rows[0]["status"] == "failed" and "RuntimeError" in rows[0]["message"]


def test_continuous_task_cannot_be_run_manually(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    admin = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    assert client.post("/api/system/tasks/pending_releases/run", cookies=admin).status_code == 400
    assert client.post("/api/system/tasks/import_list_sync/run", cookies=admin).status_code == 400


def test_task_list_can_trigger_flags(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    cookies = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    resp = client.get("/api/system/tasks", cookies=cookies)
    assert resp.status_code == 200
    tasks = {t["id"]: t for t in resp.json()}
    assert tasks["scrobble_sync"]["can_trigger"] is True
    assert tasks["mix_generation"]["can_trigger"] is True
    assert tasks["pending_releases"]["can_trigger"] is False
    assert tasks["import_list_sync"]["can_trigger"] is False


def test_manual_run_scrobble_sync(app_and_client, seeded_users, secret_key, test_db):
    from trackseerr.scrobble_worker import scrobble_worker

    _, client = app_and_client
    admin = create_auth_cookies(test_db, seeded_users["admin"], secret_key)

    with patch.object(scrobble_worker, "_get_plex", return_value=None), \
         patch.object(scrobble_worker, "run_iteration", return_value={"ingested": 3, "retried": 1}) as mock_iter:
        resp = client.post("/api/system/tasks/scrobble_sync/run", cookies=admin)
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        deadline = time.monotonic() + 5
        rows = []
        while time.monotonic() < deadline:
            rows = test_db.list_task_runs("scrobble_sync", _iso(timedelta(days=-1)))
            if rows and rows[0]["status"] != "running":
                break
            time.sleep(0.05)

    assert rows and rows[0]["trigger"] == "manual" and rows[0]["status"] == "success"
    assert rows[0]["message"] == "ingested=3, retried=1"
    mock_iter.assert_called_once()
    assert mock_iter.call_args.kwargs.get("force") is True


def test_manual_run_mix_generation_regenerates_non_due(app_and_client, seeded_users, secret_key, test_db, test_config):
    from trackseerr.mix_worker import mix_worker

    _, client = app_and_client
    admin = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    uid = seeded_users["admin"]["id"]

    mix_row = test_db.create_mix_config(
        user_id=uid,
        mix_type="daily_blend",
        name="Test Daily Blend",
    )
    test_db.record_mix_result(mix_row["id"], "{}")

    # 1. Normal non-forced run_iteration skips the non-due mix
    with patch("trackseerr.mix_worker.generate_and_sync") as mock_gen:
        outcome = mix_worker.run_iteration(test_db, test_config, plex_client=None, discovery=MagicMock(), force=False)
        assert outcome["due"] == 0
        assert outcome["generated"] == 0
        mock_gen.assert_not_called()

    # 2. Manual run via API regenerates the non-due mix (force=True)
    with patch.object(mix_worker, "_get_plex", return_value=None), \
         patch("trackseerr.mix_worker.generate_and_sync") as mock_gen:
        resp = client.post("/api/system/tasks/mix_generation/run", cookies=admin)
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        deadline = time.monotonic() + 5
        rows = []
        while time.monotonic() < deadline:
            rows = test_db.list_task_runs("mix_generation", _iso(timedelta(days=-1)))
            if rows and rows[0]["status"] != "running":
                break
            time.sleep(0.05)

        mock_gen.assert_called_once()
        assert mock_gen.call_args[0][3]["id"] == mix_row["id"]

    assert rows and rows[0]["trigger"] == "manual" and rows[0]["status"] == "success"
    assert rows[0]["message"] == "due=1, generated=1, errors=0"


def test_manual_run_non_admin_forbidden(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    alice = create_auth_cookies(test_db, seeded_users["alice"], secret_key)
    assert client.post("/api/system/tasks/scrobble_sync/run", cookies=alice).status_code == 403
    assert client.post("/api/system/tasks/mix_generation/run", cookies=alice).status_code == 403
    assert client.post("/api/system/tasks/scrobble_sync/run").status_code == 401
    assert client.post("/api/system/tasks/mix_generation/run").status_code == 401


def test_worker_iteration_locks_serialize(test_db, test_config):
    from trackseerr.scrobble_worker import ScrobbleWorker
    from trackseerr.mix_worker import MixWorker

    sw = ScrobbleWorker()
    mw = MixWorker()

    with patch.object(sw, "run_iteration", return_value={"ingested": 0, "retried": 0}), \
         patch.object(sw, "_get_plex", return_value=None):
        with sw._iteration_lock:
            t_sw = threading.Thread(target=sw.run_now, args=(test_db, test_config))
            t_sw.start()
            time.sleep(0.05)
            assert t_sw.is_alive()
        t_sw.join(timeout=2.0)
        assert not t_sw.is_alive()

    with patch.object(mw, "run_iteration", return_value={"due": 0, "generated": 0, "errors": 0}), \
         patch.object(mw, "_get_plex", return_value=None):
        with mw._iteration_lock:
            t_mw = threading.Thread(target=mw.run_now, args=(test_db, test_config))
            t_mw.start()
            time.sleep(0.05)
            assert t_mw.is_alive()
        t_mw.join(timeout=2.0)
        assert not t_mw.is_alive()


def test_mix_worker_run_now_uses_shared_discovery_if_none(test_db, test_config):
    from trackseerr.mix_worker import MixWorker

    worker = MixWorker()
    assert worker._discovery is None
    with patch.object(worker, "_get_plex", return_value=None), \
         patch("trackseerr.mix_worker.get_shared_discovery_client") as mock_shared, \
         patch.object(worker, "run_iteration", return_value={"due": 0, "generated": 0, "errors": 0}):
        worker.run_now(test_db, test_config)
        mock_shared.assert_called_once_with(test_db)
        assert worker._discovery is mock_shared.return_value


# ----------------------------------------------------------------------------- scheduled paths record


def test_seed_cleanup_sweep_records_trigger(db):
    from trackseerr import seed_cleanup

    seed_cleanup.run_sweep(db, trigger="scheduled")
    seed_cleanup.run_sweep(db)
    triggers = [r["trigger"] for r in db.list_task_runs("seed_cleanup", _iso(timedelta(days=-1)))]
    assert sorted(triggers) == ["manual", "scheduled"]


def test_recycle_bin_failure_is_recorded_and_still_handled(db):
    from trackseerr import recycle_bin

    with patch.object(recycle_bin, "run_cleanup", side_effect=OSError("bin unreadable")):
        assert recycle_bin.RecycleBinWorker().run_once(db) is False  # the worker's own handling is unchanged
    row = db.list_task_runs("recycle_bin_cleanup", _iso(timedelta(days=-1)))[0]
    assert row["status"] == "failed" and row["trigger"] == "scheduled"


def test_library_health_due_check_uses_the_given_interval(db):
    from trackseerr import library_health

    db.set_library_health_weekly(True) if hasattr(db, "set_library_health_weekly") else None
    run_id = db.conn.execute(
        "INSERT INTO library_health_runs (started_at, finished_at) VALUES (?, ?)",
        (_iso(timedelta(hours=-30)), _iso(timedelta(hours=-30))),
    ).lastrowid
    assert run_id
    db.conn.commit()
    assert library_health.weekly_due(db, interval_seconds=86400) is True
    assert library_health.weekly_due(db, interval_seconds=7 * 86400) is False


# ----------------------------------------------------------------------------- registry completeness


def _constant_thread_names() -> set[str]:
    names: set[str] = set()
    root = Path(trackseerr.__file__).parent
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "_run_manual" and len(node.args) >= 3:
                third = node.args[2]  # the manual-run helper names its thread from this argument
                if isinstance(third, ast.Constant) and isinstance(third.value, str):
                    names.add(third.value)
                continue
            is_thread = (isinstance(func, ast.Attribute) and func.attr == "Thread") or (
                isinstance(func, ast.Name) and func.id == "Thread"
            )
            if not is_thread:
                continue
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    names.add(kw.value.value)
    return names


def test_every_worker_thread_maps_to_a_registered_task():
    found = _constant_thread_names()
    known = set(task_manager.WORKER_THREAD_TASKS) | set(task_manager.NON_TASK_THREADS)
    unmapped = sorted(found - known)
    assert not unmapped, (
        f"Background threads without a task registry entry: {unmapped}. Add each to TASKS and "
        "WORKER_THREAD_TASKS (or to NON_TASK_THREADS with the reason it is not a task) in task_manager.py."
    )
    assert set(task_manager.WORKER_THREAD_TASKS.values()) <= set(task_manager.TASKS)
    stale = sorted(known - found)
    assert not stale, f"Mapped thread names that no longer exist in the code: {stale}"
    assert all(reason.strip() for reason in task_manager.NON_TASK_THREADS.values())


def test_every_registered_task_is_listed_by_the_api(db):
    from trackseerr.api.routes.system.tasks import get_all_scheduled_tasks

    db.update_media_management_settings({"library_mode": "lidarr"})
    listed = {t.id for t in get_all_scheduled_tasks(db, _cfg())}
    assert listed == set(task_manager.TASKS)


# ----------------------------------------------------------------------------- art backfill auto-run


STATS = {"scanned": 5, "generated": 2, "versioned": 1}


@pytest.fixture
def real_backfill(real_art_pipeline, monkeypatch):
    monkeypatch.setattr(art_pipeline, "_event_last_started", None)
    monkeypatch.setattr(art_pipeline, "_event_rerun", False)
    yield


@pytest.mark.real_art_pipeline
def test_event_backfill_runs_once_with_event_trigger_and_is_debounced(db, real_backfill):
    with patch.object(art_pipeline, "backfill", return_value=STATS) as bf:
        thread = art_pipeline.request_backfill_after_event(db, "library scan")
        assert thread is not None
        thread.join(timeout=5)
        assert art_pipeline.request_backfill_after_event(db, "Lidarr library import") is None  # within the window
    assert bf.call_count == 1
    rows = db.list_task_runs("art_thumbnail_backfill", _iso(timedelta(days=-1)))
    assert len(rows) == 1 and rows[0]["trigger"] == "event" and rows[0]["status"] == "success"
    assert rows[0]["message"] == "scanned 5, generated 2, versioned 1"


@pytest.mark.real_art_pipeline
def test_event_during_a_running_backfill_never_overlaps_and_queues_one_more_pass(db, real_backfill):
    inside = threading.Event()
    release = threading.Event()
    active = []
    max_active = []

    def slow_backfill(db, should_stop=None, **kw):
        active.append(1)
        max_active.append(len(active))
        inside.set()
        release.wait(5)
        active.pop()
        return dict(STATS)

    with patch.object(art_pipeline, "backfill", side_effect=slow_backfill) as bf:
        first = art_pipeline.request_backfill_after_event(db, "library scan")
        assert inside.wait(5)
        # A scheduled/manual run and a second event while one is in flight must not start a concurrent pass.
        assert art_pipeline.run_backfill_task(db, "manual") is None
        assert art_pipeline.request_backfill_after_event(db, "Lidarr library import") is None
        release.set()
        first.join(timeout=5)
    assert bf.call_count == 2  # the original pass plus exactly one rerun for the event that arrived meanwhile
    assert max(max_active) == 1
    triggers = sorted(r["trigger"] for r in db.list_task_runs("art_thumbnail_backfill", _iso(timedelta(days=-1))))
    assert triggers == ["event", "event"]


@pytest.mark.real_art_pipeline
def test_backfill_failure_is_recorded_and_releases_the_guard(db, real_backfill):
    with patch.object(art_pipeline, "backfill", side_effect=OSError("disk gone")):
        with pytest.raises(OSError):
            art_pipeline.run_backfill_task(db, "scheduled")
    row = db.list_task_runs("art_thumbnail_backfill", _iso(timedelta(days=-1)))[0]
    assert row["status"] == "failed"
    with patch.object(art_pipeline, "backfill", return_value=STATS):
        assert art_pipeline.run_backfill_task(db, "scheduled") == STATS


@pytest.mark.real_art_pipeline
def test_scheduler_runs_only_when_the_interval_has_passed(db, real_backfill):
    scheduler = art_pipeline.ArtBackfillScheduler()
    ran = threading.Event()

    def fake_backfill(db, should_stop=None, **kw):
        ran.set()
        return dict(STATS)

    # Just finished 10 minutes ago with a 24h interval: not due.
    rid = db.start_task_run("art_thumbnail_backfill", "scheduled", _iso(timedelta(minutes=-11)))
    db.finish_task_run(rid, "success", _iso(timedelta(minutes=-10)))
    with patch.object(art_pipeline, "backfill", side_effect=fake_backfill):
        scheduler.start(db, lambda: 86400.0, initial_delay=0.0, poll_seconds=0.05)
        assert not ran.wait(0.4)
        # The admin drops the schedule to 5 minutes: it is overdue now and runs without a restart.
        scheduler.stop()
        scheduler.start(db, lambda: 300.0, initial_delay=0.0, poll_seconds=0.05)
        assert ran.wait(3.0)
        scheduler.stop()
    assert db.list_task_runs("art_thumbnail_backfill", _iso(timedelta(minutes=-1)))[0]["trigger"] == "scheduled"


def test_scan_completion_requests_backfill_but_cancel_and_failure_do_not(db, art_scheduler_calls):
    scanner = LibraryScanner()
    with patch.object(scanner, "scan", return_value={"status": "completed"}):
        scanner._run_background_scan(db, None, False, None)
    assert art_scheduler_calls.event_backfill == ["library scan"]
    with patch.object(scanner, "scan", return_value={"status": "cancelled"}):
        scanner._run_background_scan(db, None, False, None)
    with patch.object(scanner, "scan", return_value={"status": "failed", "error": "bad"}):
        scanner._run_background_scan(db, None, False, None)
    assert art_scheduler_calls.event_backfill == ["library scan"]
    rows = db.list_task_runs("filesystem_scan", _iso(timedelta(days=-1)))
    assert sorted(r["status"] for r in rows) == ["cancelled", "failed", "success"]
    assert {r["trigger"] for r in rows} == {"manual"}


def test_lidarr_import_completion_requests_backfill(db, art_scheduler_calls):
    client = MagicMock(spec=LidarrClient)
    client.get_all_artists.return_value = [
        {"id": 1, "artistName": "A", "foreignArtistId": "m1", "path": "/music/A", "monitored": True}
    ]
    client.get_all_albums.return_value = []
    client.get_all_tracks.return_value = []
    client.get_all_track_files.return_value = []
    res = LidarrMigrationJob().run_migration(db, client, auto_switch_mode=False)
    assert res["status"] == "completed"
    assert art_scheduler_calls.event_backfill == ["Lidarr library import"]


def test_boot_starts_the_art_scheduler_and_closes_interrupted_runs(db, art_scheduler_calls):
    from trackseerr.cli import _start_local_workers

    db.start_task_run("library_health", "scheduled", _iso(timedelta(minutes=-3)))
    cfg = _cfg(enable_backlog_search=False, enable_rss_sync=False, enable_import_lists=False)
    with patch("trackseerr.pending_worker.pending_worker.start"), patch(
        "trackseerr.artist_refresh_worker.artist_refresh_worker.start"
    ), patch("trackseerr.scrobble_worker.scrobble_worker.start"), patch(
        "trackseerr.mix_worker.mix_worker.start"
    ), patch("trackseerr.library_health.library_health_worker.start"), patch(
        "trackseerr.seed_cleanup.seed_cleanup_worker.start"
    ), patch("trackseerr.recycle_bin.recycle_bin_worker.start"):
        _start_local_workers(db, cfg)
    assert len(art_scheduler_calls.scheduler_start) == 1
    assert db.list_running_task_runs() == []
    assert db.get_last_finished_task_run("library_health")["message"] == "interrupted by restart"


# ----------------------------------------------------------------------------- WAIT_SECONDS=0


def test_playlist_sync_is_manual_when_wait_seconds_is_zero(db):
    from trackseerr.api.routes.system.tasks import get_all_scheduled_tasks

    scheduled = {t.id: t for t in get_all_scheduled_tasks(db, _cfg(wait_seconds=86400))}["playlist_sync"]
    assert scheduled.schedule_kind == "interval" and scheduled.editable and scheduled.interval_seconds == 86400

    off = {t.id: t for t in get_all_scheduled_tasks(db, _cfg(wait_seconds=0))}["playlist_sync"]
    assert off.schedule_kind == "manual"
    assert off.editable is False
    assert off.next_run_at is None
    assert off.interval_seconds is None
    assert off.interval_presets == []
    assert off.interval == "Manual / On Demand"
    assert off.can_trigger is True  # an admin can still run it by hand


def test_schedule_edit_rejected_for_playlist_sync_when_wait_seconds_is_zero(db):
    from fastapi import HTTPException

    from trackseerr.api.routes.system.tasks import TaskScheduleUpdate, set_task_schedule

    with pytest.raises(HTTPException) as err:
        set_task_schedule("playlist_sync", TaskScheduleUpdate(interval_seconds=3600), db=db, config=_cfg(wait_seconds=0), _admin={})
    assert err.value.status_code == 400


def test_lidarr_migration_is_registered():
    assert "lidarr_migration" in task_manager.TASKS
    assert task_manager.WORKER_THREAD_TASKS["LidarrMigrationThread"] == "lidarr_migration"
    assert "LidarrMigrationThread" not in task_manager.NON_TASK_THREADS


def test_activity_shows_running_lidarr_migration(db, monkeypatch):
    from trackseerr.api.routes.system.tasks import get_system_activity
    from trackseerr.lidarr_migration import lidarr_migration_job

    monkeypatch.setattr(
        lidarr_migration_job,
        "_status",
        {
            **LidarrMigrationJob._default_status(),
            "is_migrating": True,
            "status": "running",
            "artists_total": 10,
            "artists_processed": 4,
            "artists_migrated": 10,
        },
    )
    db.start_task_run("lidarr_migration", "manual", _iso(timedelta(seconds=-5)))
    body = get_system_activity(db=db, _admin={})
    assert len(body["running"]) == 1
    entry = body["running"][0]
    assert entry["name"] == "Lidarr Library Import"
    assert entry["progress"].total == 10 and entry["progress"].current == 4

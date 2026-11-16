"""Unit and integration tests for the scheduled library_self_heal task and worker."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

import trackseerr.api.routes.library.tagging as tagging
import trackseerr.artist_repair as artist_repair
import trackseerr.library_self_heal as lsh
from tests.test_rename_verify_api import R1, R2, _seed, _seed_copy_suffix, _settings
from trackseerr.api.routes.library.tagging import safe_rename_file_ids
from trackseerr.config import Config
from trackseerr.library_self_heal import LibrarySelfHealWorker, run_self_heal
from trackseerr.storage import Database
from trackseerr.task_manager import RunHandle


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "self_heal.db")
    yield database
    database.close()


def _cfg() -> Config:
    return Config(plex_url="http://plex.invalid:32400", plex_token="t")


def test_self_heal_runs_repair_then_safe_renames(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    sentinel_server = object()

    def fake_repair(d: Any, run: Any) -> None:
        events.append("repair")
        run.message = "repair: artists=1"

    def fake_safe_ids(d: Any) -> list[str]:
        events.append("safe_ids")
        return ["fl-1", "fl-2"]

    def fake_apply(d: Any, ids: list[str], server: Any) -> dict[str, Any]:
        assert ids == ["fl-1", "fl-2"]
        assert server is sentinel_server
        events.append("apply")
        return {"renamed_count": 2, "blocked_count": 0, "errors": []}

    monkeypatch.setattr(artist_repair, "run_repair_and_match", fake_repair)
    monkeypatch.setattr(lsh, "safe_rename_file_ids", fake_safe_ids)
    monkeypatch.setattr(lsh, "apply_renames", fake_apply)
    monkeypatch.setattr(lsh, "get_media_server", lambda cfg: sentinel_server)

    run = RunHandle()
    run_self_heal(db, _cfg(), run)

    assert events == ["repair", "safe_ids", "apply"]
    assert run.message is not None
    assert "repair: artists=1" in run.message
    assert "renames: renamed=2 blocked=0 errors=0" in run.message


def test_self_heal_skips_renames_in_lidarr_mode(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    db.update_media_management_settings({"library_mode": "lidarr"})
    events: list[str] = []

    def fake_repair(d: Any, run: Any) -> None:
        events.append("repair")
        run.message = "repair: ok"

    monkeypatch.setattr(artist_repair, "run_repair_and_match", fake_repair)
    monkeypatch.setattr(
        lsh,
        "safe_rename_file_ids",
        lambda d: (_ for _ in ()).throw(AssertionError("safe_rename_file_ids must not run in Lidarr mode")),
    )
    monkeypatch.setattr(
        lsh,
        "apply_renames",
        lambda d, ids, srv: (_ for _ in ()).throw(AssertionError("apply_renames must not run in Lidarr mode")),
    )

    run = RunHandle()
    run_self_heal(db, _cfg(), run)

    assert events == ["repair"]
    assert run.message is not None
    assert "; renames: skipped (Lidarr mode)" in run.message


def test_self_heal_stops_after_repair_when_cancelled(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    def fake_repair(d: Any, run: Any) -> None:
        events.append("repair")
        run.stop_event.set()

    monkeypatch.setattr(artist_repair, "run_repair_and_match", fake_repair)
    monkeypatch.setattr(
        lsh,
        "safe_rename_file_ids",
        lambda d: (_ for _ in ()).throw(AssertionError("safe_rename_file_ids must not run when stopped")),
    )
    monkeypatch.setattr(
        lsh,
        "apply_renames",
        lambda d, ids, srv: (_ for _ in ()).throw(AssertionError("apply_renames must not run when stopped")),
    )

    run = RunHandle()
    run_self_heal(db, _cfg(), run)

    assert events == ["repair"]


def test_safe_rename_file_ids_only_default_selected(db: Database, tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    _settings(db, root)

    selected_id, _copy_path, _target = _seed_copy_suffix(db, root, R1)
    unselected_id, _other_path = _seed(db, root, 2, R2, number=4)

    ids = safe_rename_file_ids(db)
    assert ids == [selected_id]
    assert unselected_id not in ids


def test_worker_records_scheduled_run(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artist_repair, "run_repair_and_match", lambda d, run: setattr(run, "message", "repair: ok"))
    monkeypatch.setattr(lsh, "safe_rename_file_ids", lambda d: ["fl-1"])
    monkeypatch.setattr(
        lsh, "apply_renames", lambda d, ids, srv: {"renamed_count": 1, "blocked_count": 0, "errors": []}
    )
    monkeypatch.setattr(lsh, "get_media_server", lambda cfg: None)

    worker = LibrarySelfHealWorker()
    assert worker.run_once(db, _cfg()) is True

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    runs = db.list_task_runs("library_self_heal", since)
    assert len(runs) == 1
    assert runs[0]["trigger"] == "scheduled"
    assert runs[0]["status"] == "success"
    assert "renames: renamed=1 blocked=0 errors=0" in (runs[0]["message"] or "")


def test_worker_skips_when_mb_release_match_is_running(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = db.start_task_run("mb_release_match", "manual", datetime.now(timezone.utc).isoformat())
    try:
        monkeypatch.setattr(
            artist_repair,
            "run_repair_and_match",
            lambda d, run: (_ for _ in ()).throw(AssertionError("should not run while mb_release_match is active")),
        )
        worker = LibrarySelfHealWorker()
        assert worker.run_once(db, _cfg()) is False
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        assert db.list_task_runs("library_self_heal", since) == []
    finally:
        db.finish_task_run(run_id, "success", datetime.now(timezone.utc).isoformat(), "done", 10)


def test_never_calls_retag(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "music"
    root.mkdir()
    _settings(db, root)
    _seed_copy_suffix(db, root, R1)

    monkeypatch.setattr(
        tagging,
        "retag_library",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("retag_library must never be called")),
    )
    monkeypatch.setattr(artist_repair, "run_repair_and_match", lambda d, run: setattr(run, "message", "repair: ok"))
    monkeypatch.setattr(lsh, "get_media_server", lambda cfg: None)

    run = RunHandle()
    run_self_heal(db, _cfg(), run)
    assert run.message is not None
    assert "renames: renamed=1 blocked=0 errors=0" in run.message

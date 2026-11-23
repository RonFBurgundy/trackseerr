"""Tests for RunHandle.set_stage and _task_progress stage formatting."""

from trackseerr import task_manager
from trackseerr.api.routes.system.tasks import _task_progress


def test_set_stage_resets_progress() -> None:
    handle = task_manager.RunHandle()
    handle.set_progress(5, 10)
    handle.set_stage(2, 11, "Linking albums")
    assert handle.stage == "Step 2 of 11 · Linking albums"
    assert handle.progress_current == 0
    assert handle.progress_total is None


def test_task_progress_shows_stage() -> None:
    handle = task_manager.RunHandle()
    handle.set_stage(3, 11, "Searching MusicBrainz")
    handle.set_progress(3, 160)
    task_manager.register_manual_run("library_self_heal", handle)
    try:
        progress = _task_progress("library_self_heal", True)
        assert progress is not None
        assert progress.current == 3
        assert progress.total == 160
        assert progress.message is not None
        assert progress.message.startswith("Step 3 of 11 · Searching MusicBrainz")
    finally:
        task_manager.unregister_manual_run("library_self_heal", handle)


def test_task_progress_stage_without_total() -> None:
    handle = task_manager.RunHandle()
    handle.set_stage(1, 11, "Repairing artists")
    task_manager.register_manual_run("library_self_heal", handle)
    try:
        progress = _task_progress("library_self_heal", True)
        assert progress is not None
        assert progress.current is None
        assert progress.total is None
        assert progress.message is not None
        assert progress.message.startswith("Step 1 of 11 · Repairing artists")
    finally:
        task_manager.unregister_manual_run("library_self_heal", handle)

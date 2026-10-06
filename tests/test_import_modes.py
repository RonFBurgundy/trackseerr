"""Import placement modes (move/hardlink/copy), safe_atomic_move error handling, settings validation."""
import errno
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plex_playlist_sync import acquisition_worker as aw
from plex_playlist_sync.acquisition_worker import (
    place_audio_file,
    preserves_source,
    safe_atomic_move,
    settle_transfer_after_import,
)


def _src(tmp_path: Path) -> Path:
    src = tmp_path / "dl" / "a.flac"
    src.parent.mkdir()
    src.write_bytes(b"audio-bytes" * 100)
    return src


def test_preserves_source():
    assert preserves_source("hardlink") and preserves_source("copy")
    assert not preserves_source("move") and not preserves_source(None)


def test_copy_keeps_source_and_target_identical(tmp_path):
    src = _src(tmp_path)
    dst = place_audio_file(src, tmp_path / "lib" / "a.flac", mode="copy")
    assert src.exists()
    assert dst.read_bytes() == src.read_bytes()
    assert dst.stat().st_ino != src.stat().st_ino
    assert sorted(p.name for p in dst.parent.iterdir()) == ["a.flac"]  # no temp left behind


def test_hardlink_shares_inode(tmp_path):
    src = _src(tmp_path)
    dst = place_audio_file(src, tmp_path / "lib" / "a.flac", mode="hardlink")
    assert src.exists() and dst.stat().st_ino == src.stat().st_ino


def test_hardlink_falls_back_to_copy_on_exdev(tmp_path, monkeypatch):
    src = _src(tmp_path)

    def boom(*a, **k):
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(os, "link", boom)
    dst = place_audio_file(src, tmp_path / "lib" / "a.flac", mode="hardlink")
    assert src.exists()
    assert dst.read_bytes() == src.read_bytes()
    assert dst.stat().st_ino != src.stat().st_ino


def test_move_removes_source(tmp_path):
    src = _src(tmp_path)
    data = src.read_bytes()
    dst = place_audio_file(src, tmp_path / "lib" / "a.flac", mode="move")
    assert not src.exists() and dst.read_bytes() == data


def test_unknown_mode_raises(tmp_path):
    src = _src(tmp_path)
    with pytest.raises(ValueError):
        place_audio_file(src, tmp_path / "lib" / "a.flac", mode="symlink")
    assert src.exists()


def test_safe_atomic_move_exdev_copies_then_unlinks(tmp_path, monkeypatch):
    src = _src(tmp_path)
    real_replace = os.replace
    calls = {"n": 0}

    def replace(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device")
        return real_replace(a, b)

    monkeypatch.setattr(os, "replace", replace)
    dst = safe_atomic_move(src, tmp_path / "lib" / "a.flac")
    assert dst.exists() and not src.exists()
    assert sorted(p.name for p in dst.parent.iterdir()) == ["a.flac"]


def test_safe_atomic_move_reraises_non_exdev(tmp_path, monkeypatch, caplog):
    src = _src(tmp_path)

    def replace(a, b):
        raise OSError(errno.EACCES, "denied")

    monkeypatch.setattr(os, "replace", replace)
    with caplog.at_level(logging.ERROR), pytest.raises(OSError) as exc:
        safe_atomic_move(src, tmp_path / "lib" / "a.flac")
    assert exc.value.errno == errno.EACCES
    assert src.exists()
    assert "denied" in caplog.text


def test_safe_atomic_move_logs_unlink_failure(tmp_path, monkeypatch, caplog):
    src = _src(tmp_path)
    real_replace = os.replace
    calls = {"n": 0}

    def replace(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device")
        return real_replace(a, b)

    def unlink(self, missing_ok=False):
        raise PermissionError(errno.EPERM, "read-only source")

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    with caplog.at_level(logging.WARNING):
        dst = safe_atomic_move(src, tmp_path / "lib" / "a.flac")
    assert dst.exists()
    assert "could not remove the source" in caplog.text and "read-only source" in caplog.text


def test_safe_atomic_move_cleans_temp_on_copy_failure(tmp_path, monkeypatch):
    src = _src(tmp_path)
    monkeypatch.setattr(os, "replace", MagicMock(side_effect=OSError(errno.EXDEV, "x")))

    def bad_copy(s, d, *a, **k):
        Path(d).write_bytes(b"partial")
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(aw.shutil, "copy2", bad_copy)
    with pytest.raises(OSError):
        safe_atomic_move(src, tmp_path / "lib" / "a.flac")
    assert list((tmp_path / "lib").iterdir()) == []
    assert src.exists()


# ---------------------------------------------------------------- governance

def _gov(settings, mode, status):
    driver = MagicMock()
    result = settle_transfer_after_import(driver, "H", settings, mode, status)
    return driver, result


def test_governance_off_never_calls_client():
    driver, res = _gov({"seed_complete_action": "keep"}, "move", {})
    driver.cleanup_completed.assert_not_called()
    assert res == "imported"


def test_governance_limits_not_met_keeps_seeding():
    s = {"seed_complete_action": "remove", "seed_ratio_limit": 2.0}
    driver, res = _gov(s, "copy", {"ratio": 1.0})
    driver.cleanup_completed.assert_not_called()
    assert res == "completed"


def test_governance_limits_met_cleans():
    s = {"seed_complete_action": "remove", "seed_time_limit_minutes": 10}
    driver, res = _gov(s, "hardlink", {"seeding_time_seconds": 700})
    driver.cleanup_completed.assert_called_once_with("H", delete_files=False)
    assert res == "imported"


def test_governance_move_mode_ignores_limits():
    s = {"seed_complete_action": "remove", "seed_ratio_limit": 2.0}
    driver, res = _gov(s, "move", {"ratio": 0.0})
    driver.cleanup_completed.assert_called_once()
    assert res == "imported"


def test_governance_unknown_status_keeps_transfer():
    s = {"seed_complete_action": "remove", "seed_ratio_limit": 2.0}
    driver, res = _gov(s, "hardlink", None)
    driver.cleanup_completed.assert_not_called()
    assert res == "completed"


# ---------------------------------------------------------------- settings validation

def test_settings_model_rejects_bad_import_mode():
    from pydantic import ValidationError

    from plex_playlist_sync.api.routes.settings import MediaManagementUpdateModel

    assert MediaManagementUpdateModel(import_mode="copy").import_mode == "copy"
    with pytest.raises(ValidationError):
        MediaManagementUpdateModel(import_mode="symlink")

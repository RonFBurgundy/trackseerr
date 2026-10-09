"""The extra import folder (staging_folder_path) is empty by default and empty means no extra import root."""
from pathlib import Path

from trackseerr.acquisition_worker import AcquisitionWorker
from trackseerr.api.routes.library._shared import validate_media_path
from trackseerr.download_roots import build_allowed_roots
from trackseerr.models import DownloadStatus, MediaManagementSettings
from trackseerr.storage import Database
from tests.test_import_security import PAD, _run

import pytest
from fastapi import HTTPException


def test_fresh_db_and_model_default_to_empty(tmp_path):
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert db.get_media_management_settings()["staging_folder_path"] == ""
    finally:
        db.close()
    assert MediaManagementSettings().staging_folder_path == ""
    assert AcquisitionWorker().staging_dir == ""


def test_empty_staging_adds_no_import_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    allowed = build_allowed_roots({"root_folder_path": str(tmp_path / "lib"), "staging_folder_path": ""}, [])
    assert allowed.roots == []
    assert not allowed.is_allowed(tmp_path / "anything")


def test_empty_staging_not_an_import_base(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = Database(":memory:")
    lib = tmp_path / "lib"
    lib.mkdir()
    db.update_media_management_settings({"root_folder_path": str(lib), "staging_folder_path": "   "})
    with pytest.raises(HTTPException):
        validate_media_path(str(tmp_path), db=db, purpose="import")
    with pytest.raises(HTTPException):
        validate_media_path(".", db=db, purpose="import")
    db.close()


def test_quarantine_fallback_with_empty_staging_never_uses_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    # No quarantine path and no library root: nothing to quarantine into, so the file stays and the cwd stays clean.
    db, stats, paths, downloads, music = _run(
        tmp_path, "off", {"01.mp3": b"<html>nope</html>"}, mutagen_result=None,
        extra_settings={"root_folder_path": "", "staging_folder_path": ""},
    )
    assert stats["failed"] == 1 and stats["imported"] == 0
    assert db.get_active_download("dl-1")["status"] == DownloadStatus.FAILED.value
    assert list(Path(cwd).iterdir()) == []
    assert not (downloads / "_quarantine").exists()
    assert paths[0].exists()

"""validate_media_path: configured roots only, per purpose; no hardcoded /data, /tmp, cwd or config bases."""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from trackseerr.api.routes.library._shared import validate_media_path
from trackseerr.models import DownloadClientConfig, DownloadDriverType
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "vmp.db"))
    yield d
    d.close()


@pytest.fixture
def tree(tmp_path: Path):
    paths = {
        "library": tmp_path / "data/media/music",
        "qbit": tmp_path / "data/torrents/music",
        "other": tmp_path / "data/other",
        "config": tmp_path / "appconfig",
    }
    for p in paths.values():
        p.mkdir(parents=True)
    return paths


@pytest.fixture
def configured(db, tree):
    db.create_download_client(
        DownloadClientConfig(id="c1", name="qBit", driver_type=DownloadDriverType.QBITTORRENT, host_url="http://q:1")
    )
    db.update_media_management_settings({"root_folder_path": str(tree["library"]), "staging_folder_path": ""})
    driver = MagicMock()
    driver.get_download_roots.return_value = [str(tree["qbit"])]
    driver.last_roots_error = None
    with patch("trackseerr.download_roots.get_acquisition_driver", return_value=driver):
        yield db


def _status(path, db, **kw) -> int:
    with pytest.raises(HTTPException) as exc:
        validate_media_path(str(path), db=db, **kw)
    return exc.value.status_code


def test_library_root_accepted_for_every_purpose(configured, tree):
    target = tree["library"] / "Artist" / "a.flac"
    for purpose in ("library", "import", "internal"):
        assert validate_media_path(str(target), db=configured, purpose=purpose) == target.resolve()


def test_client_root_only_for_import(configured, tree):
    src = tree["qbit"] / "Album" / "a.mp3"
    assert validate_media_path(str(src), db=configured, purpose="import") == src.resolve()
    assert _status(src, configured) == 403
    assert _status(src, configured, purpose="library") == 403


def test_sibling_of_configured_roots_rejected(configured, tree):
    other = tree["other"] / "x.mp3"
    for purpose in ("library", "import", "internal"):
        assert _status(other, configured, purpose=purpose) == 403


def test_system_paths_and_defaults_rejected(configured, tmp_path, monkeypatch):
    assert _status("/etc/passwd", configured, purpose="import") == 403
    monkeypatch.chdir(tmp_path)  # cwd and /tmp-style locations are no longer implicit bases
    assert _status(tmp_path / "loose.mp3", configured, purpose="import") == 403


def test_no_db_or_no_config_rejects_everything(db, tree):
    assert _status(tree["library"] / "a.mp3", None) == 403
    assert _status(tree["library"] / "a.mp3", db) == 403


def test_traversal_rejected(configured, tree):
    sneaky = f"{tree['library']}/../other/x.mp3"
    with pytest.raises(HTTPException) as exc:
        validate_media_path(sneaky, db=configured)
    assert exc.value.status_code == 400


def test_symlink_escape_rejected(configured, tree):
    link = tree["library"] / "escape"
    os.symlink(tree["other"], link)
    assert _status(link / "x.mp3", configured) == 403
    assert _status(link / "x.mp3", configured, purpose="import") == 403


def test_config_dir_only_for_internal(configured, tree, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tree["config"]))
    cfg_file = tree["config"] / "settings.db"
    assert _status(cfg_file, configured, purpose="library") == 403
    assert _status(cfg_file, configured, purpose="import") == 403
    assert validate_media_path(str(cfg_file), db=configured, purpose="internal") == cfg_file.resolve()


def test_unknown_purpose_raises(configured, tree):
    with pytest.raises(ValueError):
        validate_media_path(str(tree["library"]), db=configured, purpose="everything")

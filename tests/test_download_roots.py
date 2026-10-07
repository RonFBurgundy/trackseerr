"""Client-derived import containment: AllowedRoots, per-client cache, worker, manual import, roots endpoint."""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import download_roots as dr
from plex_playlist_sync.acquisition_worker import AcquisitionWorker
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
)
from plex_playlist_sync.storage import Database
from tests.audio_fixtures import write_mp3


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "roots.db"))
    yield d
    d.close()


@pytest.fixture
def tree(tmp_path: Path):
    """TRaSH-style sibling roots on one filesystem."""
    paths = {
        "library": tmp_path / "data/media/music",
        "qbit": tmp_path / "data/torrents/music",
        "usenet": tmp_path / "data/usenet/music",
        "slskd": tmp_path / "data/slskd/complete/music",
        "incomplete": tmp_path / "data/slskd/incomplete",
    }
    for p in paths.values():
        p.mkdir(parents=True)
    return paths


def _driver(roots, error=None):
    d = MagicMock()
    d.get_download_roots.return_value = roots
    d.last_roots_error = error
    return d


def _client(db: Database, cid="c-qbit", driver=DownloadDriverType.QBITTORRENT, extra=None, name="qBit"):
    import json

    cfg = DownloadClientConfig(id=cid, name=name, driver_type=driver, host_url="http://qbit:8080")
    db.create_download_client(cfg)
    if extra:
        db.conn.execute("UPDATE download_clients SET extra_settings_json = ? WHERE id = ?", (json.dumps(extra), cid))
        db.conn.commit()
    return db.get_download_client(cid)


def _roots(tree, client_roots, staging=""):
    mm = {"root_folder_path": str(tree["library"]), "staging_folder_path": staging}
    cfg = {"id": "c1", "name": "qBit", "driver_type": "qbittorrent", "host_url": "http://x"}
    return dr.build_allowed_roots(mm, [cfg], driver=_driver([str(r) for r in client_roots]))


# --------------------------------------------------------------- containment
def test_accepts_client_root_rejects_library_and_outside(tree):
    allowed = _roots(tree, [tree["qbit"]])
    assert allowed.check(tree["qbit"] / "Artist - Album")[0]
    ok, why = allowed.check(tree["library"] / "Artist")
    assert not ok and "library" in why
    assert not allowed.check("/etc/passwd")[0]
    assert not allowed.check(tree["usenet"] / "x")[0]  # a sibling root this client did not report


def test_each_client_gets_its_own_roots(tree):
    mm = {"root_folder_path": str(tree["library"]), "staging_folder_path": ""}
    clients = [
        {"id": "a", "name": "qbit", "driver_type": "qbittorrent", "host_url": "http://x"},
        {"id": "b", "name": "sab", "driver_type": "sabnzbd", "host_url": "http://y"},
    ]
    drivers = {"a": _driver([str(tree["qbit"])]), "b": _driver([str(tree["usenet"])])}
    for cfg in clients:
        d = drivers[cfg["id"]]
        allowed = dr.build_allowed_roots(mm, [cfg], driver=d)
        mine, other = (tree["qbit"], tree["usenet"]) if cfg["id"] == "a" else (tree["usenet"], tree["qbit"])
        assert allowed.is_allowed(mine / "r") and not allowed.is_allowed(other / "r")


def test_rejects_symlink_escaping_a_root(tree, tmp_path):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "a.mp3").write_bytes(b"x")
    link = tree["qbit"] / "evil"
    link.symlink_to(secret, target_is_directory=True)
    allowed = _roots(tree, [tree["qbit"]])
    assert not allowed.is_allowed(link)
    assert not allowed.is_allowed(link / "a.mp3")


def test_legacy_staging_still_accepted_and_library_still_blocked(tree, tmp_path):
    legacy = tmp_path / "data"  # the broad "/data" staging users had to set before
    allowed = _roots(tree, [], staging=str(legacy))
    assert allowed.is_allowed(tree["slskd"] / "x")
    assert not allowed.is_allowed(tree["library"] / "x")
    assert not allowed.is_allowed(legacy)  # contains the library


def test_config_dir_blocked_unless_root_lives_inside_it(tree, tmp_path, monkeypatch):
    cfgdir = tmp_path / "cfg"
    (cfgdir / "dl").mkdir(parents=True)
    monkeypatch.setenv("CONFIG_DIR", str(cfgdir))
    broad = _roots(tree, [], staging=str(tmp_path))
    assert not broad.is_allowed(cfgdir / "app.db")
    dr.clear_roots_cache()
    inside = _roots(tree, [cfgdir / "dl"])
    assert inside.is_allowed(cfgdir / "dl" / "x")


def test_usable_roots_drop_library_roots(tree):
    allowed = _roots(tree, [tree["qbit"], tree["library"] / "sub"])
    assert allowed.usable_roots() == [tree["qbit"].resolve()]


# ----------------------------------------------------- mappings, cache, errors
def test_roots_translated_through_remote_path_mappings(db, tree):
    cfg = _client(db, extra={"remote_path_mappings": [{"remote_path": "/downloads", "local_path": str(tree["qbit"].parent)}]})
    roots, err = dr.fetch_client_roots(cfg, driver_factory=lambda c: _driver(["/downloads/music"]))
    assert roots == [str(tree["qbit"])] and err is None


def test_roots_cached_per_client_with_ttl_and_config_invalidation(db):
    cfg = _client(db)
    d = _driver(["/data/torrents"])
    dr.fetch_client_roots(cfg, driver_factory=lambda c: d)
    dr.fetch_client_roots(cfg, driver_factory=lambda c: d)
    assert d.get_download_roots.call_count == 1
    dr.fetch_client_roots(dict(cfg, host_url="http://other:1"), driver_factory=lambda c: d)
    assert d.get_download_roots.call_count == 2
    with patch("plex_playlist_sync.download_roots.time.monotonic", return_value=1e12):
        dr.fetch_client_roots(dict(cfg, host_url="http://other:1"), driver_factory=lambda c: d)
    assert d.get_download_roots.call_count == 3


def test_unreadable_roots_report_error(db):
    cfg = _client(db)
    roots, err = dr.fetch_client_roots(cfg, driver_factory=lambda c: _driver([], "connection refused"))
    assert roots == [] and err == "connection refused"


# -------------------------------------------------------- _find_audio_files
def test_find_audio_files_uses_client_roots_and_fallback_scans_roots(tree):
    good = tree["qbit"] / "Artist - Album"
    good.mkdir()
    write_mp3(good / "01 Song.mp3")
    in_lib = tree["library"] / "Artist - Album"
    in_lib.mkdir()
    write_mp3(in_lib / "01 Song.mp3")
    worker = AcquisitionWorker()
    worker.allowed_roots = _roots(tree, [tree["qbit"]])
    assert worker._find_audio_files(good, "x") == [(good / "01 Song.mp3").resolve()]
    assert worker._find_audio_files(in_lib, "zzz-nomatch") == []
    # stale source path: fallback search by term scans the allowed roots only
    found = worker._find_audio_files("/nonexistent/path", "Artist - Album")
    assert found == [(good / "01 Song.mp3").resolve()]


def test_fallback_scan_with_broad_legacy_root_skips_library(tree, tmp_path):
    write_mp3(tree["qbit"] / "Song Title.mp3")
    write_mp3(tree["library"] / "Song Title.mp3")
    worker = AcquisitionWorker()
    worker.allowed_roots = _roots(tree, [], staging=str(tmp_path / "data"))
    assert worker._find_audio_files(None, "song title") == [(tree["qbit"] / "Song Title.mp3").resolve()]


def _poll(db, driver, staging, tmp_path):
    return AcquisitionWorker().poll_once(db=db, staging_dir=staging)


def test_poll_unreadable_roots_leaves_download_pending_with_message(db, tree, tmp_path):
    _client(db, name="My qBit")
    db.update_media_management_settings({"root_folder_path": str(tree["library"]), "library_mode": "native"})
    db.create_active_download(
        ActiveDownload(id="dl-1", title="T", artist="A", client_id="c-qbit", download_hash="h",
                       status=DownloadStatus.COMPLETED.value)
    )
    driver = _driver([], "connection refused")
    driver.get_status.return_value = {"status": "completed", "progress": 100.0, "source_path": str(tree["qbit"] / "x")}
    worker = AcquisitionWorker()
    # an empty staging override means no legacy fallback: nothing may be accepted
    db.update_media_management_settings({"staging_folder_path": ""})
    with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=driver):
        stats = worker.poll_once(db=db)
    row = db.get_active_download("dl-1")
    assert stats["failed"] == 0 and stats["imported"] == 0
    assert row["status"] == DownloadStatus.COMPLETED.value
    assert "Could not read download folder from My qBit" in row["error_message"]
    assert "check client connection" in row["error_message"]


def test_poll_imports_from_client_root_when_staging_unset(db, tree):
    _client(db)
    db.update_media_management_settings(
        {"root_folder_path": str(tree["library"]), "staging_folder_path": "", "library_mode": "native",
         "write_audio_tags": False, "enrich_mbids": False}
    )
    src = tree["qbit"] / "Rel"
    src.mkdir()
    write_mp3(src / "01 Song.mp3")
    db.create_active_download(
        ActiveDownload(id="dl-2", title="Rel", artist="A", client_id="c-qbit", download_hash="h2",
                       status=DownloadStatus.COMPLETED.value)
    )
    driver = _driver([str(tree["qbit"])])
    driver.get_status.return_value = {"status": "completed", "progress": 100.0, "source_path": str(src)}
    with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=driver):
        AcquisitionWorker().poll_once(db=db)
    row = db.get_active_download("dl-2")
    # whatever the downstream outcome, the source must not have been rejected as outside the roots
    assert "No audio files found" not in (row.get("error_message") or "")


# ----------------------------------------------------- manual import + endpoint
@pytest.fixture
def config(tmp_path: Path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def headers(db, config):
    admin = db.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=admin["id"], username="admin_user", is_admin=True, secret_key=secret)
    db.create_session(token, admin["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api(db, config):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


def _scanned_item():
    """A scan item in the shape ``_scan_one_file`` really returns (the route's response model is strict)."""
    return {
        "file_path": "scanned", "filename": "Song.mp3", "size_bytes": 1, "tags": {"title": "Song"},
        "matched_artist_id": None, "matched_artist_name": None, "matched_album_id": None,
        "matched_album_title": None, "matched_track_id": None, "matched_track_title": None, "confidence": 0.0,
    }


_NO_MATCH = {"match_strength": "none", "suggested_track_id": None, "candidate_tracks": []}


def test_manual_import_default_folder_is_first_client_root(db, api, headers, tree):
    _client(db)
    db.update_media_management_settings({"root_folder_path": str(tree["library"]), "staging_folder_path": ""})
    write_mp3(tree["qbit"] / "Song.mp3")
    with patch("plex_playlist_sync.download_roots.get_acquisition_driver", return_value=_driver([str(tree["qbit"])])), \
         patch("plex_playlist_sync.api.routes.library._scan_one_file", return_value=(MagicMock(), _scanned_item())) as scan, \
         patch("plex_playlist_sync.api.routes.library._unscoped_match_fields", return_value=_NO_MATCH):
        resp = api.post("/api/library/manual-import/scan", json={}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert [Path(c.args[1]) for c in scan.call_args_list] == [(tree["qbit"] / "Song.mp3").resolve()]


def test_manual_import_default_folder_400_when_nothing_known(db, api, headers, tree):
    _client(db)
    db.update_media_management_settings({"root_folder_path": str(tree["library"]), "staging_folder_path": ""})
    with patch("plex_playlist_sync.download_roots.get_acquisition_driver", return_value=_driver([], "refused")):
        resp = api.post("/api/library/manual-import/scan", json={}, headers=headers)
    assert resp.status_code == 400
    assert "folder_path" in resp.json()["detail"] and "/downloads" not in resp.json()["detail"]


def test_validate_media_path_approves_client_roots(db, tree, tmp_path):
    from fastapi import HTTPException
    from plex_playlist_sync.api.routes.library import validate_media_path

    _client(db)
    db.update_media_management_settings({"root_folder_path": str(tree["library"]), "staging_folder_path": ""})
    with patch("plex_playlist_sync.download_roots.get_acquisition_driver", return_value=_driver([str(tree["qbit"])])):
        assert validate_media_path(str(tree["qbit"] / "x"), db=db, purpose="import") == (tree["qbit"] / "x").resolve()
        assert validate_media_path(str(tree["library"] / "a"), db=db) == (tree["library"] / "a").resolve()
        with pytest.raises(HTTPException) as exc:
            validate_media_path("/etc/passwd", db=db)
        assert exc.value.status_code == 403


def test_roots_endpoint_lists_roots_and_errors(db, api, headers, tree):
    _client(db, cid="c-q", name="qBit")
    _client(db, cid="c-s", name="SAB", driver=DownloadDriverType.SABNZBD)
    _client(db, cid="c-l", name="Lidarr", driver=DownloadDriverType.LIDARR)
    drivers = {"qbit": _driver([str(tree["qbit"])]), "sab": _driver([], "SABnzbd get_config misc failed (HTTP 500)")}

    def factory(cfg):
        return drivers["qbit"] if cfg["id"] == "c-q" else drivers["sab"]

    with patch("plex_playlist_sync.download_roots.get_acquisition_driver", side_effect=factory):
        resp = api.get("/api/settings/download-clients/roots", headers=headers)
    assert resp.status_code == 200, resp.text
    by_id = {r["client_id"]: r for r in resp.json()}
    assert set(by_id) == {"c-q", "c-s"}  # Lidarr has no roots concept
    assert by_id["c-q"] == {"client_id": "c-q", "name": "qBit", "roots": [str(tree["qbit"])], "error": None}
    assert by_id["c-s"]["roots"] == [] and "HTTP 500" in by_id["c-s"]["error"]


def test_roots_endpoint_requires_admin(api):
    assert api.get("/api/settings/download-clients/roots").status_code in (401, 403)

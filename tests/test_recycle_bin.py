"""Recycle bin: cleanup job, empty endpoint, settings validation, scanner/worker exclusions, migration."""

import os
import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import recycle_bin as rb
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.library_scanner import LibraryScanner
from trackseerr.models import DownloadClientConfig, DownloadDriverType
from trackseerr.storage import SCHEMA_VERSION, Database

TODAY = date(2026, 10, 6)


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "rb.db"))
    (tmp_path / "music").mkdir()
    d.update_media_management_settings({"root_folder_path": str(tmp_path / "music"), "library_mode": "native"})
    yield d
    d.close()


@pytest.fixture
def config(tmp_path: Path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


def _headers(db, config, *, admin=True):
    user = db.upsert_user("a-1" if admin else "u-1", "admin_user" if admin else "user", "u@example.com", is_admin=admin)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=user["id"], username="x", is_admin=admin, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def _client(db, config):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


def _bin_with_days(tmp_path, days):
    rec = tmp_path / "music" / rb.RECYCLE_DIRNAME
    for d in days:
        f = rec / d / "Artist" / "a.mp3"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
    return rec


# ------------------------------------------------------------------------------------------ settings + migration
def test_defaults_for_new_install(db, tmp_path):
    mm = db.get_media_management_settings()
    assert mm["recycle_bin_path"] == "" and mm["recycle_bin_cleanup_days"] == 30
    assert mm["recycle_bin_permanent_delete"] is False and mm["quarantine_folder_path"] == ""
    assert rb.effective_recycle_path(mm) == (tmp_path / "music" / ".trackseerr-recycle").resolve()
    assert rb.effective_quarantine_path(mm) == (tmp_path / "music" / ".trackseerr-quarantine").resolve()



def test_negative_cleanup_days_rejected(db):
    with pytest.raises(ValueError):
        db.update_media_management_settings({"recycle_bin_cleanup_days": -1})


# ------------------------------------------------------------------------------------------ cleanup
def test_cleanup_deletes_only_folders_older_than_n_days(tmp_path):
    rec = _bin_with_days(tmp_path, ["2026-08-01", "2026-09-05", "2026-09-07", "2026-10-06"])
    (rec / "notes.txt").write_text("keep")
    (rec / "not-a-date").mkdir()
    res = rb.cleanup_recycle_bin(rec, 30, library_root=tmp_path / "music", now=TODAY)
    assert sorted(Path(p).name for p in res.removed) == ["2026-08-01", "2026-09-05"]
    assert sorted(p.name for p in rec.iterdir()) == ["2026-09-07", "2026-10-06", "not-a-date", "notes.txt"]


def test_cleanup_zero_days_never_deletes(tmp_path):
    rec = _bin_with_days(tmp_path, ["2020-01-01"])
    res = rb.cleanup_recycle_bin(rec, 0, library_root=tmp_path / "music", now=TODAY)
    assert res.removed == [] and (rec / "2020-01-01").exists()


def test_cleanup_ignores_symlinks_and_never_follows_them(tmp_path):
    rec = _bin_with_days(tmp_path, ["2020-01-01"])
    outside = tmp_path / "outside"
    (outside / "keep").mkdir(parents=True)
    (outside / "keep" / "f.mp3").write_bytes(b"x")
    (rec / "2020-02-02").symlink_to(outside, target_is_directory=True)  # dated symlink to a folder elsewhere
    (rec / "2020-01-01" / "link").symlink_to(outside, target_is_directory=True)  # link nested in a dated folder
    res = rb.cleanup_recycle_bin(rec, 30, library_root=tmp_path / "music", now=TODAY)
    assert [Path(p).name for p in res.removed] == ["2020-01-01"]
    assert (outside / "keep" / "f.mp3").exists()  # nothing outside the bin was touched
    assert (rec / "2020-02-02").is_symlink()


def test_cleanup_refuses_unsafe_bin_locations(tmp_path):
    music = tmp_path / "music"
    music.mkdir(exist_ok=True)
    (music / "2020-01-01").mkdir()
    res = rb.cleanup_recycle_bin(music, 30, library_root=music, now=TODAY)
    assert res.removed == [] and "library" in res.skipped_reason and (music / "2020-01-01").exists()
    dl = tmp_path / "dl"
    (dl / "2020-01-01").mkdir(parents=True)
    res = rb.cleanup_recycle_bin(dl, 30, library_root=music, client_roots=[dl], now=TODAY)
    assert res.removed == [] and (dl / "2020-01-01").exists()


def test_run_cleanup_uses_settings_and_records_event(db, tmp_path):
    rec = _bin_with_days(tmp_path, ["2020-01-01"])
    db.update_media_management_settings({"recycle_bin_cleanup_days": 10})
    res = rb.run_cleanup(db)
    assert len(res.removed) == 1 and not (rec / "2020-01-01").exists()
    assert db.conn.execute("SELECT COUNT(*) FROM system_events WHERE event_type='recycle_bin_cleanup'").fetchone()[0] == 1


def test_task_is_registered_and_triggerable(db, config):
    c = _client(db, config)
    h = _headers(db, config)
    tasks = {t["id"]: t for t in c.get("/api/system/tasks", headers=h).json()}
    assert tasks["recycle_bin_cleanup"]["name"] == "Recycle Bin cleanup"
    with patch("trackseerr.recycle_bin.run_cleanup") as run:
        assert c.post("/api/system/tasks/recycle_bin_cleanup/run", headers=h).status_code == 200
        for _ in range(100):
            if run.called:
                break
            import time; time.sleep(0.02)
        assert run.called


# ------------------------------------------------------------------------------------------ empty endpoint
def test_empty_endpoint_admin_core_only_and_confirmed(db, config, tmp_path):
    rec = _bin_with_days(tmp_path, ["2026-10-01", "2026-10-02"])
    c = _client(db, config)
    assert c.post("/api/recycle-bin/empty", json={"confirm": True}).status_code in (401, 403)
    assert c.post("/api/recycle-bin/empty", json={"confirm": True}, headers=_headers(db, config, admin=False)).status_code == 403
    admin = _headers(db, config)
    assert c.post("/api/recycle-bin/empty", json={}, headers=admin).status_code == 400
    assert any(rec.iterdir())
    res = c.post("/api/recycle-bin/empty", json={"confirm": True}, headers=admin)
    assert res.status_code == 200 and res.json()["removed"] == 2
    assert rec.is_dir() and not any(rec.iterdir())


def test_empty_endpoint_blocked_on_gateway(db, tmp_path):
    cfg = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    c = _client(db, cfg)
    headers = _headers(db, cfg)
    c.app.dependency_overrides[get_config] = lambda: Config(
        plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), role="gateway"
    )
    assert c.post("/api/recycle-bin/empty", json={"confirm": True}, headers=headers).status_code in (403, 404)  # gateway: hidden or forbidden


# ------------------------------------------------------------------------------------------ validation on save
def _save(c, h, **body):
    return c.post("/api/settings/media-management", json=body, headers=h)


def test_settings_validation(db, config, tmp_path):
    c, h = _client(db, config), _headers(db, config)
    music = tmp_path / "music"
    dl = tmp_path / "downloads"
    dl.mkdir()
    db.create_download_client(DownloadClientConfig(
        id="c1", name="q", driver_type=DownloadDriverType.QBITTORRENT, host_url="http://q:8080", enabled=True))
    with patch("trackseerr.api.routes.settings.allowed_roots_for_all_clients") as roots:
        from trackseerr.download_roots import AllowedRoots
        roots.return_value = AllowedRoots(roots=[dl.resolve()])
        assert _save(c, h, recycle_bin_path="relative/bin").status_code == 422
        assert _save(c, h, recycle_bin_path=str(music)).status_code == 422
        assert _save(c, h, quarantine_folder_path=str(music)).status_code == 422
        assert _save(c, h, recycle_bin_path=str(dl / "bin")).status_code == 422
        assert _save(c, h, quarantine_folder_path=str(dl)).status_code == 422
        assert _save(c, h, recycle_bin_path=str(tmp_path / "r"), quarantine_folder_path=str(tmp_path / "r" / "q")).status_code == 422
        assert _save(c, h, recycle_bin_path=str(tmp_path / "q" / "r"), quarantine_folder_path=str(tmp_path / "q")).status_code == 422
        assert db.get_media_management_settings()["recycle_bin_path"] == ""  # nothing was persisted
        ok = _save(c, h, recycle_bin_path=str(tmp_path / "bin"), quarantine_folder_path=str(tmp_path / "quar"),
                   recycle_bin_cleanup_days=7, recycle_bin_permanent_delete=False)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["recycle_bin_path"] == str(tmp_path / "bin") and body["recycle_bin_cleanup_days"] == 7
    assert body["warnings"] == [] and body["effective_recycle_bin_path"] == str((tmp_path / "bin").resolve())


def test_settings_cross_device_warning(db, config, tmp_path):
    c, h = _client(db, config), _headers(db, config)
    real_stat = os.stat
    other = tmp_path / "other"
    other.mkdir()

    def fake_stat(p, *a, **k):
        st = real_stat(p, *a, **k)
        if str(p).startswith(str(other)):
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, *tuple(st)[3:]))
        return st

    with patch("trackseerr.recycle_bin.os.stat", side_effect=fake_stat):
        res = _save(c, h, recycle_bin_path=str(other / "bin"))
    assert res.status_code == 200
    assert any("different filesystem" in w for w in res.json()["warnings"])


def test_get_settings_exposes_effective_paths(db, config, tmp_path):
    body = _client(db, config).get("/api/settings/media-management", headers=_headers(db, config)).json()["settings"]
    assert body["effective_recycle_bin_path"].endswith(".trackseerr-recycle")
    assert body["effective_quarantine_folder_path"].endswith(".trackseerr-quarantine")


# ------------------------------------------------------------------------------------------ exclusions
def test_scanner_skips_recycle_quarantine_and_legacy(db, tmp_path):
    music = tmp_path / "music"
    custom = tmp_path / "music" / "my-custom-bin"
    db.update_media_management_settings({"recycle_bin_path": str(custom)})
    keep = music / "Artist" / "Album" / "01.mp3"
    skipped = [
        music / ".trackseerr-recycle" / "2026-10-01" / "Artist" / "old.mp3",
        music / ".trackseerr-quarantine" / "dl" / "000_bad.mp3",
        music / "_quarantine" / "replaced" / "i" / "000_x.mp3",
        music / "Artist" / "_quarantine" / "000_y.mp3",
        custom / "2026-10-01" / "z.mp3",
    ]
    for p in [keep, *skipped]:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    def fake_inspect(path):
        return {"title": "t", "artist": "Artist", "album": "Album", "track_number": 1, "disc_number": 1, "year": 2020,
                "duration": 1.0, "codec": "MP3", "bitrate": 1, "sample_rate": 44100, "bits_per_sample": 16,
                "quality_full": "MP3", "file_path": str(path)}

    with patch("trackseerr.library_scanner.inspect_audio_file", side_effect=fake_inspect):
        status = LibraryScanner().scan(db, root_folder=str(music))
    assert status["total_files_found"] == 1


def test_worker_walk_prunes_excluded_dirs(tmp_path):
    root = tmp_path / "dl"
    (root / ".trackseerr-quarantine").mkdir(parents=True)
    (root / "_quarantine").mkdir()
    (root / "ok").mkdir()
    custom = root / "custom"
    custom.mkdir()
    dirs = ["ok", ".trackseerr-quarantine", "_quarantine", "custom", ".trackseerr-recycle"]
    assert rb.prune_excluded_dirs(str(root), dirs, [custom.resolve()]) == ["ok"]

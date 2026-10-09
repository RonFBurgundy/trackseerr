"""Seed cleanup: migration, deletion safety gate, settle actions, sweep, orphan/retry routes, qBittorrent listing, worker."""
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import seed_cleanup as sc
from trackseerr.acquisition_worker import deletion_safe, settle_transfer_after_import
from trackseerr.activity_service import native_seeding
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.acquisition.base import AcquisitionDriver
from trackseerr.clients.acquisition.qbittorrent import QbittorrentDriver
from trackseerr.config import Config
from trackseerr.models import ActiveDownload, DownloadClientConfig, DownloadDriverType, DownloadStatus
from trackseerr.storage import SCHEMA_VERSION, Database

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
IMPORTED = DownloadStatus.IMPORTED.value
COMPLETED = DownloadStatus.COMPLETED.value


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "seed.db"))
    (tmp_path / "music").mkdir()
    (tmp_path / "downloads").mkdir()
    d.update_media_management_settings(
        {"root_folder_path": str(tmp_path / "music"), "library_mode": "native", "seed_complete_action": "remove",
         "import_mode": "copy"}
    )
    d.create_download_client(
        DownloadClientConfig(
            id="c1", name="qbit", driver_type=DownloadDriverType.QBITTORRENT, host_url="http://192.168.1.9:8080", enabled=True
        )
    )
    yield d
    d.close()


class FakeDriver(AcquisitionDriver):
    is_torrent = True

    def __init__(self, torrents: Optional[list[dict[str, Any]]] = None, statuses: Optional[dict[str, dict]] = None,
                 fail: bool = False) -> None:
        self.torrents, self.statuses, self.fail = torrents, statuses or {}, fail
        self.removed: list[tuple[str, bool]] = []

    def test_connection(self): return True, "ok"
    def search(self, artist, title=None, album=None): return []
    def download(self, result): return "h"
    def cancel(self, download_id): return True

    def get_status(self, download_id):
        return self.statuses.get(download_id.lower())

    def list_category(self):
        return self.torrents

    def cleanup_completed(self, download_id, delete_files=False):
        if self.fail:
            raise RuntimeError("client exploded http://u:secret@host/x")
        self.removed.append((download_id, delete_files))
        if self.torrents:
            self.torrents = [t for t in self.torrents if t["hash"] != download_id.lower()]
        return True


class World:
    """A torrent's content folder, its library copy, and a download row pointing at both."""

    def __init__(self, db: Database, tmp_path: Path, mode: str = "copy", hardlink: bool = False) -> None:
        self.db, self.tmp = db, tmp_path
        self.content = tmp_path / "downloads" / "Album"
        self.content.mkdir(parents=True)
        self.src = self.content / "01.flac"
        self.src.write_bytes(b"audio-bytes")
        self.lib = tmp_path / "music" / "Artist" / "Album" / "01.flac"
        self.lib.parent.mkdir(parents=True)
        if hardlink:
            os.link(self.src, self.lib)
        else:
            self.lib.write_bytes(b"audio-bytes")
        self.mode = mode

    def download(self, did: str = "dl-1", h: str = "abc", status: str = COMPLETED, *, placed: bool = True,
                 mode: Optional[str] = None, imported: bool = True, rule: tuple = ("global", 1.0, None)) -> dict[str, Any]:
        self.db.create_active_download(
            ActiveDownload(id=did, client_id="c1", title="Album [FLAC]", artist="Artist", download_hash=h, status=status,
                           target_path=str(self.lib) if imported else None)
        )
        if placed:
            self.db.set_download_placed_files(did, [str(self.lib)], mode or self.mode)
        if rule:
            self.db.set_download_seed_rule(did, None, rule[1], rule[2], rule[0])
        return self.db.get_active_download(did)

    def status(self, ratio: float = 2.0, secs: int = 100, content: Optional[str] = None) -> dict[str, Any]:
        return {"status": "completed", "ratio": ratio, "seeding_time_seconds": secs,
                "content_path": str(self.content) if content is None else content}

    def torrent(self, h: str = "abc", ratio: float = 2.0, state: str = "stalledUP") -> dict[str, Any]:
        return {"hash": h, "name": "Album", "size": 10, "ratio": ratio, "seeding_time": 100,
                "content_path": str(self.content), "state": state}


def run(db: Database, driver: FakeDriver) -> dict[str, Any]:
    with patch("trackseerr.seed_cleanup.get_acquisition_driver", return_value=driver):
        return sc.run_sweep(db, now=NOW)


# ------------------------------------------------------------------ migration


def test_migration_v59_maps_boolean_and_widens_finding_kinds(db):
    assert SCHEMA_VERSION >= 59
    for legacy, expected in ((1, "remove"), (0, "keep")):
        db.conn.execute("ALTER TABLE media_management_settings DROP COLUMN seed_complete_action")
        db.conn.execute("UPDATE media_management_settings SET delete_completed_transfers = ?", (legacy,))
        db._migration_v59(db.conn.cursor())
        assert db.get_media_management_settings()["seed_complete_action"] == expected
    db.upsert_library_health_findings(
        [{"kind": "weak_match", "cause": "c", "group_key": "g", "path": "/p"}], "2026-01-01T00:00:00+00:00"
    )
    db._migration_v59(db.conn.cursor())  # idempotent; findings survive the table rebuild
    assert db.count_library_health_findings() == 1
    db.upsert_library_health_findings(
        [{"kind": "orphan_torrent", "cause": "c", "group_key": "g", "path": "/o"},
         {"kind": "cleanup_failed", "cause": "c", "group_key": "g", "path": "/f"}], "2026-01-01T00:00:00+00:00"
    )
    assert db.count_library_health_findings() == 3
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(active_downloads)")}
    assert {"cleanup_attempts", "cleanup_error", "placed_files"} <= cols


def test_settings_accept_action_and_legacy_boolean(db):
    assert db.update_media_management_settings({"seed_complete_action": "remove_and_delete"})["seed_complete_action"] == "remove_and_delete"
    assert db.update_media_management_settings({"delete_completed_transfers": False})["seed_complete_action"] == "keep"
    assert db.update_media_management_settings({"delete_completed_transfers": True})["seed_complete_action"] == "remove"
    with pytest.raises(ValueError):
        db.update_media_management_settings({"seed_complete_action": "nuke"})


# ------------------------------------------------------------------ deletion_safe truth table


def _gate(world: World, *, mode: str = "copy", placed_mode: Optional[str] = "copy", unmatched=None,
          content: Optional[str] = None, placed: Optional[list[str]] = None) -> tuple[bool, str]:
    download = {"client_id": "c1", "placed_mode": placed_mode, "unmatched_files": unmatched or [],
                "placed_files": [str(world.lib)] if placed is None else placed}
    return deletion_safe(download, mode, world.db, world.status(content=content))


def test_deletion_safe_all_conditions_pass(db, tmp_path):
    ok, reason = _gate(World(db, tmp_path))
    assert ok is True and reason == "ok"


def test_deletion_safe_hardlink_with_private_copy_passes(db, tmp_path):
    assert _gate(World(db, tmp_path), mode="hardlink", placed_mode="hardlink")[0] is True


@pytest.mark.parametrize("mode,fragment", [("move", "import mode"), (None, "import mode")])
def test_deletion_safe_refuses_move_or_unknown_mode(db, tmp_path, mode, fragment):
    ok, reason = _gate(World(db, tmp_path), mode=mode)
    assert ok is False and fragment in reason


def test_deletion_safe_refuses_when_placed_mode_was_move_or_unknown(db, tmp_path):
    w = World(db, tmp_path)
    assert _gate(w, placed_mode="move")[0] is False
    assert _gate(w, placed_mode=None)[0] is False


def test_deletion_safe_refuses_held_unmatched_files(db, tmp_path):
    ok, reason = _gate(World(db, tmp_path), unmatched=[str(tmp_path / "x.flac")])
    assert ok is False and "unmatched" in reason


def test_deletion_safe_refuses_without_placed_record(db, tmp_path):
    ok, reason = _gate(World(db, tmp_path), placed=[])
    assert ok is False and "no record" in reason


def test_deletion_safe_refuses_missing_library_file(db, tmp_path):
    w = World(db, tmp_path)
    w.lib.unlink()
    ok, reason = _gate(w)
    assert ok is False and "missing" in reason


def test_deletion_safe_refuses_when_library_file_is_the_torrents_own_path(db, tmp_path):
    w = World(db, tmp_path)
    ok, reason = _gate(w, placed=[str(w.src)])
    assert ok is False and "torrent's own" in reason


def test_deletion_safe_refuses_library_symlink_into_torrent(db, tmp_path):
    w = World(db, tmp_path)
    w.lib.unlink()
    w.lib.symlink_to(w.src)
    assert _gate(w)[0] is False


def test_deletion_safe_refuses_shared_inode_for_hardlinks(db, tmp_path):
    w = World(db, tmp_path, hardlink=True)
    ok, reason = _gate(w, mode="hardlink", placed_mode="hardlink")
    assert ok is False and "inode" in reason
    # a recorded hardlink placement is still checked when the current setting says copy
    assert _gate(w, mode="copy", placed_mode="hardlink")[0] is False


def test_deletion_safe_refuses_unknown_or_empty_content_path(db, tmp_path):
    w = World(db, tmp_path)
    for content in ("", "   "):
        ok, reason = _gate(w, content=content)
        assert ok is False and "content path is unknown" in reason


def test_deletion_safe_refuses_content_inside_music_root(db, tmp_path):
    w = World(db, tmp_path)
    inside = tmp_path / "music" / "Artist" / "Album"
    assert _gate(w, content=str(inside))[0] is False
    assert _gate(w, content=str(tmp_path / "music"))[0] is False
    assert _gate(w, content=str(tmp_path))[0] is False  # contains the music root


def test_deletion_safe_refuses_dotdot_trick_into_music_root(db, tmp_path):
    w = World(db, tmp_path)
    assert _gate(w, content=str(tmp_path / "downloads" / ".." / "music" / "Artist"))[0] is False


# ------------------------------------------------------------------ settle with each action


def _settle(db: Database, w: World, action: str, *, mode: str = "copy", rule=("global", 1.0, None), ratio=2.0, secs=100,
            placed_mode: Optional[str] = None) -> tuple[str, MagicMock]:
    d = w.download(rule=rule, mode=placed_mode or mode)
    driver = MagicMock()
    driver.cleanup_completed.return_value = True
    ms = db.update_media_management_settings({"seed_complete_action": action})
    out = settle_transfer_after_import(driver, "abc", ms, mode, w.status(ratio, secs), d, db)
    return out, driver


def test_settle_keep_never_touches_client(db, tmp_path):
    out, driver = _settle(db, World(db, tmp_path), "keep")
    assert out == IMPORTED
    driver.cleanup_completed.assert_not_called()


def test_settle_remove_keeps_files(db, tmp_path):
    out, driver = _settle(db, World(db, tmp_path), "remove")
    assert out == IMPORTED
    driver.cleanup_completed.assert_called_once_with("abc", delete_files=False)


def test_settle_remove_and_delete_deletes_when_safe(db, tmp_path):
    out, driver = _settle(db, World(db, tmp_path), "remove_and_delete")
    assert out == IMPORTED
    driver.cleanup_completed.assert_called_once_with("abc", delete_files=True)


def test_settle_remove_and_delete_falls_back_in_move_mode(db, tmp_path):
    out, driver = _settle(db, World(db, tmp_path, mode="move"), "remove_and_delete", mode="move")
    assert out == IMPORTED
    driver.cleanup_completed.assert_called_once_with("abc", delete_files=False)


def test_settle_remove_and_delete_falls_back_when_gate_fails(db, tmp_path):
    w = World(db, tmp_path)
    w.lib.unlink()
    out, driver = _settle(db, w, "remove_and_delete")
    driver.cleanup_completed.assert_called_once_with("abc", delete_files=False)
    assert out == IMPORTED


def test_settle_remove_and_delete_never_deletes_without_db(db, tmp_path):
    w = World(db, tmp_path)
    d = w.download()
    driver = MagicMock()
    ms = db.update_media_management_settings({"seed_complete_action": "remove_and_delete"})
    settle_transfer_after_import(driver, "abc", ms, "copy", w.status(), d)
    driver.cleanup_completed.assert_called_once_with("abc", delete_files=False)


@pytest.mark.parametrize("action", ["remove", "remove_and_delete"])
def test_settle_unmet_indexer_rule_blocks_everything(db, tmp_path, action):
    out, driver = _settle(db, World(db, tmp_path), action, rule=("indexer", 2.0, 600), ratio=0.5, secs=60)
    assert out == COMPLETED
    driver.cleanup_completed.assert_not_called()


def test_settle_met_global_goal_vs_unmet(db, tmp_path):
    out, driver = _settle(db, World(db, tmp_path), "remove_and_delete", rule=("global", 3.0, None), ratio=1.0)
    assert out == COMPLETED
    driver.cleanup_completed.assert_not_called()


def test_settle_failed_removal_is_not_reported_as_removed(db, tmp_path):
    w = World(db, tmp_path)
    d = w.download()
    driver = MagicMock()
    driver.cleanup_completed.side_effect = RuntimeError("down")
    ms = db.get_media_management_settings()
    assert settle_transfer_after_import(driver, "abc", ms, "copy", w.status(), d, db) == IMPORTED  # sweep retries it


# ------------------------------------------------------------------ sweep: tracked


def test_sweep_tracked_removes_met_download_and_updates_status(db, tmp_path):
    w = World(db, tmp_path)
    w.download()
    driver = FakeDriver(torrents=[w.torrent()], statuses={"abc": w.status()})
    db.update_media_management_settings({"seed_complete_action": "remove_and_delete"})
    stats = run(db, driver)
    assert driver.removed == [("abc", True)]
    assert stats == {"evaluated": 1, "removed": 1, "deleted_files": 1, "orphans": 0, "failures": 0}
    assert db.get_active_download("dl-1")["status"] == IMPORTED


def test_sweep_tracked_leaves_unmet_goal_alone(db, tmp_path):
    w = World(db, tmp_path)
    w.download(rule=("global", 5.0, None))
    driver = FakeDriver(torrents=[w.torrent(ratio=1.0)], statuses={"abc": w.status(ratio=1.0)})
    stats = run(db, driver)
    assert driver.removed == [] and stats["removed"] == 0 and stats["evaluated"] == 1
    assert db.get_active_download("dl-1")["status"] == COMPLETED


def test_sweep_keep_action_never_removes_anything(db, tmp_path):
    w = World(db, tmp_path)
    w.download()
    db.update_media_management_settings({"seed_complete_action": "keep"})
    driver = FakeDriver(torrents=[w.torrent(), w.torrent(h="zzz")], statuses={"abc": w.status()})
    stats = run(db, driver)
    assert driver.removed == [] and stats["removed"] == 0
    assert stats["orphans"] == 1  # listing is read-only, so orphans are still reported


def test_sweep_skips_lidarr_mode(db, tmp_path):
    w = World(db, tmp_path)
    w.download()
    db.conn.execute("UPDATE media_management_settings SET library_mode = 'lidarr'")
    db.conn.commit()
    driver = FakeDriver(torrents=[w.torrent(h="zzz")], statuses={"abc": w.status()})
    stats = run(db, driver)
    assert stats["evaluated"] == 0 and driver.removed == []


def test_sweep_retry_then_finding_after_three_failures(db, tmp_path):
    w = World(db, tmp_path)
    w.download()
    driver = FakeDriver(torrents=[w.torrent()], statuses={"abc": w.status()}, fail=True)
    for attempt in (1, 2):
        stats = run(db, driver)
        row = db.get_active_download("dl-1")
        assert stats["failures"] == 1 and row["cleanup_attempts"] == attempt
        assert "secret" not in row["cleanup_error"]  # redacted
        assert db.count_library_health_findings() == 0
    run(db, driver)
    assert db.get_active_download("dl-1")["cleanup_attempts"] == 3
    found = [f for f in db.list_library_health_findings() if f["kind"] == "cleanup_failed"]
    assert len(found) == 1
    assert found[0]["path"] == "Album [FLAC]"
    assert found[0]["detail"]["download_id"] == "dl-1" and found[0]["detail"]["attempts"] == 3
    assert "secret" not in found[0]["detail"]["error"]
    # stops retrying until the user acts
    healthy = FakeDriver(torrents=[w.torrent()], statuses={"abc": w.status()})
    stats = run(db, healthy)
    assert healthy.removed == [] and stats["evaluated"] == 0


# ------------------------------------------------------------------ sweep: reconcile


def test_sweep_forgotten_imported_row_is_removed(db, tmp_path):
    w = World(db, tmp_path)
    w.download(status=IMPORTED)
    driver = FakeDriver(torrents=[w.torrent()])
    stats = run(db, driver)
    assert driver.removed == [("abc", False)]  # 'remove' action, files kept
    assert stats["removed"] == 1 and stats["orphans"] == 0
    assert db.get_active_download("dl-1")["status"] == IMPORTED


def test_sweep_forgotten_failed_row_keeps_its_status(db, tmp_path):
    w = World(db, tmp_path)
    w.download(status=DownloadStatus.FAILED.value, placed=False)
    run(db, FakeDriver(torrents=[w.torrent()]))
    assert db.get_active_download("dl-1")["status"] == DownloadStatus.FAILED.value


def test_sweep_forgotten_remove_and_delete_deletes_only_through_gate(db, tmp_path):
    w = World(db, tmp_path)
    w.download(status=IMPORTED)
    db.update_media_management_settings({"seed_complete_action": "remove_and_delete"})
    driver = FakeDriver(torrents=[w.torrent()])
    run(db, driver)
    assert driver.removed == [("abc", True)]


def test_sweep_forgotten_with_unmet_goal_or_unfinished_torrent_is_left(db, tmp_path):
    w = World(db, tmp_path)
    w.download(status=IMPORTED, rule=("global", 9.0, None))
    driver = FakeDriver(torrents=[w.torrent(ratio=1.0)])
    run(db, driver)
    assert driver.removed == []
    w.db.set_download_seed_rule("dl-1", None, 1.0, None, "global")
    driver = FakeDriver(torrents=[w.torrent(state="downloading")])
    run(db, driver)
    assert driver.removed == []


def test_sweep_forgotten_with_unmet_indexer_rule_is_left(db, tmp_path):
    w = World(db, tmp_path)
    w.download(status=IMPORTED, rule=("indexer", 5.0, 1000))
    driver = FakeDriver(torrents=[w.torrent(ratio=1.0)])
    run(db, driver)
    assert driver.removed == []


def test_sweep_history_only_match_is_removed_but_files_always_kept(db, tmp_path):
    w = World(db, tmp_path)
    db.conn.execute("INSERT INTO download_history (id, event, info_hash) VALUES ('h1', 'imported', 'ABC')")
    db.conn.commit()
    db.update_media_management_settings({"seed_complete_action": "remove_and_delete"})
    driver = FakeDriver(torrents=[w.torrent()])
    stats = run(db, driver)
    assert driver.removed == [("abc", False)]
    assert stats["orphans"] == 0


def test_sweep_orphan_becomes_finding_and_is_never_removed(db, tmp_path):
    w = World(db, tmp_path)
    for action in ("remove", "remove_and_delete"):
        db.update_media_management_settings({"seed_complete_action": action})
        driver = FakeDriver(torrents=[w.torrent(h="feed", ratio=9.9)])
        stats = run(db, driver)
        assert driver.removed == []
        assert stats["orphans"] == 1 and stats["removed"] == 0
    orphans = [f for f in db.list_library_health_findings() if f["kind"] == "orphan_torrent"]
    assert len(orphans) == 1
    assert orphans[0]["path"] == str(w.content)
    assert orphans[0]["detail"] == {"client_id": "c1", "hash": "feed", "name": "Album", "size": 10, "ratio": 9.9,
                                    "seeding_seconds": 100}


def test_sweep_drops_orphan_finding_when_torrent_is_gone(db, tmp_path):
    w = World(db, tmp_path)
    run(db, FakeDriver(torrents=[w.torrent(h="feed")]))
    assert db.count_library_health_findings() == 1
    run(db, FakeDriver(torrents=[]))
    assert db.count_library_health_findings() == 0


def test_sweep_keeps_orphan_findings_when_client_cannot_be_listed(db, tmp_path):
    w = World(db, tmp_path)
    run(db, FakeDriver(torrents=[w.torrent(h="feed")]))

    class Down(FakeDriver):
        def list_category(self):
            raise RuntimeError("HTTP 500")

    stats = run(db, Down())
    assert stats["failures"] == 1 and db.count_library_health_findings() == 1


def test_sweep_unsupported_driver_is_skipped(db, tmp_path):
    stats = run(db, FakeDriver(torrents=None))
    assert stats["orphans"] == 0 and stats["failures"] == 0


def test_sweep_live_rows_are_not_orphans_or_forgotten(db, tmp_path):
    w = World(db, tmp_path)
    w.download(status=DownloadStatus.DOWNLOADING.value, imported=False)
    driver = FakeDriver(torrents=[w.torrent()])
    stats = run(db, driver)
    assert stats["orphans"] == 0 and driver.removed == []


def test_sweep_is_exclusive_and_tracked(db):
    from trackseerr.job_tracker import job_tracker

    job_tracker.clear()
    assert sc._run_lock.acquire(blocking=False)
    try:
        with pytest.raises(sc.SeedCleanupBusy):
            sc.run_sweep(db, now=NOW)
        assert sc.start_sweep_async(db) is False
        assert sc.get_status()["running"] is True
    finally:
        sc._run_lock.release()
    run(db, FakeDriver(torrents=[]))
    assert any(j["task_id"] == "seed_cleanup" for j in job_tracker.snapshot()["recent"])
    last = sc.get_status()["last_run"]
    assert last["error"] is None and last["stats"]["failures"] == 0 and last["started_at"].startswith("2026-10-06")


# ------------------------------------------------------------------ routes


@pytest.fixture
def config(tmp_path: Path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


def _headers(db: Database, config: Config, *, admin: bool = True) -> dict[str, str]:
    user = db.upsert_user("a-1" if admin else "u-1", "admin_user" if admin else "user", "u@example.com", is_admin=admin)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=user["id"], username="x", is_admin=admin, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(db, config):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


def _orphan_finding(db: Database, w: World, content: Optional[str] = None) -> dict[str, Any]:
    run(db, FakeDriver(torrents=[{**w.torrent(h="feed"), "content_path": content or str(w.content)}]))
    return next(f for f in db.list_library_health_findings() if f["kind"] == "orphan_torrent")


def test_routes_require_admin(client, db, config):
    h = _headers(db, config, admin=False)
    assert client.post("/api/seed-cleanup/run", headers=h).status_code == 403
    assert client.get("/api/seed-cleanup/status", headers=h).status_code == 403
    assert client.post("/api/seed-cleanup/orphans/x/remove", json={}, headers=h).status_code == 403


def test_run_route_202_then_409_and_status_shape(client, db, config):
    h = _headers(db, config)
    assert sc._run_lock.acquire(blocking=False)
    try:
        assert client.post("/api/seed-cleanup/run", headers=h).status_code == 409
        assert client.get("/api/seed-cleanup/status", headers=h).json()["running"] is True
    finally:
        sc._run_lock.release()
    started = threading.Event()
    with patch.object(sc, "run_sweep", side_effect=lambda *a, **k: started.set()):
        res = client.post("/api/seed-cleanup/run", headers=h)
        assert res.status_code == 202 and res.json() == {"started": True}
        assert started.wait(5)
    body = client.get("/api/seed-cleanup/status", headers=h).json()
    assert set(body) == {"running", "last_run"}


def test_orphan_remove_route_honours_delete_files(client, db, config, tmp_path):
    h = _headers(db, config)
    w = World(db, tmp_path)
    for delete in (False, True):
        finding = _orphan_finding(db, w)
        driver = FakeDriver(torrents=[w.torrent(h="feed")])
        with patch("trackseerr.seed_cleanup.get_acquisition_driver", return_value=driver):
            res = client.post(f"/api/seed-cleanup/orphans/{finding['id']}/remove", json={"delete_files": delete}, headers=h)
        assert res.status_code == 200 and res.json() == {"removed": True, "delete_files": delete}
        assert driver.removed == [("feed", delete)]
        assert db.get_library_health_finding(finding["id"]) is None


def test_orphan_remove_refuses_delete_files_inside_music_root(client, db, config, tmp_path):
    h = _headers(db, config)
    w = World(db, tmp_path)
    inside = str(tmp_path / "music" / "Artist")
    finding = _orphan_finding(db, w, content=inside)
    driver = FakeDriver(torrents=[{**w.torrent(h="feed"), "content_path": inside}])
    with patch("trackseerr.seed_cleanup.get_acquisition_driver", return_value=driver):
        res = client.post(f"/api/seed-cleanup/orphans/{finding['id']}/remove", json={"delete_files": True}, headers=h)
        assert res.status_code == 409 and driver.removed == []
        assert db.get_library_health_finding(finding["id"]) is not None
        # removing just the torrent entry (files kept) is allowed
        res = client.post(f"/api/seed-cleanup/orphans/{finding['id']}/remove", json={"delete_files": False}, headers=h)
    assert res.status_code == 200 and driver.removed == [("feed", False)]


def test_orphan_remove_refuses_torrent_that_became_tracked(client, db, config, tmp_path):
    h = _headers(db, config)
    w = World(db, tmp_path)
    finding = _orphan_finding(db, w)
    w.download(h="feed", status=IMPORTED)
    driver = FakeDriver(torrents=[w.torrent(h="feed")])
    with patch("trackseerr.seed_cleanup.get_acquisition_driver", return_value=driver):
        res = client.post(f"/api/seed-cleanup/orphans/{finding['id']}/remove", json={"delete_files": False}, headers=h)
    assert res.status_code == 409 and driver.removed == []


def test_orphan_route_rejects_other_kinds_and_unknown_ids(client, db, config, tmp_path):
    h = _headers(db, config)
    assert client.post("/api/seed-cleanup/orphans/nope/remove", json={}, headers=h).status_code == 404
    db.upsert_library_health_findings([{"kind": "weak_match", "cause": "c", "group_key": "g", "path": "/p"}], "t")
    fid = db.list_library_health_findings()[0]["id"]
    assert client.post(f"/api/seed-cleanup/orphans/{fid}/remove", json={}, headers=h).status_code == 404
    assert client.post(f"/api/seed-cleanup/failed/{fid}/retry", headers=h).status_code == 404


def test_failed_retry_route_resets_and_reevaluates(client, db, config, tmp_path):
    h = _headers(db, config)
    w = World(db, tmp_path)
    w.download()
    broken = FakeDriver(torrents=[w.torrent()], statuses={"abc": w.status()}, fail=True)
    for _ in range(3):
        run(db, broken)
    finding = next(f for f in db.list_library_health_findings() if f["kind"] == "cleanup_failed")
    healthy = FakeDriver(torrents=[w.torrent()], statuses={"abc": w.status()})
    with patch("trackseerr.seed_cleanup.get_acquisition_driver", return_value=healthy):
        res = client.post(f"/api/seed-cleanup/failed/{finding['id']}/retry", headers=h)
    assert res.status_code == 200
    assert res.json() == {"retried": True, "removed": True, "status": IMPORTED, "attempts": 0, "error": None}
    assert healthy.removed == [("abc", False)]
    assert db.count_library_health_findings() == 0


def test_failed_retry_route_still_failing_counts_one_attempt(client, db, config, tmp_path):
    h = _headers(db, config)
    w = World(db, tmp_path)
    w.download()
    broken = FakeDriver(torrents=[w.torrent()], statuses={"abc": w.status()}, fail=True)
    for _ in range(3):
        run(db, broken)
    finding = next(f for f in db.list_library_health_findings() if f["kind"] == "cleanup_failed")
    with patch("trackseerr.seed_cleanup.get_acquisition_driver", return_value=broken):
        body = client.post(f"/api/seed-cleanup/failed/{finding['id']}/retry", headers=h).json()
    assert body["removed"] is False and body["attempts"] == 1 and "secret" not in body["error"]


def test_library_health_groups_and_count_include_new_kinds(client, db, config, tmp_path):
    h = _headers(db, config)
    w = World(db, tmp_path)
    _orphan_finding(db, w)
    db.upsert_library_health_findings(
        [{"kind": "cleanup_failed", "cause": "cleanup_failed", "group_key": "client:c1", "path": "x", "detail": {}}], "t"
    )
    assert client.get("/api/library-health/count", headers=h).json() == {"count": 2}
    body = client.get("/api/library-health", headers=h).json()
    assert {g["kind"] for g in body["groups"]} == {"orphan_torrent", "cleanup_failed"}
    assert all(g["suggestion"] for g in body["groups"])


def test_settings_api_returns_action_and_accepts_it(client, db, config):
    h = _headers(db, config)
    got = client.get("/api/settings/media-management", headers=h).json()["settings"]
    assert got["seed_complete_action"] in ("keep", "remove", "remove_and_delete") and "delete_completed_transfers" not in got
    res = client.post("/api/settings/media-management", json={"seed_complete_action": "remove_and_delete"}, headers=h)
    assert res.status_code == 200 and res.json()["seed_complete_action"] == "remove_and_delete"
    assert client.post("/api/settings/media-management", json={"seed_complete_action": "bogus"}, headers=h).status_code == 422
    res = client.post("/api/settings/media-management", json={"delete_completed_transfers": False}, headers=h)
    assert res.json()["seed_complete_action"] == "keep"


# ------------------------------------------------------------------ queue fields


def _seed_row(**kw: Any) -> dict[str, Any]:
    return {"status": "completed", "protocol": "torrent", "seed_rule_source": "indexer", "seed_ratio_target": 2.0,
            "seed_time_target_minutes": 600, "seed_ratio_current": 0.5, "seeding_seconds": 3600, **kw}


@pytest.mark.parametrize("action,expected", [("keep", None), ("remove", 540), ("remove_and_delete", 540)])
def test_queue_seeding_removes_in_minutes_and_action(action, expected):
    s = native_seeding(_seed_row(), {"seed_complete_action": action})
    assert s["action"] == action and s["removes_in_minutes"] == expected
    assert s["time_target_minutes"] == 600 and s["seeding_minutes"] == 60


def test_queue_seeding_without_time_target_or_when_overdue():
    assert native_seeding(_seed_row(seed_time_target_minutes=None), {"seed_complete_action": "remove"})["removes_in_minutes"] is None
    assert native_seeding(_seed_row(seeding_seconds=999999), {"seed_complete_action": "remove"})["removes_in_minutes"] == 0
    assert native_seeding(_seed_row(status="downloading"), {"seed_complete_action": "remove"}) is None


# ------------------------------------------------------------------ qBittorrent


def _http(payload: Any, status: int = 200) -> MagicMock:
    resp = MagicMock(status_code=status, text="Ok.", headers={})
    resp.json.return_value = payload
    http = MagicMock()
    http.__enter__.return_value = http
    http.get.return_value = resp
    http.headers = {}
    return http


def test_qbittorrent_list_category_request_and_parsing():
    http = _http([
        {"hash": "ABCDEF", "name": "A", "total_size": 5, "size": 4, "ratio": 1.5, "seeding_time": 90,
         "content_path": "/dl/A", "state": "stalledUP", "extra": 1},
        {"name": "no hash"},
    ])
    drv = QbittorrentDriver("http://192.168.1.9:8080", category="music-cat")
    with patch("trackseerr.clients.acquisition.qbittorrent.httpx.Client", return_value=http):
        out = drv.list_category()
    url = http.get.call_args
    assert url.args[0] == "http://192.168.1.9:8080/api/v2/torrents/info"
    assert url.kwargs["params"] == {"category": "music-cat"}
    assert out == [{"hash": "abcdef", "name": "A", "size": 5, "ratio": 1.5, "seeding_time": 90,
                    "content_path": "/dl/A", "state": "stalledUP"}]


def test_qbittorrent_list_category_refuses_empty_category_and_raises_on_http_error():
    assert QbittorrentDriver("http://192.168.1.9:8080", category="").list_category() is None
    http = _http([], status=500)
    with patch("trackseerr.clients.acquisition.qbittorrent.httpx.Client", return_value=http):
        with pytest.raises(RuntimeError):
            QbittorrentDriver("http://192.168.1.9:8080").list_category()


def test_qbittorrent_status_reports_content_path():
    http = _http([{"state": "stalledUP", "progress": 1, "content_path": "/dl/A", "save_path": "/dl", "ratio": 2.0,
                   "seeding_time": 5, "total_size": 9}])
    with patch("trackseerr.clients.acquisition.qbittorrent.httpx.Client", return_value=http):
        st = QbittorrentDriver("http://192.168.1.9:8080").get_status("ABC")
    assert st["content_path"] == "/dl/A" and st["save_path"] == "/dl"


def test_base_driver_list_category_is_unsupported():
    assert FakeDriver().__class__.__mro__[1].list_category(FakeDriver()) is None


# ------------------------------------------------------------------ worker + placed files


def test_worker_runs_once_after_delay_and_stops_promptly(db):
    calls = threading.Event()
    worker = sc.SeedCleanupWorker()
    with patch.object(sc, "run_sweep", side_effect=lambda *a, **k: calls.set()):
        assert worker.start(db, None, interval_seconds=3600, initial_delay=0.05) is True
        assert worker.start(db, None) is False
        assert calls.wait(5)
    worker.stop()
    assert worker.is_running() is False


def test_worker_stop_during_initial_delay_never_sweeps(db):
    worker = sc.SeedCleanupWorker()
    with patch.object(sc, "run_sweep") as sweep:
        worker.start(db, None, interval_seconds=3600, initial_delay=60)
        worker.stop()
    sweep.assert_not_called()
    assert worker.is_running() is False


def test_worker_survives_a_failing_sweep(db):
    worker = sc.SeedCleanupWorker()
    with patch.object(sc, "run_sweep", side_effect=RuntimeError("db gone")):
        assert worker.run_once(db) is False
    with patch.object(sc, "run_sweep", side_effect=sc.SeedCleanupBusy("x")):
        assert worker.run_once(db) is False


def test_placed_files_record_roundtrip_and_merge(db, tmp_path):
    w = World(db, tmp_path)
    w.download(placed=False)
    assert db.get_active_download("dl-1")["placed_files"] == []
    db.add_download_placed_files("dl-1", ["/a", "/b"], "copy")
    db.add_download_placed_files("dl-1", ["/b", "/c"], "hardlink")
    row = db.get_active_download("dl-1")
    assert row["placed_files"] == ["/a", "/b", "/c"] and row["placed_mode"] == "hardlink"
    db.add_download_placed_files("dl-1", ["/d"], "move")
    assert db.get_active_download("dl-1")["placed_mode"] == "move"


# ------------------------------------------------------------------ persisted last run + System -> Tasks


def test_last_run_is_persisted_and_survives_a_restart(db):
    run(db, FakeDriver(torrents=[]))
    sc._last_run = None  # simulate a fresh process
    last = sc.get_status(db)["last_run"]
    assert last["stats"]["failures"] == 0 and last["error"] is None and last["finished_at"]
    assert sc.get_status()["last_run"] is None  # without a db there is nothing to fall back to
    db.set_kv(sc.LAST_RUN_KV, "{not json")
    assert sc.get_status(db)["last_run"] is None  # corrupt value is ignored, not raised


def test_tasks_registry_lists_both_tasks_with_last_run(client, db, config):
    h = _headers(db, config)
    run(db, FakeDriver(torrents=[]))
    tasks = {t["id"]: t for t in client.get("/api/system/tasks", headers=h).json()}
    assert tasks["seed_cleanup"]["can_trigger"] and tasks["seed_cleanup"]["interval"] == "Every 24h"
    assert tasks["seed_cleanup"]["last_run_at"] and tasks["seed_cleanup"]["status"] == "idle"
    assert tasks["library_health"]["can_trigger"] and tasks["library_health"]["status"] == "idle"


def test_run_seed_cleanup_task_dispatches_and_reports_already_running(client, db, config):
    h = _headers(db, config)
    started = threading.Event()
    with patch.object(sc, "run_sweep", side_effect=lambda *a, **k: started.set()):
        res = client.post("/api/system/tasks/seed_cleanup/run", headers=h)
        assert res.status_code == 200 and "dispatched" in res.json()["message"]
        assert started.wait(5)
    assert sc._run_lock.acquire(blocking=False)
    try:
        res = client.post("/api/system/tasks/seed_cleanup/run", headers=h)
        assert res.json() == {"success": True, "message": "Task 'seed_cleanup' is already running"}
    finally:
        sc._run_lock.release()


def test_run_library_health_task_dispatches_and_reports_already_running(client, db, config):
    h = _headers(db, config)
    with patch("trackseerr.library_health.start_check_async", return_value=True) as start:
        res = client.post("/api/system/tasks/library_health/run", headers=h)
        assert res.status_code == 200 and "dispatched" in res.json()["message"]
        assert start.call_args.kwargs["music_root"] == tmp_music(db)
    with patch("trackseerr.library_health.start_check_async", return_value=False):
        res = client.post("/api/system/tasks/library_health/run", headers=h)
    assert res.json() == {"success": True, "message": "Task 'library_health' is already running"}


def tmp_music(db: Database) -> Path:
    return Path(db.get_media_management_settings()["root_folder_path"])


def test_run_seed_cleanup_task_refused_in_lidarr_mode(client, db, config):
    h = _headers(db, config)
    db.conn.execute("UPDATE media_management_settings SET library_mode = 'lidarr'")
    db.conn.commit()
    assert client.post("/api/system/tasks/seed_cleanup/run", headers=h).status_code == 409

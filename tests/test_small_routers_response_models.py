"""Strict-mode backstop for recycle-bin, seed-cleanup, library-health and queue responses (wave 2b)."""

import json
from unittest.mock import patch

from trackseerr import recycle_bin as rb
from trackseerr import seed_cleanup as sc
from trackseerr.models import DownloadStatus
from tests._rm_helpers import admin, alice, client, config, db, ok  # noqa: F401


def test_recycle_bin_status_and_empty(client, admin, alice):
    status = ok(client.get("/api/recycle-bin/status", headers=admin))
    assert set(status) == {"running", "last_run"}
    ok(client.post("/api/recycle-bin/empty", json={"confirm": True}, headers=admin))
    after = ok(client.get("/api/recycle-bin/status", headers=admin))
    assert after["last_run"]["emptied"] is True and set(after["last_run"]) == {"finished_at", "removed", "errors", "emptied"}
    assert client.get("/api/recycle-bin/status", headers=alice).status_code == 403


def test_recycle_bin_empty_body_shape(client, admin):
    body = ok(client.post("/api/recycle-bin/empty", json={"confirm": True}, headers=admin))
    assert set(body) == {"removed", "errors", "skipped_reason"} and isinstance(body["errors"], list)


def test_seed_cleanup_status_with_persisted_last_run(client, db, admin, alice):
    record = {
        "started_at": "2026-10-06T10:00:00+00:00", "finished_at": "2026-10-06T10:00:05+00:00",
        "stats": {"evaluated": 4, "removed": 1, "deleted_files": 1, "orphans": 2, "failures": 0}, "error": None,
    }
    db.set_kv(sc.LAST_RUN_KV, json.dumps(record))
    with patch.object(sc, "_last_run", None):
        body = ok(client.get("/api/seed-cleanup/status", headers=admin))
    assert body == {"running": False, "last_run": record}
    assert client.get("/api/seed-cleanup/status", headers=alice).status_code == 403


def test_seed_cleanup_retry_failed_shape(client, db, admin):
    finding_rows = [{
        "kind": sc.KIND_CLEANUP_FAILED, "server_kind": None, "cause": "cleanup_failed", "group_key": "g",
        "path": "dl-1", "detail": {"download_id": "dl-1"},
    }]
    db.upsert_library_health_findings(finding_rows, "2026-10-06T10:00:00+00:00")
    finding = db.list_library_health_findings()[0]
    with patch.object(sc, "retry_failed", return_value={
        "retried": True, "removed": False, "status": "completed", "attempts": 0, "error": None,
    }):
        body = ok(client.post(f"/api/seed-cleanup/failed/{finding['id']}/retry", headers=admin))
    assert body["retried"] is True and body["error"] is None


def _health_rows():
    return [
        {"kind": "server_unindexed", "server_kind": "plex", "cause": "not_in_server", "group_key": "Artist/Album",
         "path": "/music/Artist/Album/01.flac", "detail": {"size": 123}},
        {"kind": "server_stale", "server_kind": "plex", "cause": "missing_on_disk", "group_key": "Other",
         "path": "/music/Other/02.flac", "detail": None},
    ]


def test_library_health_full_flow(client, db, admin, alice):
    empty = ok(client.get("/api/library-health", headers=admin))
    assert empty["findings"] == [] and empty["mapping"] is None and empty["server"] is None and empty["last_run"] is None
    db.upsert_library_health_findings(_health_rows(), "2026-10-06T10:00:00+00:00")
    run_id = db.conn.execute(
        "INSERT INTO library_health_runs (started_at, finished_at, server_kind, disk_files, server_files, unindexed, stale)"
        " VALUES ('2026-10-06T10:00:00','2026-10-06T10:01:00','plex',5,4,1,1)"
    ).lastrowid
    db.conn.commit()
    ok(client.put("/api/library-health/mapping", json={"server_prefix": "/data", "local_prefix": "/music"}, headers=admin))
    body = ok(client.get("/api/library-health", headers=admin))
    assert body["count"] == 2 and body["last_run"]["id"] == run_id
    assert body["mapping"] == {"server_prefix": "/data", "local_prefix": "/music", "auto": False}
    assert {g["cause"] for g in body["groups"]} == {"not_in_server", "missing_on_disk"}
    assert ok(client.get("/api/library-health/count", headers=admin)) == {"count": 2}
    assert ok(client.put("/api/library-health/weekly", json={"enabled": False}, headers=admin)) == {"weekly": False}
    assert ok(client.post("/api/library-health/dismiss", json={"path": "/music/Other/02.flac", "scope": "file"}, headers=admin)) == {
        "dismissed": True, "removed": 1,
    }
    assert ok(client.delete("/api/library-health/mapping", headers=admin)) == {"mapping": None}
    assert client.get("/api/library-health", headers=alice).status_code == 403


def test_library_health_with_media_server_and_check(client, db, admin):
    from types import SimpleNamespace

    from trackseerr.api.app import create_app  # noqa: F401
    from trackseerr.api.dependencies import get_active_media_server
    from trackseerr import library_health as lh

    server = SimpleNamespace(kind="plex", capabilities=SimpleNamespace(file_paths=True))
    client.app.dependency_overrides[get_active_media_server] = lambda: server
    body = ok(client.get("/api/library-health", headers=admin))
    assert body["server"] == {"kind": "plex", "file_paths": True}
    with patch.object(lh, "start_check_async", return_value=True):
        assert ok(client.post("/api/library-health/check", headers=admin), 202) == {"started": True}


def test_queue_list_and_cancel(client, db, admin):
    db.create_download_client({
        "id": "c1", "name": "qbit", "driver_type": "qbittorrent", "host_url": "http://qb:8080", "enabled": True,
    })
    db.create_active_download({
        "id": "dl-1", "client_id": "c1", "download_hash": "abc", "title": "Airbag", "artist": "Radiohead",
        "status": DownloadStatus.DOWNLOADING.value, "progress": 0.4, "size_bytes": 1000,
    })
    items = ok(client.get("/api/queue", headers=admin))
    assert items[0]["client_name"] == "qbit" and items[0]["status"] == "downloading"
    with patch("trackseerr.api.routes.queue.get_acquisition_driver"):
        assert ok(client.delete("/api/queue/dl-1", headers=admin)) == {"status": "cancelled", "id": "dl-1"}

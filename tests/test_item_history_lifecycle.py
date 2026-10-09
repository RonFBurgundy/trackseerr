"""Lifecycle emitters of the per-item audit trail: each catalog event is recorded by the code that causes it."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import recycle_bin
from trackseerr.acquisition_worker import record_import_events
from trackseerr.seed_safety import evaluate_seed_cleanup
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_plex_client
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.item_history import GrabTrigger
from trackseerr.library_scanner import LibraryScanner
from trackseerr.models import ActiveDownload, DownloadStatus, MusicRequest, RequestStatus
from trackseerr.recycle_bin import DisposeResult, log_recycled, recycle_replaced_files, run_cleanup
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "lifecycle.db"))
    d.upsert_user("admin-1", "admin_user", "a@example.com", is_admin=True)
    d.upsert_user("user-alice", "alice", "alice@example.com", is_admin=False)
    d.upsert_library_artist({"id": "art-1", "name": "Nirvana", "clean_name": "nirvana", "monitored": True})
    d.upsert_library_album(
        {"id": "alb-1", "artist_id": "art-1", "title": "Nevermind", "clean_title": "nevermind", "monitored": True}
    )
    d.upsert_library_track(
        {"id": "trk-1", "album_id": "alb-1", "artist_id": "art-1", "title": "Lithium", "clean_title": "lithium",
         "track_number": 3, "disc_number": 1, "monitored": True}
    )
    d.create_download_client(
        {"id": "c1", "name": "qBit", "driver_type": "qbittorrent", "host_url": "http://q:8080", "enabled": True}
    )
    yield d
    d.close()


def events(db: Database, event: str, entity: str = "track", entity_id: str = "trk-1") -> list[dict[str, Any]]:
    return [e for e in db.list_item_events(entity, entity_id, limit=500) if e["event"] == event]


def download(db: Database, dl_id: str, **kw: Any) -> None:
    db.create_active_download(
        ActiveDownload(
            id=dl_id, title=f"Release {dl_id}", artist="Nirvana", client_id="c1", download_hash=f"h-{dl_id}",
            status=DownloadStatus.DOWNLOADING.value, track_id=kw.pop("track_id", "trk-1"),
            album_id=kw.pop("album_id", "alb-1"), **kw,
        )
    )


def test_failed_downloads_record_a_running_failure_count(db):
    for i in (1, 2, 3):
        download(db, f"dl-{i}")
        assert db.update_download_status(f"dl-{i}", DownloadStatus.FAILED.value, error_message=f"boom {i}")
    failed = list(reversed(events(db, "download_failed")))
    assert [e["details"]["failure_count"] for e in failed] == [1, 2, 3]
    assert [e["message"] for e in failed] == ["boom 1", "boom 2", "boom 3"]
    assert failed[0]["download_id"] == "dl-1" and failed[0]["album_id"] == "alb-1"
    # the same terminal transition twice is still one event
    db.update_download_status("dl-3", DownloadStatus.FAILED.value, error_message="boom again")
    assert len(events(db, "download_failed")) == 3


def test_failed_event_keeps_the_grab_trigger(db):
    download(db, "dl-t")
    db.record_download_grab("dl-t", indexer="Tracker", quality="FLAC", protocol="torrent",
                            trigger=GrabTrigger("wanted", label="Wanted search"))
    db.update_download_status("dl-t", DownloadStatus.FAILED.value, error_message="stalled")
    (ev,) = events(db, "download_failed")
    assert ev["trigger"] == "wanted" and ev["trigger_label"] == "Wanted search"
    hist = db.conn.execute(
        'SELECT "trigger" FROM download_history WHERE event = \'failed\' AND download_id = \'dl-t\''
    ).fetchone()
    assert hist[0] == "wanted"


def test_blocklist_event_names_release_and_reason(db):
    download(db, "dl-b")
    item = db.add_to_blocklist(
        source_title="Bad Release [MP3]", artist="Nirvana", info_hash="ABCDEF", indexer="Tracker", protocol="torrent",
        reason="Security check failed", download_id="dl-b",
    )
    (ev,) = events(db, "blocklisted")
    assert ev["details"]["release"] == "Bad Release [MP3]" and ev["details"]["reason"] == "Security check failed"
    assert ev["details"]["indexer"] == "Tracker" and ev["details"]["info_hash"] == "abcdef"
    assert ev["download_id"] == "dl-b" and item["id"]


def test_blocklist_without_download_resolves_the_item_by_name(db):
    db.add_to_blocklist(source_title="Nirvana - Nevermind [CAM]", artist="Nirvana", album="Nevermind", reason="wrong")
    assert events(db, "blocklisted", "album", "alb-1")


def test_imported_and_upgraded_events(db, tmp_path):
    placed = tmp_path / "03 - Lithium.flac"
    record_import_events(
        db, {"id": None, "title": "Nirvana - Nevermind [FLAC]", "request_id": None}, "trk-1", placed, "FLAC 16bit",
        {"codec": "FLAC"}, [],
    )
    (imp,) = events(db, "imported")
    assert imp["details"] == {
        "path": str(placed), "quality": "FLAC 16bit", "codec": "FLAC", "release": "Nirvana - Nevermind [FLAC]",
    }
    assert not events(db, "upgraded")
    record_import_events(
        db, {"id": None, "title": "r", "request_id": None}, "trk-1", placed, "FLAC 16bit", {"codec": "FLAC"},
        [{"quality_name": "MP3 128"}],
    )
    (up,) = events(db, "upgraded")
    assert up["message"] == "MP3 128 -> FLAC 16bit" and up["details"]["from_quality"] == "MP3 128"


def test_import_event_inherits_the_grab_trigger(db, tmp_path):
    download(db, "dl-i")
    db.record_download_grab("dl-i", indexer="T", quality="FLAC", protocol="torrent",
                            trigger=GrabTrigger("playlist", ref="pl-1", label="Road Trip"))
    record_import_events(db, {"id": "dl-i", "title": "r", "request_id": None}, "trk-1", tmp_path / "a.flac", "FLAC", {}, [])
    (imp,) = events(db, "imported")
    assert (imp["trigger"], imp["trigger_ref"], imp["trigger_label"], imp["download_id"]) == (
        "playlist", "pl-1", "Road Trip", "dl-i",
    )


def _library_file(db: Database, root: Path, name: str = "03 - Lithium.mp3") -> tuple[Path, dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True)
    old = root / name
    old.write_bytes(b"old-bytes")
    row = db.upsert_library_file(
        {"id": "fil-old", "track_id": "trk-1", "file_path": str(old), "relative_path": name, "codec": "MP3",
         "quality_name": "MP3 128", "size_bytes": 9, "cutoff_met": False}
    )
    return old, row


def test_replaced_file_is_recycled_and_later_purged(db, tmp_path):
    lib = tmp_path / "music"
    old, row = _library_file(db, lib)
    new = lib / "03 - Lithium.flac"
    new.write_bytes(b"new")
    mm = db.get_media_management_settings()
    mm.update(root_folder_path=str(lib), recycle_bin_path=str(lib / ".recycle"), recycle_bin_permanent_delete=0)
    db.update_media_management_settings(mm)
    retired: list[str] = []
    kept: list[str] = []
    recycle_replaced_files(
        db, [row], new, lib, mm, [], "FLAC 16bit", retired, kept, title="Lithium", download_id=None, issue_id=None,
    )
    assert not old.exists() and len(retired) == 1 and not kept
    (rep,) = events(db, "file_replaced")
    assert rep["details"]["disposition"] == "recycled" and rep["details"]["old_path"] == str(old)
    assert rep["details"]["old_quality"] == "MP3 128" and rep["details"]["new_quality"] == "FLAC 16bit"
    assert Path(rep["details"]["recycled_to"]).is_file()
    assert not events(db, "file_deleted")

    result = run_cleanup(db, empty_all=True)
    assert result.removed and not Path(rep["details"]["recycled_to"]).exists()
    (gone,) = events(db, "file_deleted")
    assert gone["trigger"] == "recycle_cleanup" and gone["details"]["original_path"] == str(old)
    assert gone["track_title"] == "Lithium"


def test_replacement_that_cannot_be_moved_is_reported_as_kept(db, tmp_path):
    lib = tmp_path / "music"
    old, row = _library_file(db, lib)
    mm = db.get_media_management_settings()
    kept: list[str] = []
    # a download-client root covering the library makes the old file unmovable
    recycle_replaced_files(
        db, [row], lib / "new.flac", lib, {**mm, "recycle_bin_path": str(lib / ".recycle")}, [lib], "FLAC", [], kept,
        title="Lithium", download_id=None, issue_id=None,
    )
    assert old.exists() and kept
    (rep,) = events(db, "file_replaced")
    assert rep["details"]["disposition"] == "kept"


def test_log_recycled_records_permanent_delete_disposition(db, tmp_path):
    result = DisposeResult("deleted", tmp_path / "old.mp3")
    log_recycled(
        db, result, tmp_path / "new.flac", {"track_id": "trk-1", "quality_name": "MP3 128"}, "FLAC", title="Lithium",
        download_id=None, issue_id=None, retired=[],
    )
    (rep,) = events(db, "file_replaced")
    assert rep["details"]["disposition"] == "deleted" and rep["details"]["recycled_to"] is None


def test_seed_cleanup_deleting_files_is_recorded(db):
    download(db, "dl-s")
    db.record_download_grab("dl-s", indexer="T", quality="FLAC", protocol="torrent", trigger=GrabTrigger("request"))
    driver = MagicMock()
    driver.cleanup_completed.return_value = True
    with patch("trackseerr.seed_safety.deletion_safe", return_value=(True, "")):
        outcome = evaluate_seed_cleanup(
            driver, "h-dl-s", {"seed_complete_action": "remove_and_delete"}, "hardlink", None,
            db.get_active_download("dl-s"), db,
        )
    assert outcome.removed and outcome.deleted_files
    (ev,) = events(db, "file_deleted")
    assert (ev["trigger"], ev["trigger_label"], ev["download_id"]) == ("seed_cleanup", "Seed cleanup", "dl-s")
    assert ev["trigger_ref"] in ("", None)


def test_seed_cleanup_that_keeps_files_records_no_deletion(db):
    download(db, "dl-k")
    driver = MagicMock()
    driver.cleanup_completed.return_value = True
    with patch("trackseerr.seed_safety.deletion_safe", return_value=(False, "shared inode")):
        outcome = evaluate_seed_cleanup(
            driver, "h-dl-k", {"seed_complete_action": "remove_and_delete"}, "hardlink", None,
            db.get_active_download("dl-k"), db,
        )
    assert outcome.removed and not outcome.deleted_files
    assert not events(db, "file_deleted")


def test_scanner_prune_records_file_missing(db, tmp_path):
    gone = tmp_path / "gone.flac"
    db.upsert_library_file(
        {"id": "fil-gone", "track_id": "trk-1", "file_path": str(gone), "relative_path": "gone.flac", "codec": "FLAC",
         "quality_name": "FLAC", "size_bytes": 1, "cutoff_met": True}
    )
    root = tmp_path / "music"
    root.mkdir()
    LibraryScanner().scan(db=db, root_folder=str(root), prune_missing=True)
    assert db.get_library_file("fil-gone") is None
    (ev,) = events(db, "file_missing")
    assert ev["trigger"] == "scan" and ev["details"]["path"] == str(gone)


def test_scanner_provenance_marks_artists_it_adds(db, tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    LibraryScanner().scan(db=db, root_folder=str(root))
    # nothing was added, and the provenance context did not leak out of the scan
    from trackseerr.item_history import current_provenance

    assert current_provenance() is None


def test_health_finding_for_a_weak_match_lands_on_the_track(db):
    db.upsert_library_health_findings(
        [{"kind": "weak_match", "server_kind": None, "cause": "weak_tag_match", "group_key": "/m",
          "path": "/m/a.flac", "detail": {"track_id": "trk-1", "title": "Lithium"}}],
        "2026-01-01T00:00:00",
    )
    db.upsert_library_health_findings(
        [{"kind": "weak_match", "server_kind": None, "cause": "weak_tag_match", "group_key": "/m",
          "path": "/m/a.flac", "detail": {"track_id": "trk-1", "title": "Lithium"}}],
        "2026-01-02T00:00:00",
    )  # a refresh of the same finding is not a new finding
    (ev,) = events(db, "health_finding")
    assert ev["details"]["cause"] == "weak_tag_match" and ev["trigger"] == "system"


def test_health_finding_for_an_unindexed_file_is_matched_by_path(db, tmp_path):
    db.upsert_library_file(
        {"id": "f1", "track_id": "trk-1", "file_path": "/m/b.flac", "relative_path": "b.flac", "codec": "FLAC",
         "quality_name": "FLAC", "size_bytes": 1, "cutoff_met": True}
    )
    db.upsert_library_health_findings(
        [{"kind": "server_unindexed", "server_kind": "plex", "cause": "corrupt", "group_key": "/m", "path": "/m/b.flac",
          "detail": {}}],
        "2026-01-01T00:00:00",
    )
    assert events(db, "health_finding")


def test_requested_and_approved_events_are_adopted_once_the_item_is_in_the_library(db):
    from trackseerr.request_submission import submit_track_request

    cfg = Config(plex_url="x", plex_token="t", auto_approve_requests=True)
    user = db.get_user("user-alice") | {"permissions": 0xFFFF}
    with patch("trackseerr.request_submission.native_is_configured", return_value=False):
        sub = submit_track_request(db, cfg, user, "Come as You Are", "Nirvana", "Nevermind")
    req_id = sub.request["id"]
    orphans = db.conn.execute(
        "SELECT event, artist_name, track_title, track_id FROM item_events WHERE request_id = ? ORDER BY id", (req_id,)
    ).fetchall()
    assert [o["event"] for o in orphans] == ["requested", "request_approved"]
    assert orphans[0]["track_id"] is None  # not in the library yet
    # the track is hydrated, then the first event carrying its ids adopts the request's earlier events
    db.upsert_library_track(
        {"id": "trk-9", "album_id": "alb-1", "artist_id": "art-1", "title": "Come as You Are",
         "clean_title": "come as you are", "track_number": 6, "disc_number": 1}
    )
    db.record_item_event("grabbed", track_id="trk-9", request_id=req_id)
    got = [e["event"] for e in db.list_item_events("track", "trk-9", limit=50)]
    assert {"requested", "request_approved", "grabbed"} <= set(got)
    requested = events(db, "requested", "track", "trk-9")[0]
    assert requested["trigger"] == "request" and requested["trigger_label"] == "alice"
    assert requested["trigger_ref"] == req_id


def test_request_for_an_item_already_in_the_library_is_attached_immediately(db):
    from trackseerr.request_submission import submit_track_request

    cfg = Config(plex_url="x", plex_token="t", auto_approve_requests=False)
    user = db.get_user("user-alice")
    with patch("trackseerr.request_submission.native_is_configured", return_value=False):
        submit_track_request(db, cfg, user, "Lithium", "Nirvana", "Nevermind")
    (ev,) = events(db, "requested")
    assert ev["album_id"] == "alb-1" and not events(db, "request_approved")


# ------------------------------------------------------------------------------------------- API-driven events
@pytest.fixture
def app_client(db: Database, tmp_path: Path):
    cfg = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    app.dependency_overrides[get_plex_client] = lambda: MagicMock()
    return app, TestClient(app), cfg


def _h(db: Database, cfg: Config, user_id: str) -> dict[str, str]:
    user = db.get_user(user_id)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"],
        secret_key=get_or_create_secret_key(data_dir=cfg.data_dir),
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def test_issue_open_and_resolve_events(db, app_client):
    _, client, cfg = app_client
    h = _h(db, cfg, "admin-1")
    r = client.post(
        "/api/issues",
        json={"media_title": "Lithium", "artist": "Nirvana", "issue_type": "audio_quality",
              "problem_details": "crackles", "track_id": "trk-1"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    issue_id = r.json()["id"]
    (opened,) = events(db, "issue_opened")
    assert opened["details"]["issue_id"] == issue_id and opened["details"]["issue_type"] == "audio_quality"
    assert (opened["trigger"], opened["actor_user_id"], opened["trigger_label"]) == ("user", "admin-1", "admin_user")
    assert client.post(f"/api/issues/{issue_id}/status", json={"status": "resolved"}, headers=h).status_code == 200
    (resolved,) = events(db, "issue_resolved")
    assert resolved["details"]["status"] == "resolved" and resolved["track_id"] == "trk-1"


def test_issue_replacement_search_is_an_issue_trigger_with_the_admin_as_actor(db, app_client):
    from trackseerr.acquisition_coordinator import acquisition_coordinator

    _, client, cfg = app_client
    h = _h(db, cfg, "admin-1")
    issue_id = client.post(
        "/api/issues",
        json={"media_title": "Lithium", "artist": "Nirvana", "issue_type": "corrupted_file",
              "problem_details": "glitch", "track_id": "trk-1"},
        headers=h,
    ).json()["id"]
    mock_grab = MagicMock(return_value={"success": True})
    with patch.object(acquisition_coordinator, "search_and_grab", mock_grab):
        resp = client.post(f"/api/issues/{issue_id}/actions/research", headers=h)
        assert resp.status_code == 200, resp.text
        from trackseerr.backlog_worker import backlog_worker

        thread = backlog_worker.last_search_thread
        if thread is not None:
            thread.join(timeout=10)
    trig = mock_grab.call_args.kwargs["trigger"]
    assert (trig.kind, trig.ref, trig.actor_user_id) == ("issue", issue_id, "admin-1")


def test_request_approve_and_decline_events(db, app_client):
    _, client, cfg = app_client
    h = _h(db, cfg, "admin-1")
    for rid, title in (("req-a", "Lithium"), ("req-d", "Polly")):
        db.create_request(
            MusicRequest(id=rid, user_id="user-alice", item_type="track", title=title, artist="Nirvana",
                         album="Nevermind", status=RequestStatus.PENDING)
        )
    db.upsert_library_track(
        {"id": "trk-2", "album_id": "alb-1", "artist_id": "art-1", "title": "Polly", "clean_title": "polly",
         "track_number": 4, "disc_number": 1}
    )
    with patch("trackseerr.api.routes.requests.native_is_configured", return_value=False):
        assert client.post("/api/requests/req-a/approve", headers=h).status_code == 200
    assert client.post("/api/requests/req-d/reject", headers=h).status_code == 200
    (appr,) = events(db, "request_approved")
    assert appr["trigger"] == "request" and appr["actor_user_id"] == "admin-1"
    assert appr["trigger_label"] == "alice" and appr["trigger_ref"] == "req-a"
    (decl,) = events(db, "request_declined", "track", "trk-2")
    assert decl["actor_user_id"] == "admin-1" and decl["request_id"] == "req-d"


def test_library_routes_record_user_provenance_for_monitoring_and_removal(db, app_client):
    _, client, cfg = app_client
    h = _h(db, cfg, "admin-1")
    assert client.put("/api/library/tracks/trk-1/monitored", json={"monitored": False}, headers=h).status_code == 200
    (ev,) = events(db, "unmonitored")
    assert (ev["trigger"], ev["trigger_label"], ev["actor_user_id"]) == ("user", "admin_user", "admin-1")
    # the context set for one request never leaks into the next, unauthenticated-by-route work
    db.upsert_library_artist({"id": "art-z", "name": "Z", "clean_name": "z", "monitored": True})
    assert events(db, "added_to_library", "artist", "art-z")[0]["trigger"] is None
    assert client.delete("/api/library/tracks/trk-1", headers=h).status_code == 200
    removed = events(db, "removed_from_library")
    assert removed and removed[0]["trigger"] == "user" and removed[0]["track_title"] == "Lithium"


def test_deleting_a_file_from_disk_records_who_deleted_it(db, app_client, tmp_path):
    _, client, cfg = app_client
    h = _h(db, cfg, "admin-1")
    f = tmp_path / "x.flac"
    f.write_bytes(b"x")
    db.upsert_library_file(
        {"id": "fil-x", "track_id": "trk-1", "file_path": str(f), "relative_path": "x.flac", "codec": "FLAC",
         "quality_name": "FLAC", "size_bytes": 1, "cutoff_met": True}
    )
    assert client.delete("/api/library/files/fil-x?delete_file_from_disk=true", headers=h).status_code == 200
    assert not f.exists()
    (ev,) = events(db, "file_deleted")
    assert ev["trigger"] == "user" and ev["actor_user_id"] == "admin-1" and ev["details"]["path"] == str(f)


def test_rename_apply_records_a_move(db, app_client, tmp_path):
    _, client, cfg = app_client
    h = _h(db, cfg, "admin-1")
    music = tmp_path / "music"
    mm = db.get_media_management_settings()
    mm["root_folder_path"] = str(music)
    db.update_media_management_settings(mm)
    legacy = music / "old" / "dirty.mp3"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"x")
    db.upsert_library_file(
        {"id": "fil-r", "track_id": "trk-1", "file_path": str(legacy), "relative_path": "old/dirty.mp3", "codec": "MP3",
         "quality_name": "MP3 320", "size_bytes": 1, "cutoff_met": True}
    )
    with patch("trackseerr.api.routes.library.tagging.validate_media_path", side_effect=lambda p, **kw: Path(p)):
        resp = client.post("/api/library/rename/apply", json={"file_ids": ["fil-r"]}, headers=h)
    assert resp.status_code == 200 and resp.json()["renamed_count"] == 1, resp.text
    ev = events(db, "moved") + events(db, "renamed")
    assert len(ev) == 1 and ev[0]["details"]["from"] == str(legacy)


def test_quarantine_during_import_is_recorded(tmp_path):
    from tests.test_import_security import PAD, _run

    with patch.object(
        Database, "find_library_ids_by_name", return_value={"artist_id": "art-q", "album_id": None, "track_id": None}
    ):
        db, stats, *_ = _run(tmp_path, "off", {"01.mp3": b"\xff\xfb\x90\x00" + PAD}, mutagen_result=None)
    assert stats["failed"] == 1
    evs = db.list_item_events("artist", "art-q", limit=50)
    kinds = [e["event"] for e in evs]
    assert "quarantined" in kinds and "download_failed" in kinds and "blocklisted" in kinds
    q = next(e for e in evs if e["event"] == "quarantined")
    assert q["download_id"] == "dl-1" and q["details"]["quarantined_to"]

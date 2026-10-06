"""Phase 3 backend: Activity (queue / history / blocklist) and Wanted (missing / cutoff), native and Lidarr mode."""

from plex_playlist_sync.storage import SCHEMA_VERSION
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import activity_service
from plex_playlist_sync.acquisition_coordinator import AcquisitionCoordinator
from plex_playlist_sync.acquisition_worker import AcquisitionWorker
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.backlog_worker import backlog_worker
from plex_playlist_sync.config import Config
from plex_playlist_sync.library_manager import ModeChanged
from plex_playlist_sync.models import (
    AcquisitionSearchResult,
    ActiveDownload,
    DownloadClientConfig,
    DownloadStatus,
    IndexerConfig,
    MusicRequest,
)
from plex_playlist_sync.storage import Database

API_KEY = "lidarr-secret-key-abcdef123456"
NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------------------- fixtures


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def users(test_db):
    return {
        "admin": test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True),
        "alice": test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False),
    }


@pytest.fixture
def api(test_db, test_config):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    return TestClient(app)


def _headers(user, db, config):
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_h(test_db, test_config, users):
    return _headers(users["admin"], test_db, test_config)


def _lidarr_mode(db):
    db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": API_KEY})
    db.update_media_management_settings({"library_mode": "lidarr"})


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.content = b"x" if payload is not None else b""
    r.text = ""
    return r


def _page(records, total=None):
    return {"page": 1, "pageSize": 50, "totalRecords": len(records) if total is None else total, "records": records}


def _client_row(db, cid="cli-qbit", driver="qbittorrent"):
    db.create_download_client(
        DownloadClientConfig(id=cid, name=f"{driver}-{cid}", driver_type=driver, host_url="http://c:1", enabled=True)
    )


def _dl(db, did, artist="Artist", title="Release", status="downloading", progress=0.0, size=1000, created=None, **extra):
    _client_row(db) if db.get_download_client("cli-qbit") is None else None
    db.create_active_download(
        ActiveDownload(
            id=did, client_id="cli-qbit", title=title, artist=artist, status=status, progress=progress,
            size_bytes=size, **extra,
        )
    )
    if created:
        db.conn.execute("UPDATE active_downloads SET created_at = ? WHERE id = ?", (created, did))
        db.conn.commit()


def _library(db):
    """Artist A (monitored) with tracks: missing, missing-but-unmonitored, with file meeting cutoff, below cutoff."""
    db.upsert_library_artist({"id": "ar-1", "name": "Alpha", "clean_name": "alpha", "monitored": True})
    db.upsert_library_artist({"id": "ar-2", "name": "Beta", "clean_name": "beta", "monitored": True})
    db.upsert_library_album(
        {"id": "al-1", "artist_id": "ar-1", "title": "First", "clean_title": "first", "year": 2001, "monitored": True}
    )
    db.upsert_library_album(
        {"id": "al-2", "artist_id": "ar-2", "title": "Second", "clean_title": "second", "year": 2010, "monitored": True}
    )

    def track(tid, album, artist, title, monitored=True):
        db.upsert_library_track(
            {"id": tid, "album_id": album, "artist_id": artist, "title": title, "clean_title": title.lower(),
             "track_number": 1, "disc_number": 1, "monitored": monitored}
        )

    track("t-missing", "al-1", "ar-1", "Missing One")
    track("t-missing-b", "al-2", "ar-2", "Zed Missing")
    track("t-unmonitored", "al-1", "ar-1", "Ignored", monitored=False)
    track("t-ok", "al-1", "ar-1", "Has File")
    track("t-low", "al-2", "ar-2", "Low Quality")

    def file(fid, tid, quality, cutoff_met):
        db.upsert_library_file(
            {"id": fid, "track_id": tid, "file_path": f"/m/{fid}.mp3", "relative_path": f"{fid}.mp3",
             "codec": "MP3", "quality_name": quality, "size_bytes": 10, "cutoff_met": cutoff_met}
        )

    file("f-ok", "t-ok", "FLAC 16bit", True)
    file("f-low", "t-low", "MP3 192", False)


# -------------------------------------------------------------------------------------- migration / history seed


class TestMigrationAndSeed:
    def test_schema_has_v33_objects(self, test_db):
        assert test_db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
        cols = {r[1] for r in test_db.conn.execute("PRAGMA table_info(active_downloads)")}
        assert {"progress_updated_at", "indexer", "quality", "protocol"} <= cols
        assert "last_searched_at" in {r[1] for r in test_db.conn.execute("PRAGMA table_info(library_tracks)")}
        assert test_db.conn.execute("SELECT COUNT(*) FROM download_history").fetchone()[0] == 0

    def test_seed_is_idempotent_and_only_terminal_rows(self, test_db):
        _client_row(test_db)
        _dl(test_db, "dl-done", status="downloading")
        _dl(test_db, "dl-fail", status="downloading")
        _dl(test_db, "dl-live", status="downloading")
        test_db.conn.execute("DELETE FROM download_history")
        test_db.conn.execute("UPDATE active_downloads SET status='imported' WHERE id='dl-done'")
        test_db.conn.execute("UPDATE active_downloads SET status='failed', error_message='nope' WHERE id='dl-fail'")
        test_db.conn.commit()

        first = test_db.seed_download_history()
        assert first == 4  # grabbed + terminal event, for the two terminal rows
        assert test_db.seed_download_history() == 0
        rows = test_db.conn.execute("SELECT id, event, message FROM download_history ORDER BY id").fetchall()
        assert {r["event"] for r in rows} == {"grabbed", "imported", "failed"}
        assert all("dl-live" not in r["id"] for r in rows)
        assert [r["message"] for r in rows if r["event"] == "failed"] == ["nope"]

    def test_migration_backfills_progress_clock_only_where_null_and_non_terminal(self, tmp_path):
        path = tmp_path / "stall.db"
        db = Database(str(path))
        for did in ("dl-live", "dl-kept", "dl-done"):
            _dl(db, did, status="downloading")
        db.conn.execute("UPDATE active_downloads SET status='imported' WHERE id='dl-done'")
        db.conn.execute("UPDATE active_downloads SET progress_updated_at = NULL")
        db.conn.execute("UPDATE active_downloads SET progress_updated_at = '2020-01-01 00:00:00' WHERE id='dl-kept'")
        db.conn.execute("UPDATE active_downloads SET created_at = '2020-01-01 00:00:00'")
        db.conn.execute("DELETE FROM schema_migrations WHERE version >= 33")
        db.conn.commit()
        db.close()
        db = Database(str(path))
        try:
            clocks = {r["id"]: r["progress_updated_at"] for r in db.conn.execute("SELECT id, progress_updated_at FROM active_downloads")}
            assert clocks["dl-live"] and clocks["dl-live"] >= "2026"  # started now, not at the 2020 created_at
            assert clocks["dl-kept"] == "2020-01-01 00:00:00"  # an existing clock is never reset
            assert clocks["dl-done"] is None  # terminal rows are left alone
            row = db.get_native_queue_item("dl-live")
            assert activity_service.native_queue_record(row, datetime.now(timezone.utc))["stalled"] is False
        finally:
            db.close()

    def test_migration_seeds_existing_database(self, tmp_path):
        path = tmp_path / "old.db"
        db = Database(str(path))
        _dl(db, "dl-old", status="downloading")
        db.conn.execute("UPDATE active_downloads SET status='imported' WHERE id='dl-old'")
        db.conn.execute("DELETE FROM download_history")
        db.conn.execute("DELETE FROM schema_migrations WHERE version >= 33")
        db.conn.execute("DROP TABLE download_history")
        db.conn.commit()
        db.close()
        db = Database(str(path))  # re-applies v33 over the already-altered columns
        try:
            assert db.conn.execute("SELECT COUNT(*) FROM download_history").fetchone()[0] == 2
        finally:
            db.close()
        db = Database(str(path))  # and a plain reopen adds nothing
        try:
            assert db.conn.execute("SELECT COUNT(*) FROM download_history").fetchone()[0] == 2
        finally:
            db.close()


# ----------------------------------------------------------------------------------------------- native: queue


class TestNativeQueue:
    def _seed(self, db):
        _dl(db, "dl-1", artist="Charlie", title="c rel", progress=0.5, size=300, created="2026-10-01 10:00:00")
        _dl(db, "dl-2", artist="alpha", title="a rel", progress=0.9, size=100, created="2026-10-02 10:00:00")
        _dl(db, "dl-3", artist="Bravo", title="b rel", progress=0.1, size=200, status="queued", created="2026-10-03 10:00:00")
        _dl(db, "dl-4", artist="Delta", title="d rel", progress=0.2, size=400, created="2026-10-04 10:00:00")
        _dl(db, "dl-5", artist="Echo", title="e rel", status="imported")  # terminal: not in the queue
        _client_row(db, "cli-l", "lidarr")
        db.create_active_download(
            ActiveDownload(id="dl-lid", client_id="cli-l", title="x", artist="x", status="downloading")
        )  # Lidarr-driver rows never show natively

    def test_default_order_and_record_shape(self, api, test_db, admin_h):
        self._seed(test_db)
        res = api.get("/api/activity/queue", headers=admin_h)
        assert res.status_code == 200
        body = res.json()
        assert body["mode"] == "native" and body["page"] == 1 and body["page_size"] == 50
        assert body["total"] == 4 and body["sort_key"] == "added_at" and body["sort_dir"] == "desc"
        assert [r["id"] for r in body["records"]] == ["dl-4", "dl-3", "dl-2", "dl-1"]
        rec = body["records"][3]
        assert set(rec) == {
            "id", "source", "artist", "album", "title", "release_title", "item_type", "quality", "protocol", "indexer",
            "client", "status", "progress", "size_bytes", "sizeleft_bytes", "eta_seconds", "added_at", "stalled",
            "stalled_reason", "messages", "request_id", "download_id", "needs_manual_import", "unmatched_count", "seeding",
        }
        assert rec["source"] == "native" and rec["sizeleft_bytes"] == 150 and rec["progress"] == 0.5
        assert rec["added_at"] == "2026-10-01T10:00:00Z"

    @pytest.mark.parametrize(
        "key,direction,expected",
        [
            ("artist", "asc", ["dl-2", "dl-3", "dl-1", "dl-4"]),
            ("artist", "desc", ["dl-4", "dl-1", "dl-3", "dl-2"]),
            ("progress", "desc", ["dl-2", "dl-1", "dl-4", "dl-3"]),
            ("size_bytes", "asc", ["dl-2", "dl-3", "dl-1", "dl-4"]),
            ("status", "asc", ["dl-1", "dl-2", "dl-4", "dl-3"]),
            ("title", "asc", ["dl-1", "dl-2", "dl-3", "dl-4"]),
        ],
    )
    def test_sorting(self, api, test_db, admin_h, key, direction, expected):
        self._seed(test_db)
        res = api.get(f"/api/activity/queue?sort_key={key}&sort_dir={direction}", headers=admin_h)
        ids = [r["id"] for r in res.json()["records"]]
        if key == "title":  # item title falls back to the release title when nothing resolves it
            assert ids == sorted(ids, key=lambda i: test_db.get_active_download(i)["title"])
        else:
            assert ids == expected

    def test_paging(self, api, test_db, admin_h):
        self._seed(test_db)
        p1 = api.get("/api/activity/queue?page=1&page_size=3&sort_key=artist&sort_dir=asc", headers=admin_h).json()
        p2 = api.get("/api/activity/queue?page=2&page_size=3&sort_key=artist&sort_dir=asc", headers=admin_h).json()
        p3 = api.get("/api/activity/queue?page=3&page_size=3&sort_key=artist&sort_dir=asc", headers=admin_h).json()
        assert [len(p["records"]) for p in (p1, p2, p3)] == [3, 1, 0]
        assert p1["total"] == p2["total"] == p3["total"] == 4
        assert [r["id"] for r in p1["records"] + p2["records"]] == ["dl-2", "dl-3", "dl-1", "dl-4"]

    @pytest.mark.parametrize(
        "qs",
        ["sort_key=bogus", "sort_key=artist;DROP", "page_size=0", "page_size=201", "page=0", "sort_dir=sideways"],
    )
    def test_bad_params_422(self, api, test_db, admin_h, qs):
        assert api.get(f"/api/activity/queue?{qs}", headers=admin_h).status_code == 422

    def test_stalled_detection_with_frozen_time(self, api, test_db, admin_h):
        _dl(test_db, "dl-stuck", status="downloading", progress=0.3)
        _dl(test_db, "dl-fresh", status="downloading", progress=0.3)
        _dl(test_db, "dl-warn", status="warning")
        _dl(test_db, "dl-queued-old", status="queued")
        for did, ts in (
            ("dl-stuck", "2026-10-04 11:25:00"),  # 35 min ago
            ("dl-fresh", "2026-10-04 11:45:00"),  # 15 min ago
            ("dl-queued-old", "2026-10-03 00:00:00"),  # queued is not "downloading": never stalled by the clock
        ):
            test_db.conn.execute("UPDATE active_downloads SET progress_updated_at = ? WHERE id = ?", (ts, did))
        test_db.conn.commit()
        with patch.object(activity_service, "_now", return_value=NOW):
            recs = {r["id"]: r for r in api.get("/api/activity/queue", headers=admin_h).json()["records"]}
        assert recs["dl-stuck"]["stalled"] is True and "35 minutes" in recs["dl-stuck"]["stalled_reason"]
        assert recs["dl-fresh"]["stalled"] is False and recs["dl-fresh"]["stalled_reason"] is None
        assert recs["dl-warn"]["stalled"] is True
        assert recs["dl-queued-old"]["stalled"] is False

    def test_exactly_thirty_minutes_is_stalled(self):
        row = {"status": "downloading", "progress_updated_at": "2026-10-04 11:30:00"}
        assert activity_service.native_stalled(row, NOW)[0] is True
        row["progress_updated_at"] = "2026-10-04 11:30:01"
        assert activity_service.native_stalled(row, NOW)[0] is False

    def test_progress_clock_only_moves_when_progress_changes(self, test_db):
        _dl(test_db, "dl-p", status="downloading", progress=0.4)
        test_db.conn.execute("UPDATE active_downloads SET progress_updated_at = '2000-01-01 00:00:00'")
        test_db.conn.commit()
        test_db.update_download_progress("dl-p", 0.4)
        assert test_db.get_active_download("dl-p")["progress_updated_at"] == "2000-01-01 00:00:00"
        test_db.update_download_progress("dl-p", 0.41)
        assert test_db.get_active_download("dl-p")["progress_updated_at"] != "2000-01-01 00:00:00"
        test_db.conn.execute("UPDATE active_downloads SET progress_updated_at = '2000-01-01 00:00:00'")
        test_db.conn.commit()
        test_db.update_download_status("dl-p", "importing")  # a status change restarts the clock too
        assert test_db.get_active_download("dl-p")["progress_updated_at"] != "2000-01-01 00:00:00"

    def test_queue_title_album_resolution(self, api, test_db, admin_h):
        _library(test_db)
        _dl(test_db, "dl-t", artist="Alpha", title="Alpha - Missing One [FLAC]", track_id="t-missing", album_id="al-1")
        rec = api.get("/api/activity/queue", headers=admin_h).json()["records"][0]
        assert rec["title"] == "Missing One" and rec["album"] == "First"
        assert rec["release_title"] == "Alpha - Missing One [FLAC]"

    def test_delete_with_blocklist_and_remove_from_client(self, api, test_db, admin_h):
        _dl(test_db, "dl-x", artist="Art", title="Art - Rel [FLAC]", download_hash="HASH123")
        test_db.set_download_release_meta("dl-x", indexer="Prowlarr", quality="FLAC 16bit", protocol="torrent")
        driver = MagicMock()
        with patch.object(activity_service, "get_acquisition_driver", return_value=driver):
            res = api.delete("/api/activity/queue/dl-x?remove_from_client=true&blocklist=true", headers=admin_h)
        assert res.status_code == 200 and res.json()["success"] is True
        driver.cancel.assert_called_once_with("HASH123")
        assert test_db.get_active_download("dl-x") is None
        bl = test_db.list_blocklist()
        assert len(bl) == 1 and bl[0]["source_title"] == "Art - Rel [FLAC]" and bl[0]["info_hash"] == "hash123"
        assert test_db.is_blocklisted(release_title="Art - Rel [FLAC]")
        events = {r["event"] for r in test_db.conn.execute("SELECT event FROM download_history")}
        assert {"deleted", "blocklisted"} <= events

    def test_delete_without_blocklist_or_client_removal(self, api, test_db, admin_h):
        _dl(test_db, "dl-y")
        driver = MagicMock()
        with patch.object(activity_service, "get_acquisition_driver", return_value=driver):
            res = api.delete("/api/activity/queue/dl-y?remove_from_client=false&blocklist=false", headers=admin_h)
        assert res.status_code == 200
        driver.cancel.assert_not_called()
        assert test_db.list_blocklist() == []
        assert test_db.get_active_download("dl-y") is None

    def test_delete_unknown_404(self, api, admin_h):
        assert api.delete("/api/activity/queue/nope", headers=admin_h).status_code == 404

    def test_retry_researches_the_request_and_drops_old_row(self, api, test_db, admin_h, users):
        test_db.create_request(
            MusicRequest(id="req-1", user_id="user-alice", item_type="track", title="In Bloom", artist="Nirvana")
        )
        _dl(test_db, "dl-r", artist="Nirvana", title="Nirvana - In Bloom [MP3]", request_id="req-1")
        with patch.object(activity_service, "get_acquisition_driver", return_value=MagicMock()), patch(
            "plex_playlist_sync.acquisition_coordinator.acquisition_coordinator.search_and_grab",
            return_value={"success": True, "release": "Nirvana - In Bloom [FLAC]", "download_id": "dl-new", "download_hash": "h2"},
        ) as search:
            res = api.post("/api/activity/queue/dl-r/retry", headers=admin_h)
        assert res.status_code == 200 and res.json()["success"] is True
        kwargs = search.call_args.kwargs
        assert kwargs["artist"] == "Nirvana" and kwargs["title"] == "In Bloom" and kwargs["request_id"] == "req-1"
        assert test_db.get_active_download("dl-r") is None
        assert [r["event"] for r in test_db.list_download_history(1, 10, "asc")[0]] == ["deleted"]

    def test_retry_same_hash_does_not_cancel_the_new_download(self, api, test_db, admin_h):
        _dl(test_db, "dl-same", artist="A", title="A - T", download_hash="samehash")
        driver = MagicMock()
        with patch.object(activity_service, "get_acquisition_driver", return_value=driver), patch(
            "plex_playlist_sync.acquisition_coordinator.acquisition_coordinator.search_and_grab",
            return_value={"success": True, "release": "r", "download_id": "dl-new", "download_hash": "samehash"},
        ):
            res = api.post("/api/activity/queue/dl-same/retry", headers=admin_h)
        assert res.json()["success"] is True
        driver.cancel.assert_not_called()
        assert test_db.get_active_download("dl-same") is None

    def test_retry_reports_no_result_without_failing(self, api, test_db, admin_h):
        _dl(test_db, "dl-r2", artist="A", title="A - T")
        with patch.object(activity_service, "get_acquisition_driver", return_value=MagicMock()), patch(
            "plex_playlist_sync.acquisition_coordinator.acquisition_coordinator.search_and_grab",
            return_value={"success": False, "message": "No acceptable releases found"},
        ):
            res = api.post("/api/activity/queue/dl-r2/retry", headers=admin_h)
        assert res.status_code == 200 and res.json() == {"success": False, "message": "No acceptable releases found"}
        # Nothing was found: the stuck row stays, nothing was cancelled, no "deleted" event was written.
        assert test_db.get_active_download("dl-r2") is not None
        assert test_db.list_download_history(1, 10, "asc", event="deleted")[1] == 0

    def test_retry_unknown_404(self, api, admin_h):
        assert api.post("/api/activity/queue/nope/retry", headers=admin_h).status_code == 404


# ------------------------------------------------------------------------- native: history written by the flow


def _grab(db, release="Nirvana - In Bloom [FLAC]", min_score=None):
    db.create_indexer(
        IndexerConfig(id="idx-1", name="Prowlarr", indexer_type="torznab", host_url="http://prowlarr:9696", enabled=True)
    ) if not db.list_indexers() else None
    if db.get_download_client("client-qbit") is None:
        db.create_download_client(
            DownloadClientConfig(
                id="client-qbit", name="qBittorrent", driver_type="qbittorrent", host_url="http://qb:8080", enabled=True
            )
        )
    candidate = AcquisitionSearchResult(
        download_id="guid-1", title=release, artist="Nirvana", item_type="track", size_bytes=35 * 1024 * 1024,
        magnet_url="magnet:?xt=urn:btih:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        source="torznab", protocol="torrent", seeders=5, extra={"indexer_name": "Prowlarr"},
    )
    coordinator = AcquisitionCoordinator()
    driver = MagicMock()
    driver.download.return_value = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    with patch.object(coordinator, "search_all_indexers", return_value=[candidate]), patch(
        "plex_playlist_sync.acquisition_coordinator.get_acquisition_driver", return_value=driver
    ):
        return coordinator.search_and_grab(artist="Nirvana", title="In Bloom", db=db, min_score=min_score)


class TestNativeHistory:
    def test_grab_then_import_writes_events(self, api, test_db, admin_h):
        res = _grab(test_db)
        assert res["success"]
        test_db.update_download_status(res["download_id"], "importing")
        test_db.update_download_status(res["download_id"], "imported")
        test_db.update_download_status(res["download_id"], "imported")  # repeat: no duplicate event
        body = api.get("/api/activity/history?sort_dir=asc", headers=admin_h).json()
        assert [r["event"] for r in body["records"]] == ["grabbed", "imported"]
        grabbed = body["records"][0]
        assert grabbed["source"] == "native" and grabbed["artist"] == "Nirvana"
        assert grabbed["indexer"] == "Prowlarr" and grabbed["client"] == "qBittorrent"
        assert grabbed["quality"] and grabbed["title"] == "Nirvana - In Bloom [FLAC]"
        assert set(grabbed) == {
            "id", "source", "event", "artist", "album", "title", "release_title", "quality", "indexer", "client",
            "date", "message", "can_mark_failed",
        }
        assert grabbed["can_mark_failed"] is True and body["records"][1]["can_mark_failed"] is False

    def test_grab_then_worker_failure_writes_failed_and_blocklisted(self, api, test_db, admin_h, tmp_path):
        res = _grab(test_db)
        driver = MagicMock()
        driver.get_status.return_value = {"status": "failed", "error_message": "Unpack failed"}
        test_db.update_media_management_settings({"staging_folder_path": str(tmp_path)})
        with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=driver):
            stats = AcquisitionWorker().poll_once(db=test_db, staging_dir=str(tmp_path))
        assert stats["failed"] == 1
        events = [r["event"] for r in api.get("/api/activity/history?sort_dir=asc", headers=admin_h).json()["records"]]
        assert events[0] == "grabbed" and events.count("failed") == 1 and "blocklisted" in events
        failed = api.get("/api/activity/history?event=failed", headers=admin_h).json()
        assert failed["total"] == 1 and failed["records"][0]["message"] == "Unpack failed"
        assert test_db.get_active_download(res["download_id"])["status"] == "failed"

    def test_upgrade_grab_writes_upgraded(self, api, test_db, admin_h):
        _grab(test_db, min_score=0)
        body = api.get("/api/activity/history?event=upgraded", headers=admin_h).json()
        assert body["total"] == 1

    def test_event_filter_paging_and_422(self, api, test_db, admin_h):
        for i in range(5):
            test_db.record_download_event("grabbed", artist=f"A{i}", title=f"T{i}")
            test_db.conn.execute(
                "UPDATE download_history SET created_at = ? WHERE artist = ?", (f"2026-10-0{i + 1} 10:00:00", f"A{i}")
            )
        test_db.record_download_event("failed", artist="Z")
        test_db.conn.commit()
        res = api.get("/api/activity/history?event=grabbed&page_size=2&page=3&sort_dir=asc", headers=admin_h).json()
        assert res["total"] == 5 and [r["artist"] for r in res["records"]] == ["A4"]
        newest = api.get("/api/activity/history?event=grabbed&page_size=2", headers=admin_h).json()
        assert [r["artist"] for r in newest["records"]] == ["A4", "A3"]
        assert api.get("/api/activity/history?event=exploded", headers=admin_h).status_code == 422
        assert api.get("/api/activity/history?sort_key=artist", headers=admin_h).status_code == 422
        assert api.get("/api/activity/history?page_size=500", headers=admin_h).status_code == 422

    def test_mark_failed_blocklists_and_researches(self, api, test_db, admin_h):
        res = _grab(test_db)
        grabbed = api.get("/api/activity/history", headers=admin_h).json()["records"][0]
        driver = MagicMock()
        with patch.object(activity_service, "get_acquisition_driver", return_value=driver), patch(
            "plex_playlist_sync.acquisition_coordinator.acquisition_coordinator.search_and_grab",
            return_value={"success": False, "message": "No acceptable releases found"},
        ) as search:
            out = api.post(f"/api/activity/history/{grabbed['id']}/failed", headers=admin_h)
        assert out.status_code == 200
        assert out.json() == {"success": True, "message": "Marked failed; no replacement found yet"}
        driver.cancel.assert_called_once()
        assert test_db.get_active_download(res["download_id"])["status"] == "failed"
        assert test_db.is_blocklisted(release_title="Nirvana - In Bloom [FLAC]")
        search.assert_called_once()
        events = [r["event"] for r in api.get("/api/activity/history?sort_dir=asc", headers=admin_h).json()["records"]]
        assert events.count("failed") == 1 and "blocklisted" in events

    def test_mark_failed_with_replacement_reports_grab(self, api, test_db, admin_h):
        _grab(test_db)
        grabbed = api.get("/api/activity/history", headers=admin_h).json()["records"][0]
        with patch.object(activity_service, "get_acquisition_driver", return_value=MagicMock()), patch(
            "plex_playlist_sync.acquisition_coordinator.acquisition_coordinator.search_and_grab",
            return_value={"success": True, "release": "Better Release"},
        ):
            out = api.post(f"/api/activity/history/{grabbed['id']}/failed", headers=admin_h).json()
        assert out == {"success": True, "message": "Marked as failed and blocklisted. Grabbed 'Better Release'"}

    def test_mark_failed_twice_is_409_and_writes_no_duplicates(self, api, test_db, admin_h):
        _grab(test_db)
        grabbed = api.get("/api/activity/history", headers=admin_h).json()["records"][0]
        assert grabbed["can_mark_failed"] is True
        with patch.object(activity_service, "get_acquisition_driver", return_value=MagicMock()), patch(
            "plex_playlist_sync.acquisition_coordinator.acquisition_coordinator.search_and_grab",
            return_value={"success": False, "message": "none"},
        ):
            assert api.post(f"/api/activity/history/{grabbed['id']}/failed", headers=admin_h).status_code == 200
            blocklist_before = len(test_db.list_blocklist())
            events_before = test_db.list_download_history(1, 50, "asc")[1]
            again = api.post(f"/api/activity/history/{grabbed['id']}/failed", headers=admin_h)
        assert again.status_code == 409 and "already" in again.json()["detail"]
        assert len(test_db.list_blocklist()) == blocklist_before
        assert test_db.list_download_history(1, 50, "asc")[1] == events_before
        rows = api.get("/api/activity/history?sort_dir=asc", headers=admin_h).json()["records"]
        assert [r["can_mark_failed"] for r in rows if r["event"] == "grabbed"] == [False]

    def test_mark_failed_rejects_non_grab_and_superseded_grab(self, api, test_db, admin_h):
        _client_row(test_db)
        _dl(test_db, "dl-s", status="queued")
        old = test_db.record_download_event("grabbed", download_id="dl-s")
        new = test_db.record_download_event("grabbed", download_id="dl-s")
        imported = test_db.record_download_event("imported", download_id="dl-s")
        flags = {r["id"]: r["can_mark_failed"] for r in api.get("/api/activity/history", headers=admin_h).json()["records"]}
        assert flags[old] is False and flags[new] is True and flags[imported] is False
        assert api.post(f"/api/activity/history/{old}/failed", headers=admin_h).status_code == 409
        assert api.post(f"/api/activity/history/{imported}/failed", headers=admin_h).status_code == 409

    def test_worker_failed_grab_cannot_be_marked_failed(self, api, test_db, admin_h, tmp_path):
        _grab(test_db)
        driver = MagicMock()
        driver.get_status.return_value = {"status": "failed", "error_message": "Unpack failed"}
        test_db.update_media_management_settings({"staging_folder_path": str(tmp_path)})
        with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=driver):
            AcquisitionWorker().poll_once(db=test_db, staging_dir=str(tmp_path))
        grabbed = api.get("/api/activity/history?event=grabbed", headers=admin_h).json()["records"][0]
        assert grabbed["can_mark_failed"] is False
        assert api.post(f"/api/activity/history/{grabbed['id']}/failed", headers=admin_h).status_code == 409

    def test_mark_failed_unknown_404(self, api, admin_h):
        assert api.post("/api/activity/history/nope/failed", headers=admin_h).status_code == 404


# --------------------------------------------------------------------------------------- native: blocklist


class TestNativeBlocklist:
    def test_list_sort_page_and_delete(self, api, test_db, admin_h):
        test_db.conn.execute("DELETE FROM download_history")
        items = []
        for artist, day in (("charlie", 1), ("Alpha", 3), ("bravo", 2)):
            it = test_db.add_to_blocklist(
                source_title=f"{artist} rel", artist=artist, album=f"{artist} album", indexer="Idx",
                protocol="torrent", reason="bad",
            )
            test_db.conn.execute("UPDATE download_blocklist SET created_at = ? WHERE id = ?", (f"2026-10-0{day} 09:00:00", it["id"]))
            items.append(it)
        test_db.conn.commit()
        by_artist = api.get("/api/activity/blocklist?sort_key=artist&sort_dir=asc", headers=admin_h).json()
        assert [r["artist"] for r in by_artist["records"]] == ["Alpha", "bravo", "charlie"]
        rec = by_artist["records"][0]
        assert set(rec) == {
            "id", "source", "artist", "album", "title", "release_title", "quality", "indexer", "protocol", "reason", "date",
        }
        assert rec["release_title"] == "Alpha rel" and rec["reason"] == "bad" and rec["date"] == "2026-10-03T09:00:00Z"
        by_date = api.get("/api/activity/blocklist?page_size=2", headers=admin_h).json()
        assert by_date["total"] == 3 and [r["artist"] for r in by_date["records"]] == ["Alpha", "bravo"]
        assert api.get("/api/activity/blocklist?sort_key=title", headers=admin_h).status_code == 422

        assert api.delete(f"/api/activity/blocklist/{items[0]['id']}", headers=admin_h).status_code == 200
        assert test_db.get_blocklist_item(items[0]["id"]) is None
        assert api.delete("/api/activity/blocklist/bl-nope", headers=admin_h).status_code == 404


# ------------------------------------------------------------------------------------------- native: wanted


class TestNativeWanted:
    def test_missing_excludes_unmonitored_and_files(self, api, test_db, admin_h):
        _library(test_db)
        body = api.get("/api/wanted/missing", headers=admin_h).json()
        assert body["mode"] == "native" and body["total"] == 2 and body["sort_key"] == "artist"
        assert [r["id"] for r in body["records"]] == ["t-missing", "t-missing-b"]
        rec = body["records"][0]
        assert rec == {
            "id": "t-missing", "source": "native", "artist": "Alpha", "album": "First", "album_id": "al-1",
            "title": "Missing One",
            "item_type": "track", "release_date": "2001", "monitored": True, "last_searched_at": None,
        }

    def test_missing_excludes_unmonitored_artist_and_album(self, api, test_db, admin_h):
        _library(test_db)
        test_db.conn.execute("UPDATE library_artists SET monitored = 0 WHERE id = 'ar-2'")
        test_db.conn.commit()
        ids = [r["id"] for r in api.get("/api/wanted/missing", headers=admin_h).json()["records"]]
        assert ids == ["t-missing"]

    @pytest.mark.parametrize(
        "key,direction,expected",
        [
            ("artist", "desc", ["t-missing-b", "t-missing"]),
            ("title", "asc", ["t-missing", "t-missing-b"]),
            ("album", "desc", ["t-missing-b", "t-missing"]),
            ("release_date", "desc", ["t-missing-b", "t-missing"]),
            ("last_searched_at", "asc", ["t-missing", "t-missing-b"]),
        ],
    )
    def test_missing_sorting(self, api, test_db, admin_h, key, direction, expected):
        _library(test_db)
        test_db.conn.execute("UPDATE library_tracks SET last_searched_at='2026-01-02 00:00:00' WHERE id='t-missing-b'")
        test_db.conn.commit()
        ids = [r["id"] for r in api.get(f"/api/wanted/missing?sort_key={key}&sort_dir={direction}", headers=admin_h).json()["records"]]
        assert ids == expected

    def test_missing_paging_and_422(self, api, test_db, admin_h):
        _library(test_db)
        p2 = api.get("/api/wanted/missing?page=2&page_size=1", headers=admin_h).json()
        assert p2["total"] == 2 and [r["id"] for r in p2["records"]] == ["t-missing-b"]
        for qs in ("sort_key=nope", "page_size=0", "page_size=201", "sort_dir=up"):
            assert api.get(f"/api/wanted/missing?{qs}", headers=admin_h).status_code == 422

    def test_cutoff_lists_below_cutoff_and_honours_upgrade_allowed(self, api, test_db, admin_h):
        _library(test_db)
        body = api.get("/api/wanted/cutoff", headers=admin_h).json()
        assert [r["id"] for r in body["records"]] == ["t-low"]
        rec = body["records"][0]
        assert rec["current_quality"] == "MP3 192" and rec["cutoff_quality"]
        assert set(rec) >= {"id", "source", "artist", "album", "title", "item_type", "release_date", "monitored",
                            "last_searched_at", "current_quality", "cutoff_quality"}
        # a profile that forbids upgrades drops the row, whether assigned to the artist or inherited as default
        profile = test_db.get_default_quality_profile()["id"]
        test_db.conn.execute("UPDATE quality_profiles SET upgrade_allowed = 0 WHERE id = ?", (profile,))
        test_db.conn.commit()
        assert api.get("/api/wanted/cutoff", headers=admin_h).json()["total"] == 0
        other = [p for p in test_db.list_quality_profiles() if p["id"] != profile][0]["id"]
        test_db.conn.execute("UPDATE library_artists SET quality_profile_id = ? WHERE id = 'ar-2'", (other,))
        test_db.conn.commit()
        assert api.get("/api/wanted/cutoff", headers=admin_h).json()["total"] == 1  # the artist's own profile allows it

    def test_cutoff_excludes_unmonitored(self, api, test_db, admin_h):
        _library(test_db)
        test_db.conn.execute("UPDATE library_tracks SET monitored = 0 WHERE id = 't-low'")
        test_db.conn.commit()
        assert api.get("/api/wanted/cutoff", headers=admin_h).json()["total"] == 0

    def test_search_by_ids_queues_through_backlog_machinery(self, api, test_db, admin_h):
        _library(test_db)
        backlog_worker.pace_delay = 0
        with patch(
            "plex_playlist_sync.acquisition_coordinator.acquisition_coordinator.search_and_grab",
            return_value={"success": False, "message": "none"},
        ) as search:
            res = api.post("/api/wanted/search", json={"ids": ["t-missing", "t-low"]}, headers=admin_h)
            assert res.status_code == 200 and res.json() == {"queued": 2}
            backlog_worker.last_search_thread.join(timeout=5)
        calls = {c.kwargs["track_id"]: c.kwargs for c in search.call_args_list}
        assert set(calls) == {"t-missing", "t-low"}
        assert calls["t-missing"]["min_score"] is None and calls["t-missing"]["artist"] == "Alpha"
        assert calls["t-low"]["min_score"] is not None  # a below-cutoff file is searched as an upgrade
        assert test_db.conn.execute("SELECT last_searched_at FROM library_tracks WHERE id='t-missing'").fetchone()[0]

    def test_search_all_missing(self, api, test_db, admin_h):
        _library(test_db)
        backlog_worker.pace_delay = 0
        with patch(
            "plex_playlist_sync.acquisition_coordinator.acquisition_coordinator.search_and_grab",
            return_value={"success": True, "release": "r"},
        ) as search:
            res = api.post("/api/wanted/search", json={"all": True, "list": "missing"}, headers=admin_h)
            assert res.json() == {"queued": 2}
            backlog_worker.last_search_thread.join(timeout=5)
        assert {c.kwargs["track_id"] for c in search.call_args_list} == {"t-missing", "t-missing-b"}

    def test_search_skips_recently_searched_and_reports_it(self, api, test_db, admin_h):
        _library(test_db)
        backlog_worker.pace_delay = 0
        with patch(
            "plex_playlist_sync.acquisition_coordinator.acquisition_coordinator.search_and_grab",
            return_value={"success": False, "message": "none"},
        ) as search:
            first = api.post("/api/wanted/search", json={"ids": ["t-missing"]}, headers=admin_h)
            backlog_worker.last_search_thread.join(timeout=5)
            second = api.post("/api/wanted/search", json={"ids": ["t-missing", "t-missing-b"]}, headers=admin_h)
            backlog_worker.last_search_thread.join(timeout=5)
            third = api.post("/api/wanted/search", json={"ids": ["t-missing"]}, headers=admin_h)
        assert first.json() == {"queued": 1}
        assert second.json() == {"queued": 1, "message": "Skipped 1 searched in the last 10 minutes"}
        assert third.status_code == 200
        assert third.json() == {"queued": 0, "message": "All selected items were searched in the last 10 minutes"}
        assert [c.kwargs["track_id"] for c in search.call_args_list] == ["t-missing", "t-missing-b"]

    def test_search_old_stamp_is_searched_again(self, api, test_db, admin_h):
        _library(test_db)
        backlog_worker.pace_delay = 0
        test_db.conn.execute("UPDATE library_tracks SET last_searched_at = '2020-01-01 00:00:00' WHERE id='t-missing'")
        test_db.conn.commit()
        with patch(
            "plex_playlist_sync.acquisition_coordinator.acquisition_coordinator.search_and_grab",
            return_value={"success": False, "message": "none"},
        ):
            res = api.post("/api/wanted/search", json={"ids": ["t-missing"]}, headers=admin_h)
            backlog_worker.last_search_thread.join(timeout=5)
        assert res.json() == {"queued": 1}

    def test_search_while_batch_running_queues_nothing(self, api, test_db, admin_h):
        import threading

        _library(test_db)
        backlog_worker.pace_delay = 0
        release = threading.Event()
        entered = threading.Event()

        def slow(**_kwargs):
            entered.set()
            release.wait(timeout=5)
            return {"success": False, "message": "none"}

        with patch("plex_playlist_sync.acquisition_coordinator.acquisition_coordinator.search_and_grab", side_effect=slow):
            first = api.post("/api/wanted/search", json={"ids": ["t-missing"]}, headers=admin_h)
            assert first.json() == {"queued": 1}
            assert entered.wait(timeout=5)
            second = api.post("/api/wanted/search", json={"ids": ["t-missing-b"]}, headers=admin_h)
            release.set()
            backlog_worker.last_search_thread.join(timeout=5)
        assert second.status_code == 200
        assert second.json() == {"queued": 0, "message": "A search batch is already running"}

    @pytest.mark.parametrize(
        "payload",
        [{}, {"ids": []}, {"all": True}, {"all": True, "list": "bogus"}, {"ids": ["a"], "all": True, "list": "missing"}],
    )
    def test_search_bad_body_422(self, api, admin_h, payload):
        assert api.post("/api/wanted/search", json=payload, headers=admin_h).status_code == 422


# -------------------------------------------------------------------------------------------- lidarr mode


QUEUE_REC = {
    "id": 7, "albumId": 42, "artistId": 3, "title": "Radiohead-OK.Computer-FLAC", "size": 1000.0, "sizeleft": 250.0,
    "timeleft": "01:02:03", "added": "2026-10-04T10:00:00Z", "status": "downloading",
    "trackedDownloadStatus": "warning", "trackedDownloadState": "downloadFailedPending",
    "statusMessages": [{"title": "Output", "messages": ["fetch http://x/?apikey=TOPSECRET failed"]}],
    "errorMessage": None, "protocol": "torrent", "downloadClient": "qBit", "indexer": "Prowlarr",
    "quality": {"quality": {"name": "FLAC"}},
    "artist": {"artistName": "Radiohead"}, "album": {"title": "OK Computer"},
}


def _mock_http():
    patcher = patch("plex_playlist_sync.clients.lidarr.httpx.Client")
    cls = patcher.start()
    return patcher, cls.return_value.__enter__.return_value


@pytest.fixture
def lidarr_http(test_db):
    _lidarr_mode(test_db)
    patcher, http = _mock_http()
    yield http
    patcher.stop()


class TestLidarrMode:
    def test_queue_maps_sort_and_record(self, api, admin_h, lidarr_http):
        lidarr_http.get.return_value = _resp(payload=_page([QUEUE_REC], total=61))
        res = api.get("/api/activity/queue?page=2&page_size=25&sort_key=artist&sort_dir=desc", headers=admin_h)
        assert res.status_code == 200
        url = lidarr_http.get.call_args.args[0]
        assert "/api/v1/queue?" in url and "page=2" in url and "pageSize=25" in url
        assert "sortKey=artists.sortName" in url and "sortDirection=descending" in url
        assert "includeArtist=true" in url and "includeAlbum=true" in url
        assert lidarr_http.get.call_args.kwargs["headers"]["X-Api-Key"] == API_KEY
        body = res.json()
        assert body["mode"] == "lidarr" and body["total"] == 61 and body["page"] == 2 and body["sort_key"] == "artist"
        rec = body["records"][0]
        assert rec["id"] == "7" and rec["source"] == "lidarr" and rec["artist"] == "Radiohead"
        assert rec["album"] == "OK Computer" and rec["quality"] == "FLAC" and rec["client"] == "qBit"
        assert rec["progress"] == 0.75 and rec["sizeleft_bytes"] == 250 and rec["eta_seconds"] == 3723
        assert rec["stalled"] is True and "TOPSECRET" not in str(rec["messages"])
        assert rec["status"] == "downloading" and rec["request_id"] is None

    def test_queue_ok_status_not_stalled_and_bad_sort(self, api, admin_h, lidarr_http):
        ok = dict(QUEUE_REC, trackedDownloadStatus="ok", statusMessages=[])
        lidarr_http.get.return_value = _resp(payload=_page([ok]))
        assert api.get("/api/activity/queue", headers=admin_h).json()["records"][0]["stalled"] is False
        assert api.get("/api/activity/queue?sort_key=nope", headers=admin_h).status_code == 422
        assert api.get("/api/activity/queue?page_size=999", headers=admin_h).status_code == 422

    def test_queue_delete_passes_flags(self, api, admin_h, lidarr_http):
        lidarr_http.delete.return_value = _resp(payload={})
        res = api.delete("/api/activity/queue/7?remove_from_client=true&blocklist=true", headers=admin_h)
        assert res.status_code == 200
        url = lidarr_http.delete.call_args.args[0]
        assert "/api/v1/queue/7?" in url and "removeFromClient=true" in url and "blocklist=true" in url
        api.delete("/api/activity/queue/7?remove_from_client=false", headers=admin_h)
        url = lidarr_http.delete.call_args.args[0]
        assert "removeFromClient=false" in url and "blocklist=false" in url

    def test_queue_delete_non_numeric_id_404(self, api, admin_h, lidarr_http):
        assert api.delete("/api/activity/queue/abc", headers=admin_h).status_code == 404
        lidarr_http.delete.assert_not_called()

    @pytest.mark.parametrize("bad", ["abc", "-7", "+7", "7.0", "%207", "7%0A", "12345678901", "%D9%A7", "7_0"])
    def test_numeric_id_is_strict_ascii_digits(self, api, admin_h, lidarr_http, bad):
        assert api.delete(f"/api/activity/queue/{bad}", headers=admin_h).status_code == 404
        lidarr_http.delete.assert_not_called()

    def test_wanted_last_searched_sort_is_422_in_lidarr_mode_only(self, api, admin_h, lidarr_http, test_db):
        res = api.get("/api/wanted/missing?sort_key=last_searched_at", headers=admin_h)
        assert res.status_code == 422 and "last_searched_at" in res.json()["detail"]
        lidarr_http.get.assert_not_called()
        lidarr_http.get.return_value = _resp(payload=_page([]))
        assert api.get("/api/wanted/cutoff?sort_key=release_date", headers=admin_h).status_code == 200
        assert "sortKey=albums.releaseDate" in lidarr_http.get.call_args.args[0]

    def test_history_can_mark_failed_only_for_grabs(self, api, admin_h, lidarr_http):
        recs = [
            {"id": 1, "eventType": "grabbed", "sourceTitle": "r", "date": "2026-10-01T00:00:00Z", "data": {}},
            {"id": 2, "eventType": "downloadFailed", "sourceTitle": "r", "date": "2026-10-01T00:00:00Z", "data": {}},
        ]
        lidarr_http.get.return_value = _resp(payload=_page(recs))
        out = api.get("/api/activity/history", headers=admin_h).json()["records"]
        assert [r["can_mark_failed"] for r in out] == [True, False]

    def test_retry_runs_album_search(self, api, admin_h, lidarr_http):
        lidarr_http.get.return_value = _resp(payload=_page([QUEUE_REC]))
        lidarr_http.post.return_value = _resp(payload={"id": 1})
        res = api.post("/api/activity/queue/7/retry", headers=admin_h)
        assert res.status_code == 200 and res.json()["success"] is True
        assert lidarr_http.post.call_args.args[0].endswith("/api/v1/command")
        assert lidarr_http.post.call_args.kwargs["json"] == {"name": "AlbumSearch", "albumIds": [42]}

    def test_retry_unknown_item_404(self, api, admin_h, lidarr_http):
        lidarr_http.get.return_value = _resp(payload=_page([QUEUE_REC]))
        assert api.post("/api/activity/queue/999/retry", headers=admin_h).status_code == 404
        lidarr_http.post.assert_not_called()

    def test_history_list_filter_and_mapping(self, api, admin_h, lidarr_http):
        rec = {
            "id": 9, "eventType": "downloadFailed", "date": "2026-10-04T09:00:00Z", "sourceTitle": "Rel",
            "quality": {"quality": {"name": "MP3-320"}}, "artist": {"artistName": "Radiohead"},
            "album": {"title": "OK Computer"}, "data": {"indexer": "Prowlarr", "downloadClient": "qBit", "message": "boom"},
        }
        lidarr_http.get.return_value = _resp(payload=_page([rec], total=3))
        res = api.get("/api/activity/history?event=failed&page_size=10", headers=admin_h)
        url = lidarr_http.get.call_args.args[0]
        assert "/api/v1/history?" in url and "eventType=4" in url and "sortKey=date" in url
        body = res.json()
        assert body["total"] == 3 and body["sort_key"] == "date"
        assert body["records"][0] == {
            "id": "9", "source": "lidarr", "event": "failed", "artist": "Radiohead", "album": "OK Computer",
            "title": "OK Computer", "release_title": "Rel", "quality": "MP3-320", "indexer": "Prowlarr",
            "client": "qBit", "date": "2026-10-04T09:00:00Z", "message": "boom", "can_mark_failed": False,
        }

    def test_history_event_without_lidarr_equivalent_is_empty_without_a_call(self, api, admin_h, lidarr_http):
        res = api.get("/api/activity/history?event=blocklisted", headers=admin_h)
        assert res.status_code == 200 and res.json()["total"] == 0 and res.json()["records"] == []
        lidarr_http.get.assert_not_called()

    def test_history_unfiltered_maps_event_names(self, api, admin_h, lidarr_http):
        recs = [{"id": 1, "eventType": "trackFileImported"}, {"id": 2, "eventType": "grabbed"}, {"id": 3, "eventType": "trackFileDeleted"}]
        lidarr_http.get.return_value = _resp(payload=_page(recs))
        events = [r["event"] for r in api.get("/api/activity/history", headers=admin_h).json()["records"]]
        assert events == ["imported", "grabbed", "deleted"]
        assert "eventType" not in lidarr_http.get.call_args.args[0]

    def test_history_failed_calls_lidarr(self, api, admin_h, lidarr_http):
        lidarr_http.post.return_value = _resp(payload=None)
        res = api.post("/api/activity/history/9/failed", headers=admin_h)
        assert res.status_code == 200 and res.json()["success"] is True
        assert lidarr_http.post.call_args.args[0].endswith("/api/v1/history/failed/9")

    def test_blocklist_list_and_delete(self, api, admin_h, lidarr_http):
        rec = {"id": 5, "sourceTitle": "Bad Rel", "quality": {"quality": {"name": "MP3-128"}}, "date": "2026-10-01T00:00:00Z",
               "protocol": "usenet", "indexer": "NZB", "message": "Unpack failed", "artist": {"artistName": "Radiohead"}}
        lidarr_http.get.return_value = _resp(payload=_page([rec]))
        body = api.get("/api/activity/blocklist?sort_key=artist&sort_dir=asc", headers=admin_h).json()
        assert "sortKey=artists.sortName" in lidarr_http.get.call_args.args[0]
        assert body["records"][0] == {
            "id": "5", "source": "lidarr", "artist": "Radiohead", "album": None, "title": "Bad Rel",
            "release_title": "Bad Rel", "quality": "MP3-128", "indexer": "NZB", "protocol": "usenet",
            "reason": "Unpack failed", "date": "2026-10-01T00:00:00Z",
        }
        lidarr_http.delete.return_value = _resp(payload=None)
        assert api.delete("/api/activity/blocklist/5", headers=admin_h).status_code == 200
        assert lidarr_http.delete.call_args.args[0].endswith("/api/v1/blocklist/5")

    def test_wanted_missing_and_cutoff(self, api, admin_h, lidarr_http):
        album = {"id": 42, "title": "OK Computer", "releaseDate": "1997-05-21T00:00:00Z", "monitored": True,
                 "lastSearchTime": "2026-10-03T00:00:00Z", "artist": {"artistName": "Radiohead"}}
        lidarr_http.get.return_value = _resp(payload=_page([album], total=12))
        body = api.get("/api/wanted/missing?sort_key=release_date&sort_dir=desc&page_size=5", headers=admin_h).json()
        url = lidarr_http.get.call_args.args[0]
        assert "/api/v1/wanted/missing?" in url and "sortKey=albums.releaseDate" in url and "includeArtist=true" in url
        assert body["total"] == 12 and body["records"][0] == {
            "id": "42", "source": "lidarr", "artist": "Radiohead", "album": "OK Computer", "album_id": "42",
            "title": "OK Computer",
            "item_type": "album", "release_date": "1997-05-21T00:00:00Z", "monitored": True,
            "last_searched_at": "2026-10-03T00:00:00Z",
        }
        cut = api.get("/api/wanted/cutoff", headers=admin_h).json()
        assert "/api/v1/wanted/cutoff?" in lidarr_http.get.call_args.args[0]
        assert "current_quality" in cut["records"][0] and "cutoff_quality" in cut["records"][0]
        assert api.get("/api/wanted/cutoff?sort_key=bad", headers=admin_h).status_code == 422

    def test_wanted_search_ids_and_all(self, api, admin_h, lidarr_http):
        lidarr_http.post.return_value = _resp(payload={"id": 1})
        res = api.post("/api/wanted/search", json={"ids": [42, "43"]}, headers=admin_h)
        assert res.json() == {"queued": 2}
        assert lidarr_http.post.call_args.kwargs["json"] == {"name": "AlbumSearch", "albumIds": [42, 43]}
        lidarr_http.get.return_value = _resp(payload=_page([], total=17))
        res = api.post("/api/wanted/search", json={"all": True, "list": "missing"}, headers=admin_h)
        assert res.json() == {"queued": 17}
        assert lidarr_http.post.call_args.kwargs["json"] == {"name": "MissingAlbumSearch"}
        api.post("/api/wanted/search", json={"all": True, "list": "cutoff"}, headers=admin_h)
        assert lidarr_http.post.call_args.kwargs["json"] == {"name": "CutoffUnmetAlbumSearch"}
        assert api.post("/api/wanted/search", json={"ids": ["x"]}, headers=admin_h).status_code == 422

    @pytest.mark.parametrize(
        "method,path,body",
        [
            ("get", "/api/activity/queue", None),
            ("get", "/api/activity/history", None),
            ("get", "/api/activity/blocklist", None),
            ("get", "/api/wanted/missing", None),
            ("get", "/api/wanted/cutoff", None),
            ("delete", "/api/activity/queue/7", None),
            ("post", "/api/activity/queue/7/retry", None),
            ("post", "/api/activity/history/9/failed", None),
            ("delete", "/api/activity/blocklist/5", None),
            ("post", "/api/wanted/search", {"ids": [1]}),
        ],
    )
    def test_lidarr_failure_is_502_and_redacted(self, api, admin_h, lidarr_http, method, path, body):
        boom = httpx.ConnectError(f"failed http://lidarr.test/api/v1/x?apikey={API_KEY}")
        for verb in ("get", "post", "delete"):
            getattr(lidarr_http, verb).side_effect = boom
        kwargs = {"json": body} if body is not None else {}
        res = getattr(api, method)(path, headers=admin_h, **kwargs)
        assert res.status_code == 502
        assert API_KEY not in res.text and "apikey" not in res.text.lower()

    def test_lidarr_http_error_status_is_502(self, api, admin_h, lidarr_http):
        lidarr_http.get.return_value = _resp(status=500, payload={})
        res = api.get("/api/activity/queue", headers=admin_h)
        assert res.status_code == 502 and "HTTP 500" in res.json()["detail"]
        lidarr_http.get.return_value = _resp(status=401, payload={})
        assert "API key" in api.get("/api/wanted/missing", headers=admin_h).json()["detail"]

    def test_unconfigured_lidarr_is_502(self, api, test_db, admin_h):
        test_db.update_media_management_settings({"library_mode": "lidarr"})
        res = api.get("/api/activity/queue", headers=admin_h)
        assert res.status_code == 502 and "not configured" in res.json()["detail"]

    def test_native_data_is_not_read_in_lidarr_mode(self, api, test_db, admin_h, lidarr_http):
        _dl(test_db, "dl-native")
        lidarr_http.get.return_value = _resp(payload=_page([]))
        assert api.get("/api/activity/queue", headers=admin_h).json()["records"] == []

    def test_native_mode_never_contacts_lidarr(self, api, test_db, admin_h):
        test_db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": API_KEY})
        with patch("plex_playlist_sync.clients.lidarr.httpx.Client") as cls:
            for path in ("/api/activity/queue", "/api/activity/history", "/api/activity/blocklist",
                         "/api/wanted/missing", "/api/wanted/cutoff"):
                assert api.get(path, headers=admin_h).status_code == 200
            cls.assert_not_called()


# ------------------------------------------------------------------------------- guard, 409, auth, gateway


MUTATIONS = [
    ("delete", "/api/activity/queue/dl-g", None),
    ("post", "/api/activity/queue/dl-g/retry", None),
    ("post", "/api/activity/history/dh-g/failed", None),
    ("delete", "/api/activity/blocklist/bl-g", None),
    ("post", "/api/wanted/search", {"ids": ["t-1"]}),
]


class TestGuardAndAccess:
    @pytest.mark.parametrize("method,path,body", MUTATIONS)
    def test_mode_changed_is_409(self, api, admin_h, method, path, body):
        module = "plex_playlist_sync.api.routes.wanted" if "/wanted" in path else "plex_playlist_sync.api.routes.activity"
        # Both routes share run_mutation, which lives in the activity module.
        with patch("plex_playlist_sync.api.routes.activity.run_for_mode", side_effect=ModeChanged("native", "lidarr")):
            kwargs = {"json": body} if body is not None else {}
            res = getattr(api, method)(path, headers=admin_h, **kwargs)
        assert res.status_code == 409, module

    def test_mutations_run_under_the_active_mode_guard(self, api, test_db, admin_h):
        from plex_playlist_sync import library_manager

        _dl(test_db, "dl-g")
        seen = {}

        def fake_delete(db, did, remove, bl):
            seen["in_flight"] = library_manager.in_flight_count("native")
            return "ok"

        with patch.object(activity_service, "native_delete_queue_item", side_effect=fake_delete):
            assert api.delete("/api/activity/queue/dl-g", headers=admin_h).status_code == 200
        assert seen["in_flight"] == 1
        assert library_manager.in_flight_count() == 0  # released afterwards

    def test_lidarr_mutation_runs_under_lidarr_guard(self, api, test_db, admin_h, lidarr_http):
        from plex_playlist_sync import library_manager

        seen = {}

        def fake_delete(*args, **kwargs):
            seen["lidarr"] = library_manager.in_flight_count("lidarr")
            seen["native"] = library_manager.in_flight_count("native")
            return _resp(payload={})

        lidarr_http.delete.side_effect = fake_delete
        assert api.delete("/api/activity/queue/7", headers=admin_h).status_code == 200
        assert seen == {"lidarr": 1, "native": 0}

    def test_search_is_refused_when_mode_flips_to_lidarr_for_native_work(self, api, test_db, admin_h):
        _library(test_db)
        # Real guard: a native search that was routed natively but whose guard finds Lidarr active is a 409.
        with patch("plex_playlist_sync.library_manager.get_library_mode", side_effect=["native", "lidarr"] * 3):
            res = api.post("/api/wanted/search", json={"ids": ["t-missing"]}, headers=admin_h)
        assert res.status_code == 409

    ENDPOINTS = [
        ("get", "/api/activity/queue"),
        ("get", "/api/activity/history"),
        ("get", "/api/activity/blocklist"),
        ("get", "/api/wanted/missing"),
        ("get", "/api/wanted/cutoff"),
        ("delete", "/api/activity/queue/x"),
        ("post", "/api/activity/queue/x/retry"),
        ("post", "/api/activity/history/x/failed"),
        ("delete", "/api/activity/blocklist/x"),
        ("post", "/api/wanted/search"),
    ]

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_non_admin_forbidden_and_anonymous_rejected(self, api, test_db, test_config, users, method, path):
        kwargs = {"json": {"ids": ["1"]}} if method == "post" and path.endswith("search") else {}
        alice = _headers(users["alice"], test_db, test_config)
        assert getattr(api, method)(path, headers=alice, **kwargs).status_code == 403
        assert getattr(api, method)(path, **kwargs).status_code in (401, 403)

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    def test_gateway_tier_denies_by_default(self, test_db, tmp_path, users, method, path):
        cfg = Config(
            plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), role="gateway",
            internal_core_secret="s" * 40, trackseerr_core_url="http://core.internal:5251",
        )
        app = create_app(db=test_db, config=cfg)
        app.dependency_overrides[get_db] = lambda: test_db
        app.dependency_overrides[get_config] = lambda: cfg
        client = TestClient(app)
        headers = _headers(users["admin"], test_db, cfg)
        kwargs = {"json": {"ids": ["1"]}} if method == "post" and path.endswith("search") else {}
        assert getattr(client, method)(path, headers=headers, **kwargs).status_code == 404

    def test_core_tier_dependency_blocks_gateway_role_even_if_reached(self, test_db, tmp_path, users):
        from fastapi import HTTPException

        from plex_playlist_sync.api.dependencies import require_core_tier

        cfg = Config(plex_url="http://p", plex_token="t", data_dir=str(tmp_path), role="gateway",
                     internal_core_secret="s" * 40, trackseerr_core_url="http://core.internal:5251")
        with pytest.raises(HTTPException) as exc:
            require_core_tier(cfg)
        assert exc.value.status_code == 403

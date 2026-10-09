"""Stuck Lidarr requests (albums_pending / rate_limited / monitor_failed) are retried on a schedule."""

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr import library_manager
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.lidarr_queue import LidarrTrickleWorker
from trackseerr.models import MusicRequest, RequestStatus
from trackseerr.storage import SCHEMA_VERSION, Database, request_retry_delay
from tests.lidarr_fake import FakeLidarr, FastClock

HTTPX = "trackseerr.clients.lidarr.httpx.Client"
TS = "%Y-%m-%d %H:%M:%S"


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_user("user-a", "alice", "a@x.com", is_admin=False)
    yield d
    d.close()


def make_request(db, req_id="req-1", status=RequestStatus.PROCESSING):
    db.create_request(
        MusicRequest(id=req_id, user_id="user-a", item_type="track", title="Opera Song", artist="Queen", status=status)
    )


def item(req_id="req-1"):
    return {"id": req_id, "artist": "Queen", "album": "", "title": "Opera Song", "item_type": "track", "is_request": True}


def run_worker(db, fake, items):
    from trackseerr.clients.lidarr import LidarrClient

    worker = LidarrTrickleWorker()
    worker._delay_seconds = 0.0
    worker._auto_search = True
    client = LidarrClient("http://lidarr.test:8686", "key-abcdef123456", root_folder="/music")
    with patch(HTTPX, fake), patch("trackseerr.lidarr_queue.time", FastClock()), patch(
        "trackseerr.clients.lidarr.time.sleep"
    ):
        worker._process_groups(LidarrTrickleWorker._group_by_artist(items), client, db)


def attempts(db, req_id="req-1"):
    return db.conn.execute("SELECT retry_attempts FROM music_requests WHERE id = ?", (req_id,)).fetchone()[0]


def set_due(db, req_id, delta):
    db.conn.execute(
        "UPDATE music_requests SET next_attempt_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) + delta).strftime(TS), req_id),
    )
    db.conn.commit()


class TestPolicy:
    @pytest.mark.parametrize(
        "reason,attempts,expected",
        [
            ("albums_pending", 1, timedelta(minutes=2)),
            ("albums_pending", 2, timedelta(minutes=5)),
            ("albums_pending", 3, timedelta(minutes=15)),
            ("albums_pending", 4, timedelta(hours=1)),
            ("albums_pending", 5, timedelta(hours=6)),
            ("albums_pending", 6, timedelta(hours=24)),
            ("albums_pending", 40, timedelta(hours=24)),
            ("rate_limited", 1, timedelta(minutes=5)),
            ("rate_limited", 2, timedelta(minutes=30)),
            ("rate_limited", 3, timedelta(hours=2)),
            ("rate_limited", 4, timedelta(hours=24)),
            ("monitor_failed", 1, timedelta(minutes=15)),
            ("monitor_failed", 2, timedelta(hours=1)),
            ("monitor_failed", 3, timedelta(hours=6)),
            ("monitor_failed", 4, timedelta(hours=24)),
            ("not_in_metadata_profile", 1, timedelta(days=7)),
            ("albums_pending", 0, timedelta(minutes=2)),
        ],
    )
    def test_backoff(self, reason, attempts, expected):
        assert request_retry_delay(reason, attempts) == expected

    def test_unknown_reason_is_not_scheduled(self):
        assert request_retry_delay("something_else", 1) is None

    def test_rate_limit_honours_a_longer_retry_after(self):
        assert request_retry_delay("rate_limited", 1, retry_after=900) == timedelta(seconds=900)
        assert request_retry_delay("rate_limited", 3, retry_after=60) == timedelta(hours=2)


class TestSchedule:
    def test_outcome_counts_attempts_and_sets_next_attempt(self, db):
        make_request(db)
        db.set_request_outcome("req-1", "albums_pending", "waiting")
        req = db.get_request("req-1")
        assert attempts(db) == 1 and req["next_attempt_at"]
        first = req["next_attempt_at"]
        db.set_request_outcome("req-1", "albums_pending", "waiting")
        assert attempts(db) == 2
        assert db.get_request("req-1")["next_attempt_at"] > first

    def test_success_and_status_updates_clear_the_schedule(self, db):
        make_request(db)
        db.set_request_outcome("req-1", "albums_pending", "waiting")
        db.update_request_status("req-1", "processing")  # also what a manual retry does
        req = db.get_request("req-1")
        assert (req["status_reason"], attempts(db), req["next_attempt_at"]) == (None, 0, None)

    def test_migration_v66_on_an_existing_db(self, tmp_path):
        path = tmp_path / "old.db"
        d = Database(path)
        d.conn.execute("DELETE FROM schema_migrations WHERE version >= ?", (66,))
        d.conn.execute("ALTER TABLE music_requests DROP COLUMN next_attempt_at")
        d.conn.execute("ALTER TABLE music_requests DROP COLUMN retry_attempts")
        d.conn.commit()
        d.close()
        d = Database(path)
        cols = {r[1] for r in d.conn.execute("PRAGMA table_info(music_requests)")}
        assert {"retry_attempts", "next_attempt_at"} <= cols
        assert SCHEMA_VERSION >= 66
        assert d.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
        d.close()


class TestSweep:
    def stuck_fake(self):
        fake = FakeLidarr()
        fake.lookup = [{"id": 0, "artistName": "Queen"}]
        fake.albums = []  # MusicBrainz has not listed the albums yet
        return fake

    def test_pending_then_retry_monitors_everything(self, db):
        db.update_media_management_settings({"library_mode": "lidarr"})
        db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": "key-abcdef123456", "root_folder": "/music"})
        make_request(db)
        fake = self.stuck_fake()
        run_worker(db, fake, [item()])
        req = db.get_request("req-1")
        assert req["status_reason"] == "albums_pending"
        assert req["next_attempt_at"]

        # not due yet: the sweep does nothing
        with patch.object(library_manager, "dispatch_to_lidarr") as dispatch:
            assert library_manager.retry_stuck_lidarr_requests(db) == 0
        dispatch.assert_not_called()

        # due: re-dispatched with the request's own details
        set_due(db, "req-1", timedelta(minutes=-1))
        with patch.object(library_manager, "dispatch_to_lidarr", return_value=True) as dispatch:
            assert library_manager.retry_stuck_lidarr_requests(db) == 1
        assert dispatch.call_args.args[2] == [item()]
        assert attempts(db) == 2  # deferred before dispatch, no tight loop
        assert db.get_request("req-1")["next_attempt_at"] > datetime.now(timezone.utc).strftime(TS)

        # the albums are listed now and the artist exists: the retry monitors album and artist
        fake.lookup = [{"id": 5, "artistName": "Queen"}]
        fake.existing_artist_id = 5
        fake.albums = [{"id": 2, "title": "Opera Song", "albumType": "Single", "releaseDate": "1975-01-01", "monitored": False}]
        fake.tracks = [{"id": 2, "albumId": 2, "title": "Opera Song"}]
        run_worker(db, fake, [item()])
        req = db.get_request("req-1")
        assert req["status"] == "processing"
        assert (req["status_reason"], attempts(db), req["next_attempt_at"]) == (None, 0, None)
        assert fake.requests("PUT", "album/monitor") == [{"albumIds": [2], "monitored": True}]
        assert fake.artist_monitored is True

    def test_noop_in_native_mode(self, db):
        make_request(db)
        db.set_request_outcome("req-1", "albums_pending", "waiting")
        set_due(db, "req-1", timedelta(minutes=-1))
        with patch.object(library_manager, "dispatch_to_lidarr") as dispatch:
            assert library_manager.retry_stuck_lidarr_requests(db) == 0
        dispatch.assert_not_called()

    def test_metadata_profile_is_not_fast_retried(self, db):
        make_request(db)
        db.set_request_outcome("req-1", "not_in_metadata_profile", "no")
        due_at = db.get_request("req-1")["next_attempt_at"]
        assert due_at > (datetime.now(timezone.utc) + timedelta(days=6)).strftime(TS)

    def test_only_approved_requests_with_a_retryable_reason_are_due(self, db):
        make_request(db, "a")
        make_request(db, "b", status=RequestStatus.PENDING)
        make_request(db, "c")
        for rid in ("a", "b", "c"):
            db.set_request_outcome(rid, "albums_pending", "w")
            set_due(db, rid, timedelta(minutes=-1))
        db.update_request_status("c", "available")
        assert [r["id"] for r in db.list_requests_due_for_retry()] == ["a"]

    def test_database_errors_are_logged_not_raised(self, db):
        db.update_media_management_settings({"library_mode": "lidarr"})
        with patch.object(db, "list_requests_due_for_retry", side_effect=sqlite3.OperationalError("locked")):
            assert library_manager.retry_stuck_lidarr_requests(db) == 0


@pytest.fixture
def admin_client(db, tmp_path):
    config = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id="admin-1", username="admin_user", is_admin=True, secret_key=secret)
    db.create_session(token, "admin-1", {"auth": "test"})
    return TestClient(app), {"Authorization": f"Bearer {token}"}


class TestManualRetryRoute:
    def test_retry_clears_the_schedule_and_redispatches(self, db, admin_client):
        client, headers = admin_client
        db.update_media_management_settings({"library_mode": "lidarr"})
        make_request(db)
        db.set_request_outcome("req-1", "albums_pending", "waiting")
        with patch("trackseerr.api.routes.requests.dispatch_to_lidarr", return_value=True) as dispatch:
            resp = client.post("/api/requests/req-1/retry", headers=headers)
        assert resp.status_code == 200 and resp.json()["success"] is True
        assert dispatch.call_args.args[2] == [item()]
        req = db.get_request("req-1")
        assert (req["status_reason"], attempts(db), req["next_attempt_at"]) == (None, 0, None)

    def test_next_attempt_is_exposed_in_the_list(self, db, admin_client):
        client, headers = admin_client
        make_request(db)
        db.set_request_outcome("req-1", "albums_pending", "waiting")
        body = client.get("/api/requests", headers=headers).json()["requests"][0]
        assert body["status_reason"] == "albums_pending" and body["next_attempt_at"]


class TestScheduledTask:
    def tasks(self, client, headers):
        return {t["id"]: t for t in client.get("/api/system/tasks", headers=headers).json()}

    def test_listed_in_lidarr_mode_only(self, db, admin_client):
        client, headers = admin_client
        assert "lidarr_request_retry" not in self.tasks(client, headers)
        db.update_media_management_settings({"library_mode": "lidarr"})
        task = self.tasks(client, headers)["lidarr_request_retry"]
        assert task["name"] == "Retry Stuck Lidarr Requests" and task["interval"] == "Checks every 1m" and task["can_trigger"]

    def test_run_now_is_refused_in_native_mode(self, admin_client):
        client, headers = admin_client
        assert client.post("/api/system/tasks/lidarr_request_retry/run", headers=headers).status_code == 409

    def test_run_now_sweeps_ignoring_the_schedule(self, db, admin_client):
        client, headers = admin_client
        db.update_media_management_settings({"library_mode": "lidarr"})
        make_request(db)
        db.set_request_outcome("req-1", "albums_pending", "waiting")  # due in 2 minutes, not yet
        make_request(db, "req-2")  # no retryable reason: untouched
        with patch.object(library_manager, "dispatch_to_lidarr", return_value=True) as dispatch, patch.object(
            library_manager, "build_lidarr_client", return_value=object()
        ):
            assert client.post("/api/system/tasks/lidarr_request_retry/run", headers=headers).status_code == 200
            import time

            for _ in range(100):
                if dispatch.called:
                    break
                time.sleep(0.05)
        assert [i["id"] for i in dispatch.call_args.args[2]] == ["req-1"]
        assert library_manager.last_retry_sweep_at


class TestNativeMode:
    def test_native_outcome_records_the_reason_without_a_retry_time(self, db):
        make_request(db)
        db.set_request_outcome("req-1", "albums_pending", "waiting", schedule=False)
        req = db.get_request("req-1")
        assert req["status_reason"] == "albums_pending" and req["next_attempt_at"] is None and attempts(db) == 0
        assert db.list_requests_due_for_retry(ignore_schedule=False) == []

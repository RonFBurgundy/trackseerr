"""Exactly-one-manager guarantees: the work guard / atomic switch, mode-gated grab, importer, trickle queue cap."""

import asyncio
import logging
import random
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from plex_playlist_sync import library_manager as lm
from plex_playlist_sync.acquisition_coordinator import acquisition_coordinator
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.job_tracker import job_tracker, summarize_result
from plex_playlist_sync.lidarr_migration import LidarrMigrationJob
from plex_playlist_sync.lidarr_queue import MAX_PENDING_ITEMS, lidarr_worker
from plex_playlist_sync.storage import Database

API_KEY = "lidarr-secret-key-abcdef123456"

GRAB_PAYLOAD = {
    "release": {
        "id": "rel-1",
        "title": "Daft Punk - Discovery [FLAC]",
        "indexer_name": "TestIndexer",
        "protocol": "torrent",
        "size_bytes": 350000000,
        "parsed_quality": "FLAC 16bit",
        "is_acceptable": True,
        "score": 900,
        "meets_cutoff": True,
    },
    "artist": "Daft Punk",
    "title": "Discovery",
}


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def seeded_users(test_db):
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice}


@pytest.fixture
def app_and_client(test_db, test_config):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    return app, TestClient(app)


@pytest.fixture(autouse=True)
def _clean_singletons():
    job_tracker.clear()
    yield
    with lidarr_worker._lock:
        lidarr_worker._is_running = False
        lidarr_worker._is_paused = False
        lidarr_worker._pending = []
    job_tracker.clear()


def _token(user, db, config) -> str:
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return token


def _headers(user, db, config):
    return {"Authorization": f"Bearer {_token(user, db, config)}"}


def _set_mode(db, mode):
    db.update_media_management_settings({"library_mode": mode})


def _mode(db) -> str:
    return db.get_media_management_settings()["library_mode"]


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _req_item(rid: str, artist: str = "Radiohead") -> dict[str, Any]:
    return {"id": rid, "artist": artist, "album": "OK Computer", "title": "OK Computer", "is_request": True}


# --------------------------------------------------------------------------- guard + switch semantics


class TestWorkGuard:
    def test_guard_raises_mode_changed_when_mode_differs(self, test_db):
        _set_mode(test_db, "lidarr")
        with pytest.raises(lm.ModeChanged) as info:
            with lm.work_guard(test_db, lm.MODE_NATIVE):
                pytest.fail("work must not run under the wrong mode")
        assert info.value.expected == "native" and info.value.actual == "lidarr"
        assert lm.in_flight_count() == 0

    def test_counter_released_even_when_work_raises(self, test_db):
        with pytest.raises(RuntimeError):
            with lm.work_guard(test_db, lm.MODE_NATIVE):
                assert lm.in_flight_count(lm.MODE_NATIVE) == 1
                raise RuntimeError("boom")
        assert lm.in_flight_count() == 0

    def test_switch_refused_while_guard_held_then_allowed(self, test_db):
        with lm.work_guard(test_db, lm.MODE_NATIVE):
            with pytest.raises(lm.SwitchRefused) as info:
                lm.switch_mode(test_db, lm.MODE_LIDARR, source="test")
            assert "1 native operation in progress" in info.value.reason
            assert _mode(test_db) == "native"
        assert lm.switch_mode(test_db, lm.MODE_LIDARR, source="test") == "native"
        assert _mode(test_db) == "lidarr"
        events, _ = test_db.list_events(event_type="library_manager_changed")
        assert events[0]["source"] == "test"

    def test_same_mode_switch_is_noop(self, test_db):
        assert lm.switch_mode(test_db, lm.MODE_NATIVE, source="test") == "native"
        events, _ = test_db.list_events(event_type="library_manager_changed")
        assert events == []

    def test_dispatch_after_switch_uses_new_mode(self, test_db):
        calls: list[str] = []
        _set_mode(test_db, "native")
        lm.run_for_mode(test_db, native=lambda: calls.append("native"), lidarr=lambda: calls.append("lidarr"))
        lm.switch_mode(test_db, lm.MODE_LIDARR, source="test")
        lm.run_for_mode(test_db, native=lambda: calls.append("native"), lidarr=lambda: calls.append("lidarr"))
        assert calls == ["native", "lidarr"]

    def test_run_for_mode_reroutes_when_mode_flips_before_guard(self, test_db):
        """A caller that read 'native' but lost the race re-routes to lidarr instead of dropping the work."""
        calls: list[str] = []
        real = lm.get_library_mode
        state = {"n": 0}

        def flaky(db):
            state["n"] += 1
            if state["n"] == 1:
                return "native"  # the stale read; the DB (and guard) say lidarr
            return real(db)

        _set_mode(test_db, "lidarr")
        with patch.object(lm, "get_library_mode", side_effect=flaky):
            lm.run_for_mode(test_db, native=lambda: calls.append("native"), lidarr=lambda: calls.append("lidarr"))
        assert calls == ["lidarr"]

    def test_switch_refused_during_inflight_native_grab_then_succeeds(self, test_db):
        started, release = threading.Event(), threading.Event()

        def slow_grab(*args, **kwargs):
            started.set()
            release.wait(10)
            return {"success": True}

        with patch.object(acquisition_coordinator, "_search_and_grab", side_effect=slow_grab):
            t = threading.Thread(
                target=lambda: acquisition_coordinator.search_and_grab(artist="A", title="T", db=test_db)
            )
            t.start()
            try:
                assert started.wait(5)
                with pytest.raises(lm.SwitchRefused):
                    lm.switch_mode(test_db, lm.MODE_LIDARR, source="test")
                assert lm.get_status(test_db)["can_switch"] is False
                assert _mode(test_db) == "native"
            finally:
                release.set()
                t.join(5)
        assert lm.get_status(test_db)["can_switch"] is True
        lm.switch_mode(test_db, lm.MODE_LIDARR, source="test")
        assert _mode(test_db) == "lidarr"

    def test_search_and_grab_skips_in_lidarr_mode(self, test_db):
        _set_mode(test_db, "lidarr")
        with patch.object(acquisition_coordinator, "_search_and_grab") as inner:
            res = acquisition_coordinator.search_and_grab(artist="A", title="T", db=test_db)
        inner.assert_not_called()
        assert res["success"] is False and res["mode_changed"] is True

    def test_switch_refused_while_trickle_runs_then_succeeds(self, test_db):
        _set_mode(test_db, "lidarr")
        gate = threading.Event()
        seen: list[str] = []

        def add(artist_name, album_names, auto_search, monitor_mode):
            seen.append(artist_name)
            gate.wait(10)
            return {"status": "success"}

        client = MagicMock()
        client.add_artist_and_albums.side_effect = add
        res = lidarr_worker.start_trickle(
            items=[_req_item("r1")], client=client, db=test_db, delay_seconds=0.5
        )
        assert res["status"] == "started"
        assert _wait_for(lambda: bool(seen))
        with pytest.raises(lm.SwitchRefused) as info:
            lm.switch_mode(test_db, lm.MODE_NATIVE, source="test")
        assert "lidarr" in info.value.reason
        gate.set()
        assert _wait_for(lambda: not lidarr_worker.is_running(), timeout=15)
        assert _wait_for(lambda: lm.in_flight_count() == 0)
        lm.switch_mode(test_db, lm.MODE_NATIVE, source="test")
        assert _mode(test_db) == "native"

    def test_trickle_guard_released_when_queued_not_started(self, test_db):
        _set_mode(test_db, "lidarr")
        with lidarr_worker._lock:
            lidarr_worker._is_running = True
        lidarr_worker.start_trickle(
            items=[_req_item("r1")], client=MagicMock(), db=test_db, queue_if_running=True
        )
        assert lm.in_flight_count() == 0  # the running worker owns the guard, the queuing caller must not leak one

    def test_threaded_switch_vs_dispatch_never_works_under_wrong_mode(self, test_db):
        violations: list[str] = []
        done = threading.Event()
        counts = {"native": 0, "lidarr": 0, "switches": 0, "refused": 0}
        count_lock = threading.Lock()

        def work(expected: str) -> None:
            # Real work reads the mode again; under the guard it must still be the expected one.
            time.sleep(random.random() * 0.0005)
            actual = lm.get_library_mode(test_db)
            if actual != expected:
                violations.append(f"ran {expected} work while mode was {actual}")
            with count_lock:
                counts[expected] += 1

        def dispatcher() -> None:
            while not done.is_set():
                lm.run_for_mode(test_db, native=lambda: work("native"), lidarr=lambda: work("lidarr"))
                time.sleep(0.002)  # real dispatchers are bursty; back-to-back holders would starve any switch

        def switcher() -> None:
            target = "lidarr"
            for _ in range(400):
                try:
                    lm.switch_mode(test_db, target, source="stress")
                    target = "native" if target == "lidarr" else "lidarr"
                    with count_lock:
                        counts["switches"] += 1
                except lm.SwitchRefused:
                    with count_lock:
                        counts["refused"] += 1
                time.sleep(0.0005)
            done.set()

        threads = [threading.Thread(target=dispatcher) for _ in range(4)] + [threading.Thread(target=switcher)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not any(t.is_alive() for t in threads)
        assert violations == []
        assert counts["switches"] > 0 and counts["native"] + counts["lidarr"] > 0
        assert lm.in_flight_count() == 0


# --------------------------------------------------------------------------- F1: POST /acquisition/grab


class TestGrabRoute:
    def test_grab_409_in_lidarr_mode_without_touching_clients(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _set_mode(test_db, "lidarr")
        with patch("plex_playlist_sync.api.routes.acquisition.get_acquisition_driver") as driver:
            resp = client.post(
                "/api/acquisition/grab", json=GRAB_PAYLOAD, headers=_headers(seeded_users["admin"], test_db, test_config)
            )
        assert resp.status_code == 409
        driver.assert_not_called()

    def test_grab_runs_under_native_guard(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        observed: list[int] = []

        def fake(payload, db):
            observed.append(lm.in_flight_count(lm.MODE_NATIVE))
            return {"success": True}

        with patch("plex_playlist_sync.api.routes.acquisition._grab_release", side_effect=fake):
            resp = client.post(
                "/api/acquisition/grab", json=GRAB_PAYLOAD, headers=_headers(seeded_users["admin"], test_db, test_config)
            )
        assert resp.status_code == 200 and observed == [1]
        assert lm.in_flight_count() == 0

    def test_grab_blocked_on_gateway_tier(self, app_and_client, test_db, test_config, seeded_users):
        test_config.role = "gateway"
        _, client = app_and_client
        resp = client.post(
            "/api/acquisition/grab", json=GRAB_PAYLOAD, headers=_headers(seeded_users["admin"], test_db, test_config)
        )
        assert resp.status_code in (403, 404)

    @pytest.mark.parametrize("method", ["post", "put"])
    def test_lidarr_settings_write_blocked_on_gateway_tier(
        self, app_and_client, test_db, test_config, seeded_users, method
    ):
        test_config.role = "gateway"
        _, client = app_and_client
        resp = getattr(client, method)(
            "/api/settings/lidarr",
            json={"url": "http://evil:8686", "api_key": "k"},
            headers=_headers(seeded_users["admin"], test_db, test_config),
        )
        assert resp.status_code in (403, 404)
        assert test_db.get_lidarr_settings().get("url") != "http://evil:8686"

    @pytest.mark.parametrize("method", ["post", "put"])
    def test_lidarr_settings_write_allowed_on_core(self, app_and_client, test_db, test_config, seeded_users, method):
        _, client = app_and_client
        resp = getattr(client, method)(
            "/api/settings/lidarr",
            json={"url": "http://lidarr:8686", "api_key": API_KEY},
            headers=_headers(seeded_users["admin"], test_db, test_config),
        )
        assert resp.status_code == 200


# --------------------------------------------------------------------------- F3: importer


class TestImporterSwitch:
    def test_refused_switch_warns_records_event_and_does_not_raise(self, test_db, caplog):
        _set_mode(test_db, "lidarr")
        with caplog.at_level(logging.WARNING):
            with lm.work_guard(test_db, lm.MODE_LIDARR):
                LidarrMigrationJob._switch_to_native(test_db)
        assert _mode(test_db) == "lidarr"
        assert any(r.levelno == logging.WARNING and "not switched" in r.getMessage() for r in caplog.records)
        events, _ = test_db.list_events(event_type="library_manager_switch_skipped")
        assert events and "manually" in events[0]["message"]

    def test_allowed_switch_flips_mode(self, test_db):
        _set_mode(test_db, "lidarr")
        LidarrMigrationJob._switch_to_native(test_db)
        assert _mode(test_db) == "native"


# --------------------------------------------------------------------------- F4: missing status in native mode


class TestMissingStatusNative:
    def test_native_mode_does_not_contact_lidarr(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        test_db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": API_KEY})
        with patch("plex_playlist_sync.clients.lidarr.httpx.Client") as cls, patch.object(
            httpx, "get"
        ) as raw_get:
            resp = client.get(
                "/api/missing/lidarr/status", headers=_headers(seeded_users["admin"], test_db, test_config)
            )
        assert resp.status_code == 200
        assert resp.json() == {"mode": "native", "connected": None}
        cls.assert_not_called()
        raw_get.assert_not_called()


# --------------------------------------------------------------------------- F5: pending cap / stranded events


class TestTricklePending:
    def test_pending_cap_refuses_and_dispatch_reports_not_accepted(self, test_db, caplog):
        _set_mode(test_db, "lidarr")
        test_db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": API_KEY})
        with lidarr_worker._lock:
            lidarr_worker._is_running = True
            lidarr_worker._pending = [_req_item(f"p{i}") for i in range(MAX_PENDING_ITEMS)]
        res = lidarr_worker.start_trickle(
            items=[_req_item("extra")], client=MagicMock(), db=test_db, queue_if_running=True
        )
        assert res["status"] == "queue_full" and "retry" in res["message"]
        assert len(lidarr_worker._pending) == MAX_PENDING_ITEMS
        with caplog.at_level(logging.WARNING):
            assert lm.dispatch_to_lidarr(test_db, MagicMock(), [_req_item("extra")]) is False
        assert "queue is full" in caplog.text
        assert lm.in_flight_count() == 0

    def test_cancel_records_one_event_for_unsent_requests(self, test_db):
        _set_mode(test_db, "lidarr")
        gate = threading.Event()
        seen: list[str] = []

        def add(artist_name, album_names, auto_search, monitor_mode):
            seen.append(artist_name)
            gate.wait(10)
            return {"status": "success"}

        client = MagicMock()
        client.add_artist_and_albums.side_effect = add
        assert lidarr_worker.start_trickle(
            items=[_req_item("r1")], client=client, db=test_db, delay_seconds=0.5
        )["status"] == "started"
        assert _wait_for(lambda: bool(seen))
        for rid, artist in (("r2", "Portishead"), ("r3", "Massive Attack")):
            assert lidarr_worker.start_trickle(
                items=[_req_item(rid, artist)], client=client, db=test_db, queue_if_running=True
            )["status"] == "queued"
        lidarr_worker.cancel()
        gate.set()
        assert _wait_for(lambda: not lidarr_worker.is_running(), timeout=15)
        assert _wait_for(lambda: lm.in_flight_count() == 0)
        events, _ = test_db.list_events(event_type="lidarr_requests_unsent")
        assert len(events) == 1
        assert events[0]["message"] == "2 requests not sent to Lidarr; retry them from Requests"
        assert sorted(events[0]["details"]["request_ids"]) == ["r2", "r3"]

    def test_crash_records_event_and_never_logs_raw_exception_text(self, test_db, caplog):
        _set_mode(test_db, "lidarr")
        gate = threading.Event()

        def add(artist_name, album_names, auto_search, monitor_mode):
            gate.wait(10)
            raise ValueError("http://lidarr/api?apikey=SUPERSECRETKEY failed")

        client = MagicMock()
        client.add_artist_and_albums.side_effect = add
        with caplog.at_level(logging.INFO):  # the raw traceback is DEBUG-only by design
            lidarr_worker.start_trickle(items=[_req_item("r1")], client=client, db=test_db, delay_seconds=0.5)
            assert _wait_for(lambda: lidarr_worker.is_running())
            lidarr_worker.start_trickle(
                items=[_req_item("r2", "Portishead")], client=client, db=test_db, queue_if_running=True
            )
            gate.set()
            assert _wait_for(lambda: not lidarr_worker.is_running(), timeout=15)
            assert _wait_for(lambda: lm.in_flight_count() == 0)
        events, _ = test_db.list_events(event_type="lidarr_requests_unsent")
        assert len(events) == 1 and events[0]["details"]["request_ids"] == ["r2"]
        assert "SUPERSECRETKEY" not in caplog.text
        assert "ValueError" in lidarr_worker.get_status()["message"]
        assert "SUPERSECRETKEY" not in lidarr_worker.get_status()["message"]


# --------------------------------------------------------------------------- F6: job message redaction


def test_summarize_result_redacts_string_values():
    out = summarize_result({"error": "GET http://x/api?apikey=SECRET123&a=1 failed", "count": 3})
    assert out is not None and "SECRET123" not in out and "count=3" in out


# --------------------------------------------------------------------------- F9: SSE auth


class TestLogStreamAuth:
    def test_query_token_is_rejected(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        token = _token(seeded_users["admin"], test_db, test_config)
        resp = client.get(f"/api/system/logs/stream?token={token}")
        assert resp.status_code == 401

    def test_non_admin_cookie_is_403(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        token = _token(seeded_users["alice"], test_db, test_config)
        client.cookies.set("session_token", token)
        assert client.get("/api/system/logs/stream").status_code == 403

    def test_admin_session_cookie_is_accepted(self, test_db, test_config, seeded_users):
        from plex_playlist_sync.api.routes.system import stream_system_logs

        token = _token(seeded_users["admin"], test_db, test_config)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/system/logs/stream",
            "query_string": b"",
            "headers": [(b"cookie", f"session_token={token}".encode())],
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.disconnect"}

        async def first_chunk() -> str:
            response = await stream_system_logs(Request(scope, receive), db=test_db, config=test_config)
            assert response.media_type == "text/event-stream"
            chunk = await response.body_iterator.__anext__()
            await response.body_iterator.aclose()
            return chunk if isinstance(chunk, str) else chunk.decode()

        assert '"type": "connected"' in asyncio.run(first_chunk())

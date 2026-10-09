"""Library-manager interlock: mode API, strictly mode-driven routing, worker skips, Lidarr options/health,
Lidarr settings fields and the in-memory job queue."""

import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.backlog_worker import backlog_worker, rss_worker
from trackseerr.config import Config
from trackseerr.job_tracker import job_tracker, track_job
from trackseerr.lidarr_queue import lidarr_worker
from trackseerr.library_scanner import library_scanner
from trackseerr.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadStatus,
    IndexerConfig,
    MusicRequest,
    RequestStatus,
)
from trackseerr.acquisition_worker import AcquisitionWorker
from trackseerr.storage import Database

API_KEY = "lidarr-secret-key-abcdef123456"


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
    job_tracker.clear()


def _headers(user, db, config):
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def _configure_lidarr(db):
    db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": API_KEY})


def _set_mode(db, mode):
    db.update_media_management_settings({"library_mode": mode})


def _seed_native(db):
    db.create_indexer(
        IndexerConfig(id="idx-1", name="Prowlarr", indexer_type="torznab", host_url="http://prowlarr:9696", enabled=True)
    )
    db.create_download_client(
        DownloadClientConfig(
            id="cli-qbit", name="qBittorrent", driver_type="qbittorrent", host_url="http://qbit:8080", enabled=True
        )
    )


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- mode API


class TestLibraryManagerMode:
    def test_get_defaults_to_native(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        resp = client.get("/api/settings/library-manager", headers=_headers(seeded_users["admin"], test_db, test_config))
        assert resp.status_code == 200
        assert resp.json() == {
            "mode": "native",
            "lidarr_configured": False,
            "native_configured": False,
            "can_switch": True,
            "blocking_reason": None,
        }

    def test_native_configured_needs_client_and_indexer(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _seed_native(test_db)
        resp = client.get("/api/settings/library-manager", headers=_headers(seeded_users["admin"], test_db, test_config))
        assert resp.json()["native_configured"] is True

    def test_put_to_lidarr_unconfigured_is_422(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        resp = client.put(
            "/api/settings/library-manager",
            json={"mode": "lidarr"},
            headers=_headers(seeded_users["admin"], test_db, test_config),
        )
        assert resp.status_code == 422
        assert test_db.get_media_management_settings()["library_mode"] == "native"

    def test_put_switches_and_records_event_and_preserves_settings(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        _configure_lidarr(test_db)
        headers = _headers(seeded_users["admin"], test_db, test_config)
        resp = client.put("/api/settings/library-manager", json={"mode": "lidarr"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["mode"] == "lidarr"
        assert resp.json()["lidarr_configured"] is True
        events, _ = test_db.list_events(event_type="library_manager_changed")
        assert len(events) == 1
        # Switching back keeps the Lidarr settings intact
        resp = client.put("/api/settings/library-manager", json={"mode": "native"}, headers=headers)
        assert resp.json()["mode"] == "native"
        assert test_db.get_lidarr_settings()["url"] == "http://lidarr.test:8686"

    def test_saving_lidarr_credentials_enables_in_place_switch(self, app_and_client, test_db, test_config, seeded_users):
        """The Lidarr page saves host + key, refreshes status, then switches without a trip back to General."""
        _, client = app_and_client
        headers = _headers(seeded_users["admin"], test_db, test_config)
        assert client.get("/api/settings/library-manager", headers=headers).json()["lidarr_configured"] is False
        assert client.put("/api/settings/library-manager", json={"mode": "lidarr"}, headers=headers).status_code == 422
        saved = client.put(
            "/api/settings/lidarr", json={"url": "http://lidarr.test:8686", "api_key": API_KEY}, headers=headers
        )
        assert saved.status_code == 200
        status = client.get("/api/settings/library-manager", headers=headers).json()
        assert status["lidarr_configured"] is True and status["can_switch"] is True
        assert client.put("/api/settings/library-manager", json={"mode": "lidarr"}, headers=headers).json()["mode"] == "lidarr"

    def test_put_same_mode_is_a_noop_without_event(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        resp = client.put(
            "/api/settings/library-manager",
            json={"mode": "native"},
            headers=_headers(seeded_users["admin"], test_db, test_config),
        )
        assert resp.status_code == 200
        assert test_db.list_events(event_type="library_manager_changed")[1] == 0

    def test_put_invalid_mode_is_422(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        resp = client.put(
            "/api/settings/library-manager",
            json={"mode": "plex"},
            headers=_headers(seeded_users["admin"], test_db, test_config),
        )
        assert resp.status_code == 422

    def test_put_409_when_native_download_in_flight(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _configure_lidarr(test_db)
        _seed_native(test_db)
        test_db.create_active_download(
            ActiveDownload(id="dl-1", client_id="cli-qbit", title="T", artist="A", status=DownloadStatus.DOWNLOADING.value)
        )
        headers = _headers(seeded_users["admin"], test_db, test_config)
        resp = client.put("/api/settings/library-manager", json={"mode": "lidarr"}, headers=headers)
        assert resp.status_code == 409
        body = resp.json()
        assert body["can_switch"] is False
        assert "native download" in body["blocking_reason"]
        assert body["detail"] == body["blocking_reason"]
        assert test_db.get_media_management_settings()["library_mode"] == "native"
        # Terminal rows do not block
        test_db.update_download_status("dl-1", status=DownloadStatus.IMPORTED.value)
        assert client.put("/api/settings/library-manager", json={"mode": "lidarr"}, headers=headers).status_code == 200

    def test_put_409_when_lidarr_trickle_running(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _configure_lidarr(test_db)
        _set_mode(test_db, "lidarr")
        with lidarr_worker._lock:
            lidarr_worker._is_running = True
        headers = _headers(seeded_users["admin"], test_db, test_config)
        resp = client.put("/api/settings/library-manager", json={"mode": "native"}, headers=headers)
        assert resp.status_code == 409
        assert "trickle" in resp.json()["blocking_reason"].lower()

    def test_media_settings_cannot_change_mode(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        resp = client.post(
            "/api/settings/media-management",
            json={"library_mode": "lidarr"},
            headers=_headers(seeded_users["admin"], test_db, test_config),
        )
        assert resp.status_code == 200
        assert test_db.get_media_management_settings()["library_mode"] == "native"

    def test_importer_path_goes_through_switch_guard(self, test_db):
        from trackseerr.lidarr_migration import LidarrMigrationJob

        _set_mode(test_db, "lidarr")
        LidarrMigrationJob._switch_to_native(test_db)
        assert test_db.get_media_management_settings()["library_mode"] == "native"
        events, _ = test_db.list_events(event_type="library_manager_changed")
        assert events and events[0]["source"] == "lidarr_import"


# --------------------------------------------------------------------------- request routing


def _post_request(client, headers, title="OK Computer"):
    return client.post("/api/requests", headers=headers, json={"item_type": "album", "title": title, "artist": "Radiohead"})


class TestRequestRouting:
    def test_lidarr_mode_never_uses_native_acquisition(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _seed_native(test_db)
        _configure_lidarr(test_db)
        _set_mode(test_db, "lidarr")
        test_db.update_lidarr_settings({"auto_search": False, "trickle_rate_seconds": 1.5})
        headers = _headers(seeded_users["admin"], test_db, test_config)
        with patch("trackseerr.request_submission.acquisition_coordinator") as coord, patch(
            "trackseerr.api.routes.requests.acquisition_coordinator"
        ) as coord_route, patch(
            "trackseerr.lidarr_queue.lidarr_worker.start_trickle", return_value={"status": "started"}
        ) as trickle:
            resp = _post_request(client, headers)
        assert resp.status_code == 201
        coord.search_and_grab.assert_not_called()
        coord_route.search_and_grab.assert_not_called()
        trickle.assert_called_once()
        kwargs = trickle.call_args.kwargs
        assert kwargs["auto_search"] is False  # lidarr_settings, not the env default
        assert kwargs["delay_seconds"] == 1.5
        assert kwargs["items"][0]["artist"] == "Radiohead"

    def test_native_mode_never_calls_lidarr(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _seed_native(test_db)
        _configure_lidarr(test_db)  # configured, but the mode is native
        headers = _headers(seeded_users["admin"], test_db, test_config)
        with patch(
            "trackseerr.request_submission.acquisition_coordinator.search_and_grab",
            return_value={"success": False, "message": "none"},
        ) as grab, patch("trackseerr.lidarr_queue.lidarr_worker.start_trickle") as trickle, patch(
            "trackseerr.clients.lidarr.httpx.Client"
        ) as http:
            resp = _post_request(client, headers)
        assert resp.status_code == 201
        grab.assert_called_once()
        trickle.assert_not_called()
        http.assert_not_called()

    def test_batch_in_lidarr_mode_goes_to_lidarr_only(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _seed_native(test_db)
        _configure_lidarr(test_db)
        _set_mode(test_db, "lidarr")
        headers = _headers(seeded_users["admin"], test_db, test_config)
        body = {
            "requests": [
                {"item_type": "album", "title": "A", "artist": "X"},
                {"item_type": "album", "title": "B", "artist": "X"},
            ]
        }
        with patch("trackseerr.api.routes.requests.acquisition_coordinator") as coord, patch(
            "trackseerr.request_submission.acquisition_coordinator"
        ) as coord2, patch(
            "trackseerr.lidarr_queue.lidarr_worker.start_trickle", return_value={"status": "started"}
        ) as trickle:
            resp = client.post("/api/requests/batch", headers=headers, json=body)
        assert resp.status_code == 201
        coord.search_and_grab.assert_not_called()
        coord2.search_and_grab.assert_not_called()
        trickle.assert_called_once()
        assert len(trickle.call_args.kwargs["items"]) == 2

    def test_batch_in_native_mode_never_calls_lidarr(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _seed_native(test_db)
        _configure_lidarr(test_db)
        headers = _headers(seeded_users["admin"], test_db, test_config)
        body = {"requests": [{"item_type": "album", "title": "A", "artist": "X"}]}
        with patch(
            "trackseerr.api.routes.requests.acquisition_coordinator.search_and_grab",
            return_value={"success": False, "message": "none"},
        ) as grab, patch("trackseerr.lidarr_queue.lidarr_worker.start_trickle") as trickle:
            resp = client.post("/api/requests/batch", headers=headers, json=body)
        assert resp.status_code == 201
        grab.assert_called_once()
        trickle.assert_not_called()

    @pytest.mark.parametrize("mode", ["native", "lidarr"])
    def test_approve_and_retry_follow_mode(self, app_and_client, test_db, test_config, seeded_users, mode):
        _, client = app_and_client
        _seed_native(test_db)
        _configure_lidarr(test_db)
        _set_mode(test_db, mode)
        headers = _headers(seeded_users["admin"], test_db, test_config)
        for rid in ("req-a", "req-b"):
            test_db.create_request(
                MusicRequest(
                    id=rid,
                    user_id=seeded_users["alice"]["id"],
                    item_type="album",
                    title=f"T-{rid}",
                    artist="Radiohead",
                    status=RequestStatus.PENDING,
                )
            )
        with patch(
            "trackseerr.api.routes.requests.acquisition_coordinator.search_and_grab",
            return_value={"success": False, "message": "none"},
        ) as grab, patch(
            "trackseerr.lidarr_queue.lidarr_worker.start_trickle", return_value={"status": "started"}
        ) as trickle:
            assert client.post("/api/requests/req-a/approve", headers=headers).status_code == 200
            assert client.post("/api/requests/req-b/retry", headers=headers).status_code == 200
        if mode == "lidarr":
            grab.assert_not_called()
            assert trickle.call_count == 2
        else:
            assert grab.call_count == 2
            trickle.assert_not_called()

    def test_lidarr_mode_without_lidarr_config_leaves_request_processing(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        _set_mode(test_db, "lidarr")  # forced directly; not configured
        headers = _headers(seeded_users["admin"], test_db, test_config)
        with patch("trackseerr.lidarr_queue.lidarr_worker.start_trickle") as trickle:
            resp = _post_request(client, headers)
        assert resp.status_code == 201
        trickle.assert_not_called()
        assert resp.json()["status"] == "processing"

    def test_missing_push_and_grab_are_mode_gated(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _configure_lidarr(test_db)
        headers = _headers(seeded_users["admin"], test_db, test_config)
        assert client.post("/api/missing/lidarr/push", json={"trickle": True}, headers=headers).status_code == 409
        _set_mode(test_db, "lidarr")
        assert client.post("/api/missing/1/grab", headers=headers).status_code in (404, 409)
        test_db.upsert_playlist("pl-1", "P", service="spotify")
        test_db.record_sync_result(
            "pl-1", status="partial", missing_tracks=[{"title": "Creep", "artist": "Radiohead", "album": "PH"}]
        )
        track_id = test_db.get_missing_tracks()[0]["id"]
        assert client.post(f"/api/missing/{track_id}/grab", headers=headers).status_code == 409


# --------------------------------------------------------------------------- workers / scanner / trickle


class TestWorkersFollowMode:
    def test_backlog_and_rss_skip_in_lidarr_mode(self, test_db):
        _set_mode(test_db, "lidarr")
        with patch("trackseerr.backlog_worker.acquisition_coordinator.search_and_grab") as grab:
            res = backlog_worker.poll_once(db=test_db)
            rss = rss_worker.poll_once(db=test_db)
        grab.assert_not_called()
        assert res["items_checked"] == 0 and res["skipped"]
        assert rss["releases_scanned"] == 0 and rss["skipped"]

    def test_scanner_skips_in_lidarr_mode(self, test_db, tmp_path):
        _set_mode(test_db, "lidarr")
        result = library_scanner.scan(db=test_db, root_folder=str(tmp_path))
        assert result["status"] == "skipped"

    def test_acquisition_worker_only_follows_lidarr_items_in_lidarr_mode(self, test_db, tmp_path):
        _set_mode(test_db, "lidarr")
        test_db.update_media_management_settings({"staging_folder_path": str(tmp_path)})
        _seed_native(test_db)
        test_db.create_active_download(
            ActiveDownload(id="dl-n", client_id="cli-qbit", title="T", artist="A", status=DownloadStatus.DOWNLOADING.value)
        )
        driver = MagicMock()
        with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver):
            stats = AcquisitionWorker().poll_once(db=test_db, staging_dir=str(tmp_path))
        assert stats["polled"] == 0
        driver.get_status.assert_not_called()

    def test_acquisition_worker_ignores_lidarr_driver_items_in_native_mode(self, test_db, tmp_path):
        test_db.update_media_management_settings({"staging_folder_path": str(tmp_path)})
        test_db.create_download_client(
            DownloadClientConfig(id="cli-l", name="Lidarr", driver_type="lidarr", host_url="http://lidarr:8686")
        )
        test_db.create_active_download(
            ActiveDownload(id="dl-l", client_id="cli-l", title="T", artist="A", status=DownloadStatus.COMPLETED.value)
        )
        driver = MagicMock()
        with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=driver):
            stats = AcquisitionWorker().poll_once(db=test_db, staging_dir=str(tmp_path))
        assert stats["polled"] == 0
        driver.get_status.assert_not_called()

    def test_trickle_refuses_in_native_mode(self, test_db):
        client = MagicMock()
        res = lidarr_worker.start_trickle(
            items=[{"id": "x", "artist": "Radiohead", "album": "OK Computer", "is_request": True}],
            client=client,
            db=test_db,
        )
        assert res["status"] == "refused"
        assert lidarr_worker.is_running() is False
        client.add_artist_and_albums.assert_not_called()

    def test_trickle_runs_in_lidarr_mode_and_is_recorded_as_a_job(self, test_db):
        _set_mode(test_db, "lidarr")
        client = MagicMock()
        client.add_artist_and_albums.return_value = {"status": "success"}
        res = lidarr_worker.start_trickle(
            items=[{"id": "x", "artist": "Radiohead", "album": "OK Computer", "is_request": True}],
            client=client,
            db=test_db,
            delay_seconds=0.5,
        )
        assert res["status"] == "started"
        assert _wait_for(lambda: not lidarr_worker.is_running(), timeout=10)
        client.add_artist_and_albums.assert_called_once()
        recent = job_tracker.snapshot()["recent"]
        assert recent and recent[0]["task_id"] == "lidarr_auto_trickle" and recent[0]["state"] == "completed"


# --------------------------------------------------------------------------- Lidarr client / options / health / settings


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = ""
    return r


class TestLidarrOptions:
    def test_options_success(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _configure_lidarr(test_db)
        with patch("trackseerr.clients.lidarr.httpx.Client") as cls:
            http = cls.return_value.__enter__.return_value
            http.get.side_effect = [
                _resp(payload=[{"path": "/music", "freeSpace": 12345, "id": 1}]),
                _resp(payload=[{"id": 1, "name": "Lossless"}]),
                _resp(payload=[{"id": 2, "name": "Standard"}]),
                _resp(payload=[{"id": 7, "label": "trackseerr"}]),
            ]
            resp = client.get(
                "/api/settings/lidarr/options", headers=_headers(seeded_users["admin"], test_db, test_config)
            )
        assert resp.status_code == 200
        assert resp.json() == {
            "root_folders": [{"path": "/music", "free_space": 12345}],
            "quality_profiles": [{"id": 1, "name": "Lossless"}],
            "metadata_profiles": [{"id": 2, "name": "Standard"}],
            "tags": [{"id": 7, "label": "trackseerr"}],
        }
        # bounded timeout on the HTTP client
        assert cls.call_args.kwargs["timeout"] == 15.0

    def test_options_failure_is_502_and_redacted(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _configure_lidarr(test_db)
        with patch("trackseerr.clients.lidarr.httpx.Client") as cls:
            http = cls.return_value.__enter__.return_value
            http.get.side_effect = httpx.ConnectError(f"boom http://lidarr.test:8686/?apikey={API_KEY}")
            resp = client.get(
                "/api/settings/lidarr/options", headers=_headers(seeded_users["admin"], test_db, test_config)
            )
        assert resp.status_code == 502
        assert API_KEY not in resp.text
        assert "ConnectError" in resp.json()["detail"]

    def test_options_bad_key_is_502_without_key(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _configure_lidarr(test_db)
        with patch("trackseerr.clients.lidarr.httpx.Client") as cls:
            cls.return_value.__enter__.return_value.get.return_value = _resp(status=401)
            resp = client.get(
                "/api/settings/lidarr/options", headers=_headers(seeded_users["admin"], test_db, test_config)
            )
        assert resp.status_code == 502
        assert API_KEY not in resp.text

    def test_options_unconfigured_is_422(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        resp = client.get("/api/settings/lidarr/options", headers=_headers(seeded_users["admin"], test_db, test_config))
        assert resp.status_code == 422


class TestLidarrSettingsFields:
    REMOVED = ("monitor_option", "quality_profile_id", "metadata_profile_id", "tag_ids")

    def test_settings_round_trip_without_the_removed_overrides(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        headers = _headers(seeded_users["admin"], test_db, test_config)
        default = client.get("/api/settings/lidarr", headers=headers).json()
        assert default["search_on_add"] is True
        assert not any(k in default for k in self.REMOVED)

        payload = {
            "url": "http://lidarr.test:8686",
            "api_key": API_KEY,
            "search_on_add": False,
            "root_folder": "/music",
        }
        resp = client.put("/api/settings/lidarr", json=payload, headers=headers)
        assert resp.status_code == 200
        got = client.get("/api/settings/lidarr", headers=headers).json()
        assert got["search_on_add"] is False and got["root_folder"] == "/music"
        assert not any(k in got for k in self.REMOVED)
        assert API_KEY not in str(got)
        assert test_db.get_lidarr_settings()["auto_search"] is False  # search_on_add is auto_search

    def test_clients_still_sending_the_removed_overrides_are_tolerated_and_ignored(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        headers = _headers(seeded_users["admin"], test_db, test_config)
        resp = client.put(
            "/api/settings/lidarr",
            json={"monitor_option": "everything", "quality_profile_id": 3, "metadata_profile_id": 2, "tag_ids": [1],
                  "root_folder": "/music"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert not any(k in resp.json() for k in self.REMOVED)
        stored = test_db.get_lidarr_settings()
        assert not any(k in stored for k in self.REMOVED)
        row = test_db.conn.execute("SELECT monitor_option, quality_profile_id, tag_ids FROM lidarr_settings WHERE id = 1").fetchone()
        assert row["monitor_option"] == "all" and row["quality_profile_id"] is None and row["tag_ids"] == "[]"

    def test_client_adds_artist_with_root_folder_defaults_not_settings(self):
        from trackseerr.clients.lidarr import LidarrClient
        from tests.lidarr_fake import FakeLidarr

        fake = FakeLidarr()
        fake.albums = [{"id": 1, "title": "A Night at the Opera", "albumType": "Album", "monitored": False}]
        lc = LidarrClient("http://lidarr.test", API_KEY, root_folder="/music")
        with patch("trackseerr.clients.lidarr.httpx.Client", fake):
            res = lc.add_artist_and_albums("Queen", ["A Night at the Opera"], auto_search=False)
        assert res["status"] == "success"
        sent = fake.requests("POST", "artist")[0]
        assert sent["qualityProfileId"] == 4 and sent["metadataProfileId"] == 6 and sent["tags"] == [7]
        assert sent["monitorNewItems"] == "none" and sent["addOptions"]["monitor"] == "none"


class TestLidarrHealth:
    def test_native_mode_does_not_contact_lidarr(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _configure_lidarr(test_db)
        with patch("trackseerr.clients.lidarr.httpx.Client") as cls:
            resp = client.get("/api/system/lidarr-health", headers=_headers(seeded_users["admin"], test_db, test_config))
        assert resp.status_code == 200
        assert resp.json()["mode"] == "native" and resp.json()["reachable"] is None and resp.json()["health"] == []
        cls.assert_not_called()

    def test_lidarr_mode_reports_version_and_health(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _configure_lidarr(test_db)
        _set_mode(test_db, "lidarr")
        with patch("trackseerr.clients.lidarr.httpx.Client") as cls:
            http = cls.return_value.__enter__.return_value
            http.get.side_effect = [
                _resp(payload={"version": "2.4.3", "appName": "Lidarr"}),
                _resp(
                    payload=[
                        {"source": "IndexerCheck", "type": "warning", "message": "No indexers", "wikiUrl": "https://wiki.servarr.com/x"},
                        {"source": "Odd", "type": "weird", "message": "m"},
                    ]
                ),
            ]
            resp = client.get("/api/system/lidarr-health", headers=_headers(seeded_users["admin"], test_db, test_config))
        body = resp.json()
        assert resp.status_code == 200
        assert body["mode"] == "lidarr" and body["reachable"] is True and body["version"] == "2.4.3"
        assert body["health"][0] == {
            "source": "IndexerCheck", "type": "warning", "message": "No indexers", "wiki_url": "https://wiki.servarr.com/x",
        }
        assert body["health"][1]["type"] == "notice" and body["health"][1]["wiki_url"] is None

    def test_unreachable_lidarr_never_500s(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _configure_lidarr(test_db)
        _set_mode(test_db, "lidarr")
        with patch("trackseerr.clients.lidarr.httpx.Client") as cls:
            cls.return_value.__enter__.return_value.get.side_effect = httpx.ConnectError("down")
            resp = client.get("/api/system/lidarr-health", headers=_headers(seeded_users["admin"], test_db, test_config))
        assert resp.status_code == 200
        assert resp.json() == {"mode": "lidarr", "reachable": False, "version": None, "health": []}

    def test_lidarr_mode_unconfigured_is_unreachable_not_500(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _set_mode(test_db, "lidarr")
        resp = client.get("/api/system/lidarr-health", headers=_headers(seeded_users["admin"], test_db, test_config))
        assert resp.status_code == 200 and resp.json()["reachable"] is False


# --------------------------------------------------------------------------- system queue


class TestSystemQueue:
    def test_tracker_ring_keeps_last_50_and_running(self):
        for i in range(60):
            with track_job("t", f"job-{i}"):
                pass
        running_id = job_tracker.start("t", "still-running")
        snap = job_tracker.snapshot()
        assert len(snap["recent"]) == 50
        assert snap["recent"][0]["name"] == "job-59"  # newest first
        assert [j["name"] for j in snap["running"]] == ["still-running"]
        assert snap["queued"] == []
        job_tracker.finish(running_id, "completed")
        job = job_tracker.snapshot()["recent"][0]
        assert set(job) == {"id", "task_id", "name", "state", "started_at", "finished_at", "duration_ms", "message"}
        assert job["duration_ms"] >= 0

    def test_failure_is_recorded_and_propagates(self):
        with pytest.raises(RuntimeError):
            with track_job("t", "boom"):
                raise RuntimeError("secret http://x/?apikey=abc")
        job = job_tracker.snapshot()["recent"][0]
        assert job["state"] == "failed"
        assert "abc" not in (job["message"] or "")

    def test_scheduled_loop_run_is_recorded(self, test_db):
        backlog_worker.start(db=test_db, interval_seconds=3600)
        try:
            assert _wait_for(lambda: any(j["task_id"] == "wanted_backlog_sweep" for j in job_tracker.snapshot()["recent"]))
        finally:
            backlog_worker.stop()
        job = next(j for j in job_tracker.snapshot()["recent"] if j["task_id"] == "wanted_backlog_sweep")
        assert job["state"] == "completed"

    def test_manual_run_and_failure_appear_in_queue_endpoint(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        headers = _headers(seeded_users["admin"], test_db, test_config)
        with patch(
            "trackseerr.api.routes.system.acquisition_worker.poll_once", return_value={"polled": 2, "failed": 0}
        ):
            assert client.post("/api/system/tasks/download_queue_monitor/run", headers=headers).status_code == 200
            assert _wait_for(lambda: bool(job_tracker.snapshot()["recent"]))
        body = client.get("/api/system/queue", headers=headers).json()
        assert set(body) == {"running", "queued", "recent"}
        assert body["recent"][0]["task_id"] == "download_queue_monitor"
        assert body["recent"][0]["state"] == "completed" and "polled=2" in body["recent"][0]["message"]

        job_tracker.clear()
        with patch(
            "trackseerr.api.routes.system.acquisition_worker.poll_once", side_effect=RuntimeError("kaput")
        ):
            assert client.post("/api/system/tasks/download_queue_monitor/run", headers=headers).status_code == 200
            assert _wait_for(lambda: bool(job_tracker.snapshot()["recent"]))
        failed = client.get("/api/system/queue", headers=headers).json()["recent"][0]
        assert failed["state"] == "failed" and failed["finished_at"]

    def test_scanner_failure_is_recorded(self, test_db, tmp_path):
        library_scanner._run_background_scan(test_db, str(tmp_path / "does-not-exist"), False, None)
        job = job_tracker.snapshot()["recent"][0]
        assert job["task_id"] == "filesystem_scan" and job["state"] == "failed"

    def test_manual_trickle_task_refused_in_native_mode(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        resp = client.post(
            "/api/system/tasks/lidarr_auto_trickle/run", headers=_headers(seeded_users["admin"], test_db, test_config)
        )
        assert resp.status_code == 409


# --------------------------------------------------------------------------- access control


NEW_ROUTES = [
    ("GET", "/api/settings/library-manager", None),
    ("PUT", "/api/settings/library-manager", {"mode": "native"}),
    ("GET", "/api/settings/lidarr/options", None),
    ("GET", "/api/system/queue", None),
    ("GET", "/api/system/lidarr-health", None),
]


class TestAccessControl:
    @pytest.mark.parametrize("method,path,body", NEW_ROUTES)
    def test_non_admin_is_403(self, app_and_client, test_db, test_config, seeded_users, method, path, body):
        _, client = app_and_client
        resp = client.request(method, path, json=body, headers=_headers(seeded_users["alice"], test_db, test_config))
        assert resp.status_code == 403

    @pytest.mark.parametrize("method,path,body", NEW_ROUTES)
    def test_unauthenticated_is_rejected(self, app_and_client, method, path, body):
        _, client = app_and_client
        assert client.request(method, path, json=body).status_code in (401, 403)

    @pytest.mark.parametrize("method,path,body", NEW_ROUTES)
    def test_gateway_does_not_expose_them(self, app_and_client, test_db, test_config, seeded_users, method, path, body):
        test_config.role = "gateway"
        _, client = app_and_client
        resp = client.request(method, path, json=body, headers=_headers(seeded_users["admin"], test_db, test_config))
        assert resp.status_code == 404


# --------------------------------------------------------------------------- upgrade migration (library_mode)


class TestLibraryModeMigration:
    def _run(self, db, config=None):
        from trackseerr.library_manager import migrate_library_mode

        return migrate_library_mode(db, config)

    def _mode(self, db):
        return db.get_media_management_settings()["library_mode"]

    def _event_types(self, db):
        items, _total = db.list_events(limit=50)
        return [e["event_type"] for e in items]

    def test_lidarr_only_install_moves_to_lidarr(self, test_db):
        _configure_lidarr(test_db)
        assert self._run(test_db) == "lidarr"
        assert self._mode(test_db) == "lidarr"
        assert "library_mode_migrated" in self._event_types(test_db)

    def test_lidarr_from_env_only_moves_to_lidarr(self, test_db, tmp_path):
        cfg = Config(
            plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path),
            lidarr_url="http://lidarr.env:8686", lidarr_api_key=API_KEY,
        )
        assert self._run(test_db, cfg) == "lidarr"
        assert self._mode(test_db) == "lidarr"

    def test_lidarr_driver_download_client_counts_as_lidarr(self, test_db):
        test_db.create_download_client(
            DownloadClientConfig(
                id="cli-lidarr", name="Lidarr", driver_type="lidarr", host_url="http://lidarr:8686", enabled=True
            )
        )
        assert self._run(test_db) == "lidarr"

    def test_native_only_stays_native(self, test_db):
        _seed_native(test_db)
        assert self._run(test_db) == "native"
        assert self._mode(test_db) == "native"
        assert "library_mode_migrated" not in self._event_types(test_db)

    def test_both_configured_stays_native_with_warning_event(self, test_db, caplog):
        _seed_native(test_db)
        _configure_lidarr(test_db)
        with caplog.at_level("WARNING", logger="trackseerr.library_manager"):
            assert self._run(test_db) == "native"
        assert self._mode(test_db) == "native"
        assert "library_mode_choice_needed" in self._event_types(test_db)
        assert any("Settings -> General" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)

    def test_fresh_install_stays_native(self, test_db):
        assert self._run(test_db) == "native"
        assert self._mode(test_db) == "native"
        assert self._event_types(test_db) == []

    def test_runs_only_once_and_never_overrides_admin_choice(self, test_db):
        _configure_lidarr(test_db)
        assert self._run(test_db) == "lidarr"
        _set_mode(test_db, "native")  # admin switches back
        assert self._run(test_db) is None  # already ran
        assert self._mode(test_db) == "native"

    def test_fresh_install_marker_blocks_later_auto_switch(self, test_db):
        assert self._run(test_db) == "native"
        _configure_lidarr(test_db)  # admin configures Lidarr later; not an upgrade
        assert self._run(test_db) is None
        assert self._mode(test_db) == "native"


# --------------------------------------------------------------------------- requests while trickle is running


class TestTrickleQueuesWhileRunning:
    def test_dispatch_while_running_is_queued_and_delivered(self, test_db):
        from trackseerr.library_manager import dispatch_to_lidarr

        _set_mode(test_db, "lidarr")
        _configure_lidarr(test_db)
        gate = threading.Event()
        seen = []

        def add(artist_name, album_names, auto_search, **kwargs):
            seen.append(artist_name)
            if artist_name == "Radiohead":
                gate.wait(10)
            return {"status": "success"}

        client = MagicMock()
        client.add_artist_and_albums.side_effect = add
        first = lidarr_worker.start_trickle(
            items=[{"id": "r1", "artist": "Radiohead", "album": "OK Computer", "is_request": True}],
            client=client, db=test_db, delay_seconds=0.5,
        )
        assert first["status"] == "started"
        assert _wait_for(lambda: "Radiohead" in seen)
        # trickle is mid-run: a newly approved request must be accepted, not stranded
        assert dispatch_to_lidarr(
            test_db, client, [{"id": "r2", "artist": "Portishead", "album": "Dummy", "is_request": True}]
        ) is True
        gate.set()
        assert _wait_for(lambda: not lidarr_worker.is_running(), timeout=15)
        assert seen == ["Radiohead", "Portishead"]
        assert lidarr_worker.get_status()["total_items"] == 2

    def test_queue_not_used_without_flag(self, test_db):
        _set_mode(test_db, "lidarr")
        with lidarr_worker._lock:
            lidarr_worker._is_running = True
        res = lidarr_worker.start_trickle(
            items=[{"id": "x", "artist": "A", "is_request": True}], client=MagicMock(), db=test_db
        )
        assert res["status"] == "already_running"
        assert lidarr_worker._pending == []

    def test_retry_route_reports_success_when_queued(self, app_and_client, test_db, test_config, seeded_users):
        _, client = app_and_client
        _configure_lidarr(test_db)
        _set_mode(test_db, "lidarr")
        test_db.create_request(
            MusicRequest(
                id="req-q", user_id=seeded_users["alice"]["id"], item_type="album", title="Dummy",
                artist="Portishead", status=RequestStatus.PROCESSING,
            )
        )
        with patch("trackseerr.lidarr_queue.lidarr_worker.start_trickle", return_value={"status": "queued"}):
            resp = client.post(
                "/api/requests/req-q/retry", headers=_headers(seeded_users["admin"], test_db, test_config)
            )
        assert resp.status_code == 200
        assert resp.json()["success"] is True


# --------------------------------------------------------------------------- Lidarr client redaction


class TestLidarrClientRedaction:
    def test_add_artist_error_message_redacts_apikey(self):
        from trackseerr.clients.lidarr import LidarrClient

        lc = LidarrClient("http://lidarr.test", API_KEY, root_folder="/music")
        leaky = httpx.ConnectError("failed http://lidarr.test/api/v1/artist?apikey=SECRETKEY123&x=1")
        with patch("trackseerr.clients.lidarr.httpx.Client") as cls:
            cls.return_value.__enter__.return_value.get.side_effect = leaky
            res = lc.add_artist_and_albums("Queen", [], auto_search=False)
        assert res["status"] == "error"
        assert "SECRETKEY123" not in res["message"]

    def test_generic_exception_text_is_type_only_and_logs_are_redacted(self, caplog):
        from trackseerr.clients.lidarr import LidarrClient

        lc = LidarrClient("http://lidarr.test", API_KEY, root_folder="/music")
        with patch("trackseerr.clients.lidarr.httpx.Client") as cls, caplog.at_level("WARNING"):
            cls.return_value.__enter__.return_value.get.side_effect = RuntimeError("boom ?apikey=SECRETKEY123")
            res = lc.add_artist_and_albums("Queen", [], auto_search=False)
            assert lc.get_all_artists() == []
        assert "SECRETKEY123" not in res["message"]
        assert "SECRETKEY123" not in caplog.text


class TestSearchOnAddPrecedence:
    def test_search_on_add_wins_over_stale_auto_search(self, test_db):
        test_db.update_lidarr_settings({"url": "http://l", "api_key": "k", "auto_search": True})
        res = test_db.update_lidarr_settings({"search_on_add": False, "auto_search": True})
        assert res["search_on_add"] is False and res["auto_search"] is False
        res = test_db.update_lidarr_settings({"auto_search": False})  # legacy callers still work alone
        assert res["search_on_add"] is False

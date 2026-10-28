"""Tests for system health API routes, HealthCheckWorker, and manual task run."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.storage import Database
from trackseerr.system_health import HealthCheckWorker


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def secret_key(tmp_path):
    """Provides a consistent secret key."""
    return get_or_create_secret_key(data_dir=str(tmp_path))


@pytest.fixture
def test_config(tmp_path):
    """Provides a test Config."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        media_server="none",
        data_dir=str(tmp_path),
    )


@pytest.fixture
def seeded_users(test_db):
    """Seeds admin and non-admin users in DB."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice}


def create_auth_cookies(test_db: Database, user: dict[str, Any], secret_key: bytes) -> dict[str, str]:
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret_key,
    )
    test_db.create_session(session_id=token, user_id=user["id"])
    return {"session_token": token}


@pytest.fixture
def app_and_client(test_db: Database, test_config: Config):
    """Creates a TestClient with injected DB and Config."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    client = TestClient(app)
    return app, client


def test_health_requires_admin(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    alice_cookies = create_auth_cookies(test_db, seeded_users["alice"], secret_key)

    # 401 unauthenticated
    assert client.get("/api/system/health").status_code == 401
    assert client.get("/api/system/health/settings").status_code == 401
    assert client.put("/api/system/health/settings", json={"indexer_fail_minutes": 10}).status_code == 401

    # 403 non-admin
    resp_get = client.get("/api/system/health", cookies=alice_cookies)
    assert resp_get.status_code == 403
    assert "Administrator access required" in resp_get.json()["detail"]

    resp_settings_get = client.get("/api/system/health/settings", cookies=alice_cookies)
    assert resp_settings_get.status_code == 403

    resp_settings_put = client.put(
        "/api/system/health/settings", json={"indexer_fail_minutes": 10}, cookies=alice_cookies
    )
    assert resp_settings_put.status_code == 403


def test_health_lists_checks(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    admin_cookies = create_auth_cookies(test_db, seeded_users["admin"], secret_key)

    # Empty initially
    resp_empty = client.get("/api/system/health", cookies=admin_cookies)
    assert resp_empty.status_code == 200
    body_empty = resp_empty.json()
    assert body_empty["checks"] == []
    assert body_empty["checked_at"] is None

    # Populate health check rows
    test_db.upsert_health_check(
        key="indexer:1",
        kind="indexer",
        label="Indexer Torznab",
        state="ok",
        message=None,
        failing_since=None,
        last_checked_at="2026-10-28T12:00:00Z",
    )
    test_db.upsert_health_check(
        key="disk:/media",
        kind="disk",
        label="Disk /media",
        state="error",
        message="Only 5.0 GB free on /media",
        failing_since="2026-10-28T11:00:00Z",
        last_checked_at="2026-10-28T12:30:00Z",
    )

    resp = client.get("/api/system/health", cookies=admin_cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["checked_at"] == "2026-10-28T12:30:00Z"
    assert len(body["checks"]) == 2

    checks_by_key = {c["key"]: c for c in body["checks"]}
    assert "indexer:1" in checks_by_key
    assert checks_by_key["indexer:1"]["kind"] == "indexer"
    assert checks_by_key["indexer:1"]["label"] == "Indexer Torznab"
    assert checks_by_key["indexer:1"]["state"] == "ok"
    assert checks_by_key["indexer:1"]["last_checked_at"] == "2026-10-28T12:00:00Z"

    assert "disk:/media" in checks_by_key
    assert checks_by_key["disk:/media"]["kind"] == "disk"
    assert checks_by_key["disk:/media"]["state"] == "error"
    assert checks_by_key["disk:/media"]["message"] == "Only 5.0 GB free on /media"
    assert checks_by_key["disk:/media"]["failing_since"] == "2026-10-28T11:00:00Z"
    assert checks_by_key["disk:/media"]["last_checked_at"] == "2026-10-28T12:30:00Z"


def test_settings_roundtrip(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    admin_cookies = create_auth_cookies(test_db, seeded_users["admin"], secret_key)

    # Defaults
    resp_get = client.get("/api/system/health/settings", cookies=admin_cookies)
    assert resp_get.status_code == 200
    body = resp_get.json()
    assert body["indexer_fail_minutes"] == 15
    assert body["client_fail_minutes"] == 5
    assert body["media_server_fail_minutes"] == 10
    assert body["disk_free_min_gb"] == 20
    assert body["download_stall_minutes"] == 60
    assert body["renotify_hours"] == 24

    # Update
    payload = {
        "indexer_fail_minutes": 30,
        "client_fail_minutes": 10,
        "media_server_fail_minutes": 20,
        "disk_free_min_gb": 50,
        "download_stall_minutes": 120,
        "renotify_hours": 48,
    }
    resp_put = client.put("/api/system/health/settings", json=payload, cookies=admin_cookies)
    assert resp_put.status_code == 200
    assert resp_put.json() == payload

    # Verify persisted
    resp_get2 = client.get("/api/system/health/settings", cookies=admin_cookies)
    assert resp_get2.status_code == 200
    assert resp_get2.json() == payload


def test_settings_rejects_negative(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    admin_cookies = create_auth_cookies(test_db, seeded_users["admin"], secret_key)

    # Negative int rejected with 422
    resp_neg = client.put(
        "/api/system/health/settings", json={"indexer_fail_minutes": -1}, cookies=admin_cookies
    )
    assert resp_neg.status_code == 422

    # Exceeding bounds rejected with 422
    resp_over = client.put(
        "/api/system/health/settings", json={"disk_free_min_gb": 100001}, cookies=admin_cookies
    )
    assert resp_over.status_code == 422


def test_manual_run_health_check_task(app_and_client, seeded_users, secret_key, test_db):
    _, client = app_and_client
    alice_cookies = create_auth_cookies(test_db, seeded_users["alice"], secret_key)
    admin_cookies = create_auth_cookies(test_db, seeded_users["admin"], secret_key)

    # Non-admin forbidden
    assert client.post("/api/system/tasks/health_check/run", cookies=alice_cookies).status_code == 403

    # Admin triggers run
    with patch(
        "trackseerr.system_health.run_health_checks",
        return_value={"checked": 4, "errors": 1, "warnings": 0, "alerts": 0},
    ) as mock_run:
        resp = client.post("/api/system/tasks/health_check/run", cookies=admin_cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

        # Wait briefly for thread execution to finish
        deadline = time.monotonic() + 5.0
        runs: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            runs = test_db.list_task_runs("health_check", "1970-01-01T00:00:00Z")
            if runs and runs[0]["status"] == "success":
                break
            time.sleep(0.05)

        mock_run.assert_called_once()
        assert len(runs) >= 1
        assert runs[0]["status"] == "success"
        assert runs[0]["message"] == "4 checked, 1 failing, 0 alerts"


def test_worker_run_once_records_run(test_db, test_config):
    worker = HealthCheckWorker()
    with patch(
        "trackseerr.system_health.run_health_checks",
        return_value={"checked": 3, "errors": 1, "warnings": 0, "alerts": 0},
    ) as mock_run:
        ok = worker.run_once(test_db, test_config)
        assert ok is True
        mock_run.assert_called_once()

    runs = test_db.list_task_runs("health_check", "1970-01-01T00:00:00Z")
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert runs[0]["trigger"] == "scheduled"
    assert runs[0]["message"] == "3 checked, 1 failing, 0 alerts"


def test_worker_idle_run_discarded(test_db, test_config):
    worker = HealthCheckWorker()
    with patch(
        "trackseerr.system_health.run_health_checks",
        return_value={"checked": 0, "errors": 0, "warnings": 0, "alerts": 0},
    ) as mock_run:
        ok = worker.run_once(test_db, test_config)
        assert ok is True
        mock_run.assert_called_once()

    runs = test_db.list_task_runs("health_check", "1970-01-01T00:00:00Z")
    assert len(runs) == 0

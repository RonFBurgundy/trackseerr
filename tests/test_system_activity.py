"""Tests for GET /api/system/activity playlist sync progress visibility."""

from trackseerr import task_manager
from trackseerr.api.routes.sync import sync_state
from tests.test_system_status import (  # noqa: F401
    app_and_client,
    create_auth_cookies,
    secret_key,
    seeded_users,
    test_config,
    test_db,
)


def test_activity_shows_playlist_sync_progress(app_and_client, seeded_users, secret_key, test_db, monkeypatch):
    """GET /api/system/activity includes playlist_sync progress with playlist and server name."""
    _, client = app_and_client
    cookies = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    monkeypatch.setattr(sync_state, "progress", (1, 3, "Road Trip", "Plex"))
    with task_manager.record_task_run(test_db, "playlist_sync", task_manager.TRIGGER_MANUAL):
        resp = client.get("/api/system/activity", cookies=cookies)
    assert resp.status_code == 200
    running = resp.json()["running"]
    entry = next(r for r in running if r["task_id"] == "playlist_sync")
    assert entry["progress"] == {
        "current": 1,
        "total": 3,
        "message": "Syncing 'Road Trip' to Plex",
    }


def test_activity_shows_playlist_matching_before_server_push(app_and_client, seeded_users, secret_key, test_db, monkeypatch):
    """GET /api/system/activity reports 'Matching <playlist>' when no server push is active yet."""
    _, client = app_and_client
    cookies = create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    monkeypatch.setattr(sync_state, "progress", (2, 4, "Chill Mix", None))
    with task_manager.record_task_run(test_db, "playlist_sync", task_manager.TRIGGER_MANUAL):
        resp = client.get("/api/system/activity", cookies=cookies)
    assert resp.status_code == 200
    running = resp.json()["running"]
    entry = next(r for r in running if r["task_id"] == "playlist_sync")
    assert entry["progress"] == {
        "current": 2,
        "total": 4,
        "message": "Matching 'Chill Mix'",
    }

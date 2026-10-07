"""Strict-mode backstop for ``/api/missing`` (wave 2b). Lidarr status/push/queue are covered by test_missing.py."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plex_playlist_sync.api.dependencies import get_media_client
from tests._rm_helpers import admin, alice, client, config, db, ok  # noqa: F401

M = "/api/missing"


@pytest.fixture
def seeded(db):
    db.upsert_user("admin-1", "admin_user", "a@example.com", is_admin=True)
    db.upsert_playlist("pl-1", name="Rock", service="spotify", creator_id="admin-1")
    db.record_sync_result("pl-1", "partial", missing_tracks=[
        {"title": "Airbag", "artist": "Radiohead", "album": "OK Computer"},
        {"title": "Lucky", "artist": "Radiohead", "album": ""},
    ])


def test_list_and_native_lidarr_status(client, seeded, admin, alice):
    rows = ok(client.get(M, headers=admin))
    assert {r["title"] for r in rows} == {"Airbag", "Lucky"} and rows[0]["playlist_id"] == "pl-1"
    assert len(ok(client.get(f"{M}?playlist_id=pl-1", headers=admin))) == 2
    assert client.get(M, headers=alice).status_code == 403
    assert ok(client.get(f"{M}/lidarr/status", headers=admin)) == {"mode": "native", "connected": None}
    assert ok(client.get(f"{M}/lidarr/queue", headers=admin))["is_running"] is False


def test_match_overrides_flow(client, db, seeded, admin, alice):
    body = {"source_title": "Airbag", "source_artist": "Radiohead", "plex_rating_key": "123",
            "plex_title": "Airbag", "plex_artist": "Radiohead"}
    created = ok(client.post(f"{M}/match", json=body, headers=admin), 201)
    assert created["status"] == "matched" and created["override"]["created_by"] == "admin-1"
    listed = ok(client.get(f"{M}/matches", headers=admin))
    assert listed[0]["plex_rating_key"] == "123"
    assert [r["title"] for r in ok(client.get(M, headers=admin))] == ["Lucky"]
    assert client.delete(f"{M}/match/{listed[0]['id']}", headers=alice).status_code == 403
    assert ok(client.delete(f"{M}/match/{listed[0]['id']}", headers=admin)) == {"status": "deleted", "id": listed[0]["id"]}


def test_grab_missing_track(client, db, seeded, admin):
    track_id = ok(client.get(M, headers=admin))[0]["id"]
    delayed = {"success": False, "delayed": True, "pending_id": 4, "release_at": "2026-10-07T10:00:00+00:00", "message": "held"}
    grabbed = {"success": True, "download_id": "dl-1", "download_hash": "abc", "release": "Radiohead - Airbag", "client": "qbit", "score": 90}
    for result in (delayed, grabbed):
        with patch("plex_playlist_sync.api.routes.missing.acquisition_coordinator.search_and_grab", return_value=result):
            assert ok(client.post(f"{M}/{track_id}/grab", headers=admin)) == result


def test_search_media_server_tracks(client, config, admin):
    hits = [{"rating_key": "9", "title": "Airbag", "artist": "Radiohead", "album": "OK", "duration": 284000, "thumb": "/t"},
            {"id": "7", "rating_key": "7", "title": "Lucky", "artist": "Radiohead", "album": None, "duration": 249.0}]
    server = SimpleNamespace(search_tracks=lambda q, limit=15: hits)
    client.app.dependency_overrides[get_media_client] = lambda: object()
    with patch("plex_playlist_sync.api.routes.missing.as_media_server", return_value=server):
        assert ok(client.get(f"{M}/search?query=air", headers=admin)) == hits

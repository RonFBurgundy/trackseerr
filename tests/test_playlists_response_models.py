"""Strict-mode backstop for ``/api/playlists`` (wave 2b), admin and non-admin."""

import pytest

from plex_playlist_sync.api.dependencies import get_deezer_client, get_media_client, get_spotify_client
from tests._rm_helpers import admin, alice, client, config, db, ok  # noqa: F401

P = "/api/playlists"


@pytest.fixture
def api(client):
    client.app.dependency_overrides[get_spotify_client] = lambda: None
    client.app.dependency_overrides[get_deezer_client] = lambda: None
    client.app.dependency_overrides[get_media_client] = lambda: None
    return client


def test_create_list_and_settings_flow(api, db, admin, alice):
    created = ok(api.post(P, json={"url_or_id": "3155776842", "service": "deezer"}, headers=alice), 201)
    assert created["creator_id"] == "alice-1" and created["targets"] == ["alice-1"] and created["monitor_mode"] == "none"  # no auto-request permission
    pid = created["id"]
    listed = ok(api.get(P, headers=alice))
    assert [p["id"] for p in listed] == [pid] and listed[0]["tracks_json"] is None
    assert [p["id"] for p in ok(api.get(P, headers=admin))] == [pid]
    assert ok(api.put(f"{P}/{pid}/targets", json={"user_ids": ["alice-1"]}, headers=alice)) == {"id": pid, "targets": ["alice-1"]}
    assert ok(api.put(f"{P}/{pid}/enabled", json={"enabled": False, "monitor_mode": "none"}, headers=alice)) == {
        "id": pid, "enabled": False, "monitor_mode": "none",
    }
    assert api.put(f"{P}/{pid}/monitor-mode", json={"monitor_mode": "track"}, headers=alice).status_code == 403
    db.update_user_admin_fields("alice-1", {"permissions": 34 | 128})
    assert ok(api.put(f"{P}/{pid}/monitor-mode", json={"monitor_mode": "track"}, headers=alice)) == {"id": pid, "monitor_mode": "track"}
    assert ok(api.put(f"{P}/{pid}/monitor-mode", json={"monitor_mode": "artist"}, headers=admin)) == {"id": pid, "monitor_mode": "artist"}
    assert ok(api.delete(f"{P}/{pid}", headers=alice)) == {"status": "deleted", "id": pid}
    assert ok(api.get(P, headers=admin)) == []


def test_direct_and_m3u_import(api, db, admin, alice):
    body = {"name": "Road Trip", "service": "spotify", "tracks": [{"title": "Airbag", "artist": "Radiohead"}, {"title": "Lucky"}]}
    for headers in (admin, alice):
        data = ok(api.post(f"{P}/import", json=body, headers=headers), 201)
        assert data["track_count"] == 2 and data["status"] == "imported" and data["id"].startswith("imp_")
    stored = ok(api.get(P, headers=admin))[0]
    assert stored["tracks_json"] and stored["service"] == "spotify"
    m3u = "#EXTM3U\n#EXTINF:200,Radiohead - Airbag\n/music/a.flac\n"
    data = ok(api.post(f"{P}/import/m3u", json={"content": m3u, "name": "From M3U"}, headers=admin), 201)
    assert data["service"] == "m3u" and data["track_count"] == 1

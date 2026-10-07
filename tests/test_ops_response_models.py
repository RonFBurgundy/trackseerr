"""Strict-mode backstop for the typed discovery, requests, activity, wanted, system and sync responses.

``ApiModel`` forbids undeclared keys under test, so each request fails if a route returns a key its model lacks.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db, get_discovery_client
from plex_playlist_sync.api.routes.sync import sync_state
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.storage import Database
from tests.test_activity_wanted import QUEUE_REC, _dl, _library, _lidarr_mode, _mock_http, _page, _resp


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "t.db"))
    yield d
    d.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def discovery() -> MagicMock:
    return MagicMock()


@pytest.fixture
def client(db, config, discovery) -> TestClient:
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_discovery_client] = lambda: discovery
    return TestClient(app)


def _headers(db, config, user) -> dict[str, str]:
    key = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=key)
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(db, config):
    return _headers(db, config, db.upsert_user("admin-1", "root", "a@x.tv", is_admin=True))


@pytest.fixture
def alice(db, config):
    return _headers(db, config, db.upsert_user("1001", "alice", "al@x.tv", is_admin=False))


def _ok(resp, status: int = 200) -> Any:
    assert resp.status_code == status, resp.text
    return resp.json()


# ------------------------------------------------------------------------------------------------------ discovery

TRACK = {
    "id": "deezer:track:1", "item_type": "track", "title": "Airbag", "artist": "Radiohead", "album": "OK Computer",
    "cover_url": "https://img/1.jpg", "preview_url": "https://prev/1.mp3", "release_date": "1997-05-21", "status": "none",
    "artist_discovery_id": "deezer:artist:399", "album_discovery_id": "deezer:album:1",
}
ALBUM_ITEM = {
    "id": "deezer:album:1", "item_type": "album", "title": "OK Computer", "artist": "Radiohead", "album": "OK Computer",
    "cover_url": "https://img/a.jpg", "release_date": "1997-05-21", "record_type": "album", "track_count": 12,
}


def test_discovery_lists_and_search(client, discovery, admin, alice):
    discovery.get_trending.return_value = [dict(TRACK), {**ALBUM_ITEM, "preview_url": None, "status": "none"}]
    discovery.get_new_releases.return_value = [dict(TRACK)]
    discovery.search.return_value = [dict(TRACK)]
    for headers in (admin, alice):
        assert _ok(client.get("/api/discovery/trending", headers=headers))["count"] == 2
        assert _ok(client.get("/api/discovery/new-releases", headers=headers))["items"][0]["status"] == "none"
        found = _ok(client.get("/api/discovery/search?q=air", headers=headers))
        assert found["query"] == "air" and found["type"] == "all"
    # only admins get a library routing hint
    client.app.state  # noqa: B018
    from plex_playlist_sync.api.routes import discovery as d
    with patch.object(d, "_library_artist_ids_by_name", return_value={"radiohead": "lib-1"}):
        assert _ok(client.get("/api/discovery/trending", headers=admin))["items"][0]["library_artist_id"] == "lib-1"
        assert "library_artist_id" not in _ok(client.get("/api/discovery/trending", headers=alice))["items"][0]


def test_discovery_detail_routes(client, discovery, admin):
    discovery.get_album_details.return_value = {
        "id": "deezer:album:1", "item_type": "album", "title": "OK Computer", "artist": "Radiohead",
        "artist_id": "deezer:artist:399", "cover_url": "https://img/a.jpg", "release_date": "1997-05-21",
        "label": "Parlophone", "genres": ["Rock"], "duration_seconds": 3200, "track_count": 1,
        "tracks": [{
            "id": "deezer:track:1", "item_type": "track", "title": "Airbag", "artist": "Radiohead", "album": "OK Computer",
            "track_number": 1, "disc_number": 1, "duration_seconds": 284, "preview_url": "https://prev/1.mp3",
            "release_date": "1997-05-21", "album_discovery_id": "deezer:album:1",
        }],
    }
    album = _ok(client.get("/api/discovery/album/deezer:album:1", headers=admin))
    assert album["tracks"][0]["artist_discovery_id"] == "deezer:artist:399" and album["label"] == "Parlophone"
    discovery.get_track_details.return_value = {
        **TRACK, "cover_url": None, "duration": 284, "track_position": 1, "disk_number": 1, "release_date": "1997-05-21",
        "isrc": "GBAYE9700001", "explicit": False, "contributors": [{"name": "Radiohead", "role": "Main"}],
        "bpm": 120, "gain": -9.5, "label": "Parlophone", "genres": ["Rock"],
    }
    track = _ok(client.get("/api/discovery/track/deezer:track:1", headers=admin))
    assert track["contributors"][0]["role"] == "Main" and track["bpm"] == 120
    discovery.get_artist_details.return_value = {
        "id": "deezer:artist:399", "name": "Radiohead", "image_url": "https://img/r.jpg", "nb_album": 1, "nb_fan": 10,
        "albums": [dict(ALBUM_ITEM)], "singles_eps": [], "compilations": [],
    }
    artist = _ok(client.get("/api/discovery/artist/deezer:artist:399", headers=admin))
    assert artist["albums"][0]["record_type"] == "album" and artist["nb_fan"] == 10


def test_artist_profile_admin_and_non_admin(client, discovery, db, admin, alice):
    db.upsert_library_artist({"id": "lib-1", "name": "Radiohead", "clean_name": "radiohead", "monitored": True,
                              "foreign_artist_id": "deezer:artist:399"})
    db.upsert_library_album({"id": "lib-a1", "artist_id": "lib-1", "title": "OK Computer", "clean_title": "ok computer",
                             "year": 1997, "release_date": "1997-05-21", "monitored": True, "total_tracks": 12})
    db.upsert_library_album({"id": "lib-a2", "artist_id": "lib-1", "title": "Kid A", "clean_title": "kid a",
                             "year": 2000, "monitored": True, "total_tracks": 10})
    details = {
        "id": "deezer:artist:399", "name": "Radiohead", "image_url": "https://img/r.jpg", "nb_album": 1, "nb_fan": 10,
        "albums": [dict(ALBUM_ITEM)], "singles_eps": [], "compilations": [],
    }
    discovery.get_artist_details.return_value = details
    discovery.get_artist_top_tracks_detailed.return_value = [
        {"id": "deezer:track:1", "title": "Airbag", "artist": "Radiohead", "album": "OK Computer", "duration": 284,
         "preview_url": "https://prev/1.mp3"}
    ]
    discovery.search_artist.return_value = None
    discovery.search_artists.return_value = []
    url = "/api/discovery/artist-profile?discovery_id=deezer:artist:399"
    full = _ok(client.get(url, headers=admin))
    assert full["artist"]["discovery_id"] == "deezer:artist:399" and "link_confidence" in full["artist"]
    assert full["top_tracks"][0]["title"] == "Airbag" and "library_only" in full["discography"]
    shaped = _ok(client.get(url, headers=alice))
    assert shaped["library"] is None
    assert set(shaped["artist"]) == {"name", "image_url", "discovery_id"}
    for group in ("albums", "singles_eps", "compilations", "library_only"):
        for item in shaped["discography"][group]:
            assert "library_album_id" not in item and "quality" not in item
    by_lib = _ok(client.get("/api/discovery/artist-profile?library_artist_id=lib-1", headers=admin))
    assert by_lib["library"]["artist_id"] == "lib-1"
    assert client.get("/api/discovery/artist-profile?library_artist_id=lib-1", headers=alice).status_code == 403


# ------------------------------------------------------------------------------------------------------- requests


def test_requests_lifecycle(client, db, admin, alice):
    body = {"title": "Airbag", "artist": "Radiohead", "item_type": "track", "album": "OK Computer"}
    mine = _ok(client.post("/api/requests", json=body, headers=alice), 201)
    assert mine["status"] == "pending" and mine["username"] == "alice"
    listed = _ok(client.get("/api/requests", headers=alice))
    assert listed["count"] == 1 and listed["requests"][0]["id"] == mine["id"]
    batch = _ok(client.post("/api/requests/batch", json={"requests": [
        {"title": "OK Computer", "artist": "Radiohead", "item_type": "album"},
        {"title": "Kid A", "artist": "Radiohead", "item_type": "album"},
    ]}, headers=admin), 201)
    assert batch["count"] == 2
    assert _ok(client.get("/api/requests", headers=admin))["count"] == 3
    assert _ok(client.post(f"/api/requests/{mine['id']}/approve", headers=admin))["status"] == "processing"
    retry = _ok(client.post(f"/api/requests/{mine['id']}/retry", headers=admin))
    assert retry["status"] == "processing" and retry["success"] is False and "download_id" in retry
    rejected = _ok(client.post(f"/api/requests/{batch['created'][0]['id']}/reject", headers=admin))
    assert rejected["status"] == "rejected"
    other = batch["created"][1]["id"]
    assert _ok(client.delete(f"/api/requests/{other}", headers=admin)) == {"status": "deleted", "id": other}


# --------------------------------------------------------------------------------------- activity and wanted (native)


def test_activity_native(client, db, admin):
    _dl(db, "dl-1", artist="Radiohead", title="OK Computer FLAC", progress=0.5, size=1000)
    _dl(db, "dl-seed", artist="Blur", title="Parklife", status="completed", progress=1.0, size=500)
    db.conn.execute(
        "UPDATE active_downloads SET protocol='torrent', seed_ratio_target=2.0, seed_time_target_minutes=60, "
        "seed_rule_source='indexer' WHERE id='dl-seed'"
    )
    db.conn.commit()
    queue = _ok(client.get("/api/activity/queue", headers=admin))
    seeding = [r for r in queue["records"] if r["id"] == "dl-seed"][0]["seeding"]
    assert queue["mode"] == "native" and seeding["ratio_target"] == 2.0
    db.record_download_event("grabbed", artist="Radiohead", title="OK Computer", download_id="dl-1", message="Grabbed")
    hist = _ok(client.get("/api/activity/history", headers=admin))
    assert hist["records"] and hist["records"][0]["can_mark_failed"] in (True, False)
    idx = _ok(client.get("/api/activity/history/index", headers=admin))
    assert idx["sort_key"] == "date" and idx["total"] >= 1 and idx["groups"][0]["count"] >= 1
    bl = db.add_to_blocklist("Bad.Release", artist="Radiohead", album="OK Computer", protocol="torrent", reason="fake")
    page = _ok(client.get("/api/activity/blocklist", headers=admin))
    assert page["records"][0]["reason"] == "fake"
    assert _ok(client.delete(f"/api/activity/blocklist/{bl['id']}", headers=admin)) == {
        "success": True, "message": "Removed from the blocklist"}
    assert _ok(client.post("/api/activity/queue/dl-1/retry", headers=admin))["success"] in (True, False)
    assert _ok(client.delete("/api/activity/queue/dl-1?remove_from_client=false", headers=admin))["success"] is True
    failed = [r for r in hist["records"] if r["can_mark_failed"]]
    if failed:
        assert "success" in _ok(client.post(f"/api/activity/history/{failed[0]['id']}/failed", headers=admin))


def test_wanted_native(client, db, admin):
    _library(db)
    missing = _ok(client.get("/api/wanted/missing", headers=admin))
    assert missing["total"] == 2 and "current_quality" not in missing["records"][0]
    cutoff = _ok(client.get("/api/wanted/cutoff", headers=admin))
    assert cutoff["records"][0]["current_quality"] == "MP3 192"
    assert _ok(client.get("/api/wanted/missing/index", headers=admin))["total"] == 2
    assert _ok(client.get("/api/wanted/cutoff/index", headers=admin))["total"] == 1
    queued = _ok(client.post("/api/wanted/search", json={"ids": ["t-missing"]}, headers=admin))
    assert "queued" in queued
    assert "queued" in _ok(client.post("/api/wanted/search", json={"all": True, "list": "missing"}, headers=admin))


def test_activity_and_wanted_lidarr_mode(client, db, admin):
    _lidarr_mode(db)
    patcher, http = _mock_http()
    try:
        hist_rec = {"id": 5, "eventType": "grabbed", "date": "2026-10-04T10:00:00Z", "sourceTitle": "Rel",
                    "artist": {"artistName": "Radiohead"}, "album": {"title": "OK Computer"},
                    "quality": {"quality": {"name": "FLAC"}}, "data": {"indexer": "P", "downloadClient": "qBit"}}
        block_rec = {"id": 9, "sourceTitle": "Bad", "artist": {"artistName": "Radiohead"}, "date": "2026-10-04T10:00:00Z",
                     "protocol": "torrent", "indexer": "P", "message": "fake", "quality": {"quality": {"name": "MP3"}}}
        wanted_rec = {"id": 42, "title": "OK Computer", "artist": {"artistName": "Radiohead"}, "releaseDate": "1997-05-21",
                      "monitored": True, "lastSearchTime": None}
        http.get.return_value = _resp(payload=_page([QUEUE_REC]))
        assert _ok(client.get("/api/activity/queue", headers=admin))["records"][0]["eta_seconds"] == 3723
        http.get.return_value = _resp(payload=_page([hist_rec]))
        assert _ok(client.get("/api/activity/history", headers=admin))["records"][0]["can_mark_failed"] is True
        assert _ok(client.get("/api/activity/history/index", headers=admin))["groups"] == []
        http.get.return_value = _resp(payload=_page([block_rec]))
        assert _ok(client.get("/api/activity/blocklist", headers=admin))["records"][0]["quality"] == "MP3"
        http.get.return_value = _resp(payload=_page([wanted_rec]))
        assert _ok(client.get("/api/wanted/missing", headers=admin))["records"][0]["album_id"] == "42"
        assert _ok(client.get("/api/wanted/cutoff", headers=admin))["records"][0]["current_quality"] is None
        assert _ok(client.get("/api/wanted/cutoff/index", headers=admin))["groups"] == []
        http.post.return_value = _resp(payload={"id": 1})
        assert _ok(client.post("/api/wanted/search", json={"ids": ["42"]}, headers=admin)) == {"queued": 1}
    finally:
        patcher.stop()


# ----------------------------------------------------------------------------------------------- system and sync


def test_system_routes(client, db, admin, alice):
    ms = _ok(client.get("/api/system/media-server"))
    assert set(ms["capabilities"]) == {"playlists", "users", "mixes", "library_refresh", "file_paths"}
    db.record_event("task_triggered", "Task ran", source="TaskManager", severity="info", details={"task_id": "x"})
    events = _ok(client.get("/api/system/events", headers=admin))
    assert events["items"][0]["details"] == {"task_id": "x"} and events["total"] == 1
    assert client.get("/api/system/events", headers=alice).status_code == 403
    import logging
    logging.getLogger("plex_playlist_sync.test").warning("hello logs")
    logs = _ok(client.get("/api/system/logs?search=hello", headers=admin))
    assert logs and logs[-1]["level"] == "WARNING"
    assert _ok(client.get("/api/system/queue", headers=admin)).keys() == {"running", "queued", "recent"}
    assert _ok(client.get("/api/system/lidarr-health", headers=admin)) == {
        "mode": "native", "reachable": None, "version": None, "health": []}
    tasks = _ok(client.get("/api/system/tasks", headers=admin))
    assert tasks
    with patch("plex_playlist_sync.api.routes.system.threading.Thread"):
        assert _ok(client.post("/api/system/tasks/filesystem_scan/run", headers=admin))["success"] is True
    with patch("plex_playlist_sync.api.routes.system.library_scanner.cancel_scan"):
        assert _ok(client.post("/api/system/tasks/filesystem_scan/cancel", headers=admin))["message"]
    assert _ok(client.delete("/api/system/events", headers=admin)) == {"success": True}
    assert _ok(client.delete("/api/system/logs", headers=admin)) == {"success": True}
    assert _ok(client.get("/api/system/status", headers=admin))["workers"]


def test_system_lidarr_health_in_lidarr_mode(client, db, admin):
    _lidarr_mode(db)
    patcher, http = _mock_http()
    try:
        http.get.side_effect = [
            _resp(payload={"version": "2.0.0", "appName": "Lidarr"}),
            _resp(payload=[{"source": "IndexerCheck", "type": "warning", "message": "slow", "wikiUrl": "https://wiki"}]),
        ]
        body = _ok(client.get("/api/system/lidarr-health", headers=admin))
    finally:
        patcher.stop()
    assert body["mode"] == "lidarr" and body["reachable"] in (True, False) and isinstance(body["health"], list)


def test_sync_routes(client, admin, alice):
    with patch.object(sync_state, "execute_sync", return_value={"status": "success"}):
        assert _ok(client.post("/api/sync", headers=admin)) == {
            "status": "started", "message": "Synchronization triggered successfully in background"}
        hook = _ok(client.post("/api/sync/webhook", content=json.dumps({"artist": {"name": "Nobody"}}), headers=admin))
        assert hook["status"] == "triggered" and hook["fulfilled_requests"] == 0
    status = _ok(client.get("/api/sync/status", headers=admin))
    assert status["is_syncing"] is False and set(status["last_run_stats"]) == {
        "total_playlists", "success_count", "total_matched", "total_missing"}
    assert client.get("/api/sync/status", headers=alice).status_code == 403

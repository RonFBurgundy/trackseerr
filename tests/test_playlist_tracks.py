"""Playlist track snapshot on add, GET /api/playlists/{id}/tracks, and live re-fetch during sync."""

import json
from unittest.mock import MagicMock

import pytest
import requests
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_deezer_client, get_spotify_client
from trackseerr.api.routes.sync import sync_state
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.models import Playlist, Track
from trackseerr.storage import Database

PL_ID = "37i9dQZF1DXcBWIGoYBM5M"
URL = f"https://open.spotify.com/playlist/{PL_ID}"


def _tracks(n: int = 3) -> list[Track]:
    return [Track(title=f"Song {i}", artist=f"Artist {i}", album=f"Album {i}") for i in range(1, n + 1)]


@pytest.fixture
def test_db():
    db = Database(":memory:")
    db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    db.upsert_user("user-bob", "bob", "bob@plex.tv", is_admin=False)
    db.upsert_user("user-carol", "carol", "carol@plex.tv", is_admin=False)
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    return Config(
        plex_url="",
        plex_token="",
        data_dir=str(tmp_path),
        spotify_client_id="id",
        spotify_client_secret="secret",
    )


@pytest.fixture
def secret_key(tmp_path):
    return get_or_create_secret_key(data_dir=str(tmp_path))


@pytest.fixture
def mock_spotify():
    m = MagicMock()
    m.get_playlist_by_id.return_value = Playlist(id=PL_ID, name="Mix", description="d", poster="")
    m.get_playlist_tracks.return_value = _tracks(3)
    return m


@pytest.fixture
def app_and_client(test_db, test_config, mock_spotify):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    app.dependency_overrides[get_spotify_client] = lambda: mock_spotify
    app.dependency_overrides[get_deezer_client] = lambda: None
    return app, TestClient(app)


def _cookies(db, uid, username, is_admin, secret_key):
    token = create_session_token(user_id=uid, username=username, is_admin=is_admin, secret_key=secret_key)
    db.create_session(session_id=token, user_id=uid)
    return {"session_token": token}


def _add(client, cookies, targets=None):
    body = {"url_or_id": URL}
    if targets is not None:
        body["targets"] = targets
    return client.post("/api/playlists", json=body, cookies=cookies)


def test_create_spotify_playlist_stores_track_snapshot(app_and_client, test_db, secret_key):
    _, client = app_and_client
    resp = _add(client, _cookies(test_db, "admin-1", "admin_user", True, secret_key), ["admin-1"])
    assert resp.status_code == 201
    stored = json.loads(test_db.get_playlist(PL_ID)["tracks_json"])
    assert [t["title"] for t in stored] == ["Song 1", "Song 2", "Song 3"]


def test_create_survives_track_fetch_error(app_and_client, test_db, secret_key, mock_spotify):
    _, client = app_and_client
    mock_spotify.get_playlist_tracks.side_effect = requests.RequestException("boom")
    resp = _add(client, _cookies(test_db, "admin-1", "admin_user", True, secret_key), ["admin-1"])
    assert resp.status_code == 201
    assert test_db.get_playlist(PL_ID)["tracks_json"] is None


def test_tracks_endpoint_pending_before_sync(app_and_client, test_db, secret_key):
    _, client = app_and_client
    ck = _cookies(test_db, "admin-1", "admin_user", True, secret_key)
    _add(client, ck, ["admin-1"])
    data = client.get(f"/api/playlists/{PL_ID}/tracks", cookies=ck).json()
    assert data["total"] == 3
    assert data["synced"] is False
    assert {t["status"] for t in data["tracks"]} == {"pending"}
    assert [t["position"] for t in data["tracks"]] == [1, 2, 3]


def test_tracks_endpoint_marks_missing_after_sync(app_and_client, test_db, secret_key):
    _, client = app_and_client
    ck = _cookies(test_db, "admin-1", "admin_user", True, secret_key)
    _add(client, ck, ["admin-1"])
    test_db.record_sync_result(
        PL_ID, status="success", missing_tracks=[Track(title="SONG 2", artist="artist 2 ", album="Album 2")]
    )
    data = client.get(f"/api/playlists/{PL_ID}/tracks", cookies=ck).json()
    by_title = {t["title"]: t for t in data["tracks"]}
    assert data["synced"] is True
    assert data["missing"] == 1
    assert by_title["Song 2"]["status"] == "missing"
    assert by_title["Song 2"]["missing_track_id"] == test_db.get_missing_tracks(PL_ID)[0]["id"]
    assert by_title["Song 1"]["status"] == "matched"
    assert by_title["Song 3"]["status"] == "matched"


def test_tracks_endpoint_appends_unmatched_missing_rows(app_and_client, test_db, secret_key):
    _, client = app_and_client
    ck = _cookies(test_db, "admin-1", "admin_user", True, secret_key)
    _add(client, ck, ["admin-1"])
    test_db.record_sync_result(
        PL_ID, status="success", missing_tracks=[Track(title="Ghost", artist="Nobody", album="")]
    )
    data = client.get(f"/api/playlists/{PL_ID}/tracks", cookies=ck).json()
    assert data["total"] == 4
    assert data["tracks"][-1]["title"] == "Ghost"
    assert data["tracks"][-1]["status"] == "missing"
    assert data["tracks"][-1]["position"] == 4
    assert data["missing"] == 1


def test_tracks_endpoint_access(app_and_client, test_db, secret_key):
    _, client = app_and_client
    alice = _cookies(test_db, "user-alice", "alice", False, secret_key)
    assert _add(client, alice, []).status_code == 201  # alice is the creator
    test_db.set_playlist_targets(PL_ID, ["user-bob"])
    path = f"/api/playlists/{PL_ID}/tracks"
    assert client.get(path, cookies=alice).status_code == 200
    bob = _cookies(test_db, "user-bob", "bob", False, secret_key)
    assert client.get(path, cookies=bob).status_code == 200
    carol = _cookies(test_db, "user-carol", "carol", False, secret_key)
    assert client.get(path, cookies=carol).status_code == 404
    assert client.get("/api/playlists/does-not-exist/tracks", cookies=alice).status_code == 404
    assert TestClient(client.app).get(path).status_code == 401


def _sync(db, config, spotify):
    return sync_state.execute_sync(
        db=db, config=config, plex_client=None, spotify_client=spotify, deezer_client=None
    )


def test_sync_refetches_live_and_updates_snapshot(test_db, test_config, mock_spotify):
    old = [{"title": "Song 1", "artist": "Artist 1", "album": "Album 1"}]
    test_db.upsert_playlist(
        playlist_id=PL_ID, name="Mix", service="spotify", tracks_json=json.dumps(old), creator_id="admin-1"
    )
    mock_spotify.get_playlist_tracks.return_value = _tracks(2)
    res = _sync(test_db, test_config, mock_spotify)
    assert res["status"] == "success"
    mock_spotify.get_playlist_tracks.assert_called_once_with(PL_ID)
    assert len(json.loads(test_db.get_playlist(PL_ID)["tracks_json"])) == 2


def test_sync_imported_playlist_still_uses_stored_list(test_db, test_config, mock_spotify):
    stored = [{"title": "Song 1", "artist": "Artist 1", "album": "Album 1"}]
    test_db.upsert_playlist(
        playlist_id="imp_abc", name="Imp", service="spotify", tracks_json=json.dumps(stored), creator_id="admin-1"
    )
    _sync(test_db, test_config, mock_spotify)
    mock_spotify.get_playlist_tracks.assert_not_called()
    assert json.loads(test_db.get_playlist("imp_abc")["tracks_json"]) == stored


def test_sync_empty_live_result_keeps_snapshot(test_db, test_config, mock_spotify):
    stored = [{"title": "Song 1", "artist": "Artist 1", "album": "Album 1", "url": ""}]
    test_db.upsert_playlist(
        playlist_id=PL_ID, name="Mix", service="spotify", tracks_json=json.dumps(stored), creator_id="admin-1"
    )
    mock_spotify.get_playlist_tracks.return_value = []
    res = _sync(test_db, test_config, mock_spotify)
    assert res["status"] == "success"
    assert json.loads(test_db.get_playlist(PL_ID)["tracks_json"]) == stored
    # the stored track was still matched natively (it is not in the empty library, so it is missing)
    assert [m["title"] for m in test_db.get_missing_tracks(PL_ID)] == ["Song 1"]

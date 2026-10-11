"""A non-admin adding a playlist another user already added subscribes to it instead of getting 404."""

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_deezer_client, get_spotify_client
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.models import Playlist, Track
from trackseerr.storage import Database

PL_ID = "37i9dQZF1DXcBWIGoYBM5M"
URL = f"https://open.spotify.com/playlist/{PL_ID}"


@pytest.fixture
def test_db():
    db = Database(":memory:")
    db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    yield db
    db.close()


@pytest.fixture
def secret_key(tmp_path):
    return get_or_create_secret_key(data_dir=str(tmp_path))


@pytest.fixture
def mock_spotify():
    m = MagicMock()
    m.get_playlist_by_id.return_value = Playlist(id=PL_ID, name="Mix", description="d", poster="")
    m.get_playlist_tracks.return_value = [
        Track(title=f"Song {i}", artist=f"Artist {i}", album=f"Album {i}") for i in range(1, 4)
    ]
    return m


@pytest.fixture
def client(test_db, tmp_path, mock_spotify):
    config = Config(
        plex_url="", plex_token="", data_dir=str(tmp_path), spotify_client_id="id", spotify_client_secret="secret"
    )
    app = create_app(db=test_db, config=config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_spotify_client] = lambda: mock_spotify
    app.dependency_overrides[get_deezer_client] = lambda: None
    return TestClient(app)


def _cookies(db, uid, username, is_admin, secret_key):
    token = create_session_token(user_id=uid, username=username, is_admin=is_admin, secret_key=secret_key)
    db.create_session(session_id=token, user_id=uid)
    return {"session_token": token}


@pytest.fixture
def admin(test_db, secret_key):
    return _cookies(test_db, "admin-1", "admin_user", True, secret_key)


@pytest.fixture
def alice(test_db, secret_key):
    return _cookies(test_db, "user-alice", "alice", False, secret_key)


def _add(client, cookies, targets=None):
    body = {"url_or_id": URL}
    if targets is not None:
        body["targets"] = targets
    return client.post("/api/playlists", json=body, cookies=cookies)


def test_user_subscribes_to_playlist_added_by_admin(client, test_db, admin, alice, mock_spotify):
    assert _add(client, admin, ["admin-1"]).status_code == 201
    before = test_db.get_playlist(PL_ID)
    mock_spotify.get_playlist_by_id.return_value = Playlist(id=PL_ID, name="Changed", description="x", poster="")
    mock_spotify.get_playlist_by_id.reset_mock()

    resp = _add(client, alice)

    assert resp.status_code == 201
    body = resp.json()
    assert body["subscribed"] is True
    assert sorted(body["targets"]) == ["admin-1", "user-alice"]
    after = test_db.get_playlist(PL_ID)
    assert after["creator_id"] == before["creator_id"] == "admin-1"
    assert after["name"] == before["name"] == "Mix"
    assert json.loads(after["tracks_json"]) == json.loads(before["tracks_json"])
    mock_spotify.get_playlist_by_id.assert_not_called()

    listed = client.get("/api/playlists", cookies=alice).json()
    assert PL_ID in [p["id"] for p in listed]
    assert client.get(f"/api/playlists/{PL_ID}/tracks", cookies=alice).status_code == 200


def test_resubscribing_is_idempotent(client, test_db, admin, alice):
    _add(client, admin, ["admin-1"])
    _add(client, alice)
    resp = _add(client, alice)
    assert resp.status_code == 201
    assert sorted(test_db.get_playlist_targets(PL_ID)) == ["admin-1", "user-alice"]


def test_subscriber_still_blocked_from_owner_only_routes(client, admin, alice):
    _add(client, admin, ["admin-1"])
    _add(client, alice)
    assert client.put(f"/api/playlists/{PL_ID}/targets", json={"user_ids": ["user-alice"]}, cookies=alice).status_code == 404
    assert client.put(f"/api/playlists/{PL_ID}/enabled", json={"enabled": False}, cookies=alice).status_code == 404
    assert (
        client.put(f"/api/playlists/{PL_ID}/monitor-mode", json={"monitor_mode": "track"}, cookies=alice).status_code
        == 404
    )
    assert client.delete(f"/api/playlists/{PL_ID}", cookies=alice).status_code == 404


def test_owner_readding_own_playlist_upserts(client, test_db, alice, mock_spotify):
    first = _add(client, alice)
    assert first.status_code == 201
    assert not first.json().get("subscribed")
    mock_spotify.get_playlist_by_id.return_value = Playlist(id=PL_ID, name="Renamed", description="d", poster="")
    resp = _add(client, alice)
    assert resp.status_code == 201
    assert not resp.json().get("subscribed")
    assert test_db.get_playlist(PL_ID)["name"] == "Renamed"

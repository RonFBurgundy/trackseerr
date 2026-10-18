"""Tests for artist-type discovery search and library status annotation."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_discovery_client, get_lidarr_client
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.discovery import DiscoveryClient
from trackseerr.config import Config
from trackseerr.storage import Database


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def config(tmp_path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


def _headers(db: Database, config: Config, user_id: str, is_admin: bool) -> dict[str, str]:
    user = db.upsert_user(user_id, user_id, f"{user_id}@x.tv", is_admin=is_admin)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=user["id"], username=user_id, is_admin=is_admin, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api(db, config):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_lidarr_client] = lambda: None
    return app, TestClient(app), _headers(db, config, "root", True)


def test_search_artist_type_returns_artists():
    client = DiscoveryClient(ttl_seconds=60)
    payload = {
        "data": [
            {
                "id": 1,
                "name": "Radiohead",
                "picture_xl": "https://img.deezer.com/radiohead_xl.jpg",
                "nb_fan": 1000,
            },
            {
                "id": 2,
                "name": "The Smile",
                "picture_xl": "https://img.deezer.com/smile_xl.jpg",
                "nb_fan": 500,
            },
        ]
    }
    with patch.object(client.session, "get", return_value=_resp(payload)):
        items = client.search("radiohead", "artist")

    assert len(items) == 2
    assert items[0]["item_type"] == "artist"
    assert items[0]["id"] == "deezer:artist:1"
    assert items[0]["title"] == "Radiohead"
    assert items[0]["cover_url"] == "https://img.deezer.com/radiohead_xl.jpg"
    assert items[0]["artist_discovery_id"] == "deezer:artist:1"

    assert items[1]["item_type"] == "artist"
    assert items[1]["id"] == "deezer:artist:2"
    assert items[1]["title"] == "The Smile"
    assert items[1]["cover_url"] == "https://img.deezer.com/smile_xl.jpg"
    assert items[1]["artist_discovery_id"] == "deezer:artist:2"


def test_search_all_excludes_artists():
    client = DiscoveryClient(ttl_seconds=60)

    requested_urls = []

    def mock_get(url, **kwargs):
        requested_urls.append(url)
        return _resp({"data": [], "results": []})

    with patch.object(client.session, "get", side_effect=mock_get):
        client.search("x", "all")

    assert not any("/search/artist" in u for u in requested_urls)


def test_search_artist_failure_not_cached():
    client = DiscoveryClient(ttl_seconds=60)

    good_payload = {
        "data": [
            {
                "id": 1,
                "name": "Radiohead",
                "picture_xl": "https://img.deezer.com/radiohead_xl.jpg",
                "nb_fan": 1000,
            }
        ]
    }

    # Call 1: HTTP 500 failure
    with patch.object(client.session, "get", return_value=_resp({}, status=500)):
        res1 = client.search("radiohead", "artist")
    assert res1 == []

    # Call 2: HTTP 200 success with good payload - should return artists (not cached empty)
    with patch.object(client.session, "get", return_value=_resp(good_payload, status=200)):
        res2 = client.search("radiohead", "artist")
    assert len(res2) == 1
    assert res2[0]["id"] == "deezer:artist:1"
    assert res2[0]["title"] == "Radiohead"


def test_search_route_artist_marks_in_library(api, db):
    app, client, headers = api

    db.upsert_library_artist({
        "id": "a1",
        "name": "Radiohead",
        "foreign_artist_id": "deezer:artist:1",
    })

    artist_item = {
        "id": "deezer:artist:1",
        "item_type": "artist",
        "title": "Radiohead",
        "artist": "Radiohead",
        "cover_url": "https://img.deezer.com/radiohead.jpg",
        "artist_discovery_id": "deezer:artist:1",
    }
    not_in_lib_artist = {
        "id": "deezer:artist:2",
        "item_type": "artist",
        "title": "The Smile",
        "artist": "The Smile",
        "cover_url": "https://img.deezer.com/smile.jpg",
        "artist_discovery_id": "deezer:artist:2",
    }

    mock_discovery = MagicMock(spec=DiscoveryClient)
    mock_discovery.search.return_value = [artist_item, not_in_lib_artist]
    app.dependency_overrides[get_discovery_client] = lambda: mock_discovery

    resp = client.get("/api/discovery/search?q=radiohead&type=artist", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    items = data["items"]
    assert len(items) == 2
    assert items[0]["id"] == "deezer:artist:1"
    assert items[0]["status"] == "in_library"
    assert items[1]["id"] == "deezer:artist:2"
    assert items[1]["status"] == "none"

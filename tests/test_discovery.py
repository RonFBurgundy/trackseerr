"""Comprehensive unit tests for Zero-Key Music Discovery client and REST API endpoints."""

from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_discovery_client, get_plex_client
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.discovery import DiscoveryClient
from trackseerr.config import Config
from trackseerr.models import MusicRequest, RequestStatus
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    """Provides a test Config pointing to tmp_path."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        data_dir=str(tmp_path),
    )


@pytest.fixture
def seeded_user(test_db):
    """Seeds a test user."""
    return test_db.upsert_user("user-test", "testuser", "test@plex.tv", is_admin=False)


@pytest.fixture
def app_and_client(test_db, test_config):
    """Creates a FastAPI test client with injected test database and config."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config

    client = TestClient(app)
    return app, client


def _auth_headers(user: dict, test_db: Database, config: Config) -> dict[str, str]:
    """Generates an authenticated Bearer header for a given user."""
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


# =============================================================================
# 1. DiscoveryClient Unit Tests
# =============================================================================


class TestDiscoveryClient:
    def test_get_trending_deezer_success(self):
        client = DiscoveryClient(ttl_seconds=60)
        mock_tracks = {
            "data": [
                {
                    "id": 101,
                    "title": "Levitating",
                    "artist": {"name": "Dua Lipa"},
                    "album": {
                        "title": "Future Nostalgia",
                        "cover_big": "https://api.deezer.com/cover/101-big.jpg",
                    },
                    "preview": "https://cdns-preview-d.dzcdn.net/preview-101.mp3",
                    "release_date": "2020-03-27",
                }
            ]
        }
        mock_albums = {
            "data": [
                {
                    "id": 202,
                    "title": "Future Nostalgia",
                    "artist": {"name": "Dua Lipa"},
                    "cover_big": "https://api.deezer.com/cover/202-big.jpg",
                    "release_date": "2020-03-27",
                }
            ]
        }

        with patch.object(client.session, "get") as mock_get:
            def side_effect(url, **kwargs):
                resp = MagicMock()
                resp.status_code = 200
                if "tracks" in url:
                    resp.json.return_value = mock_tracks
                else:
                    resp.json.return_value = mock_albums
                return resp

            mock_get.side_effect = side_effect
            results = client.get_trending(limit=10)

        assert len(results) >= 2
        track_item = next(it for it in results if it["item_type"] == "track")
        assert track_item["id"] == "deezer:track:101"
        assert track_item["title"] == "Levitating"
        assert track_item["artist"] == "Dua Lipa"
        assert track_item["preview_url"] == "https://cdns-preview-d.dzcdn.net/preview-101.mp3"

        album_item = next(it for it in results if it["item_type"] == "album")
        assert album_item["id"] == "deezer:album:202"
        assert album_item["title"] == "Future Nostalgia"

    def test_get_trending_fallback_to_itunes(self):
        client = DiscoveryClient(ttl_seconds=60)
        mock_itunes_rss = {
            "feed": {
                "entry": [
                    {
                        "im:name": {"label": "Abbey Road"},
                        "im:artist": {"label": "The Beatles"},
                        "im:image": [{"label": "http://img/170x170bb.jpg"}],
                        "id": {"attributes": {"im:id": "999"}},
                        "im:releaseDate": {"label": "1969-09-26"},
                    }
                ]
            }
        }

        with patch.object(client.session, "get") as mock_get:
            def side_effect(url, **kwargs):
                resp = MagicMock()
                if "deezer.com" in url:
                    resp.status_code = 500
                    resp.json.side_effect = Exception("Deezer down")
                else:
                    resp.status_code = 200
                    resp.json.return_value = mock_itunes_rss
                return resp

            mock_get.side_effect = side_effect
            results = client.get_trending(limit=10)

        assert len(results) == 1
        item = results[0]
        assert item["title"] == "Abbey Road"
        assert item["artist"] == "The Beatles"
        assert "600x600bb" in item["cover_url"]

    def test_get_new_releases_itunes_success(self):
        client = DiscoveryClient(ttl_seconds=60)
        mock_itunes_rss = {
            "feed": {
                "entry": [
                    {
                        "im:name": {"label": "The Dark Side of the Moon"},
                        "im:artist": {"label": "Pink Floyd"},
                        "im:image": [{"label": "http://img/170x170bb.png"}],
                        "id": {"attributes": {"im:id": "777"}},
                        "im:releaseDate": {"label": "1973-03-01"},
                    }
                ]
            }
        }

        with patch.object(client.session, "get") as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = mock_itunes_rss
            mock_get.return_value = resp

            results = client.get_new_releases(limit=10)

        assert len(results) == 1
        item = results[0]
        assert item["id"] == "itunes:album:777"
        assert item["title"] == "The Dark Side of the Moon"
        assert item["artist"] == "Pink Floyd"
        assert "600x600bb" in item["cover_url"]

    def test_search_unified_with_preview_deduplication(self):
        client = DiscoveryClient(ttl_seconds=60)

        # Deezer track response
        deezer_tracks = {
            "data": [
                {
                    "id": 555,
                    "title": "Here Comes The Sun",
                    "artist": {"name": "The Beatles"},
                    "album": {"title": "Abbey Road", "cover_big": "http://cover.jpg"},
                    "preview": "http://preview.mp3",
                }
            ]
        }
        # iTunes search response (track without preview)
        itunes_results = {
            "results": [
                {
                    "wrapperType": "track",
                    "kind": "song",
                    "trackId": 888,
                    "trackName": "Here Comes The Sun",
                    "artistName": "The Beatles",
                    "collectionName": "Abbey Road",
                    "artworkUrl100": "http://itunes/100x100bb.jpg",
                    "previewUrl": None,
                }
            ]
        }

        with patch.object(client.session, "get") as mock_get:
            def side_effect(url, **kwargs):
                resp = MagicMock()
                resp.status_code = 200
                if "deezer.com" in url:
                    resp.json.return_value = deezer_tracks
                else:
                    resp.json.return_value = itunes_results
                return resp

            mock_get.side_effect = side_effect
            results = client.search(query="Here Comes The Sun", item_type="track")

        # Deduplication merges items and keeps preview URL
        assert len(results) == 1
        assert results[0]["title"] == "Here Comes The Sun"
        assert results[0]["artist"] == "The Beatles"
        assert results[0]["preview_url"] == "http://preview.mp3"

    def test_empty_search_returns_empty(self):
        client = DiscoveryClient()
        assert client.search("") == []
        assert client.search("   ") == []

    def test_caching_and_clear_cache(self):
        client = DiscoveryClient(ttl_seconds=60)
        mock_tracks = {"data": []}

        with patch.object(client.session, "get") as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = mock_tracks
            mock_get.return_value = resp

            # 1st call
            client.get_trending(limit=10)
            assert mock_get.call_count > 0

            call_count_before = mock_get.call_count
            # 2nd call hits cache
            client.get_trending(limit=10)
            assert mock_get.call_count == call_count_before

            # Clear cache and query again
            client.clear_cache()
            client.get_trending(limit=10)
            assert mock_get.call_count > call_count_before


# =============================================================================
# 2. REST API & Status Cross-Referencing Tests
# =============================================================================


class TestDiscoveryAPI:
    def test_unauthenticated_request_rejected(self, app_and_client):
        _, client = app_and_client
        resp = client.get("/api/discovery/trending")
        assert resp.status_code == 401

    def test_trending_api_endpoint(self, app_and_client, test_db, test_config, seeded_user):
        app, client = app_and_client
        headers = _auth_headers(seeded_user, test_db, test_config)

        mock_client = MagicMock(spec=DiscoveryClient)
        mock_client.get_trending.return_value = [
            {
                "id": "deezer:album:1",
                "item_type": "album",
                "title": "Rumours",
                "artist": "Fleetwood Mac",
                "cover_url": "http://example.com/rumours.jpg",
                "preview_url": None,
                "release_date": "1977-02-04",
            }
        ]
        app.dependency_overrides[get_discovery_client] = lambda: mock_client

        resp = client.get("/api/discovery/trending", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["items"][0]["title"] == "Rumours"
        assert data["items"][0]["status"] == "none"

    def test_new_releases_api_endpoint(self, app_and_client, test_db, test_config, seeded_user):
        app, client = app_and_client
        headers = _auth_headers(seeded_user, test_db, test_config)

        mock_client = MagicMock(spec=DiscoveryClient)
        mock_client.get_new_releases.return_value = [
            {
                "id": "itunes:album:2",
                "item_type": "album",
                "title": "A Moon Shaped Pool",
                "artist": "Radiohead",
                "cover_url": "http://example.com/amsp.jpg",
                "preview_url": None,
                "release_date": "2016-05-08",
            }
        ]
        app.dependency_overrides[get_discovery_client] = lambda: mock_client

        resp = client.get("/api/discovery/new-releases", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["items"][0]["title"] == "A Moon Shaped Pool"

    def test_search_api_endpoint(self, app_and_client, test_db, test_config, seeded_user):
        app, client = app_and_client
        headers = _auth_headers(seeded_user, test_db, test_config)

        mock_client = MagicMock(spec=DiscoveryClient)
        mock_client.search.return_value = [
            {
                "id": "deezer:track:3",
                "item_type": "track",
                "title": "Paranoid Android",
                "artist": "Radiohead",
                "cover_url": "http://example.com/okc.jpg",
                "preview_url": "http://example.com/preview.mp3",
                "release_date": "1997-05-21",
            }
        ]
        app.dependency_overrides[get_discovery_client] = lambda: mock_client

        resp = client.get("/api/discovery/search?q=Radiohead&type=track", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["query"] == "Radiohead"
        assert data["count"] == 1
        assert data["items"][0]["title"] == "Paranoid Android"

    def test_status_cross_referencing_with_requests_and_plex(
        self, app_and_client, test_db, test_config, seeded_user
    ):
        app, client = app_and_client
        headers = _auth_headers(seeded_user, test_db, test_config)

        # 1. Existing pending request in DB
        test_db.create_request(
            MusicRequest(
                id="req-pending",
                user_id=seeded_user["id"],
                item_type="album",
                title="Requested Album",
                artist="Artist 1",
                foreign_id="itunes:album:req1",
                status=RequestStatus.PENDING,
            )
        )

        # 2. Existing available request in DB
        test_db.create_request(
            MusicRequest(
                id="req-avail",
                user_id=seeded_user["id"],
                item_type="track",
                title="Available Track",
                artist="Artist 2",
                foreign_id="deezer:track:req2",
                status=RequestStatus.AVAILABLE,
            )
        )

        # 3. Plex client mock that has a matching track in library
        mock_plex = MagicMock()
        mock_plex.search_library_tracks.return_value = [
            {"title": "Library Track", "artist": "Artist 3"}
        ]
        app.dependency_overrides[get_plex_client] = lambda: mock_plex

        # Mock discovery results containing all 4 states
        mock_disc = MagicMock(spec=DiscoveryClient)
        mock_disc.get_trending.return_value = [
            {"id": "itunes:album:req1", "item_type": "album", "title": "Requested Album", "artist": "Artist 1"},
            {"id": "deezer:track:req2", "item_type": "track", "title": "Available Track", "artist": "Artist 2"},
            {"id": "other:track:3", "item_type": "track", "title": "Library Track", "artist": "Artist 3"},
            {"id": "other:track:4", "item_type": "track", "title": "New Track", "artist": "Artist 4"},
        ]
        app.dependency_overrides[get_discovery_client] = lambda: mock_disc

        resp = client.get("/api/discovery/trending", headers=headers)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 4

        status_map = {it["title"]: it["status"] for it in items}
        assert status_map["Requested Album"] == "requested"
        assert status_map["Available Track"] == "available"
        assert status_map["Library Track"] == "in_library"
        assert status_map["New Track"] == "none"

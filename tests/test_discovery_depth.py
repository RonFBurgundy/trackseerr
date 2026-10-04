"""Comprehensive unit and integration tests for Discovery Depth and Batch Requests."""

from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import (
    get_config,
    get_db,
    get_discovery_client,
    get_lidarr_client,
    get_plex_client,
)
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.clients.discovery import DiscoveryClient
from plex_playlist_sync.clients.lidarr import LidarrClient
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import (
    DownloadClientConfig,
    IndexerConfig,
    MusicRequest,
    RequestStatus,
)
from plex_playlist_sync.storage import Database


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
        user_request_quota=3,
        auto_approve_requests=False,
    )


@pytest.fixture
def seeded_users(test_db):
    """Seeds admin and regular users into test DB."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    bob = test_db.upsert_user("user-bob", "bob", "bob@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice, "bob": bob}


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
# 1. DiscoveryClient Depth Unit Tests
# =============================================================================


class TestDiscoveryClientDepth:
    def test_get_album_details_deezer_success(self):
        client = DiscoveryClient(ttl_seconds=60)
        mock_deezer_album = {
            "id": 302127,
            "title": "Discovery",
            "artist": {"id": 27, "name": "Daft Punk"},
            "cover_xl": "https://api.deezer.com/cover/302127-xl.jpg",
            "cover_big": "https://api.deezer.com/cover/302127-big.jpg",
            "release_date": "2001-03-07",
            "label": "Parlophone (France)",
            "duration": 3650,
            "nb_tracks": 2,
            "genres": {"data": [{"name": "Dance"}, {"name": "Electronic"}]},
            "tracks": {
                "data": [
                    {
                        "id": 3135556,
                        "title": "One More Time",
                        "artist": {"name": "Daft Punk"},
                        "track_position": 1,
                        "disk_number": 1,
                        "duration": 320,
                        "preview": "https://cdns-preview-d.dzcdn.net/preview-1.mp3",
                    },
                    {
                        "id": 3135557,
                        "title": "Aerodynamic",
                        "artist": {"name": "Daft Punk"},
                        "track_position": 2,
                        "disk_number": 1,
                        "duration": 212,
                        "preview": "https://cdns-preview-d.dzcdn.net/preview-2.mp3",
                    },
                ]
            },
        }

        with patch.object(client.session, "get") as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = mock_deezer_album
            mock_get.return_value = resp

            album = client.get_album_details("deezer:album:302127")

        assert album is not None
        assert album["id"] == "deezer:album:302127"
        assert album["title"] == "Discovery"
        assert album["artist"] == "Daft Punk"
        assert album["artist_id"] == "deezer:artist:27"
        assert album["cover_url"] == "https://api.deezer.com/cover/302127-xl.jpg"
        assert album["release_date"] == "2001-03-07"
        assert album["label"] == "Parlophone (France)"
        assert album["genres"] == ["Dance", "Electronic"]
        assert album["duration_seconds"] == 3650
        assert album["track_count"] == 2
        assert len(album["tracks"]) == 2

        t1 = album["tracks"][0]
        assert t1["id"] == "deezer:track:3135556"
        assert t1["title"] == "One More Time"
        assert t1["artist"] == "Daft Punk"
        assert t1["album"] == "Discovery"
        assert t1["track_number"] == 1
        assert t1["disc_number"] == 1
        assert t1["duration_seconds"] == 320
        assert t1["preview_url"] == "https://cdns-preview-d.dzcdn.net/preview-1.mp3"

    def test_get_album_details_itunes_success(self):
        client = DiscoveryClient(ttl_seconds=60)
        mock_itunes_lookup = {
            "resultCount": 3,
            "results": [
                {
                    "wrapperType": "collection",
                    "collectionType": "Album",
                    "artistId": 909253,
                    "collectionId": 1440857781,
                    "artistName": "Jack Johnson",
                    "collectionName": "In Between Dreams",
                    "artworkUrl100": "https://itunes/100x100bb.jpg",
                    "releaseDate": "2005-03-01T08:00:00Z",
                    "primaryGenreName": "Rock",
                    "trackCount": 2,
                    "copyright": "℗ 2005 Jack Johnson",
                },
                {
                    "wrapperType": "track",
                    "kind": "song",
                    "trackId": 1440857785,
                    "artistName": "Jack Johnson",
                    "trackName": "Better Together",
                    "trackNumber": 1,
                    "discNumber": 1,
                    "trackTimeMillis": 207000,
                    "previewUrl": "https://audio-ssl.itunes.apple.com/preview-better.mp3",
                    "releaseDate": "2005-03-01T08:00:00Z",
                },
                {
                    "wrapperType": "track",
                    "kind": "song",
                    "trackId": 1440857786,
                    "artistName": "Jack Johnson",
                    "trackName": "Never Know",
                    "trackNumber": 2,
                    "discNumber": 1,
                    "trackTimeMillis": 212000,
                    "previewUrl": "https://audio-ssl.itunes.apple.com/preview-never.mp3",
                    "releaseDate": "2005-03-01T08:00:00Z",
                },
            ],
        }

        with patch.object(client.session, "get") as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = mock_itunes_lookup
            mock_get.return_value = resp

            album = client.get_album_details("itunes:album:1440857781")

        assert album is not None
        assert album["id"] == "itunes:album:1440857781"
        assert album["title"] == "In Between Dreams"
        assert album["artist"] == "Jack Johnson"
        assert album["artist_id"] == "itunes:artist:909253"
        assert "600x600bb" in album["cover_url"]
        assert album["label"] == "℗ 2005 Jack Johnson"
        assert album["genres"] == ["Rock"]
        assert album["track_count"] == 2
        assert album["duration_seconds"] == 419  # 207 + 212
        assert len(album["tracks"]) == 2

        t1 = album["tracks"][0]
        assert t1["id"] == "itunes:track:1440857785"
        assert t1["title"] == "Better Together"
        assert t1["track_number"] == 1
        assert t1["duration_seconds"] == 207
        assert t1["preview_url"] == "https://audio-ssl.itunes.apple.com/preview-better.mp3"

    def test_get_album_details_numeric_fallback_deezer_to_itunes(self):
        client = DiscoveryClient(ttl_seconds=60)
        mock_itunes_lookup = {
            "resultCount": 2,
            "results": [
                {
                    "wrapperType": "collection",
                    "collectionId": 500500,
                    "collectionName": "Fallback Album",
                    "artistName": "Fallback Artist",
                },
                {
                    "wrapperType": "track",
                    "kind": "song",
                    "trackId": 600600,
                    "trackName": "Track 1",
                    "trackNumber": 1,
                    "trackTimeMillis": 180000,
                },
            ],
        }

        with patch.object(client.session, "get") as mock_get:
            def side_effect(url, **kwargs):
                resp = MagicMock()
                if "deezer.com" in url:
                    resp.status_code = 404
                    resp.json.return_value = {"error": {"message": "Data not found"}}
                else:
                    resp.status_code = 200
                    resp.json.return_value = mock_itunes_lookup
                return resp

            mock_get.side_effect = side_effect
            album = client.get_album_details("500500")

        assert album is not None
        assert album["id"] == "itunes:album:500500"
        assert album["title"] == "Fallback Album"
        assert len(album["tracks"]) == 1

    def test_get_album_details_caching(self):
        client = DiscoveryClient(ttl_seconds=60)
        mock_album = {
            "id": 999,
            "title": "Cached Album",
            "artist": {"name": "Cached Artist"},
            "tracks": {"data": []},
        }

        with patch.object(client.session, "get") as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = mock_album
            mock_get.return_value = resp

            # 1st call
            res1 = client.get_album_details("deezer:album:999")
            assert mock_get.call_count == 1
            assert res1["title"] == "Cached Album"

            # Mutate res1 to ensure cache returns deepcopy
            res1["title"] = "Mutated"

            # 2nd call hits cache
            res2 = client.get_album_details("deezer:album:999")
            assert mock_get.call_count == 1
            assert res2["title"] == "Cached Album"

    def test_get_artist_details_deezer_grouping(self):
        client = DiscoveryClient(ttl_seconds=60)
        mock_artist = {
            "id": 27,
            "name": "Daft Punk",
            "picture_xl": "https://api.deezer.com/artist/27-xl.jpg",
            "nb_album": 4,
            "nb_fan": 4000000,
        }
        mock_albums = {
            "data": [
                {
                    "id": 301,
                    "title": "Discovery",
                    "record_type": "album",
                    "release_date": "2001-03-07",
                    "cover_big": "http://img/discovery.jpg",
                },
                {
                    "id": 302,
                    "title": "Random Access Memories",
                    "record_type": "album",
                    "release_date": "2013-05-17",
                    "cover_big": "http://img/ram.jpg",
                },
                {
                    "id": 303,
                    "title": "One More Time",
                    "record_type": "single",
                    "release_date": "2000-11-13",
                    "cover_big": "http://img/omt.jpg",
                },
                {
                    "id": 304,
                    "title": "Musique Vol. 1",
                    "record_type": "compile",
                    "release_date": "2006-03-29",
                    "cover_big": "http://img/musique.jpg",
                },
            ]
        }

        with patch.object(client.session, "get") as mock_get:
            def side_effect(url, **kwargs):
                resp = MagicMock()
                resp.status_code = 200
                if "albums" in url:
                    resp.json.return_value = mock_albums
                else:
                    resp.json.return_value = mock_artist
                return resp

            mock_get.side_effect = side_effect
            artist = client.get_artist_details("deezer:artist:27")

        assert artist is not None
        assert artist["id"] == "deezer:artist:27"
        assert artist["name"] == "Daft Punk"
        assert artist["image_url"] == "https://api.deezer.com/artist/27-xl.jpg"
        assert artist["nb_fan"] == 4000000
        assert len(artist["albums"]) == 2
        assert len(artist["singles_eps"]) == 1
        assert len(artist["compilations"]) == 1

        assert artist["albums"][0]["title"] == "Discovery"
        assert artist["singles_eps"][0]["title"] == "One More Time"
        assert artist["compilations"][0]["title"] == "Musique Vol. 1"

    def test_get_artist_details_itunes_grouping(self):
        client = DiscoveryClient(ttl_seconds=60)
        mock_itunes_artist = {
            "resultCount": 4,
            "results": [
                {
                    "wrapperType": "artist",
                    "artistId": 909253,
                    "artistName": "Jack Johnson",
                },
                {
                    "wrapperType": "collection",
                    "collectionId": 1001,
                    "collectionName": "In Between Dreams",
                    "artistName": "Jack Johnson",
                    "collectionType": "Album",
                    "trackCount": 14,
                    "artworkUrl100": "http://itunes/100x100bb.jpg",
                },
                {
                    "wrapperType": "collection",
                    "collectionId": 1002,
                    "collectionName": "Upside Down - Single",
                    "artistName": "Jack Johnson",
                    "collectionType": "Album",
                    "trackCount": 2,
                    "artworkUrl100": "http://itunes/100x100bb.jpg",
                },
                {
                    "wrapperType": "collection",
                    "collectionId": 1003,
                    "collectionName": "The Best of Jack Johnson",
                    "artistName": "Jack Johnson",
                    "collectionType": "Compilation",
                    "trackCount": 18,
                    "artworkUrl100": "http://itunes/100x100bb.jpg",
                },
            ],
        }

        with patch.object(client.session, "get") as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = mock_itunes_artist
            mock_get.return_value = resp

            artist = client.get_artist_details("itunes:artist:909253")

        assert artist is not None
        assert artist["id"] == "itunes:artist:909253"
        assert artist["name"] == "Jack Johnson"
        assert len(artist["albums"]) == 1
        assert len(artist["singles_eps"]) == 1
        assert len(artist["compilations"]) == 1

    def test_get_artist_details_caching(self):
        client = DiscoveryClient(ttl_seconds=60)
        mock_artist = {"id": 11, "name": "Artist 11"}

        with patch.object(client.session, "get") as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = mock_artist
            mock_get.return_value = resp

            # 1st call
            res1 = client.get_artist_details("deezer:artist:11")
            assert mock_get.call_count > 0
            call_count_1 = mock_get.call_count

            # 2nd call hits cache
            res2 = client.get_artist_details("deezer:artist:11")
            assert mock_get.call_count == call_count_1
            assert res2["name"] == "Artist 11"

    def test_empty_or_invalid_id_returns_none(self):
        client = DiscoveryClient()
        assert client.get_album_details("") is None
        assert client.get_album_details("   ") is None
        assert client.get_artist_details("") is None
        assert client.get_artist_details("   ") is None


# =============================================================================
# 2. REST API: Album & Artist Endpoints
# =============================================================================


class TestDiscoveryDepthAPI:
    def test_unauthenticated_requests_rejected(self, app_and_client):
        _, client = app_and_client
        assert client.get("/api/discovery/album/deezer:album:123").status_code == 401
        assert client.get("/api/discovery/artist/deezer:artist:123").status_code == 401

    def test_album_endpoint_200_with_annotated_tracklist(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        app, client = app_and_client
        alice = seeded_users["alice"]
        headers = _auth_headers(alice, test_db, test_config)

        # Pre-seed one request in DB for track 2
        test_db.create_request(
            MusicRequest(
                id="req-track-2",
                user_id=alice["id"],
                item_type="track",
                title="Aerodynamic",
                artist="Daft Punk",
                foreign_id="deezer:track:2",
                status=RequestStatus.AVAILABLE,
            )
        )

        mock_discovery = MagicMock(spec=DiscoveryClient)
        mock_discovery.get_album_details.return_value = {
            "id": "deezer:album:302127",
            "title": "Discovery",
            "artist": "Daft Punk",
            "cover_url": "http://img/cover.jpg",
            "release_date": "2001-03-07",
            "tracks": [
                {
                    "id": "deezer:track:1",
                    "title": "One More Time",
                    "artist": "Daft Punk",
                    "track_number": 1,
                    "duration_seconds": 320,
                    "preview_url": "http://preview1.mp3",
                },
                {
                    "id": "deezer:track:2",
                    "title": "Aerodynamic",
                    "artist": "Daft Punk",
                    "track_number": 2,
                    "duration_seconds": 212,
                    "preview_url": "http://preview2.mp3",
                },
            ],
        }
        app.dependency_overrides[get_discovery_client] = lambda: mock_discovery

        resp = client.get("/api/discovery/album/deezer:album:302127", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "deezer:album:302127"
        assert data["title"] == "Discovery"
        assert data["status"] == "none"  # album itself not requested

        tracks = data["tracks"]
        assert len(tracks) == 2
        assert tracks[0]["title"] == "One More Time"
        assert tracks[0]["status"] == "none"

        assert tracks[1]["title"] == "Aerodynamic"
        assert tracks[1]["status"] == "available"
        assert tracks[1]["request_id"] == "req-track-2"

    def test_album_endpoint_404_when_missing(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        app, client = app_and_client
        headers = _auth_headers(seeded_users["alice"], test_db, test_config)

        mock_discovery = MagicMock(spec=DiscoveryClient)
        mock_discovery.get_album_details.return_value = None
        app.dependency_overrides[get_discovery_client] = lambda: mock_discovery

        resp = client.get("/api/discovery/album/deezer:album:999999", headers=headers)
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_album_endpoint_with_plex_library_match(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        app, client = app_and_client
        headers = _auth_headers(seeded_users["alice"], test_db, test_config)

        mock_plex = MagicMock()
        mock_plex.search_library_tracks.return_value = [
            {"title": "One More Time", "artist": "Daft Punk"}
        ]
        app.dependency_overrides[get_plex_client] = lambda: mock_plex

        mock_discovery = MagicMock(spec=DiscoveryClient)
        mock_discovery.get_album_details.return_value = {
            "id": "deezer:album:302127",
            "title": "Discovery",
            "artist": "Daft Punk",
            "tracks": [
                {
                    "id": "deezer:track:1",
                    "title": "One More Time",
                    "artist": "Daft Punk",
                    "track_number": 1,
                    "duration_seconds": 320,
                }
            ],
        }
        app.dependency_overrides[get_discovery_client] = lambda: mock_discovery

        resp = client.get("/api/discovery/album/deezer:album:302127", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["tracks"][0]["status"] == "in_library"

    def test_artist_endpoint_200_with_annotated_discography(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        app, client = app_and_client
        alice = seeded_users["alice"]
        headers = _auth_headers(alice, test_db, test_config)

        # Pre-seed one album request in DB
        test_db.create_request(
            MusicRequest(
                id="req-discovery",
                user_id=alice["id"],
                item_type="album",
                title="Discovery",
                artist="Daft Punk",
                foreign_id="deezer:album:301",
                status=RequestStatus.PENDING,
            )
        )

        mock_discovery = MagicMock(spec=DiscoveryClient)
        mock_discovery.get_artist_details.return_value = {
            "id": "deezer:artist:27",
            "name": "Daft Punk",
            "image_url": "http://img/daft.jpg",
            "nb_album": 3,
            "nb_fan": 50000,
            "albums": [
                {"id": "deezer:album:301", "title": "Discovery", "artist": "Daft Punk"},
                {"id": "deezer:album:302", "title": "Homework", "artist": "Daft Punk"},
            ],
            "singles_eps": [
                {"id": "deezer:album:401", "title": "Da Funk", "artist": "Daft Punk"},
            ],
            "compilations": [],
        }
        app.dependency_overrides[get_discovery_client] = lambda: mock_discovery

        resp = client.get("/api/discovery/artist/deezer:artist:27", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Daft Punk"
        assert len(data["albums"]) == 2
        assert data["albums"][0]["title"] == "Discovery"
        assert data["albums"][0]["status"] == "requested"
        assert data["albums"][1]["status"] == "none"
        assert len(data["singles_eps"]) == 1
        assert data["singles_eps"][0]["status"] == "none"

    def test_artist_endpoint_404_when_missing(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        app, client = app_and_client
        headers = _auth_headers(seeded_users["alice"], test_db, test_config)

        mock_discovery = MagicMock(spec=DiscoveryClient)
        mock_discovery.get_artist_details.return_value = None
        app.dependency_overrides[get_discovery_client] = lambda: mock_discovery

        resp = client.get("/api/discovery/artist/deezer:artist:888888", headers=headers)
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


# =============================================================================
# 3. Batch Requests REST API Tests
# =============================================================================


class TestBatchRequestsAPI:
    def test_batch_create_unauthenticated_fails(self, app_and_client):
        _, client = app_and_client
        payload = {
            "requests": [
                {"item_type": "track", "title": "Song 1", "artist": "Artist 1"},
            ]
        }
        resp = client.post("/api/requests/batch", json=payload)
        assert resp.status_code == 401

    def test_batch_create_success_regular_user(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        alice = seeded_users["alice"]
        headers = _auth_headers(alice, test_db, test_config)

        payload = {
            "requests": [
                {
                    "item_type": "track",
                    "title": "Track One",
                    "artist": "Artist A",
                    "album": "Album A",
                    "foreign_id": "deezer:track:111",
                },
                {
                    "item_type": "track",
                    "title": "Track Two",
                    "artist": "Artist A",
                    "album": "Album A",
                    "foreign_id": "deezer:track:222",
                },
            ]
        }
        resp = client.post("/api/requests/batch", json=payload, headers=headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["count"] == 2
        assert len(data["created"]) == 2

        titles = [r["title"] for r in data["created"]]
        assert "Track One" in titles
        assert "Track Two" in titles
        # Non-admin with auto_approve_requests=False -> pending
        for r in data["created"]:
            assert r["status"] == "pending"
            assert r["user_id"] == alice["id"]

    def test_batch_create_quota_enforcement(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        test_db.update_account_settings({"default_quota_tracks": 1})  # per-type quota (was one shared cap of 2)
        alice = seeded_users["alice"]
        headers = _auth_headers(alice, test_db, test_config)

        # Seed 1 active request for Alice
        test_db.create_request(
            MusicRequest(
                id="req-existing",
                user_id=alice["id"],
                item_type="album",
                title="Existing Album",
                artist="Existing Artist",
                status=RequestStatus.PENDING,
            )
        )

        # Batch with 2 items exceeds remaining quota (1)
        payload = {
            "requests": [
                {"item_type": "track", "title": "Track 1", "artist": "Artist 1"},
                {"item_type": "track", "title": "Track 2", "artist": "Artist 2"},
            ]
        }
        resp = client.post("/api/requests/batch", json=payload, headers=headers)
        assert resp.status_code == 400
        assert "quota reached" in resp.json()["detail"].lower()

    def test_batch_create_admin_bypasses_quota_and_auto_approves(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        test_db.update_account_settings({"default_quota_tracks": 1})
        admin = seeded_users["admin"]
        headers = _auth_headers(admin, test_db, test_config)

        # Admin requests 3 items despite quota=1
        payload = {
            "requests": [
                {"item_type": "track", "title": "Track A", "artist": "Band 1"},
                {"item_type": "track", "title": "Track B", "artist": "Band 1"},
                {"item_type": "track", "title": "Track C", "artist": "Band 1"},
            ]
        }
        resp = client.post("/api/requests/batch", json=payload, headers=headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["count"] == 3
        for r in data["created"]:
            assert r["status"] == "processing"

    def test_batch_create_idempotent_deduplication(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        test_db.update_account_settings({"default_quota_tracks": 5, "default_quota_albums": 5})
        alice = seeded_users["alice"]
        headers = _auth_headers(alice, test_db, test_config)

        # Pre-seed active request for Song A
        test_db.create_request(
            MusicRequest(
                id="req-song-a",
                user_id=alice["id"],
                item_type="track",
                title="Song A",
                artist="Artist Alpha",
                status=RequestStatus.PROCESSING,
            )
        )

        # Batch contains:
        # 1. Song A (duplicate of existing active request)
        # 2. Song B (unique)
        # 3. Song B (duplicate within the same batch)
        # 4. Song C (unique)
        payload = {
            "requests": [
                {"item_type": "track", "title": "Song A", "artist": "Artist Alpha"},
                {"item_type": "track", "title": "Song B", "artist": "Artist Beta"},
                {"item_type": "track", "title": "Song B", "artist": "Artist Beta"},
                {"item_type": "track", "title": "Song C", "artist": "Artist Gamma"},
            ]
        }
        resp = client.post("/api/requests/batch", json=payload, headers=headers)
        assert resp.status_code == 201
        data = resp.json()
        # Should only create Song B and Song C
        assert data["count"] == 2
        titles = [r["title"] for r in data["created"]]
        assert "Song B" in titles
        assert "Song C" in titles
        assert "Song A" not in titles

    def test_batch_create_native_grab_without_lidarr_fallback(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        app, client = app_and_client
        admin = seeded_users["admin"]
        headers = _auth_headers(admin, test_db, test_config)

        # Configure mock Lidarr client
        mock_lidarr = MagicMock(spec=LidarrClient)
        app.dependency_overrides[get_lidarr_client] = lambda: mock_lidarr

        # Configure test download client & indexer to enable native search
        test_db.create_download_client(
            DownloadClientConfig(
                id="client-qbit",
                name="qBittorrent",
                driver_type="qbittorrent",
                host_url="http://127.0.0.1:8080",
                enabled=True,
            )
        )
        test_db.create_indexer(
            IndexerConfig(
                id="indexer-torznab",
                name="Torznab Indexer",
                indexer_type="torznab",
                host_url="http://127.0.0.1:9696/api",
                enabled=True,
            )
        )

        # Mock acquisition coordinator to succeed on Track 1 and fail on Track 2
        with patch(
            "plex_playlist_sync.api.routes.requests.acquisition_coordinator.search_and_grab"
        ) as mock_grab, patch(
            "plex_playlist_sync.lidarr_queue.lidarr_worker.start_trickle"
        ) as mock_trickle:
            def grab_side_effect(**kwargs):
                if kwargs.get("title") == "Track 1":
                    return {"success": True, "download_id": "dl-101"}
                return {"success": False, "message": "No match found"}

            mock_grab.side_effect = grab_side_effect

            payload = {
                "requests": [
                    {"item_type": "track", "title": "Track 1", "artist": "Artist"},
                    {"item_type": "track", "title": "Track 2", "artist": "Artist"},
                ]
            }
            resp = client.post("/api/requests/batch", json=payload, headers=headers)
            assert resp.status_code == 201
            assert resp.json()["count"] == 2

            # Native search attempted for both
            assert mock_grab.call_count == 2

            # Native mode is strict: an unmatched track is never sent to Lidarr
            assert mock_trickle.call_count == 0

    def test_batch_create_empty_requests_list_validation_error(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["alice"], test_db, test_config)
        resp = client.post("/api/requests/batch", json={"requests": []}, headers=headers)
        assert resp.status_code == 422

    def test_batch_create_invalid_item_type_rejected(
        self, app_and_client, test_db, test_config, seeded_users
    ):
        _, client = app_and_client
        headers = _auth_headers(seeded_users["alice"], test_db, test_config)
        payload = {
            "requests": [
                {"item_type": "invalid_type", "title": "Song", "artist": "Band"}
            ]
        }
        resp = client.post("/api/requests/batch", json=payload, headers=headers)
        assert resp.status_code == 422


def test_concurrent_batches_cannot_exceed_quota(app_and_client, test_db, test_config, seeded_users):
    """Count+insert must be atomic per user: two simultaneous 2-item batches against quota 3 cannot both land."""
    import threading
    import time

    _, client = app_and_client
    test_db.update_account_settings({"default_quota_tracks": 3})
    headers = _auth_headers(seeded_users["alice"], test_db, test_config)
    real_create = test_db.create_request

    def slow_create(req):
        time.sleep(0.05)  # widen the check-then-insert race window
        return real_create(req)

    codes = []

    def post(tag):
        payload = {"requests": [
            {"item_type": "track", "title": f"{tag}-1", "artist": f"Art{tag}"},
            {"item_type": "track", "title": f"{tag}-2", "artist": f"Art{tag}"},
        ]}
        codes.append(client.post("/api/requests/batch", json=payload, headers=headers).status_code)

    with patch.object(test_db, "create_request", side_effect=slow_create):
        threads = [threading.Thread(target=post, args=(t,)) for t in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert sorted(codes) == [201, 400]
    assert len(test_db.list_requests(user_id=seeded_users["alice"]["id"])) == 2

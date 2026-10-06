"""Track-level discovery: album_discovery_id on parsers, get_track_details, GET /api/discovery/track/{id}."""

from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db, get_discovery_client
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.clients.discovery import DiscoveryClient
from plex_playlist_sync.config import Config
from plex_playlist_sync.storage import Database

DEEZER_TRACK = {
    "id": 3135556,
    "title": "Harder, Better, Faster, Stronger",
    "duration": 224,
    "track_position": 4,
    "disk_number": 1,
    "release_date": "2001-03-07",
    "isrc": "GBDUW0000059",
    "explicit_lyrics": False,
    "bpm": 123.4,
    "gain": -12.5,
    "preview": "https://cdn/prev.mp3",
    "contributors": [{"name": "Daft Punk", "role": "Main"}, {"name": "Guest", "role": "Featured"}],
    "artist": {"id": 27, "name": "Daft Punk"},
    "album": {"id": 302127, "title": "Discovery", "cover_xl": "https://cdn/xl.jpg"},
}
DEEZER_ALBUM = {
    "id": 302127,
    "title": "Discovery",
    "artist": {"id": 27, "name": "Daft Punk"},
    "label": "Parlophone",
    "genres": {"data": [{"name": "Electro"}]},
    "tracks": {"data": []},
}


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def users(test_db):
    return {
        "admin": test_db.upsert_user("admin-1", "admin_user", "a@x.tv", is_admin=True),
        "alice": test_db.upsert_user("user-alice", "alice", "al@x.tv", is_admin=False),
    }


@pytest.fixture
def app_client(test_db, test_config):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    return app, TestClient(app)


def _headers(user, db, config):
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


class TestParsersCarryAlbumId:
    def test_trending_deezer_track(self):
        client = DiscoveryClient(ttl_seconds=60)
        track = {"id": 1, "title": "T", "artist": {"id": 5, "name": "A"}, "album": {"id": 77, "title": "Alb"}}

        def get(url, **kw):
            return _resp({"data": [track]} if "tracks" in url else {"data": []})

        with patch.object(client.session, "get", side_effect=get):
            items = client.get_trending(limit=10)
        t = next(i for i in items if i["item_type"] == "track")
        assert t["album_discovery_id"] == "deezer:album:77"

    def test_trending_track_without_album_omits_field(self):
        client = DiscoveryClient(ttl_seconds=60)
        track = {"id": 1, "title": "T", "artist": {"name": "A"}, "album": {"title": "Alb"}}

        def get(url, **kw):
            return _resp({"data": [track]} if "tracks" in url else {"data": []})

        with patch.object(client.session, "get", side_effect=get):
            items = client.get_trending(limit=10)
        assert "album_discovery_id" not in next(i for i in items if i["item_type"] == "track")

    def test_deezer_search_track(self):
        client = DiscoveryClient(ttl_seconds=60)
        track = {"id": 1, "title": "T", "artist": {"name": "A"}, "album": {"id": 9, "title": "Alb"}}
        with patch.object(client.session, "get", return_value=_resp({"data": [track]})):
            out = client._search_deezer("q", "track")
        assert out[0]["album_discovery_id"] == "deezer:album:9"

    def test_itunes_search_track_only(self):
        client = DiscoveryClient(ttl_seconds=60)
        row = {
            "trackId": 11, "trackName": "S", "artistName": "A", "collectionName": "Alb",
            "collectionId": 555, "artistId": 3,
        }
        alb_row = {"collectionId": 555, "collectionName": "Alb", "artistName": "A"}

        def get(url, **kw):
            return _resp({"results": [row if "entity=song" in url else alb_row]})

        with patch.object(client.session, "get", side_effect=get):
            out = client._search_itunes("q", "all")
        track = next(i for i in out if i["item_type"] == "track")
        album = next(i for i in out if i["item_type"] == "album")
        assert track["album_discovery_id"] == "itunes:album:555"
        assert "album_discovery_id" not in album

    def test_top_tracks_detailed_and_album_tracks(self):
        client = DiscoveryClient(ttl_seconds=60)
        with patch.object(client.session, "get", return_value=_resp({"data": [DEEZER_TRACK]})):
            top = client.get_artist_top_tracks_detailed("deezer:artist:27")
        assert top[0]["album_discovery_id"] == "deezer:album:302127"
        album = dict(DEEZER_ALBUM, tracks={"data": [DEEZER_TRACK]})
        with patch.object(client.session, "get", return_value=_resp(album)):
            detail = client.get_album_details("deezer:album:302127")
        assert detail["tracks"][0]["album_discovery_id"] == "deezer:album:302127"


class TestGetTrackDetails:
    def _deezer_get(self, url, **kw):
        if "/track/" in url:
            return _resp(DEEZER_TRACK)
        return _resp(DEEZER_ALBUM)

    def test_deezer(self):
        client = DiscoveryClient(ttl_seconds=60)
        with patch.object(client.session, "get", side_effect=self._deezer_get):
            r = client.get_track_details("deezer:track:3135556")
        assert r["id"] == "deezer:track:3135556"
        assert r["album_discovery_id"] == "deezer:album:302127"
        assert r["artist_discovery_id"] == "deezer:artist:27"
        assert r["duration"] == 224 and r["track_position"] == 4 and r["disk_number"] == 1
        assert r["isrc"] == "GBDUW0000059" and r["explicit"] is False and r["bpm"] == 123
        assert r["label"] == "Parlophone" and r["genres"] == ["Electro"]
        assert r["contributors"][1] == {"name": "Guest", "role": "Featured"}
        assert r["cover_url"] == "https://cdn/xl.jpg"

    def test_deezer_zero_bpm_omitted_and_album_failure_tolerated(self):
        client = DiscoveryClient(ttl_seconds=60)
        track = dict(DEEZER_TRACK, bpm=0)

        def get(url, **kw):
            if "/track/" in url:
                return _resp(track)
            raise requests.ConnectionError("boom")

        with patch.object(client.session, "get", side_effect=get):
            r = client.get_track_details("deezer:track:3135556")
        assert "bpm" not in r and "label" not in r

    def test_deezer_error_body_is_none(self):
        client = DiscoveryClient(ttl_seconds=60)
        with patch.object(client.session, "get", return_value=_resp({"error": {"type": "DataException"}})):
            assert client.get_track_details("deezer:track:1") is None

    def test_itunes(self):
        client = DiscoveryClient(ttl_seconds=60)
        row = {
            "wrapperType": "track", "kind": "song", "trackId": 11, "trackName": "S", "artistName": "A",
            "artistId": 3, "collectionName": "Alb", "collectionId": 555, "artworkUrl100": "http://x/100x100bb.jpg",
            "trackTimeMillis": 200500, "trackNumber": 2, "discNumber": 1, "releaseDate": "2020-01-01T00:00:00Z",
            "trackExplicitness": "explicit", "primaryGenreName": "Pop", "previewUrl": "http://p",
        }
        with patch.object(client.session, "get", return_value=_resp({"results": [row]})):
            r = client.get_track_details("itunes:track:11")
        assert r["album_discovery_id"] == "itunes:album:555"
        assert r["artist_discovery_id"] == "itunes:artist:3"
        assert r["explicit"] is True and r["duration"] == 200 and r["genres"] == ["Pop"]
        assert r["cover_url"].endswith("600x600bb.jpg")

    def test_unknown_prefix_none(self):
        assert DiscoveryClient().get_track_details("spotify:track:1") is None


class TestTrackEndpoint:
    def test_deezer_ok_and_status(self, app_client, test_db, test_config, users):
        app, client = app_client
        mock = MagicMock(spec=DiscoveryClient)
        mock.get_track_details.return_value = {
            "id": "deezer:track:1", "item_type": "track", "title": "T", "artist": "A", "album": "Alb",
            "album_discovery_id": "deezer:album:9", "duration": 100,
        }
        app.dependency_overrides[get_discovery_client] = lambda: mock
        r = client.get("/api/discovery/track/deezer:track:1", headers=_headers(users["alice"], test_db, test_config))
        assert r.status_code == 200
        body = r.json()
        assert body["album_discovery_id"] == "deezer:album:9"
        assert body["status"] == "none"
        mock.get_track_details.assert_called_once_with("deezer:track:1")

    def test_404(self, app_client, test_db, test_config, users):
        app, client = app_client
        mock = MagicMock(spec=DiscoveryClient)
        mock.get_track_details.return_value = None
        app.dependency_overrides[get_discovery_client] = lambda: mock
        r = client.get("/api/discovery/track/itunes:track:1", headers=_headers(users["alice"], test_db, test_config))
        assert r.status_code == 404

    def test_upstream_failure_502(self, app_client, test_db, test_config, users):
        from plex_playlist_sync.clients.discovery import DiscoveryUpstreamError

        app, client = app_client
        mock = MagicMock(spec=DiscoveryClient)
        mock.get_track_details.side_effect = DiscoveryUpstreamError("timeout")
        app.dependency_overrides[get_discovery_client] = lambda: mock
        r = client.get("/api/discovery/track/deezer:track:1", headers=_headers(users["alice"], test_db, test_config))
        assert r.status_code == 502

    def test_requires_auth(self, app_client):
        _, client = app_client
        assert client.get("/api/discovery/track/deezer:track:1").status_code == 401

    def test_non_admin_has_no_library_ids(self, app_client, test_db, test_config, users):
        app, client = app_client
        mock = MagicMock(spec=DiscoveryClient)
        mock.get_track_details.return_value = {
            "id": "deezer:track:1", "item_type": "track", "title": "T", "artist": "A", "album": "Alb",
        }
        app.dependency_overrides[get_discovery_client] = lambda: mock
        hint = {"a": "lib-artist-1"}
        with patch(
            "plex_playlist_sync.api.routes.discovery._library_artist_ids_by_name", return_value=hint
        ), patch("plex_playlist_sync.api.routes.discovery.normalize_artist_name", return_value="a"):
            admin = client.get("/api/discovery/track/deezer:track:1", headers=_headers(users["admin"], test_db, test_config))
            alice = client.get("/api/discovery/track/deezer:track:1", headers=_headers(users["alice"], test_db, test_config))
        assert admin.json().get("library_artist_id") == "lib-artist-1"
        assert not any(k.startswith("library") for k in alice.json())

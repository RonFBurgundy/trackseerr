"""Phase 4 backend: the Library served live from Lidarr (library_mode == lidarr), through the existing routes."""

import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import lidarr_library
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.library_manager import ModeChanged
from plex_playlist_sync.list_index import NULL_LABEL, group_label_for_name
from plex_playlist_sync.storage import Database

API_KEY = "lidarr-secret-key-abcdef123456"
NATIVE_ONLY = "Not available while Lidarr manages the library"

ARTIST_NAMES = [
    "The Beatles", "Beatles Tribute", "A Tribe Called Quest", "Éric Clapton", "Eric Church", "Ørsted", "123 Band",
    "!!!", "Zebra", "Muse", "Metallica", "Anna", "Cher", "Radiohead", "Жук", "Blur",
]


# --------------------------------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _fresh_cache():
    lidarr_library.invalidate()
    yield
    lidarr_library.invalidate()


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
        "admin": test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True),
        "alice": test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False),
    }


@pytest.fixture
def api(test_db, test_config):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    return TestClient(app)


def _headers(user, db, config):
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_h(test_db, test_config, users):
    return _headers(users["admin"], test_db, test_config)


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.content = b"x" if payload is not None else b""
    r.text = ""
    return r


def _artist(i, name, monitored=True):
    return {
        "id": i, "artistName": name, "foreignArtistId": f"mbid-{i}", "path": f"/music/{name}",
        "monitored": monitored, "added": f"202{i % 6}-0{1 + i % 9}-1{i % 10}T10:00:00Z", "overview": "bio",
        "genres": ["rock"], "status": "continuing",
        "images": [
            {"coverType": "poster", "url": f"/MediaCover/{i}/poster.jpg?lastWrite=1"},
            {"coverType": "banner", "url": f"/MediaCover/{i}/banner.jpg?lastWrite=1"},
            {"coverType": "fanart", "url": f"/MediaCover/{i}/fanart.jpg"},
        ],
        "statistics": {"albumCount": i % 4, "trackFileCount": i, "totalTrackCount": i * 3, "sizeOnDisk": i * 1000},
    }


def _album(i, artist, title, monitored=True, release="2001-05-01T00:00:00Z"):
    return {
        "id": 100 + i, "title": title, "artistId": artist["id"], "artist": {"id": artist["id"], "artistName": artist["artistName"]},
        "releaseDate": release, "added": f"2023-0{1 + i % 9}-0{1 + i % 9}T08:00:00Z", "monitored": monitored,
        "albumType": "Album", "foreignAlbumId": f"alb-{i}",
        "images": [{"coverType": "cover", "url": f"/MediaCover/Albums/{100 + i}/cover.jpg"}],
        "statistics": {"trackFileCount": 1, "trackCount": 10, "totalTrackCount": 10, "sizeOnDisk": 5000},
    }


ARTISTS = [_artist(i + 1, n, monitored=(i % 3 != 0)) for i, n in enumerate(ARTIST_NAMES)]
TITLES = ["The Wall", "A Night", "An Album", "Éclat", "OK Computer", "9", "#1 Hits", "Zen", "Abbey Road", "Kid A"]
RELEASES = ["1995-03-01T00:00:00Z", "2003-06-01T00:00:00Z", None, "2010-01-01T00:00:00Z", "2001-12-31T00:00:00Z"]
ALBUMS = [
    _album(i, ARTISTS[i % len(ARTISTS)], TITLES[i % len(TITLES)], monitored=(i % 4 != 0), release=RELEASES[i % 5])
    for i in range(24)
]
TRACKS = [
    {"id": 900 + n, "title": t, "albumId": 100, "artistId": 1, "trackNumber": str(n + 1), "mediumNumber": 1,
     "duration": 180000, "hasFile": n != 1,
     **({"trackFile": {"id": 5, "path": "/m/a.flac", "size": 1234, "quality": {"quality": {"name": "FLAC"}}}} if n == 0 else {})}
    for n, t in enumerate(["Intro", "The Song", "Ünder"])
]


class Lidarr:
    """A tiny fake Lidarr behind a patched ``httpx.Client`` (record of calls in ``calls``)."""

    def __init__(self, test_db):
        test_db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": API_KEY})
        test_db.update_media_management_settings({"library_mode": "lidarr"})
        self.patcher = patch("plex_playlist_sync.clients.lidarr.httpx.Client")
        self.http = self.patcher.start().return_value.__enter__.return_value
        self.calls: list[tuple[str, str]] = []
        self.artists = [dict(a) for a in ARTISTS]
        self.albums = [dict(a) for a in ALBUMS]
        self.tracks = TRACKS
        self.delay = 0.0
        self.fail: object = None
        self.http.get.side_effect = self._get
        self.http.put.side_effect = lambda url, **kw: self._write("PUT", url, kw)
        self.http.post.side_effect = lambda url, **kw: self._write("POST", url, kw)
        self.puts: list[tuple[str, object]] = []

    def stop(self):
        self.patcher.stop()

    def count(self, suffix):
        return sum(1 for m, u in self.calls if m == "GET" and u.endswith(suffix))

    def _get(self, url, **kw):
        self.calls.append(("GET", url))
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        path = url.split("/api/v1/", 1)[1]
        if path == "artist":
            return _resp(payload=self.artists)
        if path.startswith("album?includeAllArtistAlbums"):
            return _resp(payload=self.albums)
        if path.startswith("artist/"):
            found = [a for a in self.artists if str(a["id"]) == path.split("/")[1]]
            return _resp(payload=found[0]) if found else _resp(404)
        if path.startswith("album?artistId="):
            aid = int(path.split("=")[1])
            return _resp(payload=[a for a in self.albums if a["artistId"] == aid])
        if path.startswith("album/"):
            found = [a for a in self.albums if str(a["id"]) == path.split("/")[1]]
            return _resp(payload=found[0]) if found else _resp(404)
        if path.startswith("track?albumId="):
            return _resp(payload=self.tracks)
        raise AssertionError(f"unexpected GET {url}")

    def _write(self, method, url, kw):
        self.calls.append((method, url))
        self.puts.append((url, kw.get("json")))
        body = kw.get("json")
        return _resp(payload=body if url.split("/api/v1/")[1].startswith("artist/") else {"id": 1})

    def stream_image(self, status=200, content_type="image/jpeg", body=b"\xff\xd8img", length=None):
        resp = MagicMock()
        resp.status_code = status
        resp.headers = {"content-type": content_type, **({"content-length": str(length)} if length is not None else {})}
        resp.iter_bytes.return_value = iter([body[: len(body) // 2], body[len(body) // 2:]])
        self.http.stream.return_value.__enter__.return_value = resp
        return resp


@pytest.fixture
def lidarr(test_db):
    fake = Lidarr(test_db)
    yield fake
    fake.stop()


# ------------------------------------------------------------------------------------------------ paged + index


def _all_records(api, h, kind, extra=""):
    out, page = [], 1
    while True:
        res = api.get(f"/api/library/{kind}/paged?page={page}&page_size=7{extra}", headers=h)
        assert res.status_code == 200, res.text
        body = res.json()
        out += body["records"]
        if len(out) >= body["total"]:
            return body, out
        page += 1


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _date_label_ok(label, date):
    if label == NULL_LABEL:
        return not date
    if not date:
        return False
    if label.isdigit():
        return date[:4] == label
    return label == f"{_MONTHS[int(date[5:7]) - 1]} {date[:4]}"


def _check_offsets(api, h, kind, sort_key, sort_dir, extra=""):
    qs = f"sort_key={sort_key}&sort_dir={sort_dir}{extra}"
    body, records = _all_records(api, h, kind, "&" + qs)
    idx = api.get(f"/api/library/{kind}/index?{qs}", headers=h).json()
    assert idx["total"] == body["total"] == len(records) and idx["sort_key"] == sort_key
    assert body["mode"] == idx["mode"] == "lidarr" and body["sort_dir"] == sort_dir
    assert sum(g["count"] for g in idx["groups"]) == len(records)
    running = 0
    labelled = {"name": "name", "title": "title", "artist": "artist_name"}.get(sort_key)
    for g in idx["groups"]:
        assert g["offset"] == running
        running += g["count"]
        if labelled:
            field = labelled if sort_key != "artist" else "artist_name"
            assert group_label_for_name(records[g["offset"]][field]) == g["label"]
            if g["offset"]:
                assert group_label_for_name(records[g["offset"] - 1][field]) != g["label"]
        elif sort_key in ("release_date", "added_at"):
            assert _date_label_ok(g["label"], records[g["offset"]][sort_key])
            if g["offset"]:
                assert not _date_label_ok(g["label"], records[g["offset"] - 1][sort_key])
    return records


class TestPagedAndIndex:
    @pytest.mark.parametrize("sort_key", ["name", "added_at", "album_count"])
    @pytest.mark.parametrize("sort_dir", ["asc", "desc"])
    def test_artists_offsets(self, api, admin_h, lidarr, sort_key, sort_dir):
        _check_offsets(api, admin_h, "artists", sort_key, sort_dir)

    @pytest.mark.parametrize("sort_key", ["title", "artist", "release_date", "added_at"])
    @pytest.mark.parametrize("sort_dir", ["asc", "desc"])
    def test_albums_offsets(self, api, admin_h, lidarr, sort_key, sort_dir):
        _check_offsets(api, admin_h, "albums", sort_key, sort_dir)

    def test_name_sort_order_and_record_shape(self, api, admin_h, lidarr):
        body, records = _all_records(api, admin_h, "artists", "&sort_key=name")
        names = [r["name"] for r in records]
        assert set(names[:3]) == {"!!!", "123 Band", "Жук"}  # digits, symbols and non-Latin share the "#" block
        assert names.index("Anna") < names.index("The Beatles") < names.index("Beatles Tribute") < names.index("A Tribe Called Quest")
        rec = next(r for r in records if r["name"] == "The Beatles")
        assert rec["id"] == "1" and rec["monitored"] is False and rec["album_count"] == 1
        assert rec["track_count"] == 3 and rec["image_url"] == "/api/library/artists/1/image"
        assert rec["banner_url"] == "/api/library/artists/1/banner" and rec["mbid"] == "mbid-1"
        assert API_KEY not in str(body)

    def test_album_record_shape(self, api, admin_h, lidarr):
        res = api.get("/api/library/albums/paged?sort_key=title&page_size=200", headers=admin_h).json()
        rec = next(r for r in res["records"] if r["id"] == "101")
        assert rec["artist_id"] == "2" and rec["artist_name"] == "Beatles Tribute" and rec["title"] == "A Night"
        assert rec["cover_url"] == "/api/library/albums/101/cover" and rec["track_count"] == 10
        assert rec["release_date"] == "2003-06-01T00:00:00Z" and rec["year"] == 2003 and rec["monitored"] is True
        assert res["total"] == 24

    def test_search_monitored_and_artist_filters(self, api, admin_h, lidarr):
        res = api.get("/api/library/artists/paged?q=beatles&sort_key=name", headers=admin_h).json()
        assert [r["name"] for r in res["records"]] == ["The Beatles", "Beatles Tribute"] and res["total"] == 2
        # case- and accent-insensitive, same as native mode: "ERIC" finds both "Éric Clapton" and "Eric Church"
        assert api.get("/api/library/artists/paged?q=ERIC&sort_key=name", headers=admin_h).json()["total"] == 2
        mon = api.get("/api/library/artists/paged?monitored_only=true&page_size=200", headers=admin_h).json()
        assert mon["total"] == sum(1 for a in ARTISTS if a["monitored"])
        assert all(r["monitored"] for r in mon["records"])
        alb = api.get("/api/library/albums/paged?artist_id=1&page_size=200", headers=admin_h).json()
        assert alb["total"] == sum(1 for a in ALBUMS if a["artistId"] == 1) and alb["total"] > 0
        assert {r["artist_id"] for r in alb["records"]} == {"1"}
        # q matches the album title and the artist name, and the index honours the same filters
        assert api.get("/api/library/albums/paged?q=wall", headers=admin_h).json()["total"] >= 1
        idx = api.get("/api/library/albums/index?artist_id=1&monitored_only=true", headers=admin_h).json()
        assert idx["total"] == sum(1 for a in ALBUMS if a["artistId"] == 1 and a["monitored"])
        _check_offsets(api, admin_h, "artists", "name", "asc", "&q=e&monitored_only=true")

    def test_search_folds_accents_both_ways(self, api, admin_h, lidarr):
        def names(path, q):
            return [r["name"] for r in api.get(f"{path}?q={q}&page_size=200", headers=admin_h).json()["records"]]

        assert sorted(names("/api/library/artists/paged", "%C3%89RIC")) == ["Eric Church", "Éric Clapton"]
        assert sorted(names("/api/library/artists/paged", "eric")) == ["Eric Church", "Éric Clapton"]
        assert names("/api/library/artists/paged", "%C3%B8rsted") == ["Ørsted"]
        assert names("/api/library/artists/paged", "orsted") == ["Ørsted"]
        albums = api.get("/api/library/albums/paged?q=eclat&page_size=200", headers=admin_h).json()
        assert albums["total"] > 0 and {r["title"] for r in albums["records"]} == {"Éclat"}
        # the index sees the same filter
        idx = api.get("/api/library/artists/index?q=%C3%89RIC", headers=admin_h).json()
        assert idx["total"] == 2

    def test_validation(self, api, admin_h, lidarr):
        assert api.get("/api/library/artists/paged?sort_key=nope", headers=admin_h).status_code == 422
        assert api.get("/api/library/artists/paged?page_size=999", headers=admin_h).status_code == 422
        assert api.get("/api/library/albums/index?sort_dir=sideways", headers=admin_h).status_code == 422

    def test_native_mode_never_contacts_lidarr(self, api, admin_h, test_db):
        patcher = patch("plex_playlist_sync.clients.lidarr.httpx.Client")
        cls = patcher.start()
        try:
            test_db.update_lidarr_settings({"url": "http://lidarr.test:8686", "api_key": API_KEY})
            for path in (
                "/api/library/artists/paged", "/api/library/artists/index", "/api/library/albums/paged",
                "/api/library/albums/index", "/api/library/tracks/paged", "/api/library/tracks/index",
                "/api/library/artists/1/image", "/api/library/albums/1/cover", "/api/library/artists/1",
                "/api/library/albums/1",
            ):
                res = api.get(path, headers=admin_h)
                assert res.status_code in (200, 307, 404), (path, res.status_code)
            assert api.get("/api/library/artists/paged", headers=admin_h).json()["mode"] == "native"
            cls.assert_not_called()
        finally:
            patcher.stop()


# ------------------------------------------------------------------------------------------- cache behaviour


class TestCache:
    def test_ttl_and_reuse(self, api, admin_h, lidarr):
        now = [1000.0]
        with patch.object(lidarr_library, "_clock", lambda: now[0]):
            api.get("/api/library/artists/paged", headers=admin_h)
            api.get("/api/library/artists/index", headers=admin_h)
            assert lidarr.count("/api/v1/artist") == 1
            now[0] += 59
            api.get("/api/library/artists/paged?q=x", headers=admin_h)
            assert lidarr.count("/api/v1/artist") == 1
            now[0] += 2
            api.get("/api/library/artists/paged", headers=admin_h)
            assert lidarr.count("/api/v1/artist") == 2

    def test_kinds_cached_separately(self, api, admin_h, lidarr):
        api.get("/api/library/artists/paged", headers=admin_h)
        api.get("/api/library/albums/paged", headers=admin_h)
        api.get("/api/library/albums/paged?q=a", headers=admin_h)
        assert lidarr.count("/api/v1/artist") == 1
        assert lidarr.count("/api/v1/album?includeAllArtistAlbums=true") == 1

    def test_single_flight_under_concurrency(self, api, admin_h, lidarr):
        lidarr.delay = 0.2
        results = []

        def hit():
            results.append(api.get("/api/library/artists/paged", headers=admin_h).status_code)

        threads = [threading.Thread(target=hit) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == [200] * 6
        assert lidarr.count("/api/v1/artist") == 1

    def test_invalidated_after_mutation(self, api, admin_h, lidarr):
        api.get("/api/library/artists/paged", headers=admin_h)
        assert lidarr.count("/api/v1/artist") == 1
        assert api.put("/api/library/artists/2/monitored", json={"monitored": False}, headers=admin_h).status_code == 200
        api.get("/api/library/artists/paged", headers=admin_h)
        assert lidarr.count("/api/v1/artist") == 2
        api.get("/api/library/albums/paged", headers=admin_h)
        assert api.put("/api/library/albums/101/monitored", json={"monitored": False}, headers=admin_h).status_code == 200
        api.get("/api/library/albums/paged", headers=admin_h)
        assert lidarr.count("/api/v1/album?includeAllArtistAlbums=true") == 2

    def test_failed_fetch_is_not_cached(self, api, admin_h, lidarr):
        lidarr.fail = httpx.ConnectError("boom")
        assert api.get("/api/library/artists/paged", headers=admin_h).status_code == 502
        lidarr.fail = None
        assert api.get("/api/library/artists/paged", headers=admin_h).status_code == 200

    def test_invalidate_during_fetch_does_not_store_stale(self, lidarr):
        client = MagicMock(base_url="http://lidarr.test:8686")

        def slow():
            lidarr_library.invalidate()  # a mutation lands while the fetch is in flight
            return [dict(ARTISTS[0])]

        client.fetch_artists.side_effect = slow
        lidarr_library.snapshot("artists", client)
        lidarr_library.snapshot("artists", client)
        assert client.fetch_artists.call_count == 2


# ------------------------------------------------------------------------------------------------ image proxy


class TestImages:
    def test_artist_image_headers_and_key_not_leaked(self, api, admin_h, lidarr):
        lidarr.stream_image()
        res = api.get("/api/library/artists/1/image", headers=admin_h)
        assert res.status_code == 200 and res.content == b"\xff\xd8img"
        assert res.headers["content-type"] == "image/jpeg"
        assert res.headers["cache-control"] == "private, max-age=86400"
        args, kwargs = lidarr.http.stream.call_args
        assert args == ("GET", "http://lidarr.test:8686/api/v1/mediacover/artist/1/poster.jpg")
        assert kwargs["headers"]["X-Api-Key"] == API_KEY
        assert API_KEY not in str(dict(res.headers)) and API_KEY not in args[1]

    def test_banner_and_album_cover_pick_cover_type(self, api, admin_h, lidarr):
        lidarr.stream_image(content_type="image/png")
        api.get("/api/library/artists/3/banner", headers=admin_h)
        assert lidarr.http.stream.call_args.args[1].endswith("/mediacover/artist/3/banner.jpg")
        res = api.get("/api/library/albums/105/cover", headers=admin_h)
        assert lidarr.http.stream.call_args.args[1].endswith("/mediacover/album/105/cover.jpg")
        assert res.headers["content-type"] == "image/png"

    def test_non_image_content_type_rejected(self, api, admin_h, lidarr):
        lidarr.stream_image(content_type="text/html")
        res = api.get("/api/library/artists/1/image", headers=admin_h, follow_redirects=False)
        assert res.status_code == 307 and res.headers["location"] == "/placeholder.svg"

    def test_size_cap_by_header_and_by_stream(self, api, admin_h, lidarr):
        lidarr.stream_image(length=lidarr_library.MAX_COVER_BYTES + 1)
        assert api.get("/api/library/artists/1/image", headers=admin_h).status_code == 502
        resp = lidarr.stream_image()
        resp.iter_bytes.return_value = iter([b"x" * (lidarr_library.MAX_COVER_BYTES // 2 + 1)] * 2)
        assert api.get("/api/library/artists/1/image", headers=admin_h).status_code == 502

    def test_upstream_errors(self, api, admin_h, lidarr):
        lidarr.stream_image(status=404)
        assert api.get("/api/library/artists/1/image", headers=admin_h, follow_redirects=False).status_code == 307
        lidarr.stream_image(status=302)
        assert api.get("/api/library/artists/1/image", headers=admin_h, follow_redirects=False).status_code == 502
        lidarr.stream_image(status=401)
        res = api.get("/api/library/artists/1/image", headers=admin_h)
        assert res.status_code == 502 and API_KEY not in res.text

    def test_no_artwork_redirects_to_placeholder(self, api, admin_h, lidarr):
        lidarr.artists[0]["images"] = []
        res = api.get("/api/library/artists/1/image", follow_redirects=False, headers=admin_h)
        assert res.status_code == 307 and res.headers["location"] == "/placeholder.svg"
        lidarr.http.stream.assert_not_called()

    @pytest.mark.parametrize("bad", ["abc", "-1", "1.5", "12345678901", "%2e%2e", "1%2F2", "%D9%A1"])
    def test_bad_id_404_without_contacting_lidarr(self, api, admin_h, lidarr, bad):
        for path in (f"/api/library/artists/{bad}/image", f"/api/library/artists/{bad}/banner",
                     f"/api/library/albums/{bad}/cover"):
            assert api.get(path, headers=admin_h).status_code == 404
        lidarr.http.stream.assert_not_called()
        assert lidarr.calls == []

    def test_unknown_id_404(self, api, admin_h, lidarr):
        assert api.get("/api/library/artists/99999/image", headers=admin_h).status_code == 404

    def test_client_rejects_unsafe_file_names(self):
        from plex_playlist_sync.clients.lidarr import LidarrApiError, LidarrClient

        client = LidarrClient("http://lidarr.test:8686", API_KEY)
        for name in ("../x.jpg", "a/b.jpg", "x.svg", "x.jpg?y=1", ""):
            with pytest.raises(LidarrApiError):
                client.fetch_mediacover("artist", 1, name, 100)
        with pytest.raises(LidarrApiError):
            client.fetch_mediacover("system", 1, "poster.jpg", 100)


# ------------------------------------------------------------------------------------------ details, actions


class TestDetailAndActions:
    def test_artist_detail(self, api, admin_h, lidarr):
        res = api.get("/api/library/artists/1", headers=admin_h)
        assert res.status_code == 200
        body = res.json()
        assert body["id"] == "1" and body["name"] == "The Beatles" and body["bio"] == "bio"
        assert body["albums"] and {a["artist_id"] for a in body["albums"]} == {"1"}
        assert any(u.endswith("/api/v1/album?artistId=1") for _m, u in lidarr.calls)

    def test_album_detail_with_tracks(self, api, admin_h, lidarr):
        body = api.get("/api/library/albums/100", headers=admin_h).json()
        assert body["id"] == "100" and body["title"] == "The Wall"
        assert [t["title"] for t in body["tracks"]] == ["Intro", "The Song", "Ünder"]
        assert body["tracks"][0]["track_number"] == 1 and body["tracks"][0]["duration_seconds"] == 180.0
        assert body["tracks"][0]["file"]["size_bytes"] == 1234 and body["tracks"][1]["file"] is None
        assert any(u.endswith("/api/v1/track?albumId=100") for _m, u in lidarr.calls)

    def test_details_404_and_bad_ids(self, api, admin_h, lidarr):
        assert api.get("/api/library/artists/424242", headers=admin_h).status_code == 404
        assert api.get("/api/library/albums/424242", headers=admin_h).status_code == 404
        assert api.get("/api/library/artists/not-a-number", headers=admin_h).status_code == 404
        assert api.get("/api/library/albums/1;2", headers=admin_h).status_code == 404

    def test_artist_monitor_is_fetch_modify_put(self, api, admin_h, lidarr):
        res = api.put("/api/library/artists/2/monitored", json={"monitored": False}, headers=admin_h)
        assert res.status_code == 200 and res.json()["id"] == "2"
        (url, body), = lidarr.puts
        assert url == "http://lidarr.test:8686/api/v1/artist/2"
        assert body["monitored"] is False and body["artistName"] == "Beatles Tribute" and body["path"] == "/music/Beatles Tribute"

    def test_album_monitor(self, api, admin_h, lidarr):
        res = api.put("/api/library/albums/101/monitored", json={"monitored": False}, headers=admin_h)
        assert res.status_code == 200 and res.json()["id"] == "101"
        assert lidarr.puts == [("http://lidarr.test:8686/api/v1/album/monitor", {"albumIds": [101], "monitored": False})]

    @pytest.mark.parametrize(
        "option,artist_monitored,monitored,unmonitored",
        [
            ("all", True, [201, 202, 203, 204], []),
            ("albums", True, [201, 204], [202, 203]),
            ("singles_eps", True, [202, 203], [201, 204]),
            ("none", False, [], [201, 202, 203, 204]),
        ],
    )
    def test_monitor_presets_map_to_lidarr(self, api, admin_h, lidarr, option, artist_monitored, monitored, unmonitored):
        def alb(i, kind):
            rec = dict(ALBUMS[0], id=i, artistId=1, albumType=kind)
            rec["artist"] = {"id": 1, "artistName": "The Beatles"}
            return rec

        lidarr.albums = [alb(201, "Album"), alb(202, "EP"), alb(203, "Single"), alb(204, "Album")]
        res = api.put(
            "/api/library/artists/1/monitored",
            json={"monitored": option != "none", "cascade_children": True, "monitor_option": option},
            headers=admin_h,
        )
        assert res.status_code == 200 and res.json()["monitor_option"] == option
        artist_put = [b for u, b in lidarr.puts if u.endswith("/api/v1/artist/1")]
        assert [b["monitored"] for b in artist_put] == [artist_monitored]
        album_puts = [b for u, b in lidarr.puts if u.endswith("/api/v1/album/monitor")]
        assert {tuple(b["albumIds"]) for b in album_puts if b["monitored"]} == ({tuple(monitored)} if monitored else set())
        assert {tuple(b["albumIds"]) for b in album_puts if not b["monitored"]} == (
            {tuple(unmonitored)} if unmonitored else set()
        )

    def test_preset_invalidates_cache(self, api, admin_h, lidarr):
        api.get("/api/library/artists/paged", headers=admin_h)
        api.put("/api/library/artists/1/monitored", json={"monitored": True, "monitor_option": "all"}, headers=admin_h)
        api.get("/api/library/artists/paged", headers=admin_h)
        assert lidarr.count("/api/v1/artist") == 2

    def test_search_and_refresh_commands(self, api, admin_h, lidarr):
        assert api.post("/api/library/artists/3/search", headers=admin_h).status_code == 200
        assert api.post("/api/library/albums/105/search", headers=admin_h).status_code == 200
        assert api.post("/api/library/artists/3/refresh", headers=admin_h).status_code == 200
        bodies = [b for u, b in lidarr.puts if u.endswith("/api/v1/command")]
        assert bodies == [
            {"name": "ArtistSearch", "artistId": 3},
            {"name": "AlbumSearch", "albumIds": [105]},
            {"name": "RefreshArtist", "artistId": 3},
        ]

    def test_bad_ids_never_reach_lidarr(self, api, admin_h, lidarr):
        for method, path, kw in (
            ("put", "/api/library/artists/x/monitored", {"json": {"monitored": True}}),
            ("put", "/api/library/albums/-1/monitored", {"json": {"monitored": True}}),
            ("post", "/api/library/artists/1.0/search", {}),
            ("post", "/api/library/albums/abc/search", {}),
            ("post", "/api/library/artists/abc/refresh", {}),
        ):
            assert getattr(api, method)(path, headers=admin_h, **kw).status_code == 404
        assert lidarr.calls == []

    def test_mutations_run_under_lidarr_work_guard(self, api, admin_h, lidarr):
        with patch("plex_playlist_sync.api.routes.library.work_guard") as guard:
            guard.return_value.__enter__.return_value = None
            guard.return_value.__exit__.return_value = False
            assert api.post("/api/library/albums/105/search", headers=admin_h).status_code == 200
            assert guard.call_args.args[1] == "lidarr"

    def test_mode_change_is_409_and_sends_nothing(self, api, admin_h, lidarr):
        with patch("plex_playlist_sync.api.routes.library.work_guard", side_effect=ModeChanged("lidarr", "native")):
            for method, path, kw in (
                ("put", "/api/library/artists/2/monitored", {"json": {"monitored": True}}),
                ("put", "/api/library/albums/101/monitored", {"json": {"monitored": True}}),
                ("post", "/api/library/artists/2/search", {}),
                ("post", "/api/library/albums/101/search", {}),
                ("post", "/api/library/artists/2/refresh", {}),
            ):
                assert getattr(api, method)(path, headers=admin_h, **kw).status_code == 409
        assert lidarr.puts == []

    def test_search_routes_409_in_native_mode(self, api, admin_h, test_db):
        assert api.post("/api/library/artists/1/search", headers=admin_h).status_code == 409
        assert api.post("/api/library/albums/1/search", headers=admin_h).status_code == 409


# ------------------------------------------------------------------------------- tracks, native-only routes


NATIVE_ONLY_ROUTES = [
    ("post", "/api/library/scan"),
    ("post", "/api/library/scan/cancel"),
    ("delete", "/api/library/files/f1"),
    ("put", "/api/library/tracks/1/monitored"),
    ("delete", "/api/library/tracks/1"),
    ("delete", "/api/library/artists/1"),
    ("delete", "/api/library/albums/1"),
    ("post", "/api/library/rename/preview"),
    ("post", "/api/library/rename/apply"),
    ("post", "/api/library/manual-import/scan"),
    ("post", "/api/library/manual-import/commit"),
    ("post", "/api/library/manual-import/fingerprint"),
]


def _body(method, path):
    return {"json": {"monitored": True}} if path.endswith("monitored") else (
        {"json": {"items": []}} if method == "post" and "commit" in path else {}
    )


class TestTracksAndNativeOnly:
    def test_tracks_by_album(self, api, admin_h, lidarr):
        res = api.get("/api/library/tracks/paged?album_id=100&sort_key=title&sort_dir=desc", headers=admin_h)
        assert res.status_code == 200
        body = res.json()
        assert body["mode"] == "lidarr" and body["total"] == 3
        assert [t["title"] for t in body["records"]] == ["Ünder", "The Song", "Intro"]
        assert body["records"][0]["album_title"] == "The Wall" and body["records"][0]["artist_name"] == "The Beatles"
        assert api.get("/api/library/tracks/paged?album_id=100&q=song", headers=admin_h).json()["total"] == 1
        page2 = api.get("/api/library/tracks/paged?album_id=100&page=2&page_size=2", headers=admin_h).json()
        assert [t["title"] for t in page2["records"]] == ["Ünder"] and page2["total"] == 3

    def test_tracks_without_album_is_409_and_bad_album_404(self, api, admin_h, lidarr):
        res = api.get("/api/library/tracks/paged", headers=admin_h)
        assert res.status_code == 409 and res.json()["detail"] == NATIVE_ONLY
        assert api.get("/api/library/tracks/paged?album_id=abc", headers=admin_h).status_code == 404
        assert lidarr.calls == []

    def test_tracks_index_is_empty(self, api, admin_h, lidarr):
        body = api.get("/api/library/tracks/index", headers=admin_h).json()
        assert body["groups"] == [] and body["total"] == 0 and body["mode"] == "lidarr"
        assert api.get("/api/library/tracks/index?album_id=100", headers=admin_h).json()["groups"] == []
        assert lidarr.calls == [("GET", "http://lidarr.test:8686/api/v1/album/100"),
                                ("GET", "http://lidarr.test:8686/api/v1/track?albumId=100")]

    @pytest.mark.parametrize("method,path", NATIVE_ONLY_ROUTES)
    def test_native_only_routes_409(self, api, admin_h, lidarr, method, path):
        res = getattr(api, method)(path, headers=admin_h, **_body(method, path))
        assert res.status_code == 409 and res.json()["detail"] == NATIVE_ONLY
        assert lidarr.calls == []

    @pytest.mark.parametrize("method,path", NATIVE_ONLY_ROUTES)
    def test_native_only_routes_still_native_in_native_mode(self, api, admin_h, test_db, method, path):
        res = getattr(api, method)(path, headers=admin_h, **_body(method, path))
        assert res.status_code != 409 or res.json().get("detail") != NATIVE_ONLY


# ----------------------------------------------------------------------- auth, gateway, upstream-failure redaction


LIDARR_ENDPOINTS = [
    ("get", "/api/library/artists/paged"),
    ("get", "/api/library/artists/index"),
    ("get", "/api/library/albums/paged"),
    ("get", "/api/library/albums/index"),
    ("get", "/api/library/tracks/paged?album_id=100"),
    ("get", "/api/library/artists/1"),
    ("get", "/api/library/albums/100"),
    ("get", "/api/library/artists/1/image"),
    ("get", "/api/library/artists/1/banner"),
    ("get", "/api/library/albums/100/cover"),
    ("put", "/api/library/artists/1/monitored"),
    ("put", "/api/library/albums/100/monitored"),
    ("post", "/api/library/artists/1/search"),
    ("post", "/api/library/albums/100/search"),
    ("post", "/api/library/scan"),
]


class TestAuthAndFailures:
    @pytest.mark.parametrize("method,path", LIDARR_ENDPOINTS)
    def test_non_admin_forbidden_and_anonymous_rejected(self, api, test_db, test_config, users, lidarr, method, path):
        kw = {"json": {"monitored": True}} if method == "put" else {}
        alice = _headers(users["alice"], test_db, test_config)
        assert getattr(api, method)(path, headers=alice, **kw).status_code == 403
        assert getattr(api, method)(path, **kw).status_code in (401, 403)
        assert lidarr.calls == []

    @pytest.mark.parametrize("method,path", LIDARR_ENDPOINTS[:2] + LIDARR_ENDPOINTS[10:])
    def test_gateway_tier_denies(self, test_db, tmp_path, users, lidarr, method, path):
        cfg = Config(
            plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), role="gateway",
            internal_core_secret="s" * 40, trackseerr_core_url="http://core.internal:5251",
        )
        app = create_app(db=test_db, config=cfg)
        app.dependency_overrides[get_db] = lambda: test_db
        app.dependency_overrides[get_config] = lambda: cfg
        client = TestClient(app)
        kw = {"json": {"monitored": True}} if method == "put" else {}
        res = getattr(client, method)(path, headers=_headers(users["admin"], test_db, cfg), **kw)
        assert res.status_code in (403, 404)
        assert lidarr.calls == []

    def test_upstream_failure_is_502_and_redacted(self, api, admin_h, lidarr):
        lidarr.fail = httpx.ConnectError(f"cannot connect http://lidarr.test:8686/api/v1/artist?apikey={API_KEY}")
        for path in ("/api/library/artists/paged", "/api/library/artists/index", "/api/library/artists/1",
                     "/api/library/albums/100"):
            res = api.get(path, headers=admin_h)
            assert res.status_code == 502, path
            assert API_KEY not in res.text and "apikey" not in res.text.lower()

    def test_rejected_key_is_502(self, api, admin_h, lidarr):
        lidarr.http.get.side_effect = lambda url, **kw: _resp(401)
        res = api.get("/api/library/albums/paged", headers=admin_h)
        assert res.status_code == 502 and API_KEY not in res.text

    def test_mutation_failure_is_502_and_still_invalidates(self, api, admin_h, lidarr):
        api.get("/api/library/artists/paged", headers=admin_h)
        lidarr.http.put.side_effect = lambda url, **kw: _resp(500)
        res = api.put("/api/library/artists/2/monitored", json={"monitored": False}, headers=admin_h)
        assert res.status_code == 502 and API_KEY not in res.text
        api.get("/api/library/artists/paged", headers=admin_h)
        assert lidarr.count("/api/v1/artist") == 2  # list again after invalidation (the put fetch is artist/2)

    def test_lidarr_not_configured_is_502(self, api, admin_h, test_db):
        test_db.update_media_management_settings({"library_mode": "lidarr"})
        res = api.get("/api/library/artists/paged", headers=admin_h)
        assert res.status_code == 502 and res.json()["detail"] == "Lidarr is not configured"


# ------------------------------------------------------------------------------- security hardening (audit)


from plex_playlist_sync.clients.lidarr import COVER_CONTENT_TYPES, LidarrApiError, LidarrClient  # noqa: E402


def _img(api, h, path="/api/library/artists/1/image", headers=None):
    return api.get(path, headers={**h, **(headers or {})}, follow_redirects=False)


class TestCoverProxyHardening:
    def test_headers_nosniff_csp_and_etag(self, api, admin_h, lidarr):
        lidarr.stream_image()
        res = _img(api, admin_h)
        assert res.status_code == 200
        assert res.headers["x-content-type-options"] == "nosniff"
        assert res.headers["content-security-policy"] == "default-src 'none'; sandbox"
        assert res.headers["etag"].startswith('W/"')

    def test_allowlist_is_exactly_four_raster_types(self):
        assert COVER_CONTENT_TYPES == {"image/jpeg", "image/png", "image/webp", "image/gif"}

    @pytest.mark.parametrize("ctype", ["image/png", "image/webp", "image/gif", "IMAGE/JPEG; charset=binary"])
    def test_allowed_types_pass(self, api, admin_h, lidarr, ctype):
        lidarr.stream_image(content_type=ctype)
        assert _img(api, admin_h).status_code == 200

    @pytest.mark.parametrize("ctype", ["image/svg+xml", "image/svg+xml; charset=utf-8", "image/x-icon", "text/html", ""])
    def test_svg_and_other_types_become_placeholder(self, api, admin_h, lidarr, ctype, caplog):
        lidarr.stream_image(content_type=ctype)
        with caplog.at_level("WARNING"):
            res = _img(api, admin_h)
        assert res.status_code == 307 and res.headers["location"] == "/placeholder.svg"
        assert any("refused" in r.message for r in caplog.records) and API_KEY not in caplog.text

    def test_default_cap_is_3mb(self):
        assert lidarr_library.MAX_COVER_BYTES == 3 * 1024 * 1024

    def test_oversized_content_length(self, api, admin_h, lidarr):
        lidarr.stream_image(length=lidarr_library.MAX_COVER_BYTES + 1)
        assert _img(api, admin_h).status_code == 502

    def test_oversized_stream_without_length(self, api, admin_h, lidarr):
        resp = lidarr.stream_image()
        assert "content-length" not in resp.headers
        resp.iter_bytes.return_value = iter([b"x" * (lidarr_library.MAX_COVER_BYTES // 2 + 1)] * 2)
        assert _img(api, admin_h).status_code == 502

    def test_redirect_not_followed(self, api, admin_h, lidarr):
        lidarr.stream_image(status=302)
        assert _img(api, admin_h).status_code == 502

    def test_deadline_exceeded(self, api, admin_h, lidarr):
        lidarr.stream_image()
        ticks = iter([0.0, 6.0, 12.0, 18.0, 24.0])
        with patch("plex_playlist_sync.clients.lidarr._monotonic", lambda: next(ticks)):
            res = _img(api, admin_h)
        assert res.status_code == 502 and "timed out" in res.json()["detail"]

    def test_semaphore_saturation_returns_503_with_retry_after(self, api, admin_h, lidarr):
        lidarr.stream_image()
        full = threading.BoundedSemaphore(1)
        full.acquire()
        with patch.object(lidarr_library, "_cover_slots", full), patch.object(lidarr_library, "COVER_SLOT_WAIT_SECONDS", 0.05):
            res = _img(api, admin_h)
        assert res.status_code == 503 and res.headers["retry-after"] == "2"
        lidarr.http.stream.assert_not_called()

    def test_slot_is_released_after_each_fetch(self, api, admin_h, lidarr):
        lidarr.stream_image()
        slots = threading.BoundedSemaphore(1)
        with patch.object(lidarr_library, "_cover_slots", slots):
            for _ in range(3):
                assert _img(api, admin_h).status_code == 200
        lidarr.stream_image(content_type="text/html")
        with patch.object(lidarr_library, "_cover_slots", slots):
            assert _img(api, admin_h).status_code == 307
            assert slots.acquire(blocking=False)

    def test_etag_revalidation_304(self, api, admin_h, lidarr):
        lidarr.stream_image()
        first = _img(api, admin_h)
        etag = first.headers["etag"]
        assert lidarr.http.stream.call_count == 1
        again = _img(api, admin_h, headers={"If-None-Match": etag})
        assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == etag
        assert lidarr.http.stream.call_count == 1  # answered without another upstream fetch

    def test_etag_changes_with_upstream_validator(self, api, admin_h, lidarr):
        resp = lidarr.stream_image()
        resp.headers["etag"] = '"v1"'
        e1 = _img(api, admin_h).headers["etag"]
        lidarr_library.invalidate()
        resp = lidarr.stream_image()
        resp.headers["etag"] = '"v2"'
        e2 = _img(api, admin_h).headers["etag"]
        assert e1 != e2
        lidarr_library.invalidate()
        stale = lidarr.stream_image()
        stale.headers["etag"] = '"v2"'
        assert _img(api, admin_h, headers={"If-None-Match": e2}).status_code == 304

    def test_etag_mismatch_serves_body(self, api, admin_h, lidarr):
        lidarr.stream_image()
        assert _img(api, admin_h, headers={"If-None-Match": 'W/"nope"'}).status_code == 200

    def test_etag_matches_helper(self):
        assert lidarr_library.etag_matches('"a", W/"b"', 'W/"b"')
        assert lidarr_library.etag_matches("*", 'W/"b"')
        assert not lidarr_library.etag_matches(None, 'W/"b"')

    def test_client_fetch_returns_validator(self):
        client = LidarrClient("http://lidarr.test:8686", API_KEY)
        with patch("plex_playlist_sync.clients.lidarr.httpx.Client") as cls:
            resp = MagicMock(status_code=200, headers={"content-type": "image/png", "last-modified": "Mon"})
            resp.iter_bytes.return_value = iter([b"abc"])
            cls.return_value.__enter__.return_value.stream.return_value.__enter__.return_value = resp
            cover = client.fetch_mediacover("album", 3, "cover.png", 100)
            assert cover.body == b"abc" and cover.content_type == "image/png" and cover.validator == "Mon"
            assert cls.call_args.kwargs["follow_redirects"] is False

    def test_native_image_routes_carry_hardening_headers(self, api, admin_h, test_db, tmp_path):
        test_db.update_media_management_settings({"library_mode": "native"})
        folder = tmp_path / "Art"
        folder.mkdir()
        (folder / "artist.png").write_bytes(b"\x89PNG")
        artist = {"id": "n1", "name": "Nat", "path": str(folder)}
        with patch.object(Database, "get_library_artist", return_value=artist), patch(
            "plex_playlist_sync.api.routes.library.validate_media_path", return_value=folder
        ):
            res = _img(api, admin_h, "/api/library/artists/n1/image")
        assert res.status_code == 200 and res.headers["content-type"] == "image/png"
        assert res.headers["x-content-type-options"] == "nosniff"
        assert res.headers["content-security-policy"] == "default-src 'none'; sandbox"


class TestSnapshotPerformance:
    @staticmethod
    def _client(n):
        client = MagicMock(base_url="http://lidarr.test:8686", api_key=API_KEY)
        artists = [_artist(i + 1, f"Artist {i:05d}") for i in range(100)]
        albums = []
        for i in range(n):
            a = artists[i % 100]
            albums.append({**_album(i, a, f"Album {i:06d}"), "id": 1000 + i})
        client.fetch_artists.return_value = artists
        client.fetch_albums.return_value = albums
        return client

    def test_deep_page_and_index_fast_and_second_request_does_not_rebuild(self):
        client = self._client(50_000)
        args = ("albums", client, 1, 50, "title", "asc", None, False)
        lidarr_library.list_page(*args)  # first build
        with patch.object(lidarr_library, "_table", wraps=lidarr_library._table) as table:
            t0 = time.perf_counter()
            deep, total = lidarr_library.list_page("albums", client, 999, 50, "title", "asc", None, False)
            page_ms = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            lidarr_library.list_index("albums", client, "title", "asc", None, False)  # first index build
            index_first_ms = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            total2, groups = lidarr_library.list_index("albums", client, "title", "asc", None, False)
            index_ms = (time.perf_counter() - t0) * 1000
            lidarr_library.list_page("albums", client, 3, 50, "title", "asc", None, False)
        assert total == total2 == 50_000 and len(deep) == 50
        assert deep[0]["title"] == "Album 049900"
        assert page_ms < 500 and index_ms < 500, (page_ms, index_ms)
        assert index_first_ms < 5000
        # the sorted order was built once (first page); index re-uses its rows but builds no new filtered sort
        assert table.call_count == 1  # only the lazy index build
        assert sum(g["count"] for g in groups) == 50_000
        assert client.fetch_albums.call_count == 1

    def test_offsets_stay_consistent_between_cached_page_and_index(self):
        client = self._client(300)
        _, groups = lidarr_library.list_index("albums", client, "title", "desc", None, False)
        for g in groups:
            first, _ = lidarr_library.list_page("albums", client, g["offset"] // 10 + 1, 10, "title", "desc", None, False)
            assert first[g["offset"] % 10]["title"]

    def test_distinct_filters_cached_separately_and_lru_bounded(self):
        client = self._client(200)
        state = lidarr_library.snapshot_state("albums", client)
        for i in range(lidarr_library.ORDER_CACHE_SIZE + 5):
            lidarr_library.list_page("albums", client, 1, 10, "title", "asc", f"Album {i}", False)
        assert len(state._orders) == lidarr_library.ORDER_CACHE_SIZE
        a, _ = lidarr_library.list_page("albums", client, 1, 10, "title", "asc", None, False)
        b, _ = lidarr_library.list_page("albums", client, 1, 10, "title", "desc", None, False)
        assert a != b

    def test_cover_file_uses_id_dict(self, lidarr):
        client = self._client(10)
        state = lidarr_library.snapshot_state("albums", client)
        assert state.by_id["1003"].id == "1003"
        assert lidarr_library.cover_file("albums", client, 1003, ("cover",)) == "cover.jpg"
        client.fetch_album.assert_not_called()

    def test_cache_keyed_by_url_and_key_hash(self):
        a = self._client(5)
        b = self._client(5)
        b.api_key = "another-key-1234567890"
        lidarr_library.snapshot("albums", a)
        lidarr_library.snapshot("albums", b)
        assert a.fetch_albums.call_count == 1 and b.fetch_albums.call_count == 1  # key change is a cache miss
        assert API_KEY not in str(lidarr_library._entries["albums"].identity)


class TestInvalidationTriggers:
    def _warm(self, api, h, lidarr):
        api.get("/api/library/artists/paged", headers=h)
        assert lidarr.count("/api/v1/artist") == 1

    def test_lidarr_settings_save(self, api, admin_h, lidarr):
        self._warm(api, admin_h, lidarr)
        res = api.put("/api/settings/lidarr", json={"url": "http://lidarr.test:8686", "api_key": "new-key-9876543210"}, headers=admin_h)
        assert res.status_code == 200, res.text
        api.get("/api/library/artists/paged", headers=admin_h)
        assert lidarr.count("/api/v1/artist") == 2

    def test_mode_switch(self, api, admin_h, lidarr, test_db):
        from plex_playlist_sync import library_manager

        self._warm(api, admin_h, lidarr)
        library_manager.switch_mode(test_db, "native", "test")
        assert lidarr_library._entries == {}

    def test_acquisition_adapter_add(self):
        from plex_playlist_sync.clients.acquisition.lidarr_adapter import LidarrAdapter
        from plex_playlist_sync.models import AcquisitionSearchResult

        client = self._client_with_entry()
        adapter = LidarrAdapter("http://lidarr.test:8686", API_KEY)
        adapter.client = MagicMock()
        adapter.client.add_artist_and_albums.return_value = {"status": "success"}
        result = MagicMock(spec=AcquisitionSearchResult, artist="X", album="Y", title="Y", item_type="album")
        with patch("plex_playlist_sync.clients.acquisition.lidarr_adapter.is_safe_service_url", return_value=True):
            adapter.download(result)
        assert lidarr_library._entries == {} and client

    def test_trickle_worker_add(self, test_db):
        from plex_playlist_sync.lidarr_queue import lidarr_worker

        self._client_with_entry()
        mock_client = MagicMock()
        mock_client.add_artist_and_albums.return_value = {"status": "error", "message": "nope"}
        lidarr_worker._process_groups({"x": [{"artist": "X", "album": "Y", "id": "r1"}]}, mock_client, test_db)
        assert lidarr_library._entries == {}

    @staticmethod
    def _client_with_entry():
        client = TestSnapshotPerformance._client(3)
        lidarr_library.snapshot("albums", client)
        assert lidarr_library._entries
        return client


class TestSynthesizedValues:
    def test_track_monitored_is_null_not_true(self, api, admin_h, lidarr):
        body = api.get("/api/library/albums/101", headers=admin_h).json()
        assert all(t["monitored"] is None for t in body["tracks"])

    def test_artist_monitor_option_from_lidarr_else_null(self):
        base = _artist(1, "A")
        assert lidarr_library.artist_row(base).record["monitor_option"] is None
        assert lidarr_library.artist_row({**base, "monitorNewItems": "all"}).record["monitor_option"] == "all"
        assert lidarr_library.artist_row({**base, "monitorNewItems": "none"}).record["monitor_option"] == "none"
        assert lidarr_library.artist_row({**base, "monitorNewItems": "new"}).record["monitor_option"] is None
        assert lidarr_library.artist_row({**base, "addOptions": {"monitor": "all"}}).record["monitor_option"] == "all"


class TestGatewayDenial:
    PATHS = [
        "/api/library/artists/1",
        "/api/library/albums/101",
        "/api/library/artists/1/image",
        "/api/library/artists/1/banner",
        "/api/library/albums/101/cover",
    ]

    @pytest.mark.parametrize("path", PATHS)
    def test_gateway_tier_is_denied_and_never_reaches_lidarr(self, test_db, tmp_path, users, lidarr, path):
        cfg = Config(
            plex_url="http://p", plex_token="t", data_dir=str(tmp_path), role="gateway",
            internal_core_secret="s" * 40, trackseerr_core_url="http://core.internal:5251",
        )
        app = create_app(db=test_db, config=cfg)
        app.dependency_overrides[get_db] = lambda: test_db
        app.dependency_overrides[get_config] = lambda: cfg
        res = TestClient(app).get(path, headers=_headers(users["admin"], test_db, cfg), follow_redirects=False)
        assert res.status_code in (403, 404)  # the gateway guard answers first; the route dependency is the backstop
        lidarr.http.stream.assert_not_called()

    @pytest.mark.parametrize("pattern", ["/artists/{artist_id}", "/albums/{album_id}", "/artists/{artist_id}/image",
                                         "/artists/{artist_id}/banner", "/albums/{album_id}/cover"])
    def test_route_depends_on_require_core_tier(self, pattern):
        from plex_playlist_sync.api.dependencies import require_core_tier
        from plex_playlist_sync.api.routes.library import router

        route = next(r for r in router.routes if getattr(r, "path", "") in (pattern, f"/library{pattern}") and "GET" in r.methods)
        calls = []
        stack = [route.dependant]
        while stack:
            dep = stack.pop()
            calls.append(dep.call)
            stack.extend(dep.dependencies)
        assert require_core_tier in calls

    def test_dependency_denies_gateway_role_directly(self, tmp_path):
        from fastapi import HTTPException

        from plex_playlist_sync.api.dependencies import require_core_tier

        cfg = Config(plex_url="http://p", plex_token="t", data_dir=str(tmp_path), role="gateway",
                     internal_core_secret="s" * 40, trackseerr_core_url="http://core.internal:5251")
        with pytest.raises(HTTPException) as exc:
            require_core_tier(cfg)
        assert exc.value.status_code == 403


class TestPageBound:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/library/artists/paged",
            "/api/library/albums/paged",
            "/api/library/tracks/paged",
            "/api/activity/queue",
            "/api/activity/history",
            "/api/activity/blocklist",
            "/api/wanted/missing",
            "/api/wanted/cutoff",
        ],
    )
    def test_huge_page_is_422(self, api, admin_h, lidarr, path):
        for page in (10**30, 1_000_001):
            assert api.get(f"{path}?page={page}", headers=admin_h).status_code == 422, path

    def test_max_page_accepted(self, api, admin_h, lidarr):
        res = api.get("/api/library/artists/paged?page=1000000", headers=admin_h)
        assert res.status_code == 200 and res.json()["records"] == []

"""Performance regressions: native Wanted (cutoff) index, bounded Lidarr calls for paging/Wanted, cover caching."""

import sqlite3
from unittest.mock import MagicMock

import pytest

from plex_playlist_sync import lidarr_library
from plex_playlist_sync.storage import Database
from tests.test_lidarr_library import (  # noqa: F401  (fixtures + helpers shared with the Lidarr library suite)
    API_KEY,
    Lidarr,
    _fresh_cache,
    _resp,
    admin_h,
    api,
    test_config,
    test_db,
    users,
)


class WantedLidarr(Lidarr):
    """The shared fake plus Lidarr's paged ``wanted/*`` endpoint."""

    def _get(self, url, **kw):
        path = url.split("/api/v1/", 1)[1]
        if path.startswith("wanted/"):
            self.calls.append(("GET", url))
            return _resp(payload={
                "page": 1, "pageSize": 50, "totalRecords": 1500,
                "records": [
                    {"id": n, "title": f"Album {n}", "artist": {"artistName": f"Artist {n}"},
                     "releaseDate": "2020-01-01T00:00:00Z", "monitored": True}
                    for n in range(50)
                ],
            })
        return super()._get(url, **kw)


@pytest.fixture
def lidarr(test_db):
    fake = WantedLidarr(test_db)
    yield fake
    fake.stop()


# ------------------------------------------------------------------------------------------------ indexes


def test_cutoff_composite_index_exists_and_is_used():
    db = Database(":memory:")
    names = {r[1] for r in db.conn.execute("PRAGMA index_list(library_files)")}
    assert "idx_lib_files_track_cutoff" in names
    cols = [r[2] for r in db.conn.execute("PRAGMA index_info(idx_lib_files_track_cutoff)")]
    assert cols == ["track_id", "cutoff_met", "id"]
    # Seed enough rows for the planner to have a reason to prefer the wrong (single column) index.
    for a in range(20):
        db.conn.execute("INSERT INTO library_artists(id,name,clean_name,sort_name) VALUES (?,?,?,?)", (f"a{a}", "n", "n", "n"))
        db.conn.execute("INSERT INTO library_albums(id,artist_id,title,clean_title) VALUES (?,?,?,?)", (f"b{a}", f"a{a}", "t", "t"))
        for t in range(50):
            tid = f"b{a}t{t}"
            db.conn.execute("INSERT INTO library_tracks(id,album_id,artist_id,title,clean_title) VALUES (?,?,?,?,?)", (tid, f"b{a}", f"a{a}", "x", "x"))
            db.conn.execute(
                "INSERT INTO library_files(id,track_id,file_path,relative_path,codec,quality_name,cutoff_met) VALUES (?,?,?,?,?,?,0)",
                (tid + "f", tid, tid, tid, "mp3", "MP3"),
            )
    db.conn.execute("ANALYZE")
    plan = " ".join(
        str(r[3])
        for r in db.conn.execute("EXPLAIN QUERY PLAN " + db._WANTED_SELECT + db._WANTED_CUTOFF_FROM + " ORDER BY ar.sort_name LIMIT 50")
    )
    assert "idx_lib_files_track_cutoff" in plan and "idx_lib_files_cutoff" not in plan
    rows, total = db.list_wanted("cutoff", 1, 50, "artist", "asc")
    assert total == 1000 and len(rows) == 50


def test_migration_is_idempotent_on_existing_db(tmp_path):
    path = tmp_path / "x.db"
    Database(str(path)).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX idx_lib_files_track_cutoff")
    conn.execute("DELETE FROM schema_migrations WHERE version >= 42")
    conn.commit()
    conn.close()
    db = Database(str(path))
    assert "idx_lib_files_track_cutoff" in {r[1] for r in db.conn.execute("PRAGMA index_list(library_files)")}


# ------------------------------------------------------------------------------------------ bounded Lidarr calls


def test_wanted_page_is_one_paged_lidarr_call(api, admin_h, lidarr):
    res = api.get("/api/wanted/missing?page=1&page_size=50", headers=admin_h)
    assert res.status_code == 200 and res.json()["total"] == 1500 and len(res.json()["records"]) == 50
    wanted = [u for m, u in lidarr.calls if "wanted/" in u]
    assert len(wanted) == 1 and "pageSize=50" in wanted[0] and "page=1" in wanted[0]
    assert len(lidarr.calls) == 1  # nothing else (no per-artist or whole-library fetch) before page 1


def test_albums_paging_fetches_lidarr_once_for_many_pages(api, admin_h, lidarr):
    for page in range(1, 6):
        assert api.get(f"/api/library/albums/paged?page={page}&page_size=5", headers=admin_h).status_code == 200
    api.get("/api/library/albums/index", headers=admin_h)
    assert lidarr.count("/api/v1/album?includeAllArtistAlbums=true") == 1
    assert lidarr.count("/api/v1/artist") == 0  # albums carry their artist: no extra artist list fetch


def test_stale_list_is_served_at_once_and_refreshed_in_background(api, admin_h, lidarr, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(lidarr_library, "_clock", lambda: now[0])
    queued = []
    monkeypatch.setattr(lidarr_library, "_run_background", queued.append)
    url = "/api/library/albums/paged?page=1&page_size=5"
    assert api.get(url, headers=admin_h).status_code == 200
    now[0] += lidarr_library.LIST_TTL_SECONDS + 1  # expired, but inside the stale window
    assert api.get(url, headers=admin_h).status_code == 200
    assert api.get(url, headers=admin_h).status_code == 200
    assert lidarr.count("/api/v1/album?includeAllArtistAlbums=true") == 1  # requests never waited on Lidarr
    assert len(queued) == 1  # one refresh, not one per request
    queued[0]()
    assert lidarr.count("/api/v1/album?includeAllArtistAlbums=true") == 2
    api.get(url, headers=admin_h)
    assert lidarr.count("/api/v1/album?includeAllArtistAlbums=true") == 2  # fresh again


def test_beyond_stale_window_blocks_and_refetches(api, admin_h, lidarr, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(lidarr_library, "_clock", lambda: now[0])
    url = "/api/library/albums/paged?page=1&page_size=5"
    api.get(url, headers=admin_h)
    now[0] += lidarr_library.LIST_STALE_SECONDS + 1
    api.get(url, headers=admin_h)
    assert lidarr.count("/api/v1/album?includeAllArtistAlbums=true") == 2


def test_cover_lookup_never_loads_the_whole_album_list(api, admin_h, lidarr):
    lidarr.stream_image()
    assert api.get("/api/library/albums/105/cover", headers=admin_h).status_code == 200
    assert lidarr.count("/api/v1/album?includeAllArtistAlbums=true") == 0
    assert lidarr.count("/api/v1/album/105") == 1


# ------------------------------------------------------------------------------------------------ artwork


def _stream(lidarr, *responses):
    ctxs = []
    for status, body in responses:
        resp = MagicMock()
        resp.status_code = status
        resp.headers = {"content-type": "image/jpeg"}
        resp.iter_bytes.return_value = iter([body])
        ctx = MagicMock()
        ctx.__enter__.return_value = resp
        ctxs.append(ctx)
    lidarr.http.stream.side_effect = ctxs


def test_cover_has_caching_headers_and_304(api, admin_h, lidarr):
    lidarr.stream_image()
    res = api.get("/api/library/artists/1/image", headers=admin_h)
    assert res.status_code == 200
    cc = res.headers["cache-control"]
    assert cc == "private, max-age=86400"
    etag = res.headers["etag"]
    again = api.get("/api/library/artists/1/image", headers={**admin_h, "If-None-Match": etag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == etag
    assert lidarr.http.stream.call_count == 1


def test_304_on_matching_etag_without_any_upstream_request_after_restart(api, admin_h, lidarr):
    lidarr.stream_image()
    etag = api.get("/api/library/albums/105/cover", headers=admin_h).headers["etag"]
    lidarr_library._cover_validators.clear()  # in-memory validators gone (process restart)
    lidarr.http.stream.reset_mock()
    res = api.get("/api/library/albums/105/cover", headers={**admin_h, "If-None-Match": etag})
    assert res.status_code == 304 and lidarr.http.stream.call_count == 0


def test_cached_cover_hit_makes_no_upstream_request(api, admin_h, lidarr):
    lidarr.stream_image(body=b"\xff\xd8thumbnail-bytes")
    first = api.get("/api/library/artists/2/image", headers=admin_h)
    lidarr_library._cover_validators.clear()
    lidarr.http.stream.reset_mock()
    lidarr.calls.clear()
    second = api.get("/api/library/artists/2/image", headers=admin_h)
    assert second.status_code == 200 and second.content == first.content == b"\xff\xd8thumbnail-bytes"
    assert second.headers["etag"] == first.headers["etag"]
    assert lidarr.http.stream.call_count == 0


def test_size_param_requests_lidarr_thumbnail_variant(api, admin_h, lidarr):
    lidarr.stream_image()
    assert api.get("/api/library/artists/1/image?size=250", headers=admin_h).status_code == 200
    assert lidarr.http.stream.call_args.args[1].endswith("/mediacover/artist/1/poster-250.jpg")
    assert api.get("/api/library/albums/105/cover?size=500", headers=admin_h).status_code == 200
    assert lidarr.http.stream.call_args.args[1].endswith("/mediacover/album/105/cover-500.jpg")
    # A size and the original are separate cache entries.
    lidarr.stream_image()
    api.get("/api/library/artists/1/image", headers=admin_h)
    assert lidarr.http.stream.call_args.args[1].endswith("/mediacover/artist/1/poster.jpg")


def test_unsupported_size_is_ignored_and_missing_variant_falls_back_to_original(api, admin_h, lidarr):
    lidarr.stream_image()
    api.get("/api/library/artists/1/image?size=999", headers=admin_h)
    assert lidarr.http.stream.call_args.args[1].endswith("/poster.jpg")
    lidarr.http.stream.reset_mock()
    _stream(lidarr, (404, b""), (200, b"\xff\xd8orig"))
    res = api.get("/api/library/artists/3/image?size=250", headers=admin_h)
    assert res.status_code == 200 and res.content == b"\xff\xd8orig"
    urls = [c.args[1] for c in lidarr.http.stream.call_args_list]
    assert urls[0].endswith("/poster-250.jpg") and urls[1].endswith("/poster.jpg")
    lidarr.http.stream.reset_mock()
    assert api.get("/api/library/artists/3/image?size=250", headers=admin_h).content == b"\xff\xd8orig"
    assert lidarr.http.stream.call_count == 0  # the fallback is cached under the sized key


def test_sized_cover_name():
    assert lidarr_library.sized_cover_name("poster.jpg", 250) == "poster-250.jpg"
    assert lidarr_library.sized_cover_name("cover-500.png", 250) == "cover-250.png"
    assert lidarr_library.sized_cover_name("poster.jpg", None) == "poster.jpg"
    assert lidarr_library.sized_cover_name("poster.jpg", 123) == "poster.jpg"

"""Lidarr mode: stats follow Lidarr's trackCount (not totalTrackCount) and bulk/single unmonitor cascades to albums."""

import pytest

from tests.test_lidarr_library import (  # noqa: F401
    ALBUMS,
    ARTISTS,
    Lidarr,
    _fresh_cache,
    _headers,
    admin_h,
    api,
    lidarr,
    test_config,
    test_db,
    users,
)


def _stat(artist_id, **stats):
    for a in ARTISTS:
        if a["id"] == artist_id:
            return {**a, **stats}


def _set_artists(lid, rows):
    lid.artists = rows


def test_stats_use_track_count_not_total_track_count(api, admin_h, lidarr):
    _set_artists(
        lidarr,
        [
            {**ARTISTS[0], "monitored": True, "status": "continuing",
             "statistics": {"albumCount": 2, "trackCount": 10, "totalTrackCount": 400, "trackFileCount": 8, "sizeOnDisk": 1000}},
            {**ARTISTS[1], "monitored": False, "status": "ended",
             "statistics": {"albumCount": 1, "trackCount": 5, "totalTrackCount": 50, "trackFileCount": 5, "sizeOnDisk": 500}},
            {**ARTISTS[2], "monitored": True, "status": "continuing",
             "statistics": {"albumCount": 0, "trackCount": 0, "totalTrackCount": 7, "trackFileCount": 0, "sizeOnDisk": 0}},
        ],
    )
    r = api.get("/api/library/stats", headers=admin_h)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["source"] == "lidarr"
    assert s["artist_count"] == 3
    assert (s["monitored_artist_count"], s["unmonitored_artist_count"]) == (2, 1)
    assert (s["continuing_artist_count"], s["ended_artist_count"]) == (2, 1)
    assert s["album_count"] == 3
    assert s["track_count"] == 15  # trackCount, not the 457 totalTrackCount sum
    assert s["total_track_count"] == 457
    assert s["track_file_count"] == 13 and s["file_count"] == 13
    assert s["missing_track_count"] == 2
    assert s["total_size_bytes"] == 1500


def test_artist_record_track_count_is_monitored_release_count(api, admin_h, lidarr):
    _set_artists(lidarr, [{**ARTISTS[0], "statistics": {"trackCount": 12, "totalTrackCount": 99, "trackFileCount": 3}}])
    rec = api.get("/api/library/artists/paged", headers=admin_h).json()["records"][0]
    assert rec["track_count"] == 12 and rec["total_track_count"] == 99 and rec["track_file_count"] == 3


def _album_puts(lid):
    return [body for url, body in lid.puts if url.endswith("/api/v1/album/monitor")]


def test_bulk_unmonitor_with_flag_cascades_to_albums(api, admin_h, lidarr):
    ids = [1, 2]
    expected = sorted(a["id"] for a in lidarr.albums if a["artistId"] in ids)
    assert expected
    r = api.post(
        "/api/library/artists/bulk-edit",
        json={"artist_ids": ["1", "2"], "monitored": False, "apply_monitor_to_albums": True},
        headers=admin_h,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"artists_updated": 2, "albums_monitored": 0, "albums_unmonitored": len(expected)}
    assert any(u.endswith("/api/v1/artist/editor") and b["monitored"] is False for u, b in lidarr.puts)
    puts = _album_puts(lidarr)
    assert sorted(i for b in puts for i in b["albumIds"]) == expected
    assert all(b["monitored"] is False for b in puts)


def test_bulk_unmonitor_cascades_by_default_and_flag_false_opts_out(api, admin_h, lidarr):
    api.post("/api/library/artists/bulk-edit", json={"artist_ids": ["1"], "monitored": False}, headers=admin_h)
    assert _album_puts(lidarr)
    lidarr.puts.clear()
    r = api.post(
        "/api/library/artists/bulk-edit",
        json={"artist_ids": ["1"], "monitored": False, "apply_monitor_to_albums": False},
        headers=admin_h,
    )
    assert r.status_code == 200
    assert _album_puts(lidarr) == []


def test_bulk_unmonitor_all_batches_album_calls(api, admin_h, lidarr, monkeypatch):
    monkeypatch.setattr("plex_playlist_sync.lidarr_library._ALBUM_BATCH", 5)
    r = api.post("/api/library/artists/bulk-edit", json={"all": True, "monitored": False}, headers=admin_h)
    assert r.status_code == 200, r.text
    puts = _album_puts(lidarr)
    assert len(puts) == -(-len(ALBUMS) // 5)
    assert all(len(b["albumIds"]) <= 5 for b in puts)
    assert sorted(i for b in puts for i in b["albumIds"]) == sorted(a["id"] for a in ALBUMS)


def test_single_artist_unmonitor_cascades_to_albums(api, admin_h, lidarr):
    r = api.put("/api/library/artists/1/monitored", json={"monitored": False}, headers=admin_h)
    assert r.status_code == 200, r.text
    ids = sorted(i for b in _album_puts(lidarr) for i in b["albumIds"])
    assert ids == sorted(a["id"] for a in ALBUMS if a["artistId"] == 1)


def test_bulk_album_unmonitor_is_one_album_monitor_call(api, admin_h, lidarr):
    r = api.post("/api/library/albums/bulk-edit", json={"album_ids": ["100", "101"], "monitored": False}, headers=admin_h)
    assert r.status_code == 200 and r.json() == {"albums_updated": 2}
    assert _album_puts(lidarr) == [{"albumIds": [100, 101], "monitored": False}]


def test_track_bulk_edit_is_409_in_lidarr_mode(api, admin_h, lidarr):
    r = api.post("/api/library/tracks/bulk-edit", json={"track_ids": ["900"], "monitored": False}, headers=admin_h)
    assert r.status_code == 409
    assert _album_puts(lidarr) == []


def test_bulk_monitor_does_not_remonitor_albums_unless_explicit(api, admin_h, lidarr):
    r = api.post("/api/library/artists/bulk-edit", json={"artist_ids": ["1"], "monitored": True}, headers=admin_h)
    assert r.status_code == 200, r.text
    assert any(u.endswith("/api/v1/artist/editor") and b["monitored"] is True for u, b in lidarr.puts)
    assert _album_puts(lidarr) == []
    lidarr.puts.clear()
    r = api.post(
        "/api/library/artists/bulk-edit",
        json={"artist_ids": ["1"], "monitored": True, "apply_monitor_to_albums": True},
        headers=admin_h,
    )
    assert r.status_code == 200, r.text
    assert _album_puts(lidarr) and all(b["monitored"] is True for b in _album_puts(lidarr))

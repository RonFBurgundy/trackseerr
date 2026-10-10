"""Contract tests: "Import from Lidarr" (``LidarrMigrationJob``) against a REAL Lidarr (see docs/INTEGRATION_TESTS.md).

The import once made unfiltered ``GET /track`` / ``GET /trackfile`` calls (real Lidarr answers 400) and one unfiltered
``GET /album`` (timed out on a big library); only fakes were tested, so nobody noticed. These tests run the whole job.
"""

import pytest

from trackseerr.clients.lidarr import LidarrClient
from trackseerr.lidarr_migration import LidarrMigrationJob
from trackseerr.storage import Database
from tests.integration.conftest import make_client

pytestmark = pytest.mark.integration


def _lidarr_mode_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "it.db"))
    db.update_media_management_settings({"library_mode": "lidarr"})
    assert db.get_media_management_settings()["library_mode"] == "lidarr"
    return db


def test_unfiltered_track_and_trackfile_are_rejected(lidarr_admin, settled_artist):
    for path in ("track", "trackfile"):
        resp = lidarr_admin._http.get(lidarr_admin.url(path))
        assert resp.status_code == 400, f"GET {path} without a filter: {resp.status_code} {resp.text[:200]}"


def test_strict_fetchers_read_one_artist(client: LidarrClient, settled_artist):
    artist_id = settled_artist["id"]
    albums = client.fetch_artist_albums(artist_id)
    assert albums
    assert all(a["artistId"] == artist_id for a in albums)
    album_id = albums[0]["id"]
    tracks = client.fetch_album_tracks(album_id)
    assert tracks
    assert all(t["albumId"] == album_id for t in tracks)
    files = client.fetch_artist_track_files(artist_id)
    assert isinstance(files, list)
    assert files == []  # this stack has no media files


def test_import_end_to_end_populates_wanted(client: LidarrClient, lidarr_admin, settled_artist, tmp_path):
    artist_id = settled_artist["id"]
    try:
        target = lidarr_admin.albums(artist_id)[0]
        lidarr_admin.put("album/monitor", {"albumIds": [target["id"]], "monitored": True})
        artist = lidarr_admin.get(f"artist/{artist_id}")
        if not artist.get("monitored"):
            artist["monitored"] = True
            lidarr_admin.put(f"artist/{artist_id}", artist)
        assert lidarr_admin.monitored_album_ids(artist_id) == [target["id"]]

        albums = lidarr_admin.albums(artist_id)
        expected_tracks = {a["id"]: lidarr_admin.get(f"track?albumId={a['id']}") for a in albums}
        monitored_titles = sorted(t["title"] for t in expected_tracks[target["id"]])
        assert monitored_titles

        db = _lidarr_mode_db(tmp_path)
        res = LidarrMigrationJob().run_migration(db, client, auto_switch_mode=True)

        assert res["status"] == "completed", res
        assert res["artists_migrated"] == 1
        assert res["albums_migrated"] == len(albums)
        assert res["tracks_migrated"] == sum(len(t) for t in expected_tracks.values())
        assert res["files_migrated"] == 0
        assert db.get_media_management_settings()["library_mode"] == "native"

        rows, total = db.list_wanted("missing", 1, 500, "artist", "asc")
        assert total == len(rows) == len(monitored_titles)
        assert sorted(r["title"] for r in rows) == monitored_titles
        assert {r["album"] for r in rows} == {target["title"]}
    finally:
        lidarr_admin.unmonitor_all(artist_id)


def test_import_fails_safely_when_lidarr_is_unreachable(tmp_path, lidarr_target):
    dead = make_client("http://127.0.0.1:1", lidarr_target[1])
    db = _lidarr_mode_db(tmp_path)
    res = LidarrMigrationJob().run_migration(db, dead, auto_switch_mode=True)
    assert res["status"] == "failed"
    assert res["error"]
    assert db.get_media_management_settings()["library_mode"] == "lidarr"

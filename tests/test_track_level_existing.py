"""Track-level "existing" monitoring, on-demand album track hydration and the new-install defaults."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plex_playlist_sync.api.dependencies import get_discovery_client, get_mbid_enricher
from plex_playlist_sync.artist_refresh import refresh_single_artist
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.mediacover import mediacover_service
from plex_playlist_sync.storage import SCHEMA_VERSION, Database

from tests.test_library_api import (  # noqa: F401
    _auth_headers,
    app_and_client,
    seeded_users,
    test_config,
    test_db,
)


def _artist(db: Database, option: str = "existing", monitored: bool = True, aid: str = "ar") -> None:
    db.upsert_library_artist(
        {"id": aid, "name": aid, "monitored": monitored, "monitor_option": option, "mbid": "mb-" + aid}
    )


def _album(db: Database, album_id: str, n_tracks: int, with_files: tuple[int, ...] = (), aid: str = "ar") -> None:
    """An album whose tracks all start monitored (as after an 'all' era); ``with_files`` are 1-based indexes."""
    db.upsert_library_album(
        {"id": album_id, "artist_id": aid, "title": album_id, "monitored": True, "mb_release_group_id": "rg-" + album_id}
    )
    for i in range(1, n_tracks + 1):
        db.upsert_library_track(
            {"id": f"{album_id}-t{i}", "album_id": album_id, "artist_id": aid, "title": f"T{i}",
             "track_number": i, "monitored": True}
        )
        if i in with_files:
            _file(db, f"{album_id}-t{i}")


def _file(db: Database, track_id: str) -> None:
    db.upsert_library_file(
        {"id": f"f-{track_id}", "track_id": track_id, "file_path": f"/m/{track_id}.flac",
         "relative_path": f"{track_id}.flac", "codec": "FLAC", "quality_name": "FLAC", "size_bytes": 1}
    )


def _t(db: Database, track_id: str) -> bool:
    return bool(db.get_library_track(track_id)["monitored"])


def _a(db: Database, album_id: str) -> bool:
    return bool(db.get_library_album(album_id)["monitored"])


# ------------------------------------------------------------ recompute (single + bulk) and wanted counts

def test_recompute_monitors_exactly_tracks_with_files(test_db: Database):
    _artist(test_db)
    _album(test_db, "partial", 12, with_files=(1, 5, 9))
    _album(test_db, "empty", 4)
    test_db.bulk_edit_library_artists(["ar"], monitor_option="existing", apply_monitor_to_albums=True)

    assert _a(test_db, "partial") is True
    assert _a(test_db, "empty") is False
    assert {i for i in range(1, 13) if _t(test_db, f"partial-t{i}")} == {1, 5, 9}
    assert not any(_t(test_db, f"empty-t{i}") for i in range(1, 5))


def test_wanted_and_stats_only_count_monitored_tracks(test_db: Database):
    _artist(test_db)
    _album(test_db, "partial", 12, with_files=(1, 2, 3))
    _album(test_db, "empty", 5)
    test_db.bulk_edit_library_artists(["ar"], monitor_option="existing", apply_monitor_to_albums=True)

    wanted, total = test_db.list_wanted("missing", 1, 50, "title", "asc")
    assert total == 0 and wanted == []
    assert test_db.get_library_stats()["missing_track_count"] == 0
    assert test_db.get_monitored_missing_catalog_tracks() == []

    # The user explicitly monitors one more track: only that one is missing.
    test_db.set_track_monitored("partial-t7", True)
    wanted, total = test_db.list_wanted("missing", 1, 50, "title", "asc")
    assert total == 1 and wanted[0]["track_id"] == "partial-t7"
    assert test_db.get_library_stats()["missing_track_count"] == 1
    assert [t["track_id"] for t in test_db.get_monitored_missing_catalog_tracks()] == ["partial-t7"]


def test_other_options_still_follow_album(test_db: Database):
    _artist(test_db, option="all")
    _album(test_db, "partial", 4, with_files=(1,))
    test_db.bulk_edit_library_artists(["ar"], monitor_option="all", apply_monitor_to_albums=True)
    assert all(_t(test_db, f"partial-t{i}") for i in range(1, 5))


def test_bulk_endpoint_existing_recomputes_without_explicit_flag(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _artist(test_db, option="all", aid="a1")
    _artist(test_db, option="all", aid="a2")
    _album(test_db, "x", 6, with_files=(2,), aid="a1")
    _album(test_db, "y", 3, with_files=(), aid="a2")

    r = client.post("/api/library/artists/bulk-edit", json={"artist_ids": ["a1", "a2"], "monitor_option": "existing"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["albums_monitored"] == 1 and r.json()["albums_unmonitored"] == 1
    assert [i for i in range(1, 7) if _t(test_db, f"x-t{i}")] == [2]
    assert not _a(test_db, "y")

    # single edit goes through the same recompute
    _artist(test_db, option="all", aid="a3")
    _album(test_db, "z", 3, with_files=(3,), aid="a3")
    r = client.put("/api/library/artists/a3/monitored", json={"monitored": True, "monitor_option": "existing"}, headers=h)
    assert r.status_code == 200, r.text
    assert [i for i in range(1, 4) if _t(test_db, f"z-t{i}")] == [3]


def test_explicit_apply_false_leaves_children_alone(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _artist(test_db, option="all")
    _album(test_db, "x", 3, with_files=(1,))
    r = client.post(
        "/api/library/artists/bulk-edit",
        json={"artist_ids": ["ar"], "monitor_option": "existing", "apply_monitor_to_albums": False},
        headers=h,
    )
    assert r.status_code == 200
    assert test_db.get_library_artist("ar")["monitor_option"] == "existing"
    assert all(_t(test_db, f"x-t{i}") for i in range(1, 4))


# ------------------------------------------------------------ new files monitor their track

def test_new_file_monitors_track_and_album_for_existing_artist(test_db: Database):
    _artist(test_db)
    _album(test_db, "alb", 3)
    test_db.bulk_edit_library_artists(["ar"], monitor_option="existing", apply_monitor_to_albums=True)
    assert not _a(test_db, "alb") and not _t(test_db, "alb-t2")

    _file(test_db, "alb-t2")  # a scan / import links a new file
    assert _a(test_db, "alb") is True
    assert _t(test_db, "alb-t2") is True
    assert _t(test_db, "alb-t1") is False and _t(test_db, "alb-t3") is False


def test_new_file_via_batch_monitors_track(test_db: Database):
    _artist(test_db)
    _album(test_db, "alb", 2)
    test_db.bulk_edit_library_artists(["ar"], monitor_option="existing", apply_monitor_to_albums=True)
    test_db.upsert_library_files_batch(
        [{"id": "fb", "track_id": "alb-t1", "file_path": "/m/b.flac", "relative_path": "b.flac", "codec": "FLAC",
          "quality_name": "FLAC", "size_bytes": 1}]
    )
    assert _t(test_db, "alb-t1") and _a(test_db, "alb") and not _t(test_db, "alb-t2")


def test_new_file_does_not_monitor_for_other_options_or_unmonitored_artists(test_db: Database):
    _artist(test_db, option="albums", aid="o1")
    _artist(test_db, option="existing", monitored=False, aid="o2")
    for aid in ("o1", "o2"):
        _album(test_db, f"{aid}-alb", 1, aid=aid)
        test_db.set_track_monitored(f"{aid}-alb-t1", False)
        test_db.set_album_monitored(f"{aid}-alb", False, cascade_tracks=False)
        _file(test_db, f"{aid}-alb-t1")
        assert not _t(test_db, f"{aid}-alb-t1") and not _a(test_db, f"{aid}-alb")


def test_rescan_of_known_file_does_not_remonitor_user_unmonitored_track(test_db: Database):
    _artist(test_db)
    _album(test_db, "alb", 2, with_files=(1,))
    test_db.bulk_edit_library_artists(["ar"], monitor_option="existing", apply_monitor_to_albums=True)
    test_db.set_track_monitored("alb-t1", False)  # explicit user choice
    _file(test_db, "alb-t1")  # scanner re-upserts the same file/track link
    test_db.upsert_library_files_batch(
        [{"id": "f-alb-t1", "track_id": "alb-t1", "file_path": "/m/alb-t1.flac", "relative_path": "x", "codec": "FLAC",
          "quality_name": "FLAC", "size_bytes": 2}]
    )
    assert _t(test_db, "alb-t1") is False


# ------------------------------------------------------------ refresh keeps explicit choices

def test_refresh_does_not_override_user_track_monitoring(test_db: Database):
    _artist(test_db)
    _album(test_db, "alb", 3, with_files=(1, 2))
    test_db.bulk_edit_library_artists(["ar"], monitor_option="existing", apply_monitor_to_albums=True)
    test_db.set_track_monitored("alb-t2", False)  # owned but explicitly unmonitored
    test_db.set_track_monitored("alb-t3", True)  # not owned but explicitly wanted

    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_artist_details.return_value = {"id": "mb-ar"}
    enricher.get_artist_discography.return_value = [{"id": "rg-alb", "title": "alb", "album_type": "album", "year": 2000}]
    enricher.get_artist_discography_result.return_value = (enricher.get_artist_discography.return_value, True)
    enricher.get_release_group_tracks.return_value = [
        {"track_number": i, "title": f"T{i}", "disc_number": 1, "duration_seconds": 1.0, "mb_recording_id": None}
        for i in (1, 2, 3, 4)
    ]
    with patch.object(mediacover_service, "ensure_artwork", return_value=Path("/tmp/c.jpg")):
        assert refresh_single_artist(artist_id="ar", db=test_db, enricher=enricher, discovery_client=MagicMock())["success"]
    assert _t(test_db, "alb-t1") is True
    assert _t(test_db, "alb-t2") is False
    assert _t(test_db, "alb-t3") is True
    # the track the refresh newly discovered is not owned, so it stays unmonitored
    new = test_db.get_library_track_by_title("alb", "T4", track_number=4)
    assert new is not None and not new["monitored"]


# ------------------------------------------------------------ add (ingest)

def test_ingest_under_existing_stores_albums_unmonitored_and_fetches_no_tracks(
    app_and_client, test_db, test_config, seeded_users
):
    app, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    disc = MagicMock()
    disc.get_artist_details.return_value = {
        "albums": [{"id": "dz:1", "title": "One", "year": 2001}, {"id": "dz:2", "title": "Two", "year": 2002}],
    }
    app.dependency_overrides[get_discovery_client] = lambda: disc
    r = client.post(
        "/api/library/artists/ingest",
        json={"foreign_artist_id": "dz:artist:9", "artist_name": "Newband", "monitor_option": "existing"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["albums_ingested"] == 2 and r.json()["tracks_ingested"] == 0
    disc.get_album_details.assert_not_called()
    artist = test_db.get_library_artist_by_name("Newband")
    assert artist["monitor_option"] == "existing"
    assert not any(a["monitored"] for a in test_db.list_library_albums(artist_id=artist["id"]))


def test_ingest_defaults_to_existing_on_fresh_db(app_and_client, test_db, test_config, seeded_users):
    app, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    disc = MagicMock()
    disc.get_artist_details.return_value = {}
    app.dependency_overrides[get_discovery_client] = lambda: disc
    r = client.post("/api/library/artists/ingest", json={"foreign_artist_id": "dz:artist:5", "artist_name": "Def"}, headers=h)
    assert r.status_code == 200, r.text
    assert test_db.get_library_artist_by_name("Def")["monitor_option"] == "existing"


# ------------------------------------------------------------ defaults and migration

def test_fresh_db_defaults_to_existing(tmp_path: Path):
    db = Database(str(tmp_path / "fresh.db"))
    try:
        assert db.get_media_management_settings()["add_monitor_option"] == "existing"
        assert db.get_media_management_settings()["scan_monitor_option"] == "existing"
        db.upsert_library_artist({"id": "n", "name": "n"})
        assert db.get_library_artist("n")["monitor_option"] == "existing"
    finally:
        db.close()


def test_existing_install_keeps_saved_add_option_and_artist_options(tmp_path: Path):
    path = str(tmp_path / "old.db")
    db = Database(path)
    db.upsert_library_artist({"id": "old", "name": "old", "monitor_option": "all"})
    db.update_media_management_settings({"add_monitor_option": "all"})
    # Pretend this install predates the migration.
    db.conn.execute("DELETE FROM schema_migrations WHERE version = ?", (SCHEMA_VERSION,))
    db.conn.commit()
    db.close()

    db = Database(path)
    try:
        assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
        assert db.get_media_management_settings()["add_monitor_option"] == "all"
        assert db.get_library_artist("old")["monitor_option"] == "all"
    finally:
        db.close()


# ------------------------------------------------------------ on-demand hydration

class _Resp:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


MB_RELEASE = {
    "releases": [
        {
            "media": [
                {"position": 1, "tracks": [
                    {"position": 1, "title": "Opener", "length": 180000, "recording": {"id": "rec-1"}},
                    {"position": 2, "title": "Second", "length": 200000, "recording": {"id": "rec-2"}},
                ]}
            ]
        }
    ]
}


@pytest.fixture
def mb_enricher():
    """A real enricher whose HTTP session is mocked; cache disabled so only the DB guard prevents a refetch."""
    enricher = MbidEnricherClient(base_url="https://mb.test", cache_ttl=0, min_interval=0)
    enricher._session = MagicMock()
    enricher._session.get.return_value = _Resp(MB_RELEASE)
    return enricher


def _bare_album(db: Database, monitored: bool = False, option: str = "existing") -> None:
    _artist(db, option=option)
    db.upsert_library_album(
        {"id": "alb", "artist_id": "ar", "title": "Alb", "monitored": monitored, "mb_release_group_id": "rg-alb"}
    )


def test_opening_album_hydrates_once_and_stores_unmonitored(app_and_client, test_db, test_config, seeded_users, mb_enricher):
    app, client = app_and_client
    app.dependency_overrides[get_mbid_enricher] = lambda: mb_enricher
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _bare_album(test_db)

    r = client.get("/api/library/tracks/paged", params={"album_id": "alb"}, headers=h)
    assert r.status_code == 200, r.text
    recs = r.json()["records"]
    assert [t["title"] for t in recs] == ["Opener", "Second"] or {t["title"] for t in recs} == {"Opener", "Second"}
    assert r.json()["total"] == 2
    assert all(t["monitored"] is False for t in recs)
    assert mb_enricher._session.get.call_count == 1
    assert {t["mb_recording_id"] for t in test_db.list_library_tracks(album_id="alb")} == {"rec-1", "rec-2"}

    r2 = client.get("/api/library/tracks/paged", params={"album_id": "alb"}, headers=h)
    assert r2.json()["total"] == 2
    assert mb_enricher._session.get.call_count == 1  # second open does not refetch
    assert len(test_db.list_library_tracks(album_id="alb")) == 2  # and creates no duplicates


def test_hydration_noop_without_release_group_or_when_mb_empty(app_and_client, test_db, test_config, seeded_users, mb_enricher):
    app, client = app_and_client
    app.dependency_overrides[get_mbid_enricher] = lambda: mb_enricher
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _artist(test_db)
    test_db.upsert_library_album({"id": "norg", "artist_id": "ar", "title": "No RG", "monitored": False})
    r = client.get("/api/library/tracks/paged", params={"album_id": "norg"}, headers=h)
    assert r.status_code == 200 and r.json()["total"] == 0
    mb_enricher._session.get.assert_not_called()

    _bare_album(test_db)
    mb_enricher._session.get.return_value = _Resp({"releases": []})
    r = client.get("/api/library/tracks/paged", params={"album_id": "alb"}, headers=h)
    assert r.status_code == 200 and r.json()["total"] == 0


def test_monitoring_an_album_with_no_tracks_hydrates_and_monitors_them(
    app_and_client, test_db, test_config, seeded_users, mb_enricher
):
    app, client = app_and_client
    app.dependency_overrides[get_mbid_enricher] = lambda: mb_enricher
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _bare_album(test_db)
    r = client.put("/api/library/albums/alb/monitored", json={"monitored": True}, headers=h)
    assert r.status_code == 200, r.text
    tracks = test_db.list_library_tracks(album_id="alb")
    assert len(tracks) == 2 and all(t["monitored"] for t in tracks)
    assert mb_enricher._session.get.call_count == 1


def test_bulk_monitor_albums_hydrates_empty_ones(app_and_client, test_db, test_config, seeded_users, mb_enricher):
    app, client = app_and_client
    app.dependency_overrides[get_mbid_enricher] = lambda: mb_enricher
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _bare_album(test_db)
    r = client.post("/api/library/albums/bulk-edit", json={"album_ids": ["alb"], "monitored": True}, headers=h)
    assert r.status_code == 200, r.text
    assert len(test_db.list_library_tracks(album_id="alb")) == 2
    assert all(t["monitored"] for t in test_db.list_library_tracks(album_id="alb"))

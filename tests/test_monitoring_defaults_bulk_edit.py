"""Monitor options (all/albums/singles_eps/existing/future/none), scan/add defaults and bulk-edit endpoints."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plex_playlist_sync.api.routes.library import refresh_single_artist
from plex_playlist_sync.clients.mbid_enricher import MbidEnricherClient
from plex_playlist_sync.library_monitoring import (
    NATIVE_MONITOR_OPTIONS,
    album_monitored_for_option,
)
from plex_playlist_sync.library_scanner import LibraryScanner
from plex_playlist_sync.mediacover import mediacover_service
from plex_playlist_sync.models import LibraryArtist
from plex_playlist_sync.storage import Database

# Reuse the authenticated TestClient fixtures of the library API tests.
from tests.test_library_api import (  # noqa: F401
    _auth_headers,
    app_and_client,
    seeded_users,
    test_config,
    test_db,
)

ADDED = "2024-06-15 10:00:00"


@pytest.fixture(autouse=True)
def _no_background_hydration():
    """The scanner launches a background refresh (network) for new artists; keep these tests offline."""
    with patch("plex_playlist_sync.artist_refresh_worker.artist_refresh_worker.refresh_once"):
        yield


def _helper(option: str, **kw: Any) -> bool:
    base: dict[str, Any] = dict(
        artist_monitored=True, album_type="album", has_files=False, release_date=None, year=None, artist_added_at=ADDED
    )
    base.update(kw)
    return album_monitored_for_option(option, **base)


class TestHelper:
    def test_options_constant(self):
        assert NATIVE_MONITOR_OPTIONS == ("all", "albums", "singles_eps", "existing", "future", "none")

    @pytest.mark.parametrize("option", NATIVE_MONITOR_OPTIONS)
    def test_unmonitored_artist_is_always_false(self, option):
        assert _helper(option, artist_monitored=False, has_files=True, release_date="2030-01-01") is False

    def test_all(self):
        assert _helper("all", album_type="compilation") is True

    def test_albums(self):
        assert _helper("albums", album_type="album") is True
        assert _helper("albums", album_type="Studio") is True
        assert _helper("albums", album_type="single") is False
        assert _helper("albums", album_type="ep") is False
        assert _helper("albums", album_type=None) is True

    def test_singles_eps(self):
        assert _helper("singles_eps", album_type="single") is True
        assert _helper("singles_eps", album_type="EP") is True
        assert _helper("singles_eps", album_type="album") is False

    def test_existing(self):
        assert _helper("existing", has_files=True) is True
        assert _helper("existing", has_files=False) is False

    def test_future(self):
        assert _helper("future", release_date="2024-06-16") is True
        assert _helper("future", release_date="2024-06-15") is False  # strictly after
        assert _helper("future", release_date="2020-01-01") is False
        assert _helper("future", release_date="2025") is True
        assert _helper("future", release_date="2024") is False  # year-only pads to Jan 1
        assert _helper("future", year=2025) is True
        assert _helper("future", year=2023) is False
        assert _helper("future", release_date=None, year=None) is False  # unknown date
        assert _helper("future", release_date="garbage") is False
        assert _helper("future", release_date="2030-01-01", artist_added_at=None) is False

    def test_none_and_unknown(self):
        assert _helper("none") is False
        assert _helper("bogus") is False


def _seed_artist(db: Database, artist_id: str, option: str = "all", monitored: bool = True, created_at: str = ADDED):
    db.upsert_library_artist(
        {"id": artist_id, "name": artist_id, "monitored": monitored, "monitor_option": option, "created_at": created_at}
    )


def _seed_album(db: Database, album_id: str, artist_id: str, *, with_file: bool, **kw: Any):
    db.upsert_library_album({"id": album_id, "artist_id": artist_id, "title": album_id, "monitored": True, **kw})
    db.upsert_library_track(
        {"id": f"{album_id}-t1", "album_id": album_id, "artist_id": artist_id, "title": "t", "monitored": True}
    )
    if with_file:
        db.upsert_library_file(
            {
                "id": f"{album_id}-f1",
                "track_id": f"{album_id}-t1",
                "file_path": f"/m/{album_id}.flac",
                "relative_path": f"{album_id}.flac",
                "codec": "FLAC",
                "quality_name": "FLAC",
                "size_bytes": 1,
            }
        )


# ---------------------------------------------------------------- refresh

def test_refresh_new_albums_unmonitored_under_existing_but_existing_albums_keep_flag(test_db: Database):
    _seed_artist(test_db, "art-r", option="existing")
    test_db.upsert_library_artist(
        LibraryArtist(id="art-r", name="Band", mbid="mbid-band", monitored=True, monitor_option="existing")
    )
    test_db.upsert_library_album(
        {"id": "alb-have", "artist_id": "art-r", "title": "Owned", "mb_release_group_id": "rg-owned", "monitored": True}
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_artist_details.return_value = {"id": "mbid-band"}
    enricher.get_artist_discography.return_value = [
        {"id": "rg-owned", "title": "Owned", "album_type": "album", "year": 2000},
        {"id": "rg-new1", "title": "New One", "album_type": "album", "year": 2001},
        {"id": "rg-new2", "title": "New Single", "album_type": "single", "year": 2002},
    ]
    enricher.get_release_group_tracks.return_value = []
    with patch.object(mediacover_service, "ensure_artwork", return_value=Path("/tmp/c.jpg")):
        assert refresh_single_artist(artist_id="art-r", db=test_db, enricher=enricher)["success"] is True
    by_title = {a["title"]: a["monitored"] for a in test_db.list_library_albums(artist_id="art-r")}
    assert by_title == {"Owned": True, "New One": False, "New Single": False}


def test_refresh_new_albums_follow_all_option(test_db: Database):
    test_db.upsert_library_artist(
        LibraryArtist(id="art-a", name="Band2", mbid="mbid-b2", monitored=True, monitor_option="all")
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_artist_details.return_value = {"id": "mbid-b2"}
    enricher.get_artist_discography.return_value = [{"id": "rg-x", "title": "X", "album_type": "album", "year": 2001}]
    enricher.get_release_group_tracks.return_value = []
    with patch.object(mediacover_service, "ensure_artwork", return_value=Path("/tmp/c.jpg")):
        refresh_single_artist(artist_id="art-a", db=test_db, enricher=enricher)
    assert [a["monitored"] for a in test_db.list_library_albums(artist_id="art-a")] == [True]


# ---------------------------------------------------------------- scanner

def _scan_one(db: Database, root: Path, artist: str = "Band", album: str = "LP", total_tracks: int = 1) -> None:
    f = root / artist / album / "01 - Song.flac"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")
    meta = {
        "title": "Song", "artist": artist, "album": album, "track_number": 1, "disc_number": 1, "year": 2000,
        "total_tracks": total_tracks, "duration": 10.0, "codec": "FLAC", "bitrate": 1, "sample_rate": 44100,
        "bits_per_sample": 16, "quality_full": "FLAC 16bit 44.1kHz", "file_path": str(f.resolve()),
    }
    with patch("plex_playlist_sync.library_scanner.inspect_audio_file", return_value=meta):
        LibraryScanner().scan(db, root_folder=str(root))


def test_scan_new_artist_gets_scan_default_and_rescan_keeps_user_choice(test_db: Database, tmp_path: Path):
    root = tmp_path / "music"
    _scan_one(test_db, root)
    artist = test_db.get_library_artist_by_name("Band")
    assert artist["monitor_option"] == "existing"  # default scan_monitor_option
    album = test_db.get_library_album_by_title(artist["id"], "LP")
    assert album["monitored"] is True  # real files stay monitored

    # user changes everything; a rescan that touches the rows (new mbid) must not revert it
    test_db.bulk_edit_library_artists([artist["id"]], monitored=False, monitor_option="none")
    test_db.bulk_set_albums_monitored([album["id"]], False)
    meta_mbid = {"musicbrainz_artistid": "mb-1", "musicbrainz_releasegroupid": "rg-1"}
    f = root / "Band" / "LP" / "01 - Song.flac"
    meta = {
        "title": "Song", "artist": "Band", "album": "LP", "track_number": 1, "disc_number": 1, "year": 2000,
        "total_tracks": 1, "duration": 10.0, "codec": "FLAC", "bitrate": 1, "sample_rate": 44100,
        "bits_per_sample": 16, "quality_full": "FLAC 16bit 44.1kHz", "file_path": str(f.resolve()), **meta_mbid,
    }
    f.write_bytes(b"changed so the scanner re-reads the file")
    with patch("plex_playlist_sync.library_scanner.inspect_audio_file", return_value=meta):
        LibraryScanner().scan(test_db, root_folder=str(root))
    artist2 = test_db.get_library_artist(artist["id"])
    assert artist2["mbid"] == "mb-1"  # the rescan did update the row ...
    assert artist2["monitored"] is False and artist2["monitor_option"] == "none"  # ... but not the user's choice
    album2 = test_db.get_library_album(album["id"])
    assert album2["mb_release_group_id"] == "rg-1" and album2["monitored"] is False


def test_scan_uses_configured_scan_option(test_db: Database, tmp_path: Path):
    test_db.update_media_management_settings({"scan_monitor_option": "future"})
    _scan_one(test_db, tmp_path / "music", artist="Other")
    assert test_db.get_library_artist_by_name("Other")["monitor_option"] == "future"


@pytest.mark.parametrize(
    "option, album_flag, single_flag",
    [
        ("existing", True, True),
        ("all", True, True),
        ("none", False, False),
        ("albums", True, False),
        ("singles_eps", False, True),
        ("future", False, False),  # files dated 2000, artist added now
    ],
)
def test_scan_applies_option_to_new_albums_and_tracks(
    test_db: Database, tmp_path: Path, option: str, album_flag: bool, single_flag: bool
):
    test_db.update_media_management_settings({"scan_monitor_option": option})
    root = tmp_path / "music"
    _scan_one(test_db, root, artist="Band", album="Full LP", total_tracks=10)
    _scan_one(test_db, root, artist="Band", album="Lone Single", total_tracks=1)
    artist = test_db.get_library_artist_by_name("Band")
    assert artist["monitor_option"] == option
    for title, expected in (("Full LP", album_flag), ("Lone Single", single_flag)):
        album = test_db.get_library_album_by_title(artist["id"], title)
        assert album["monitored"] is expected, (option, title)
        track = test_db.get_library_track_by_title(album["id"], "Song", 1)
        assert track["monitored"] is expected, (option, title, "track")


def test_scan_new_album_under_existing_artist_uses_artist_option(test_db: Database, tmp_path: Path):
    root = tmp_path / "music"
    _scan_one(test_db, root, artist="Band", album="First", total_tracks=10)
    artist = test_db.get_library_artist_by_name("Band")
    test_db.bulk_edit_library_artists([artist["id"]], monitor_option="none")
    _scan_one(test_db, root, artist="Band", album="Second", total_tracks=10)
    second = test_db.get_library_album_by_title(artist["id"], "Second")
    assert second["monitored"] is False
    assert test_db.get_library_track_by_title(second["id"], "Song", 1)["monitored"] is False
    assert test_db.get_library_album_by_title(artist["id"], "First")["monitored"] is True  # existing row untouched


# ---------------------------------------------------------------- single-artist route

@pytest.mark.parametrize("option", NATIVE_MONITOR_OPTIONS)
def test_set_artist_monitored_native_every_option(app_and_client, test_db, test_config, seeded_users, option):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _seed_artist(test_db, "S", option="all")
    _seed_album(test_db, "s-own", "S", with_file=True, album_type="album", year=2000)
    _seed_album(test_db, "s-miss", "S", with_file=False, album_type="single", year=2001)
    r = client.put(
        "/api/library/artists/S/monitored", json={"monitored": True, "monitor_option": option}, headers=h
    )
    assert r.status_code == 200, r.text
    artist = test_db.get_library_artist("S")
    assert artist["monitor_option"] == option
    assert artist["monitored"] is (option != "none")
    for alb in test_db.list_library_albums(artist_id="S"):
        expected = album_monitored_for_option(
            option,
            artist_monitored=option != "none",
            album_type=alb["album_type"],
            has_files=alb["id"] == "s-own",
            release_date=alb.get("release_date"),
            year=alb.get("year"),
            artist_added_at=ADDED,
        )
        assert alb["monitored"] is expected, (option, alb["id"])
        assert test_db.get_library_track(f"{alb['id']}-t1")["monitored"] is expected


def test_set_artist_monitored_lidarr_preset_and_unsupported_option(app_and_client, test_db, test_config, seeded_users):
    from plex_playlist_sync.api.dependencies import get_lidarr_client

    app, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    test_db.conn.execute("UPDATE media_management_settings SET library_mode = 'lidarr' WHERE id = 1")
    test_db.conn.commit()
    lidarr = MagicMock()
    lidarr.fetch_artist_albums.return_value = []
    lidarr.set_artist_monitored.return_value = {"id": 7, "artistName": "X", "monitored": True}
    app.dependency_overrides[get_lidarr_client] = lambda: lidarr
    r = client.put("/api/library/artists/7/monitored", json={"monitored": True, "monitor_option": "existing"}, headers=h)
    assert r.status_code == 400, r.text
    assert "existing" in r.json()["detail"]
    lidarr.set_artist_monitored.assert_not_called()
    r = client.put("/api/library/artists/7/monitored", json={"monitored": True, "monitor_option": "all"}, headers=h)
    assert r.status_code == 200, r.text
    lidarr.set_artist_monitored.assert_called_once_with(7, True)


def test_lidarr_bulk_partial_failure_reports_progress(app_and_client, test_db, test_config, seeded_users):
    from plex_playlist_sync.api.dependencies import get_lidarr_client
    from plex_playlist_sync.clients.lidarr import LidarrApiError

    app, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    test_db.conn.execute("UPDATE media_management_settings SET library_mode = 'lidarr' WHERE id = 1")
    test_db.conn.commit()
    lidarr = MagicMock()
    lidarr.fetch_artist_albums.side_effect = [[], [], LidarrApiError("boom")]
    lidarr.set_artist_monitored.return_value = {"id": 1, "artistName": "X", "monitored": True}
    app.dependency_overrides[get_lidarr_client] = lambda: lidarr
    r = client.post(
        "/api/library/artists/bulk-edit", json={"artist_ids": ["1", "2", "3"], "monitor_option": "all"}, headers=h
    )
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert "artist 3" in detail and "2 of 3 artists were updated" in detail


# ---------------------------------------------------------------- SQL vs helper dates

@pytest.mark.parametrize(
    "release_date",
    ["2020-13-45", "2020-3-5", "2020-02-30", "2020-00-10", "0000-05-01", "20200101", "2020-01-01T00:00", "2025-01-02",
     "2025-07", "2025", "garbage", "", None],
)
def test_future_sql_and_helper_agree_on_malformed_dates(test_db: Database, release_date):
    _seed_artist(test_db, "D", option="future", created_at="2024-06-15 10:00:00")
    for year in (None, 2023, 2030):
        alb_id = f"d-{year}"
        _seed_album(test_db, alb_id, "D", with_file=False, release_date=release_date, year=year)
    test_db.bulk_edit_library_artists(["D"], monitor_option="future", apply_monitor_to_albums=True)
    for alb in test_db.list_library_albums(artist_id="D"):
        expected = album_monitored_for_option(
            "future", artist_monitored=True, album_type="album", has_files=False,
            release_date=alb.get("release_date"), year=alb.get("year"), artist_added_at="2024-06-15 10:00:00",
        )
        assert alb["monitored"] is expected, (release_date, alb["year"])


# ---------------------------------------------------------------- settings

def test_settings_defaults_roundtrip_and_validation(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    s = client.get("/api/settings/media-management", headers=h).json()["settings"]
    assert s["scan_monitor_option"] == "existing" and s["add_monitor_option"] == "existing"

    r = client.post(
        "/api/settings/media-management", json={"scan_monitor_option": "future", "add_monitor_option": "albums"}, headers=h
    )
    assert r.status_code == 200
    r = client.put("/api/settings/media-management", json={"add_monitor_option": "singles_eps"}, headers=h)
    assert r.status_code == 200
    s = client.get("/api/settings/media-management", headers=h).json()["settings"]
    assert s["scan_monitor_option"] == "future" and s["add_monitor_option"] == "singles_eps"

    bad = client.post("/api/settings/media-management", json={"scan_monitor_option": "everything"}, headers=h)
    assert bad.status_code == 422
    assert test_db.get_media_management_settings()["scan_monitor_option"] == "future"
    with pytest.raises(ValueError):
        test_db.update_media_management_settings({"add_monitor_option": "nope"})


# ---------------------------------------------------------------- ingest

def test_ingest_defaults_to_add_monitor_option(app_and_client, test_db, test_config, seeded_users):
    from plex_playlist_sync.api.dependencies import get_discovery_client

    app, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    discovery = MagicMock()
    discovery.get_artist_details.return_value = {"albums": [{"id": "d:1", "title": "A", "year": 2000}]}
    app.dependency_overrides[get_discovery_client] = lambda: discovery
    test_db.update_media_management_settings({"add_monitor_option": "none"})
    r = client.post(
        "/api/library/artists/ingest", json={"foreign_artist_id": "deezer:artist:1", "artist_name": "Ing"}, headers=h
    )
    assert r.status_code == 200, r.text
    art = test_db.get_library_artist_by_name("Ing")
    assert art["monitor_option"] == "none"
    assert [a["monitored"] for a in test_db.list_library_albums(artist_id=art["id"])] == [False]
    r = client.post(
        "/api/library/artists/ingest",
        json={"foreign_artist_id": "deezer:artist:2", "artist_name": "Ing2", "monitor_option": "future"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert test_db.get_library_artist_by_name("Ing2")["monitor_option"] == "future"
    assert [a["monitored"] for a in test_db.list_library_albums(artist_id=test_db.get_library_artist_by_name("Ing2")["id"])] == [False]


# ---------------------------------------------------------------- bulk edit

def _seed_bulk(db: Database) -> None:
    _seed_artist(db, "A1")
    _seed_artist(db, "A2")
    _seed_artist(db, "A3")
    _seed_album(db, "a1-own", "A1", with_file=True, album_type="album", year=2000)
    _seed_album(db, "a1-miss", "A1", with_file=False, album_type="album", year=2001)
    _seed_album(db, "a2-own", "A2", with_file=True, album_type="single", year=2000)
    _seed_album(db, "a2-miss", "A2", with_file=False, album_type="ep", year=2002)
    _seed_album(db, "a3-miss", "A3", with_file=False, album_type="album", year=2003)


def _flags(db: Database) -> dict[str, bool]:
    return {a["id"]: a["monitored"] for a in db.list_library_albums()}


def test_bulk_edit_artists_by_ids_existing_applies_to_albums(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _seed_bulk(test_db)
    r = client.post(
        "/api/library/artists/bulk-edit",
        json={"artist_ids": ["A1", "A2"], "monitor_option": "existing", "apply_monitor_to_albums": True},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"artists_updated": 2, "albums_monitored": 2, "albums_unmonitored": 2}
    f = _flags(test_db)
    assert f == {"a1-own": True, "a1-miss": False, "a2-own": True, "a2-miss": False, "a3-miss": True}
    assert test_db.get_library_artist("A1")["monitor_option"] == "existing"
    assert test_db.get_library_artist("A3")["monitor_option"] == "all"
    # tracks follow their album, so unmonitored albums leave Wanted
    assert test_db.get_library_track("a1-miss-t1")["monitored"] is False
    assert test_db.get_library_track("a1-own-t1")["monitored"] is True
    wanted, _ = test_db.list_wanted("missing", 1, 50, "title", "asc")
    assert {w["album_id"] for w in wanted} == {"a3-miss"}
    assert {t["album_id"] for t in test_db.get_monitored_missing_catalog_tracks()} == {"a3-miss"}


def test_bulk_edit_artists_all_with_every_option_matches_helper(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    for option in NATIVE_MONITOR_OPTIONS:
        _seed_bulk(test_db)
        r = client.post(
            "/api/library/artists/bulk-edit",
            json={"all": True, "monitor_option": option, "apply_monitor_to_albums": True},
            headers=h,
        )
        assert r.status_code == 200, r.text
        assert r.json()["artists_updated"] == 3
        for alb in test_db.list_library_albums():
            expected = album_monitored_for_option(
                option,
                artist_monitored=True,
                album_type=alb["album_type"],
                has_files=alb["id"].endswith("-own"),
                release_date=alb.get("release_date"),
                year=alb.get("year"),
                artist_added_at=ADDED,
            )
            assert alb["monitored"] is expected, (option, alb["id"])


def test_bulk_edit_artists_future_uses_artist_created_at(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _seed_artist(test_db, "F", created_at="2024-06-15 10:00:00")
    _seed_album(test_db, "f-old", "F", with_file=False, release_date="2024-06-15")
    _seed_album(test_db, "f-new", "F", with_file=False, release_date="2024-06-16")
    _seed_album(test_db, "f-yr", "F", with_file=False, year=2026)
    _seed_album(test_db, "f-unk", "F", with_file=False)
    r = client.post(
        "/api/library/artists/bulk-edit",
        json={"artist_ids": ["F"], "monitor_option": "future", "apply_monitor_to_albums": True},
        headers=h,
    )
    assert r.status_code == 200
    assert _flags(test_db) == {"f-old": False, "f-new": True, "f-yr": True, "f-unk": False}


def test_bulk_edit_artists_monitored_profile_and_unmonitored_artist(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _seed_bulk(test_db)
    r = client.post(
        "/api/library/artists/bulk-edit",
        json={"artist_ids": ["A1"], "monitored": False, "quality_profile_id": None, "apply_monitor_to_albums": True},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json() == {"artists_updated": 1, "albums_monitored": 0, "albums_unmonitored": 2}
    assert test_db.get_library_artist("A1")["monitored"] is False
    # without apply, albums are untouched
    r = client.post("/api/library/artists/bulk-edit", json={"artist_ids": ["A2"], "monitor_option": "none"}, headers=h)
    assert r.json() == {"artists_updated": 1, "albums_monitored": 0, "albums_unmonitored": 0}
    assert _flags(test_db)["a2-own"] is True


def test_bulk_edit_artists_validation(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    url = "/api/library/artists/bulk-edit"
    assert client.post(url, json={"artist_ids": ["A1"], "all": True, "monitored": True}, headers=h).status_code == 400
    assert client.post(url, json={"monitored": True}, headers=h).status_code == 400
    assert client.post(url, json={"artist_ids": [], "monitored": True}, headers=h).status_code == 400
    assert client.post(url, json={"artist_ids": ["A1"]}, headers=h).status_code == 400
    assert client.post(url, json={"artist_ids": ["A1"], "monitor_option": "bogus"}, headers=h).status_code == 422
    alice = _auth_headers(seeded_users["alice"], test_db, test_config)
    assert client.post(url, json={"all": True, "monitored": True}, headers=alice).status_code == 403


def test_bulk_edit_artists_chunks_large_id_lists(test_db: Database):
    for i in range(1300):
        _seed_artist(test_db, f"bulk-{i}")
    res = test_db.bulk_edit_library_artists([f"bulk-{i}" for i in range(1300)], monitor_option="none")
    assert res["artists_updated"] == 1300
    assert test_db.get_library_artist("bulk-1299")["monitor_option"] == "none"


def test_bulk_edit_albums(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _seed_bulk(test_db)
    r = client.post(
        "/api/library/albums/bulk-edit", json={"album_ids": ["a1-miss", "a3-miss", "nope"], "monitored": False}, headers=h
    )
    assert r.status_code == 200
    assert r.json() == {"albums_updated": 2}
    f = _flags(test_db)
    assert f["a1-miss"] is False and f["a3-miss"] is False and f["a1-own"] is True
    assert test_db.get_library_track("a1-miss-t1")["monitored"] is False
    wanted, _ = test_db.list_wanted("missing", 1, 50, "title", "asc")
    assert {w["album_id"] for w in wanted} == {"a2-miss"}
    assert client.post("/api/library/albums/bulk-edit", json={"album_ids": [], "monitored": True}, headers=h).status_code == 400


def test_bulk_edit_albums_lidarr_mode_uses_single_call(app_and_client, test_db, test_config, seeded_users):
    from plex_playlist_sync.api.dependencies import get_lidarr_client

    app, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    test_db.conn.execute("UPDATE media_management_settings SET library_mode = 'lidarr' WHERE id = 1")
    test_db.conn.commit()
    lidarr = MagicMock()
    app.dependency_overrides[get_lidarr_client] = lambda: lidarr
    r = client.post("/api/library/albums/bulk-edit", json={"album_ids": ["4", "5"], "monitored": True}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"albums_updated": 2}
    lidarr.set_albums_monitored.assert_called_once_with([4, 5], True)
    r = client.post(
        "/api/library/artists/bulk-edit", json={"artist_ids": ["1", "2"], "monitored": False, "quality_profile_id": "3"}, headers=h
    )
    assert r.status_code == 200, r.text
    lidarr.bulk_edit_artists.assert_called_once_with([1, 2], monitored=False, quality_profile_id=3)
    r = client.post("/api/library/artists/bulk-edit", json={"artist_ids": ["1"], "monitor_option": "future"}, headers=h)
    assert r.status_code == 400

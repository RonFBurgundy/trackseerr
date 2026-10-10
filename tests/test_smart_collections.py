"""Tests for smart collections (rule-based library playlists)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests._rm_helpers import auth_headers
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, get_media_client
from trackseerr.api.routes.sync import sync_state
from trackseerr.config import Config
from trackseerr.library_filters import LibraryFacetFilter
from trackseerr.library_manager import MODE_LIDARR
from trackseerr.models import Track
from trackseerr.smart_collections import (
    SmartCollectionError,
    SmartRules,
    count,
    evaluate,
    evaluate_tracks,
    refresh_tracks,
)
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    yield database
    database.close()


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(plex_url="", plex_token="", data_dir=str(tmp_path), media_server="none")


@pytest.fixture
def api(db, config) -> TestClient:
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_media_client] = lambda: None
    return TestClient(app)


def _user(db: Database, uid: str, *, admin: bool = False) -> dict:
    db.upsert_user(uid, uid, f"{uid}@example.com", is_admin=admin)
    return db.get_user(uid)


def _headers(db, config, user) -> dict[str, str]:
    return auth_headers(db, config, user)


@pytest.fixture
def admin_user(db):
    return _user(db, "admin", admin=True)


@pytest.fixture
def regular_user(db):
    return _user(db, "regular", admin=False)


@pytest.fixture
def admin_headers(db, config, admin_user):
    return _headers(db, config, admin_user)


@pytest.fixture
def regular_headers(db, config, regular_user):
    return _headers(db, config, regular_user)


@pytest.fixture
def seeded_db(db):
    """Seeds artists, albums, tracks and library_files."""
    # Artist 1: rock, US, group, 1980
    db.upsert_library_artist(
        {"id": "ar-01", "name": "Rock Band", "genres": "rock", "country": "US", "monitored": True}
    )
    db.conn.execute(
        "UPDATE library_artists SET artist_type = ?, member_count = ?, begin_year = ?, popularity = ? WHERE id = ?",
        ("group", 4, 1980, 500, "ar-01"),
    )
    # Album 1: 1985
    db.upsert_library_album(
        {"id": "al-01", "artist_id": "ar-01", "title": "Album 1", "year": 1985, "genres": "rock"}
    )
    db.conn.execute("UPDATE library_albums SET album_type = ? WHERE id = ?", ("album", "al-01"))
    db.upsert_library_track({"id": "tr-01", "album_id": "al-01", "artist_id": "ar-01", "title": "Track 1"})
    db.upsert_library_track({"id": "tr-02", "album_id": "al-01", "artist_id": "ar-01", "title": "Track 2"})

    # Album 2: 1990
    db.upsert_library_album(
        {"id": "al-02", "artist_id": "ar-01", "title": "Album 2", "year": 1990, "genres": "rock"}
    )
    db.conn.execute("UPDATE library_albums SET album_type = ? WHERE id = ?", ("album", "al-02"))
    db.upsert_library_track({"id": "tr-03", "album_id": "al-02", "artist_id": "ar-01", "title": "Track 3"})

    # Artist 2: jazz, GB, person, 1970
    db.upsert_library_artist(
        {"id": "ar-02", "name": "Jazz Solo", "genres": "jazz", "country": "GB", "monitored": True}
    )
    db.conn.execute(
        "UPDATE library_artists SET artist_type = ?, member_count = ?, begin_year = ?, popularity = ? WHERE id = ?",
        ("person", 1, 1970, 800, "ar-02"),
    )
    # Album 3: 1975
    db.upsert_library_album(
        {"id": "al-03", "artist_id": "ar-02", "title": "Album 3", "year": 1975, "genres": "jazz"}
    )
    db.conn.execute("UPDATE library_albums SET album_type = ? WHERE id = ?", ("album", "al-03"))
    db.upsert_library_track({"id": "tr-04", "album_id": "al-03", "artist_id": "ar-02", "title": "Track 4"})
    # Track without file
    db.upsert_library_track({"id": "tr-05", "album_id": "al-03", "artist_id": "ar-02", "title": "Track 5 (No File)"})

    # Files for tr-01, tr-02, tr-03, tr-04
    for tid in ("tr-01", "tr-02", "tr-03", "tr-04"):
        db.upsert_library_file({
            "track_id": tid,
            "file_path": f"/music/{tid}.mp3",
            "size_bytes": 1000,
            "cutoff_met": 1,
        })

    db.conn.commit()
    return db


# -----------------------------------------------------------------------------
# Unit / Domain Tests
# -----------------------------------------------------------------------------

def test_artist_type_filter_is_case_insensitive():
    f1 = LibraryFacetFilter(artist_types=("Group", "PERSON"))
    assert f1.artist_types == ("group", "person")


def test_rules_json_roundtrip_and_validation():
    filt = LibraryFacetFilter(genres=("rock",), year_from=1980)
    rules = SmartRules(filter=filt, sort="year_desc", limit=50)
    raw = rules.to_json()
    parsed = SmartRules.from_json(raw)
    assert parsed.sort == "year_desc"
    assert parsed.limit == 50
    assert parsed.filter.genres == ("rock",)
    assert parsed.filter.year_from == 1980

    # Bad sort
    with pytest.raises(SmartCollectionError, match="Invalid sort"):
        SmartRules.from_json(json.dumps({"filter": {}, "sort": "invalid", "limit": 10}))

    # Limit 0
    with pytest.raises(SmartCollectionError, match="Limit must be between 1 and 1000"):
        SmartRules.from_json(json.dumps({"filter": {}, "sort": "random", "limit": 0}))

    # Limit 1001
    with pytest.raises(SmartCollectionError, match="Limit must be between 1 and 1000"):
        SmartRules.from_json(json.dumps({"filter": {}, "sort": "random", "limit": 1001}))

    # Malformed JSON
    with pytest.raises(SmartCollectionError, match="Invalid JSON"):
        SmartRules.from_json("not valid json")


def test_evaluate_only_tracks_with_files(seeded_db):
    rules = SmartRules(filter=LibraryFacetFilter(), limit=100)
    tracks = evaluate(seeded_db, rules)
    titles = [t["title"] for t in tracks]
    assert "Track 1" in titles
    assert "Track 2" in titles
    assert "Track 3" in titles
    assert "Track 4" in titles
    assert "Track 5 (No File)" not in titles


def test_evaluate_applies_filter_sort_and_limit(seeded_db):
    filt = LibraryFacetFilter(genres=("rock",), year_from=1980, year_to=1995)
    rules = SmartRules(filter=filt, sort="year_desc", limit=2)
    tracks = evaluate(seeded_db, rules)
    assert len(tracks) == 2
    # tr-03 is 1990; tr-01 & tr-02 are 1985
    assert tracks[0]["title"] == "Track 3"
    assert tracks[0]["year"] == 1990


# -----------------------------------------------------------------------------
# API Route Tests
# -----------------------------------------------------------------------------

def test_preview_counts_all_but_returns_sample(api, admin_headers, seeded_db):
    body = {
        "genres": ["rock"],
        "sort": "year_desc",
        "limit": 10,
    }
    res = api.post("/api/smart-collections/preview", headers=admin_headers, json=body)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["track_count"] == 3  # Track 1, 2, 3 have files
    assert len(data["tracks"]) == 3
    assert data["tracks"][0]["title"] == "Track 3"


def test_create_one_time_collection(api, admin_headers, seeded_db):
    body = {
        "name": "Rock 80s",
        "description": "My 80s rock tracks",
        "rules": {
            "genres": ["rock"],
            "year_from": 1980,
            "year_to": 1989,
            "sort": "year_desc",
            "limit": 100,
        },
        "keep_in_sync": False,
    }
    res = api.post("/api/smart-collections", headers=admin_headers, json=body)
    assert res.status_code == 201, res.text
    data = res.json()
    pl_id = data["id"]
    assert pl_id.startswith("smart_")
    assert data["name"] == "Rock 80s"
    assert data["track_count"] == 2  # Track 1 & 2
    assert data["targets"] == ["admin"]

    # Verify playlist row in DB
    row = seeded_db.get_playlist(pl_id)
    assert row is not None
    assert row["service"] == "trackseerr"
    assert row["source_kind"] == "smart"
    assert row["enabled"] is False
    assert row["monitor_mode"] == "none"
    assert row["auto_request"] is False
    assert row["tracks_json"] is not None
    stored_tracks = json.loads(row["tracks_json"])
    assert len(stored_tracks) == 2

    # Appears in GET /api/playlists
    pl_res = api.get("/api/playlists", headers=admin_headers)
    assert pl_res.status_code == 200
    all_pls = pl_res.json()
    assert any(p["id"] == pl_id for p in all_pls)


def test_create_empty_result_400(api, admin_headers, seeded_db):
    body = {
        "name": "Empty Collection",
        "rules": {
            "genres": ["nonexistentgenre"],
            "limit": 100,
        },
    }
    res = api.post("/api/smart-collections", headers=admin_headers, json=body)
    assert res.status_code == 400
    assert "The collection matched no tracks" in res.json()["detail"]


def test_create_rejects_unknown_target_user(api, admin_headers, seeded_db):
    body = {
        "name": "Target Test",
        "rules": {"genres": ["rock"]},
        "targets": ["unknown_user_id_123"],
    }
    res = api.post("/api/smart-collections", headers=admin_headers, json=body)
    assert res.status_code == 400
    assert "Target user 'unknown_user_id_123' does not exist" in res.json()["detail"]


def test_non_admin_forbidden(api, regular_headers, admin_headers, seeded_db):
    # Preview
    res = api.post("/api/smart-collections/preview", headers=regular_headers, json={"genres": ["rock"]})
    assert res.status_code == 403

    # Create
    res = api.post(
        "/api/smart-collections",
        headers=regular_headers,
        json={"name": "Test", "rules": {"genres": ["rock"]}},
    )
    assert res.status_code == 403

    # Create one as admin first
    create_res = api.post(
        "/api/smart-collections",
        headers=admin_headers,
        json={"name": "Admin Coll", "rules": {"genres": ["rock"]}},
    )
    pl_id = create_res.json()["id"]

    # Get
    res = api.get(f"/api/smart-collections/{pl_id}", headers=regular_headers)
    assert res.status_code == 403

    # Put
    res = api.put(f"/api/smart-collections/{pl_id}", headers=regular_headers, json={"name": "New Name"})
    assert res.status_code == 403

    # Sync
    res = api.post(f"/api/smart-collections/{pl_id}/sync", headers=regular_headers)
    assert res.status_code == 403


def test_lidarr_mode_409(api, admin_headers, seeded_db):
    seeded_db.update_media_management_settings({"library_mode": MODE_LIDARR})

    res = api.post("/api/smart-collections/preview", headers=admin_headers, json={"genres": ["rock"]})
    assert res.status_code == 409

    res = api.post(
        "/api/smart-collections",
        headers=admin_headers,
        json={"name": "Test", "rules": {"genres": ["rock"]}},
    )
    assert res.status_code == 409


def test_sync_cycle_refreshes_auto_update_collection(api, admin_headers, seeded_db, config):
    body = {
        "name": "Auto Sync Rock",
        "rules": {"genres": ["rock"], "sort": "added_desc", "limit": 100},
        "keep_in_sync": True,
    }
    create_res = api.post("/api/smart-collections", headers=admin_headers, json=body)
    assert create_res.status_code == 201
    pl_id = create_res.json()["id"]
    initial_tracks = json.loads(seeded_db.get_playlist(pl_id)["tracks_json"])
    assert len(initial_tracks) == 3

    # Add a new matching track + file
    seeded_db.upsert_library_track({"id": "tr-new", "album_id": "al-01", "artist_id": "ar-01", "title": "New Rock Track"})
    seeded_db.upsert_library_file({
        "track_id": "tr-new",
        "file_path": "/music/new_rock.mp3",
        "size_bytes": 1000,
        "cutoff_met": 1,
    })

    # Execute sync cycle
    sync_state.execute_sync(db=seeded_db, config=config, plex_client=None, spotify_client=None, deezer_client=None)

    updated_tracks = json.loads(seeded_db.get_playlist(pl_id)["tracks_json"])
    assert len(updated_tracks) == 4
    titles = [t["title"] for t in updated_tracks]
    assert "New Rock Track" in titles


def test_sync_cycle_skips_one_time_collection(api, admin_headers, seeded_db, config):
    body = {
        "name": "One Time Rock",
        "rules": {"genres": ["rock"], "sort": "added_desc", "limit": 100},
        "keep_in_sync": False,
    }
    create_res = api.post("/api/smart-collections", headers=admin_headers, json=body)
    assert create_res.status_code == 201
    pl_id = create_res.json()["id"]
    initial_tracks = json.loads(seeded_db.get_playlist(pl_id)["tracks_json"])
    assert len(initial_tracks) == 3

    # Add a new matching track + file
    seeded_db.upsert_library_track({"id": "tr-new-2", "album_id": "al-01", "artist_id": "ar-01", "title": "Another Rock Track"})
    seeded_db.upsert_library_file({
        "track_id": "tr-new-2",
        "file_path": "/music/another_rock.mp3",
        "size_bytes": 1000,
        "cutoff_met": 1,
    })

    # Execute sync cycle
    sync_state.execute_sync(db=seeded_db, config=config, plex_client=None, spotify_client=None, deezer_client=None)

    # Tracks unchanged because enabled=0
    stored_tracks = json.loads(seeded_db.get_playlist(pl_id)["tracks_json"])
    assert len(stored_tracks) == 3
    titles = [t["title"] for t in stored_tracks]
    assert "Another Rock Track" not in titles


def test_sync_cycle_empty_result_keeps_snapshot(api, admin_headers, seeded_db, config):
    body = {
        "name": "Auto Sync Jazz",
        "rules": {"genres": ["jazz"], "limit": 100},
        "keep_in_sync": True,
    }
    create_res = api.post("/api/smart-collections", headers=admin_headers, json=body)
    assert create_res.status_code == 201
    pl_id = create_res.json()["id"]
    initial_tracks = json.loads(seeded_db.get_playlist(pl_id)["tracks_json"])
    assert len(initial_tracks) == 1

    # Delete the files so nothing matches
    seeded_db.conn.execute("DELETE FROM library_files WHERE track_id = 'tr-04'")
    seeded_db.conn.commit()

    # Sync cycle
    sync_state.execute_sync(db=seeded_db, config=config, plex_client=None, spotify_client=None, deezer_client=None)

    row = seeded_db.get_playlist(pl_id)
    assert row["sync_status"] == "error"
    # Tracks snapshot kept!
    kept_tracks = json.loads(row["tracks_json"])
    assert len(kept_tracks) == 1
    assert kept_tracks[0]["title"] == "Track 4"


def test_sync_now_pushes_one_time_collection(api, admin_headers, seeded_db):
    body = {
        "name": "One Time Jazz",
        "rules": {"genres": ["jazz"], "limit": 100},
        "keep_in_sync": False,
    }
    create_res = api.post("/api/smart-collections", headers=admin_headers, json=body)
    assert create_res.status_code == 201
    pl_id = create_res.json()["id"]

    # Add second track and file
    seeded_db.upsert_library_track({"id": "tr-04b", "album_id": "al-03", "artist_id": "ar-02", "title": "Track 4B"})
    seeded_db.upsert_library_file({
        "track_id": "tr-04b",
        "file_path": "/music/tr04b.mp3",
        "size_bytes": 1000,
        "cutoff_met": 1,
    })

    # Sync now
    sync_res = api.post(f"/api/smart-collections/{pl_id}/sync", headers=admin_headers)
    assert sync_res.status_code == 200
    assert sync_res.json()["track_count"] == 2

    row = seeded_db.get_playlist(pl_id)
    updated_tracks = json.loads(row["tracks_json"])
    assert len(updated_tracks) == 2


def test_update_rules_resyncs(api, admin_headers, seeded_db):
    body = {
        "name": "Modifiable",
        "description": "Initial description",
        "rules": {"genres": ["rock"], "limit": 100},
        "keep_in_sync": False,
    }
    create_res = api.post("/api/smart-collections", headers=admin_headers, json=body)
    pl_id = create_res.json()["id"]

    # PUT new rules (genres: ["jazz"])
    update_res = api.put(
        f"/api/smart-collections/{pl_id}",
        headers=admin_headers,
        json={
            "description": "Updated description",
            "rules": {"genres": ["jazz"], "limit": 100},
        },
    )
    assert update_res.status_code == 200, update_res.text
    record = update_res.json()
    assert record["description"] == "Updated description"
    assert record["rules"]["genres"] == ["jazz"]
    assert record["track_count"] == 1
    assert record["enabled"] is False

    # GET returns updated state
    get_res = api.get(f"/api/smart-collections/{pl_id}", headers=admin_headers)
    assert get_res.status_code == 200
    assert get_res.json()["rules"]["genres"] == ["jazz"]


def test_monitor_mode_and_auto_request_rejected(api, admin_headers, seeded_db):
    body = {
        "name": "Mode Test",
        "rules": {"genres": ["rock"], "limit": 100},
    }
    create_res = api.post("/api/smart-collections", headers=admin_headers, json=body)
    pl_id = create_res.json()["id"]

    # PUT monitor mode != none -> 400
    mode_res = api.put(
        f"/api/playlists/{pl_id}/monitor-mode",
        headers=admin_headers,
        json={"monitor_mode": "track"},
    )
    assert mode_res.status_code == 400
    assert "Smart collections only list tracks already in your library" in mode_res.json()["detail"]

    # PUT auto-request -> 400
    ar_res = api.put(
        f"/api/playlists/{pl_id}/auto-request",
        headers=admin_headers,
        json={"auto_request": True},
    )
    assert ar_res.status_code == 400
    assert "Smart collections only list tracks already in your library" in ar_res.json()["detail"]


def test_media_server_push_called(api, admin_headers, seeded_db):
    fake_server = MagicMock()
    fake_server.sync_playlist.return_value = []
    fake_server.match_playlist_tracks.return_value = ([], [])

    with patch("trackseerr.api.routes.playlists.as_media_server", return_value=fake_server):
        body = {
            "name": "Push Test",
            "rules": {"genres": ["rock"], "limit": 100},
        }
        res = api.post("/api/smart-collections", headers=admin_headers, json=body)
        assert res.status_code == 201

        assert fake_server.sync_playlist.call_count == 1
        call_args = fake_server.sync_playlist.call_args
        model_pl = call_args[0][0]
        target_usernames = call_args[0][1]
        assert model_pl.name == "Push Test"
        assert len(model_pl.tracks) == 3
        assert target_usernames == ["admin"]

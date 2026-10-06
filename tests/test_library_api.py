"""Comprehensive integration and unit tests for TrackSeerr Native Library Management REST API.

Tests statistics, browsing, monitored toggles, cascading deletion, scanner controls,
Lidarr migration triggers, manual import pipeline, preview/batch renamer, and security traversal defenses.
"""

import json
from pathlib import Path
import struct
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db, get_lidarr_client, get_plex_client
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.library_scanner import library_scanner
from plex_playlist_sync.lidarr_migration import lidarr_migration_job
from plex_playlist_sync.storage import Database


def _create_minimal_flac(path: Path) -> None:
    """Writes a valid minimal FLAC file stream header recognizable by Mutagen."""
    sr_chan_bps_samples = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    streaminfo = (
        struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00")
        + sr_chan_bps_samples
        + b"\x00" * 16
    )
    header = b"fLaC\x80\x00\x00\x22" + streaminfo
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header)


@pytest.fixture
def test_db(tmp_path: Path):
    """Provides an isolated disk-backed Database for testing."""
    db_file = tmp_path / "test_library_api.db"
    db = Database(str(db_file))
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path: Path):
    """Provides a test Config pointing data_dir to tmp_path."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-token",
        data_dir=str(tmp_path),
    )


@pytest.fixture
def seeded_users(test_db: Database):
    """Seeds admin and standard non-admin users in the test database."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@example.com", is_admin=False)
    return {"admin": admin, "alice": alice}


def _auth_headers(user: dict[str, Any], test_db: Database, config: Config) -> dict[str, str]:
    """Generates an authenticated Bearer Authorization header."""
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app_and_client(test_db: Database, test_config: Config):
    """Creates a FastAPI test client with injected test database and config dependencies."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config

    client = TestClient(app)
    return app, client


# =========================================================================
# 1. Statistics & Browsing Routes
# =========================================================================

def test_library_stats_and_browsing_routes(
    app_and_client, test_db: Database, test_config: Config, seeded_users
):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # 1. Seed initial artist, album, track, and file
    art = test_db.upsert_library_artist({
        "id": "art-1",
        "name": "Radiohead",
        "clean_name": "radiohead",
        "monitored": True,
    })
    alb = test_db.upsert_library_album({
        "id": "alb-1",
        "artist_id": "art-1",
        "title": "OK Computer",
        "clean_title": "ok computer",
        "year": 1997,
        "cover_url": "https://example.com/okc.jpg",
        "monitored": True,
    })
    trk = test_db.upsert_library_track({
        "id": "trk-1",
        "album_id": "alb-1",
        "artist_id": "art-1",
        "title": "Airbag",
        "clean_title": "airbag",
        "track_number": 1,
        "disc_number": 1,
        "monitored": True,
    })
    fl = test_db.upsert_library_file({
        "id": "fl-1",
        "track_id": "trk-1",
        "file_path": "/music/Radiohead/OK Computer/01 - Airbag.flac",
        "relative_path": "Radiohead/OK Computer/01 - Airbag.flac",
        "codec": "FLAC",
        "bitrate": 900,
        "quality_name": "FLAC 16bit 44.1kHz",
        "size_bytes": 1048576,
        "cutoff_met": True,
    })

    # Test GET /api/library/stats
    resp_stats = client.get("/api/library/stats", headers=admin_headers)
    assert resp_stats.status_code == 200
    stats = resp_stats.json()
    assert stats["artist_count"] == 1
    assert stats["album_count"] == 1
    assert stats["track_count"] == 1
    assert stats["file_count"] == 1
    assert stats["total_size_bytes"] == 1048576

    # Test GET /api/library/artists
    resp_artists = client.get("/api/library/artists", headers=admin_headers)
    assert resp_artists.status_code == 200
    artists = resp_artists.json()
    assert len(artists) == 1
    assert artists[0]["id"] == "art-1"
    assert artists[0]["name"] == "Radiohead"
    assert artists[0]["album_count"] == 1
    assert artists[0]["track_count"] == 1
    assert artists[0]["image_url"] == "https://example.com/okc.jpg"

    # Search query
    resp_search = client.get("/api/library/artists?query=Radio", headers=admin_headers)
    assert resp_search.status_code == 200
    assert len(resp_search.json()) == 1

    resp_search_empty = client.get("/api/library/artists?query=Nonexistent", headers=admin_headers)
    assert resp_search_empty.status_code == 200
    assert len(resp_search_empty.json()) == 0

    # Test GET /api/library/artists/{artist_id}
    resp_art = client.get("/api/library/artists/art-1", headers=admin_headers)
    assert resp_art.status_code == 200
    art_detail = resp_art.json()
    assert art_detail["id"] == "art-1"
    assert "albums" in art_detail
    assert len(art_detail["albums"]) == 1
    assert art_detail["albums"][0]["id"] == "alb-1"
    assert art_detail["image_url"] == "https://example.com/okc.jpg"

    # Artist 404
    resp_art_404 = client.get("/api/library/artists/unknown-id", headers=admin_headers)
    assert resp_art_404.status_code == 404

    # Test GET /api/library/albums
    resp_albs = client.get("/api/library/albums", headers=admin_headers)
    assert resp_albs.status_code == 200
    albums = resp_albs.json()
    assert len(albums) == 1
    assert albums[0]["id"] == "alb-1"
    assert albums[0]["artist_name"] == "Radiohead"
    assert albums[0]["track_count"] == 1

    # Test GET /api/library/albums/{album_id}
    resp_alb = client.get("/api/library/albums/alb-1", headers=admin_headers)
    assert resp_alb.status_code == 200
    alb_detail = resp_alb.json()
    assert alb_detail["id"] == "alb-1"
    assert "tracks" in alb_detail
    assert len(alb_detail["tracks"]) == 1
    assert alb_detail["tracks"][0]["id"] == "trk-1"
    assert alb_detail["tracks"][0]["file"]["id"] == "fl-1"

    # Album 404
    resp_alb_404 = client.get("/api/library/albums/unknown-id", headers=admin_headers)
    assert resp_alb_404.status_code == 404

    # Test GET /api/library/tracks
    resp_trks = client.get("/api/library/tracks", headers=admin_headers)
    assert resp_trks.status_code == 200
    tracks = resp_trks.json()
    assert len(tracks) == 1
    assert tracks[0]["id"] == "trk-1"
    assert tracks[0]["file"]["id"] == "fl-1"

    # Filter tracks by album_id
    resp_trks_filtered = client.get("/api/library/tracks?album_id=alb-1", headers=admin_headers)
    assert resp_trks_filtered.status_code == 200
    assert len(resp_trks_filtered.json()) == 1

    # Verify metadata_json image_url takes precedence over child album cover_url
    test_db.upsert_library_artist({
        "id": "art-1",
        "name": "Radiohead",
        "metadata_json": {"image_url": "https://example.com/radiohead_artist.jpg"},
    })
    resp_art_meta = client.get("/api/library/artists/art-1", headers=admin_headers)
    assert resp_art_meta.status_code == 200
    assert resp_art_meta.json()["image_url"] == "https://example.com/radiohead_artist.jpg"

    resp_artists_meta = client.get("/api/library/artists", headers=admin_headers)
    assert resp_artists_meta.status_code == 200
    assert resp_artists_meta.json()[0]["image_url"] == "https://example.com/radiohead_artist.jpg"


# =========================================================================
# 2. Monitored Toggle Endpoints
# =========================================================================

def test_library_monitored_toggle_endpoints(
    app_and_client, test_db: Database, test_config: Config, seeded_users
):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    test_db.upsert_library_artist({"id": "art-1", "name": "Artist 1", "monitored": True})
    test_db.upsert_library_album({
        "id": "alb-1", "artist_id": "art-1", "title": "Album 1", "album_type": "album", "monitored": True
    })
    test_db.upsert_library_track({
        "id": "trk-1", "album_id": "alb-1", "artist_id": "art-1", "title": "Track 1", "monitored": True
    })

    # 1. Unmonitor artist with cascade_children=True
    resp = client.put(
        "/api/library/artists/art-1/monitored",
        json={"monitored": False, "cascade_children": True},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["monitored"] is False
    assert test_db.get_library_album("alb-1")["monitored"] is False
    assert test_db.get_library_track("trk-1")["monitored"] is False

    # 2. Monitor artist with cascade_children=False
    resp = client.put(
        "/api/library/artists/art-1/monitored",
        json={"monitored": True, "cascade_children": False},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["monitored"] is True
    # Children remain unmonitored
    assert test_db.get_library_album("alb-1")["monitored"] is False
    assert test_db.get_library_track("trk-1")["monitored"] is False

    # 3. Monitor album with cascade_tracks=True
    resp = client.put(
        "/api/library/albums/alb-1/monitored",
        json={"monitored": True, "cascade_tracks": True},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["monitored"] is True
    assert test_db.get_library_track("trk-1")["monitored"] is True

    # 4. Toggle track monitored directly
    resp = client.put(
        "/api/library/tracks/trk-1/monitored",
        json={"monitored": False},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["monitored"] is False
    assert test_db.get_library_track("trk-1")["monitored"] is False

    # 5. Presets: "albums", "singles_eps", "none", "all"
    # Seed a single/EP album alb-single with album_type="single" and a child track trk-single
    test_db.upsert_library_album({
        "id": "alb-single",
        "artist_id": "art-1",
        "title": "Single 1",
        "album_type": "single",
        "monitored": True,
    })
    test_db.upsert_library_track({
        "id": "trk-single",
        "album_id": "alb-single",
        "artist_id": "art-1",
        "title": "Single Track 1",
        "monitored": True,
    })

    # Test preset "albums"
    resp_preset_albums = client.put(
        "/api/library/artists/art-1/monitored",
        json={"monitored": True, "monitor_option": "albums"},
        headers=admin_headers,
    )
    assert resp_preset_albums.status_code == 200
    assert resp_preset_albums.json()["monitored"] is True
    assert test_db.get_library_artist("art-1")["monitored"] is True
    # Verify studio album alb-1 is monitored (1), track trk-1 is monitored (1)
    assert test_db.get_library_album("alb-1")["monitored"] is True
    assert test_db.get_library_track("trk-1")["monitored"] is True
    # but alb-single is unmonitored (0) and trk-single is unmonitored (0)
    assert test_db.get_library_album("alb-single")["monitored"] is False
    assert test_db.get_library_track("trk-single")["monitored"] is False

    # Test preset "singles_eps"
    resp_preset_singles = client.put(
        "/api/library/artists/art-1/monitored",
        json={"monitored": True, "monitor_option": "singles_eps"},
        headers=admin_headers,
    )
    assert resp_preset_singles.status_code == 200
    assert resp_preset_singles.json()["monitored"] is True
    assert test_db.get_library_artist("art-1")["monitored"] is True
    # Verify alb-single and trk-single are monitored (1)
    assert test_db.get_library_album("alb-single")["monitored"] is True
    assert test_db.get_library_track("trk-single")["monitored"] is True
    # but alb-1 and trk-1 are unmonitored (0)
    assert test_db.get_library_album("alb-1")["monitored"] is False
    assert test_db.get_library_track("trk-1")["monitored"] is False

    # Test preset "none"
    resp_preset_none = client.put(
        "/api/library/artists/art-1/monitored",
        json={"monitored": False, "monitor_option": "none"},
        headers=admin_headers,
    )
    assert resp_preset_none.status_code == 200
    assert resp_preset_none.json()["monitored"] is False
    # Verify artist, all albums, and all tracks are unmonitored (0)
    assert test_db.get_library_artist("art-1")["monitored"] is False
    assert test_db.get_library_album("alb-1")["monitored"] is False
    assert test_db.get_library_track("trk-1")["monitored"] is False
    assert test_db.get_library_album("alb-single")["monitored"] is False
    assert test_db.get_library_track("trk-single")["monitored"] is False

    # Test preset "all"
    resp_preset_all = client.put(
        "/api/library/artists/art-1/monitored",
        json={"monitored": True, "monitor_option": "all"},
        headers=admin_headers,
    )
    assert resp_preset_all.status_code == 200
    assert resp_preset_all.json()["monitored"] is True
    # Verify artist, all albums, and all tracks are monitored (1)
    assert test_db.get_library_artist("art-1")["monitored"] is True
    assert test_db.get_library_album("alb-1")["monitored"] is True
    assert test_db.get_library_track("trk-1")["monitored"] is True
    assert test_db.get_library_album("alb-single")["monitored"] is True
    assert test_db.get_library_track("trk-single")["monitored"] is True

    # 6. 404 checks
    assert client.put(
        "/api/library/artists/unknown/monitored", json={"monitored": True}, headers=admin_headers
    ).status_code == 404
    assert client.put(
        "/api/library/albums/unknown/monitored", json={"monitored": True}, headers=admin_headers
    ).status_code == 404
    assert client.put(
        "/api/library/tracks/unknown/monitored", json={"monitored": True}, headers=admin_headers
    ).status_code == 404


# =========================================================================
# 3. Delete Endpoints (Track, Album, Artist, File)
# =========================================================================

def test_library_delete_endpoints(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # 1. Test DELETE /api/library/tracks/{id} with delete_files=False
    dummy_file = tmp_path / "song.flac"
    _create_minimal_flac(dummy_file)

    test_db.upsert_library_artist({"id": "art-1", "name": "Artist 1"})
    test_db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": "Album 1"})
    test_db.upsert_library_track({"id": "trk-1", "album_id": "alb-1", "artist_id": "art-1", "title": "Track 1"})
    test_db.upsert_library_file({"id": "fl-1", "track_id": "trk-1", "file_path": str(dummy_file)})

    resp_del_trk = client.delete("/api/library/tracks/trk-1?delete_files=false", headers=admin_headers)
    assert resp_del_trk.status_code == 200
    assert resp_del_trk.json() == {"success": True}
    assert test_db.get_library_track("trk-1") is None
    # File remains on disk
    assert dummy_file.exists()

    # 2. Test DELETE /api/library/files/{file_id} with delete_file_from_disk=True
    test_db.upsert_library_track({"id": "trk-2", "album_id": "alb-1", "artist_id": "art-1", "title": "Track 2"})
    test_db.upsert_library_file({"id": "fl-2", "track_id": "trk-2", "file_path": str(dummy_file)})

    resp_del_file = client.delete("/api/library/files/fl-2?delete_file_from_disk=true", headers=admin_headers)
    assert resp_del_file.status_code == 200
    assert resp_del_file.json() == {"success": True}
    assert test_db.get_library_file("fl-2") is None
    assert not dummy_file.exists()

    # 3. Test DELETE /api/library/albums/{id} with delete_files=True
    dummy_file_album = tmp_path / "album_song.flac"
    _create_minimal_flac(dummy_file_album)

    test_db.upsert_library_track({"id": "trk-3", "album_id": "alb-1", "artist_id": "art-1", "title": "Track 3"})
    test_db.upsert_library_file({"id": "fl-3", "track_id": "trk-3", "file_path": str(dummy_file_album)})

    resp_del_alb = client.delete("/api/library/albums/alb-1?delete_files=true", headers=admin_headers)
    assert resp_del_alb.status_code == 200
    assert resp_del_alb.json() == {"success": True}
    assert test_db.get_library_album("alb-1") is None
    assert not dummy_file_album.exists()

    # 4. Test DELETE /api/library/artists/{id} with delete_files=True
    dummy_file_art = tmp_path / "artist_song.flac"
    _create_minimal_flac(dummy_file_art)

    test_db.upsert_library_album({"id": "alb-2", "artist_id": "art-1", "title": "Album 2"})
    test_db.upsert_library_track({"id": "trk-4", "album_id": "alb-2", "artist_id": "art-1", "title": "Track 4"})
    test_db.upsert_library_file({"id": "fl-4", "track_id": "trk-4", "file_path": str(dummy_file_art)})

    resp_del_art = client.delete("/api/library/artists/art-1?delete_files=true", headers=admin_headers)
    assert resp_del_art.status_code == 200
    assert resp_del_art.json() == {"success": True}
    assert test_db.get_library_artist("art-1") is None
    assert not dummy_file_art.exists()


# =========================================================================
# 4. Scanner and Migration Trigger Routes
# =========================================================================

def test_library_scan_and_migration_trigger_routes(
    app_and_client, test_db: Database, test_config: Config, seeded_users
):
    app, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    test_db.update_media_management_settings({"root_folder_path": "/music"})

    # 1. Filesystem Scanner Endpoints
    with patch.object(library_scanner, "start_scan", return_value=True) as mock_start, \
         patch.object(library_scanner, "get_status", return_value={"status": "scanning", "is_scanning": True}), \
         patch.object(library_scanner, "cancel_scan", return_value={"status": "cancelled", "is_scanning": False}):

        resp_scan = client.post(
            "/api/library/scan",
            json={"root_folder": "/music", "prune_missing": True},
            headers=admin_headers,
        )
        assert resp_scan.status_code == 200
        data = resp_scan.json()
        assert data["success"] is True
        assert data["status"]["status"] == "scanning"
        mock_start.assert_called_once()

        resp_status = client.get("/api/library/scan/status", headers=admin_headers)
        assert resp_status.status_code == 200
        assert resp_status.json()["status"] == "scanning"

        resp_cancel = client.post("/api/library/scan/cancel", headers=admin_headers)
        assert resp_cancel.status_code == 200
        assert resp_cancel.json()["status"] == "cancelled"

    # 2. Lidarr Migration Endpoints
    # Case A: Lidarr not configured (client is None)
    app.dependency_overrides[get_lidarr_client] = lambda: None
    resp_mig_none = client.post(
        "/api/library/migrate-lidarr",
        json={"auto_switch_mode": True},
        headers=admin_headers,
    )
    assert resp_mig_none.status_code == 400
    assert "not configured" in resp_mig_none.json()["detail"]

    # Case B: Lidarr offline
    mock_lidarr = MagicMock()
    mock_lidarr.test_connection.return_value = {"online": False, "error": "Connection refused"}
    app.dependency_overrides[get_lidarr_client] = lambda: mock_lidarr

    resp_mig_offline = client.post(
        "/api/library/migrate-lidarr",
        json={"auto_switch_mode": True},
        headers=admin_headers,
    )
    assert resp_mig_offline.status_code == 400
    assert "offline" in resp_mig_offline.json()["detail"]

    # Case C: Lidarr online, migration started
    mock_lidarr.test_connection.return_value = {"online": True, "version": "1.0.0"}
    with patch.object(lidarr_migration_job, "start_migration", return_value=True) as mock_mig_start, \
         patch.object(lidarr_migration_job, "get_status", return_value={"status": "running", "is_migrating": True}), \
         patch.object(lidarr_migration_job, "cancel", return_value={"status": "cancelled", "is_migrating": False}):

        resp_mig_ok = client.post(
            "/api/library/migrate-lidarr",
            json={"auto_switch_mode": True},
            headers=admin_headers,
        )
        assert resp_mig_ok.status_code == 200
        assert resp_mig_ok.json()["success"] is True
        assert resp_mig_ok.json()["status"]["status"] == "running"
        mock_mig_start.assert_called_once()

        resp_mig_status = client.get("/api/library/migrate-lidarr/status", headers=admin_headers)
        assert resp_mig_status.status_code == 200
        assert resp_mig_status.json()["status"] == "running"

        resp_mig_cancel = client.post("/api/library/migrate-lidarr/cancel", headers=admin_headers)
        assert resp_mig_cancel.status_code == 200
        assert resp_mig_cancel.json()["status"] == "cancelled"


# =========================================================================
# 5. Manual Import Pipeline (Scan and Commit)
# =========================================================================

def test_manual_import_scan_and_commit(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    app, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # Setup directories within tmp_path
    music_dir = tmp_path / "music"
    staging_dir = tmp_path / "downloads"
    music_dir.mkdir(parents=True)
    staging_dir.mkdir(parents=True)

    test_db.update_media_management_settings({
        "root_folder_path": str(music_dir),
        "staging_folder_path": str(staging_dir),
        "standard_track_format": "{track:00} - {Track Title}",
        "album_folder_format": "{Album Title} ({Release Year})",
    })

    # Seed target artist, album, track for matching
    art = test_db.upsert_library_artist({"id": "art-10", "name": "Radiohead", "monitored": True})
    alb = test_db.upsert_library_album({
        "id": "alb-10", "artist_id": "art-10", "title": "OK Computer", "year": 1997, "monitored": True
    })
    trk = test_db.upsert_library_track({
        "id": "trk-10", "album_id": "alb-10", "artist_id": "art-10", "title": "Karma Police", "track_number": 6, "monitored": True
    })

    # Create dummy FLAC in staging folder
    test_audio = staging_dir / "06 - Karma Police.flac"
    _create_minimal_flac(test_audio)

    # Mock inspect_audio_file to return matching metadata for the dummy file
    mock_meta = {
        "title": "Karma Police",
        "artist": "Radiohead",
        "album": "OK Computer",
        "album_artist": "Radiohead",
        "year": 1997,
        "track_number": 6,
        "disc_number": 1,
        "codec": "FLAC",
        "bitrate": 950,
        "sample_rate": 44100,
        "bits_per_sample": 16,
        "quality_full": "FLAC 16bit 44.1kHz",
        "file_path": str(test_audio),
        "duration": 261.0,
    }

    mock_plex = MagicMock()
    app.dependency_overrides[get_plex_client] = lambda: mock_plex

    with patch("plex_playlist_sync.api.routes.library.inspect_audio_file", return_value=mock_meta):
        # 1. Scan staging folder
        resp_scan = client.post(
            "/api/library/manual-import/scan",
            json={"folder_path": str(staging_dir)},
            headers=admin_headers,
        )
        assert resp_scan.status_code == 200
        candidates = resp_scan.json()
        assert len(candidates) == 1
        cand = candidates[0]
        assert cand["filename"] == "06 - Karma Police.flac"
        assert cand["matched_artist_id"] == "art-10"
        assert cand["matched_album_id"] == "alb-10"
        assert cand["matched_track_id"] == "trk-10"
        assert cand["confidence"] == 1.0

        # 2. Commit import item
        commit_payload = {
            "items": [
                {
                    "source_path": str(test_audio),
                    "artist_id": "art-10",
                    "album_id": "alb-10",
                    "track_id": "trk-10",
                    "artist_name": "Radiohead",
                    "album_title": "OK Computer",
                    "track_title": "Karma Police",
                    "track_number": 6,
                    "disc_number": 1,
                    "year": 1997,
                    "mode": "move",
                    "write_tags": False,
                }
            ]
        }

        resp_commit = client.post(
            "/api/library/manual-import/commit",
            json=commit_payload,
            headers=admin_headers,
        )
        assert resp_commit.status_code == 200
        res_data = resp_commit.json()
        assert res_data["imported_count"] == 1
        assert res_data["failed_count"] == 0
        assert len(res_data["results"]) == 1

        result = res_data["results"][0]
        assert result["status"] == "imported"
        dest_path = Path(result["destination_path"])
        assert dest_path.exists()
        assert dest_path.name == "06 - Karma Police.flac"
        assert "Radiohead" in dest_path.parts
        assert "OK Computer (1997)" in dest_path.parts

        # File moved: staging file no longer exists
        assert not test_audio.exists()

        # Database library_files record created
        db_file = test_db.get_library_file_for_track("trk-10")
        assert db_file is not None
        assert db_file["file_path"] == str(dest_path)
        assert db_file["codec"] == "FLAC"

        # Plex refresh triggered
        mock_plex.refresh_music_library.assert_called_once()


def _manual_import_cutoff(app, client, test_db, test_config, seeded_users, tmp_path, history_title=None):
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    music_dir, staging_dir = tmp_path / "music", tmp_path / "downloads"
    music_dir.mkdir(parents=True)
    staging_dir.mkdir(parents=True)
    test_db.update_media_management_settings({
        "root_folder_path": str(music_dir),
        "staging_folder_path": str(staging_dir),
        "standard_track_format": "{track:00} - {Track Title}",
        "album_folder_format": "{Album Title} ({Release Year})",
    })
    prof = test_db.get_default_quality_profile()
    test_db.update_quality_profile(prof["id"], {"cutoff_format_score": 500})
    test_db.upsert_library_artist({"id": "art-c", "name": "Radiohead", "monitored": True})
    test_db.upsert_library_album({"id": "alb-c", "artist_id": "art-c", "title": "OK Computer", "year": 1997, "monitored": True})
    test_db.upsert_library_track({
        "id": "trk-c", "album_id": "alb-c", "artist_id": "art-c", "title": "Karma Police", "track_number": 6, "monitored": True
    })
    if history_title:
        test_db.record_download_event("imported", track_id="trk-c", release_title=history_title)
    audio = staging_dir / "06 - Karma Police.flac"
    _create_minimal_flac(audio)
    meta = {
        "title": "Karma Police", "artist": "Radiohead", "album": "OK Computer", "album_artist": "Radiohead",
        "year": 1997, "track_number": 6, "disc_number": 1, "codec": "FLAC", "bitrate": 950, "sample_rate": 44100,
        "bits_per_sample": 16, "quality_full": "FLAC 16bit 44.1kHz", "file_path": str(audio), "duration": 261.0,
    }
    app.dependency_overrides[get_plex_client] = lambda: MagicMock()
    item = {
        "source_path": str(audio), "artist_id": "art-c", "album_id": "alb-c", "track_id": "trk-c",
        "artist_name": "Radiohead", "album_title": "OK Computer", "track_title": "Karma Police",
        "track_number": 6, "disc_number": 1, "year": 1997, "mode": "move", "write_tags": False,
    }
    with patch("plex_playlist_sync.api.routes.library.inspect_audio_file", return_value=meta):
        resp = client.post("/api/library/manual-import/commit", json={"items": [item]}, headers=admin_headers)
    assert resp.status_code == 200 and resp.json()["imported_count"] == 1
    return test_db.get_library_file_for_track("trk-c")


def test_manual_import_bare_quality_meets_cutoff_despite_format_score(
    app_and_client, test_db, test_config, seeded_users, tmp_path
):
    """A bare quality has no release title, so format score 0 must not defeat a cutoff_format_score."""
    app, client = app_and_client
    f = _manual_import_cutoff(app, client, test_db, test_config, seeded_users, tmp_path)
    assert f["cutoff_met"]


def test_manual_import_uses_imported_release_title_for_cutoff(
    app_and_client, test_db, test_config, seeded_users, tmp_path
):
    """When the imported release title is known and matches the quality, its (low) format score decides."""
    app, client = app_and_client
    f = _manual_import_cutoff(
        app, client, test_db, test_config, seeded_users, tmp_path, history_title="Radiohead - OK Computer (1997) [FLAC]"
    )
    assert not f["cutoff_met"]


# =========================================================================
# 6. Token-Template Preview & Batch Renamer
# =========================================================================

def test_rename_preview_and_apply(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    app, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    music_dir = tmp_path / "music"
    music_dir.mkdir(parents=True)

    test_db.update_media_management_settings({
        "root_folder_path": str(music_dir),
        "standard_track_format": "{track:00} - {Track Title}",
        "album_folder_format": "{Album Title} ({Release Year})",
    })

    # Create file at a non-standard legacy path
    legacy_dir = music_dir / "old_downloads"
    legacy_dir.mkdir(parents=True)
    legacy_file = legacy_dir / "radiohead_track_03_dirty.flac"
    _create_minimal_flac(legacy_file)

    art = test_db.upsert_library_artist({"id": "art-20", "name": "Radiohead", "monitored": True})
    alb = test_db.upsert_library_album({
        "id": "alb-20", "artist_id": "art-20", "title": "The Bends", "year": 1995, "monitored": True
    })
    trk = test_db.upsert_library_track({
        "id": "trk-20", "album_id": "alb-20", "artist_id": "art-20", "title": "High and Dry", "track_number": 3, "disc_number": 1, "monitored": True
    })
    fl = test_db.upsert_library_file({
        "id": "fl-20",
        "track_id": "trk-20",
        "file_path": str(legacy_file),
        "relative_path": "old_downloads/radiohead_track_03_dirty.flac",
        "codec": "FLAC",
    })

    mock_plex = MagicMock()
    app.dependency_overrides[get_plex_client] = lambda: mock_plex

    # 1. Preview renaming
    resp_prev = client.post(
        "/api/library/rename/preview",
        json={"album_id": "alb-20"},
        headers=admin_headers,
    )
    assert resp_prev.status_code == 200
    diffs = resp_prev.json()
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff["file_id"] == "fl-20"
    assert diff["current_path"] == str(legacy_file)
    assert diff["needs_rename"] is True
    assert diff["proposed_path"].endswith("Radiohead/The Bends (1995)/03 - High and Dry.flac")

    # 2. Apply renaming
    resp_apply = client.post(
        "/api/library/rename/apply",
        json={"file_ids": ["fl-20"]},
        headers=admin_headers,
    )
    assert resp_apply.status_code == 200
    res = resp_apply.json()
    assert res["renamed_count"] == 1
    assert res["errors"] == []

    # Verify physical file was renamed and old file is gone
    assert not legacy_file.exists()
    expected_path = music_dir / "Radiohead" / "The Bends (1995)" / "03 - High and Dry.flac"
    assert expected_path.exists()

    # Verify database library_files record is updated
    updated_fl = test_db.get_library_file("fl-20")
    assert updated_fl is not None
    assert updated_fl["file_path"] == str(expected_path)
    assert updated_fl["relative_path"] == "Radiohead/The Bends (1995)/03 - High and Dry.flac"

    # Plex refresh invoked
    mock_plex.refresh_music_library.assert_called_once()


# =========================================================================
# 7. Security: Traversal Defense & Permission Gates
# =========================================================================

def test_security_path_traversal_and_permissions(
    app_and_client, test_db: Database, test_config: Config, seeded_users
):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)

    # 1. Traversal attempt in manual import scan
    resp_bad_scan = client.post(
        "/api/library/manual-import/scan",
        json={"folder_path": "/music/../../etc"},
        headers=admin_headers,
    )
    assert resp_bad_scan.status_code == 400
    assert "traversal" in resp_bad_scan.json()["detail"].lower()

    # 2. Non-approved directory path in scan
    resp_unapproved = client.post(
        "/api/library/manual-import/scan",
        json={"folder_path": "/var/run"},
        headers=admin_headers,
    )
    assert resp_unapproved.status_code in (400, 403)

    # 3. Non-admin user cannot execute mutation endpoints
    assert client.put(
        "/api/library/artists/art-1/monitored",
        json={"monitored": True},
        headers=alice_headers,
    ).status_code == 403

    assert client.delete("/api/library/artists/art-1", headers=alice_headers).status_code == 403
    assert client.post("/api/library/scan", json={}, headers=alice_headers).status_code == 403
    assert client.post("/api/library/migrate-lidarr", json={}, headers=alice_headers).status_code == 403
    assert client.post("/api/library/manual-import/commit", json={"items": []}, headers=alice_headers).status_code == 403
    assert client.post("/api/library/rename/apply", json={"file_ids": []}, headers=alice_headers).status_code == 403


# =========================================================================
# 8. Artwork Endpoints: Local File & Remote Redirects
# =========================================================================

def test_get_album_cover_local_file(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    """Serves local cover file for an album when present on disk."""
    _, client = app_and_client
    alice_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    test_db.update_media_management_settings({"root_folder_path": str(tmp_path / "music")})
    album_dir = tmp_path / "music" / "Local Cover Band" / "Local Album"
    album_dir.mkdir(parents=True, exist_ok=True)
    cover_file = album_dir / "cover.jpg"
    fake_image_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 32
    cover_file.write_bytes(fake_image_bytes)

    art = test_db.upsert_library_artist({
        "id": "art-local-cover",
        "name": "Local Cover Band",
    })
    alb = test_db.upsert_library_album({
        "id": "alb-local-cover",
        "artist_id": art["id"],
        "title": "Local Album",
        "path": str(album_dir),
    })

    resp = client.get(f"/api/library/albums/{alb['id']}/cover", headers=alice_headers)
    assert resp.status_code == 200
    assert resp.content == fake_image_bytes
    assert "image/jpeg" in resp.headers.get("content-type", "")


def test_get_album_cover_remote_redirect(
    app_and_client, test_db: Database, test_config: Config, seeded_users
):
    """Redirects to remote cover URL when no local cover exists."""
    _, client = app_and_client
    alice_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    art = test_db.upsert_library_artist({
        "id": "art-remote-cover",
        "name": "Remote Cover Band",
    })
    remote_url = "https://example.com/cover.jpg"
    alb = test_db.upsert_library_album({
        "id": "alb-remote-cover",
        "artist_id": art["id"],
        "title": "Remote Album",
        "cover_url": remote_url,
    })

    resp = client.get(
        f"/api/library/albums/{alb['id']}/cover",
        headers=alice_headers,
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == remote_url


def test_get_artist_image_redirect(
    app_and_client, test_db: Database, test_config: Config, seeded_users
):
    """Redirects to remote artist image URL when present."""
    _, client = app_and_client
    alice_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    remote_img = "https://example.com/artist.jpg"
    art = test_db.upsert_library_artist({
        "id": "art-remote-img",
        "name": "Remote Image Band",
        "image_url": remote_img,
    })

    resp = client.get(
        f"/api/library/artists/{art['id']}/image",
        headers=alice_headers,
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == remote_img



def test_album_total_discs_helper_counts_catalog_discs():
    from plex_playlist_sync.api.routes.library import _album_total_discs

    class FakeDB:
        def list_library_tracks(self, album_id=None, limit=0):
            return [{"disc_number": 1}, {"disc_number": 2}, {"disc_number": None}]

    assert _album_total_discs(FakeDB(), "a") == 2
    assert _album_total_discs(FakeDB(), "a", 3) == 3
    assert _album_total_discs(FakeDB(), "a", None, "x") == 2

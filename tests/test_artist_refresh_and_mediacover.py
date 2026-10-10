"""Comprehensive test suite for Arr-Grade Artist Refresh, MediaCover Caching, and Scheduled Tasks.

Validates:
1. MbidEnricherClient.get_release_group_tracks correctly parses multi-disc media, recordings, and track numbers.
2. MediaCoverService fetches, magic-byte validates, and caches images locally with atomic replacement.
3. MediaCoverService rejects SSRF targets and invalid/corrupt image bytes without writing to disk.
4. refresh_single_artist enriches metadata, discography, hydrates canonical tracks, and caches artwork.
5. ArtistRefreshWorker thread lifecycle management (start, is_running, get_status, stop, idempotency).
6. Scheduled tasks registry and worker dispatch for artist_metadata_refresh endpoint.
7. LibraryScanner auto-hydration background trigger for newly discovered artists.
8. Lidarr catalog migration mapping of artist and album artwork.
"""

from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest
import requests

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.artist_refresh import refresh_single_artist
from trackseerr.artist_refresh_worker import (
    ArtistRefreshWorker,
    artist_refresh_worker,
)
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.lidarr import LidarrClient
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.config import Config
from trackseerr.library_scanner import LibraryScanner
from trackseerr.lidarr_migration import LidarrMigrationJob
from trackseerr.mediacover import MediaCoverService, mediacover_service
from trackseerr.models import LibraryAlbum, LibraryArtist
from trackseerr.storage import Database

pytestmark = pytest.mark.real_mbid_enricher

@pytest.fixture
def test_db(tmp_path: Path):
    """Provides an isolated disk-backed Database for testing."""
    db_file = tmp_path / "test_refresh.db"
    db = Database(str(db_file))
    yield db
    db.close()


@pytest.fixture
def secret_key(tmp_path: Path) -> bytes:
    """Provides a consistent test secret key."""
    return get_or_create_secret_key(data_dir=str(tmp_path))


@pytest.fixture
def test_config(tmp_path: Path) -> Config:
    """Provides a test Config."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-token",
        data_dir=str(tmp_path),
        role="all-in-one",
    )


@pytest.fixture
def seeded_users(test_db: Database) -> dict[str, dict[str, Any]]:
    """Seeds admin and standard non-admin users in test database."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@example.com", is_admin=False)
    return {"admin": admin, "alice": alice}


def _create_auth_cookies(db: Database, user: dict[str, Any], secret_key: bytes) -> dict[str, str]:
    """Creates authenticated session cookie."""
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret_key,
    )
    db.create_session(session_id=token, user_id=user["id"])
    return {"session_token": token}


@pytest.fixture
def app_and_client(test_db: Database, test_config: Config):
    """Creates a FastAPI test client with injected test database and config."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config

    client = TestClient(app)
    return app, client


# =========================================================================
# Test 1: get_release_group_tracks parses media and recordings
# =========================================================================

def test_get_release_group_tracks_parses_media_and_recordings():
    """Validates that MbidEnricherClient queries /ws/2/release and correctly extracts

    multi-disc track numbers, positions, titles, duration, and recording MBIDs.
    """
    enricher = MbidEnricherClient(base_url="https://api.brainzmash.cc")

    mock_release_payload = {
        "releases": [
            {
                "id": "rel-the-wall-123",
                "title": "The Wall",
                "media": [
                    {
                        "position": 1,
                        "tracks": [
                            {
                                "position": 1,
                                "title": "In the Flesh?",
                                "length": 196000,
                                "recording": {"id": "rec-flesh-01"},
                            },
                            {
                                "position": 2,
                                "title": "The Thin Ice",
                                "length": 147500,
                                "recording": {"id": "rec-ice-02"},
                            },
                        ],
                    },
                    {
                        "position": 2,
                        "tracks": [
                            {
                                "position": 1,
                                "title": "Hey You",
                                "length": 280000,
                                "recording": {"id": "rec-hey-03"},
                            },
                            {
                                "position": 2,
                                "title": "Is There Anybody Out There?",
                                "length": None,
                                "recording": {"id": "rec-out-04"},
                            },
                        ],
                    },
                ],
            }
        ]
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_release_payload

    with patch.object(enricher, "_do_http_call", return_value=mock_resp) as mock_http:
        tracks = enricher.get_release_group_tracks("rg-the-wall-uuid")

        assert len(tracks) == 4
        # Disc 1, Track 1
        assert tracks[0]["track_number"] == 1
        assert tracks[0]["disc_number"] == 1
        assert tracks[0]["title"] == "In the Flesh?"
        assert tracks[0]["duration_seconds"] == 196.0
        assert tracks[0]["mb_recording_id"] == "rec-flesh-01"

        # Disc 1, Track 2
        assert tracks[1]["track_number"] == 2
        assert tracks[1]["disc_number"] == 1
        assert tracks[1]["title"] == "The Thin Ice"
        assert tracks[1]["duration_seconds"] == 147.5
        assert tracks[1]["mb_recording_id"] == "rec-ice-02"

        # Disc 2, Track 1
        assert tracks[2]["track_number"] == 1
        assert tracks[2]["disc_number"] == 2
        assert tracks[2]["title"] == "Hey You"
        assert tracks[2]["duration_seconds"] == 280.0
        assert tracks[2]["mb_recording_id"] == "rec-hey-03"

        # Disc 2, Track 2 (no duration length)
        assert tracks[3]["track_number"] == 2
        assert tracks[3]["disc_number"] == 2
        assert tracks[3]["title"] == "Is There Anybody Out There?"
        assert tracks[3]["duration_seconds"] is None
        assert tracks[3]["mb_recording_id"] == "rec-out-04"

        # Verify caching: subsequent call uses in-memory cache without repeating HTTP call
        cached_tracks = enricher.get_release_group_tracks("rg-the-wall-uuid")
        assert len(cached_tracks) == 4
        assert mock_http.call_count == 1


# =========================================================================
# Test 2: mediacover_service caches and validates image
# =========================================================================

def test_mediacover_service_caches_and_validates_image(tmp_path: Path):
    """Verifies that MediaCoverService downloads valid JPEG artwork, checks magic bytes,

    atomically writes to cache, and serves subsequent requests from disk without network calls.
    """
    service = MediaCoverService(base_dir=tmp_path)
    valid_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 50

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.iter_content.return_value = [valid_jpeg[:20], valid_jpeg[20:]]

    remote_url = "https://coverartarchive.org/release-group/rg-1/front-500.jpg"

    with patch("requests.get", return_value=mock_resp) as mock_get:
        # First call: downloads and caches
        result_path = service.ensure_artwork("artist_poster", "artist-uuid-1", remote_url, block=True)
        assert result_path is not None
        assert result_path.is_file()
        assert result_path.read_bytes() == valid_jpeg
        assert mock_get.call_count == 1

        # Second call: returns cached path directly without network request
        cached_path = service.ensure_artwork("artist_poster", "artist-uuid-1", remote_url)
        assert cached_path == result_path
        assert mock_get.call_count == 1


# =========================================================================
# Test 3: mediacover_service rejects unsafe and invalid bytes
# =========================================================================

def test_mediacover_service_rejects_unsafe_and_invalid_bytes(tmp_path: Path):
    """Verifies SSRF blocking for private/loopback/cloud IPs and rejection of corrupt

    or non-image payloads.
    """
    service = MediaCoverService(base_dir=tmp_path)

    # 1. SSRF URL rejection
    ssrf_urls = [
        "http://127.0.0.1:8080/secret.jpg",
        "http://localhost:3000/avatar.png",
        "http://10.0.0.1/admin.jpg",
        "http://192.168.1.50/cam.jpg",
        "http://172.16.0.1/internal.jpg",
        "http://169.254.169.254/latest/meta-data",
        "ftp://example.com/image.jpg",
        "file:///etc/passwd",
    ]

    for bad_url in ssrf_urls:
        target = service.get_artist_poster_path(f"artist-{hash(bad_url)}")
        success = service.cache_image(target, bad_url)
        assert success is False
        assert not target.exists()

    # 2. Corrupt / non-image payload rejection (status 200 but not JPEG/PNG/WEBP)
    invalid_payloads = [
        b"<html><body>502 Bad Gateway</body></html>",
        b"plain text content not an image header",
        b"\xff\xd8\xff",  # Too short (< 12 bytes)
        b"RIFF\x00\x00\x00\x00NOTW",  # Bad WEBP header
    ]

    for idx, payload in enumerate(invalid_payloads):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_content.return_value = [payload]

        target = tmp_path / f"invalid_{idx}.jpg"
        with patch("requests.get", return_value=mock_resp):
            success = service.cache_image(target, f"https://example.com/img_{idx}.jpg")
            assert success is False
            assert not target.exists()


# =========================================================================
# Test 4: refresh_single_artist hydrates tracks and caches artwork
# =========================================================================

def test_refresh_single_artist_hydrates_tracks_and_caches_artwork(test_db: Database, tmp_path: Path):
    """Tests refresh_single_artist enriching metadata, creating albums and tracks,

    invoking artwork caching, and populating monitored missing tracks.
    """
    # 1. Create artist in test_db with known MBID
    artist_id = "artist-pf-1"
    test_db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name="Pink Floyd",
            mbid="mbid-pink-floyd",
            monitored=True,
            monitor_option="all",
        )
    )

    # 2. Setup mock enricher
    mock_enricher = MagicMock(spec=MbidEnricherClient)
    mock_enricher.get_artist_details.return_value = {
        "id": "mbid-pink-floyd",
        "country": "GB",
        "genres": ["Progressive Rock", "Psychedelic Rock"],
        "bio": "Legendary English progressive rock band",
    }
    mock_enricher.get_artist_discography.return_value = [
        {
            "id": "rg-dark-side-123",
            "title": "The Dark Side of the Moon",
            "album_type": "album",
            "year": 1973,
            "cover_url": "https://coverartarchive.org/release-group/rg-dark-side-123/front-500.jpg",
        }
    ]
    mock_enricher.get_artist_discography_result.return_value = (
        mock_enricher.get_artist_discography.return_value,
        True,
    )
    mock_enricher.source_available.return_value = True
    mock_enricher.stats.return_value = {"network_requests": 0, "cache_hits": 0}
    mock_enricher.get_release_group_tracks.return_value = [
        {
            "track_number": 1,
            "disc_number": 1,
            "title": "Speak to Me",
            "duration_seconds": 65.0,
            "mb_recording_id": "rec-speak-01",
        },
        {
            "track_number": 2,
            "disc_number": 1,
            "title": "Breathe",
            "duration_seconds": 169.0,
            "mb_recording_id": "rec-breathe-02",
        },
        {
            "track_number": 3,
            "disc_number": 1,
            "title": "Time",
            "duration_seconds": 425.0,
            "mb_recording_id": "rec-time-03",
        },
    ]

    # 3. Patch mediacover_service to track image caching calls
    with patch.object(mediacover_service, "ensure_artwork", return_value=Path("/tmp/cover.jpg")) as mock_cover:
        result = refresh_single_artist(
            artist_id=artist_id,
            db=test_db,
            enricher=mock_enricher,
        )

        assert result["success"] is True

        # Verify artist enrichment in DB
        artist = test_db.get_library_artist(artist_id)
        assert artist is not None
        assert artist["country"] == "GB"
        assert "Progressive Rock" in artist["genres"]
        assert artist["bio"] == "Legendary English progressive rock band"

        # Verify album creation in DB
        albums = test_db.list_library_albums(artist_id=artist_id)
        assert len(albums) == 1
        album = albums[0]
        assert album["title"] == "The Dark Side of the Moon"
        assert album["year"] == 1973
        assert album["mb_release_group_id"] == "rg-dark-side-123"
        assert album["monitored"] == 1

        # Verify canonical track hydration in DB
        tracks = test_db.list_library_tracks(album_id=album["id"])
        assert len(tracks) == 3
        track_titles = {t["title"] for t in tracks}
        assert track_titles == {"Speak to Me", "Breathe", "Time"}

        for t in tracks:
            assert t["monitored"] == 1
            assert t["mb_recording_id"] is not None

        # Verify unacquired missing tracks query returns all 3 hydrated tracks
        missing_tracks = test_db.get_monitored_missing_catalog_tracks()
        assert len(missing_tracks) >= 3
        missing_titles = {t["track_title"] for t in missing_tracks}
        assert {"Speak to Me", "Breathe", "Time"}.issubset(missing_titles)

        # Verify artwork caching was invoked for album cover
        mock_cover.assert_called_once_with(
            "album_cover",
            album["id"],
            "https://coverartarchive.org/release-group/rg-dark-side-123/front-500.jpg",
        )


# =========================================================================
# Test 5: artist_refresh_worker lifecycle and status
# =========================================================================

def test_artist_refresh_worker_lifecycle_and_status(test_db: Database):
    """Tests ArtistRefreshWorker background thread start, double-start guard,

    is_running check, status inspection, and graceful stop.
    """
    worker = ArtistRefreshWorker()

    # Initial state
    assert worker.is_running() is False
    init_status = worker.get_status()
    assert init_status["running"] is False
    assert init_status["artists_checked"] == 0

    # Start worker thread with small pace delay
    started = worker.start(
        db=test_db,
        interval_seconds=3600,
        pace_delay=0.05,
    )
    assert started is True
    assert worker.is_running() is True

    # Guard: starting already-running worker returns False
    double_start = worker.start(db=test_db)
    assert double_start is False

    # Check status snapshot while running
    status = worker.get_status()
    assert status["running"] is True
    assert status["interval_seconds"] == 3600
    assert status["pace_delay"] == 0.05

    # Stop worker gracefully
    worker.stop()
    assert worker.is_running() is False
    stopped_status = worker.get_status()
    assert stopped_status["running"] is False


# =========================================================================
# Test 6: system scheduled task artist_metadata_refresh
# =========================================================================

def test_system_scheduled_task_artist_metadata_refresh(
    app_and_client,
    seeded_users: dict[str, dict[str, Any]],
    secret_key: bytes,
    test_db: Database,
):
    """Verifies that artist_metadata_refresh is listed in GET /api/system/tasks,

    can be triggered via POST /api/system/tasks/artist_metadata_refresh/run,
    and returns 400 for cancel attempts.
    """
    _, client = app_and_client
    admin_cookies = _create_auth_cookies(test_db, seeded_users["admin"], secret_key)
    alice_cookies = _create_auth_cookies(test_db, seeded_users["alice"], secret_key)

    # 1. Non-admin GET /api/system/tasks returns 403
    resp_forbidden = client.get("/api/system/tasks", cookies=alice_cookies)
    assert resp_forbidden.status_code == 403

    # 2. Admin GET /api/system/tasks contains artist_metadata_refresh
    resp_tasks = client.get("/api/system/tasks", cookies=admin_cookies)
    assert resp_tasks.status_code == 200
    tasks = resp_tasks.json()
    task_map = {t["id"]: t for t in tasks}

    assert "artist_metadata_refresh" in task_map
    ar_task = task_map["artist_metadata_refresh"]
    assert ar_task["name"] == "Artist Metadata & Discography Refresh"
    assert ar_task["interval"] == "Every 24h"
    assert ar_task["can_trigger"] is True
    assert ar_task["can_cancel"] is False

    # 3. Trigger task manually via POST /run
    with patch.object(artist_refresh_worker, "refresh_once") as mock_refresh:
        resp_run = client.post(
            "/api/system/tasks/artist_metadata_refresh/run",
            cookies=admin_cookies,
        )
        assert resp_run.status_code == 200
        run_data = resp_run.json()
        assert run_data["success"] is True
        assert "dispatched" in run_data["message"]

        # Give background thread a moment to invoke worker
        time.sleep(0.1)
        mock_refresh.assert_called_once()

    # 4. Attempt cancellation returns 400 (can_cancel=False)
    resp_cancel = client.post(
        "/api/system/tasks/artist_metadata_refresh/cancel",
        cookies=admin_cookies,
    )
    assert resp_cancel.status_code == 400
    assert "does not support cancellation" in resp_cancel.json()["detail"]


# =========================================================================
# Test 7: LibraryScanner triggers auto-hydration for newly created artists
# =========================================================================

@pytest.mark.real_scanner_hydration
def test_library_scanner_triggers_auto_hydration_for_new_artists(test_db: Database, tmp_path: Path):
    """Validates that LibraryScanner.scan() launches a background auto-hydration thread

    when new artists are discovered and created.
    """
    scanner = LibraryScanner()

    # Create dummy music folder with an audio file
    root = tmp_path / "music"
    artist_dir = root / "Queen" / "A Night at the Opera"
    artist_dir.mkdir(parents=True, exist_ok=True)
    audio_file = artist_dir / "01 - Bohemian Rhapsody.flac"
    audio_file.write_bytes(b"\x00" * 100)

    # Mock inspect_audio_file to return Queen metadata
    mock_meta = {
        "title": "Bohemian Rhapsody",
        "artist": "Queen",
        "album": "A Night at the Opera",
        "track_number": 1,
        "disc_number": 1,
        "codec": "FLAC",
        "duration": 354.0,
        "quality_full": "FLAC",
        "file_path": str(audio_file),
        "musicbrainz_artistid": "mbid-queen-123",
        "musicbrainz_albumid": None,
        "musicbrainz_releasegroupid": None,
        "musicbrainz_trackid": None,
        "isrc": None,
    }

    with patch("trackseerr.library_scanner.inspect_audio_file", return_value=mock_meta), \
         patch("trackseerr.artist_refresh_worker.artist_refresh_worker.refresh_once") as mock_refresh:

        res = scanner.scan(db=test_db, root_folder=str(root))
        assert res["status"] == "completed"
        assert res["artists_created"] == 1

        # Check that artist was created
        queen = test_db.get_library_artist_by_name("Queen")
        assert queen is not None

        # Give background thread time to start
        time.sleep(0.15)
        mock_refresh.assert_called_once()
        call_kwargs = mock_refresh.call_args.kwargs
        assert call_kwargs.get("artist_ids") == [queen["id"]]


def test_artist_refresh_worker_refresh_once_not_stubbed_by_default():
    """Regression test"""
    from trackseerr.artist_refresh_worker import artist_refresh_worker
    assert artist_refresh_worker.refresh_once.__name__ == "refresh_once"


def test_library_scanner_auto_hydration_neutralized_without_marker(test_db: Database, tmp_path: Path):
    scanner = LibraryScanner()
    root = tmp_path / "music"
    artist_dir = root / "Queen" / "A Night at the Opera"
    artist_dir.mkdir(parents=True, exist_ok=True)
    audio_file = artist_dir / "01 - Bohemian Rhapsody.flac"
    audio_file.write_bytes(b"\x00" * 100)
    mock_meta = {"title": "Bohemian Rhapsody", "artist": "Queen", "album": "A Night at the Opera", "track_number": 1, "disc_number": 1, "codec": "FLAC", "duration": 354.0, "quality_full": "FLAC", "file_path": str(audio_file), "musicbrainz_artistid": "mbid-queen-123", "musicbrainz_albumid": None, "musicbrainz_releasegroupid": None, "musicbrainz_trackid": None, "isrc": None}
    with patch("trackseerr.library_scanner.inspect_audio_file", return_value=mock_meta), \
         patch("trackseerr.artist_refresh_worker.artist_refresh_worker.refresh_once") as mock_refresh:
        res = scanner.scan(db=test_db, root_folder=str(root))
        assert res["status"] == "completed"
        assert res["artists_created"] == 1
        time.sleep(0.15)
        mock_refresh.assert_not_called()

    # Regression check: threading.Thread is no longer replaced globally.
    # Subclasses and Thread.start work as expected without RuntimeError: thread.__init__() not called.
    timer = threading.Timer(0.01, lambda: None)
    timer.start()
    timer.join()
    assert issubclass(threading.Timer, threading.Thread)
    assert callable(threading.Thread.start)


# =========================================================================
# Test 8: Lidarr migration maps artist and album artwork
# =========================================================================

def test_lidarr_migration_maps_artist_and_album_artwork(test_db: Database):
    """Validates that LidarrMigrationJob extracts and stores poster/banner for artists

    and cover artwork for albums from Lidarr API images payload.
    """
    job = LidarrMigrationJob()

    mock_lidarr = MagicMock(spec=LidarrClient)
    mock_lidarr.get_all_artists.return_value = [
        {
            "id": 101,
            "artistName": "Radiohead",
            "foreignArtistId": "a74b1b7f-71a5-4011-9441-d0b5e4122711",
            "path": "/music/Radiohead",
            "monitored": True,
            "images": [
                {"coverType": "poster", "url": "https://lidarr.local/MediaCover/Artists/101/poster.jpg"},
                {"coverType": "banner", "url": "https://lidarr.local/MediaCover/Artists/101/banner.jpg"},
                {"coverType": "fanart", "url": "https://lidarr.local/MediaCover/Artists/101/fanart.jpg"},
            ],
        }
    ]
    okc_albums = [
        {
            "id": 201,
            "artistId": 101,
            "title": "OK Computer",
            "foreignAlbumId": "album-mbid-okc",
            "year": 1997,
            "monitored": True,
            "images": [
                {"coverType": "cover", "url": "https://lidarr.local/MediaCover/Albums/201/cover.jpg"}
            ],
        }
    ]
    mock_lidarr.fetch_artist_albums.side_effect = lambda artist_id, **kw: [
        a for a in okc_albums if a["artistId"] == artist_id
    ]
    mock_lidarr.fetch_album_tracks.side_effect = lambda album_id, **kw: []
    mock_lidarr.fetch_artist_track_files.side_effect = lambda artist_id, **kw: []

    res = job.run_migration(db=test_db, lidarr_client=mock_lidarr, auto_switch_mode=False)
    assert res["status"] == "completed"
    assert res["artists_migrated"] == 1
    assert res["albums_migrated"] == 1

    # Verify artist has mapped poster and banner URLs
    artist = test_db.get_library_artist_by_name("Radiohead")
    assert artist is not None
    assert artist["image_url"] == "https://lidarr.local/MediaCover/Artists/101/poster.jpg"
    assert artist["banner_url"] == "https://lidarr.local/MediaCover/Artists/101/banner.jpg"

    # Verify album has mapped cover URL
    album = test_db.get_library_album_by_title(artist["id"], "OK Computer")
    assert album is not None
    assert album["cover_url"] == "https://lidarr.local/MediaCover/Albums/201/cover.jpg"

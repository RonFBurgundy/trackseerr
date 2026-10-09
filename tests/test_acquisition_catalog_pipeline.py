"""Comprehensive test suite for Phase 1: Acquisition-to-Catalog Pipeline & Anti-Stall.

Covers:
- Test 1: Migration v17 creates download_blocklist and adds track_id / album_id columns to active_downloads.
- Test 2: Blocklist CRUD methods (add, is_blocklisted by hash, guid, title, list, remove).
- Test 3: Blocklisted release filtering during search and quality evaluation / ranking.
- Test 4: AcquisitionWorker file import automatically populates native library catalog tables.
- Test 5: Failing downloads and empty audio transfers automatically add releases to blocklist.
- Test 6: WantedBacklogWorker sweeps monitored missing catalog tracks and dispatches search & grab.
- Test 7: WantedBacklogWorker sweeps cutoff-unmet catalog tracks when quality upgrades are enabled.
- Test 8: Blocklist REST API endpoints (GET / DELETE) with admin RBAC enforcement.
- Test 9: Library mode == "lidarr" skips native catalog table population.
"""

from tests.audio_fixtures import write_flac
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from trackseerr.acquisition_coordinator import (
    AcquisitionCoordinator,
    _to_quality_profile,
)
from trackseerr.acquisition_worker import AcquisitionWorker
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.backlog_worker import WantedBacklogWorker
from trackseerr.config import Config
from trackseerr.models import (
    AcquisitionSearchResult,
    ActiveDownload,
    BlocklistItem,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    LibraryAlbum,
    LibraryArtist,
    LibraryFile,
    LibraryTrack,
    MusicRequest,
    QualityProfile,
    QualityProfileItem,
    RequestStatus,
)
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance with WAL / foreign keys and all migrations."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def workspace_dirs(tmp_path):
    """Provides isolated staging / downloads and music destination directories."""
    downloads_dir = tmp_path / "downloads"
    music_dir = tmp_path / "music"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    music_dir.mkdir(parents=True, exist_ok=True)
    return downloads_dir, music_dir


@pytest.fixture
def test_config(tmp_path):
    """Provides test Config pointing to isolated temp directory."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-token",
        data_dir=str(tmp_path),
        auto_approve_requests=True,
    )


@pytest.fixture
def seeded_users(test_db):
    """Seeds admin and standard user accounts in test DB."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice}


@pytest.fixture
def app_and_client(test_db, test_config):
    """Instantiates the FastAPI test client with dependency overrides."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    client = TestClient(app)
    return app, client


def _auth_headers(user: dict[str, Any], test_db: Database, config: Config) -> dict[str, str]:
    """Generates a valid signed Bearer token and active DB session."""
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# Test 1: Migration v17 Schema & Columns
# ===========================================================================

def test_migration_v17_schema_and_columns(test_db: Database):
    """Verifies migration v17 creates download_blocklist and adds columns to active_downloads."""
    cur = test_db.conn.cursor()

    # Check migration version
    cur.execute("SELECT MAX(version) FROM schema_migrations")
    assert cur.fetchone()[0] >= 17

    # Check download_blocklist table exists
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='download_blocklist'"
    )
    assert cur.fetchone() is not None

    # Check download_blocklist table columns
    cur.execute("PRAGMA table_info(download_blocklist)")
    cols = {row[1]: row[2] for row in cur.fetchall()}
    expected_cols = [
        "id",
        "source_title",
        "artist",
        "album",
        "release_guid",
        "info_hash",
        "protocol",
        "indexer",
        "reason",
        "created_at",
    ]
    for c in expected_cols:
        assert c in cols, f"Column '{c}' missing from download_blocklist"

    # Check download_blocklist indexes
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='download_blocklist'"
    )
    indexes = [row[0] for row in cur.fetchall()]
    assert "idx_blocklist_hash" in indexes
    assert "idx_blocklist_title" in indexes
    assert "idx_blocklist_guid" in indexes

    # Check active_downloads has track_id and album_id columns
    cur.execute("PRAGMA table_info(active_downloads)")
    active_cols = [row[1] for row in cur.fetchall()]
    assert "track_id" in active_cols
    assert "album_id" in active_cols


# ===========================================================================
# Test 2: Blocklist CRUD and Matching
# ===========================================================================

def test_blocklist_crud_and_matching(test_db: Database):
    """Tests adding, matching by hash/guid/title, listing, and removing blocklist entries."""
    hash_val = "4a5b6c7d8e9f0123456789abcdef0123456789ab"
    guid_val = "guid-release-test-123"
    title_val = "Radiohead - OK Computer (1997) [FLAC 24bit]"

    # 1. Add item with hash
    item1 = test_db.add_to_blocklist(
        source_title=title_val,
        artist="Radiohead",
        album="OK Computer",
        release_guid=guid_val,
        info_hash=hash_val,
        protocol="torrent",
        indexer="Redacted",
        reason="Corrupt FLAC headers",
    )
    assert item1["id"].startswith("bl-")
    assert item1["source_title"] == title_val
    assert item1["info_hash"] == hash_val.lower()

    # 2. Check matching by case-insensitive hash
    assert test_db.is_blocklisted(info_hash=hash_val.upper()) is True
    assert test_db.is_blocklisted(info_hash="0000000000000000000000000000000000000000") is False

    # 3. Check matching by release_guid
    assert test_db.is_blocklisted(release_guid=guid_val) is True
    assert test_db.is_blocklisted(release_guid="unrelated-guid") is False

    # 4. Check matching by title (exact and clean)
    assert test_db.is_blocklisted(release_title=title_val) is True
    assert test_db.is_blocklisted(release_title="radiohead ok computer 1997 flac 24bit") is True
    assert test_db.is_blocklisted(release_title="Pink Floyd - The Wall") is False

    # 5. List items
    items = test_db.list_blocklist(limit=50)
    assert len(items) == 1
    assert items[0]["id"] == item1["id"]

    # 6. Remove item
    removed = test_db.remove_from_blocklist(item1["id"])
    assert removed is True
    assert test_db.is_blocklisted(info_hash=hash_val) is False
    assert test_db.is_blocklisted(release_guid=guid_val) is False
    assert test_db.is_blocklisted(release_title=title_val) is False
    assert len(test_db.list_blocklist()) == 0

    # Removing nonexistent returns False
    assert test_db.remove_from_blocklist("nonexistent-id") is False


# ===========================================================================
# Test 3: Blocklisted Release Skipped During Ranking & Search
# ===========================================================================

def test_blocklisted_release_skipped_in_ranking_and_search(test_db: Database):
    """Verifies that blocklisted candidates are filtered out during ranking and search sweeps."""
    coordinator = AcquisitionCoordinator()
    default_profile = test_db.get_default_quality_profile()

    blocklisted_hash = "1111222233334444555566667777888899990000"
    test_db.add_to_blocklist(
        source_title="Daft Punk - Discovery [FLAC]",
        info_hash=blocklisted_hash,
        reason="Persistent stalled release",
    )

    clean_candidate = AcquisitionSearchResult(
        download_id="cand-good",
        title="Daft Punk - Discovery (2001) [FLAC 16bit]",
        artist="Daft Punk",
        album="Discovery",
        item_type="album",
        size_bytes=350_000_000,
        protocol="torrent",
        magnet_url="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        source="torznab",
    )

    bad_candidate = AcquisitionSearchResult(
        download_id="cand-bad",
        title="Daft Punk - Discovery [FLAC]",
        artist="Daft Punk",
        album="Discovery",
        item_type="album",
        size_bytes=360_000_000,
        protocol="torrent",
        magnet_url=f"magnet:?xt=urn:btih:{blocklisted_hash}",
        source="torznab",
    )

    # evaluate_and_rank with db passed filters out bad_candidate
    ranked = coordinator.evaluate_and_rank(
        candidates=[bad_candidate, clean_candidate],
        profile=default_profile,
        db=test_db,
    )

    assert len(ranked) == 1
    assert ranked[0][0].download_id == "cand-good"

    # search_all_indexers with db passed also filters out blocklisted results
    mock_indexer_driver = MagicMock()
    mock_indexer_driver.search.return_value = [bad_candidate, clean_candidate]

    test_db.create_indexer({
        "id": "idx-1",
        "name": "Test Torznab",
        "indexer_type": "torznab",
        "host_url": "http://indexer.local",
        "enabled": True,
    })

    with patch("trackseerr.acquisition_coordinator.get_indexer_driver", return_value=mock_indexer_driver):
        search_res = coordinator.search_all_indexers(artist="Daft Punk", title="Discovery", db=test_db)
        assert len(search_res) == 1
        assert search_res[0].download_id == "cand-good"


# ===========================================================================
# Test 4: AcquisitionWorker Imports and Upserts Native Catalog
# ===========================================================================

def test_acquisition_worker_imports_and_populates_native_catalog(test_db: Database, workspace_dirs):
    """Verifies that an imported file immediately creates LibraryArtist, LibraryAlbum, LibraryTrack, and LibraryFile."""
    downloads_dir, music_dir = workspace_dirs

    # Configure media management to native mode and point root to music_dir
    settings = test_db.get_media_management_settings()
    settings["root_folder_path"] = str(music_dir)
    settings["library_mode"] = "native"
    settings["staging_folder_path"] = str(downloads_dir)
    test_db.update_media_management_settings(settings)

    # Create download client
    client = test_db.create_download_client(
        DownloadClientConfig(
            id="client-qbit-1",
            name="Test Qbit",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://qbit:8080",
        )
    )

    # Create audio file in downloads staging
    download_folder = downloads_dir / "Pink_Floyd_Time"
    download_folder.mkdir(parents=True, exist_ok=True)
    audio_file = download_folder / "04_Time.flac"
    write_flac(audio_file)

    # Insert active download
    test_db.create_active_download(
        ActiveDownload(
            id="dl-pf-time",
            client_id="client-qbit-1",
            title="Pink Floyd - Time [FLAC 24bit]",
            artist="Pink Floyd",
            item_type="track",
            status=DownloadStatus.COMPLETED.value,
            download_hash="hash-pf-time",
            size_bytes=len("dummy binary audio content"),
        )
    )

    mock_driver = MagicMock()
    mock_driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value,
        "progress": 100.0,
        "size_bytes": len("dummy binary audio content"),
        "source_path": str(audio_file),
    }

    mock_meta = {
        "artist": "Pink Floyd",
        "title": "Time",
        "album": "The Dark Side of the Moon",
        "year": 1973,
        "codec": "FLAC",
        "bitrate": 1411,
        "sample_rate": 96000,
        "bits_per_sample": 24,
        "duration": 425.0,
        "track_number": 4,
        "disc_number": 1,
        "total_discs": 1,
        "total_tracks": 10,
        "file_path": str(audio_file),
    }

    worker = AcquisitionWorker()
    worker.staging_dir = str(downloads_dir)

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
        with patch("trackseerr.acquisition_worker.inspect_audio_file", return_value=mock_meta):
            stats = worker.poll_once(db=test_db, staging_dir=str(downloads_dir))
            assert stats["completed"] == 1
            assert stats["imported"] == 1

    # Verify download marked IMPORTED
    updated_dl = test_db.get_active_download("dl-pf-time")
    assert updated_dl is not None
    assert updated_dl["status"] == DownloadStatus.IMPORTED.value

    # Verify LibraryArtist was created
    artist = test_db.get_library_artist_by_name("Pink Floyd")
    assert artist is not None
    assert artist["name"] == "Pink Floyd"
    assert artist["monitored"] is True

    # Verify LibraryAlbum was created
    album = test_db.get_library_album_by_title(artist["id"], "The Dark Side of the Moon")
    assert album is not None
    assert album["title"] == "The Dark Side of the Moon"
    assert album["year"] == 1973

    # Verify LibraryTrack was created
    track = test_db.get_library_track_by_title(album["id"], "Time", track_number=4)
    assert track is not None
    assert track["title"] == "Time"
    assert track["track_number"] == 4
    assert track["disc_number"] == 1

    # Verify LibraryFile was created and linked to track
    lib_file = test_db.get_library_file_for_track(track["id"])
    assert lib_file is not None
    assert lib_file["codec"] == "FLAC"
    assert lib_file["bits_per_sample"] == 24
    assert lib_file["quality_name"] == "FLAC 24bit"
    assert lib_file["cutoff_met"] is True
    assert Path(lib_file["file_path"]).exists()


# ===========================================================================
# Test 5: Failing Downloads Automatically Add Release to Blocklist
# ===========================================================================

def test_failing_download_automatically_blocklists(test_db: Database, workspace_dirs):
    """Verifies that failed transfers (driver error or no audio in staging) are blacklisted."""
    downloads_dir, _ = workspace_dirs

    test_db.create_download_client(
        DownloadClientConfig(
            id="client-sab-1",
            name="Test SAB",
            driver_type=DownloadDriverType.SABNZBD,
            host_url="http://sabnzbd:8080",
        )
    )

    # 1. Download fails in driver
    test_db.create_active_download(
        ActiveDownload(
            id="dl-fail-1",
            client_id="client-sab-1",
            title="Corrupted Audio Release [MP3]",
            artist="The Beatles",
            item_type="album",
            status=DownloadStatus.DOWNLOADING.value,
            download_hash="hash-fail-1",
        )
    )

    mock_driver = MagicMock()
    mock_driver.get_status.return_value = {
        "status": DownloadStatus.FAILED.value,
        "error_message": "Unpack error: corrupted RAR volume",
    }

    worker = AcquisitionWorker()
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
        stats = worker.poll_once(db=test_db, staging_dir=str(downloads_dir))
        assert stats["failed"] == 1

    # Verify release is now blocklisted
    assert test_db.is_blocklisted(release_title="Corrupted Audio Release [MP3]") is True
    assert test_db.is_blocklisted(info_hash="hash-fail-1") is True

    # 2. Download completes but contains no audio files
    test_db.create_active_download(
        ActiveDownload(
            id="dl-noaudio-2",
            client_id="client-sab-1",
            title="Fake Release No Audio",
            artist="Radiohead",
            item_type="album",
            status=DownloadStatus.COMPLETED.value,
            download_hash="hash-noaudio-2",
        )
    )

    mock_driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value,
        "source_path": str(downloads_dir / "nonexistent_folder"),
    }

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
        stats2 = worker.poll_once(db=test_db, staging_dir=str(downloads_dir))
        assert stats2["failed"] == 1

    assert test_db.is_blocklisted(release_title="Fake Release No Audio") is True
    assert test_db.is_blocklisted(info_hash="hash-noaudio-2") is True


# ===========================================================================
# Test 6: WantedBacklogWorker Finds Monitored Missing Catalog Tracks
# ===========================================================================

def test_wanted_backlog_worker_catalog_missing_tracks_sweep(test_db: Database):
    """Verifies that WantedBacklogWorker discovers monitored missing catalog tracks and dispatches grabs."""
    # Ensure native library mode
    settings = test_db.get_media_management_settings()
    settings["library_mode"] = "native"
    test_db.update_media_management_settings(settings)

    # Seed monitored artist, album, and track (no file)
    art = test_db.upsert_library_artist(
        LibraryArtist(id="art-1", name="Led Zeppelin", clean_name="led zeppelin", monitored=True)
    )
    alb = test_db.upsert_library_album(
        LibraryAlbum(id="alb-1", artist_id=art["id"], title="Led Zeppelin IV", clean_title="led zeppelin iv", year=1971, monitored=True)
    )
    trk = test_db.upsert_library_track(
        LibraryTrack(
            id="trk-stairway",
            album_id=alb["id"],
            artist_id=art["id"],
            title="Stairway to Heaven",
            clean_title="stairway to heaven",
            track_number=4,
            disc_number=1,
            monitored=True,
        )
    )

    # Verify get_monitored_missing_catalog_tracks finds this track
    missing = test_db.get_monitored_missing_catalog_tracks(limit=10)
    assert len(missing) == 1
    assert missing[0]["track_id"] == "trk-stairway"
    assert missing[0]["artist_name"] == "Led Zeppelin"
    assert missing[0]["track_title"] == "Stairway to Heaven"

    # Run backlog worker sweep with mocked search_and_grab
    backlog = WantedBacklogWorker()
    backlog.pace_delay = 0.0

    mock_grab_result = {
        "success": True,
        "download_id": "dl-grabbed-stairway",
        "release": "Led Zeppelin - Stairway to Heaven [FLAC]",
        "score": 900,
    }

    with patch("trackseerr.backlog_worker.acquisition_coordinator.search_and_grab", return_value=mock_grab_result) as mock_grab:
        stats = backlog.poll_once(db=test_db)
        assert stats["items_checked"] >= 1
        assert stats["items_grabbed"] >= 1

        # Check call arguments
        mock_grab.assert_called_once()
        call_kwargs = mock_grab.call_args.kwargs
        assert call_kwargs["artist"] == "Led Zeppelin"
        assert call_kwargs["title"] == "Stairway to Heaven"
        assert call_kwargs["album"] == "Led Zeppelin IV"
        assert call_kwargs["track_id"] == "trk-stairway"
        assert call_kwargs["album_id"] == "alb-1"


# ===========================================================================
# Test 7: WantedBacklogWorker Handles Cutoff-Unmet Catalog Tracks
# ===========================================================================

def test_wanted_backlog_worker_catalog_cutoff_unmet_sweep(test_db: Database):
    """Verifies that cutoff-unmet catalog tracks trigger quality upgrade grabs."""
    settings = test_db.get_media_management_settings()
    settings["library_mode"] = "native"
    settings["enable_quality_upgrades"] = 1
    test_db.update_media_management_settings(settings)

    art = test_db.upsert_library_artist(
        LibraryArtist(id="art-2", name="Nirvana", monitored=True)
    )
    alb = test_db.upsert_library_album(
        LibraryAlbum(id="alb-2", artist_id=art["id"], title="Nevermind", year=1991, monitored=True)
    )
    trk = test_db.upsert_library_track(
        LibraryTrack(
            id="trk-lithium",
            album_id=alb["id"],
            artist_id=art["id"],
            title="Lithium",
            track_number=5,
            monitored=True,
        )
    )

    # Add file with cutoff_met = False (e.g. MP3 192)
    test_db.upsert_library_file(
        LibraryFile(
            id="fil-lithium-low",
            track_id=trk["id"],
            file_path="/music/Nirvana/Nevermind/05 Lithium.mp3",
            relative_path="Nevermind/05 Lithium.mp3",
            codec="MP3",
            quality_name="MP3 192",
            size_bytes=4_000_000,
            cutoff_met=False,
        )
    )

    unmet = test_db.get_cutoff_unmet_catalog_tracks(limit=10)
    assert len(unmet) == 1
    assert unmet[0]["track_id"] == "trk-lithium"
    assert unmet[0]["quality_name"] == "MP3 192"

    backlog = WantedBacklogWorker()
    backlog.pace_delay = 0.0

    mock_grab_result = {
        "success": True,
        "download_id": "dl-grabbed-upgrade",
        "release": "Nirvana - Lithium [FLAC]",
        "score": 900,
    }

    with patch("trackseerr.backlog_worker.acquisition_coordinator.search_and_grab", return_value=mock_grab_result) as mock_grab:
        stats = backlog.poll_once(db=test_db)
        assert stats["items_checked"] == 1
        assert stats["items_grabbed"] == 1
        call_kwargs = mock_grab.call_args.kwargs
        assert call_kwargs["track_id"] == "trk-lithium"
        assert call_kwargs["min_score"] is not None


# ===========================================================================
# Test 8: Blocklist REST API Endpoints with Admin RBAC
# ===========================================================================

def test_blocklist_api_routes(app_and_client, seeded_users, test_db, test_config):
    """Tests GET /api/acquisition/blocklist and DELETE /api/acquisition/blocklist/{id}."""
    _, client = app_and_client
    admin = seeded_users["admin"]
    alice = seeded_users["alice"]

    admin_headers = _auth_headers(admin, test_db, test_config)
    alice_headers = _auth_headers(alice, test_db, test_config)

    # 1. Seed blocklist item
    item = test_db.add_to_blocklist(
        source_title="Bad Torrent Title",
        artist="Bad Artist",
        info_hash="aabbccddeeff00112233445566778899aabbccdd",
        reason="Stalled torrent",
    )
    bl_id = item["id"]

    # 2. Unauthenticated requests are rejected (401)
    resp = client.get("/api/acquisition/blocklist")
    assert resp.status_code == 401

    resp = client.delete(f"/api/acquisition/blocklist/{bl_id}")
    assert resp.status_code == 401

    # 3. Non-admin user is forbidden (403)
    resp = client.get("/api/acquisition/blocklist", headers=alice_headers)
    assert resp.status_code == 403

    resp = client.delete(f"/api/acquisition/blocklist/{bl_id}", headers=alice_headers)
    assert resp.status_code == 403

    # 4. Admin lists blocklist (200)
    resp = client.get("/api/acquisition/blocklist", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["id"] == bl_id
    assert data[0]["source_title"] == "Bad Torrent Title"

    # 5. Admin removes item from blocklist (200)
    resp = client.delete(f"/api/acquisition/blocklist/{bl_id}", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    # 6. Check item is gone from DB
    assert test_db.is_blocklisted(release_title="Bad Torrent Title") is False

    # 7. Removing again returns 404
    resp = client.delete(f"/api/acquisition/blocklist/{bl_id}", headers=admin_headers)
    assert resp.status_code == 404


# ===========================================================================
# Test 9: Library Mode Lidarr Skips Native Catalog Ingestion
# ===========================================================================

def test_lidarr_mode_skips_native_catalog_upsert(test_db: Database, workspace_dirs):
    """Verifies that in library_mode == 'lidarr', native catalog tables are untouched."""
    downloads_dir, music_dir = workspace_dirs

    settings = test_db.get_media_management_settings()
    settings["root_folder_path"] = str(music_dir)
    settings["library_mode"] = "lidarr"
    settings["staging_folder_path"] = str(downloads_dir)
    test_db.update_media_management_settings(settings)

    test_db.create_download_client(
        DownloadClientConfig(
            id="client-lidarr-1",
            name="Lidarr Client",
            driver_type=DownloadDriverType.LIDARR,
            host_url="http://lidarr:8686",
        )
    )

    test_db.create_active_download(
        ActiveDownload(
            id="dl-lidarr-item",
            client_id="client-lidarr-1",
            title="Lidarr Delegated Item",
            artist="Lidarr Artist",
            status=DownloadStatus.COMPLETED.value,
        )
    )

    mock_driver = MagicMock()
    mock_driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value,
        "progress": 100.0,
    }

    worker = AcquisitionWorker()
    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
        stats = worker.poll_once(db=test_db, staging_dir=str(downloads_dir))
        assert stats["imported"] == 1

    # Verify no native library records were created
    artists = test_db.list_library_artists()
    assert len(artists) == 0
    albums = test_db.list_library_albums()
    assert len(albums) == 0
    tracks = test_db.list_library_tracks()
    assert len(tracks) == 0
    files = test_db.list_library_files()
    assert len(files) == 0

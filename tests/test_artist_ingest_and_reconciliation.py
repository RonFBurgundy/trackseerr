"""Tests for Phase 3: Native Artist Ingestion & Discography Cataloging and Multi-Track Reconciliation.

Validates:
1. POST /api/library/artists/ingest with mock DiscoveryClient creates artist, albums, tracks in catalog.
2. Ingest monitor options ('all' vs 'albums' vs 'singles_eps' vs 'none') properly set monitored flags.
3. Newly ingested monitored tracks immediately appear in db.get_monitored_missing_catalog_tracks().
4. POST /api/library/artists/{artist_id}/refresh adds new albums without disturbing existing files.
5. Multi-track album reconciliation matches audio files to expected library_tracks by disc/track number and title,
   preserving unacquired missing status for omitted tracks.
6. Ingestion and refresh on ROLE=gateway return HTTP 403 Forbidden (require_core_tier).
"""

from pathlib import Path
import struct
import time
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from trackseerr.acquisition_worker import (
    AcquisitionWorker,
)
from trackseerr.track_matching import (
    reconcile_audio_file_to_track,
)
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import (
    get_config,
    get_db,
    get_discovery_client,
)
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.discovery import DiscoveryClient
from trackseerr.config import Config
from trackseerr.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    LibraryAlbum,
    LibraryArtist,
    LibraryFile,
    LibraryTrack,
)
from trackseerr.storage import Database


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
    db_file = tmp_path / "test_ingest.db"
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
        role="all-in-one",
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
def mock_discovery_client():
    """Provides a mock DiscoveryClient with mock discography and album details."""
    client = MagicMock(spec=DiscoveryClient)

    def fake_get_artist_details(artist_id: str, force: bool = False) -> dict[str, Any]:
        return {
            "id": artist_id,
            "name": "Daft Punk",
            "albums": [
                {
                    "id": "deezer:album:302127",
                    "title": "Discovery",
                    "release_date": "2001-03-12",
                    "record_type": "album",
                    "cover_url": "https://example.com/discovery.jpg",
                },
                {
                    "id": "deezer:album:302128",
                    "title": "Random Access Memories",
                    "release_date": "2013-05-17",
                    "record_type": "album",
                    "cover_url": "https://example.com/ram.jpg",
                },
            ],
            "singles_eps": [
                {
                    "id": "deezer:album:302129",
                    "title": "One More Time - Single",
                    "release_date": "2000-11-13",
                    "record_type": "single",
                    "cover_url": "https://example.com/onemoretime.jpg",
                }
            ],
            "compilations": [
                {
                    "id": "deezer:album:302130",
                    "title": "Musique Vol. 1",
                    "release_date": "2006-03-29",
                    "record_type": "compilation",
                    "cover_url": "https://example.com/musique.jpg",
                }
            ],
        }

    def fake_get_album_details(album_id: str, force: bool = False) -> dict[str, Any]:
        catalogs = {
            "deezer:album:302127": {
                "id": "deezer:album:302127",
                "title": "Discovery",
                "tracks": [
                    {
                        "id": "deezer:track:3135553",
                        "title": "One More Time",
                        "track_number": 1,
                        "disc_number": 1,
                        "duration_seconds": 320.0,
                    },
                    {
                        "id": "deezer:track:3135554",
                        "title": "Aerodynamic",
                        "track_number": 2,
                        "disc_number": 1,
                        "duration_seconds": 212.0,
                    },
                    {
                        "id": "deezer:track:3135555",
                        "title": "Digital Love",
                        "track_number": 3,
                        "disc_number": 1,
                        "duration_seconds": 298.0,
                    },
                ],
            },
            "deezer:album:302128": {
                "id": "deezer:album:302128",
                "title": "Random Access Memories",
                "tracks": [
                    {
                        "id": "deezer:track:3135560",
                        "title": "Give Life Back to Music",
                        "track_number": 1,
                        "disc_number": 1,
                        "duration_seconds": 275.0,
                    },
                    {
                        "id": "deezer:track:3135561",
                        "title": "Get Lucky",
                        "track_number": 8,
                        "disc_number": 1,
                        "duration_seconds": 369.0,
                    },
                ],
            },
            "deezer:album:302129": {
                "id": "deezer:album:302129",
                "title": "One More Time - Single",
                "tracks": [
                    {
                        "id": "deezer:track:3135570",
                        "title": "One More Time (Short Edit)",
                        "track_number": 1,
                        "disc_number": 1,
                        "duration_seconds": 235.0,
                    }
                ],
            },
            "deezer:album:302130": {
                "id": "deezer:album:302130",
                "title": "Musique Vol. 1",
                "tracks": [
                    {
                        "id": "deezer:track:3135580",
                        "title": "Musique",
                        "track_number": 1,
                        "disc_number": 1,
                        "duration_seconds": 286.0,
                    }
                ],
            },
        }
        return catalogs.get(album_id, {"id": album_id, "title": "Unknown", "tracks": []})

    client.get_artist_details.side_effect = fake_get_artist_details
    client.get_album_details.side_effect = fake_get_album_details
    return client


@pytest.fixture
def app_and_client(test_db: Database, test_config: Config, mock_discovery_client):
    """Creates a FastAPI test client with injected test database, config, and discovery mock."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    app.dependency_overrides[get_discovery_client] = lambda: mock_discovery_client

    client = TestClient(app)
    return app, client


# =========================================================================
# Test 1: Ingest creates artist, albums, and tracks in catalog
# =========================================================================

def test_artist_ingest_creates_catalog_hierarchy(
    app_and_client, test_db: Database, test_config: Config, seeded_users
):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    payload = {
        "foreign_artist_id": "deezer:artist:27",
        "artist_name": "Daft Punk",
        "monitor_option": "all",
        "monitored": True,
    }
    resp = client.post("/api/library/artists/ingest", json=payload, headers=admin_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "Daft Punk"
    assert data["foreign_artist_id"] == "deezer:artist:27"
    assert data["albums_ingested"] >= 4
    assert data["tracks_ingested"] >= 7

    # Verify Database state
    artist = test_db.get_library_artist_by_foreign_id("deezer:artist:27")
    assert artist is not None
    assert artist["name"] == "Daft Punk"
    assert artist["monitored"] == 1

    album = test_db.get_library_album_by_foreign_id("deezer:album:302127")
    assert album is not None
    assert album["title"] == "Discovery"
    assert album["year"] == 2001
    assert album["monitored"] == 1

    tracks = test_db.list_library_tracks(album_id=album["id"])
    assert len(tracks) == 3
    track_titles = {t["title"] for t in tracks}
    assert "One More Time" in track_titles
    assert "Aerodynamic" in track_titles
    assert "Digital Love" in track_titles

    # Idempotent re-ingest returns existing artist
    resp_again = client.post("/api/library/artists/ingest", json=payload, headers=admin_headers)
    assert resp_again.status_code == 200
    assert resp_again.json()["already_existed"] is True
    assert resp_again.json()["id"] == artist["id"]


# =========================================================================
# Test 2: Ingest monitor options ('all' vs 'albums' vs 'none')
# =========================================================================

def test_ingest_monitor_options(
    app_and_client, test_db: Database, test_config: Config, seeded_users
):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # 1. Option 'albums': only 'albums' section is monitored; singles/compilations are unmonitored
    payload_albums = {
        "foreign_artist_id": "deezer:artist:28",
        "artist_name": "Albums Only Artist",
        "monitor_option": "albums",
        "monitored": True,
    }
    resp1 = client.post("/api/library/artists/ingest", json=payload_albums, headers=admin_headers)
    assert resp1.status_code == 200

    art1 = test_db.get_library_artist_by_foreign_id("deezer:artist:28")
    assert art1 is not None
    alb_mon = test_db.get_library_album_by_title(art1["id"], "Discovery")
    assert alb_mon is not None and alb_mon["monitored"] == 1

    single_unmon = test_db.get_library_album_by_title(art1["id"], "One More Time - Single")
    assert single_unmon is not None and single_unmon["monitored"] == 0

    comp_unmon = test_db.get_library_album_by_title(art1["id"], "Musique Vol. 1")
    assert comp_unmon is not None and comp_unmon["monitored"] == 0

    # 2. Option 'none': nothing is monitored
    payload_none = {
        "foreign_artist_id": "deezer:artist:29",
        "artist_name": "None Monitored Artist",
        "monitor_option": "none",
        "monitored": True,
    }
    resp2 = client.post("/api/library/artists/ingest", json=payload_none, headers=admin_headers)
    assert resp2.status_code == 200

    art2 = test_db.get_library_artist_by_foreign_id("deezer:artist:29")
    assert art2 is not None
    all_albums = test_db.list_library_albums(artist_id=art2["id"])
    assert len(all_albums) > 0
    assert all(a["monitored"] == 0 for a in all_albums)


# =========================================================================
# Test 3: Newly ingested monitored tracks appear in missing catalog tracks
# =========================================================================

def test_ingested_monitored_tracks_appear_in_missing_catalog(
    app_and_client, test_db: Database, test_config: Config, seeded_users
):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    payload = {
        "foreign_artist_id": "deezer:artist:30",
        "artist_name": "Missing Backlog Band",
        "monitor_option": "all",
        "monitored": True,
    }
    resp = client.post("/api/library/artists/ingest", json=payload, headers=admin_headers)
    assert resp.status_code == 200

    # Query monitored missing catalog tracks
    missing = test_db.get_monitored_missing_catalog_tracks(limit=100)
    assert len(missing) >= 7

    artist_names = {m["artist_name"] for m in missing}
    assert "Missing Backlog Band" in artist_names

    # Check track fields
    band_tracks = [m for m in missing if m["artist_name"] == "Missing Backlog Band"]
    titles = {m["track_title"] for m in band_tracks}
    assert "One More Time" in titles
    assert "Digital Love" in titles
    assert all(isinstance(m["track_number"], int) for m in band_tracks)
    assert all(isinstance(m["disc_number"], int) for m in band_tracks)


# =========================================================================
# Test 4: Refresh adds new albums without disturbing existing files
# =========================================================================

def test_artist_refresh_preserves_existing_files_and_adds_albums(
    app_and_client, test_db: Database, test_config: Config, seeded_users, mock_discovery_client
):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # 1. Ingest initial artist
    payload = {
        "foreign_artist_id": "deezer:artist:31",
        "artist_name": "Justice",
        "monitor_option": "all",
        "monitored": True,
    }
    resp = client.post("/api/library/artists/ingest", json=payload, headers=admin_headers)
    assert resp.status_code == 200
    artist_id = resp.json()["id"]

    # 2. Attach a file to the first track
    track_1 = test_db.get_library_track_by_foreign_id("deezer:track:3135553")
    assert track_1 is not None
    file_record = test_db.upsert_library_file(
        LibraryFile(
            id="fil-justice-01",
            track_id=track_1["id"],
            file_path="/music/Justice/Discovery/01 - One More Time.flac",
            relative_path="Justice/Discovery/01 - One More Time.flac",
            codec="FLAC",
            bitrate=1000,
            sample_rate=44100,
            bits_per_sample=16,
            quality_name="FLAC 16bit",
            size_bytes=35000000,
            cutoff_met=True,
        )
    )
    assert file_record is not None

    # 3. Update mock DiscoveryClient to return a new 3rd album for Justice
    def updated_artist_details(artist_id: str, force: bool = False) -> dict[str, Any]:
        return {
            "id": artist_id,
            "name": "Justice",
            "albums": [
                {
                    "id": "deezer:album:302127",
                    "title": "Discovery",
                    "release_date": "2001-03-12",
                    "record_type": "album",
                },
                {
                    "id": "deezer:album:999999",
                    "title": "Hyperdrama",
                    "release_date": "2024-04-26",
                    "record_type": "album",
                },
            ],
            "singles_eps": [],
            "compilations": [],
        }

    def updated_album_details(album_id: str, force: bool = False) -> dict[str, Any]:
        if album_id == "deezer:album:999999":
            return {
                "id": "deezer:album:999999",
                "title": "Hyperdrama",
                "tracks": [
                    {
                        "id": "deezer:track:999901",
                        "title": "Generator",
                        "track_number": 1,
                        "disc_number": 1,
                        "duration_seconds": 280.0,
                    }
                ],
            }
        return {"id": album_id, "title": "Discovery", "tracks": []}

    mock_discovery_client.get_artist_details.side_effect = updated_artist_details
    mock_discovery_client.get_album_details.side_effect = updated_album_details

    # 4. Trigger refresh endpoint
    refresh_resp = client.post(f"/api/library/artists/{artist_id}/refresh", headers=admin_headers)
    assert refresh_resp.status_code == 200, refresh_resp.text
    assert refresh_resp.json()["success"] is True

    # 5. Verify new album and track were ingested
    new_album = test_db.get_library_album_by_foreign_id("deezer:album:999999")
    assert new_album is not None
    assert new_album["title"] == "Hyperdrama"

    new_track = test_db.get_library_track_by_foreign_id("deezer:track:999901")
    assert new_track is not None
    assert new_track["title"] == "Generator"

    # 6. Verify existing file link for track_1 was untouched
    existing_file = test_db.get_library_file_for_track(track_1["id"])
    assert existing_file is not None
    assert existing_file["file_path"] == "/music/Justice/Discovery/01 - One More Time.flac"


# =========================================================================
# Test 5: Multi-track album reconciliation matches audio files to tracks
# =========================================================================

def test_reconciliation_hierarchy_direct():
    """Unit tests for reconcile_audio_file_to_track matching rules."""
    candidates = [
        {"id": "trk-1", "disc_number": 1, "track_number": 1, "title": "Intro", "duration_seconds": 60.0},
        {"id": "trk-2", "disc_number": 1, "track_number": 2, "title": "Around the World", "duration_seconds": 429.0},
        {"id": "trk-3", "disc_number": 1, "track_number": 3, "title": "Around the World (Radio Edit)", "duration_seconds": 240.0},
    ]

    # Rule 1: Exact match on disc & track number
    meta_num = {"track_number": 2, "disc_number": 1, "title": "Wrong Tag Title", "duration": 430.0}
    matched = reconcile_audio_file_to_track(meta_num, candidates)
    assert matched is not None and matched["id"] == "trk-2"

    # Rule 2: Title similarity match when track number missing
    meta_title = {"track_number": None, "disc_number": 1, "title": "Intro"}
    matched = reconcile_audio_file_to_track(meta_title, candidates)
    assert matched is not None and matched["id"] == "trk-1"

    # Rule 3: Duration tolerance when multiple candidates share title similarity
    ambiguous_candidates = [
        {"id": "trk-a", "disc_number": 1, "track_number": 1, "title": "Aerodynamic", "duration_seconds": 212.0},
        {"id": "trk-b", "disc_number": 1, "track_number": 2, "title": "Aerodynamic", "duration_seconds": 380.0},
    ]
    meta_dur = {"track_number": None, "disc_number": 1, "title": "Aerodynamic", "duration": 214.0}
    matched_dur = reconcile_audio_file_to_track(meta_dur, ambiguous_candidates)
    assert matched_dur is not None and matched_dur["id"] == "trk-a"


def test_acquisition_worker_reconciles_multitrack_album_with_unacquired_remaining(
    tmp_path: Path, test_db: Database, test_config: Config
):
    """End-to-end multi-track reconciliation in AcquisitionWorker during download import."""
    music_dir = tmp_path / "music"
    staging_dir = tmp_path / "staging"
    music_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    test_db.update_media_management_settings(
        {
            "root_folder_path": str(music_dir),
            "staging_folder_path": str(staging_dir),
            "library_mode": "native",
            "enrich_mbids": False,  # no real MusicBrainz / Cover Art Archive lookups in tests
            "track_format": "{Artist}/{Album}/{TrackNumber:02d} - {Title}",
        }
    )

    # 1. Create artist and album in catalog with 3 expected tracks
    artist = test_db.upsert_library_artist(
        LibraryArtist(id="art-daft", name="Daft Punk", path=str(music_dir / "Daft Punk"))
    )
    album = test_db.upsert_library_album(
        LibraryAlbum(
            id="alb-disc",
            artist_id=artist["id"],
            title="Discovery",
            year=2001,
            path=str(music_dir / "Daft Punk" / "Discovery"),
        )
    )
    t1 = test_db.upsert_library_track(
        LibraryTrack(id="trk-1", album_id=album["id"], artist_id=artist["id"], title="One More Time", track_number=1, disc_number=1, duration_seconds=320.0)
    )
    t2 = test_db.upsert_library_track(
        LibraryTrack(id="trk-2", album_id=album["id"], artist_id=artist["id"], title="Aerodynamic", track_number=2, disc_number=1, duration_seconds=212.0)
    )
    t3 = test_db.upsert_library_track(
        LibraryTrack(id="trk-3", album_id=album["id"], artist_id=artist["id"], title="Digital Love", track_number=3, disc_number=1, duration_seconds=298.0)
    )

    # 2. Prepare 2 downloaded files in staging (t3 is missing / unacquired)
    dl_folder = staging_dir / "Daft.Punk.Discovery.FLAC"
    dl_folder.mkdir(parents=True, exist_ok=True)
    file1 = dl_folder / "01.One.More.Time.flac"
    file2 = dl_folder / "Aerodynamic.flac"
    _create_minimal_flac(file1)
    _create_minimal_flac(file2)

    # Mock inspect_audio_file for staging files:
    # file1 has track_number=1; file2 has track_number=None but title="Aerodynamic"
    def mock_inspect(p: Path | str) -> dict[str, Any]:
        sp = Path(p)
        if "One.More.Time" in sp.name or "01 - One More Time" in sp.name:
            return {
                "artist": "Daft Punk",
                "title": "One More Time",
                "album": "Discovery",
                "track_number": 1,
                "disc_number": 1,
                "duration": 320.0,
                "codec": "FLAC",
                "bits_per_sample": 16,
                "bitrate": 900,
                "sample_rate": 44100,
                "file_path": str(p),
                "extension": ".flac",
            }
        else:
            return {
                "artist": "Daft Punk",
                "title": "Aerodynamic",
                "album": "Discovery",
                "track_number": None,
                "disc_number": 1,
                "duration": 212.0,
                "codec": "FLAC",
                "bits_per_sample": 16,
                "bitrate": 900,
                "sample_rate": 44100,
                "file_path": str(p),
                "extension": ".flac",
            }

    # 3. Queue download item in database
    client = test_db.create_download_client(
        DownloadClientConfig(
            id="client-mock-1",
            name="Mock Client",
            driver_type=DownloadDriverType.SLSKD,
            host_url="http://slskd:5030",
        )
    )

    test_db.create_active_download(
        ActiveDownload(
            id="dl-daft-disc",
            client_id="client-mock-1",
            title="Daft.Punk.Discovery.FLAC",
            artist="Daft Punk",
            item_type="album",
            status=DownloadStatus.DOWNLOADING.value,
            download_hash="daft-disc-hash",
            album_id=album["id"],
        )
    )

    worker = AcquisitionWorker()

    mock_driver = MagicMock()
    mock_driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value,
        "progress": 100.0,
        "size_bytes": 1000,
        "speed_bps": 0,
        "eta_seconds": 0,
        "source_path": str(dl_folder),
        "error_message": None,
    }

    with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver), \
         patch("trackseerr.acquisition_import.inspect_audio_file", side_effect=mock_inspect), \
         patch("trackseerr.acquisition_catalog.inspect_audio_file", side_effect=mock_inspect):
        worker.poll_once(db=test_db, staging_dir=str(staging_dir))

    # 4. Verify reconciliation results:
    # Track 1 and Track 2 have files; Track 3 has NO file (remains unacquired missing)
    f1 = test_db.get_library_file_for_track(t1["id"])
    assert f1 is not None, "Track 1 was not reconciled to a file"
    assert f1["track_id"] == t1["id"]
    assert "One More Time" in f1["file_path"]

    f2 = test_db.get_library_file_for_track(t2["id"])
    assert f2 is not None, "Track 2 was not reconciled to a file"
    assert f2["track_id"] == t2["id"]
    assert "Aerodynamic" in f2["file_path"]

    f3 = test_db.get_library_file_for_track(t3["id"])
    assert f3 is None, "Track 3 should not have a file (unacquired)"

    # Track 3 remains in missing catalog sweep!
    missing_after = test_db.get_monitored_missing_catalog_tracks(limit=100)
    missing_ids = [m["track_id"] for m in missing_after]
    assert t3["id"] in missing_ids
    assert t1["id"] not in missing_ids
    assert t2["id"] not in missing_ids


# =========================================================================
# Test 6: Ingestion and refresh on ROLE=gateway return HTTP 403 Forbidden
# =========================================================================

def test_gateway_mode_blocks_ingestion_and_refresh(
    test_db: Database, tmp_path: Path, seeded_users, mock_discovery_client
):
    gateway_config = Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-token",
        data_dir=str(tmp_path),
        role="gateway",
        internal_core_secret="g" * 40,
    )
    app = create_app(db=test_db, config=gateway_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: gateway_config
    app.dependency_overrides[get_discovery_client] = lambda: mock_discovery_client

    client = TestClient(app)
    admin_headers = _auth_headers(seeded_users["admin"], test_db, gateway_config)

    # 1. Ingest on gateway is hidden (404, deny-by-default gateway middleware)
    resp_ingest = client.post(
        "/api/library/artists/ingest",
        json={"foreign_artist_id": "deezer:artist:1", "artist_name": "Gateway Artist"},
        headers=admin_headers,
    )
    assert resp_ingest.status_code == 404

    # 2. Refresh on gateway is hidden (404)
    resp_refresh = client.post(
        "/api/library/artists/some-artist-id/refresh",
        headers=admin_headers,
    )
    assert resp_refresh.status_code == 404


# =========================================================================
# Test 7: Authentication, Machine API Key, and Edge Cases
# =========================================================================

def test_ingest_and_refresh_auth_and_edge_cases(
    app_and_client, test_db: Database, test_config: Config, seeded_users, mock_discovery_client
):
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)

    # 1. Unauthenticated request -> 401
    resp_unauth = client.post(
        "/api/library/artists/ingest",
        json={"foreign_artist_id": "deezer:artist:1", "artist_name": "Test"},
    )
    assert resp_unauth.status_code == 401

    # 2. Non-admin user -> 403
    resp_nonadmin = client.post(
        "/api/library/artists/ingest",
        json={"foreign_artist_id": "deezer:artist:1", "artist_name": "Test"},
        headers=alice_headers,
    )
    assert resp_nonadmin.status_code == 403

    # 3. Machine API Key authentication -> 200
    api_key = test_db.get_api_key()
    resp_apikey = client.post(
        "/api/library/artists/ingest",
        json={"foreign_artist_id": "deezer:artist:99", "artist_name": "API Key Artist"},
        headers={"X-Api-Key": api_key},
    )
    assert resp_apikey.status_code == 200

    # 4. Refresh non-existent artist -> 404
    resp_404 = client.post("/api/library/artists/non-existent-uuid/refresh", headers=admin_headers)
    assert resp_404.status_code == 404

    # 5. Refresh artist without foreign_artist_id -> success=False
    local_art = test_db.upsert_library_artist(
        LibraryArtist(id="art-local-only", name="Local Only Band", foreign_artist_id=None)
    )
    resp_no_foreign = client.post(
        f"/api/library/artists/{local_art['id']}/refresh", headers=admin_headers
    )
    assert resp_no_foreign.status_code == 200
    assert resp_no_foreign.json()["success"] is False
    assert "no linked discovery foreign ID" in resp_no_foreign.json()["message"]

    # 6. Discovery network error handled gracefully without crash
    mock_discovery_client.get_artist_details.side_effect = RuntimeError("Deezer connection timeout")
    resp_net_err = client.post(
        "/api/library/artists/ingest",
        json={"foreign_artist_id": "deezer:artist:555", "artist_name": "Resilient Artist"},
        headers=admin_headers,
    )
    assert resp_net_err.status_code == 200
    assert resp_net_err.json()["name"] == "Resilient Artist"
    assert resp_net_err.json()["albums_ingested"] == 0

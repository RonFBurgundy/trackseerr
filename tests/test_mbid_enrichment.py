"""Comprehensive unit and integration tests for MusicBrainz (MBID) metadata enrichment,
Mutagen tag reading/writing, Scanner ingestion, Collections CRUD, and AcoustID fingerprinting.
"""

from pathlib import Path
import struct
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from trackseerr.acquisition_worker import AcquisitionWorker
from trackseerr.import_files import _is_safe_cover_url
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.config import Config
from trackseerr.library import (
    fingerprint_audio_file,
    inspect_audio_file,
    write_audio_tags,
)
from trackseerr.library_scanner import LibraryScanner
from trackseerr.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    LibraryAlbum,
    LibraryArtist,
    LibraryCollection,
    LibraryTrack,
    MusicRequest,
    RequestStatus,
)
from trackseerr.storage import Database

pytestmark = pytest.mark.real_mbid_enricher

def _create_minimal_flac(path: Path) -> None:
    """Writes a valid minimal FLAC stream header recognizable by Mutagen."""
    sr_chan_bps_samples = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    streaminfo = (
        struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00")
        + sr_chan_bps_samples
        + b"\x00" * 16
    )
    header = b"fLaC\x80\x00\x00\x22" + streaminfo
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header)


def _create_minimal_mp3(path: Path) -> None:
    """Writes a valid minimal MPEG-1 Layer 3 audio frame."""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frame * 2)


@pytest.fixture
def test_db(tmp_path: Path):
    """Provides an isolated disk-backed Database for testing."""
    db_file = tmp_path / "test_mbid.db"
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
    """Seeds admin and standard user in test database."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    user = test_db.upsert_user("user-1", "regular_user", "user@example.com", is_admin=False)
    return {"admin": admin, "user": user}


@pytest.fixture
def app_and_client(test_db: Database, test_config: Config):
    """Creates a FastAPI test client with injected dependencies."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    client = TestClient(app)
    return app, client


def _auth_headers(user: dict[str, Any], test_db: Database, config: Config) -> dict[str, str]:
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
# 1. TestMbidEnricher
# ===========================================================================

class TestMbidEnricher:
    """Unit tests for MbidEnricherClient REST resolution, caching, and SSRF URLs."""

    def test_lookup_track_by_isrc(self):
        client = MbidEnricherClient(base_url="https://api.brainzmash.org")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "recordings": [
                {
                    "id": "trk-1111",
                    "title": "Around the World",
                    "artist-credit": [{"artist": {"id": "art-2222", "name": "Daft Punk"}}],
                    "releases": [
                        {
                            "id": "rel-3333",
                            "title": "Homework",
                            "release-group": {"id": "rg-4444"},
                        }
                    ],
                }
            ]
        }

        with patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            result = client.lookup_track_mbids("Daft Punk", "Homework", "Around the World", isrc="USSUB9600001")

        assert result is not None
        assert result["musicbrainz_trackid"] == "trk-1111"
        assert result["musicbrainz_artistid"] == "art-2222"
        assert result["musicbrainz_albumid"] == "rel-3333"
        assert result["musicbrainz_releasegroupid"] == "rg-4444"
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert "isrc:USSUB9600001" in kwargs["params"]["query"]

    def test_lookup_track_by_metadata_fallback(self):
        client = MbidEnricherClient(base_url="https://api.brainzmash.org")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "recordings": [
                {
                    "id": "trk-9999",
                    "title": "Get Lucky",
                    "artist-credit": [{"artist": {"id": "art-8888", "name": "Daft Punk"}}],
                    "releases": [
                        {
                            "id": "rel-7777",
                            "title": "Random Access Memories",
                            "release-group": {"id": "rg-6666"},
                        }
                    ],
                }
            ]
        }

        with patch.object(client._session, "get", return_value=mock_resp):
            result = client.lookup_track_mbids("Daft Punk", "Random Access Memories", "Get Lucky")

        assert result is not None
        assert result["musicbrainz_trackid"] == "trk-9999"
        assert result["musicbrainz_artistid"] == "art-8888"
        assert result["musicbrainz_albumid"] == "rel-7777"
        assert result["musicbrainz_releasegroupid"] == "rg-6666"

    def test_caching_identical_call_makes_no_second_http_request(self):
        client = MbidEnricherClient(base_url="https://api.brainzmash.org")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "recordings": [
                {
                    "id": "trk-cached",
                    "artist-credit": [{"artist": {"id": "art-cached"}}],
                    "releases": [{"id": "rel-cached", "release-group": {"id": "rg-cached"}}],
                }
            ]
        }

        with patch.object(client._session, "get", return_value=mock_resp) as mock_get:
            res1 = client.lookup_track_mbids("Artist", "Album", "Song")
            res2 = client.lookup_track_mbids("Artist", "Album", "Song")

        assert res1 == res2
        assert mock_get.call_count == 1

    def test_graceful_failures_on_http_errors_and_timeout(self):
        client = MbidEnricherClient(base_url="https://api.brainzmash.org")

        # 404
        r404 = MagicMock(status_code=404)
        with patch.object(client._session, "get", return_value=r404):
            assert client.lookup_artist_mbid("Unknown Artist") is None

        # 429
        r429 = MagicMock(status_code=429)
        with patch.object(client._session, "get", return_value=r429):
            assert client.lookup_album_mbids("Artist", "Album") is None

        # Timeout
        with patch.object(client._session, "get", side_effect=requests.Timeout("Timeout")):
            assert client.lookup_track_mbids("Artist", "Album", "Title") is None

    def test_lookup_artist_and_album_mbids(self):
        client = MbidEnricherClient(base_url="https://api.brainzmash.org")

        art_resp = MagicMock(status_code=200)
        art_resp.json.return_value = {"artists": [{"id": "art-found", "name": "Radiohead"}]}
        with patch.object(client._session, "get", return_value=art_resp):
            art_id = client.lookup_artist_mbid("Radiohead")
            assert art_id == "art-found"

        alb_resp = MagicMock(status_code=200)
        alb_resp.json.return_value = {
            "release-groups": [
                {
                    "id": "rg-found",
                    "artist-credit": [{"artist": {"id": "art-found"}}],
                }
            ]
        }
        with patch.object(client._session, "get", return_value=alb_resp):
            alb_mbids = client.lookup_album_mbids("Radiohead", "OK Computer")
            assert alb_mbids == {"mb_release_group_id": "rg-found", "mb_artist_id": "art-found"}

    def test_get_cover_art_url(self):
        client = MbidEnricherClient()
        assert client.get_cover_art_url(release_group_id="rg-123") == "https://coverartarchive.org/release-group/rg-123/front-500"
        assert client.get_cover_art_url(release_id="rel-456") == "https://coverartarchive.org/release/rel-456/front-500"
        assert client.get_cover_art_url() is None

    def test_get_artist_discography(self):
        client = MbidEnricherClient(base_url="https://api.brainzmash.org")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "release-groups": [
                {
                    "id": "rg-studio",
                    "title": "OK Computer",
                    "primary-type": "Album",
                    "secondary-types": [],
                    "first-release-date": "1997-05-21",
                },
                {
                    "id": "rg-ep",
                    "title": "Airbag / How Am I Driving?",
                    "primary-type": "EP",
                    "secondary-types": [],
                    "first-release-date": "1998-04-21",
                },
                {
                    "id": "rg-single",
                    "title": "Paranoid Android",
                    "primary-type": "Single",
                    "secondary-types": [],
                    "first-release-date": "1997-05-26",
                },
                {
                    "id": "rg-live",
                    "title": "I Might Be Wrong: Live Recordings",
                    "primary-type": "Album",
                    "secondary-types": ["Live"],
                    "first-release-date": "2001-11-12",
                },
                {
                    "id": "rg-comp",
                    "title": "Radiohead: The Best Of",
                    "primary-type": "Album",
                    "secondary-types": ["Compilation"],
                    "first-release-date": "2008-06-02",
                },
            ]
        }
        with patch.object(client._session, "get", return_value=mock_resp):
            disco = client.get_artist_discography("art-radiohead")

        assert len(disco) == 5
        types_by_id = {item["id"]: item["album_type"] for item in disco}
        years_by_id = {item["id"]: item["year"] for item in disco}
        covers_by_id = {item["id"]: item["cover_url"] for item in disco}

        assert types_by_id["rg-studio"] == "album"
        assert years_by_id["rg-studio"] == 1997
        assert covers_by_id["rg-studio"] == "https://coverartarchive.org/release-group/rg-studio/front-500"

        assert types_by_id["rg-ep"] == "ep"
        assert years_by_id["rg-ep"] == 1998

        assert types_by_id["rg-single"] == "single"
        assert years_by_id["rg-single"] == 1997

        assert types_by_id["rg-live"] == "live"
        assert years_by_id["rg-live"] == 2001

        assert types_by_id["rg-comp"] == "compilation"
        assert years_by_id["rg-comp"] == 2008

    def test_get_artist_details(self):
        client = MbidEnricherClient(base_url="https://api.brainzmash.org")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "id": "art-radiohead",
            "name": "Radiohead",
            "country": "GB",
            "disambiguation": "British alternative rock band",
            "genres": [{"name": "alternative rock", "count": 10}],
            "tags": [{"name": "art rock", "count": 5}],
            "relations": [
                {
                    "type": "wikidata",
                    "url": {"resource": "https://www.wikidata.org/wiki/Q44190"},
                }
            ],
        }
        with patch.object(client._session, "get", return_value=mock_resp):
            details = client.get_artist_details("art-radiohead")

        assert details is not None
        assert details["country"] == "GB"
        assert "alternative rock" in details["genres"]
        assert "art rock" in details["genres"]
        assert details["urls"].get("wikidata") == "https://www.wikidata.org/wiki/Q44190"



# ===========================================================================
# 2. TestMutagenMbidTagging
# ===========================================================================

class TestMutagenMbidTagging:
    """Verifies reading and writing Vorbis comments (FLAC) and ID3 TXXX/UFID frames (MP3)."""

    def test_flac_mbid_tags(self, tmp_path: Path):
        flac_file = tmp_path / "song.flac"
        _create_minimal_flac(flac_file)

        tags = {
            "title": "Around the World",
            "artist": "Daft Punk",
            "album": "Homework",
            "musicbrainz_artistid": "art-flac-111",
            "musicbrainz_albumid": "alb-flac-222",
            "musicbrainz_releasegroupid": "rg-flac-333",
            "musicbrainz_trackid": "trk-flac-444",
            "isrc": "USSUB9600001",
        }

        ok = write_audio_tags(flac_file, tags)
        assert ok is True

        audio = FLAC(str(flac_file))
        assert audio["musicbrainz_artistid"] == ["art-flac-111"]
        assert audio["musicbrainz_albumid"] == ["alb-flac-222"]
        assert audio["musicbrainz_releasegroupid"] == ["rg-flac-333"]
        assert audio["musicbrainz_trackid"] == ["trk-flac-444"]
        assert audio["isrc"] == ["USSUB9600001"]

        inspected = inspect_audio_file(flac_file)
        assert inspected["musicbrainz_artistid"] == "art-flac-111"
        assert inspected["musicbrainz_albumid"] == "alb-flac-222"
        assert inspected["musicbrainz_releasegroupid"] == "rg-flac-333"
        assert inspected["musicbrainz_trackid"] == "trk-flac-444"
        assert inspected["isrc"] == "USSUB9600001"

    def test_mp3_mbid_tags(self, tmp_path: Path):
        mp3_file = tmp_path / "song.mp3"
        _create_minimal_mp3(mp3_file)

        tags = {
            "title": "Get Lucky",
            "artist": "Daft Punk",
            "album": "Random Access Memories",
            "musicbrainz_artistid": "art-mp3-111",
            "musicbrainz_albumid": "alb-mp3-222",
            "musicbrainz_releasegroupid": "rg-mp3-333",
            "musicbrainz_trackid": "trk-mp3-444",
            "isrc": "USAT21300259",
        }

        ok = write_audio_tags(mp3_file, tags)
        assert ok is True

        audio = MP3(str(mp3_file))
        assert audio.tags.get("TXXX:MusicBrainz Artist Id").text == ["art-mp3-111"]
        assert audio.tags.get("TXXX:MusicBrainz Album Id").text == ["alb-mp3-222"]
        assert audio.tags.get("TXXX:MusicBrainz Release Group Id").text == ["rg-mp3-333"]
        assert audio.tags.get("UFID:http://musicbrainz.org").data == b"trk-mp3-444"
        assert audio.tags.get("TSRC").text == ["USAT21300259"]

        inspected = inspect_audio_file(mp3_file)
        assert inspected["musicbrainz_artistid"] == "art-mp3-111"
        assert inspected["musicbrainz_albumid"] == "alb-mp3-222"
        assert inspected["musicbrainz_releasegroupid"] == "rg-mp3-333"
        assert inspected["musicbrainz_trackid"] == "trk-mp3-444"
        assert inspected["isrc"] == "USAT21300259"


# ===========================================================================
# 3. TestAcquisitionEnrichment
# ===========================================================================

class TestAcquisitionEnrichment:
    """Verifies AcquisitionWorker post-processing enrichment and non-blocking resilience."""

    def test_ssrf_cover_whitelisting(self):
        assert _is_safe_cover_url("https://coverartarchive.org/release-group/123/front-500") is True
        assert _is_safe_cover_url("https://archive.org/download/item/cover.jpg") is True
        assert _is_safe_cover_url("https://ia800100.us.archive.org/cover.jpg") is True
        assert _is_safe_cover_url("http://127.0.0.1/evil.jpg") is False

    def test_acquisition_worker_enriches_mbids(self, test_db: Database, tmp_path: Path):
        music_dir = tmp_path / "music"
        staging_dir = tmp_path / "staging"
        music_dir.mkdir(parents=True)
        staging_dir.mkdir(parents=True)

        test_db.update_media_management_settings({
            "root_folder_path": str(music_dir),
            "staging_folder_path": str(staging_dir),
            "write_audio_tags": True,
            "enrich_mbids": True,
        })

        client_cfg = DownloadClientConfig(
            id="client-1",
            name="Mock Client",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://localhost:8080",
        )
        test_db.create_download_client(client_cfg)

        download_item = ActiveDownload(
            id="dl-enrich-1",
            request_id=None,
            client_id="client-1",
            download_hash="hash-1",
            title="Daft Punk - One More Time",
            artist="Daft Punk",
            status=DownloadStatus.DOWNLOADING.value,
        )
        test_db.create_active_download(download_item)

        dl_folder = staging_dir / "dl-enrich-1"
        dl_folder.mkdir(parents=True)
        flac_file = dl_folder / "01 - One More Time.flac"
        _create_minimal_flac(flac_file)

        mock_driver = MagicMock()
        mock_driver.get_status.return_value = {
            "status": DownloadStatus.COMPLETED.value,
            "progress": 100.0,
            "source_path": str(flac_file),
        }

        resolved_mbids = {
            "musicbrainz_artistid": "mb-art-daft",
            "musicbrainz_albumid": "mb-alb-discovery",
            "musicbrainz_releasegroupid": "mb-rg-discovery",
            "musicbrainz_trackid": "mb-trk-onemoretime",
        }

        with (
            patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver),
            patch("trackseerr.clients.mbid_enricher.MbidEnricherClient.lookup_track_mbids", return_value=resolved_mbids) as mock_lookup,
        ):
            worker = AcquisitionWorker()
            stats = worker.poll_once(db=test_db, staging_dir=str(staging_dir))

        assert stats["completed"] == 1
        assert stats["imported"] == 1
        mock_lookup.assert_called()

        dl_state = test_db.get_active_download("dl-enrich-1")
        assert dl_state["status"] == DownloadStatus.IMPORTED.value

        # Inspect moved file in music library
        imported_files = list(music_dir.glob("**/*.flac"))
        assert len(imported_files) == 1
        tags = inspect_audio_file(imported_files[0])
        assert tags["musicbrainz_artistid"] == "mb-art-daft"
        assert tags["musicbrainz_trackid"] == "mb-trk-onemoretime"

    def test_acquisition_worker_continues_on_enricher_timeout(self, test_db: Database, tmp_path: Path):
        music_dir = tmp_path / "music"
        staging_dir = tmp_path / "staging"
        music_dir.mkdir(parents=True)
        staging_dir.mkdir(parents=True)

        test_db.update_media_management_settings({
            "root_folder_path": str(music_dir),
            "staging_folder_path": str(staging_dir),
            "write_audio_tags": True,
            "enrich_mbids": True,
        })

        client_cfg = DownloadClientConfig(
            id="client-2",
            name="Mock Client",
            driver_type=DownloadDriverType.QBITTORRENT,
            host_url="http://localhost:8080",
        )
        test_db.create_download_client(client_cfg)

        download_item = ActiveDownload(
            id="dl-timeout-1",
            request_id=None,
            client_id="client-2",
            download_hash="hash-2",
            title="Track Timeout",
            artist="Artist Timeout",
            status=DownloadStatus.DOWNLOADING.value,
        )
        test_db.create_active_download(download_item)

        dl_folder = staging_dir / "dl-timeout-1"
        dl_folder.mkdir(parents=True)
        flac_file = dl_folder / "01 - Song.flac"
        _create_minimal_flac(flac_file)

        mock_driver = MagicMock()
        mock_driver.get_status.return_value = {
            "status": DownloadStatus.COMPLETED.value,
            "progress": 100.0,
            "source_path": str(flac_file),
        }

        with (
            patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver),
            patch("trackseerr.clients.mbid_enricher.MbidEnricherClient.lookup_track_mbids", side_effect=Exception("Mirror timed out")),
        ):
            worker = AcquisitionWorker()
            stats = worker.poll_once(db=test_db, staging_dir=str(staging_dir))

        # Import must still succeed cleanly
        assert stats["completed"] == 1
        assert stats["imported"] == 1
        dl_state = test_db.get_active_download("dl-timeout-1")
        assert dl_state["status"] == DownloadStatus.IMPORTED.value
        imported_files = list(music_dir.glob("**/*.flac"))
        assert len(imported_files) == 1


# ===========================================================================
# 4. TestScannerMbidIngestion
# ===========================================================================

class TestScannerMbidIngestion:
    """Verifies LibraryScanner persists disk tags into library_artists, library_albums, and library_tracks."""

    def test_scanner_persists_mbids_and_local_cover(self, test_db: Database, tmp_path: Path):
        music_dir = tmp_path / "music"
        album_dir = music_dir / "Daft Punk" / "Discovery"
        album_dir.mkdir(parents=True)

        track_file = album_dir / "01 - One More Time.flac"
        _create_minimal_flac(track_file)

        cover_file = album_dir / "cover.jpg"
        cover_file.write_bytes(b"fake-cover-bytes")

        tags_to_embed = {
            "title": "One More Time",
            "artist": "Daft Punk",
            "album": "Discovery",
            "musicbrainz_artistid": "mb-art-daft",
            "musicbrainz_albumid": "mb-alb-discovery",
            "musicbrainz_releasegroupid": "mb-rg-discovery",
            "musicbrainz_trackid": "mb-trk-onemoretime",
            "isrc": "USSUB0000001",
        }
        write_audio_tags(track_file, tags_to_embed)

        scanner = LibraryScanner()
        status = scanner.scan(test_db, root_folder=str(music_dir))

        assert status["status"] == "completed"
        assert status["artists_created"] == 1
        assert status["albums_created"] == 1
        assert status["tracks_created"] == 1

        # Check Artist
        artist = test_db.get_library_artist_by_name("Daft Punk")
        assert artist is not None
        assert artist["mbid"] == "mb-art-daft"
        assert artist["foreign_artist_id"] == "musicbrainz:artist:mb-art-daft"

        # Check Album
        album = test_db.get_library_album_by_title(artist["id"], "Discovery")
        assert album is not None
        assert album["mb_release_group_id"] == "mb-rg-discovery"
        assert album["mb_release_id"] == "mb-alb-discovery"
        assert album["cover_url"] == f"/api/library/albums/{album['id']}/cover"

        # Check Track
        track = test_db.get_library_track_by_title(album["id"], "One More Time", track_number=1)
        assert track is not None
        assert track["mb_recording_id"] == "mb-trk-onemoretime"
        assert track["isrc"] == "USSUB0000001"


# ===========================================================================
# 5. TestCollectionsStorageAndAPI
# ===========================================================================

class TestCollectionsStorageAndAPI:
    """Verifies database storage and REST API CRUD for library_collections and library_collection_albums."""

    def test_database_collections_crud(self, test_db: Database):
        # 1. Create collection
        col = LibraryCollection(
            id="col-1",
            name="Daft Punk Discography",
            summary="All studio releases",
            poster_url="https://example.com/poster.jpg",
            monitored=True,
        )
        created = test_db.upsert_library_collection(col)
        assert created["name"] == "Daft Punk Discography"
        assert created["monitored"] is True

        # 2. Get and List
        fetched = test_db.get_library_collection("col-1")
        assert fetched is not None
        assert fetched["summary"] == "All studio releases"

        cols = test_db.list_library_collections()
        assert len(cols) == 1

        # 3. Add Album to Collection
        artist = test_db.upsert_library_artist(LibraryArtist(id="art-c", name="Artist C"))
        album1 = test_db.upsert_library_album(LibraryAlbum(id="alb-c1", artist_id=artist["id"], title="Album 1"))
        album2 = test_db.upsert_library_album(LibraryAlbum(id="alb-c2", artist_id=artist["id"], title="Album 2"))

        test_db.add_album_to_collection("col-1", album1["id"], order_index=1)
        test_db.add_album_to_collection("col-1", album2["id"], order_index=2)

        albums = test_db.get_collection_albums("col-1")
        assert len(albums) == 2
        assert albums[0]["id"] == "alb-c1"
        assert albums[0]["order_index"] == 1
        assert albums[1]["id"] == "alb-c2"
        assert albums[1]["order_index"] == 2

        # Collection list should reflect album_count
        updated_cols = test_db.list_library_collections()
        assert updated_cols[0]["album_count"] == 2

        # 4. Remove Album from Collection
        test_db.remove_album_from_collection("col-1", album1["id"])
        albums_after = test_db.get_collection_albums("col-1")
        assert len(albums_after) == 1
        assert albums_after[0]["id"] == "alb-c2"

        # 5. Delete Collection
        deleted = test_db.delete_library_collection("col-1")
        assert deleted is True
        assert test_db.get_library_collection("col-1") is None

    def test_collections_rest_api(self, app_and_client, test_db: Database, test_config: Config, seeded_users):
        _, client = app_and_client
        admin = seeded_users["admin"]
        headers = _auth_headers(admin, test_db, test_config)

        # 1. Create collection via POST
        create_payload = {
            "name": "Electronic Masterpieces",
            "summary": "Best electronic albums",
            "poster_url": "https://example.com/art.jpg",
            "monitored": True,
        }
        res = client.post("/api/library/collections", json=create_payload, headers=headers)
        assert res.status_code == 200
        col_data = res.json()
        col_id = col_data["id"]
        assert col_data["name"] == "Electronic Masterpieces"

        # 2. List collections via GET
        res = client.get("/api/library/collections", headers=headers)
        assert res.status_code == 200
        items = res.json()
        assert len(items) >= 1
        assert any(c["id"] == col_id for c in items)

        # 3. Add album to collection
        artist = test_db.upsert_library_artist(LibraryArtist(id="art-api", name="Kraftwerk"))
        album = test_db.upsert_library_album(LibraryAlbum(id="alb-api", artist_id=artist["id"], title="Trans-Europe Express"))

        add_res = client.post(
            f"/api/library/collections/{col_id}/albums",
            json={"album_id": album["id"], "order_index": 0},
            headers=headers,
        )
        assert add_res.status_code == 200
        assert add_res.json()["success"] is True

        # 4. Get collection detail
        detail_res = client.get(f"/api/library/collections/{col_id}", headers=headers)
        assert detail_res.status_code == 200
        detail = detail_res.json()
        assert len(detail["albums"]) == 1
        assert detail["albums"][0]["title"] == "Trans-Europe Express"

        # 5. Remove album from collection
        del_album_res = client.delete(f"/api/library/collections/{col_id}/albums/{album['id']}", headers=headers)
        assert del_album_res.status_code == 200

        # 6. Delete collection
        del_col_res = client.delete(f"/api/library/collections/{col_id}", headers=headers)
        assert del_col_res.status_code == 200

        # Verify 404 after deletion
        get_deleted = client.get(f"/api/library/collections/{col_id}", headers=headers)
        assert get_deleted.status_code == 404

    def test_get_artist_payload_includes_mbid_and_image_fallbacks(self, app_and_client, test_db: Database, test_config: Config, seeded_users):
        _, client = app_and_client
        admin = seeded_users["admin"]
        headers = _auth_headers(admin, test_db, test_config)

        # Seed artist with MBID, banner, bio, genres, country
        art = test_db.upsert_library_artist(
            LibraryArtist(
                id="art-rich",
                name="Rich Artist",
                mbid="mbid-art-123",
                banner_url="https://example.com/banner.jpg",
                image_url="https://example.com/image.jpg",
                bio="Artist biography",
                genres="Electronic, Synthpop",
                country="FR",
            )
        )

        res = client.get(f"/api/library/artists/{art['id']}", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["mbid"] == "mbid-art-123"
        assert data["banner_url"] == "https://example.com/banner.jpg"
        assert data["image_url"] == "https://example.com/image.jpg"
        assert data["bio"] == "Artist biography"
        assert data["genres"] == "Electronic, Synthpop"
        assert data["country"] == "FR"

    def test_collections_with_preview_covers(self, test_db: Database):
        # 1. Create collection
        col = LibraryCollection(id="col-preview", name="Test Cover Grid", monitored=True)
        test_db.upsert_library_collection(col)

        # 2. Insert albums with cover URLs
        art = test_db.upsert_library_artist(LibraryArtist(id="art-p", name="Artist P"))
        alb1 = test_db.upsert_library_album(
            LibraryAlbum(
                id="alb-p1",
                artist_id=art["id"],
                title="Album 1",
                cover_url="https://caa.org/front1.jpg",
            )
        )
        alb2 = test_db.upsert_library_album(
            LibraryAlbum(
                id="alb-p2",
                artist_id=art["id"],
                title="Album 2",
                cover_url="https://caa.org/front2.jpg",
            )
        )

        test_db.add_album_to_collection("col-preview", alb1["id"], order_index=1)
        test_db.add_album_to_collection("col-preview", alb2["id"], order_index=2)

        cols = test_db.list_library_collections()
        target = next((c for c in cols if c["id"] == "col-preview"), None)
        assert target is not None
        assert target["album_count"] == 2
        assert target["preview_covers"] == [
            "https://caa.org/front1.jpg",
            "https://caa.org/front2.jpg",
        ]

    def test_refresh_artist_via_musicbrainz(
        self,
        app_and_client,
        test_db: Database,
        test_config: Config,
        seeded_users,
    ):
        _, client = app_and_client
        admin = seeded_users["admin"]
        headers = _auth_headers(admin, test_db, test_config)

        art = test_db.upsert_library_artist(
            LibraryArtist(id="art-mb-refresh", name="The Cure", mbid="mbid-cure")
        )

        mock_details = {
            "id": "mbid-cure",
            "name": "The Cure",
            "country": "GB",
            "disambiguation": "English rock band formed in 1978",
            "genres": ["gothic rock", "post-punk"],
            "urls": {},
        }
        mock_disco = [
            {
                "id": "rg-disintegration",
                "title": "Disintegration",
                "primary_type": "Album",
                "secondary_types": [],
                "first_release_date": "1989-05-02",
                "year": 1989,
                "album_type": "album",
                "cover_url": "https://coverartarchive.org/release-group/rg-disintegration/front-500",
            },
            {
                "id": "rg-boys-dont-cry",
                "title": "Boys Don't Cry",
                "primary_type": "Single",
                "secondary_types": [],
                "first_release_date": "1979-06-12",
                "year": 1979,
                "album_type": "single",
                "cover_url": "https://coverartarchive.org/release-group/rg-boys-dont-cry/front-500",
            },
        ]

        with patch("trackseerr.clients.mbid_enricher.MbidEnricherClient.get_artist_details", return_value=mock_details), \
             patch("trackseerr.clients.mbid_enricher.MbidEnricherClient.get_artist_discography_result", return_value=(mock_disco, True)):
            res = client.post(f"/api/library/artists/{art['id']}/refresh", headers=headers)

        assert res.status_code == 200
        body = res.json()
        assert body["success"] is True

        refreshed_artist = test_db.get_library_artist(art["id"])
        assert refreshed_artist is not None
        assert refreshed_artist["country"] == "GB"
        assert "gothic rock" in refreshed_artist["genres"]
        assert "English rock band" in refreshed_artist["bio"]

        albums = test_db.list_library_albums(artist_id=art["id"])
        assert len(albums) == 2
        titles = {a["title"]: a for a in albums}
        assert "Disintegration" in titles
        assert titles["Disintegration"]["mb_release_group_id"] == "rg-disintegration"
        assert titles["Disintegration"]["year"] == 1989
        assert titles["Disintegration"]["album_type"] == "album"
        assert "https://coverartarchive.org/release-group/rg-disintegration/front-500" in titles["Disintegration"]["cover_url"]

        assert "Boys Don't Cry" in titles
        assert titles["Boys Don't Cry"]["album_type"] == "single"



# ===========================================================================
# 6. TestAcoustIDEndpoint
# ===========================================================================

class TestAcoustIDEndpoint:
    """Verifies on-demand AcoustID fingerprinting endpoint behavior and error cases."""

    def test_fingerprint_file_not_found_returns_400(self, app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path):
        _, client = app_and_client
        admin = seeded_users["admin"]
        headers = _auth_headers(admin, test_db, test_config)
        test_db.update_media_management_settings({"root_folder_path": str(tmp_path)})

        res = client.post(
            "/api/library/manual-import/fingerprint",
            json={"file_path": str(tmp_path / "nonexistent.flac")},
            headers=headers,
        )
        assert res.status_code == 400
        assert "does not exist" in res.json()["detail"].lower()

    def test_fingerprint_path_traversal_returns_400(self, app_and_client, test_db: Database, test_config: Config, seeded_users):
        _, client = app_and_client
        admin = seeded_users["admin"]
        headers = _auth_headers(admin, test_db, test_config)

        res = client.post(
            "/api/library/manual-import/fingerprint",
            json={"file_path": "../../../etc/passwd"},
            headers=headers,
        )
        assert res.status_code == 400

    def test_fingerprint_unavailable_returns_clean_false(self, app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path):
        _, client = app_and_client
        admin = seeded_users["admin"]
        headers = _auth_headers(admin, test_db, test_config)
        test_db.update_media_management_settings({"root_folder_path": str(tmp_path)})

        flac_file = tmp_path / "song.flac"
        _create_minimal_flac(flac_file)

        # When acoustid is not installed or returns None
        with patch("trackseerr.api.routes.library.manual_import.fingerprint_audio_file", return_value=None):
            res = client.post(
                "/api/library/manual-import/fingerprint",
                json={"file_path": str(flac_file)},
                headers=headers,
            )

        assert res.status_code == 200
        body = res.json()
        assert body["success"] is False
        assert "unavailable" in body["message"].lower()

    def test_fingerprint_success_mocked(self, app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path):
        _, client = app_and_client
        admin = seeded_users["admin"]
        headers = _auth_headers(admin, test_db, test_config)
        test_db.update_media_management_settings({"root_folder_path": str(tmp_path)})

        flac_file = tmp_path / "song.flac"
        _create_minimal_flac(flac_file)

        matched_data = {
            "score": 0.98,
            "recording_id": "rec-match-123",
            "title": "Around the World",
            "artist": "Daft Punk",
        }

        with patch("trackseerr.api.routes.library.manual_import.fingerprint_audio_file", return_value=matched_data):
            res = client.post(
                "/api/library/manual-import/fingerprint",
                json={"file_path": str(flac_file)},
                headers=headers,
            )

        assert res.status_code == 200
        body = res.json()
        assert body["success"] is True
        assert body["fingerprint"]["recording_id"] == "rec-match-123"
        assert body["fingerprint"]["title"] == "Around the World"

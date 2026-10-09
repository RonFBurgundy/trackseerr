"""Unit and integration tests for tag writing, artwork embedding, and acquisition worker import."""

import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from trackseerr.acquisition_worker import AcquisitionWorker, _is_safe_cover_url
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.library import (
    embed_album_artwork,
    inspect_audio_file,
    write_audio_tags,
)
from trackseerr.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    MusicRequest,
    RequestStatus,
)
from trackseerr.storage import Database


def _create_minimal_flac(path: Path) -> None:
    """Writes a valid minimal FLAC file stream header."""
    sr_chan_bps_samples = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    streaminfo = (
        struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00")
        + sr_chan_bps_samples
        + b"\x00" * 16
    )
    header = b"fLaC\x80\x00\x00\x22" + streaminfo
    path.write_bytes(header)


def _create_minimal_mp3(path: Path) -> None:
    """Writes a valid minimal MPEG-1 Layer 3 audio frame."""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    path.write_bytes(frame * 2)


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    """Provides a test Config pointing to tmp_path."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        data_dir=str(tmp_path),
    )


@pytest.fixture
def seeded_users(test_db):
    """Seeds admin and regular users into test DB."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice}


@pytest.fixture
def app_and_client(test_db, test_config):
    """Creates a FastAPI test client with injected test database and config."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config

    client = TestClient(app)
    return app, client


def _auth_headers(user: dict, test_db: Database, config: Config) -> dict[str, str]:
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Unit Tests: Audio Tag Writer & Artwork Embedding
# ---------------------------------------------------------------------------


class TestAudioTagWriter:
    """Tests for write_audio_tags and embed_album_artwork across audio containers."""

    def test_write_flac_tags_and_artwork(self, tmp_path):
        flac_path = tmp_path / "track.flac"
        _create_minimal_flac(flac_path)

        tags = {
            "title": "Around the World",
            "artist": "Daft Punk",
            "album": "Homework",
            "album_artist": "Daft Punk",
            "date": "1997",
            "tracknumber": 7,
            "totaltracks": 16,
            "discnumber": 1,
            "totaldiscs": 1,
        }
        cover_bytes = b"fake-flac-cover-art-data"

        ok = write_audio_tags(flac_path, tags=tags, cover_art_bytes=cover_bytes)
        assert ok is True

        audio = FLAC(str(flac_path))
        assert audio["title"] == ["Around the World"]
        assert audio["artist"] == ["Daft Punk"]
        assert audio["album"] == ["Homework"]
        assert audio["albumartist"] == ["Daft Punk"]
        assert audio["date"] == ["1997"]
        assert audio["tracknumber"] == ["7"]
        assert audio["totaltracks"] == ["16"]
        assert audio["discnumber"] == ["1"]
        assert audio["totaldiscs"] == ["1"]

        assert len(audio.pictures) == 1
        pic = audio.pictures[0]
        assert pic.type == 3
        assert pic.mime == "image/jpeg"
        assert pic.data == cover_bytes

    def test_embed_artwork_flac_preserves_tags(self, tmp_path):
        flac_path = tmp_path / "track.flac"
        _create_minimal_flac(flac_path)

        write_audio_tags(
            flac_path,
            tags={"title": "Harder Better Faster Stronger", "artist": "Daft Punk"},
            cover_art_bytes=b"original-art",
        )

        new_art = b"\x89PNG\r\n\x1a\nfake-png-bytes"
        ok = embed_album_artwork(flac_path, new_art)
        assert ok is True

        audio = FLAC(str(flac_path))
        assert audio["title"] == ["Harder Better Faster Stronger"]
        assert audio["artist"] == ["Daft Punk"]
        assert len(audio.pictures) == 1
        assert audio.pictures[0].mime == "image/png"
        assert audio.pictures[0].data == new_art

    def test_write_mp3_tags_and_artwork(self, tmp_path):
        mp3_path = tmp_path / "track.mp3"
        _create_minimal_mp3(mp3_path)

        tags = {
            "title": "Karma Police",
            "artist": "Radiohead",
            "album": "OK Computer",
            "albumartist": "Radiohead",
            "date": "1997-05-21",
            "tracknumber": 6,
            "totaltracks": 12,
            "discnumber": 1,
            "totaldiscs": 1,
        }
        cover_bytes = b"fake-mp3-cover-bytes"

        ok = write_audio_tags(mp3_path, tags=tags, cover_art_bytes=cover_bytes)
        assert ok is True

        audio = MP3(str(mp3_path))
        assert audio.tags is not None
        assert audio.tags.get("TIT2").text == ["Karma Police"]
        assert audio.tags.get("TPE1").text == ["Radiohead"]
        assert audio.tags.get("TALB").text == ["OK Computer"]
        assert audio.tags.get("TPE2").text == ["Radiohead"]
        assert str(audio.tags.get("TDRC").text[0]) == "1997-05-21"
        assert audio.tags.get("TRCK").text == ["6/12"]
        assert audio.tags.get("TPOS").text == ["1/1"]

        apic = audio.tags.get("APIC:Cover") or audio.tags.getall("APIC")[0]
        assert apic.type == 3
        assert apic.mime == "image/jpeg"
        assert apic.data == cover_bytes

    def test_embed_artwork_mp3_preserves_tags(self, tmp_path):
        mp3_path = tmp_path / "track.mp3"
        _create_minimal_mp3(mp3_path)

        write_audio_tags(mp3_path, tags={"title": "No Surprises", "artist": "Radiohead"})
        new_art = b"new-mp3-cover"
        ok = embed_album_artwork(mp3_path, new_art)
        assert ok is True

        audio = MP3(str(mp3_path))
        assert audio.tags.get("TIT2").text == ["No Surprises"]
        assert audio.tags.get("TPE1").text == ["Radiohead"]
        apics = audio.tags.getall("APIC")
        assert len(apics) == 1
        assert apics[0].data == new_art

    def test_write_m4a_tags_mock(self, tmp_path):
        m4a_file = tmp_path / "song.m4a"
        m4a_file.touch()

        mock_mp4_instance = MagicMock()
        mock_mp4_instance.tags = {}
        mock_mp4_instance.__setitem__ = MagicMock()

        with patch("trackseerr.library.MP4", return_value=mock_mp4_instance):
            tags = {
                "title": "Get Lucky",
                "artist": "Daft Punk",
                "album": "Random Access Memories",
                "album_artist": "Daft Punk",
                "date": "2013",
                "tracknumber": 8,
                "totaltracks": 13,
                "discnumber": 1,
                "totaldiscs": 1,
            }
            png_bytes = b"\x89PNGfake-png-art"
            ok = write_audio_tags(m4a_file, tags=tags, cover_art_bytes=png_bytes)
            assert ok is True

            mock_mp4_instance.__setitem__.assert_any_call("\xa9nam", ["Get Lucky"])
            mock_mp4_instance.__setitem__.assert_any_call("\xa9ART", ["Daft Punk"])
            mock_mp4_instance.__setitem__.assert_any_call("\xa9alb", ["Random Access Memories"])
            mock_mp4_instance.__setitem__.assert_any_call("aART", ["Daft Punk"])
            mock_mp4_instance.__setitem__.assert_any_call("\xa9day", ["2013"])
            mock_mp4_instance.__setitem__.assert_any_call("trkn", [(8, 13)])
            mock_mp4_instance.__setitem__.assert_any_call("disk", [(1, 1)])
            mock_mp4_instance.save.assert_called_once()

    def test_write_ogg_opus_tags_mock(self, tmp_path):
        ogg_file = tmp_path / "song.ogg"
        ogg_file.touch()
        opus_file = tmp_path / "song.opus"
        opus_file.touch()

        mock_ogg = MagicMock()
        mock_ogg.tags = {}
        mock_opus = MagicMock()
        mock_opus.tags = {}

        with patch("trackseerr.library.OggVorbis", return_value=mock_ogg):
            ok = write_audio_tags(
                ogg_file,
                tags={"title": "Song OGG", "artist": "Artist"},
                cover_art_bytes=b"ogg-art",
            )
            assert ok is True
            mock_ogg.save.assert_called_once()

        with patch("trackseerr.library.OggOpus", return_value=mock_opus):
            ok = write_audio_tags(
                opus_file,
                tags={"title": "Song OPUS", "artist": "Artist"},
                cover_art_bytes=b"opus-art",
            )
            assert ok is True
            mock_opus.save.assert_called_once()

    def test_write_tags_error_handling(self, tmp_path):
        # 1. Nonexistent file
        assert write_audio_tags(tmp_path / "ghost.flac", tags={"title": "Test"}) is False

        # 2. Plain text / corrupted file
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("not audio")
        assert write_audio_tags(txt_file, tags={"title": "Test"}) is False

        # 3. Unsupported extension
        pdf_file = tmp_path / "doc.pdf"
        pdf_file.touch()
        assert write_audio_tags(pdf_file, tags={"title": "Test"}) is False

        # 4. Empty image data for embed_album_artwork
        flac_path = tmp_path / "track.flac"
        _create_minimal_flac(flac_path)
        assert embed_album_artwork(flac_path, b"") is False


# ---------------------------------------------------------------------------
# Storage Migration v11 & Media Management Settings CRUD Tests
# ---------------------------------------------------------------------------


class TestStorageMigrationV11:
    """Validates schema migration v11 and getter/setter persistence."""

    def test_migration_v11_defaults(self, test_db):
        settings = test_db.get_media_management_settings()
        assert settings["write_audio_tags"] is True
        assert settings["embed_artwork"] is True
        assert settings["save_cover_art_file"] is True

    def test_update_media_management_tagger_options(self, test_db):
        updated = test_db.update_media_management_settings(
            {
                "write_audio_tags": False,
                "embed_artwork": False,
                "save_cover_art_file": False,
            }
        )
        assert updated["write_audio_tags"] is False
        assert updated["embed_artwork"] is False
        assert updated["save_cover_art_file"] is False

        # Read back afresh from DB
        fetched = test_db.get_media_management_settings()
        assert fetched["write_audio_tags"] is False
        assert fetched["embed_artwork"] is False
        assert fetched["save_cover_art_file"] is False


# ---------------------------------------------------------------------------
# API Tests: Media Management Settings Route
# ---------------------------------------------------------------------------


class TestMediaManagementSettingsAPI:
    """Validates the GET and POST /api/settings/media-management routes with v11 fields."""

    def test_get_media_management_settings(self, app_and_client, seeded_users, test_db, test_config):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        res = client.get("/api/settings/media-management", headers=admin_headers)
        assert res.status_code == 200
        data = res.json()
        assert "settings" in data
        assert data["settings"]["write_audio_tags"] is True
        assert data["settings"]["embed_artwork"] is True
        assert data["settings"]["save_cover_art_file"] is True

    def test_update_media_management_settings(self, app_and_client, seeded_users, test_db, test_config):
        _, client = app_and_client
        admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

        payload = {
            "write_audio_tags": False,
            "embed_artwork": False,
            "save_cover_art_file": False,
        }
        res = client.post("/api/settings/media-management", json=payload, headers=admin_headers)
        assert res.status_code == 200
        updated = res.json()
        assert updated["write_audio_tags"] is False
        assert updated["embed_artwork"] is False
        assert updated["save_cover_art_file"] is False


# ---------------------------------------------------------------------------
# Integration Tests: AcquisitionWorker Tagging & Cover Art Pipeline
# ---------------------------------------------------------------------------


class TestAcquisitionWorkerTaggerImport:
    """Tests the full download completion import pipeline including tag writing and cover.jpg saving."""

    @pytest.fixture
    def staging_and_music(self, tmp_path, test_db):
        staging = tmp_path / "downloads"
        music = tmp_path / "music"
        staging.mkdir()
        music.mkdir()

        settings = test_db.get_media_management_settings()
        settings["staging_folder_path"] = str(staging)
        settings["root_folder_path"] = str(music)
        settings["write_audio_tags"] = True
        settings["embed_artwork"] = True
        settings["save_cover_art_file"] = True
        test_db.update_media_management_settings(settings)

        return staging, music

    def test_completed_transfer_tags_audio_and_writes_cover(
        self, test_db, seeded_users, staging_and_music, tmp_path
    ):
        staging, music = staging_and_music

        # 1. Setup Request and Download Client in DB
        req = test_db.create_request(
            MusicRequest(
                id="req-daft-punk",
                user_id="admin-1",
                item_type="album",
                title="Discovery",
                artist="Daft Punk",
                album="Discovery",
                release_date="2001-03-12",
                cover_url="https://is.mzstatic.com/image/thumb/Music115/cover.jpg",
                status=RequestStatus.PROCESSING.value,
            )
        )

        client = test_db.create_download_client(
            DownloadClientConfig(
                id="client-test-1",
                name="Slskd Tagger",
                driver_type=DownloadDriverType.SLSKD,
                host_url="http://localhost:5030",
            )
        )

        # 2. Create audio file in download staging
        dl_dir = staging / "Daft Punk - Discovery"
        dl_dir.mkdir(parents=True)
        track_flac = dl_dir / "01 - One More Time.flac"
        _create_minimal_flac(track_flac)

        # 3. Create active download in DB
        download = test_db.create_active_download(
            ActiveDownload(
                id="dl-tagger-1",
                request_id="req-daft-punk",
                title="Discovery",
                artist="Daft Punk",
                client_id="client-test-1",
                download_hash="hash-tagger-1",
                status=DownloadStatus.COMPLETED.value,
                source_path=str(track_flac),
            )
        )

        mock_driver = MagicMock()
        mock_driver.get_status.return_value = {
            "status": DownloadStatus.COMPLETED.value,
            "progress": 100.0,
            "source_path": str(track_flac),
            "error_message": None,
        }

        # Mock cover art HTTP fetch
        fake_cover_bytes = b"fake-discovery-album-cover-bytes"
        mock_resp = MagicMock(status_code=200, content=fake_cover_bytes)

        worker = AcquisitionWorker()

        with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
            with patch("httpx.get", return_value=mock_resp) as mock_httpx:
                stats = worker.poll_once(db=test_db, staging_dir=str(staging))
                assert stats["imported"] == 1
                assert stats["failed"] == 0
                mock_httpx.assert_called_once_with(
                    "https://is.mzstatic.com/image/thumb/Music115/cover.jpg",
                    timeout=10.0,
                    follow_redirects=True,
                )

        # 4. Verify request status is AVAILABLE
        updated_req = test_db.get_request("req-daft-punk")
        assert updated_req["status"] == RequestStatus.AVAILABLE.value

        # 5. Verify download status is IMPORTED
        updated_dl = test_db.get_active_download("dl-tagger-1")
        assert updated_dl["status"] == DownloadStatus.IMPORTED.value
        imported_target = Path(updated_dl["target_path"])
        assert imported_target.exists()

        # 6. Verify tags were written and cover art was embedded into the FLAC file
        flac = FLAC(str(imported_target))
        assert flac["artist"] == ["Daft Punk"]
        assert flac["album"] == ["Discovery"]
        assert flac["date"] == ["2001-03-12"]
        assert len(flac.pictures) == 1
        assert flac.pictures[0].data == fake_cover_bytes

        # 7. Verify cover.jpg was written to the destination album folder
        album_dir = imported_target.parent
        cover_file = album_dir / "cover.jpg"
        assert cover_file.exists()
        assert cover_file.read_bytes() == fake_cover_bytes

    def test_acquisition_worker_toggles_disabled(self, test_db, seeded_users, staging_and_music):
        staging, music = staging_and_music

        # Disable tagging and cover file saving
        test_db.update_media_management_settings(
            {
                "write_audio_tags": False,
                "embed_artwork": False,
                "save_cover_art_file": False,
            }
        )

        test_db.create_request(
            MusicRequest(
                id="req-toggle-off",
                user_id="admin-1",
                item_type="album",
                title="Paranoid",
                artist="Black Sabbath",
                album="Paranoid",
                release_date="1970",
                cover_url="https://is.mzstatic.com/image/cover.jpg",
                status=RequestStatus.PROCESSING.value,
            )
        )

        client = test_db.create_download_client(
            DownloadClientConfig(
                id="client-test-2",
                name="Test Client 2",
                driver_type=DownloadDriverType.SLSKD,
                host_url="http://localhost:5030",
            )
        )

        track_mp3 = staging / "Iron Man.mp3"
        _create_minimal_mp3(track_mp3)

        test_db.create_active_download(
            ActiveDownload(
                id="dl-toggle-off",
                request_id="req-toggle-off",
                title="Paranoid",
                artist="Black Sabbath",
                client_id="client-test-2",
                download_hash="hash-toggle",
                status=DownloadStatus.COMPLETED.value,
                source_path=str(track_mp3),
            )
        )

        mock_driver = MagicMock()
        mock_driver.get_status.return_value = {
            "status": DownloadStatus.COMPLETED.value,
            "progress": 100.0,
            "source_path": str(track_mp3),
            "error_message": None,
        }

        worker = AcquisitionWorker()

        with patch("trackseerr.acquisition_worker.get_acquisition_driver", return_value=mock_driver):
            with patch("httpx.get") as mock_httpx:
                stats = worker.poll_once(db=test_db, staging_dir=str(staging))
                assert stats["imported"] == 1
                mock_httpx.assert_not_called()

        updated_dl = test_db.get_active_download("dl-toggle-off")
        imported_target = Path(updated_dl["target_path"])
        assert imported_target.exists()

        # Verify cover.jpg was NOT created
        cover_file = imported_target.parent / "cover.jpg"
        assert not cover_file.exists()

    def test_ssrf_protection_rejects_unsafe_cover_urls(self):
        assert _is_safe_cover_url("http://169.254.169.254/latest/meta-data") is False
        assert _is_safe_cover_url("http://127.0.0.1:8080/internal.jpg") is False
        assert _is_safe_cover_url("http://0.0.0.0/test.jpg") is False
        assert _is_safe_cover_url("file:///etc/passwd") is False
        assert _is_safe_cover_url("ftp://server/cover.jpg") is False
        assert _is_safe_cover_url("https://is.mzstatic.com/image/cover.jpg") is True
        assert _is_safe_cover_url("https://e-cdns-images.dzcdn.net/images/cover/123.jpg") is True
        assert _is_safe_cover_url("https://example.com/cover.jpg") is True

"""Unit tests for Lidarr REST API client extensions and LidarrMigrationJob."""

import time
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from trackseerr.clients.lidarr import LidarrApiError, LidarrClient
from trackseerr.lidarr_migration import LidarrMigrationJob
from trackseerr.storage import Database
from tests.lidarr_fake import FakeLidarr

HTTPX = "trackseerr.clients.lidarr.httpx.Client"


@pytest.fixture
def db(tmp_path):
    """Provides an isolated SQLite Database instance."""
    db_file = tmp_path / "test_migration.db"
    database = Database(db_file)
    yield database
    database.close()


@pytest.fixture
def sample_lidarr_data() -> dict[str, list[dict[str, Any]]]:
    """Sample payload representing Lidarr API v1 entities."""
    return {
        "artists": [
            {
                "id": 101,
                "artistName": "Radiohead",
                "foreignArtistId": "mbid-art-101",
                "path": "/music/Radiohead",
                "monitored": True,
            },
            {
                "id": 102,
                "artistName": "Daft Punk",
                "foreignArtistId": "mbid-art-102",
                "path": "/music/Daft Punk",
                "monitored": False,
            },
        ],
        "albums": [
            {
                "id": 201,
                "artistId": 101,
                "title": "OK Computer",
                "foreignAlbumId": "mbid-alb-201",
                "releaseDate": "1997-05-21T00:00:00Z",
                "year": 1997,
                "monitored": True,
                "path": "/music/Radiohead/OK Computer",
            },
            {
                "id": 202,
                "artistId": 102,
                "title": "Discovery",
                "foreignAlbumId": "mbid-alb-202",
                "releaseDate": "2001-03-12T00:00:00Z",
                "monitored": True,
                "path": "/music/Daft Punk/Discovery",
            },
        ],
        "tracks": [
            {
                "id": 301,
                "artistId": 101,
                "albumId": 201,
                "title": "Airbag",
                "trackNumber": 1,
                "discNumber": 1,
                "duration": 284000,
                "monitored": True,
                "foreignTrackId": "mbid-trk-301",
            },
            {
                "id": 302,
                "artistId": 101,
                "albumId": 201,
                "title": "Paranoid Android",
                "trackNumber": 2,
                "discNumber": 1,
                "duration": 383000,
                "monitored": True,
                "foreignTrackId": "mbid-trk-302",
            },
            {
                "id": 303,
                "artistId": 102,
                "albumId": 202,
                "title": "One More Time",
                "trackNumber": 1,
                "discNumber": 1,
                "duration": 320000,
                "monitored": True,
                "foreignTrackId": "mbid-trk-303",
            },
        ],
        "track_files": [
            {
                "id": 401,
                "trackId": 301,
                "path": "/music/Radiohead/OK Computer/01 - Airbag.flac",
                "relativePath": "01 - Airbag.flac",
                "size": 31457280,
                "quality": {"quality": {"name": "FLAC 24bit"}},
                "mediaInfo": {
                    "audioCodec": "FLAC",
                    "audioBitrate": 1100,
                    "audioSampleRate": 96000,
                    "audioBitsPerSample": 24,
                },
            },
            {
                "id": 402,
                "trackIds": [302],
                "path": "/music/Radiohead/OK Computer/02 - Paranoid Android.flac",
                "relativePath": "02 - Paranoid Android.flac",
                "size": 41943040,
                "quality": {"quality": {"name": "FLAC 16bit"}},
                "mediaInfo": {
                    "audioCodec": "FLAC",
                    "audioBitrate": 900,
                    "audioSampleRate": 44100,
                    "audioBitsPerSample": 16,
                },
            },
        ],
    }


def loaded_fake(data: dict[str, list[dict[str, Any]]]) -> FakeLidarr:
    """A FakeLidarr (strict about unfiltered track/trackfile calls) serving the given payload."""
    fake = FakeLidarr()
    fake.artists = data["artists"]
    fake.albums = data["albums"]
    fake.tracks = data["tracks"]
    fake.track_files = data["track_files"]
    return fake


def real_client() -> LidarrClient:
    return LidarrClient(base_url="http://lidarr:8686", api_key="test-key")


def count(db: Database, table: str) -> int:
    return db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class TestLidarrClientExtensions:
    """Verifies the new REST API helper endpoints added to LidarrClient."""

    def test_get_all_artists_success(self):
        client = LidarrClient(base_url="http://lidarr:8686", api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"id": 1, "artistName": "Radiohead"}]

        with patch("httpx.Client.get", return_value=mock_resp) as mock_get:
            res = client.get_all_artists()
            assert len(res) == 1
            assert res[0]["artistName"] == "Radiohead"
            mock_get.assert_called_once()
            args, kwargs = mock_get.call_args
            assert args[0] == "http://lidarr:8686/api/v1/artist"
            assert kwargs["headers"]["X-Api-Key"] == "test-key"

    def test_get_all_albums_with_artist_id(self):
        client = LidarrClient(base_url="http://lidarr:8686", api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"id": 2, "title": "Kid A"}]

        with patch("httpx.Client.get", return_value=mock_resp) as mock_get:
            res = client.get_all_albums(artist_id=1)
            assert len(res) == 1
            assert res[0]["title"] == "Kid A"
            mock_get.assert_called_once()
            _, kwargs = mock_get.call_args
            assert kwargs["params"] == {"artistId": 1}

    def test_get_all_tracks_with_filters(self):
        client = LidarrClient(base_url="http://lidarr:8686", api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"id": 3, "title": "Everything in Its Right Place"}]

        with patch("httpx.Client.get", return_value=mock_resp) as mock_get:
            res = client.get_all_tracks(artist_id=1, album_id=2)
            assert len(res) == 1
            _, kwargs = mock_get.call_args
            assert kwargs["params"] == {"artistId": 1, "albumId": 2}

    def test_get_all_track_files_with_error(self):
        client = LidarrClient(base_url="http://lidarr:8686", api_key="test-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("httpx.Client.get", return_value=mock_resp):
            res = client.get_all_track_files()
            assert res == []


class TestLidarrMigrationJob:
    """Verifies LidarrMigrationJob state management, ingestion, and mode switching."""

    def test_migration_job_initial_state(self):
        job = LidarrMigrationJob()
        assert not job.is_running()
        status = job.get_status()
        assert status["status"] == "idle"
        assert status["is_migrating"] is False
        assert status["artists_migrated"] == 0

    def test_start_migration_records_task_run(self, db: Database, sample_lidarr_data):
        job = LidarrMigrationJob()
        with patch(HTTPX, loaded_fake(sample_lidarr_data)):
            assert job.start_migration(db, real_client(), auto_switch_mode=False) is True
            assert job._thread is not None
            job._thread.join(timeout=10)
        row = db.latest_task_runs()["lidarr_migration"]
        assert row["status"] == "success"
        assert "artists" in row["message"]

    def test_failed_migration_records_failed_run(self, db: Database):
        job = LidarrMigrationJob()
        client = real_client()
        with patch.object(LidarrClient, "get_all_artists", side_effect=LidarrApiError("lidarr down")):
            assert job.start_migration(db, client, auto_switch_mode=False) is True
            assert job._thread is not None
            job._thread.join(timeout=10)
        assert db.latest_task_runs()["lidarr_migration"]["status"] == "failed"

    def test_progress_snapshot_counts(self, db: Database, sample_lidarr_data):
        job = LidarrMigrationJob()
        with patch(HTTPX, loaded_fake(sample_lidarr_data)):
            job.run_migration(db, real_client(), auto_switch_mode=False)
        processed, total, message = job.progress_snapshot()
        assert processed == total == len(sample_lidarr_data["artists"])
        assert "artists" in message

    def test_run_migration_full_ingestion(self, db: Database, sample_lidarr_data):
        fake = loaded_fake(sample_lidarr_data)

        # Ensure starting mode is lidarr
        db.update_media_management_settings({"library_mode": "lidarr"})
        assert db.get_media_management_settings()["library_mode"] == "lidarr"

        job = LidarrMigrationJob()
        with patch(HTTPX, fake):
            res = job.run_migration(db, real_client(), auto_switch_mode=True)

        assert res["status"] == "completed"
        assert res["is_migrating"] is False
        assert res["artists_migrated"] == 2
        assert res["albums_migrated"] == 2
        assert res["tracks_migrated"] == 3
        assert res["files_migrated"] == 2
        assert res["completed_at"] is not None

        # Verify auto-switch mode switched to native
        settings = db.get_media_management_settings()
        assert settings["library_mode"] == "native"

        # Verify database contents
        art1 = db.get_library_artist_by_name("Radiohead")
        assert art1 is not None
        assert art1["foreign_artist_id"] == "mbid-art-101"
        assert art1["monitored"] == 1

        art2 = db.get_library_artist_by_name("Daft Punk")
        assert art2 is not None
        assert art2["monitored"] == 0

        alb1 = db.get_library_album_by_title(art1["id"], "OK Computer")
        assert alb1 is not None
        assert alb1["year"] == 1997
        assert alb1["foreign_album_id"] == "mbid-alb-201"

        trk1 = db.get_library_track_by_title(alb1["id"], "Airbag", track_number=1)
        assert trk1 is not None
        assert trk1["duration_seconds"] == 284.0

        trk2 = db.get_library_track_by_title(alb1["id"], "Paranoid Android", track_number=2)
        assert trk2 is not None
        assert trk2["duration_seconds"] == 383.0

        # Verify track files
        file1 = db.get_library_file_for_track(trk1["id"])
        assert file1 is not None
        assert file1["codec"] == "FLAC"
        assert file1["sample_rate"] == 96000
        assert file1["bits_per_sample"] == 24
        assert file1["quality_name"] == "FLAC 24bit"
        assert file1["size_bytes"] == 31457280

        file2 = db.get_library_file_for_track(trk2["id"])
        assert file2 is not None
        assert file2["quality_name"] == "FLAC 16bit"

        # Track 3 has no file in sample data
        alb2 = db.get_library_album_by_title(art2["id"], "Discovery")
        trk3 = db.get_library_track_by_title(alb2["id"], "One More Time", track_number=1)
        assert trk3 is not None
        assert db.get_library_file_for_track(trk3["id"]) is None

    def test_run_migration_auto_switch_mode_false(self, db: Database, sample_lidarr_data):
        fake = loaded_fake(sample_lidarr_data)

        db.update_media_management_settings({"library_mode": "lidarr"})
        job = LidarrMigrationJob()
        with patch(HTTPX, fake):
            res = job.run_migration(db, real_client(), auto_switch_mode=False)

        assert res["status"] == "completed"
        # Mode should remain lidarr
        assert db.get_media_management_settings()["library_mode"] == "lidarr"

    def test_migration_cancellation(self, db: Database, sample_lidarr_data):
        db.update_media_management_settings({"library_mode": "lidarr"})

        # 1. Pre-set cancel event
        job = LidarrMigrationJob()
        job.cancel()
        with patch(HTTPX, loaded_fake(sample_lidarr_data)):
            res = job.run_migration(db, real_client(), auto_switch_mode=True)
        assert res["status"] == "cancelled"
        assert res["is_migrating"] is False

        # 2. Mid-migration cancel: signal while the first artist's albums are fetched
        job2 = LidarrMigrationJob()
        fake = loaded_fake(sample_lidarr_data)
        original = fake._respond

        def cancelling(method: str, url: str, body: Any = None) -> httpx.Response:
            if "/album?" in url:
                job2.cancel()
            return original(method, url, body)

        fake._respond = cancelling  # type: ignore[method-assign]
        with patch(HTTPX, fake):
            res2 = job2.run_migration(db, real_client(), auto_switch_mode=True)
        assert res2["status"] == "cancelled"
        assert res2["is_migrating"] is False
        assert db.get_media_management_settings()["library_mode"] == "lidarr"

    def test_migration_job_start_background_and_prevent_concurrency(self, db: Database):
        mock_client = MagicMock(spec=LidarrClient)
        # Slow artist retrieval to simulate active background task
        def slow_artists():
            time.sleep(0.1)
            return []

        mock_client.get_all_artists.side_effect = slow_artists
        mock_client._get_list.return_value = []

        job = LidarrMigrationJob()
        started = job.start_migration(db, mock_client)
        assert started is True
        assert job.is_running()

        # Second start while running must return False
        started_again = job.start_migration(db, mock_client)
        assert started_again is False

        # Wait for thread to finish
        if job._thread:
            job._thread.join(timeout=2.0)

        assert not job.is_running()
        assert job.get_status()["status"] == "completed"

    def test_migration_error_handling(self, db: Database):
        mock_client = MagicMock(spec=LidarrClient)
        mock_client.get_all_artists.side_effect = RuntimeError("Network timeout to Lidarr")

        job = LidarrMigrationJob()
        res = job.run_migration(db, mock_client)

        assert res["status"] == "failed"
        assert "Network timeout" in str(res["error"])
        assert res["is_migrating"] is False


def two_artist_payload() -> dict[str, list[dict[str, Any]]]:
    """Two artists, each with two albums, two tracks per album and a file per track."""
    data: dict[str, list[dict[str, Any]]] = {"artists": [], "albums": [], "tracks": [], "track_files": []}
    for a_idx, name in enumerate(("Radiohead", "Daft Punk")):
        artist_id = 101 + a_idx
        data["artists"].append(
            {"id": artist_id, "artistName": name, "foreignArtistId": f"mbid-art-{artist_id}", "monitored": True}
        )
        for b_idx in range(2):
            album_id = 201 + a_idx * 10 + b_idx
            data["albums"].append(
                {"id": album_id, "artistId": artist_id, "title": f"{name} Album {b_idx}", "monitored": True}
            )
            for t_idx in range(2):
                track_id = 300 + a_idx * 100 + b_idx * 10 + t_idx
                data["tracks"].append(
                    {"id": track_id, "artistId": artist_id, "albumId": album_id,
                     "title": f"{name} {b_idx}-{t_idx}", "trackNumber": t_idx + 1}
                )
                data["track_files"].append(
                    {"id": track_id + 1000, "trackId": track_id, "path": f"/music/{name}/{b_idx}/{t_idx}.flac",
                     "size": 1, "mediaInfo": {"audioCodec": "FLAC"}}
                )
    return data


class TestPerArtistImport:
    """The import walks Lidarr artist by artist, as a real Lidarr only answers filtered track/trackfile calls."""

    def test_migration_imports_tracks_and_files_per_artist(self, db: Database):
        fake = loaded_fake(two_artist_payload())
        db.update_media_management_settings({"library_mode": "lidarr"})

        with patch(HTTPX, fake):
            res = LidarrMigrationJob().run_migration(db, real_client())

        assert res["status"] == "completed"
        assert (res["artists_migrated"], res["albums_migrated"], res["tracks_migrated"], res["files_migrated"]) == (
            2, 4, 8, 8,
        )
        assert count(db, "library_albums") == 4
        assert count(db, "library_tracks") == 8
        assert count(db, "library_files") == 8
        assert db.get_media_management_settings()["library_mode"] == "native"

    def test_unfiltered_track_call_is_never_made(self, db: Database):
        fake = loaded_fake(two_artist_payload())
        with patch(HTTPX, fake):
            res = LidarrMigrationJob().run_migration(db, real_client())

        assert res["status"] == "completed"
        paths = fake.paths("GET")
        for path in paths:
            if path.split("?")[0] in ("track", "trackfile"):
                assert "?" in path and path.split("?")[1].split("=")[0] in ("albumId", "artistId"), path
        assert not [p for p in paths if p in ("track", "trackfile", "album")]
        assert any(p.startswith("track?albumId=") for p in paths)
        assert any(p.startswith("trackfile?artistId=") for p in paths)

    def test_album_fetch_timeout_fails_without_switching(self, db: Database):
        fake = loaded_fake(two_artist_payload())
        fake.raise_on[("GET", "album?artistId=102")] = httpx.ReadTimeout("slow")
        db.update_media_management_settings({"library_mode": "lidarr"})

        with patch(HTTPX, fake):
            res = LidarrMigrationJob().run_migration(db, real_client())

        assert res["status"] == "failed"
        assert res["is_migrating"] is False
        assert "Daft Punk" in res["error"]
        assert "album?artistId=102" in res["error"]
        assert db.get_media_management_settings()["library_mode"] == "lidarr"
        radiohead = db.get_library_artist_by_name("Radiohead")
        assert db.get_library_album_by_title(radiohead["id"], "Radiohead Album 0") is not None

    def test_trackfile_400_fails_without_switching(self, db: Database):
        fake = loaded_fake(two_artist_payload())
        fake.fail[("GET", "trackfile?artistId=102")] = 400
        db.update_media_management_settings({"library_mode": "lidarr"})

        with patch(HTTPX, fake):
            res = LidarrMigrationJob().run_migration(db, real_client())

        assert res["status"] == "failed"
        assert "HTTP 400" in res["error"]
        assert "trackfile?artistId=102" in res["error"]
        assert "Daft Punk" in res["error"]
        assert db.get_media_management_settings()["library_mode"] == "lidarr"
        radiohead = db.get_library_artist_by_name("Radiohead")
        album = db.get_library_album_by_title(radiohead["id"], "Radiohead Album 0")
        assert db.get_library_track_by_title(album["id"], "Radiohead 0-0", track_number=1) is not None

    def test_rerun_after_failure_is_idempotent(self, db: Database):
        fake = loaded_fake(two_artist_payload())
        fake.fail[("GET", "trackfile?artistId=102")] = 400
        db.update_media_management_settings({"library_mode": "lidarr"})

        with patch(HTTPX, fake):
            first = LidarrMigrationJob().run_migration(db, real_client())
            assert first["status"] == "failed"
            fake.fail.clear()
            second = LidarrMigrationJob().run_migration(db, real_client())

        assert second["status"] == "completed"
        assert second["error"] is None
        assert count(db, "library_artists") == 2
        assert count(db, "library_albums") == 4
        assert count(db, "library_tracks") == 8
        assert count(db, "library_files") == 8
        assert db.get_media_management_settings()["library_mode"] == "native"

    def test_empty_artist_list_is_rechecked_and_fails_on_outage(self, db: Database):
        fake = FakeLidarr()
        fake.fail[("GET", "artist")] = 500
        db.update_media_management_settings({"library_mode": "lidarr"})

        with patch(HTTPX, fake):
            res = LidarrMigrationJob().run_migration(db, real_client())

        assert res["status"] == "failed"
        assert "HTTP 500" in res["error"]
        assert db.get_media_management_settings()["library_mode"] == "lidarr"

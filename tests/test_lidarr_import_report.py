"""The reconciliation report written by every Lidarr import and its download route."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.clients.lidarr import LidarrApiError, LidarrClient
from trackseerr.config import Config
from trackseerr.lidarr_import_report import KEEP_REPORTS, ImportReport, latest_report_path, write_report
from trackseerr.lidarr_migration import LidarrMigrationJob
from trackseerr.storage import Database
from tests.test_lidarr_migration import HTTPX, loaded_fake, real_client

API_KEY = "super-secret-report-key-123"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "report.db")
    yield database
    database.close()


@pytest.fixture
def payload():
    """Radiohead: 2 tracks, 2 files (+1 orphan file). Daft Punk: 1 track, no file."""
    return {
        "artists": [
            {"id": 101, "artistName": "Radiohead", "foreignArtistId": "a101", "monitored": True,
             "statistics": {"trackCount": 2, "trackFileCount": 2, "albumCount": 1}},
            {"id": 102, "artistName": "Daft Punk", "foreignArtistId": "a102", "monitored": True,
             "statistics": {"trackCount": 5, "trackFileCount": 1, "albumCount": 1}},
        ],
        "albums": [
            {"id": 201, "artistId": 101, "title": "OK Computer", "foreignAlbumId": "b201", "monitored": True},
            {"id": 202, "artistId": 102, "title": "Discovery", "foreignAlbumId": "b202", "monitored": True},
        ],
        "tracks": [
            {"id": 301, "artistId": 101, "albumId": 201, "trackFileId": 401, "title": "Airbag", "trackNumber": 1},
            {"id": 302, "artistId": 101, "albumId": 201, "trackFileId": 402, "title": "Lucky", "trackNumber": 2},
            {"id": 303, "artistId": 102, "albumId": 202, "trackFileId": 0, "title": "One More Time", "trackNumber": 1},
        ],
        "track_files": [
            {"id": 401, "artistId": 101, "albumId": 201, "path": "/m/a.flac", "size": 1},
            {"id": 402, "artistId": 101, "albumId": 201, "path": "/m/b.flac", "size": 1},
        ],
    }


def run_job(db, payload, config_dir, client=None):
    job = LidarrMigrationJob()
    with patch(HTTPX, loaded_fake(payload)):
        result = job.run_migration(db, client or real_client(), auto_switch_mode=False, config_dir=str(config_dir))
    return result


def only_report(config_dir: Path) -> dict:
    files = list((config_dir / "reports").glob("lidarr-import-*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text())


def test_report_totals_compare_lidarr_and_trackseerr(db, payload, tmp_path):
    result = run_job(db, payload, tmp_path)
    assert result["status"] == "completed"
    rep = only_report(tmp_path)
    assert result["report_name"].startswith("lidarr-import-")
    assert rep["status"] == "completed"
    assert rep["totals"]["lidarr"]["missing"] == (2 - 2) + (5 - 1)
    assert rep["totals"]["lidarr"]["tracks"] == 7
    stats = db.get_library_stats()
    for key in ("artist_count", "album_count", "track_count", "track_file_count", "missing_track_count"):
        assert rep["totals"]["trackseerr"][key] == stats[key]
    by_name = {a["name"]: a for a in rep["artists"]}
    assert by_name["Radiohead"]["imported_tracks"] == 2
    assert by_name["Radiohead"]["imported_tracks_monitored"] == 2
    assert by_name["Radiohead"]["files_linked"] == 2
    assert by_name["Daft Punk"]["trackseerr_missing"] == 1
    assert by_name["Daft Punk"]["delta"] == 1 - 4


def test_report_lists_skipped_track_files(db, payload, tmp_path):
    payload["track_files"].append({"id": 999, "artistId": 101, "albumId": 201, "path": "/m/orphan.flac", "size": 1})
    run_job(db, payload, tmp_path)
    rep = only_report(tmp_path)
    assert rep["skipped"]["counts"]["no_matching_track"] == 1
    assert any(i["path"] == "/m/orphan.flac" and i["reason"] == "no_matching_track" for i in rep["skipped"]["items"])


def test_report_sorts_mismatched_artists_first(db, payload, tmp_path):
    run_job(db, payload, tmp_path)
    rep = only_report(tmp_path)
    assert [a["name"] for a in rep["artists"]] == ["Daft Punk", "Radiohead"]


def test_report_written_on_failure(db, payload, tmp_path):
    with patch.object(LidarrClient, "fetch_artist_albums", side_effect=LidarrApiError("boom")):
        result = run_job(db, payload, tmp_path)
    assert result["status"] == "failed"
    rep = only_report(tmp_path)
    assert rep["status"] == "failed"
    assert "boom" in rep["error"]


def test_report_not_written_without_config_dir(db, payload, tmp_path):
    job = LidarrMigrationJob()
    with patch(HTTPX, loaded_fake(payload)):
        result = job.run_migration(db, real_client(), auto_switch_mode=False)
    assert result["status"] == "completed"
    assert result["report_name"] is None
    assert not (tmp_path / "reports").exists()


def test_report_keeps_newest_five(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    for i in range(7):
        (reports / f"lidarr-import-20240101T00000{i}.json").write_text("{}")
    path = write_report({"x": 1}, str(tmp_path))
    names = sorted(p.name for p in reports.glob("lidarr-import-*.json"))
    assert len(names) == KEEP_REPORTS
    assert path is not None and path.name in names
    assert "lidarr-import-20240101T000000.json" not in names
    assert latest_report_path(str(tmp_path)) == path


def test_skipped_items_capped_but_counted():
    report = ImportReport()
    for i in range(1200):
        report.skip("track", "album_or_artist_unresolved", {"lidarr_track_id": i})
    assert report._skip_counts["album_or_artist_unresolved"] == 1200
    assert len(report._skipped) == 1000


def test_write_report_oserror_is_swallowed(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert write_report({"x": 1}, str(blocker)) is None


def test_report_has_no_api_key(db, payload, tmp_path):
    run_job(db, payload, tmp_path, client=LidarrClient(base_url="http://lidarr:8686", api_key=API_KEY))
    files = list((tmp_path / "reports").glob("*.json"))
    assert files and API_KEY not in files[0].read_text()
    assert "lidarr:8686" not in files[0].read_text()


def test_report_route_404_then_download(db, payload, tmp_path):
    config = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), config_dir=str(tmp_path))
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    client = TestClient(app)
    secret = get_or_create_secret_key(data_dir=config.data_dir)

    def headers(user):
        token = create_session_token(
            user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
        )
        db.create_session(token, user["id"], {"auth": "test"})
        return {"Authorization": f"Bearer {token}"}

    admin = headers(db.upsert_user("admin-1", "admin_user", "a@plex.tv", is_admin=True))
    alice = headers(db.upsert_user("user-alice", "alice", "b@plex.tv", is_admin=False))

    assert client.get("/api/library/migrate-lidarr/report", headers=admin).status_code == 404
    run_job(db, payload, tmp_path)
    assert client.get("/api/library/migrate-lidarr/report", headers=alice).status_code == 403
    resp = client.get("/api/library/migrate-lidarr/report", headers=admin)
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    assert "lidarr-import-" in resp.headers["content-disposition"]

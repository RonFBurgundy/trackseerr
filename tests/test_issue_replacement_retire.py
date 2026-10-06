"""An issue-driven replacement import retires the track's previous file(s) into the library quarantine."""

import errno
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plex_playlist_sync.acquisition_worker import AcquisitionWorker
from plex_playlist_sync.import_security import QUARANTINE_DIRNAME
from plex_playlist_sync.models import (
    ActiveDownload, DownloadClientConfig, DownloadDriverType, DownloadStatus, MediaIssue, MusicRequest, RequestStatus,
)
from plex_playlist_sync.storage import Database
from tests.audio_fixtures import write_mp3


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def _meta(path):
    return {"artist": "Daft Punk", "title": "Get Lucky", "album": "Random Access Memories", "file_path": str(path),
            "extension": ".mp3", "track_number": 3, "year": 2013, "disc_number": 1, "total_discs": 1}


def _run_import(db, tmp_path, *, replacement, old_path_factory, rename_error=None):
    """Imports one track download for t-1 whose library row currently points at the path old_path_factory returns."""
    downloads, music = tmp_path / "downloads", tmp_path / "music"
    downloads.mkdir(), music.mkdir()
    db.create_download_client(DownloadClientConfig(
        id="c1", name="SAB", driver_type=DownloadDriverType.SABNZBD, host_url="http://s:8080", api_key="k"))
    db.upsert_user("user-1", "bob", "b@example.com")
    db.create_request(MusicRequest(id="req-1", user_id="user-1", item_type="track", title="Get Lucky",
                                   artist="Daft Punk", album="Random Access Memories", status=RequestStatus.PROCESSING))
    db.upsert_library_artist({"id": "ar-1", "name": "Daft Punk", "clean_name": "daft punk", "monitored": True})
    db.upsert_library_album({"id": "al-1", "artist_id": "ar-1", "title": "Random Access Memories",
                             "clean_title": "random access memories", "year": 2013, "monitored": True})
    db.upsert_library_track({"id": "t-1", "album_id": "al-1", "artist_id": "ar-1", "title": "Get Lucky",
                             "clean_title": "get lucky", "track_number": 3, "disc_number": 1, "monitored": True})
    old = old_path_factory(downloads, music)
    db.upsert_library_file({"id": "f-old", "track_id": "t-1", "file_path": str(old), "relative_path": old.name,
                            "codec": "MP3", "quality_name": "MP3", "size_bytes": 1, "cutoff_met": True})
    db.create_issue(MediaIssue(id="iss-1", user_id="user-1", media_title="Get Lucky", artist="Daft Punk",
                               issue_type="corrupted_file", problem_details="skips"))
    db.create_active_download(ActiveDownload(
        id="dl-1", title="Get Lucky", artist="Daft Punk", client_id="c1", download_hash="h",
        status=DownloadStatus.DOWNLOADING.value, request_id="req-1", track_id="t-1", album_id="al-1"))
    if replacement:
        db.record_download_grab("dl-1", replacement_issue_id="iss-1")
    dl_file = downloads / "new" / "03 - Get Lucky.mp3"
    dl_file.parent.mkdir()
    write_mp3(dl_file)
    driver = MagicMock()
    driver.get_status.return_value = {"status": DownloadStatus.COMPLETED.value, "progress": 100.0,
                                      "size_bytes": dl_file.stat().st_size, "speed_bps": 0, "eta_seconds": 0,
                                      "source_path": str(dl_file), "error_message": None}
    settings = db.get_media_management_settings()
    settings["root_folder_path"] = str(music)
    db.update_media_management_settings(settings)
    real_rename = os.rename

    def fake_rename(src, dst):
        if rename_error and QUARANTINE_DIRNAME in str(dst):
            raise OSError(rename_error, "Invalid cross-device link")
        return real_rename(src, dst)

    with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("plex_playlist_sync.acquisition_worker.inspect_audio_file", return_value=_meta(dl_file)), \
         patch("plex_playlist_sync.import_security.os.rename", side_effect=fake_rename):
        stats = AcquisitionWorker().poll_once(db=db, plex_client=MagicMock(), staging_dir=str(downloads))
    assert stats["imported"] == 1
    return downloads, music, old


def _old_in_library(downloads, music):
    p = music / "Daft Punk" / "wrong" / "Get Lucky.mp3"
    p.parent.mkdir(parents=True)
    write_mp3(p)
    return p


def _rows(db):
    return db.list_library_files_for_track("t-1")


def test_replacement_retires_old_file_into_quarantine(db, tmp_path):
    downloads, music, old = _run_import(db, tmp_path, replacement=True, old_path_factory=_old_in_library)
    assert not old.exists()
    q = list((music / QUARANTINE_DIRNAME / "replaced" / "iss-1").iterdir())
    assert len(q) == 1 and q[0].name.endswith("Get Lucky.mp3")
    rows = _rows(db)
    assert len(rows) == 1 and rows[0]["id"] != "f-old" and Path(rows[0]["file_path"]).exists()
    assert str(old) != rows[0]["file_path"]
    admin = db.list_issue_comments("iss-1")
    assert "Retired old file" in admin[-1]["body"] and str(old) in admin[-1]["body"]
    # requesters never see system comments, so no path reaches them
    requester = db.list_issue_comments("iss-1", include_system=False)
    assert all(str(tmp_path) not in c["body"] for c in requester)


def test_non_replacement_import_leaves_old_file_and_rows_unchanged(db, tmp_path):
    downloads, music, old = _run_import(db, tmp_path, replacement=False, old_path_factory=_old_in_library)
    assert old.exists()
    assert not (music / QUARANTINE_DIRNAME).exists()
    assert {r["id"] for r in _rows(db)} >= {"f-old"} and len(_rows(db)) == 2


def test_replacement_same_path_does_not_retire(db, tmp_path):
    worker = AcquisitionWorker()
    retired, kept = [], []
    p = tmp_path / "music" / "x.mp3"
    p.parent.mkdir()
    write_mp3(p)
    worker._retire_replaced_files(db, "iss-1", [{"id": "f1", "file_path": str(p)}], p, tmp_path / "music", retired, kept)
    assert p.exists() and not retired and not kept


def test_hardlink_to_client_file_moves_library_link_only(db, tmp_path):
    client_file = {}

    def linked(downloads, music):
        src = downloads / "seeding" / "Get Lucky.mp3"
        src.parent.mkdir()
        write_mp3(src)
        lib = music / "Daft Punk" / "wrong" / "Get Lucky.mp3"
        lib.parent.mkdir(parents=True)
        os.link(src, lib)
        client_file["p"] = src
        return lib

    downloads, music, old = _run_import(db, tmp_path, replacement=True, old_path_factory=linked)
    assert not old.exists()
    assert client_file["p"].exists() and os.stat(client_file["p"]).st_nlink >= 1
    assert len(list((music / QUARANTINE_DIRNAME / "replaced" / "iss-1").iterdir())) == 1


def test_path_under_client_root_is_never_moved(db, tmp_path):
    def in_client_root(downloads, music):
        p = downloads / "seeding" / "Get Lucky.mp3"
        p.parent.mkdir()
        write_mp3(p)
        return p

    downloads, music, old = _run_import(db, tmp_path, replacement=True, old_path_factory=in_client_root)
    assert old.exists()
    assert not (music / QUARANTINE_DIRNAME).exists()
    assert len(_rows(db)) == 1
    assert "Old file kept at" in db.list_issue_comments("iss-1")[-1]["body"]


def test_cross_device_move_keeps_file_and_comments(db, tmp_path):
    downloads, music, old = _run_import(
        db, tmp_path, replacement=True, old_path_factory=_old_in_library, rename_error=errno.EXDEV
    )
    assert old.exists()
    body = db.list_issue_comments("iss-1")[-1]["body"]
    assert f"Old file kept at {old}: could not move" in body
    assert db.list_issue_comments("iss-1", include_system=False) == []

"""An import that replaces a track's file (upgrade or issue replacement) recycles the old file(s) into the recycle bin."""

import errno
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plex_playlist_sync.acquisition_worker import AcquisitionWorker
from plex_playlist_sync.naming import build_track_path
from plex_playlist_sync.recycle_bin import RECYCLE_DIRNAME
from plex_playlist_sync.models import (
    ActiveDownload, DownloadClientConfig, DownloadDriverType, DownloadStatus, MediaIssue, MusicRequest, RequestStatus,
)
from plex_playlist_sync.storage import Database
from tests.audio_fixtures import write_flac, write_mp3


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def _meta(path, ext=".mp3"):
    return {"artist": "Daft Punk", "title": "Get Lucky", "album": "Random Access Memories", "file_path": str(path),
            "extension": ext, "track_number": 3, "year": 2013, "disc_number": 1, "total_discs": 1}


def _run_import(db, tmp_path, *, replacement, old_path_factory, rename_error=None, ext=".mp3", extra=None,
                driver_type=DownloadDriverType.SABNZBD):
    """Imports one track download for t-1 whose library row currently points at the path old_path_factory returns."""
    downloads, music = tmp_path / "downloads", tmp_path / "music"
    downloads.mkdir(), music.mkdir()
    db.create_download_client(DownloadClientConfig(
        id="c1", name="SAB", driver_type=driver_type, host_url="http://s:8080", api_key="k"))
    db.upsert_user("user-1", "bob", "b@example.com")
    db.create_request(MusicRequest(id="req-1", user_id="user-1", item_type="track", title="Get Lucky",
                                   artist="Daft Punk", album="Random Access Memories", status=RequestStatus.PROCESSING))
    db.upsert_library_artist({"id": "ar-1", "name": "Daft Punk", "clean_name": "daft punk", "monitored": True})
    db.upsert_library_album({"id": "al-1", "artist_id": "ar-1", "title": "Random Access Memories",
                             "clean_title": "random access memories", "year": 2013, "monitored": True})
    db.upsert_library_track({"id": "t-1", "album_id": "al-1", "artist_id": "ar-1", "title": "Get Lucky",
                             "clean_title": "get lucky", "track_number": 3, "disc_number": 1, "monitored": True})
    settings = db.get_media_management_settings()
    settings["root_folder_path"] = str(music)
    settings.update(extra or {})
    db.update_media_management_settings(settings)
    mm = db.get_media_management_settings()
    old = old_path_factory(downloads, music, Path(build_track_path(_meta("x", ext), mm)))
    db.upsert_library_file({"id": "f-old", "track_id": "t-1", "file_path": str(old), "relative_path": old.name,
                            "codec": "MP3", "quality_name": "MP3", "size_bytes": 1, "cutoff_met": True})
    db.create_issue(MediaIssue(id="iss-1", user_id="user-1", media_title="Get Lucky", artist="Daft Punk",
                               issue_type="corrupted_file", problem_details="skips"))
    db.create_active_download(ActiveDownload(
        id="dl-1", title="Get Lucky", artist="Daft Punk", client_id="c1", download_hash="h",
        status=DownloadStatus.DOWNLOADING.value, request_id="req-1", track_id="t-1", album_id="al-1"))
    if replacement:
        db.record_download_grab("dl-1", replacement_issue_id="iss-1")
    dl_file = downloads / "new" / f"03 - Get Lucky{ext}"
    dl_file.parent.mkdir()
    (write_flac if ext == ".flac" else write_mp3)(dl_file)
    driver = MagicMock()
    driver.get_status.return_value = {"status": DownloadStatus.COMPLETED.value, "progress": 100.0,
                                      "size_bytes": dl_file.stat().st_size, "speed_bps": 0, "eta_seconds": 0,
                                      "source_path": str(dl_file), "error_message": None}
    real_rename = os.rename

    def fake_rename(src, dst):
        if rename_error and RECYCLE_DIRNAME in str(dst):
            raise OSError(rename_error, "Invalid cross-device link")
        return real_rename(src, dst)

    with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("plex_playlist_sync.acquisition_worker.inspect_audio_file", return_value=_meta(dl_file, ext)), \
         patch("plex_playlist_sync.recycle_bin.os.rename", side_effect=fake_rename):
        stats = AcquisitionWorker().poll_once(db=db, plex_client=MagicMock(), staging_dir=str(downloads))
    assert stats["imported"] == 1
    return downloads, music, old


def _old_in_library(downloads, music, target=None):
    p = music / "Daft Punk" / "wrong" / "Get Lucky.mp3"
    p.parent.mkdir(parents=True)
    write_mp3(p)
    return p


def _rows(db):
    return db.list_library_files_for_track("t-1")


def _recycled(music):
    """Every file in the default recycle bin as a path relative to it."""
    root = music / RECYCLE_DIRNAME
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_replacement_recycles_old_file_with_library_structure(db, tmp_path):
    downloads, music, old = _run_import(db, tmp_path, replacement=True, old_path_factory=_old_in_library)
    assert not old.exists()
    rec = _recycled(music)
    assert len(rec) == 1 and str(rec[0]).endswith(str(old.relative_to(music)))
    assert len(rec[0].parts[0]) == 10 and rec[0].parts[0][4] == "-"  # YYYY-MM-DD folder
    rows = _rows(db)
    assert len(rows) == 1 and rows[0]["id"] != "f-old" and Path(rows[0]["file_path"]).exists()
    assert str(old) != rows[0]["file_path"]
    admin = db.list_issue_comments("iss-1")
    assert "Retired old file" in admin[-1]["body"] and str(old) in admin[-1]["body"]
    assert RECYCLE_DIRNAME in admin[-1]["body"]
    # requesters never see system comments, so no path reaches them
    requester = db.list_issue_comments("iss-1", include_system=False)
    assert all(str(tmp_path) not in c["body"] for c in requester)


def test_normal_mp3_to_flac_upgrade_recycles_mp3_and_leaves_one_row(db, tmp_path):
    downloads, music, old = _run_import(
        db, tmp_path, replacement=False, old_path_factory=_old_in_library, ext=".flac"
    )
    assert not old.exists()
    rec = _recycled(music)
    assert len(rec) == 1 and str(rec[0]).endswith("Get Lucky.mp3")
    rows = _rows(db)
    assert len(rows) == 1 and rows[0]["file_path"].endswith(".flac") and Path(rows[0]["file_path"]).exists()
    ev = db.conn.execute("SELECT details_json FROM system_events WHERE event_type='file_recycled'").fetchall()
    assert len(ev) == 1 and '"old_quality": "MP3"' in ev[0][0] and '"new_quality"' in ev[0][0]


def test_same_path_overwrite_recycles_old_bytes_first(db, tmp_path):
    marker = b"OLD-BYTES-MARKER"

    def at_target(downloads, music, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        write_mp3(target)
        with open(target, "ab") as fh:
            fh.write(marker)
        return target

    downloads, music, old = _run_import(db, tmp_path, replacement=False, old_path_factory=at_target)
    rec = _recycled(music)
    assert len(rec) == 1
    assert (music / RECYCLE_DIRNAME / rec[0]).read_bytes().endswith(marker)  # the old bytes were not overwritten
    assert old.exists() and not old.read_bytes().endswith(marker)  # the new file took the clean name
    assert not list(old.parent.glob("*(1)*"))
    rows = _rows(db)
    assert len(rows) == 1 and rows[0]["file_path"] == str(old.resolve())


def test_name_clash_in_recycle_bin_gets_numeric_suffix(db, tmp_path):
    from plex_playlist_sync.recycle_bin import dispose_replaced_file
    from datetime import date

    music = tmp_path / "music"
    f = music / "A" / "b.mp3"
    f.parent.mkdir(parents=True)
    d = date(2026, 1, 2)
    for i in range(3):
        write_mp3(f)
        res = dispose_replaced_file(f, library_root=music, recycle_root=music / RECYCLE_DIRNAME, client_roots=[], today=d)
        assert res.status == "recycled"
    names = sorted(p.name for p in (music / RECYCLE_DIRNAME / "2026-01-02" / "A").iterdir())
    assert names == ["b.mp3", "b.mp3.1", "b.mp3.2"]


def test_custom_recycle_path_is_used(db, tmp_path):
    custom = tmp_path / "bin"
    downloads, music, old = _run_import(
        db, tmp_path, replacement=True, old_path_factory=_old_in_library, extra={"recycle_bin_path": str(custom)}
    )
    assert not old.exists() and not (music / RECYCLE_DIRNAME).exists()
    assert len([p for p in custom.rglob("*") if p.is_file()]) == 1


def test_permanent_delete_only_when_enabled(db, tmp_path):
    downloads, music, old = _run_import(
        db, tmp_path, replacement=True, old_path_factory=_old_in_library, extra={"recycle_bin_permanent_delete": True}
    )
    assert not old.exists() and _recycled(music) == []
    assert len(_rows(db)) == 1


def test_empty_recycle_path_without_toggle_never_deletes(db, tmp_path):
    downloads, music, old = _run_import(db, tmp_path, replacement=False, old_path_factory=_old_in_library)
    assert db.get_media_management_settings()["recycle_bin_path"] == ""
    assert db.get_media_management_settings()["recycle_bin_permanent_delete"] is False
    assert len(_recycled(music)) == 1  # recycled, not deleted


def test_dispose_same_path_is_noop(db, tmp_path):
    worker = AcquisitionWorker()
    retired, kept = [], []
    p = tmp_path / "music" / "x.mp3"
    p.parent.mkdir()
    write_mp3(p)
    worker._recycle_replaced_files(
        db, {"id": "dl"}, "iss-1", [{"id": "f1", "file_path": str(p)}], p, tmp_path / "music",
        {"root_folder_path": str(tmp_path / "music")}, "FLAC", retired, kept,
    )
    assert p.exists() and not retired and not kept


def test_hardlink_to_client_file_moves_library_link_only(db, tmp_path):
    client_file = {}

    def linked(downloads, music, target=None):
        src = downloads / "seeding" / "Get Lucky.mp3"
        src.parent.mkdir()
        write_mp3(src)
        lib = music / "Daft Punk" / "wrong" / "Get Lucky.mp3"
        lib.parent.mkdir(parents=True)
        os.link(src, lib)
        client_file["p"] = src
        client_file["bytes"] = src.read_bytes()
        return lib

    downloads, music, old = _run_import(db, tmp_path, replacement=False, old_path_factory=linked, ext=".flac")
    assert not old.exists()
    assert client_file["p"].exists() and os.stat(client_file["p"]).st_nlink >= 1
    assert client_file["p"].read_bytes() == client_file["bytes"]  # the seeding torrent file is untouched
    rec = _recycled(music)
    assert len(rec) == 1 and os.stat(music / RECYCLE_DIRNAME / rec[0]).st_ino == os.stat(client_file["p"]).st_ino


def test_path_under_client_root_is_never_moved(db, tmp_path):
    def in_client_root(downloads, music, target=None):
        p = downloads / "seeding" / "Get Lucky.mp3"
        p.parent.mkdir()
        write_mp3(p)
        return p

    downloads, music, old = _run_import(db, tmp_path, replacement=True, old_path_factory=in_client_root)
    assert old.exists()
    assert _recycled(music) == []
    assert len(_rows(db)) == 1
    assert "Old file kept at" in db.list_issue_comments("iss-1")[-1]["body"]


def test_client_roots_of_other_clients_are_protected(db, tmp_path):
    from plex_playlist_sync.recycle_bin import dispose_replaced_file

    music = tmp_path / "music"
    other_root = music / "torrents-inside-library"  # a second client's folder that overlaps the library tree
    f = other_root / "t.mp3"
    f.parent.mkdir(parents=True)
    write_mp3(f)
    res = dispose_replaced_file(f, library_root=music, recycle_root=music / RECYCLE_DIRNAME, client_roots=[other_root])
    assert res.status == "kept" and f.exists()


def test_symlink_escaping_library_is_not_moved(db, tmp_path):
    from plex_playlist_sync.recycle_bin import dispose_replaced_file

    music, outside = tmp_path / "music", tmp_path / "outside"
    music.mkdir(), outside.mkdir()
    target = outside / "real.mp3"
    write_mp3(target)
    link = music / "link.mp3"
    link.symlink_to(target)
    res = dispose_replaced_file(link, library_root=music, recycle_root=music / RECYCLE_DIRNAME, client_roots=[])
    assert res.status == "kept" and link.is_symlink() and target.exists()


def test_cross_device_move_keeps_file_and_comments(db, tmp_path):
    downloads, music, old = _run_import(
        db, tmp_path, replacement=True, old_path_factory=_old_in_library, rename_error=errno.EXDEV
    )
    assert old.exists()
    body = db.list_issue_comments("iss-1")[-1]["body"]
    assert f"Old file kept at {old}: could not move" in body
    assert db.list_issue_comments("iss-1", include_system=False) == []
    assert len(_rows(db)) == 1  # the stale row is gone; the file is still on disk and the scanner will see it

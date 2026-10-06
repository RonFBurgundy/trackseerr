"""Weak-match findings from auto-import, and Re-match through manual import commit (hardlink-safe)."""
import sqlite3
import struct
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.acquisition_worker import AcquisitionWorker
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import (
    ActiveDownload, DownloadClientConfig, DownloadDriverType, DownloadStatus,
    LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack,
)
from plex_playlist_sync.storage import Database


def _flac(path: Path) -> None:
    sr = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    streaminfo = struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00") + sr + b"\x00" * 16
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC\x80\x00\x00\x22" + streaminfo)


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "h.db"))
    yield d
    d.close()


@pytest.fixture
def config(tmp_path: Path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def headers(db, config):
    admin = db.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=admin["id"], username="admin_user", is_admin=True, secret_key=secret)
    db.create_session(token, admin["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(db, config):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


def _seed(db: Database, tmp_path: Path):
    music, staging = tmp_path / "music", tmp_path / "staging"
    music.mkdir(exist_ok=True)
    staging.mkdir(exist_ok=True)
    db.update_media_management_settings(
        {"root_folder_path": str(music), "staging_folder_path": str(staging), "library_mode": "native",
         "enrich_mbids": False, "write_audio_tags": False}
    )
    db.upsert_library_artist(LibraryArtist(id="art-1", name="Daft Punk", path=str(music / "DP")))
    db.upsert_library_album(LibraryAlbum(id="alb-1", artist_id="art-1", title="Discovery", year=2001))
    for tid, title, n in (("trk-1", "One More Time", 1), ("trk-2", "Aerodynamic", 2)):
        db.upsert_library_track(LibraryTrack(id=tid, album_id="alb-1", artist_id="art-1", title=title,
                                             track_number=n, disc_number=1))
    return music, staging


# ------------------------------------------------------------------ weak match on auto-import


def _import(db: Database, tmp_path: Path, meta: dict[str, Any]) -> Path:
    music, staging = _seed(db, tmp_path)
    dl = staging / "Daft.Punk.Discovery.FLAC"
    _flac(dl / "file.flac")
    db.create_download_client(
        DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030"))
    db.create_active_download(
        ActiveDownload(id="dl-1", client_id="c1", title="Daft.Punk.Discovery.FLAC", artist="Daft Punk",
                       item_type="album", status=DownloadStatus.DOWNLOADING.value, download_hash="h", album_id="alb-1"))
    driver = MagicMock()
    driver.get_status.return_value = {"status": DownloadStatus.COMPLETED.value, "progress": 100.0, "size_bytes": 1,
                                      "speed_bps": 0, "eta_seconds": 0, "source_path": str(dl), "error_message": None}
    full = {"artist": "Daft Punk", "album": "Discovery", "disc_number": 1, "codec": "FLAC", "bits_per_sample": 16,
            "bitrate": 900, "sample_rate": 44100, "extension": ".flac", **meta}
    with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("plex_playlist_sync.acquisition_worker.inspect_audio_file", return_value=full), \
         patch("plex_playlist_sync.acquisition_worker.fingerprint_audio_file", return_value=None):
        AcquisitionWorker().poll_once(db=db, staging_dir=str(staging))
    return music


FUZZY = {"title": "One More Tim", "track_number": None}  # fuzzy title only: a weak match
STRONG = {"title": "Aerodynamic", "track_number": 2}


def test_weak_match_import_records_finding_at_placed_path(db, tmp_path):
    music = _import(db, tmp_path, FUZZY)
    placed = db.get_library_file_for_track("trk-1")
    assert placed is not None, "the weak match was imported"
    findings = [f for f in db.list_library_health_findings() if f["kind"] == "weak_match"]
    assert len(findings) == 1
    f = findings[0]
    assert f["path"] == placed["file_path"] and Path(f["path"]).is_relative_to(music)
    assert f["cause"] == "weak_tag_match"
    assert f["detail"] == {"track_id": "trk-1", "title": "One More Time", "source_name": "Daft.Punk.Discovery.FLAC",
                           "strength": "weak"}


def test_strong_match_import_records_nothing(db, tmp_path):
    _import(db, tmp_path, STRONG)
    assert db.get_library_file_for_track("trk-2") is not None
    assert db.list_library_health_findings() == []


def test_recording_failure_does_not_break_the_import(db, tmp_path):
    with patch.object(Database, "upsert_library_health_findings", side_effect=sqlite3.OperationalError("locked")):
        _import(db, tmp_path, FUZZY)
    assert db.get_library_file_for_track("trk-1") is not None
    assert db.get_active_download("dl-1")["status"] != "failed"


# ------------------------------------------------------------------ re-match through manual import


def _commit(client, headers, items):
    with patch("plex_playlist_sync.api.routes.library.inspect_audio_file",
               return_value={"title": "t", "codec": "FLAC", "file_path": "x"}):
        resp = client.post("/api/library/manual-import/commit", json={"items": items}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _register(db: Database, path: Path, track_id: str, music: Path) -> None:
    db.upsert_library_file(LibraryFile(id=f"f-{track_id}", track_id=track_id, file_path=str(path),
                                       relative_path=str(path.relative_to(music)), codec="FLAC",
                                       quality_name="FLAC", size_bytes=path.stat().st_size))


def _item(path: Path, track_id: str, number: int, title: str, mode: str = "hardlink") -> dict[str, Any]:
    return {"source_path": str(path), "artist_id": "art-1", "album_id": "alb-1", "track_id": track_id,
            "track_title": title, "track_number": number, "mode": mode, "write_tags": True}


def test_rematch_relinks_track_moves_file_and_clears_finding(db, tmp_path, client, headers):
    music, _ = _seed(db, tmp_path)
    wrong = music / "Wrong" / "mismatch.flac"
    _flac(wrong)
    _register(db, wrong, "trk-1", music)
    db.upsert_library_health_findings(
        [{"kind": "weak_match", "cause": "weak_tag_match", "group_key": str(wrong.parent), "path": str(wrong),
          "detail": {"track_id": "trk-1"}}], "2026-01-01T00:00:00+00:00")
    other = music / "unrelated.flac"
    db.upsert_library_health_findings(
        [{"kind": "weak_match", "cause": "weak_tag_match", "group_key": "g", "path": str(other), "detail": {}}],
        "2026-01-01T00:00:00+00:00")

    body = _commit(client, headers, [_item(wrong, "trk-2", 2, "Aerodynamic", mode="copy")])
    res = body["results"][0]
    assert body["imported_count"] == 1 and res["rematch"] is True and res["mode"] == "move", "always moved"
    new_path = Path(res["destination_path"])
    assert new_path.exists() and not wrong.exists() and new_path.is_relative_to(music)
    assert db.get_library_file_for_track("trk-1") is None, "the old track loses its file"
    row = db.get_library_file_for_track("trk-2")
    assert row["file_path"] == str(new_path)
    assert db.get_library_file_by_path(str(wrong)) is None
    assert [f["path"] for f in db.list_library_health_findings()] == [str(other)]


def test_rematch_in_place_when_already_at_target_path(db, tmp_path, client, headers):
    music, _ = _seed(db, tmp_path)
    wrong = music / "x.flac"
    _flac(wrong)
    _register(db, wrong, "trk-1", music)
    first = _commit(client, headers, [_item(wrong, "trk-2", 2, "Aerodynamic")])["results"][0]
    placed = Path(first["destination_path"])
    # re-matching the file back onto the first track keeps a single file row per path
    back = _commit(client, headers, [_item(placed, "trk-1", 1, "One More Time")])["results"][0]
    assert back["rematch"] is True and Path(back["destination_path"]).exists()
    assert db.get_library_file_for_track("trk-2") is None
    assert db.get_library_file_for_track("trk-1") is not None
    assert len(list(music.rglob("*.flac"))) == 1


def test_rematch_keeps_the_torrent_hardlink_inode(db, tmp_path, client, headers):
    music, staging = _seed(db, tmp_path)
    torrent = staging / "torrent" / "01.flac"
    _flac(torrent)
    lib = music / "Wrong" / "01.flac"
    lib.parent.mkdir(parents=True)
    lib.hardlink_to(torrent)
    inode = torrent.stat().st_ino
    _register(db, lib, "trk-1", music)
    before = torrent.read_bytes()

    res = _commit(client, headers, [_item(lib, "trk-2", 2, "Aerodynamic")])["results"][0]
    new_path = Path(res["destination_path"])
    assert torrent.exists() and torrent.stat().st_ino == inode, "seeding source is untouched"
    assert new_path.stat().st_nlink == 1 and new_path.stat().st_ino != inode, "link broken before tagging"
    assert torrent.stat().st_nlink == 1
    assert not lib.exists()
    assert torrent.read_bytes() == before, "tags are not rewritten through a shared inode"
    assert db.get_library_file_for_track("trk-2")["file_path"] == str(new_path)


def test_non_library_source_is_still_a_normal_import(db, tmp_path, client, headers):
    music, staging = _seed(db, tmp_path)
    src = staging / "loose.flac"
    _flac(src)
    res = _commit(client, headers, [_item(src, "trk-2", 2, "Aerodynamic", mode="copy")])["results"][0]
    assert res["rematch"] is False and res["mode"] == "copy" and src.exists()

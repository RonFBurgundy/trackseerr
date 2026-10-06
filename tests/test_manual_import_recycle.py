"""Manual import recycles a track's superseded files (same rules as the worker); scanner prunes the bin."""
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.library_scanner import LibraryScanner
from plex_playlist_sync.models import (
    DownloadClientConfig, DownloadDriverType, LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack, MediaIssue,
)
from plex_playlist_sync.naming import build_track_path
from plex_playlist_sync.recycle_bin import RECYCLE_DIRNAME
from plex_playlist_sync.storage import Database
from tests.audio_fixtures import flac_bytes


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "mi.db"))
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


META = {"title": "One More Time", "artist": "Daft Punk", "album": "Discovery", "year": 2001, "track_number": 1,
        "disc_number": 1, "codec": "FLAC", "file_path": "x"}


def _seed(db, tmp_path):
    music, staging = tmp_path / "music", tmp_path / "staging"
    music.mkdir(), staging.mkdir()
    db.update_media_management_settings(
        {"root_folder_path": str(music), "staging_folder_path": str(staging), "library_mode": "native",
         "enrich_mbids": False, "write_audio_tags": False})
    db.upsert_library_artist(LibraryArtist(id="art-1", name="Daft Punk", path=str(music / "DP")))
    db.upsert_library_album(LibraryAlbum(id="alb-1", artist_id="art-1", title="Discovery", year=2001))
    for tid, n, title in (("trk-1", 1, "One More Time"), ("trk-2", 2, "Aerodynamic")):
        db.upsert_library_track(LibraryTrack(id=tid, album_id="alb-1", artist_id="art-1", title=title,
                                             track_number=n, disc_number=1))
    return music, staging


def _proper_path(db, music: Path, title="One More Time", n=1) -> Path:
    meta = dict(META, title=title, track_number=n, total_discs=1, extension=".flac", album_artist="Daft Punk",
                quality_full="FLAC 16bit", release_year=2001)
    return Path(build_track_path(meta, db.get_media_management_settings())).resolve()


def _row(db, fid, track_id, path: Path, quality="MP3"):
    db.upsert_library_file(LibraryFile(id=fid, track_id=track_id, file_path=str(path), relative_path=path.name,
                                       codec="FLAC", quality_name=quality, size_bytes=path.stat().st_size))


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _commit(client, headers, items, **extra):
    with patch("plex_playlist_sync.api.routes.library.inspect_audio_file", return_value=dict(META)):
        resp = client.post("/api/library/manual-import/commit", json={"items": items, **extra}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _item(path: Path, track_id="trk-1", mode="move"):
    return {"source_path": str(path), "track_id": track_id, "mode": mode, "write_tags": False}


def _bin_files(music: Path) -> list[Path]:
    return [p for p in (music / RECYCLE_DIRNAME).rglob("*") if p.is_file()]


def test_different_path_old_file_recycled_single_row(tmp_path, db, client, headers):
    music, staging = _seed(db, tmp_path)
    old = _write(music / "wrong" / "old.mp3", b"OLDBYTES")
    _row(db, "f-old", "trk-1", old)
    src = _write(staging / "new.flac", flac_bytes())
    out = _commit(client, headers, [_item(src)])
    assert out["imported_count"] == 1, out
    assert not old.exists()
    assert [p.read_bytes() for p in _bin_files(music)] == [b"OLDBYTES"]
    rows = db.list_library_files_for_track("trk-1")
    assert len(rows) == 1 and Path(rows[0]["file_path"]).exists() and rows[0]["id"] != "f-old"


def test_same_path_old_bytes_recycled_new_placed(tmp_path, db, client, headers):
    music, staging = _seed(db, tmp_path)
    target = _proper_path(db, music)
    _write(target, b"OLDBYTES")
    _row(db, "f-old", "trk-1", target)
    new_bytes = flac_bytes()
    src = _write(staging / "new.flac", new_bytes)
    out = _commit(client, headers, [_item(src)])
    assert out["imported_count"] == 1, out
    res = out["results"][0]
    assert Path(res["destination_path"]) == target  # clean name, no "(1)"
    assert target.read_bytes() == new_bytes
    assert [p.read_bytes() for p in _bin_files(music)] == [b"OLDBYTES"]
    rows = db.list_library_files_for_track("trk-1")
    assert len(rows) == 1 and rows[0]["file_path"] == str(target)


def test_same_path_placement_failure_restores_old_file(tmp_path, db, client, headers):
    music, staging = _seed(db, tmp_path)
    target = _proper_path(db, music)
    _write(target, b"OLDBYTES")
    _row(db, "f-old", "trk-1", target)
    src = _write(staging / "new.flac", flac_bytes())
    with patch("plex_playlist_sync.api.routes.library.place_audio_file", side_effect=OSError("boom")):
        out = _commit(client, headers, [_item(src)])
    assert out["failed_count"] == 1
    assert target.read_bytes() == b"OLDBYTES" and _bin_files(music) == []
    assert [r["id"] for r in db.list_library_files_for_track("trk-1")] == ["f-old"]


def test_in_place_rematch_not_recycled(tmp_path, db, client, headers):
    music, _ = _seed(db, tmp_path)
    target = _proper_path(db, music)
    _write(target, flac_bytes())
    _row(db, "f-1", "trk-1", target, quality="FLAC")
    out = _commit(client, headers, [_item(target)])
    assert out["imported_count"] == 1 and out["results"][0]["rematch"] is True
    assert target.exists() and _bin_files(music) == []
    assert len(db.list_library_files_for_track("trk-1")) == 1


def test_library_file_moved_to_proper_path_not_recycled(tmp_path, db, client, headers):
    music, _ = _seed(db, tmp_path)
    wrong = _write(music / "wrong" / "x.flac", flac_bytes())
    _row(db, "f-1", "trk-1", wrong, quality="FLAC")
    out = _commit(client, headers, [_item(wrong)])
    assert out["imported_count"] == 1, out
    dest = Path(out["results"][0]["destination_path"])
    assert dest.exists() and not wrong.exists() and _bin_files(music) == []
    rows = db.list_library_files_for_track("trk-1")
    assert [r["file_path"] for r in rows] == [str(dest)]


def test_rematch_onto_track_with_file_recycles_that_file(tmp_path, db, client, headers):
    music, _ = _seed(db, tmp_path)
    other = _write(music / "elsewhere" / "t2.flac", flac_bytes())
    _row(db, "f-2", "trk-2", other, quality="FLAC")
    existing = _write(music / "wrong" / "t1-old.mp3", b"T1OLD")
    _row(db, "f-1", "trk-1", existing)
    out = _commit(client, headers, [_item(other, track_id="trk-1")])
    assert out["imported_count"] == 1, out
    assert db.list_library_files_for_track("trk-2") == []
    assert [r["file_path"] for r in db.list_library_files_for_track("trk-1")] == [out["results"][0]["destination_path"]]
    assert [p.read_bytes() for p in _bin_files(music)] == [b"T1OLD"]


def test_same_batch_files_not_recycled(tmp_path, db, client, headers):
    music, staging = _seed(db, tmp_path)
    a = _write(staging / "a.flac", flac_bytes())
    b = _write(staging / "b.flac", flac_bytes(sample_rate=48000))
    out = _commit(client, headers, [_item(a), _item(b)])  # both aimed at trk-1
    assert out["imported_count"] == 2, out
    assert _bin_files(music) == []
    assert len(db.list_library_files_for_track("trk-1")) == 2


def test_hardlinked_old_file_recycled_client_copy_untouched(tmp_path, db, client, headers):
    music, staging = _seed(db, tmp_path)
    downloads = tmp_path / "downloads"
    seed = _write(downloads / "seeding" / "old.mp3", b"SEEDBYTES")
    db.create_download_client(DownloadClientConfig(
        id="c1", name="Q", driver_type=DownloadDriverType.QBITTORRENT, host_url="http://q:8080"))
    old = music / "wrong" / "old.mp3"
    old.parent.mkdir(parents=True)
    os.link(seed, old)
    _row(db, "f-old", "trk-1", old)
    src = _write(staging / "new.flac", flac_bytes())
    with patch("plex_playlist_sync.api.routes.library.allowed_roots_for_all_clients") as roots:
        from plex_playlist_sync.download_roots import AllowedRoots
        roots.return_value = AllowedRoots(roots=[downloads.resolve()])
        out = _commit(client, headers, [_item(src)])
    assert out["imported_count"] == 1, out
    assert seed.read_bytes() == b"SEEDBYTES"  # the client's name is never touched
    assert not old.exists()
    assert [p.read_bytes() for p in _bin_files(music)] == [b"SEEDBYTES"]


def test_old_file_under_client_root_is_kept_not_moved(tmp_path, db, client, headers):
    music, staging = _seed(db, tmp_path)
    old = _write(music / "wrong" / "old.mp3", b"OLD")
    _row(db, "f-old", "trk-1", old)
    src = _write(staging / "new.flac", flac_bytes())
    from plex_playlist_sync.download_roots import AllowedRoots
    with patch("plex_playlist_sync.api.routes.library.allowed_roots_for_all_clients",
               return_value=AllowedRoots(roots=[(music / "wrong").resolve()])):
        out = _commit(client, headers, [_item(src)])
    assert out["imported_count"] == 1
    assert old.read_bytes() == b"OLD" and _bin_files(music) == []


def test_issue_gets_system_comment_and_event(tmp_path, db, client, headers):
    music, staging = _seed(db, tmp_path)
    old = _write(music / "wrong" / "old.mp3", b"OLD")
    _row(db, "f-old", "trk-1", old)
    db.upsert_user("user-1", "bob", "b@example.com")
    issue = db.create_issue(MediaIssue(id="iss-1", user_id="user-1", media_title="One More Time", artist="Daft Punk",
                                       issue_type="quality", problem_details="bad"))
    iid = issue["id"] if isinstance(issue, dict) else "iss-1"
    src = _write(staging / "new.flac", flac_bytes())
    _commit(client, headers, [_item(src)], issue_id=iid)
    comments = db.list_issue_comments(iid)
    assert any("Retired old file" in c["body"] and c.get("is_system") for c in comments)
    ev, _ = db.list_events(limit=50, event_type="file_recycled")
    assert len(ev) == 1 and iid in str(ev[0])


# ------------------------------------------------------------------------------------------ scanner
def _fake_inspect(path):
    return {"title": Path(path).stem, "artist": "A", "album": "B", "track_number": 1, "disc_number": 1, "year": 2020,
            "duration": 1.0, "codec": "MP3", "bitrate": 1, "sample_rate": 44100, "bits_per_sample": 16,
            "quality_full": "MP3", "file_path": str(path)}


def test_scanner_never_lists_excluded_dirs_and_finds_same_files(tmp_path, db):
    music = tmp_path / "music"
    keep = [music / "A" / "B" / "01.mp3", music / "A" / "B" / "sub" / "02.flac", music / "top.mp3"]
    skip = [music / RECYCLE_DIRNAME / "2026-10-01" / "x.mp3", music / ".trackseerr-quarantine" / "q.mp3",
            music / "_quarantine" / "y.mp3"]
    for p in keep + skip + [music / "A" / "B" / "cover.jpg"]:
        _write(p, b"x")
    (music / "A" / "linkdir").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    _write(tmp_path / "elsewhere" / "z.mp3", b"x")  # a symlinked dir is not descended into
    listed: list[str] = []
    real_walk = os.walk

    def spy_walk(top, *a, **kw):
        for root, dirs, files in real_walk(top, *a, **kw):
            listed.append(root)
            yield root, dirs, files

    with patch("plex_playlist_sync.library_scanner.os.walk", side_effect=spy_walk), \
            patch("plex_playlist_sync.library_scanner.inspect_audio_file", side_effect=_fake_inspect):
        status = LibraryScanner().scan(db, root_folder=str(music))
    assert status["total_files_found"] == 3
    assert not any(RECYCLE_DIRNAME in r or "_quarantine" in r or "quarantine" in r for r in listed)
    assert not any("elsewhere" in r or "linkdir" in r for r in listed)
    assert {Path(f["file_path"]) for f in db.list_library_files(limit=100)} == {p.resolve() for p in keep}

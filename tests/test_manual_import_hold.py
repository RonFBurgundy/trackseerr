"""Unmatched download files are held for manual import; scoped manual-import scan/commit; queue record fields."""
import json
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync import activity_service as svc
from plex_playlist_sync.acquisition_worker import AcquisitionWorker
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import (
    ActiveDownload,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    LibraryAlbum,
    LibraryArtist,
    LibraryFile,
    LibraryTrack,
)
from plex_playlist_sync.storage import SCHEMA_VERSION, Database

HELD_MSG = "1 file(s) couldn't be matched — manual import required"


def _flac(path: Path) -> None:
    sr = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    streaminfo = struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00") + sr + b"\x00" * 16
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC\x80\x00\x00\x22" + streaminfo)


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "hold.db"))
    yield d
    d.close()


@pytest.fixture
def config(tmp_path: Path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def headers(db: Database, config: Config) -> dict[str, str]:
    admin = db.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=admin["id"], username="admin_user", is_admin=True, secret_key=secret)
    db.create_session(token, admin["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(db: Database, config: Config):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


def _seed(db: Database, tmp_path: Path, *, with_download: bool = True, album_id: str | None = "alb-1"):
    music, staging = tmp_path / "music", tmp_path / "staging"
    music.mkdir(exist_ok=True)
    staging.mkdir(exist_ok=True)
    db.update_media_management_settings(
        {"root_folder_path": str(music), "staging_folder_path": str(staging), "library_mode": "native",
         "enrich_mbids": False, "write_audio_tags": False}
    )
    artist = db.upsert_library_artist(LibraryArtist(id="art-1", name="Daft Punk", path=str(music / "DP")))
    album = db.upsert_library_album(LibraryAlbum(id="alb-1", artist_id="art-1", title="Discovery", year=2001))
    t1 = db.upsert_library_track(LibraryTrack(id="trk-1", album_id="alb-1", artist_id="art-1", title="One More Time",
                                              track_number=1, disc_number=1))
    t2 = db.upsert_library_track(LibraryTrack(id="trk-2", album_id="alb-1", artist_id="art-1", title="Aerodynamic",
                                              track_number=2, disc_number=1))
    if with_download:
        db.create_download_client(
            DownloadClientConfig(id="c1", name="C", driver_type=DownloadDriverType.SLSKD, host_url="http://slskd:5030")
        )
        db.create_active_download(
            ActiveDownload(id="dl-1", client_id="c1", title="Daft.Punk.Discovery.FLAC", artist="Daft Punk",
                           item_type="album", status=DownloadStatus.DOWNLOADING.value, download_hash="h",
                           album_id=album_id)
        )
    return music, staging, artist, album, t1, t2


def _run_worker(db: Database, dl: Path, staging: Path, metas: dict[str, dict[str, Any]], driver: MagicMock | None = None):
    driver = driver or MagicMock()
    driver.get_status.return_value = {
        "status": DownloadStatus.COMPLETED.value, "progress": 100.0, "size_bytes": 1, "speed_bps": 0,
        "eta_seconds": 0, "source_path": str(dl), "error_message": None,
    }

    def inspect(path):
        base = {"artist": "Daft Punk", "album": "Discovery", "disc_number": 1, "codec": "FLAC",
                "bits_per_sample": 16, "bitrate": 900, "sample_rate": 44100, "extension": ".flac"}
        return {**base, **metas[Path(path).name]}

    with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("plex_playlist_sync.acquisition_worker.inspect_audio_file", side_effect=inspect), \
         patch("plex_playlist_sync.acquisition_worker.fingerprint_audio_file", return_value=None):
        stats = AcquisitionWorker().poll_once(db=db, staging_dir=str(staging))
    return stats, driver


MATCHED = {"title": "Aerodynamic", "track_number": 2}
UNMATCHED = {"title": "Zzz Qqq Nothing", "track_number": None}


# ------------------------------------------------------------------ migration


def test_migration_v55_adds_unmatched_files_column(db: Database):
    assert SCHEMA_VERSION >= 55
    assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(active_downloads)").fetchall()]
    assert "unmatched_files" in cols
    # Re-running the migration on an upgraded database is a no-op.
    db._migration_v55(db.conn.cursor())


def test_unmatched_files_round_trip_and_corrupt_value(db: Database, tmp_path: Path):
    _seed(db, tmp_path)
    assert db.get_active_download("dl-1")["unmatched_files"] == []
    db.set_download_unmatched_files("dl-1", ["/a.flac", "/b.flac"])
    assert db.get_active_download("dl-1")["unmatched_files"] == ["/a.flac", "/b.flac"]
    db.conn.execute("UPDATE active_downloads SET unmatched_files = 'not json' WHERE id = 'dl-1'")
    db.conn.commit()
    assert db.get_active_download("dl-1")["unmatched_files"] == []
    db.set_download_unmatched_files("dl-1", [])
    assert db.conn.execute("SELECT unmatched_files FROM active_downloads").fetchone()[0] is None


# ------------------------------------------------------------------ worker


def test_worker_holds_unmatched_file_and_imports_matched_sibling(tmp_path, db):
    music, staging, *_ , t2 = _seed(db, tmp_path)
    dl = staging / "Daft.Punk.Discovery.FLAC"
    _flac(dl / "good.flac")
    _flac(dl / "mystery.flac")

    stats, driver = _run_worker(db, dl, staging, {"good.flac": MATCHED, "mystery.flac": UNMATCHED})

    placed = list(music.rglob("*.flac"))
    assert len(placed) == 1
    assert db.get_library_file_for_track(t2["id"]) is not None
    assert (dl / "mystery.flac").exists(), "held file stays where it was downloaded"
    row = db.get_active_download("dl-1")
    assert row["status"] == "warning"
    assert row["error_message"] == HELD_MSG
    assert row["unmatched_files"] == [str((dl / "mystery.flac"))]
    assert stats["imported"] == 1


def test_worker_with_only_unmatched_files_parks_download_without_failing(tmp_path, db):
    music, staging, *_ = _seed(db, tmp_path)
    dl = staging / "Daft.Punk.Discovery.FLAC"
    _flac(dl / "mystery.flac")

    stats, _ = _run_worker(db, dl, staging, {"mystery.flac": UNMATCHED})

    assert not list(music.rglob("*.flac"))
    assert (dl / "mystery.flac").exists()
    row = db.get_active_download("dl-1")
    assert (row["status"], row["error_message"]) == ("warning", HELD_MSG)
    assert stats["failed"] == 0
    assert not db.is_blocklisted(release_title="Daft.Punk.Discovery.FLAC")


def test_held_files_survive_post_import_cleanup(tmp_path, db):
    music, staging, *_ = _seed(db, tmp_path)
    db.update_media_management_settings({"delete_completed_transfers": True, "import_mode": "move"})
    dl = staging / "Daft.Punk.Discovery.FLAC"
    _flac(dl / "good.flac")
    _flac(dl / "mystery.flac")

    _, driver = _run_worker(db, dl, staging, {"good.flac": MATCHED, "mystery.flac": UNMATCHED})

    driver.cleanup_completed.assert_not_called()
    driver.cancel.assert_not_called()
    assert (dl / "mystery.flac").exists()
    assert db.get_active_download("dl-1")["status"] == "warning"


def test_cleanup_still_runs_when_nothing_is_held(tmp_path, db):
    music, staging, *_ = _seed(db, tmp_path)
    db.update_media_management_settings({"delete_completed_transfers": True, "import_mode": "move"})
    dl = staging / "Daft.Punk.Discovery.FLAC"
    _flac(dl / "good.flac")

    _, driver = _run_worker(db, dl, staging, {"good.flac": MATCHED})

    driver.cleanup_completed.assert_called_once()
    row = db.get_active_download("dl-1")
    assert row["status"] == "imported" and row["unmatched_files"] == []


def test_warning_download_is_not_reprocessed_on_next_poll(tmp_path, db):
    music, staging, *_ = _seed(db, tmp_path)
    dl = staging / "Daft.Punk.Discovery.FLAC"
    _flac(dl / "good.flac")
    _flac(dl / "mystery.flac")
    _run_worker(db, dl, staging, {"good.flac": MATCHED, "mystery.flac": UNMATCHED})

    driver = MagicMock()
    stats, _ = _run_worker(db, dl, staging, {}, driver=driver)

    assert stats["polled"] == 0
    driver.get_status.assert_not_called()
    assert len(list(music.rglob("*.flac"))) == 1
    assert db.get_active_download("dl-1")["status"] == "warning"


def test_download_without_expected_tracks_keeps_current_behaviour(tmp_path, db):
    music, staging, *_ = _seed(db, tmp_path, album_id=None)
    # No catalog album matches this download, so there are no expected tracks.
    db.conn.execute("UPDATE active_downloads SET artist = 'Nobody', title = 'Unknown.Release' WHERE id = 'dl-1'")
    db.conn.commit()
    dl = staging / "Unknown.Release"
    _flac(dl / "mystery.flac")

    _run_worker(db, dl, staging, {"mystery.flac": UNMATCHED})

    assert len(list(music.rglob("*.flac"))) == 1
    row = db.get_active_download("dl-1")
    assert row["status"] == "imported"
    assert row["unmatched_files"] == []


def test_queue_delete_cancels_client_even_while_files_are_held(tmp_path, db):
    music, staging, *_ = _seed(db, tmp_path)
    dl = staging / "Daft.Punk.Discovery.FLAC"
    held = dl / "mystery.flac"
    _flac(held)
    db.set_download_unmatched_files("dl-1", [str(held)])
    db.update_download_status("dl-1", status="warning", error_message=HELD_MSG)

    driver = MagicMock()
    with patch("plex_playlist_sync.activity_service.get_acquisition_driver", return_value=driver):
        assert svc.native_delete_queue_item(db, "dl-1", remove_from_client=True, blocklist=False) is not None
    driver.cancel.assert_called_once()


# ------------------------------------------------------------------ queue record


def test_native_queue_record_manual_import_fields(tmp_path, db):
    _seed(db, tmp_path)
    now = datetime.now(timezone.utc)
    row = db.get_native_queue_item("dl-1")
    rec = svc.native_queue_record(row, now)
    assert (rec["download_id"], rec["needs_manual_import"], rec["unmatched_count"]) == ("dl-1", False, 0)

    db.set_download_unmatched_files("dl-1", ["/x/a.flac", "/x/b.flac"])
    db.update_download_status("dl-1", status="warning", error_message="2 file(s) couldn't be matched — manual import required")
    rec = svc.native_queue_record(db.get_native_queue_item("dl-1"), now)
    assert (rec["download_id"], rec["needs_manual_import"], rec["unmatched_count"]) == ("dl-1", True, 2)
    assert rec["stalled"] is True and rec["status"] == "warning"


def test_lidarr_queue_record_has_uniform_shape():
    rec = svc.lidarr_queue_record({"id": 5, "size": 100, "sizeleft": 50, "title": "x"})
    assert (rec["download_id"], rec["needs_manual_import"], rec["unmatched_count"]) == (None, False, 0)


# ------------------------------------------------------------------ scan


def _scan(client, headers, body):
    resp = client.post("/api/library/manual-import/scan", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _patch_inspect(metas: dict[str, dict[str, Any]]):
    def inspect(path):
        return {"artist": "Daft Punk", "album": "Discovery", "disc_number": 1, "codec": "FLAC",
                "file_path": str(path), **metas[Path(path).name]}
    return patch("plex_playlist_sync.api.routes.library.inspect_audio_file", side_effect=inspect)


def test_scan_download_id_scopes_to_held_files_and_missing_tracks(tmp_path, db, client, headers):
    music, staging, _, _, t1, t2 = _seed(db, tmp_path)
    dl = staging / "Daft.Punk.Discovery.FLAC"
    held = dl / "held.flac"
    _flac(held)
    _flac(dl / "other.flac")  # not held: must not be scanned
    db.set_download_unmatched_files("dl-1", [str(held), str(dl / "gone.flac")])
    # Track 2 already has a file, so it is not a candidate.
    db.upsert_library_file(LibraryFile(id="f2", track_id="trk-2", file_path=str(music / "a.flac"),
                                       relative_path="a.flac", codec="FLAC", size_bytes=1))

    with _patch_inspect({"held.flac": {"title": "One More Time", "track_number": 1}}):
        items = _scan(client, headers, {"download_id": "dl-1"})

    assert [i["filename"] for i in items] == ["held.flac"]
    item = items[0]
    assert item["match_strength"] == "strong"
    assert item["suggested_track_id"] == "trk-1" and item["matched_track_id"] == "trk-1"
    assert item["matched_album_id"] == "alb-1" and item["matched_artist_id"] == "art-1"
    assert [c["id"] for c in item["candidate_tracks"]] == ["trk-1"]
    cand = item["candidate_tracks"][0]
    assert set(cand) == {"id", "title", "track_number", "disc_number", "album_id", "album_title",
                         "artist_id", "artist_name", "has_file"}
    assert cand["has_file"] is False and cand["artist_name"] == "Daft Punk" and cand["album_title"] == "Discovery"
    for key in ("file_path", "filename", "size_bytes", "tags", "confidence", "matched_artist_name",
                "matched_album_title", "matched_track_title"):
        assert key in item


def test_scan_download_id_unknown_is_404(tmp_path, db, client, headers):
    _seed(db, tmp_path)
    resp = client.post("/api/library/manual-import/scan", json={"download_id": "nope"}, headers=headers)
    assert resp.status_code == 404


def test_scan_scoped_never_suggests_same_track_twice(tmp_path, db, client, headers):
    _, staging, *_ = _seed(db, tmp_path)
    dl = staging / "rel"
    a, b = dl / "a.flac", dl / "b.flac"
    _flac(a)
    _flac(b)
    metas = {"a.flac": {"title": "Aerodynamic", "track_number": 2}, "b.flac": {"title": "Aerodynamic", "track_number": 2}}
    with _patch_inspect(metas):
        items = _scan(client, headers, {"file_paths": [str(a), str(b)], "album_id": "alb-1"})
    suggested = [i["suggested_track_id"] for i in items]
    assert suggested[0] == "trk-2"
    assert suggested[1] != "trk-2"
    # The picker still offers every album track without a file to each item.
    assert all({c["id"] for c in i["candidate_tracks"]} == {"trk-1", "trk-2"} for i in items)


def test_scan_album_id_scopes_folder_scan_and_reports_none(tmp_path, db, client, headers):
    _, staging, *_ = _seed(db, tmp_path)
    folder = staging / "rel"
    _flac(folder / "weak.flac")
    _flac(folder / "junk.flac")
    metas = {"weak.flac": {"title": "Aerodynamics", "track_number": None},
             "junk.flac": {"title": "Zzz Qqq Nothing", "track_number": None}}
    with _patch_inspect(metas):
        items = _scan(client, headers, {"folder_path": str(folder), "album_id": "alb-1"})
    by_name = {i["filename"]: i for i in items}
    assert by_name["weak.flac"]["match_strength"] == "weak"
    assert by_name["weak.flac"]["suggested_track_id"] == "trk-2"
    assert by_name["junk.flac"]["match_strength"] == "none"
    assert by_name["junk.flac"]["suggested_track_id"] is None
    assert by_name["junk.flac"]["matched_track_id"] is None
    assert len(by_name["junk.flac"]["candidate_tracks"]) == 2


def test_scan_file_paths_validates_each_path(tmp_path, db, client, headers):
    _, staging, *_ = _seed(db, tmp_path)
    f = staging / "x.flac"
    _flac(f)
    assert client.post("/api/library/manual-import/scan", json={"file_paths": ["/etc/passwd"]},
                       headers=headers).status_code == 403
    assert client.post("/api/library/manual-import/scan", json={"file_paths": [str(staging / "missing.flac")]},
                       headers=headers).status_code == 400
    with _patch_inspect({"x.flac": {"title": "Aerodynamic", "track_number": 2}}):
        items = _scan(client, headers, {"file_paths": [str(f)]})
    # file_paths without an album scope behaves like the unscoped folder scan for matching.
    assert len(items) == 1 and items[0]["match_strength"] == "strong"
    assert items[0]["suggested_track_id"] == "trk-2"
    assert {c["id"] for c in items[0]["candidate_tracks"]} == {"trk-1", "trk-2"}


def test_scan_unscoped_strength_and_candidates(tmp_path, db, client, headers):
    _, staging, *_ = _seed(db, tmp_path)
    for name in ("strong.flac", "weak.flac", "none.flac"):
        _flac(staging / name)
    metas = {
        "strong.flac": {"title": "Aerodynamic", "track_number": 2},
        "weak.flac": {"title": "Not In Catalog", "track_number": 9},
        "none.flac": {"title": "x", "track_number": None, "artist": "Somebody Else", "album": "Other"},
    }
    with _patch_inspect(metas):
        items = {i["filename"]: i for i in _scan(client, headers, {"folder_path": str(staging)})}
    assert items["strong.flac"]["match_strength"] == "strong"
    assert items["strong.flac"]["suggested_track_id"] == "trk-2"
    assert len(items["strong.flac"]["candidate_tracks"]) == 2
    assert items["weak.flac"]["match_strength"] == "weak"
    assert items["weak.flac"]["suggested_track_id"] is None
    assert items["none.flac"]["match_strength"] == "none"
    assert items["none.flac"]["candidate_tracks"] == []


def test_album_tracks_endpoint(tmp_path, db, client, headers):
    music, *_ = _seed(db, tmp_path)
    db.upsert_library_file(LibraryFile(id="f1", track_id="trk-1", file_path=str(music / "a.flac"),
                                       relative_path="a.flac", codec="FLAC", size_bytes=1))
    resp = client.get("/api/library/manual-import/album-tracks", params={"album_id": "alb-1"}, headers=headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert [(r["id"], r["has_file"]) for r in rows] == [("trk-1", True), ("trk-2", False)]
    assert rows[0]["album_title"] == "Discovery" and rows[0]["artist_name"] == "Daft Punk"
    assert client.get("/api/library/manual-import/album-tracks", params={"album_id": "nope"},
                      headers=headers).status_code == 404


# ------------------------------------------------------------------ commit


def _held_download(db: Database, tmp_path: Path, names: list[str]):
    music, staging, *_ = _seed(db, tmp_path)
    dl = staging / "Daft.Punk.Discovery.FLAC"
    files = []
    for n in names:
        _flac(dl / n)
        files.append(dl / n)
    db.set_download_unmatched_files("dl-1", [str(f) for f in files])
    db.update_download_status("dl-1", status="warning",
                              error_message=f"{len(files)} file(s) couldn't be matched — manual import required")
    return music, files


def _item(path: Path, track_id: str, number: int, title: str) -> dict[str, Any]:
    return {"source_path": str(path), "artist_id": "art-1", "album_id": "alb-1", "track_id": track_id,
            "track_title": title, "track_number": number, "mode": "move", "write_tags": False}


def _item_no_mode(path: Path, track_id: str, number: int, title: str) -> dict[str, Any]:
    item = _item(path, track_id, number, title)
    del item["mode"]
    return item


def _commit(client, headers, items, download_id="dl-1"):
    with patch("plex_playlist_sync.api.routes.library.inspect_audio_file",
               return_value={"title": "t", "codec": "FLAC", "file_path": "x"}):
        resp = client.post("/api/library/manual-import/commit",
                           json={"items": items, "download_id": download_id}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_commit_clears_warning_only_when_all_held_files_are_done(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac", "b.flac"])

    out = _commit(client, headers, [_item(files[0], "trk-1", 1, "One More Time")])
    assert out["imported_count"] == 1 and out["download_cleared"] is False
    row = db.get_active_download("dl-1")
    assert row["status"] == "warning"
    assert row["unmatched_files"] == [str(files[1])]
    assert row["error_message"] == "1 file(s) couldn't be matched — manual import required"

    out = _commit(client, headers, [_item(files[1], "trk-2", 2, "Aerodynamic")])
    assert out["download_cleared"] is True
    row = db.get_active_download("dl-1")
    assert row["status"] == "imported"
    assert not row["error_message"]
    assert row["unmatched_files"] == []


def test_commit_clears_warning_when_remaining_files_no_longer_exist(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac", "b.flac"])
    files[1].unlink()
    out = _commit(client, headers, [_item(files[0], "trk-1", 1, "One More Time")])
    assert out["download_cleared"] is True
    assert db.get_active_download("dl-1")["status"] == "imported"


def test_commit_failed_item_keeps_warning(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    out = _commit(client, headers, [{"source_path": str(files[0].parent / "missing.flac")}])
    assert out["failed_count"] == 1 and out["download_cleared"] is False
    assert db.get_active_download("dl-1")["status"] == "warning"


def test_commit_without_download_id_reports_not_cleared(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    with patch("plex_playlist_sync.api.routes.library.inspect_audio_file",
               return_value={"title": "t", "codec": "FLAC", "file_path": "x"}):
        resp = client.post("/api/library/manual-import/commit",
                           json={"items": [_item(files[0], "trk-1", 1, "One More Time")]}, headers=headers)
    assert resp.json()["download_cleared"] is False
    assert db.get_active_download("dl-1")["status"] == "warning"


def test_commit_unknown_download_id_is_404(tmp_path, db, client, headers):
    _seed(db, tmp_path)
    resp = client.post("/api/library/manual-import/commit", json={"items": [], "download_id": "nope"}, headers=headers)
    assert resp.status_code == 404


def _with_client(db: Database):
    db.conn.execute("UPDATE active_downloads SET download_hash = 'HASH1' WHERE id = 'dl-1'")
    db.conn.commit()
    driver = MagicMock()
    driver.get_status.return_value = {"status": "completed", "ratio": 0.0, "seeding_time_seconds": 0}
    cfg = {"id": "c1", "name": "qb"}
    return driver, patch.object(db, "get_download_client", return_value=cfg)


def test_commit_cleans_up_client_when_download_cleared(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    db.update_media_management_settings({"delete_completed_transfers": True})
    driver, cfg = _with_client(db)
    with cfg, patch("plex_playlist_sync.api.routes.library.get_acquisition_driver", return_value=driver):
        out = _commit(client, headers, [_item(files[0], "trk-1", 1, "One More Time")])
    assert out["download_cleared"] is True
    driver.cleanup_completed.assert_called_once_with("HASH1", delete_files=False)


def test_commit_does_not_clean_up_client_while_files_remain(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac", "b.flac"])
    driver, cfg = _with_client(db)
    with cfg, patch("plex_playlist_sync.api.routes.library.get_acquisition_driver", return_value=driver):
        out = _commit(client, headers, [_item(files[0], "trk-1", 1, "One More Time")])
    assert out["download_cleared"] is False
    driver.cleanup_completed.assert_not_called()


def test_commit_survives_client_cleanup_failure(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    db.update_media_management_settings({"delete_completed_transfers": True})
    driver, cfg = _with_client(db)
    driver.cleanup_completed.side_effect = RuntimeError("client down")
    with cfg, patch("plex_playlist_sync.api.routes.library.get_acquisition_driver", return_value=driver):
        out = _commit(client, headers, [_item(files[0], "trk-1", 1, "One More Time")])
    assert out["download_cleared"] is True
    assert db.get_active_download("dl-1")["status"] == "imported"


# ------------------------------------------------------------------ seeding safety


def _commit_with_driver(client, headers, db, files, driver, mode_item=_item_no_mode):
    cfg = patch.object(db, "get_download_client", return_value={"id": "c1", "name": "qb"})
    with cfg, patch("plex_playlist_sync.api.routes.library.get_acquisition_driver", return_value=driver):
        return _commit(client, headers, [mode_item(files[0], "trk-1", 1, "One More Time")])


def test_commit_without_mode_uses_settings_import_mode(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    db.update_media_management_settings({"import_mode": "hardlink"})
    out = _commit(client, headers, [_item_no_mode(files[0], "trk-1", 1, "One More Time")])
    assert out["imported_count"] == 1
    assert files[0].exists()
    assert Path(out["results"][0]["destination_path"]).stat().st_ino == files[0].stat().st_ino


def test_commit_without_mode_defaults_to_move(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    out = _commit(client, headers, [_item_no_mode(files[0], "trk-1", 1, "One More Time")])
    assert out["imported_count"] == 1
    assert not files[0].exists()


def test_commit_rejects_invalid_mode(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    item = _item(files[0], "trk-1", 1, "One More Time")
    item["mode"] = "symlink"
    resp = client.post("/api/library/manual-import/commit", json={"items": [item], "download_id": "dl-1"},
                       headers=headers)
    assert resp.status_code == 422


def test_commit_keeps_transfer_when_delete_completed_off(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    driver, _ = _with_client(db)
    out = _commit_with_driver(client, headers, db, files, driver)
    assert out["download_cleared"] is True
    driver.cleanup_completed.assert_not_called()
    assert db.get_active_download("dl-1")["status"] == "imported"


def test_commit_hardlink_keeps_seeding_until_limits_met(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    db.update_media_management_settings(
        {"delete_completed_transfers": True, "import_mode": "hardlink", "seed_ratio_limit": 2.0}
    )
    driver, _ = _with_client(db)
    driver.get_status.return_value = {"status": "completed", "ratio": 0.5, "seeding_time_seconds": 0}
    _commit_with_driver(client, headers, db, files, driver)
    driver.cleanup_completed.assert_not_called()
    row = db.get_active_download("dl-1")
    assert row["status"] == "completed" and row["target_path"]
    assert files[0].exists()


def test_commit_hardlink_cleans_up_when_limits_met(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    db.update_media_management_settings(
        {"delete_completed_transfers": True, "import_mode": "hardlink", "seed_ratio_limit": 2.0}
    )
    driver, _ = _with_client(db)
    driver.get_status.return_value = {"status": "completed", "ratio": 2.5, "seeding_time_seconds": 0}
    _commit_with_driver(client, headers, db, files, driver)
    driver.cleanup_completed.assert_called_once_with("HASH1", delete_files=False)
    assert db.get_active_download("dl-1")["status"] == "imported"


def test_commit_keeps_transfer_when_status_fetch_fails(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    db.update_media_management_settings({"delete_completed_transfers": True})
    driver, _ = _with_client(db)
    driver.get_status.side_effect = RuntimeError("client down")
    out = _commit_with_driver(client, headers, db, files, driver)
    assert out["download_cleared"] is True
    driver.cleanup_completed.assert_not_called()
    assert db.get_active_download("dl-1")["status"] == "completed"


def test_worker_keeps_transfer_when_delete_completed_off(tmp_path, db):
    music, staging, *_ = _seed(db, tmp_path)
    db.update_media_management_settings({"delete_completed_transfers": False, "import_mode": "move"})
    dl = staging / "Daft.Punk.Discovery.FLAC"
    _flac(dl / "good.flac")
    _, driver = _run_worker(db, dl, staging, {"good.flac": MATCHED})
    driver.cleanup_completed.assert_not_called()
    assert db.get_active_download("dl-1")["status"] == "imported"


@pytest.mark.parametrize("mode", ["hardlink", "copy"])
@pytest.mark.parametrize("ratio,cleaned", [(0.5, False), (3.0, True)])
def test_worker_source_preserving_mode_respects_seed_limits(tmp_path, db, mode, ratio, cleaned):
    music, staging, *_ = _seed(db, tmp_path)
    db.update_media_management_settings(
        {"delete_completed_transfers": True, "import_mode": mode, "seed_ratio_limit": 2.0}
    )
    dl = staging / "Daft.Punk.Discovery.FLAC"
    _flac(dl / "good.flac")
    driver = MagicMock()
    status = {"status": DownloadStatus.COMPLETED.value, "progress": 100.0, "size_bytes": 1, "speed_bps": 0,
              "eta_seconds": 0, "source_path": str(dl), "error_message": None, "ratio": ratio}
    driver.get_status.return_value = status

    def inspect(path):
        return {"artist": "Daft Punk", "album": "Discovery", "disc_number": 1, "codec": "FLAC",
                "bits_per_sample": 16, "bitrate": 900, "sample_rate": 44100, "extension": ".flac", **MATCHED}

    with patch("plex_playlist_sync.acquisition_worker.get_acquisition_driver", return_value=driver), \
         patch("plex_playlist_sync.acquisition_worker.inspect_audio_file", side_effect=inspect), \
         patch("plex_playlist_sync.acquisition_worker.fingerprint_audio_file", return_value=None):
        AcquisitionWorker().poll_once(db=db, staging_dir=str(staging))
    assert (dl / "good.flac").exists()
    if cleaned:
        driver.cleanup_completed.assert_called_once()
        assert db.get_active_download("dl-1")["status"] == "imported"
    else:
        driver.cleanup_completed.assert_not_called()
        assert db.get_active_download("dl-1")["status"] == "completed"

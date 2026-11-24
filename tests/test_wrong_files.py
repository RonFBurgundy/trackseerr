"""Routes and service tests for library-wide AcoustID "Wrong files" checks."""

import os
from pathlib import Path
from typing import Any

import pytest

import trackseerr.rename_verification as rv
import trackseerr.wrong_files as wf
from tests.test_library_api import (  # noqa: F401  (fixtures are used by name)
    _auth_headers,
    _create_minimal_flac,
    app_and_client,
    seeded_users,
    test_config,
    test_db,
)
from trackseerr.storage import Database

R1 = "11111111-1111-1111-1111-111111111111"
R2 = "22222222-2222-2222-2222-222222222222"
R3 = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def _reset_state():
    with rv._state_lock:
        rv._state.clear()
        rv._state.update(rv._fresh_state())
    yield


def _settings(db: Database, root: Path, key: str | None = None) -> None:
    db.update_media_management_settings({
        "root_folder_path": str(root),
        "standard_track_format": "{track:00} - {Track Title}",
        "album_folder_format": "{Album Title} ({Release Year})",
        **({"acoustid_api_key": key} if key else {}),
    })


def _seed(db: Database, root: Path, idx: int, rec: str | None, number: int = 3, album: str = "alb") -> tuple[str, Path]:
    db.upsert_library_artist({"id": "art", "name": "Radiohead", "monitored": True})
    db.upsert_library_album({"id": album, "artist_id": "art", "title": "The Bends", "year": 1995, "monitored": True})
    db.upsert_library_track({
        "id": f"trk-{idx}", "album_id": album, "artist_id": "art", "title": f"Song {idx}",
        "track_number": number, "disc_number": 1, "monitored": True, "mb_recording_id": rec,
    })
    path = root / "old" / f"file_{idx}.flac"
    _create_minimal_flac(path)
    db.upsert_library_file({
        "id": f"fl-{idx}", "track_id": f"trk-{idx}", "file_path": str(path),
        "relative_path": f"old/file_{idx}.flac", "codec": "FLAC",
    })
    return f"fl-{idx}", path


def _cache(
    db: Database,
    fid: str,
    path: Path,
    rids: list[str],
    status: str = "ok",
    title: str = "Actual",
    artist: str = "Radiohead",
) -> None:
    st = os.stat(path)
    db.upsert_file_fingerprint({
        "file_id": fid, "file_path": str(path), "size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns,
        "status": status, "duration": 100.0, "fingerprint": "fp", "error": None,
        "matches": [{"recording_id": r, "title": title, "artist": artist, "score": 0.95} for r in rids],
    })


@pytest.fixture
def admin(test_db, test_config, seeded_users, tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    _settings(test_db, root)
    return _auth_headers(seeded_users["admin"], test_db, test_config), root


def test_list_wrong_files_returns_only_mismatches(app_and_client, test_db, admin):
    _, client = app_and_client
    headers, root = admin
    f1, p1 = _seed(test_db, root, 1, R1, number=1)
    f2, p2 = _seed(test_db, root, 2, R2, number=2)
    f3, p3 = _seed(test_db, root, 3, R3, number=3)

    _cache(test_db, f1, p1, [R1])  # verified
    _cache(test_db, f2, p2, [R1])  # mismatch -> points to trk-1
    _cache(test_db, f3, p3, [], status="no_result")  # unknown

    resp = client.get("/api/library/wrong-files", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["checked"] == 3
    assert body["library_files"] == 3
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["file_id"] == f2
    assert item["track_id"] == "trk-2"
    assert item["actual_track_id"] == "trk-1"
    assert item["actual_track_title"] == "Song 1"
    assert item["actual_album_title"] == "The Bends"
    assert item["actual_artist_name"] == "Radiohead"


def test_list_wrong_files_mismatch_outside_library(app_and_client, test_db, admin):
    _, client = app_and_client
    headers, root = admin
    f1, p1 = _seed(test_db, root, 1, R1, number=1)
    _cache(test_db, f1, p1, [R3], title="External Song", artist="Other Artist")

    resp = client.get("/api/library/wrong-files", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["file_id"] == f1
    assert item["actual_track_id"] is None
    assert item["actual_track_title"] is None
    assert item["actual_title"] == "External Song"
    assert item["actual_artist"] == "Other Artist"


def test_check_requires_api_key(app_and_client, admin):
    _, client = app_and_client
    headers, _ = admin
    resp = client.post("/api/library/wrong-files/check", headers=headers)
    assert resp.status_code == 400
    assert "AcoustID API key" in resp.json()["detail"]


def test_check_starts_verification_over_all_files(app_and_client, test_db, admin, monkeypatch):
    _, client = app_and_client
    headers, root = admin
    _settings(test_db, root, key="k")
    f1, _ = _seed(test_db, root, 1, R1, number=1)
    f2, _ = _seed(test_db, root, 2, R2, number=2)

    calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(wf, "start_verification", lambda db, ids, key: calls.append((ids, key)) or True)

    resp = client.post("/api/library/wrong-files/check", headers=headers)
    assert resp.status_code == 200
    assert len(calls) == 1
    assert set(calls[0][0]) == {f1, f2}
    assert calls[0][1] == "k"
    assert resp.json()["api_key_set"] is True

    status_resp = client.get("/api/library/wrong-files/check", headers=headers)
    assert status_resp.status_code == 200
    assert status_resp.json()["api_key_set"] is True


def test_check_conflict_when_running(app_and_client, test_db, admin, monkeypatch):
    _, client = app_and_client
    headers, root = admin
    _settings(test_db, root, key="k")
    monkeypatch.setattr(wf, "start_verification", lambda db, ids, key: False)

    resp = client.post("/api/library/wrong-files/check", headers=headers)
    assert resp.status_code == 409
    assert "already running" in resp.json()["detail"]


def test_wrong_files_requires_admin(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)

    assert client.get("/api/library/wrong-files", headers=alice_headers).status_code == 403
    assert client.post("/api/library/wrong-files/check", headers=alice_headers).status_code == 403
    assert client.get("/api/library/wrong-files/check", headers=alice_headers).status_code == 403

"""Routes and rename preview/apply wiring for AcoustID verification (cache rows are seeded; no network, no fpcalc)."""

import os
from pathlib import Path
from typing import Any

import pytest

import trackseerr.api.routes.library.tagging as tagging
import trackseerr.rename_verification as rv
from tests.test_library_api import (  # noqa: F401  (fixtures are used by name)
    _auth_headers,
    _create_minimal_flac,
    app_and_client,
    seeded_users,
    test_config,
    test_db,
)
from trackseerr.storage import Database

R1, R2 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


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
    """A track + file at a legacy path whose proposed path differs (and numbering differs from the file name)."""
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


def _cache(db: Database, fid: str, path: Path, rids: list[str], file_path: str | None = None) -> None:
    st = os.stat(path)
    db.upsert_file_fingerprint({
        "file_id": fid, "file_path": file_path or str(path), "size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns,
        "status": "ok", "duration": 100.0, "fingerprint": "fp", "error": None,
        "matches": [{"recording_id": r, "title": "Actual", "artist": "Radiohead", "score": 0.95} for r in rids],
    })


def _preview(client: Any, headers: dict[str, str]) -> dict[str, dict[str, Any]]:
    resp = client.post("/api/library/rename/preview", json={"album_id": "alb"}, headers=headers)
    assert resp.status_code == 200
    return {row["file_id"]: row for row in resp.json()}


@pytest.fixture
def admin(test_db, test_config, seeded_users, tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    _settings(test_db, root)
    return _auth_headers(seeded_users["admin"], test_db, test_config), root


def test_preview_marks_verified_row_selected(app_and_client, test_db, admin):
    _, client = app_and_client
    headers, root = admin
    fid, path = _seed(test_db, root, 1, R1)
    _cache(test_db, fid, path, [R1])
    row = _preview(client, headers)[fid]
    assert "track_number" in row["reasons"] or "file_title" in row["reasons"]
    assert "verified" in row["reasons"]
    assert row["default_selected"] is True
    assert row["verification"]["status"] == "verified"


def test_preview_marks_mismatch_and_links_actual_track(app_and_client, test_db, admin):
    _, client = app_and_client
    headers, root = admin
    fid, path = _seed(test_db, root, 1, R1)
    _seed(test_db, root, 2, R2, number=4)
    _cache(test_db, fid, path, [R2])
    row = _preview(client, headers)[fid]
    assert "audio_mismatch" in row["reasons"]
    assert row["default_selected"] is False
    assert row["verification"]["status"] == "mismatch"
    assert row["verification"]["actual_track_id"] == "trk-2"


def test_preview_ignores_stale_cache_path(app_and_client, test_db, admin):
    _, client = app_and_client
    headers, root = admin
    fid, path = _seed(test_db, root, 1, R1)
    _cache(test_db, fid, path, [R1], file_path=str(root / "elsewhere.flac"))
    row = _preview(client, headers)[fid]
    assert row.get("verification") is None
    assert "verified" not in row["reasons"]


def test_verify_requires_api_key(app_and_client, admin):
    _, client = app_and_client
    headers, _ = admin
    resp = client.post("/api/library/rename/verify", json={"file_ids": ["a"]}, headers=headers)
    assert resp.status_code == 400
    assert "AcoustID API key" in resp.json()["detail"]


def test_verify_rejects_empty_ids(app_and_client, test_db, admin):
    _, client = app_and_client
    headers, root = admin
    _settings(test_db, root, key="k")
    resp = client.post("/api/library/rename/verify", json={"file_ids": []}, headers=headers)
    assert resp.status_code == 400


def test_verify_conflict_when_running(app_and_client, test_db, admin, monkeypatch):
    _, client = app_and_client
    headers, root = admin
    _settings(test_db, root, key="k")
    monkeypatch.setattr(tagging, "start_verification", lambda db, ids, key: False)
    resp = client.post("/api/library/rename/verify", json={"file_ids": ["a"]}, headers=headers)
    assert resp.status_code == 409


def test_verify_starts_run(app_and_client, test_db, admin, monkeypatch):
    _, client = app_and_client
    headers, root = admin
    _settings(test_db, root, key="k")
    calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(tagging, "start_verification", lambda db, ids, key: calls.append((ids, key)) or True)
    resp = client.post("/api/library/rename/verify", json={"file_ids": ["a", "b"]}, headers=headers)
    assert resp.status_code == 200
    assert calls == [(["a", "b"], "k")]
    assert resp.json()["api_key_set"] is True


def test_verify_status_reports_api_key_set(app_and_client, test_db, admin):
    _, client = app_and_client
    headers, root = admin
    body = client.get("/api/library/rename/verify/status", headers=headers).json()
    assert body["api_key_set"] is False and body["running"] is False and body["done"] == 0
    _settings(test_db, root, key="k")
    assert client.get("/api/library/rename/verify/status", headers=headers).json()["api_key_set"] is True


def test_apply_blocks_mismatch_but_moves_others(app_and_client, test_db, admin):
    _, client = app_and_client
    headers, root = admin
    good, good_path = _seed(test_db, root, 1, R1)
    bad, bad_path = _seed(test_db, root, 2, R2, number=4)
    _cache(test_db, bad, bad_path, [R1])  # audio of file 2 is track 1's recording
    resp = client.post("/api/library/rename/apply", json={"file_ids": [good, bad]}, headers=headers)
    assert resp.status_code == 200
    res = resp.json()
    assert res["renamed_count"] == 1
    assert res["blocked_count"] == 1
    assert len(res["errors"]) == 1 and "fix the file link first" in res["errors"][0]
    assert bad_path.exists()
    assert not good_path.exists()


def test_verify_routes_require_admin(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    headers = _auth_headers(seeded_users["alice"], test_db, test_config)
    assert client.post("/api/library/rename/verify", json={"file_ids": ["a"]}, headers=headers).status_code == 403
    assert client.get("/api/library/rename/verify/status", headers=headers).status_code == 403

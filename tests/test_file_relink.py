"""Relinking a file to the track its audio really is: unit behaviour and the /rename/relink route."""

from pathlib import Path
from typing import Any

import pytest

from tests.test_library_api import (  # noqa: F401  (fixtures are used by name)
    _auth_headers,
    _create_minimal_flac,
    app_and_client,
    seeded_users,
    test_config,
    test_db,
)
from tests.test_rename_verify_api import R1, R2, _cache, _preview, _seed
from trackseerr.file_relink import RelinkConflict, quality_key, relink_file
from trackseerr.storage import Database

URL = "/api/library/rename/relink"


@pytest.fixture
def env(test_db, test_config, seeded_users, tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    test_db.update_media_management_settings({
        "root_folder_path": str(root),
        "recycle_bin_path": str(tmp_path / "bin"),
        "standard_track_format": "{track:00} - {Track Title}",
        "album_folder_format": "{Album Title} ({Release Year})",
    })
    return _auth_headers(seeded_users["admin"], test_db, test_config), root, tmp_path / "bin"


def _two(db: Database, root: Path, codec2: str = "FLAC", codec1: str = "FLAC") -> tuple[Path, Path]:
    """trk-1 (file fl-1) is the mislinked one; trk-2 has fl-2. Codecs are set on the rows."""
    _, p1 = _seed(db, root, 1, R1)
    _, p2 = _seed(db, root, 2, R2, number=4)
    for fid, tid, p, codec in (("fl-1", "trk-1", p1, codec1), ("fl-2", "trk-2", p2, codec2)):
        db.upsert_library_file({
            "id": fid, "track_id": tid, "file_path": str(p), "relative_path": f"old/{p.name}", "codec": codec,
            "bitrate": 320 if codec == "MP3" else None, "size_bytes": 100,
        })
    return p1, p2


def _alone(db: Database, root: Path) -> Path:
    """trk-1/fl-1 plus trk-2 with no file."""
    _, p1 = _seed(db, root, 1, R1)
    _seed(db, root, 2, R2, number=4)
    db.delete_library_file("fl-2")
    return p1


def _post(client: Any, headers: dict[str, str], conflict: str | None = None, file_id: str = "fl-1", target: str = "trk-2"):
    return client.post(URL, json={"file_id": file_id, "target_track_id": target, "conflict": conflict}, headers=headers)


def _recycled(bin_dir: Path) -> list[Path]:
    return [p for p in bin_dir.rglob("*") if p.is_file()] if bin_dir.exists() else []


def test_relink_moves_file_to_target_without_file(app_and_client, test_db, env):
    _, client = app_and_client
    headers, root, _ = env
    _alone(test_db, root)
    resp = _post(client, headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "action": "relinked", "kept_file_id": "fl-1", "recycled_file_id": None,
        "old_track_id": "trk-1", "target_track_id": "trk-2",
    }
    assert test_db.get_library_file("fl-1")["track_id"] == "trk-2"
    assert test_db.list_library_files_for_track("trk-1") == []
    for tid in ("trk-1", "trk-2"):
        events = [e["event"] for e in test_db.list_item_events("track", tid)]
        assert "relinked" in events


def test_relink_conflict_returns_both_files(app_and_client, test_db, env):
    _, client = app_and_client
    headers, root, _ = env
    _two(test_db, root)
    resp = _post(client, headers)
    assert resp.status_code == 409
    body = resp.json()
    assert body["existing"]["id"] == "fl-2"
    assert body["incoming"]["id"] == "fl-1"
    assert body["existing"]["codec"] == "FLAC"
    assert "choose which to keep" in body["detail"]
    assert test_db.get_library_file("fl-1")["track_id"] == "trk-1"


def test_relink_keep_incoming_recycles_existing(app_and_client, test_db, env):
    _, client = app_and_client
    headers, root, bin_dir = env
    _, p2 = _two(test_db, root)
    resp = _post(client, headers, "keep_incoming")
    assert resp.status_code == 200
    assert resp.json()["recycled_file_id"] == "fl-2"
    assert test_db.get_library_file("fl-2") is None
    assert test_db.get_library_file("fl-1")["track_id"] == "trk-2"
    assert not p2.exists()
    assert [p.name for p in _recycled(bin_dir)] == [p2.name]


def test_relink_keep_existing_recycles_incoming(app_and_client, test_db, env):
    _, client = app_and_client
    headers, root, bin_dir = env
    p1, p2 = _two(test_db, root)
    resp = _post(client, headers, "keep_existing")
    assert resp.status_code == 200
    assert resp.json()["action"] == "kept_existing"
    assert resp.json()["kept_file_id"] == "fl-2"
    assert test_db.get_library_file("fl-1") is None
    assert test_db.get_library_file("fl-2")["track_id"] == "trk-2"
    assert p2.exists() and not p1.exists()
    assert [p.name for p in _recycled(bin_dir)] == [p1.name]


def test_relink_prefer_quality_keeps_flac_over_mp3(app_and_client, test_db, env):
    _, client = app_and_client
    headers, root, _ = env
    _two(test_db, root, codec2="MP3", codec1="FLAC")  # incoming FLAC beats existing MP3
    resp = _post(client, headers, "prefer_quality")
    assert resp.status_code == 200
    assert resp.json()["recycled_file_id"] == "fl-2"
    assert test_db.get_library_file("fl-1")["track_id"] == "trk-2"


def test_relink_prefer_quality_keeps_existing_flac_over_incoming_mp3(app_and_client, test_db, env):
    _, client = app_and_client
    headers, root, _ = env
    _two(test_db, root, codec2="FLAC", codec1="MP3")
    resp = _post(client, headers, "prefer_quality")
    assert resp.status_code == 200
    assert resp.json()["action"] == "kept_existing"
    assert test_db.get_library_file("fl-1") is None
    assert test_db.get_library_file("fl-2") is not None


def test_relink_prefer_quality_tie_conflicts(app_and_client, test_db, env):
    _, client = app_and_client
    headers, root, _ = env
    _two(test_db, root)
    resp = _post(client, headers, "prefer_quality")
    assert resp.status_code == 409
    assert test_db.get_library_file("fl-1")["track_id"] == "trk-1"


def test_relink_same_track_400(app_and_client, test_db, env):
    _, client = app_and_client
    headers, root, _ = env
    _alone(test_db, root)
    assert _post(client, headers, target="trk-1").status_code == 400


def test_relink_unknown_ids_404(app_and_client, test_db, env):
    _, client = app_and_client
    headers, root, _ = env
    _alone(test_db, root)
    assert _post(client, headers, file_id="nope").status_code == 404
    assert _post(client, headers, target="nope").status_code == 404


def test_relink_requires_admin(app_and_client, test_db, test_config, seeded_users, env):
    _, client = app_and_client
    _, root, _ = env
    _alone(test_db, root)
    alice = _auth_headers(seeded_users["alice"], test_db, test_config)
    assert _post(client, alice).status_code == 403
    assert test_db.get_library_file("fl-1")["track_id"] == "trk-1"


def test_relink_disposal_failure_keeps_rows(test_db, env, monkeypatch):
    _, root, _ = env
    p1, p2 = _two(test_db, root)
    import trackseerr.file_relink as fr
    from trackseerr.recycle_bin import DisposeResult

    monkeypatch.setattr(fr, "dispose_for_settings", lambda *a, **k: DisposeResult("kept", p2, reason="could not move"))
    with pytest.raises(OSError):
        relink_file(test_db, "fl-1", "trk-2", "keep_incoming")
    assert test_db.get_library_file("fl-2") is not None
    assert test_db.get_library_file("fl-1")["track_id"] == "trk-1"
    assert p1.exists() and p2.exists()


def test_relink_conflict_raises_without_choice(test_db, env):
    _, root, _ = env
    _two(test_db, root)
    with pytest.raises(RelinkConflict) as info:
        relink_file(test_db, "fl-1", "trk-2", None)
    assert info.value.existing["id"] == "fl-2" and info.value.incoming["id"] == "fl-1"
    with pytest.raises(ValueError):
        relink_file(test_db, "fl-1", "trk-2", "bogus")


def test_quality_key_orders_lossless_then_depth_then_rate():
    mp3 = {"codec": "mp3", "bitrate": 320, "size_bytes": None}
    flac16 = {"codec": "flac", "bits_per_sample": 16, "sample_rate": 44100}
    flac24 = {"codec": "FLAC", "bits_per_sample": 24, "sample_rate": 96000}
    assert quality_key(mp3) < quality_key(flac16) < quality_key(flac24)


def test_relinked_row_previews_verified(app_and_client, test_db, env):
    _, client = app_and_client
    headers, root, _ = env
    p1 = _alone(test_db, root)
    _cache(test_db, "fl-1", p1, [R2])
    assert _preview(client, headers)["fl-1"]["verification"]["status"] == "mismatch"
    assert _post(client, headers).status_code == 200
    _cache(test_db, "fl-1", p1, [R2])
    assert _preview(client, headers)["fl-1"]["verification"]["status"] == "verified"

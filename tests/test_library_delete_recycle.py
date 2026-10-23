"""Admin library deletes go through the recycle bin and dispose of every file of a track."""

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
from trackseerr.storage import Database


@pytest.fixture
def env(test_db, test_config, seeded_users, tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    test_db.update_media_management_settings({
        "root_folder_path": str(root),
        "recycle_bin_path": str(tmp_path / "bin"),
    })
    return _auth_headers(seeded_users["admin"], test_db, test_config), root, tmp_path / "bin"


def _seed(db: Database, track_id: str = "trk-1") -> None:
    db.upsert_library_artist({"id": "art-1", "name": "Artist 1"})
    db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": "Album 1"})
    db.upsert_library_track({"id": track_id, "album_id": "alb-1", "artist_id": "art-1", "title": f"Track {track_id}"})


def _file(db: Database, file_id: str, track_id: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _create_minimal_flac(path)
    db.upsert_library_file({"id": file_id, "track_id": track_id, "file_path": str(path)})
    return path


def _in_bin(bin_dir: Path) -> list[Path]:
    return [p for p in bin_dir.rglob("*") if p.is_file()] if bin_dir.exists() else []


def _deleted_events(db: Database, track_id: str = "trk-1") -> list[dict[str, Any]]:
    return [e for e in db.list_item_events("track", track_id) if e["event"] == "file_deleted"]


def test_delete_track_recycles_every_file(app_and_client, test_db, env):
    _, client = app_and_client
    headers, root, bin_dir = env
    _seed(test_db)
    p1 = _file(test_db, "fl-1", "trk-1", root / "a" / "one.flac")
    p2 = _file(test_db, "fl-2", "trk-1", root / "a" / "two.flac")

    resp = client.delete("/api/library/tracks/trk-1?delete_files=true", headers=headers)

    assert resp.status_code == 200
    assert not p1.exists() and not p2.exists()
    assert {p.name for p in _in_bin(bin_dir)} == {"one.flac", "two.flac"}
    events = _deleted_events(test_db)
    assert len(events) == 2
    assert all(e["details"].get("recycled_to") for e in events)


def test_delete_file_permanent_when_setting_on(app_and_client, test_db, env):
    _, client = app_and_client
    headers, root, bin_dir = env
    test_db.update_media_management_settings({"recycle_bin_permanent_delete": True})
    _seed(test_db)
    p1 = _file(test_db, "fl-1", "trk-1", root / "one.flac")

    resp = client.delete("/api/library/files/fl-1", headers=headers)

    assert resp.status_code == 200
    assert not p1.exists()
    assert _in_bin(bin_dir) == []


def test_delete_outside_library_root_is_kept(app_and_client, test_db, env, tmp_path):
    _, client = app_and_client
    headers, _root, bin_dir = env
    _seed(test_db)
    p1 = _file(test_db, "fl-1", "trk-1", tmp_path / "elsewhere" / "one.flac")

    resp = client.delete("/api/library/tracks/trk-1?delete_files=true", headers=headers)

    assert resp.status_code == 200
    assert p1.exists()
    assert test_db.get_library_track("trk-1") is None
    assert _in_bin(bin_dir) == []
    assert _deleted_events(test_db) == []


def test_delete_album_and_artist_recycle(app_and_client, test_db, env):
    _, client = app_and_client
    headers, root, bin_dir = env
    _seed(test_db)
    p1 = _file(test_db, "fl-1", "trk-1", root / "album_song.flac")

    assert client.delete("/api/library/albums/alb-1?delete_files=true", headers=headers).status_code == 200
    assert not p1.exists()
    assert {p.name for p in _in_bin(bin_dir)} == {"album_song.flac"}

    _seed(test_db, "trk-2")
    p2 = _file(test_db, "fl-2", "trk-2", root / "artist_song.flac")
    assert client.delete("/api/library/artists/art-1?delete_files=true", headers=headers).status_code == 200
    assert not p2.exists()
    assert {p.name for p in _in_bin(bin_dir)} == {"album_song.flac", "artist_song.flac"}


def test_delete_without_delete_files_leaves_disk_alone(app_and_client, test_db, env):
    _, client = app_and_client
    headers, root, bin_dir = env
    _seed(test_db)
    p1 = _file(test_db, "fl-1", "trk-1", root / "one.flac")

    resp = client.delete("/api/library/tracks/trk-1?delete_files=false", headers=headers)

    assert resp.status_code == 200
    assert p1.exists()
    assert _in_bin(bin_dir) == []

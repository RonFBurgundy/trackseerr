"""Tests for shortening long file and folder names in library health."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_active_media_server, get_config, get_db
from trackseerr.api.routes.library.tagging import _rename_meta
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.library_health import KIND_LONG_NAME, shorten_long_name
from trackseerr.media_servers import refresh_scheduler
from trackseerr.media_servers.base import MediaServer
from trackseerr.naming import MAX_NAME_BYTES, build_track_path, truncate_utf8_bytes
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()


@pytest.fixture
def music_root(tmp_path: Path, db: Database) -> Path:
    m = tmp_path / "music"
    m.mkdir(parents=True, exist_ok=True)
    mm = db.get_media_management_settings()
    mm["root_folder_path"] = str(m)
    db.update_media_management_settings(mm)
    return m


@pytest.fixture
def config(tmp_path: Path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def client(db, config):
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_active_media_server] = lambda: None
    return TestClient(app)


def _auth_headers(db: Database, config: Config, *, admin: bool = True) -> dict[str, str]:
    user = db.upsert_user("a-1" if admin else "u-1", "admin" if admin else "user", "a@example.com", is_admin=admin)
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(user_id=user["id"], username="admin", is_admin=admin, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def test_shorten_unlinked_file(db, music_root):
    long_name = "Track_" + "a" * 219 + ".flac"
    assert len(long_name.encode("utf-8")) == 230
    file_path = music_root / "Artist" / "Album" / long_name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"audio-data")

    db.upsert_library_health_findings(
        [{
            "kind": KIND_LONG_NAME,
            "cause": "name_too_long",
            "group_key": "Artist/Album",
            "path": str(file_path),
            "detail": {"name_bytes": 230, "limit": MAX_NAME_BYTES, "is_dir": False},
        }],
        "2026-01-01T00:00:00Z",
    )
    findings = db.list_library_health_findings()
    assert len(findings) == 1
    finding = findings[0]

    res = shorten_long_name(db, finding, music_root)
    assert res["method"] == "shorten"
    assert not file_path.exists()
    new_p = Path(res["new_path"])
    assert new_p.exists()
    assert len(new_p.name.encode("utf-8")) <= MAX_NAME_BYTES
    assert new_p.suffix == ".flac"
    assert len(db.list_library_health_findings()) == 0


def test_shorten_linked_file_uses_rename_template(db, music_root):
    db.upsert_library_artist({"id": "art-1", "name": "Sufjan Stevens", "monitored": True})
    db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": "Illinois", "year": 2005, "monitored": True})
    db.upsert_library_track({
        "id": "trk-1",
        "artist_id": "art-1",
        "album_id": "alb-1",
        "title": "The Black Hawk War",
        "track_number": 2,
        "disc_number": 1,
        "monitored": True,
    })

    long_name = "02 - " + "X" * 219 + ".flac"
    assert len(long_name.encode("utf-8")) == 229
    folder = music_root / "Sufjan Stevens" / "Illinois (2005)"
    folder.mkdir(parents=True, exist_ok=True)
    long_file = folder / long_name
    long_file.write_bytes(b"audio-content")

    db.upsert_library_file({
        "id": "fil-1",
        "track_id": "trk-1",
        "file_path": str(long_file),
        "relative_path": str(long_file.relative_to(music_root)),
        "codec": "FLAC",
        "quality_name": "FLAC",
        "size_bytes": 13,
    })

    db.upsert_library_health_findings(
        [{
            "kind": KIND_LONG_NAME,
            "cause": "name_too_long",
            "group_key": str(folder),
            "path": str(long_file),
            "detail": {"name_bytes": len(long_name.encode("utf-8")), "limit": MAX_NAME_BYTES, "is_dir": False},
        }],
        "2026-01-01T00:00:00Z",
    )
    finding = db.list_library_health_findings()[0]

    media_settings = db.get_media_management_settings()
    track = db.get_library_track("trk-1")
    album = db.get_library_album("alb-1")
    artist = db.get_library_artist("art-1")
    meta = _rename_meta(db, track, album, artist, db.get_library_file("fil-1"), str(long_file), {}, media_settings)
    expected_path = build_track_path(meta, media_settings)

    mock_server = MagicMock()
    res = shorten_long_name(db, finding, music_root, server=mock_server)
    assert res["method"] == "rename"
    assert res["new_path"] == expected_path
    assert not long_file.exists()
    assert Path(expected_path).exists()
    updated_file = db.get_library_file("fil-1")
    assert updated_file["file_path"] == expected_path
    assert len(db.list_library_health_findings()) == 0


def test_shorten_lyrics_sidecar(db, music_root):
    long_name = "Song_" + "a" * 220 + ".lrc"
    assert len(long_name.encode("utf-8")) == 229
    file_path = music_root / "Artist" / "Album" / long_name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("[00:01.00] lyrics content", encoding="utf-8")

    db.upsert_library_health_findings(
        [{
            "kind": KIND_LONG_NAME,
            "cause": "name_too_long",
            "group_key": "Artist/Album",
            "path": str(file_path),
            "detail": {"name_bytes": len(long_name.encode("utf-8")), "limit": MAX_NAME_BYTES, "is_dir": False},
        }],
        "2026-01-01T00:00:00Z",
    )
    finding = db.list_library_health_findings()[0]

    res = shorten_long_name(db, finding, music_root)
    assert res["method"] == "shorten"
    assert not file_path.exists()
    new_p = Path(res["new_path"])
    assert new_p.exists()
    assert len(new_p.name.encode("utf-8")) <= MAX_NAME_BYTES
    assert new_p.suffix == ".lrc"
    assert len(db.list_library_health_findings()) == 0


def test_shorten_folder_repoints_library_rows(db, music_root):
    long_folder_name = "Folder_" + "F" * 223
    assert len(long_folder_name.encode("utf-8")) == 230
    long_folder = music_root / long_folder_name
    long_folder.mkdir(parents=True, exist_ok=True)

    sibling_name = long_folder_name + "X"
    sibling_folder = music_root / sibling_name
    sibling_folder.mkdir(parents=True, exist_ok=True)

    f1 = long_folder / "01.flac"
    f1.write_bytes(b"track 1")
    f2 = long_folder / "02.flac"
    f2.write_bytes(b"track 2")
    f_sib = sibling_folder / "01.flac"
    f_sib.write_bytes(b"sibling track")

    db.upsert_library_artist({"id": "art-f", "name": "Folder Artist", "monitored": True})
    db.upsert_library_album({"id": "alb-f", "artist_id": "art-f", "title": "Folder Album", "year": 2020, "monitored": True})
    db.upsert_library_track({"id": "trk-f1", "artist_id": "art-f", "album_id": "alb-f", "title": "Track 1", "track_number": 1, "disc_number": 1, "monitored": True})
    db.upsert_library_track({"id": "trk-f2", "artist_id": "art-f", "album_id": "alb-f", "title": "Track 2", "track_number": 2, "disc_number": 1, "monitored": True})
    db.upsert_library_track({"id": "trk-fsib", "artist_id": "art-f", "album_id": "alb-f", "title": "Track Sib", "track_number": 1, "disc_number": 1, "monitored": True})

    db.upsert_library_file({
        "id": "fil-1",
        "track_id": "trk-f1",
        "file_path": str(f1),
        "relative_path": f"{long_folder_name}/01.flac",
        "codec": "FLAC",
        "quality_name": "FLAC",
    })
    db.upsert_library_file({
        "id": "fil-2",
        "track_id": "trk-f2",
        "file_path": str(f2),
        "relative_path": f"{long_folder_name}/02.flac",
        "codec": "FLAC",
        "quality_name": "FLAC",
    })
    db.upsert_library_file({
        "id": "fil-sib",
        "track_id": "trk-fsib",
        "file_path": str(f_sib),
        "relative_path": f"{sibling_name}/01.flac",
        "codec": "FLAC",
        "quality_name": "FLAC",
    })

    db.upsert_library_health_findings(
        [{
            "kind": KIND_LONG_NAME,
            "cause": "name_too_long",
            "group_key": str(music_root),
            "path": str(long_folder),
            "detail": {"name_bytes": 230, "limit": MAX_NAME_BYTES, "is_dir": True},
        }],
        "2026-01-01T00:00:00Z",
    )
    finding = db.list_library_health_findings()[0]

    res = shorten_long_name(db, finding, music_root)
    assert res["method"] == "shorten_folder"
    assert not long_folder.exists()
    new_folder = Path(res["new_path"])
    assert new_folder.exists()
    assert len(new_folder.name.encode("utf-8")) <= MAX_NAME_BYTES

    # Check repointed rows
    row1 = db.get_library_file("fil-1")
    row2 = db.get_library_file("fil-2")
    row_sib = db.get_library_file("fil-sib")

    assert row1["file_path"] == str(new_folder / "01.flac")
    assert row1["relative_path"] == str((new_folder / "01.flac").relative_to(music_root))
    assert row2["file_path"] == str(new_folder / "02.flac")
    assert row2["relative_path"] == str((new_folder / "02.flac").relative_to(music_root))

    # Sibling is untouched
    assert row_sib["file_path"] == str(f_sib)
    assert row_sib["relative_path"] == f"{sibling_name}/01.flac"
    assert f_sib.exists()

    assert len(db.list_library_health_findings()) == 0


def test_shorten_collision_gets_suffix(db, music_root):
    long_name = "Collision_" + "z" * 216 + ".flac"
    assert len(long_name.encode("utf-8")) == 231
    stem = truncate_utf8_bytes("Collision_" + "z" * 216, MAX_NAME_BYTES - len(".flac".encode())).rstrip(" .")

    # Pre-create the collision destination
    existing_file = music_root / f"{stem}.flac"
    existing_file.write_bytes(b"existing")

    colliding_file = music_root / long_name
    colliding_file.write_bytes(b"new")

    db.upsert_library_health_findings(
        [{
            "kind": KIND_LONG_NAME,
            "cause": "name_too_long",
            "group_key": str(music_root),
            "path": str(colliding_file),
            "detail": {"name_bytes": 231, "limit": MAX_NAME_BYTES, "is_dir": False},
        }],
        "2026-01-01T00:00:00Z",
    )
    finding = db.list_library_health_findings()[0]

    res = shorten_long_name(db, finding, music_root)
    assert res["method"] == "shorten"
    assert existing_file.exists()
    new_p = Path(res["new_path"])
    assert new_p.exists()
    assert new_p != existing_file
    assert " (1)" in new_p.name
    assert not colliding_file.exists()


def test_shorten_rejects_path_outside_root(client, db, config, tmp_path):
    headers = _auth_headers(db, config, admin=True)
    outside_file = tmp_path / "outside" / ("bad_" + "x" * 210 + ".flac")
    outside_file.parent.mkdir(parents=True, exist_ok=True)
    outside_file.write_bytes(b"outside")

    db.upsert_library_health_findings(
        [{
            "kind": KIND_LONG_NAME,
            "cause": "name_too_long",
            "group_key": "outside",
            "path": str(outside_file),
            "detail": {"name_bytes": 218, "limit": MAX_NAME_BYTES, "is_dir": False},
        }],
        "2026-01-01T00:00:00Z",
    )
    finding = db.list_library_health_findings()[0]

    resp = client.post(f"/api/library-health/findings/{finding['id']}/shorten", headers=headers)
    assert resp.status_code == 400
    assert "outside music root" in resp.json()["detail"]


def test_shorten_missing_path_removes_finding(client, db, config, music_root):
    headers = _auth_headers(db, config, admin=True)
    missing_file = music_root / ("vanished_" + "y" * 210 + ".flac")

    db.upsert_library_health_findings(
        [{
            "kind": KIND_LONG_NAME,
            "cause": "name_too_long",
            "group_key": "missing",
            "path": str(missing_file),
            "detail": {"name_bytes": 223, "limit": MAX_NAME_BYTES, "is_dir": False},
        }],
        "2026-01-01T00:00:00Z",
    )
    finding = db.list_library_health_findings()[0]

    resp = client.post(f"/api/library-health/findings/{finding['id']}/shorten", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "The file or folder is gone; the finding was removed."
    assert db.get_library_health_finding(finding["id"]) is None


def test_wrong_kind_rejected(db, music_root):
    file_path = music_root / "normal.flac"
    file_path.write_bytes(b"normal")

    finding = {
        "id": "f-wrong",
        "kind": "server_unindexed",
        "cause": "not_scanned_yet",
        "group_key": str(music_root),
        "path": str(file_path),
        "detail": {},
    }

    with pytest.raises(ValueError, match="Finding kind must be 'long_name'"):
        shorten_long_name(db, finding, music_root)


def test_shorten_triggers_folder_rescan(db, music_root):
    refresh_scheduler.reset_for_tests()
    long_name = "Track_" + "a" * 219 + ".flac"
    file_path = music_root / "Artist" / "Album" / long_name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"audio-data")

    db.upsert_library_health_findings(
        [{
            "kind": KIND_LONG_NAME,
            "cause": "name_too_long",
            "group_key": "Artist/Album",
            "path": str(file_path),
            "detail": {"name_bytes": 230, "limit": MAX_NAME_BYTES, "is_dir": False},
        }],
        "2026-01-01T00:00:00Z",
    )
    finding = db.list_library_health_findings()[0]

    server = MagicMock(spec=MediaServer)
    server.kind = "plex"
    server.capabilities = MagicMock(folder_refresh=True)
    server.library_roots.return_value = [str(music_root)]
    server.refresh_paths.return_value = True

    res = shorten_long_name(db, finding, music_root, server=server)
    assert res["method"] == "shorten"
    assert refresh_scheduler.flush_now() is True
    server.refresh_paths.assert_called_once_with([str(file_path.parent)])


"""Strict-mode backstop for the typed ``/api/library`` responses.

``ApiModel`` forbids undeclared keys under test, so every request here fails if a route returns a key its model does
not declare. The scenario is a populated native catalog (artist with a metadata profile, tags, albums, tracks, a file,
a collection) so optional and nested fields are exercised, with an admin and a non-admin where a route serves both.
"""

import struct
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.library_scanner import library_scanner
from trackseerr.lidarr_migration import lidarr_migration_job
from trackseerr.storage import Database

PREFIX = "/api/library"


def _flac(path: Path) -> None:
    sr = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    info = struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00") + sr + b"\x00" * 16
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC\x80\x00\x00\x22" + info)


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "t.db"))
    yield database
    database.close()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


@pytest.fixture
def client(db: Database, config: Config) -> TestClient:
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


def _headers(db: Database, config: Config, user: dict[str, Any]) -> dict[str, str]:
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(db: Database, config: Config) -> dict[str, str]:
    return _headers(db, config, db.upsert_user("admin-1", "admin_user", "a@example.com", is_admin=True))


@pytest.fixture
def alice(db: Database, config: Config) -> dict[str, str]:
    return _headers(db, config, db.upsert_user("alice-1", "alice", "al@example.com", is_admin=False))


@pytest.fixture
def seeded(db: Database, tmp_path: Path) -> dict[str, Any]:
    profile = db.create_metadata_profile("Studio", ["album"], ["studio"])
    db.upsert_library_artist({
        "id": "art-1", "name": "Radiohead", "clean_name": "radiohead", "monitored": True,
        "metadata_profile_id": profile["id"], "mbid": "a74b1b7f-71a5-4011-9441-d0b5e4122711",
        "genres": "rock", "country": "GB", "bio": "Band", "foreign_artist_id": "deezer:artist:399",
        "metadata_json": '{"image_url": "https://example.com/a.jpg"}',
    })
    db.upsert_library_artist({"id": "art-2", "name": "Blur", "clean_name": "blur", "monitored": False})
    db.upsert_library_album({
        "id": "alb-1", "artist_id": "art-1", "title": "OK Computer", "clean_title": "ok computer", "year": 1997,
        "release_date": "1997-05-21", "cover_url": "https://example.com/c.jpg", "monitored": True,
        "album_type": "album", "total_tracks": 2, "foreign_album_id": "deezer:album:1",
    })
    db.upsert_library_track({
        "id": "trk-1", "album_id": "alb-1", "artist_id": "art-1", "title": "Airbag", "clean_title": "airbag",
        "track_number": 1, "disc_number": 1, "duration_seconds": 284.5, "monitored": True,
    })
    db.upsert_library_track({
        "id": "trk-2", "album_id": "alb-1", "artist_id": "art-1", "title": "Lucky", "clean_title": "lucky",
        "track_number": 2, "disc_number": 1, "monitored": True,
    })
    audio = tmp_path / "music" / "Radiohead" / "OK Computer" / "01 - Airbag.flac"
    _flac(audio)
    db.upsert_library_file({
        "id": "fl-1", "track_id": "trk-1", "file_path": str(audio),
        "relative_path": "Radiohead/OK Computer/01 - Airbag.flac", "codec": "FLAC", "bitrate": 900,
        "sample_rate": 44100, "bits_per_sample": 16, "quality_name": "FLAC 16bit 44.1kHz",
        "size_bytes": 1048576, "cutoff_met": True,
    })
    return {"audio": audio, "profile": profile}


def _ok(resp, status: int = 200):
    assert resp.status_code == status, resp.text
    return resp.json()


def test_stats_and_lists(client, admin, seeded):
    assert _ok(client.get(f"{PREFIX}/stats", headers=admin))["artist_count"] == 2
    artists = _ok(client.get(f"{PREFIX}/artists", headers=admin))
    assert {a["id"] for a in artists} == {"art-1", "art-2"}
    assert "tags" in artists[0] and "discovery_id" in artists[0]
    assert _ok(client.get(f"{PREFIX}/albums", headers=admin))[0]["artist_name"] == "Radiohead"
    track = _ok(client.get(f"{PREFIX}/tracks?album_id=alb-1", headers=admin))[0]
    assert track["file"]["file_path"] and track["album_title"] == "OK Computer"


def test_paged_and_index(client, admin, seeded):
    page = _ok(client.get(f"{PREFIX}/artists/paged", headers=admin))
    assert page["mode"] == "native" and page["total"] == 2 and "search_text" not in page["records"][0]
    assert _ok(client.get(f"{PREFIX}/albums/paged", headers=admin))["records"][0]["track_count"] == 2
    assert _ok(client.get(f"{PREFIX}/tracks/paged?album_id=alb-1", headers=admin))["total"] == 2
    for kind in ("artists", "albums", "tracks"):
        index = _ok(client.get(f"{PREFIX}/{kind}/index", headers=admin))
        assert "mode" not in index  # native index never carried a mode key; exclude_unset keeps it absent
        assert index["groups"] and set(index["groups"][0]) == {"label", "offset", "count"}


def test_details_and_mutations(client, admin, seeded):
    detail = _ok(client.get(f"{PREFIX}/artists/art-1", headers=admin))
    assert detail["albums"][0]["in_profile"] is True and detail["albums"][0]["track_file_count"] == 1
    assert detail["metadata_profile_id"] == seeded["profile"]["id"]
    album = _ok(client.get(f"{PREFIX}/albums/alb-1", headers=admin))
    assert album["tracks"][0]["file"]["id"] == "fl-1" and album["tracks"][1]["file"] is None

    assert _ok(client.put(f"{PREFIX}/artists/art-1/monitored", json={"monitored": True}, headers=admin))["id"] == "art-1"
    assert _ok(client.put(f"{PREFIX}/albums/alb-1/monitored", json={"monitored": False}, headers=admin))["id"] == "alb-1"
    assert _ok(client.put(f"{PREFIX}/tracks/trk-1/monitored", json={"monitored": False}, headers=admin))["monitored"] is False
    assert client.put(f"{PREFIX}/tracks/nope/monitored", json={"monitored": True}, headers=admin).status_code == 404

    bulk = _ok(client.post(f"{PREFIX}/artists/bulk-edit", json={"all": True, "monitored": True}, headers=admin))
    assert set(bulk) >= {"artists_updated", "albums_monitored", "albums_unmonitored"}
    assert _ok(client.post(f"{PREFIX}/albums/bulk-edit", json={"album_ids": ["alb-1"], "monitored": False}, headers=admin)) == {"albums_updated": 1}
    assert _ok(client.post(f"{PREFIX}/tracks/bulk-edit", json={"track_ids": ["trk-1"], "monitored": True}, headers=admin)) == {"tracks_updated": 1}


def test_ingest_existing_artist(client, admin, seeded):
    body = _ok(client.post(
        f"{PREFIX}/artists/ingest", json={"foreign_artist_id": "deezer:artist:399", "artist_name": "Radiohead"},
        headers=admin,
    ))
    assert body["already_existed"] is True and body["albums_ingested"] == 0 and body["id"] == "art-1"


def test_refresh_and_search_native(client, admin, seeded):
    result = {"success": True, "artist_id": "art-1", "refreshed_at": "2026-01-01T00:00:00"}
    with patch("trackseerr.api.routes.library.artists.refresh_single_artist", return_value=result):
        assert _ok(client.post(f"{PREFIX}/artists/art-1/refresh", headers=admin)) == result
    failed = {"success": False, "message": "Artist is a system folder; skipped", "artist_id": "art-1"}
    with patch("trackseerr.api.routes.library.artists.refresh_single_artist", return_value=failed):
        assert _ok(client.post(f"{PREFIX}/artists/art-1/refresh", headers=admin)) == failed
    assert client.post(f"{PREFIX}/artists/art-1/search", headers=admin).status_code == 409


def test_metadata_profiles(client, admin, seeded):
    listing = _ok(client.get(f"{PREFIX}/metadata-profiles", headers=admin))
    studio = next(p for p in listing["profiles"] if p["id"] == seeded["profile"]["id"])
    assert studio["artist_count"] == 1 and "album" in listing["primary_types"]
    created = _ok(client.post(
        f"{PREFIX}/metadata-profiles", json={"name": "All", "primary_types": ["album", "ep"], "secondary_types": ["studio"]},
        headers=admin,
    ), 201)
    assert created["artist_count"] == 0
    updated = _ok(client.put(
        f"{PREFIX}/metadata-profiles/{created['id']}",
        json={"name": "All2", "primary_types": ["album"], "secondary_types": ["studio", "live"]}, headers=admin,
    ))
    assert updated["name"] == "All2"
    preview = _ok(client.get(f"{PREFIX}/artists/art-1/metadata-profile-preview?profile_id={created['id']}", headers=admin))
    assert set(preview["would_change"]) == {
        "albums_to_monitor", "albums_to_unmonitor", "tracks_to_monitor", "tracks_to_unmonitor"
    }
    assert _ok(client.delete(f"{PREFIX}/metadata-profiles/{created['id']}", headers=admin)) == {
        "deleted": 1, "artists_cleared": 0,
    }


def test_item_history_admin_and_non_admin(client, admin, alice, seeded):
    for headers in (admin, alice):
        body = _ok(client.get(f"{PREFIX}/artist/art-1/history", headers=headers))
        assert body["entity"] == "artist" and isinstance(body["events"], list) and "next_before" in body
    assert client.get(f"{PREFIX}/track/nope/history", headers=admin).status_code == 404


def test_availability_admin_and_non_admin(client, admin, alice, seeded):
    for headers in (admin, alice):
        hit = _ok(client.get(
            f"{PREFIX}/availability?artist_name=Radiohead&album_title=OK Computer&track_title=Airbag", headers=headers
        ))
        assert hit == {
            "in_library": True, "monitored": True, "status": "available", "quality": "FLAC 16bit 44.1kHz",
            "file_count": 1, "track_count": 1,
        }
        assert _ok(client.get(f"{PREFIX}/availability?artist_name=Nobody", headers=headers))["status"] == "none"
    partial = _ok(client.get(f"{PREFIX}/availability?artist_name=Radiohead&album_title=OK Computer", headers=alice))
    assert partial["status"] == "partial"


def test_scan_and_migration_jobs(client, admin, seeded):
    status = _ok(client.get(f"{PREFIX}/scan/status", headers=admin))
    assert "is_scanning" in status and "status" in status
    with patch.object(library_scanner, "start_scan", return_value=True):
        assert _ok(client.post(f"{PREFIX}/scan", json={}, headers=admin))["status"]["status"] in {
            "idle", "scanning", "completed", "cancelled", "failed", "skipped",
        }
    assert "status" in _ok(client.post(f"{PREFIX}/scan/cancel", headers=admin))
    assert "is_migrating" in _ok(client.get(f"{PREFIX}/migrate-lidarr/status", headers=admin))
    assert "is_migrating" in _ok(client.post(f"{PREFIX}/migrate-lidarr/cancel", headers=admin))
    assert lidarr_migration_job.get_status()["status"] in {"idle", "cancelled"}


def test_manual_import_scan_album_tracks_and_fingerprint(client, admin, seeded, db):
    audio = seeded["audio"]
    with patch("trackseerr.api.routes.library.manual_import.validate_media_path", side_effect=lambda p, **_: Path(p)):
        items = _ok(client.post(
            f"{PREFIX}/manual-import/scan", json={"file_paths": [str(audio)], "album_id": "alb-1"}, headers=admin
        ))
        assert items[0]["tags"]["codec"] and len(items[0]["candidate_tracks"]) == 1  # trk-2 has no file
        assert items[0]["match_strength"] in {"strong", "weak", "none"}
        with patch(
            "trackseerr.api.routes.library.manual_import.fingerprint_audio_file",
            return_value={"score": 0.98, "recording_id": "rec-1", "title": "Airbag", "artist": "Radiohead"},
        ):
            db.conn.execute("UPDATE library_tracks SET mb_recording_id='rec-1' WHERE id='trk-1'")
            db.conn.commit()
            fp = _ok(client.post(f"{PREFIX}/manual-import/fingerprint", json={"file_path": str(audio)}, headers=admin))
            assert fp["fingerprint"]["recording_id"] == "rec-1" and fp["library_track"]["artist"] == "Radiohead"
        with patch("trackseerr.api.routes.library.manual_import.fingerprint_audio_file", return_value=None):
            miss = _ok(client.post(f"{PREFIX}/manual-import/fingerprint", json={"file_path": str(audio)}, headers=admin))
            assert miss["success"] is False and "fingerprint" not in miss
    tracks = _ok(client.get(f"{PREFIX}/manual-import/album-tracks?album_id=alb-1", headers=admin))
    assert [t["has_file"] for t in tracks] == [True, False]


def test_rename_preview_apply(client, admin, seeded):
    with patch("trackseerr.api.routes.library.tagging.validate_media_path", side_effect=lambda p, **_: Path(p)):
        preview = _ok(client.post(f"{PREFIX}/rename/preview", json={}, headers=admin))
        assert set(preview[0]) == {"file_id", "track_id", "current_path", "proposed_path", "needs_rename"}
        applied = _ok(client.post(f"{PREFIX}/rename/apply", json={"file_ids": ["missing"]}, headers=admin))
        assert applied["renamed_count"] == 0 and applied["errors"]


def test_collections_crud(client, admin, seeded):
    created = _ok(client.post(f"{PREFIX}/collections", json={"name": "Faves", "summary": "s"}, headers=admin))
    cid = created["id"]
    assert created["album_count"] == 0 and created["preview_covers"] == []
    assert _ok(client.post(f"{PREFIX}/collections/{cid}/albums", json={"album_id": "alb-1"}, headers=admin)) == {
        "success": True, "collection_id": cid, "album_id": "alb-1",
    }
    listing = _ok(client.get(f"{PREFIX}/collections", headers=admin))
    assert listing[0]["album_count"] == 1 and listing[0]["preview_covers"] == ["https://example.com/c.jpg"]
    detail = _ok(client.get(f"{PREFIX}/collections/{cid}", headers=admin))
    assert detail["albums"][0]["order_index"] == 0
    assert _ok(client.delete(f"{PREFIX}/collections/{cid}/albums/alb-1", headers=admin))["success"] is True
    assert _ok(client.delete(f"{PREFIX}/collections/{cid}", headers=admin)) == {"success": True, "id": cid}


def test_deletes(client, admin, seeded):
    assert _ok(client.delete(f"{PREFIX}/files/fl-1?delete_file_from_disk=false", headers=admin)) == {"success": True}
    assert _ok(client.delete(f"{PREFIX}/tracks/trk-2", headers=admin)) == {"success": True}
    assert _ok(client.delete(f"{PREFIX}/albums/alb-1", headers=admin)) == {"success": True}
    assert _ok(client.delete(f"{PREFIX}/artists/art-2", headers=admin)) == {"success": True}


def test_non_admin_is_refused_on_admin_routes(client, alice, seeded):
    assert client.get(f"{PREFIX}/stats", headers=alice).status_code == 403
    assert client.get(f"{PREFIX}/artists/art-1", headers=alice).status_code == 403

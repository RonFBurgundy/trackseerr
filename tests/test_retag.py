"""Integration and unit tests for bulk audio file retagging (preview & apply).

Tests cover:
- Preview listing only differing fields;
- Apply writing tags and second preview being empty;
- Seeding safety: hardlink with keep_hardlink is skipped, inode and content unchanged;
- Seeding safety: hardlink with copy_and_tag breaks link, original target byte-identical, nlink becomes 1;
- Client-supplied tag values ignored (server recomputes from catalog);
- Non-admin receives 403 Forbidden;
- Lidarr mode receives 409 Conflict (native_only);
- Importer tag dictionary construction regression test (build_tags_to_write);
- Embedded album artwork;
- Tracked task execution for large file batches (>50 files).
"""

from __future__ import annotations

import os
from pathlib import Path
import struct
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from mutagen.flac import FLAC
from mutagen.mp3 import MP3

from plex_playlist_sync.acquisition_worker import AcquisitionWorker
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db, get_media_client
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.library import (
    build_tags_to_write,
    inspect_audio_file,
    write_audio_tags,
)
from plex_playlist_sync.library_manager import MODE_LIDARR
from plex_playlist_sync.storage import Database


# ---------------------------------------------------------------------------
# Helpers & Audio Fixtures
# ---------------------------------------------------------------------------

def _create_minimal_flac(path: Path) -> None:
    """Writes a valid minimal FLAC file stream header."""
    sr_chan_bps_samples = struct.pack(">BBBBBI", 0x0A, 0xC4, 0x42, 0xF0, 0x00, 44100)
    streaminfo = (
        struct.pack(">HH3s3s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00")
        + sr_chan_bps_samples
        + b"\x00" * 16
    )
    header = b"fLaC\x80\x00\x00\x22" + streaminfo
    path.write_bytes(header)


def _create_minimal_mp3(path: Path) -> None:
    """Writes a valid minimal MPEG-1 Layer 3 audio frame."""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    path.write_bytes(frame * 2)


_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00"
    b"\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def test_db():
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        data_dir=str(tmp_path),
    )


@pytest.fixture
def seeded_users(test_db):
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@plex.tv", is_admin=True)
    alice = test_db.upsert_user("user-alice", "alice", "alice@plex.tv", is_admin=False)
    return {"admin": admin, "alice": alice}


@pytest.fixture
def app_and_client(test_db, test_config):
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    client = TestClient(app)
    return app, client


def _auth_headers(user: dict[str, Any], test_db: Database, config: Config) -> dict[str, str]:
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


def _setup_library_track(
    test_db: Database,
    music_dir: Path,
    *,
    artist_name: str = "Daft Punk",
    album_title: str = "Discovery",
    track_title: str = "One More Time",
    track_number: int = 1,
    year: int = 2001,
    artist_mbid: str = "mbid-artist-1",
    album_mbid: str = "mbid-album-1",
    release_group_mbid: str = "mbid-rg-1",
    track_mbid: str = "mbid-trk-1",
    initial_tags: dict[str, Any] | None = None,
    is_flac: bool = True,
    file_name: str = "track.flac",
) -> tuple[dict[str, Any], Path]:
    """Helper to set up an artist, album, track, and library file on disk."""
    music_dir.mkdir(parents=True, exist_ok=True)
    file_path = music_dir / file_name

    if is_flac:
        _create_minimal_flac(file_path)
    else:
        _create_minimal_mp3(file_path)

    if initial_tags:
        write_audio_tags(file_path, initial_tags)

    test_db.update_media_management_settings({
        "root_folder_path": str(music_dir),
        "write_audio_tags": True,
        "torrent_hardlink_tags": "copy_and_tag",
    })

    art = test_db.upsert_library_artist({
        "id": "art-1",
        "name": artist_name,
        "mbid": artist_mbid,
        "monitored": True,
    })
    alb = test_db.upsert_library_album({
        "id": "alb-1",
        "artist_id": "art-1",
        "title": album_title,
        "year": year,
        "release_date": f"{year}-03-12",
        "total_tracks": 14,
        "mb_release_id": album_mbid,
        "mb_release_group_id": release_group_mbid,
        "monitored": True,
    })
    trk = test_db.upsert_library_track({
        "id": "trk-1",
        "album_id": "alb-1",
        "artist_id": "art-1",
        "title": track_title,
        "track_number": track_number,
        "disc_number": 1,
        "mb_recording_id": track_mbid,
        "monitored": True,
    })
    fl = test_db.upsert_library_file({
        "id": "fl-1",
        "track_id": "trk-1",
        "file_path": str(file_path),
        "relative_path": file_name,
        "codec": "FLAC" if is_flac else "MP3",
    })

    return {"artist": art, "album": alb, "track": trk, "file": fl}, file_path


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


def test_retag_preview_lists_only_differing_fields(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    """Preview lists only fields that differ between current file tags and catalog metadata."""
    app, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    music_dir = tmp_path / "music"

    # Initial file tags: title and artist differ from catalog, but album and date match
    initial_tags = {
        "artist": "Old Artist",
        "album": "Discovery",
        "title": "Wrong Title",
        "date": "2001-03-12",
        "tracknumber": 1,
        "totaltracks": 14,
    }
    catalog, file_path = _setup_library_track(test_db, music_dir, initial_tags=initial_tags)

    resp = client.post(
        "/api/library/retag/preview",
        json={"album_id": "alb-1"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1

    item = items[0]
    assert item["file_id"] == "fl-1"
    assert item["skipped_reason"] is None
    diffs = item["changes"]
    assert len(diffs) > 0

    fields_in_diff = {d["field"]: d for d in diffs}
    # Differing fields must be present
    assert "artist" in fields_in_diff
    assert fields_in_diff["artist"]["current"] == "Old Artist"
    assert fields_in_diff["artist"]["proposed"] == "Daft Punk"

    assert "title" in fields_in_diff
    assert fields_in_diff["title"]["current"] == "Wrong Title"
    assert fields_in_diff["title"]["proposed"] == "One More Time"

    # Matching fields (album, totaltracks, date) must NOT be listed
    assert "album" not in fields_in_diff
    assert "totaltracks" not in fields_in_diff


def test_retag_apply_writes_tags_and_second_preview_is_empty(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    """Apply writes proposed tags to disk, and a subsequent preview finds no differences."""
    app, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    music_dir = tmp_path / "music"

    initial_tags = {
        "artist": "Old Artist",
        "album": "Old Album",
        "title": "Old Title",
        "date": "1999",
        "tracknumber": 99,
    }
    catalog, file_path = _setup_library_track(test_db, music_dir, initial_tags=initial_tags)

    # 1. Preview shows differing fields
    p1 = client.post(
        "/api/library/retag/preview",
        json={"album_id": "alb-1"},
        headers=admin_headers,
    )
    assert p1.status_code == 200
    assert len(p1.json()) == 1

    # 2. Apply retag
    apply_resp = client.post(
        "/api/library/retag/apply",
        json={"file_ids": ["fl-1"]},
        headers=admin_headers,
    )
    assert apply_resp.status_code == 200
    res = apply_resp.json()
    assert res["retagged_count"] == 1
    assert res["applied_count"] == 1
    assert res["skipped_count"] == 0
    assert res["error_count"] == 0
    assert res["results"] == [{"file_id": "fl-1", "status": "ok", "message": None, "reason": None}]

    # 3. Verify file on disk has updated tags
    meta = inspect_audio_file(file_path)
    assert meta["artist"] == "Daft Punk"
    assert meta["album"] == "Discovery"
    assert meta["title"] == "One More Time"
    assert meta["track_number"] == 1
    assert meta["year"] == 2001
    assert meta["musicbrainz_artistid"] == "mbid-artist-1"

    # 4. Item history event recorded
    history = test_db.list_item_events("album", "alb-1")
    assert any(ev.get("event") == "retagged" for ev in history)

    # 5. Second preview must be empty because all tags now match
    p2 = client.post(
        "/api/library/retag/preview",
        json={"album_id": "alb-1"},
        headers=admin_headers,
    )
    assert p2.status_code == 200
    assert p2.json() == []


def test_retag_hardlink_keep_hardlink_skipped_and_inode_unchanged(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    """Hardlinked file with keep_hardlink is skipped, preserving torrent inode and byte content."""
    app, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    staging_dir = tmp_path / "torrents"
    staging_dir.mkdir(parents=True)
    source_file = staging_dir / "seeding_source.flac"
    _create_minimal_flac(source_file)
    write_audio_tags(source_file, {"artist": "Seeding Artist", "title": "Seeding Track"})

    music_dir = tmp_path / "music"
    music_dir.mkdir(parents=True)
    lib_file = music_dir / "lib_track.flac"
    os.link(source_file, lib_file)

    src_ino = source_file.stat().st_ino
    src_bytes = source_file.read_bytes()
    assert lib_file.stat().st_nlink == 2

    # Configure keep_hardlink
    test_db.update_media_management_settings({
        "root_folder_path": str(music_dir),
        "write_audio_tags": True,
        "torrent_hardlink_tags": "keep_hardlink",
    })

    test_db.upsert_library_artist({"id": "art-1", "name": "Catalog Artist"})
    test_db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": "Catalog Album", "year": 2020})
    test_db.upsert_library_track({"id": "trk-1", "album_id": "alb-1", "artist_id": "art-1", "title": "Catalog Track", "track_number": 1})
    test_db.upsert_library_file({"id": "fl-hl-1", "track_id": "trk-1", "file_path": str(lib_file), "codec": "FLAC"})

    # Preview indicates file will be skipped
    p = client.post("/api/library/retag/preview", json={"album_id": "alb-1"}, headers=admin_headers)
    assert p.status_code == 200
    items = p.json()
    assert len(items) == 1
    assert items[0]["skipped_reason"] == "hardlink keep"

    # Apply retag
    res = client.post("/api/library/retag/apply", json={"file_ids": ["fl-hl-1"]}, headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["retagged_count"] == 0
    assert data["skipped_count"] == 1
    assert data["results"][0]["status"] == "skipped"
    assert data["results"][0]["reason"] == "hardlink keep"

    # Hardlink and content remain untouched
    assert lib_file.stat().st_nlink == 2
    assert source_file.stat().st_ino == src_ino
    assert lib_file.stat().st_ino == src_ino
    assert source_file.read_bytes() == src_bytes
    assert lib_file.read_bytes() == src_bytes


def test_retag_hardlink_copy_and_tag_breaks_link(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    """Hardlinked file with copy_and_tag breaks link: nlink becomes 1, source file byte-identical."""
    app, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    staging_dir = tmp_path / "torrents"
    staging_dir.mkdir(parents=True)
    source_file = staging_dir / "seeding_source.flac"
    _create_minimal_flac(source_file)
    write_audio_tags(source_file, {"artist": "Seeding Artist", "title": "Seeding Track"})

    music_dir = tmp_path / "music"
    music_dir.mkdir(parents=True)
    lib_file = music_dir / "lib_track.flac"
    os.link(source_file, lib_file)

    src_ino = source_file.stat().st_ino
    src_bytes = source_file.read_bytes()
    assert lib_file.stat().st_nlink == 2

    test_db.update_media_management_settings({
        "root_folder_path": str(music_dir),
        "write_audio_tags": True,
        "torrent_hardlink_tags": "copy_and_tag",
    })

    test_db.upsert_library_artist({"id": "art-1", "name": "Catalog Artist"})
    test_db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": "Catalog Album", "year": 2020})
    test_db.upsert_library_track({"id": "trk-1", "album_id": "alb-1", "artist_id": "art-1", "title": "Catalog Track", "track_number": 1})
    test_db.upsert_library_file({"id": "fl-hl-2", "track_id": "trk-1", "file_path": str(lib_file), "codec": "FLAC"})

    # Preview does not flag skipped_reason since copy_and_tag allows writing
    p = client.post("/api/library/retag/preview", json={"album_id": "alb-1"}, headers=admin_headers)
    assert p.status_code == 200
    assert p.json()[0]["skipped_reason"] is None

    # Apply retag
    res = client.post("/api/library/retag/apply", json={"file_ids": ["fl-hl-2"]}, headers=admin_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["retagged_count"] == 1
    assert data["results"][0]["status"] == "ok"

    # Source file remains untouched and byte-identical
    assert source_file.stat().st_ino == src_ino
    assert source_file.stat().st_nlink == 1
    assert source_file.read_bytes() == src_bytes

    # Library file is now an independent private copy with updated tags
    assert lib_file.stat().st_nlink == 1
    assert lib_file.stat().st_ino != src_ino
    meta = inspect_audio_file(lib_file)
    assert meta["artist"] == "Catalog Artist"
    assert meta["title"] == "Catalog Track"


def test_retag_client_supplied_values_are_ignored(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    """Server recomputes proposals server-side; client cannot inject tag values in apply."""
    app, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    music_dir = tmp_path / "music"

    initial_tags = {"artist": "Old Artist", "title": "Old Title"}
    catalog, file_path = _setup_library_track(test_db, music_dir, initial_tags=initial_tags)

    # Client attempts to pass spoofed tag values in request
    res = client.post(
        "/api/library/retag/apply",
        json={
            "file_ids": ["fl-1"],
            "artist": "Hacker Artist",
            "title": "Hacked Title",
        },
        headers=admin_headers,
    )
    assert res.status_code == 200
    assert res.json()["retagged_count"] == 1

    # Tags on disk reflect the server catalog ("Daft Punk", "One More Time"), NOT client inputs
    meta = inspect_audio_file(file_path)
    assert meta["artist"] == "Daft Punk"
    assert meta["title"] == "One More Time"
    assert meta["artist"] != "Hacker Artist"


def test_retag_non_admin_forbidden_403(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    """Non-admin user receives HTTP 403 on preview and apply endpoints."""
    app, client = app_and_client
    alice_headers = _auth_headers(seeded_users["alice"], test_db, test_config)

    resp_prev = client.post(
        "/api/library/retag/preview",
        json={},
        headers=alice_headers,
    )
    assert resp_prev.status_code == 403

    resp_apply = client.post(
        "/api/library/retag/apply",
        json={"file_ids": ["fl-1"]},
        headers=alice_headers,
    )
    assert resp_apply.status_code == 403


def test_retag_lidarr_mode_conflict_409(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    """When Lidarr manages the library, retag preview and apply return 409 (native_only)."""
    app, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    test_db.update_media_management_settings({"library_mode": MODE_LIDARR})

    resp_prev = client.post(
        "/api/library/retag/preview",
        json={},
        headers=admin_headers,
    )
    assert resp_prev.status_code == 409

    resp_apply = client.post(
        "/api/library/retag/apply",
        json={"file_ids": ["fl-1"]},
        headers=admin_headers,
    )
    assert resp_apply.status_code == 409


def test_importer_writes_same_tags_regression(tmp_path: Path):
    """build_tags_to_write produces the exact canonical tag dictionary expected by acquisition worker."""
    metadata = {
        "artist": "Meta Artist",
        "album": "Meta Album",
        "title": "Meta Title",
        "year": 2022,
        "track_number": 4,
        "total_tracks": 10,
        "disc_number": 1,
        "total_discs": 1,
        "musicbrainz_artistid": "mb-art",
        "musicbrainz_albumid": "mb-alb",
        "musicbrainz_releasegroupid": "mb-rg",
        "musicbrainz_trackid": "mb-trk",
        "isrc": "US12345",
    }
    req = {
        "artist": "Req Artist",
        "album": "Req Album",
        "title": "Req Title",
        "release_date": "2022-04-01",
    }

    # Case 1: Multi-file album import (audio_files_count > 1) preserves track metadata title
    tags_multi = build_tags_to_write(metadata, req=req, audio_files_count=2)
    assert tags_multi["artist"] == "Req Artist"
    assert tags_multi["album"] == "Req Album"
    assert tags_multi["title"] == "Meta Title"
    assert tags_multi["date"] == "2022-04-01"
    assert tags_multi["tracknumber"] == 4
    assert tags_multi["totaltracks"] == 10
    assert tags_multi["musicbrainz_artistid"] == "mb-art"
    assert tags_multi["isrc"] == "US12345"

    # Case 2: Single-file release prioritizes request title
    tags_single = build_tags_to_write(metadata, req=req, audio_files_count=1)
    assert tags_single["title"] == "Req Title"

    # Case 3: Retag direct keyword arguments override metadata/req
    tags_retag = build_tags_to_write(
        artist="Direct Artist",
        album="Direct Album",
        title="Direct Track",
        date="2023",
        track_number=5,
        total_tracks=12,
        disc_number=2,
        total_discs=2,
        musicbrainz_artistid="mb-art-retag",
    )
    assert tags_retag["artist"] == "Direct Artist"
    assert tags_retag["album"] == "Direct Album"
    assert tags_retag["title"] == "Direct Track"
    assert tags_retag["date"] == "2023"
    assert tags_retag["tracknumber"] == 5
    assert tags_retag["totaltracks"] == 12
    assert tags_retag["discnumber"] == 2
    assert tags_retag["totaldiscs"] == 2
    assert tags_retag["musicbrainz_artistid"] == "mb-art-retag"


def test_retag_embed_art(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    """Retag apply with embed_art embeds cover art bytes into the target audio file."""
    app, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    music_dir = tmp_path / "music"

    catalog, file_path = _setup_library_track(
        test_db, music_dir, initial_tags={"artist": "Old", "title": "Old"}
    )

    # Mock _album_cover_bytes to return test cover art
    with patch("plex_playlist_sync.api.routes.library._album_cover_bytes", return_value=_TINY_PNG):
        resp = client.post(
            "/api/library/retag/apply",
            json={"file_ids": ["fl-1"], "embed_art": True},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["retagged_count"] == 1

    # Verify FLAC picture is present
    audio = FLAC(str(file_path))
    assert len(audio.pictures) > 0
    assert audio.pictures[0].data == _TINY_PNG


def test_retag_tracked_job_over_50_files(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    """Apply of >50 files runs inside track_job and record_task_run with progress updates."""
    app, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    music_dir = tmp_path / "music"
    music_dir.mkdir(parents=True)

    test_db.update_media_management_settings({"root_folder_path": str(music_dir)})
    test_db.upsert_library_artist({"id": "art-bulk", "name": "Bulk Artist"})
    test_db.upsert_library_album({"id": "alb-bulk", "artist_id": "art-bulk", "title": "Bulk Album", "year": 2024})

    file_ids: list[str] = []
    for i in range(55):
        fid = f"fl-bulk-{i}"
        tid = f"trk-bulk-{i}"
        p = music_dir / f"bulk_{i}.flac"
        _create_minimal_flac(p)
        write_audio_tags(p, {"artist": "Old Artist", "title": f"Old {i}"})
        test_db.upsert_library_track({
            "id": tid,
            "album_id": "alb-bulk",
            "artist_id": "art-bulk",
            "title": f"New {i}",
            "track_number": i + 1,
        })
        test_db.upsert_library_file({
            "id": fid,
            "track_id": tid,
            "file_path": str(p),
            "codec": "FLAC",
        })
        file_ids.append(fid)

    with patch("plex_playlist_sync.api.routes.library.track_job") as mock_track, \
         patch("plex_playlist_sync.api.routes.library.record_task_run") as mock_run:
        mock_job_inst = MagicMock()
        mock_run_inst = MagicMock()
        mock_track.return_value.__enter__.return_value = mock_job_inst
        mock_run.return_value.__enter__.return_value = mock_run_inst

        resp = client.post(
            "/api/library/retag/apply",
            json={"file_ids": file_ids},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["retagged_count"] == 55

        assert mock_track.called
        assert mock_run.called
        # Verify job progress message was called
        assert mock_job_inst.update_message.called


def test_retag_apply_max_limit_validation(
    app_and_client, test_db: Database, test_config: Config, seeded_users
):
    """Retag apply rejects payloads with more than 500 files with 400 or 422."""
    app, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    too_many = [f"fl-{i}" for i in range(501)]
    resp = client.post(
        "/api/library/retag/apply",
        json={"file_ids": too_many},
        headers=admin_headers,
    )
    assert resp.status_code in (400, 422)


def test_retag_preview_unsupported_format_shows_skipped_reason(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    """Files with unsupported audio extensions are flagged with skipped_reason in preview."""
    app, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)
    music_dir = tmp_path / "music"
    music_dir.mkdir(parents=True)

    txt_file = music_dir / "notes.txt"
    txt_file.write_text("not audio")

    test_db.update_media_management_settings({"root_folder_path": str(music_dir)})
    test_db.upsert_library_artist({"id": "art-1", "name": "Artist"})
    test_db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": "Album", "year": 2020})
    test_db.upsert_library_track({"id": "trk-txt", "album_id": "alb-1", "artist_id": "art-1", "title": "Notes"})
    test_db.upsert_library_file({"id": "fl-txt", "track_id": "trk-txt", "file_path": str(txt_file)})

    resp = client.post(
        "/api/library/retag/preview",
        json={"album_id": "alb-1"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert "unsupported format" in items[0]["skipped_reason"]


def test_preview_skips_path_outside_media_roots(
    app_and_client, test_db: Database, test_config: Config, seeded_users, tmp_path: Path
):
    """A library file whose file_path lies outside the configured root is skipped and never opened."""
    app, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    music_dir = tmp_path / "music"
    music_dir.mkdir(parents=True)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(parents=True)

    outside_file = outside_dir / "outside_track.flac"
    _create_minimal_flac(outside_file)

    test_db.update_media_management_settings({"root_folder_path": str(music_dir)})
    test_db.upsert_library_artist({"id": "art-outside", "name": "Outside Artist"})
    test_db.upsert_library_album({"id": "alb-outside", "artist_id": "art-outside", "title": "Outside Album", "year": 2021})
    test_db.upsert_library_track({"id": "trk-outside", "album_id": "alb-outside", "artist_id": "art-outside", "title": "Outside Track"})
    test_db.upsert_library_file({"id": "fl-outside", "track_id": "trk-outside", "file_path": str(outside_file)})

    with patch("plex_playlist_sync.api.routes.library.inspect_audio_file") as mock_inspect:
        resp = client.post(
            "/api/library/retag/preview",
            json={"album_id": "alb-outside"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        item = items[0]
        assert item["file_id"] == "fl-outside"
        assert item["skipped_reason"] == "Path is outside the configured media roots"
        mock_inspect.assert_not_called()


def test_cover_lookup_oserror_falls_through(tmp_path: Path):
    """Patching art_pipeline.cached_art_path to raise OSError still returns folder-art bytes."""
    from plex_playlist_sync.api.routes.library import _album_cover_bytes

    folder = tmp_path / "album_folder"
    folder.mkdir(parents=True)
    audio_file = folder / "track.flac"
    audio_file.touch()

    folder_art_file = folder / "cover.jpg"
    folder_art_bytes = b"fake-cover-bytes"
    folder_art_file.write_bytes(folder_art_bytes)

    album_dict = {"id": "alb-test", "path": str(folder)}

    with patch("plex_playlist_sync.api.routes.library.art_pipeline.cached_art_path", side_effect=OSError("Disk read failed")):
        result = _album_cover_bytes(album_dict, file_path=audio_file)
        assert result == folder_art_bytes


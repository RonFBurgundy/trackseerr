"""Tests for owned elsewhere badge API metadata on file-less library tracks."""

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.storage import Database


@pytest.fixture
def test_db(tmp_path: Path):
    """Provides an isolated disk-backed Database for testing."""
    db_file = tmp_path / "test_owned_elsewhere_api.db"
    db = Database(str(db_file))
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path: Path):
    """Provides a test Config pointing data_dir to tmp_path."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-token",
        data_dir=str(tmp_path),
    )


@pytest.fixture
def seeded_users(test_db: Database):
    """Seeds admin user in the test database."""
    admin = test_db.upsert_user("admin-1", "admin_user", "admin@example.com", is_admin=True)
    return {"admin": admin}


def _auth_headers(user: dict[str, Any], test_db: Database, config: Config) -> dict[str, str]:
    """Generates an authenticated Bearer Authorization header."""
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"],
        username=user["username"],
        is_admin=user["is_admin"],
        secret_key=secret,
    )
    test_db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def app_and_client(test_db: Database, test_config: Config):
    """Creates a FastAPI test client with injected test database and config dependencies."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config

    client = TestClient(app)
    return app, client


def test_fileless_track_reports_owned_elsewhere(
    app_and_client, test_db: Database, test_config: Config, seeded_users
) -> None:
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    test_db.upsert_library_artist({"id": "art-1", "name": "Stephen King", "monitored": True})
    test_db.upsert_library_album({"id": "alb-a", "artist_id": "art-1", "title": "Album A", "monitored": True})
    test_db.upsert_library_album({"id": "alb-b", "artist_id": "art-1", "title": "Album B", "monitored": True})

    # Album A has file-less "It - 2020 Remaster"
    test_db.upsert_library_track({
        "id": "trk-a1",
        "album_id": "alb-a",
        "artist_id": "art-1",
        "title": "It - 2020 Remaster",
        "duration_seconds": 200.0,
        "monitored": True,
    })

    # Album B has "It" with a file
    test_db.upsert_library_track({
        "id": "trk-b1",
        "album_id": "alb-b",
        "artist_id": "art-1",
        "title": "It",
        "duration_seconds": 201.0,
        "monitored": True,
    })
    test_db.upsert_library_file({
        "id": "fl-b1",
        "track_id": "trk-b1",
        "file_path": "/music/Stephen King/Album B/01 - It.flac",
        "cutoff_met": True,
    })

    resp = client.get("/api/library/tracks?album_id=alb-a", headers=admin_headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    track = rows[0]
    assert track["id"] == "trk-a1"
    assert track.get("file") is None
    assert track.get("owned_elsewhere") is not None
    assert track["owned_elsewhere"]["track_id"] == "trk-b1"
    assert track["owned_elsewhere"]["album_id"] == "alb-b"
    assert track["owned_elsewhere"]["album_title"] == "Album B"


def test_owned_track_has_no_owned_elsewhere(
    app_and_client, test_db: Database, test_config: Config, seeded_users
) -> None:
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    test_db.upsert_library_artist({"id": "art-1", "name": "Stephen King", "monitored": True})
    test_db.upsert_library_album({"id": "alb-b", "artist_id": "art-1", "title": "Album B", "monitored": True})
    test_db.upsert_library_track({
        "id": "trk-b1",
        "album_id": "alb-b",
        "artist_id": "art-1",
        "title": "It",
        "duration_seconds": 201.0,
        "monitored": True,
    })
    test_db.upsert_library_file({
        "id": "fl-b1",
        "track_id": "trk-b1",
        "file_path": "/music/Stephen King/Album B/01 - It.flac",
        "cutoff_met": True,
    })

    resp = client.get("/api/library/tracks?album_id=alb-b", headers=admin_headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    track = rows[0]
    assert track["id"] == "trk-b1"
    assert track.get("file") is not None
    assert track.get("owned_elsewhere") is None


def test_unrelated_title_has_none(
    app_and_client, test_db: Database, test_config: Config, seeded_users
) -> None:
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    test_db.upsert_library_artist({"id": "art-1", "name": "Stephen King", "monitored": True})
    test_db.upsert_library_album({"id": "alb-a", "artist_id": "art-1", "title": "Album A", "monitored": True})
    test_db.upsert_library_album({"id": "alb-b", "artist_id": "art-1", "title": "Album B", "monitored": True})

    # Track A is file-less but with an unrelated title
    test_db.upsert_library_track({
        "id": "trk-a2",
        "album_id": "alb-a",
        "artist_id": "art-1",
        "title": "Something Completely Different",
        "duration_seconds": 180.0,
        "monitored": True,
    })

    # Album B has "It" with a file
    test_db.upsert_library_track({
        "id": "trk-b1",
        "album_id": "alb-b",
        "artist_id": "art-1",
        "title": "It",
        "duration_seconds": 201.0,
        "monitored": True,
    })
    test_db.upsert_library_file({
        "id": "fl-b1",
        "track_id": "trk-b1",
        "file_path": "/music/Stephen King/Album B/01 - It.flac",
        "cutoff_met": True,
    })

    resp = client.get("/api/library/tracks?album_id=alb-a", headers=admin_headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    track = rows[0]
    assert track["id"] == "trk-a2"
    assert track.get("file") is None
    assert track.get("owned_elsewhere") is None


def test_badge_shown_even_when_multiple_editions_allowed(
    app_and_client, test_db: Database, test_config: Config, seeded_users
) -> None:
    _, client = app_and_client
    admin_headers = _auth_headers(seeded_users["admin"], test_db, test_config)

    # Enable allow_multiple_editions in media management settings
    test_db.update_media_management_settings({"allow_multiple_editions": True})

    test_db.upsert_library_artist({"id": "art-1", "name": "Stephen King", "monitored": True})
    test_db.upsert_library_album({"id": "alb-a", "artist_id": "art-1", "title": "Album A", "monitored": True})
    test_db.upsert_library_album({"id": "alb-b", "artist_id": "art-1", "title": "Album B", "monitored": True})

    test_db.upsert_library_track({
        "id": "trk-a1",
        "album_id": "alb-a",
        "artist_id": "art-1",
        "title": "It - 2020 Remaster",
        "duration_seconds": 200.0,
        "monitored": True,
    })
    test_db.upsert_library_track({
        "id": "trk-b1",
        "album_id": "alb-b",
        "artist_id": "art-1",
        "title": "It",
        "duration_seconds": 201.0,
        "monitored": True,
    })
    test_db.upsert_library_file({
        "id": "fl-b1",
        "track_id": "trk-b1",
        "file_path": "/music/Stephen King/Album B/01 - It.flac",
        "cutoff_met": True,
    })

    resp = client.get("/api/library/tracks?album_id=alb-a", headers=admin_headers)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    track = rows[0]
    assert track["id"] == "trk-a1"
    assert track.get("file") is None
    assert track.get("owned_elsewhere") is not None
    assert track["owned_elsewhere"]["track_id"] == "trk-b1"
    assert track["owned_elsewhere"]["album_id"] == "alb-b"
    assert track["owned_elsewhere"]["album_title"] == "Album B"

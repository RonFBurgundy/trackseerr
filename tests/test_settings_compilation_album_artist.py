"""Media management setting: compilation_album_artist API and store round-trip."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, require_admin
from trackseerr.config import Config
from trackseerr.storage import Database


@pytest.fixture
def test_db(tmp_path: Path):
    db = Database(str(tmp_path / "compilation_album_artist.db"))
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path: Path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


def test_compilation_album_artist_defaults_various_and_round_trips(
    test_db: Database, test_config: Config
) -> None:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config
    app.dependency_overrides[require_admin] = lambda: {
        "id": "admin-1",
        "username": "admin_user",
        "is_admin": True,
    }
    client = TestClient(app)

    resp = client.get("/api/settings/media-management")
    assert resp.status_code == 200
    assert resp.json()["settings"]["compilation_album_artist"] == "various"

    put_resp = client.put(
        "/api/settings/media-management",
        json={"compilation_album_artist": "series_label"},
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["compilation_album_artist"] == "series_label"
    assert test_db.get_media_management_settings()["compilation_album_artist"] == "series_label"

    get_series = client.get("/api/settings/media-management")
    assert get_series.status_code == 200
    assert get_series.json()["settings"]["compilation_album_artist"] == "series_label"

    invalid = client.put(
        "/api/settings/media-management",
        json={"compilation_album_artist": "invalid_mode"},
    )
    assert invalid.status_code == 422

    put_various = client.put(
        "/api/settings/media-management",
        json={"compilation_album_artist": "various"},
    )
    assert put_various.status_code == 200
    assert put_various.json()["compilation_album_artist"] == "various"
    assert test_db.get_media_management_settings()["compilation_album_artist"] == "various"

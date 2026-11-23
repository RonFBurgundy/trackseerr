"""Media management setting: keep_compilations_together API and store round-trip."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db, require_admin
from trackseerr.config import Config
from trackseerr.storage import Database


@pytest.fixture
def test_db(tmp_path: Path):
    db = Database(str(tmp_path / "keep_compilations.db"))
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path: Path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))


def test_keep_compilations_together_defaults_false_and_round_trips(
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
    assert resp.json()["settings"]["keep_compilations_together"] is False

    put_resp = client.put(
        "/api/settings/media-management",
        json={"keep_compilations_together": True},
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["keep_compilations_together"] is True
    assert test_db.get_media_management_settings()["keep_compilations_together"] is True

    get_true = client.get("/api/settings/media-management")
    assert get_true.status_code == 200
    assert get_true.json()["settings"]["keep_compilations_together"] is True

    put_false = client.put(
        "/api/settings/media-management",
        json={"keep_compilations_together": False},
    )
    assert put_false.status_code == 200
    assert put_false.json()["keep_compilations_together"] is False
    assert test_db.get_media_management_settings()["keep_compilations_together"] is False

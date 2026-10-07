"""Shared fixtures for the ``tests/test_*_response_models.py`` strict-mode backstops (wave 2b).

``ApiModel`` forbids undeclared keys under test, so every request through these fixtures fails when a route returns a
key its response model does not declare.
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.storage import Database


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


def auth_headers(db: Database, config: Config, user: dict[str, Any]) -> dict[str, str]:
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin(db: Database, config: Config) -> dict[str, str]:
    return auth_headers(db, config, db.upsert_user("admin-1", "admin_user", "a@example.com", is_admin=True))


@pytest.fixture
def alice(db: Database, config: Config) -> dict[str, str]:
    return auth_headers(db, config, db.upsert_user("alice-1", "alice", "al@example.com", is_admin=False))


def ok(resp, status: int = 200):
    assert resp.status_code == status, resp.text
    return resp.json()

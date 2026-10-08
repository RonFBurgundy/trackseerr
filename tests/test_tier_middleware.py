"""Tests for tier middleware gateway default-denial of Lidarr-compatible API routes."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.api import tier_middleware as tm
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.config import Config
from plex_playlist_sync.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    d = Database(str(tmp_path / "tier.db"))
    yield d
    d.close()


@pytest.fixture
def gateway_config(tmp_path: Path):
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="token",
        data_dir=str(tmp_path),
        role="gateway",
        internal_core_secret="s" * 40,
    )


@pytest.fixture
def gateway_client(db: Database, gateway_config: Config):
    app = create_app(db=db, config=gateway_config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: gateway_config
    return TestClient(app)


def test_gateway_allowlists_exclude_lidarr_compat():
    """Asserts that no Lidarr compatibility routes (/api/v1/*) are present on gateway allowlists."""
    all_allowlist_paths = [
        path
        for allowlist in (
            tm.GATEWAY_LOCAL_ALLOWLIST,
            tm.GATEWAY_FORWARD_SERVICE_ALLOWLIST,
            tm.GATEWAY_FORWARD_ALLOWLIST,
        )
        for _, path in allowlist
    ]

    for path in all_allowlist_paths:
        assert not path.startswith("/api/v1"), f"Gateway allowlist must not contain /api/v1 path: {path}"
        assert "indexer" not in path, f"Gateway allowlist must not contain indexer path: {path}"


def test_gateway_tier_denies_lidarr_indexer(gateway_client: TestClient):
    """The gateway tier default-denies /api/v1/indexer (stays core-only)."""
    resp = gateway_client.get("/api/v1/indexer")
    assert resp.status_code in (403, 404)


def test_gateway_tier_denies_lidarr_compat_routes(gateway_client: TestClient):
    """Gateway tier denies all /api/v1 routes."""
    for method, path in [
        ("GET", "/api/v1/system/status"),
        ("GET", "/api/v1/indexer/schema"),
        ("GET", "/api/v1/indexer"),
        ("GET", "/api/v1/indexer/1"),
        ("POST", "/api/v1/indexer"),
        ("POST", "/api/v1/indexer/test"),
        ("PUT", "/api/v1/indexer/1"),
        ("DELETE", "/api/v1/indexer/1"),
    ]:
        resp = gateway_client.request(method, path)
        assert resp.status_code in (403, 404), f"Gateway failed to deny {method} {path}: {resp.status_code}"

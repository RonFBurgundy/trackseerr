"""Tests for HTTP response compression and immutable asset caching headers."""

from contextlib import contextmanager
import gzip
import json
import os
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trackseerr.api import app as app_module
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.config import Config
from trackseerr.storage import Database


@pytest.fixture
def test_db(tmp_path: Path) -> Generator[Database, None, None]:
    db = Database(str(tmp_path / "test.db"))
    yield db
    db.close()


@pytest.fixture
def app_config(tmp_path: Path) -> Config:
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="token",
        data_dir=str(tmp_path),
        role="all-in-one",
    )


@contextmanager
def _patch_app_dist(dist_path: Path) -> Generator[None, None, None]:
    """Patches path resolution in trackseerr.api.app to use a specific dist directory."""

    class FrontendProxy:
        def __init__(self, target_dist: Path) -> None:
            self._target_dist = target_dist

        def __truediv__(self, other: str) -> Path:
            if other == "dist":
                return self._target_dist
            return self._target_dist.parent / other

        def __str__(self) -> str:
            return str(self._target_dist.parent)

        def __fspath__(self) -> str:
            return os.fspath(self._target_dist.parent)

    real_path = app_module.Path

    class PathProxy:
        def __init__(self, target: Path) -> None:
            self._target = target

        def resolve(self) -> "PathProxy":
            return PathProxy(self._target.resolve())

        @property
        def parent(self) -> "PathProxy":
            return PathProxy(self._target.parent)

        def __truediv__(self, other: str):
            if other == "frontend":
                return FrontendProxy(dist_path)
            return self._target / other

        def __str__(self) -> str:
            return str(self._target)

        def __fspath__(self) -> str:
            return os.fspath(self._target)

        def __getattr__(self, name: str) -> object:
            return getattr(self._target, name)

    with patch.object(
        app_module,
        "Path",
        side_effect=lambda *args, **kwargs: PathProxy(real_path(*args, **kwargs)),
    ):
        yield


def test_json_response_gzipped_when_requested(
    test_db: Database, app_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET a JSON endpoint returning >1 KB with Accept-Encoding: gzip -> content-encoding: gzip, body decodes to valid JSON."""
    monkeypatch.setenv("ENABLE_API_DOCS", "1")
    app = create_app(db=test_db, config=app_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: app_config

    with TestClient(app) as client:
        # Using stream=True allows reading the raw compressed wire bytes from iter_raw()
        req = client.build_request("GET", "/api/openapi.json", headers={"Accept-Encoding": "gzip"})
        resp = client.send(req, stream=True)
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding") == "gzip"
        raw_wire_bytes = b"".join(resp.iter_raw())
        # Body decodes to valid JSON
        decompressed = gzip.decompress(raw_wire_bytes)
        data = json.loads(decompressed.decode("utf-8"))
        assert isinstance(data, dict)
        assert data.get("info", {}).get("title") == "TrackSeerr API"


def test_small_response_not_gzipped(test_db: Database, app_config: Config) -> None:
    """A tiny endpoint (health/status-like, <1 KB) -> no content-encoding."""
    app = create_app(db=test_db, config=app_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: app_config

    with TestClient(app) as client:
        resp = client.get("/api/health/ready", headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200
        assert "content-encoding" not in resp.headers
        assert len(resp.content) < 1024


def test_hashed_asset_cache_headers(
    test_db: Database, app_config: Config, tmp_path: Path
) -> None:
    """GET /assets/app-abc123.js -> 200, cache-control == public, max-age=31536000, immutable."""
    dist_dir = tmp_path / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")
    asset_file = assets_dir / "app-abc123.js"
    # Write >2 KB content
    asset_file.write_text("console.log('asset chunk data');\n" * 100, encoding="utf-8")

    with _patch_app_dist(dist_dir):
        app = create_app(db=test_db, config=app_config)
        app.dependency_overrides[get_db] = lambda: test_db
        app.dependency_overrides[get_config] = lambda: app_config

        with TestClient(app) as client:
            resp = client.get("/assets/app-abc123.js")
            assert resp.status_code == 200
            assert resp.headers.get("cache-control") == "public, max-age=31536000, immutable"


def test_security_headers_survive_gzip(
    test_db: Database, app_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gzipped response still carries x-content-type-options: nosniff."""
    monkeypatch.setenv("ENABLE_API_DOCS", "1")
    app = create_app(db=test_db, config=app_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: app_config

    with TestClient(app) as client:
        resp = client.get("/api/openapi.json", headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding") == "gzip"
        assert resp.headers.get("x-content-type-options") == "nosniff"

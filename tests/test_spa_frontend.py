"""Tests for Modern React + Vite Single Page Application Integration."""

from collections.abc import Generator
from contextlib import contextmanager
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import trackseerr.api.app as app_module
from trackseerr.api.app import create_app
from trackseerr.config import Config
from trackseerr.storage import Database


@pytest.fixture
def memory_db() -> Generator[Database, None, None]:
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def mock_config(tmp_path: Path) -> Config:
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-token",
        data_dir=str(tmp_path),
    )


def _create_mock_dist(base_path: Path) -> Path:
    """Creates a mock Vite dist directory structure with index.html and assets."""
    mock_dist = base_path / "mock_frontend_dist"
    mock_assets = mock_dist / "assets"
    mock_assets.mkdir(parents=True, exist_ok=True)
    (mock_assets / "index-mock.js").write_text("console.log('mock asset');")
    (mock_dist / "index.html").write_text(
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="UTF-8" />\n'
        "  <title>TrackSeerr</title>\n"
        '  <meta name="theme-color" content="#0a0a0a" />\n'
        '  <link rel="manifest" href="/manifest.json" />\n'
        '  <script type="module" crossorigin src="/assets/index-mock.js"></script>\n'
        "</head>\n"
        "<body>\n"
        '  <div id="root"></div>\n'
        "</body>\n"
        "</html>\n"
    )
    return mock_dist


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

        def __truediv__(self, other: str) -> "Path | FrontendProxy":
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


def _assert_spa_index_html(html: str) -> None:
    """Verifies that the HTML content contains the required SPA elements."""
    # React root mount element
    assert '<div id="root"></div>' in html
    # Vite script tag
    assert 'src="/assets/' in html or 'type="module"' in html
    # Brand and meta tags
    assert "TrackSeerr" in html
    assert 'name="theme-color" content="#0a0a0a"' in html
    assert 'rel="manifest"' in html


class TestSPAFastAPIIntegration:
    """Verifies that FastAPI correctly serves the Vite-built React SPA."""

    def test_app_mounts_assets_when_dist_exists(
        self, memory_db: Database, mock_config: Config, tmp_path: Path
    ) -> None:
        dist_assets = Path("frontend/dist/assets")
        if dist_assets.is_dir():
            app = create_app(db=memory_db, config=mock_config)
            mounted_routes = [getattr(route, "path", None) for route in app.routes]
            assert "/assets" in mounted_routes
        else:
            mock_dist = _create_mock_dist(tmp_path)
            with _patch_app_dist(mock_dist):
                app = create_app(db=memory_db, config=mock_config)
                mounted_routes = [getattr(route, "path", None) for route in app.routes]
                assert "/assets" in mounted_routes

    def test_spa_index_served_when_legacy_env_is_disabled(
        self, memory_db: Database, mock_config: Config, tmp_path: Path
    ) -> None:
        dist_index = Path("frontend/dist/index.html")
        if dist_index.is_file():
            app = create_app(db=memory_db, config=mock_config)
            with TestClient(app) as client:
                resp = client.get("/")
                assert resp.status_code == 200
                assert "text/html" in resp.headers.get("content-type", "")
                _assert_spa_index_html(resp.text)
        else:
            mock_dist = _create_mock_dist(tmp_path)
            with _patch_app_dist(mock_dist):
                app = create_app(db=memory_db, config=mock_config)
                with TestClient(app) as client:
                    resp = client.get("/")
                    assert resp.status_code == 200
                    assert "text/html" in resp.headers.get("content-type", "")
                    _assert_spa_index_html(resp.text)

    def test_placeholder_when_dist_index_missing(
        self, memory_db: Database, mock_config: Config, tmp_path: Path
    ) -> None:
        empty_dist = tmp_path / "empty_dist"
        empty_dist.mkdir()
        with _patch_app_dist(empty_dist):
            app = create_app(db=memory_db, config=mock_config)
            with TestClient(app) as client:
                resp = client.get("/")
                assert resp.status_code == 200
                assert "text/html" in resp.headers.get("content-type", "")
                assert "web UI is not built" in resp.text

                tok = "a1B2" * 11
                resp_invite = client.get(f"/invite/{tok}")
                assert resp_invite.status_code == 200
                assert "text/html" in resp_invite.headers.get("content-type", "")
                assert resp_invite.headers.get("cache-control") == "no-store"
                assert resp_invite.text == resp.text

    def test_public_pwa_assets_served_at_root(
        self, memory_db: Database, mock_config: Config
    ) -> None:
        app = create_app(db=memory_db, config=mock_config)
        with TestClient(app) as client:
            resp_manifest = client.get("/manifest.json")
            assert resp_manifest.status_code == 200
            assert "TrackSeerr" in resp_manifest.text

            resp_favicon = client.get("/favicon.svg")
            assert resp_favicon.status_code == 200

            resp_logo = client.get("/trackseerr-logo.svg")
            assert resp_logo.status_code == 200

    def test_static_assets_chunk_served(
        self, memory_db: Database, mock_config: Config, tmp_path: Path
    ) -> None:
        dist_assets = Path("frontend/dist/assets")
        if dist_assets.is_dir() and list(dist_assets.glob("*.js")):
            target_chunk = list(dist_assets.glob("*.js"))[0].name
            app = create_app(db=memory_db, config=mock_config)
            with TestClient(app) as client:
                resp = client.get(f"/assets/{target_chunk}")
                assert resp.status_code == 200
                assert len(resp.content) > 0
        else:
            mock_dist = _create_mock_dist(tmp_path)
            with _patch_app_dist(mock_dist):
                app = create_app(db=memory_db, config=mock_config)
                with TestClient(app) as client:
                    resp = client.get("/assets/index-mock.js")
                    assert resp.status_code == 200
                    assert len(resp.content) > 0

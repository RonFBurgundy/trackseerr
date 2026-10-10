"""Tests for Frontend Single-Page Dashboard & Static Assets (Phase 4)."""

import pytest
from fastapi.testclient import TestClient

from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.config import Config
from trackseerr.storage import Database


@pytest.fixture
def test_db():
    """Provides an isolated in-memory Database instance."""
    db = Database(":memory:")
    yield db
    db.close()


@pytest.fixture
def test_config(tmp_path):
    """Provides a test Config pointing to tmp_path."""
    return Config(
        plex_url="http://127.0.0.1:32400",
        plex_token="test-plex-token",
        data_dir=str(tmp_path),
        spotify_client_id="sp-client-id",
        spotify_client_secret="sp-client-secret",
    )


@pytest.fixture
def client(test_db, test_config):
    """Creates a FastAPI test client configured with in-memory DB and test Config."""
    app = create_app(db=test_db, config=test_config)
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_config] = lambda: test_config

    with TestClient(app) as test_client:
        yield test_client


class TestStaticAssets:
    """Validates that JavaScript, CSS, and asset files are served correctly."""

    def test_pwa_manifest_served_and_valid(self, client):
        """Validates that manifest.json is served with valid JSON structure."""
        resp = client.get("/static/manifest.json")
        assert resp.status_code == 200
        manifest = resp.json()
        assert manifest["name"] == "TrackSeerr"
        assert manifest["short_name"] == "TrackSeerr"
        assert manifest["start_url"] == "/"
        assert manifest["display"] == "standalone"
        assert manifest["background_color"] == "#0a0a0a"
        assert manifest["theme_color"] == "#0a0a0a"

        icons = manifest.get("icons", [])
        icon_srcs = [icon["src"] for icon in icons]
        assert "/static/icon-192.png" in icon_srcs
        assert "/static/icon-512.png" in icon_srcs

    def test_pwa_mobile_app_icons_served(self, client):
        """Validates that PWA and Apple Touch icon assets exist and are valid PNG images."""
        png_sig = b"\x89PNG\r\n\x1a\n"
        for icon_path in ("/static/icon-192.png", "/static/icon-512.png", "/static/apple-touch-icon.png"):
            resp = client.get(icon_path)
            assert resp.status_code == 200, f"Failed to fetch {icon_path}"
            assert "image/png" in resp.headers.get("content-type", "")
            assert resp.content.startswith(png_sig), f"{icon_path} is not a valid PNG"
            assert len(resp.content) >= 1_000, f"{icon_path} unexpectedly small"

    def test_static_placeholder_svg_served(self, client):
        resp = client.get("/static/placeholder.svg")
        assert resp.status_code == 200
        assert "svg" in resp.headers.get("content-type", "")


class TestContentSecurityPolicy:
    """Validates CSP header allows CDN scripts, Google Fonts, and external artwork."""

    def test_csp_header_values(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

        csp = resp.headers.get("Content-Security-Policy", "")
        assert csp, "Content-Security-Policy header is missing"

        # Default self
        assert "default-src 'self'" in csp

        # Scripts: self, eval, inline, tailwindcdn, unpkg, jsdelivr
        assert "script-src 'self' 'unsafe-eval' 'unsafe-inline'" in csp
        assert "https://cdn.tailwindcss.com" in csp
        assert "https://unpkg.com" in csp
        assert "https://cdn.jsdelivr.net" in csp

        # Styles: self, inline, google fonts
        assert "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com" in csp

        # Fonts: self, gstatic
        assert "font-src 'self' https://fonts.gstatic.com" in csp

        # Images: self, data:, https: (for Spotify/Deezer artwork)
        assert "img-src 'self' data: https:" in csp

        # Media: self, https:, data: (for Deezer/iTunes 30s audio previews)
        assert "media-src 'self' https: data:" in csp

        # Connections: self (SSE and API)
        assert "connect-src 'self'" in csp

        # Frame ancestors: none
        assert "frame-ancestors 'none'" in csp

    def test_csp_header_on_api_endpoints(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp


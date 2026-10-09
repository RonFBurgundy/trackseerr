"""Tests for Frontend Single-Page Dashboard & Static Assets (Phase 4)."""

import re
from html.parser import HTMLParser
from pathlib import Path

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


class TestFrontendDashboard:
    """Validates root route serving and HTML structure."""

    def test_root_returns_200_and_index_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "TrackSeerr" in resp.text

    def test_root_contains_dashboard_elements(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.text

        # Alpine.js state hook
        assert "plexHubApp()" in html
        assert "x-data" in html

        # Plex / Seerr dark styling and accents
        assert "#e5a00d" in html
        assert "bg-slate-950" in html

        # Key navigation & header elements
        assert "TrackSeerr" in html
        assert "Live Sync Status" in html or "syncStatus" in html

        # Tape deck transport navigation & Walkman tactile brand button
        assert "tape-transport-bay" in html
        assert "tape-deck-btn" in html
        assert "tape-deck-indicator" in html
        assert "active:translate-y-[2px]" in html

        # Modals & drawers
        assert "Add Playlist" in html
        assert "Unmatched Tracks" in html or "Missing Tracks" in html
        assert "Sync Engine Live Console" in html or "terminal-console" in html

        # PIN auth flow elements & Native mobile landing hero
        assert "Sign In with Plex" in html
        assert "Authorize with Plex" in html
        assert "plex.tv" in html
        assert "landing-hero" in html
        assert "pin-code-digit" not in html

        # Target toggling and CSV export
        assert "target-pill" in html or "toggleUserTarget" in html
        assert "Export Safe CSV" in html

        # Keyless Spotify & Multi-tab Import Features
        assert "By Link" in html
        assert "Paste Tracks" in html
        assert "1-Click Helper" in html
        assert "No Spotify API Key Needed" in html
        assert "Send to Plexamp" in html
        assert "Import to Plexamp" in html

        # Lidarr & Automated Missing Feeds
        assert "Lidarr & Feeds" in html
        assert "Automated Missing Music Feeds" in html
        assert "pushAllToLidarr" in html
        assert "pushTrackToLidarr" in html

    def test_mobile_navigation_elements(self, client):
        """Validates mobile navigation header, drawer, and tactile switches."""
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.text

        # Mobile drawer, hamburger button, and tactile switches
        assert "isMobileMenuOpen" in html
        assert "toggleMobileMenu" in html
        assert "closeMobileMenu" in html
        assert "tactile-switch" in html
        assert "tape-deck-btn w-full" in html

    def test_modal_internal_scroll_architecture(self, client):
        """Validates that modals enforce internal scroll architecture and full-width mobile constraints."""
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.text

        assert "modal-body-scroll" in html
        assert "max-h-[90dvh]" in html
        assert "isAddModalOpen" in html
        assert "isMissingModalOpen" in html
        assert "isMatchModalOpen" in html
        assert "isClientModalOpen" in html
        assert "isIndexerModalOpen" in html
        assert "isProfileModalOpen" in html
        assert "isSearchModalOpen" in html

        # Full-width mobile and sharp 4px fillet architecture
        assert "w-full max-w-none" in html
        assert "rounded-none sm:rounded-[4px]" in html
        assert "border-0 sm:border border-[#262626]" in html
        assert "items-end sm:items-center" in html

    def test_pwa_head_metadata_and_manifest(self, client):
        """Validates PWA manifest, Apple touch icon, and mobile web app meta tags."""
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.text

        assert '<link rel="manifest" href="/static/manifest.json">' in html
        assert '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">' in html
        assert '<meta name="mobile-web-app-capable" content="yes">' in html
        assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
        assert '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">' in html
        assert '<meta name="apple-mobile-web-app-title" content="TrackSeerr">' in html
        assert '<meta name="application-name" content="TrackSeerr">' in html
        assert '<meta name="theme-color" content="#0a0a0a">' in html


class TestStaticAssets:
    """Validates that JavaScript, CSS, and asset files are served correctly."""

    def test_static_app_js_served(self, client):
        resp = client.get("/static/app.js")
        assert resp.status_code == 200
        content_type = resp.headers.get("content-type", "")
        assert "javascript" in content_type or "text/plain" in content_type
        assert "plexHubApp" in resp.text
        assert "api/sync/stream" in resp.text
        assert "api/auth/plex/pin" in resp.text
        assert "api/missing/csv" in resp.text
        assert "api/playlists/import" in resp.text
        assert "parseImportText" in resp.text
        assert "pasteFromClipboard" in resp.text
        assert "getBookmarkletHref" in resp.text
        assert "checkHashImport" in resp.text
        assert "openPlexAuth" in resp.text
        assert "startPlexAuth" in resp.text
        assert "checkAuthCallback" in resp.text
        assert "verifyPinAndLogin" in resp.text
        assert "cancelAuthFlow" in resp.text
        assert "fetchLidarrStatus" in resp.text
        assert "pushAllToLidarr" in resp.text
        assert "pushTrackToLidarr" in resp.text
        assert "getRssFeedUrl" in resp.text
        assert "getTextFeedUrl" in resp.text
        assert "getWebhookUrl" in resp.text
        assert "toggleMobileMenu" in resp.text
        assert "isAnyModalOpen" in resp.text

    def test_static_style_css_served(self, client):
        resp = client.get("/static/style.css")
        assert resp.status_code == 200
        assert "text/css" in resp.headers.get("content-type", "")
        assert "#e5a00d" in resp.text
        assert "glass-panel" in resp.text
        assert "terminal-console" in resp.text

    def test_design_tokens_and_theme(self, client):
        """Validates responsive design tokens, tape transport rules, and sharp fillets."""
        resp = client.get("/static/style.css")
        assert resp.status_code == 200
        css = resp.text

        # Obsidian chassis color variables
        assert "--bg-canvas: #0a0a0a;" in css
        assert "--bg-surface: #121212;" in css
        assert "--border-subtle: #222222;" in css
        assert "--border-default: #2a2a2a;" in css
        assert "--color-plex-amber" in css

        # Scroll lock and momentum scrolling
        assert "body.modal-open" in css
        assert "modal-body-scroll" in css

        # Tape deck transport and tactile switch classes
        assert ".tape-transport-bay" in css
        assert ".tape-deck-btn" in css
        assert ".tape-deck-indicator" in css
        assert ".tactile-switch" in css

        # Sharp 4px industrial fillets
        assert "border-radius: 4px;" in css
        assert ".playlist-card" in css
        assert ".overseerr-card" in css
        assert ".glass-modal" in css

        # Full-width mobile responsive overrides
        assert "@media (max-width: 639px)" in css
        assert "border-radius: 0 !important;" in css
        assert "width: 100% !important;" in css

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


class HTMLDOMIntegrityParser(HTMLParser):
    """HTML parser that validates tag balancing and tracks Alpine sub-tab nesting."""

    VOID_ELEMENTS: frozenset[str] = frozenset({"img", "input", "br", "hr", "meta", "link"})

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None], tuple[int, int]]] = []
        self.unclosed_tags: list[tuple[str, tuple[int, int]]] = []
        self.mismatched_tags: list[dict[str, object]] = []
        self.subtabs: dict[str, dict[str, object]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        x_show = attrs_dict.get("x-show") or ""
        match = re.search(r"settingsSubTab\s*===\s*'([^']+)'", x_show)
        if match:
            subtab_name = match.group(1)
            parent_tag = self.stack[-1][0] if self.stack else None
            parent_attrs = self.stack[-1][1] if self.stack else {}
            self.subtabs[subtab_name] = {
                "depth": len(self.stack),
                "parent_tag": parent_tag,
                "parent_attrs": parent_attrs,
                "pos": self.getpos(),
            }

        if tag in self.VOID_ELEMENTS:
            return

        self.stack.append((tag, attrs_dict, self.getpos()))

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID_ELEMENTS:
            return

        if not self.stack:
            self.mismatched_tags.append({
                "error": "unexpected_closing_tag",
                "tag": tag,
                "pos": self.getpos(),
            })
            return

        last_tag, _, start_pos = self.stack.pop()
        if last_tag != tag:
            self.mismatched_tags.append({
                "error": "tag_mismatch",
                "expected": last_tag,
                "found": tag,
                "start_pos": start_pos,
                "end_pos": self.getpos(),
            })

    def close(self) -> None:
        super().close()
        self.unclosed_tags = [(tag, pos) for tag, _, pos in self.stack]


class TestDOMIntegrity:
    """Validates HTML tag balance and settings sub-tab hierarchy."""

    EXPECTED_SETTINGS_SUBTABS: frozenset[str] = frozenset({
        "general",
        "media",
        "clients",
        "indexers",
        "lidarr",
        "profiles",
        "status",
    })

    def test_static_index_html_tag_balance_and_dom_hierarchy(self) -> None:
        """Validates that index.html has 0 unclosed tags and 0 tag mismatches."""
        html_path = Path(__file__).resolve().parent.parent / "trackseerr" / "static" / "index.html"
        assert html_path.is_file(), f"index.html not found at {html_path}"

        content = html_path.read_text(encoding="utf-8")
        parser = HTMLDOMIntegrityParser()
        parser.feed(content)
        parser.close()

        assert len(parser.unclosed_tags) == 0, f"Unclosed tags found: {parser.unclosed_tags}"
        assert len(parser.mismatched_tags) == 0, f"Mismatched tags found: {parser.mismatched_tags}"

        # Assert all 6 settings subtabs exist
        assert set(parser.subtabs.keys()) == self.EXPECTED_SETTINGS_SUBTABS

        # Assert all 6 settings subtabs are at identical DOM depth
        depths = {info["depth"] for info in parser.subtabs.values()}
        assert len(depths) == 1, f"Settings sub-tabs have divergent nesting depths: {parser.subtabs}"

        # Assert all 6 settings subtabs share the identical parent (Settings tab container)
        for name, info in parser.subtabs.items():
            assert info["parent_tag"] == "div", f"Subtab '{name}' parent tag is {info['parent_tag']}, expected 'div'"
            parent_attrs = info["parent_attrs"]
            assert isinstance(parent_attrs, dict)
            parent_x_show = parent_attrs.get("x-show", "")
            assert parent_x_show == "activeTab === 'settings'", (
                f"Subtab '{name}' is nested under x-show='{parent_x_show}', expected 'activeTab === 'settings''"
            )

    def test_served_dashboard_dom_integrity(self, client: TestClient) -> None:
        """Validates that GET / served HTML satisfies full DOM integrity."""
        resp = client.get("/")
        assert resp.status_code == 200

        parser = HTMLDOMIntegrityParser()
        parser.feed(resp.text)
        parser.close()

        assert len(parser.unclosed_tags) == 0, f"Unclosed tags in served HTML: {parser.unclosed_tags}"
        assert len(parser.mismatched_tags) == 0, f"Mismatched tags in served HTML: {parser.mismatched_tags}"
        assert set(parser.subtabs.keys()) == self.EXPECTED_SETTINGS_SUBTABS

        depths = {info["depth"] for info in parser.subtabs.values()}
        assert len(depths) == 1, f"Subtabs have mismatched depths in served HTML: {parser.subtabs}"


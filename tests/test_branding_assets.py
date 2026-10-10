"""Unit tests for TrackSeerr branding assets, icons, and references.

Validates the master Sony Walkman vector SVG, rendered PNGs, static Web UI favicons,
and template/documentation asset links across Unraid XML, index.html, and README.md.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _read_png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    """Extract width and height from PNG IHDR chunk."""
    assert len(png_bytes) >= 24, "PNG data too short"
    assert png_bytes[:8] == PNG_SIGNATURE, "Invalid PNG magic signature"
    assert png_bytes[12:16] == b"IHDR", "Missing IHDR chunk"
    width, height = struct.unpack(">II", png_bytes[16:24])
    return width, height


class TestUnraidBrandingAssets:
    """Tests for Unraid vector SVG, rendered PNG, and XML template."""

    @property
    def svg_path(self) -> Path:
        return REPO_ROOT / "unraid" / "trackseerr.svg"

    @property
    def png_path(self) -> Path:
        return REPO_ROOT / "unraid" / "trackseerr.png"

    @property
    def xml_path(self) -> Path:
        return REPO_ROOT / "unraid" / "trackseerr.xml"

    def test_unraid_svg_exists_and_parses_valid_xml(self) -> None:
        assert self.svg_path.is_file(), f"Expected SVG at {self.svg_path}"
        content = self.svg_path.read_text(encoding="utf-8")
        assert len(content) > 500, "SVG content unexpectedly short"

        tree = ET.parse(self.svg_path)
        root = tree.getroot()
        assert root.tag.endswith("svg"), f"Root tag must be svg, got {root.tag}"
        assert root.attrib.get("viewBox") == "0 0 512 512"
        assert root.attrib.get("width") == "512"
        assert root.attrib.get("height") == "512"

    def test_unraid_svg_contains_required_walkman_components_and_stickers(self) -> None:
        tree = ET.parse(self.svg_path)
        root = tree.getroot()

        all_ids: set[str] = {
            elem.attrib["id"] for elem in root.iter() if "id" in elem.attrib
        }

        # Check Walkman structural sections
        assert "walkman-shadow" in all_ids
        assert "sticker-adhesion" in all_ids
        assert "top-controls" in all_ids
        assert "side-controls" in all_ids
        assert "cassette-section" in all_ids
        assert "lower-faceplate" in all_ids

        # Check required partner and project stickers
        assert "sticker-seerr" in all_ids, "Missing Overseerr/Jellyseerr sticker"
        assert "sticker-lidarr" in all_ids, "Missing Lidarr sticker"
        assert "sticker-plex" in all_ids, "Missing Plex sticker"

    def test_unraid_svg_filter_compatibility(self) -> None:
        content = self.svg_path.read_text(encoding="utf-8")
        # Ensure SVG 1.1 filter primitives are used for librsvg and browser compatibility
        assert "feGaussianBlur" in content
        assert "feOffset" in content
        assert "feComponentTransfer" in content
        assert "feMerge" in content

    def test_unraid_png_integrity_and_resolution(self) -> None:
        assert self.png_path.is_file(), f"Expected PNG at {self.png_path}"
        png_data = self.png_path.read_bytes()

        assert png_data.startswith(PNG_SIGNATURE), "File does not have PNG header signature"
        assert len(png_data) > 10_000, f"PNG size {len(png_data)} is below 10,000 bytes"

        width, height = _read_png_dimensions(png_data)
        assert width == 512, f"Expected width 512, got {width}"
        assert height == 512, f"Expected height 512, got {height}"

    def test_unraid_xml_icon_reference(self) -> None:
        assert self.xml_path.is_file(), f"Expected XML at {self.xml_path}"
        tree = ET.parse(self.xml_path)
        root = tree.getroot()
        assert root.tag == "Container", f"Expected root <Container>, got <{root.tag}>"

        icon_elem = root.find("Icon")
        assert icon_elem is not None, "Missing <Icon> element in trackseerr.xml"
        expected_icon = (
            "https://raw.githubusercontent.com/RonFBurgundy/trackseerr/main/unraid/trackseerr.png"
        )
        assert icon_elem.text == expected_icon


class TestStaticWebUIBrandingAssets:
    """Tests for Web UI static logos, favicons, and index.html markup."""

    @property
    def static_dir(self) -> Path:
        return REPO_ROOT / "trackseerr" / "static"

    def test_static_assets_exist(self) -> None:
        logo_svg = self.static_dir / "trackseerr-logo.svg"
        fav_svg = self.static_dir / "favicon.svg"
        fav_png = self.static_dir / "favicon.png"

        assert logo_svg.is_file(), f"Missing {logo_svg}"
        assert fav_svg.is_file(), f"Missing {fav_svg}"
        assert fav_png.is_file(), f"Missing {fav_png}"

    def test_static_svgs_are_valid_xml(self) -> None:
        for svg_name in ("trackseerr-logo.svg", "favicon.svg"):
            path = self.static_dir / svg_name
            tree = ET.parse(path)
            root = tree.getroot()
            assert root.tag.endswith("svg"), f"{svg_name} root is not svg"
            assert root.attrib.get("viewBox") == "0 0 512 512"

    def test_static_favicon_png_integrity(self) -> None:
        fav_png = self.static_dir / "favicon.png"
        data = fav_png.read_bytes()
        assert data.startswith(PNG_SIGNATURE)
        assert len(data) >= 1_000, f"Favicon PNG too small ({len(data)} bytes)"
        width, height = _read_png_dimensions(data)
        assert width == 64
        assert height == 64

    def test_index_html_branding_references(self) -> None:
        index_path = REPO_ROOT / "frontend" / "index.html"
        assert index_path.is_file(), f"Missing {index_path}"
        html = index_path.read_text(encoding="utf-8")

        # Favicons, apple-touch-icon, and manifest in <head>
        assert '<link rel="manifest" href="/manifest.json" />' in html
        assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png" />' in html
        assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg" />' in html
        assert '<link rel="icon" type="image/png" href="/favicon.png" />' in html


class TestReadmeHeroBranding:
    """Tests for repository README.md branding presentation."""

    @property
    def readme_path(self) -> Path:
        return REPO_ROOT / "README.md"

    def test_readme_references_trackseerr_png(self) -> None:
        assert self.readme_path.is_file(), f"Missing {self.readme_path}"
        content = self.readme_path.read_text(encoding="utf-8")

        assert '<img src="unraid/trackseerr.png"' in content
        assert 'alt="TrackSeerr Logo"' in content
        # Ensure centered presentation is placed at the top before # TrackSeerr
        logo_pos = content.find('<img src="unraid/trackseerr.png"')
        title_pos = content.find("# TrackSeerr")
        assert logo_pos != -1, "Logo img tag not found"
        assert title_pos != -1, "Title # TrackSeerr not found"
        assert logo_pos < title_pos, "Hero logo must appear before # TrackSeerr heading"


class TestMakeIconScript:
    """Tests for the unraid/make-icon.py generation utility."""

    @pytest.fixture
    def make_icon_module(self):
        script_path = REPO_ROOT / "unraid" / "make-icon.py"
        spec = importlib.util.spec_from_file_location("make_icon", script_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_find_repo_root(self, make_icon_module) -> None:
        root = make_icon_module.find_repo_root()
        assert (root / "unraid" / "trackseerr.svg").is_file()

    def test_render_svg_missing_file_returns_false(self, make_icon_module, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.svg"
        output = tmp_path / "out.png"
        assert make_icon_module.render_svg_to_png(missing, output) is False
        assert not output.exists()

    def test_render_svg_with_mocked_rsvg(self, make_icon_module, tmp_path: Path) -> None:
        svg_file = tmp_path / "sample.svg"
        svg_file.write_text("<svg></svg>", encoding="utf-8")
        out_png = tmp_path / "sample.png"

        with patch("shutil.which", return_value="/usr/bin/rsvg-convert"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                # simulate creating output file
                out_png.write_bytes(PNG_SIGNATURE + b"\x00" * 20)
                result = make_icon_module.render_svg_to_png(svg_file, out_png, 128, 128)
                assert result is True
                mock_run.assert_called_once()

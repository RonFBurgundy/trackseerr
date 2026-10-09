"""Tests for security module: SSRF prevention, ID extraction, path safety, and text sanitization."""

from pathlib import Path
import pytest

from trackseerr.security import (
    extract_deezer_id,
    extract_spotify_id,
    is_safe_service_url,
    mask_secret,
    safe_data_path,
    sanitize_csv_cell,
    sanitize_text,
)


class TestExtractSpotifyId:
    def test_valid_raw_id(self):
        valid_id = "37i9dQZF1DXcBWIGoYBM5M"
        assert extract_spotify_id(valid_id) == valid_id
        assert extract_spotify_id("4aawyAB9vmqN3uQ7FjRGTy") == "4aawyAB9vmqN3uQ7FjRGTy"

    def test_valid_spotify_uri(self):
        assert (
            extract_spotify_id("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
            == "37i9dQZF1DXcBWIGoYBM5M"
        )

    def test_valid_spotify_urls(self):
        # Standard HTTPS URL
        assert (
            extract_spotify_id("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
            == "37i9dQZF1DXcBWIGoYBM5M"
        )
        # HTTP URL
        assert (
            extract_spotify_id("http://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
            == "37i9dQZF1DXcBWIGoYBM5M"
        )
        # URL with query parameters
        assert (
            extract_spotify_id(
                "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abcd1234&pt=5678"
            )
            == "37i9dQZF1DXcBWIGoYBM5M"
        )
        # URL with trailing slash
        assert (
            extract_spotify_id("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M/")
            == "37i9dQZF1DXcBWIGoYBM5M"
        )
        # URL with international code
        assert (
            extract_spotify_id(
                "https://open.spotify.com/intl-de/playlist/37i9dQZF1DXcBWIGoYBM5M"
            )
            == "37i9dQZF1DXcBWIGoYBM5M"
        )
        # Legacy user playlist URL
        assert (
            extract_spotify_id(
                "https://open.spotify.com/user/spotify/playlist/37i9dQZF1DXcBWIGoYBM5M"
            )
            == "37i9dQZF1DXcBWIGoYBM5M"
        )

    def test_ssrf_and_malicious_urls_rejected(self):
        # Attacker host
        assert extract_spotify_id("https://evil.com/playlist/37i9dQZF1DXcBWIGoYBM5M") is None
        # Subdomain bypass attempt
        assert (
            extract_spotify_id(
                "https://open.spotify.com.attacker.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
            )
            is None
        )
        # Userinfo bypass attempt
        assert (
            extract_spotify_id(
                "https://open.spotify.com@evil.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
            )
            is None
        )
        assert (
            extract_spotify_id(
                "https://admin:secret@open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
            )
            is None
        )
        # Port injection
        assert (
            extract_spotify_id(
                "https://open.spotify.com:8443/playlist/37i9dQZF1DXcBWIGoYBM5M"
            )
            is None
        )
        # Internal network / SSRF probes
        assert extract_spotify_id("http://169.254.169.254/latest/meta-data/") is None
        assert extract_spotify_id("http://localhost:8000/playlist/37i9dQZF1DXcBWIGoYBM5M") is None
        assert extract_spotify_id("http://127.0.0.1/playlist/37i9dQZF1DXcBWIGoYBM5M") is None
        # Non-HTTP schemes
        assert extract_spotify_id("file:///etc/passwd") is None
        assert extract_spotify_id("javascript:alert(1)") is None
        assert extract_spotify_id("data:text/html,37i9dQZF1DXcBWIGoYBM5M") is None
        # Non-playlist paths
        assert extract_spotify_id("https://open.spotify.com/track/37i9dQZF1DXcBWIGoYBM5M") is None
        assert extract_spotify_id("https://open.spotify.com/album/37i9dQZF1DXcBWIGoYBM5M") is None
        # Path traversal within path
        assert (
            extract_spotify_id(
                "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M/../../evil"
            )
            is None
        )

    def test_invalid_ids_rejected(self):
        # Too short (21 chars)
        assert extract_spotify_id("37i9dQZF1DXcBWIGoYBM5") is None
        # Too long (23 chars)
        assert extract_spotify_id("37i9dQZF1DXcBWIGoYBM5M1") is None
        # Special characters / SQL injection attempt
        assert extract_spotify_id("37i9dQZF1DXcBWIGoYBM5M'") is None
        assert extract_spotify_id("37i9dQZF1DXcBWIGoYBM5M; DROP TABLE playlists;") is None
        # Non-string / empty
        assert extract_spotify_id("") is None
        assert extract_spotify_id(None) is None
        assert extract_spotify_id("   ") is None


class TestExtractDeezerId:
    def test_valid_raw_id(self):
        assert extract_deezer_id("1313621735") == "1313621735"
        # Min boundary (5 digits)
        assert extract_deezer_id("12345") == "12345"
        # Max boundary (15 digits)
        assert extract_deezer_id("123456789012345") == "123456789012345"

    def test_valid_deezer_urls(self):
        assert (
            extract_deezer_id("https://www.deezer.com/playlist/1313621735")
            == "1313621735"
        )
        assert (
            extract_deezer_id("https://deezer.com/playlist/1313621735")
            == "1313621735"
        )
        assert (
            extract_deezer_id("https://www.deezer.com/us/playlist/1313621735")
            == "1313621735"
        )
        assert (
            extract_deezer_id(
                "http://www.deezer.com/fr/playlist/1313621735?utm_source=web"
            )
            == "1313621735"
        )

    def test_ssrf_and_malicious_urls_rejected(self):
        # Attacker host
        assert extract_deezer_id("https://attacker.com/playlist/1313621735") is None
        # Subdomain bypass
        assert (
            extract_deezer_id("https://deezer.com.evil.com/playlist/1313621735")
            is None
        )
        # Userinfo bypass
        assert (
            extract_deezer_id("https://user:pass@deezer.com/playlist/1313621735")
            is None
        )
        # Port injection
        assert (
            extract_deezer_id("https://deezer.com:8080/playlist/1313621735")
            is None
        )
        # Internal network SSRF
        assert extract_deezer_id("http://169.254.169.254/playlist/1313621735") is None
        # Non-playlist Deezer URLs
        assert extract_deezer_id("https://www.deezer.com/album/1313621735") is None
        assert extract_deezer_id("https://www.deezer.com/track/1313621735") is None

    def test_invalid_ids_rejected(self):
        # Too short (< 5 digits)
        assert extract_deezer_id("1234") is None
        # Too long (> 15 digits)
        assert extract_deezer_id("1234567890123456") is None
        # Alphabetic / alphanumeric
        assert extract_deezer_id("1313621735abc") is None
        assert extract_deezer_id("abcdef") is None
        # Empty / None
        assert extract_deezer_id("") is None
        assert extract_deezer_id(None) is None
        assert extract_deezer_id("   ") is None


class TestSanitizeText:
    def test_strip_html_tags(self):
        assert sanitize_text("<b>Bold Title</b>") == "Bold Title"
        assert (
            sanitize_text("<p>Paragraph <a href='https://evil.com'>Link</a></p>")
            == "Paragraph Link"
        )
        assert sanitize_text("<script>alert('xss')</script>") == "alert('xss')"
        assert sanitize_text("<img src=x onerror=alert(1)>Image") == "Image"

    def test_strip_control_characters(self):
        assert sanitize_text("Track\x00Name\x08") == "TrackName"
        assert sanitize_text("Song\x1b[31mTitle\x1f") == "Song[31mTitle"
        assert sanitize_text("Safe\x7fText") == "SafeText"

    def test_preserve_normal_text(self):
        assert sanitize_text("Rock & Roll (2024 Remaster)") == "Rock & Roll (2024 Remaster)"
        assert sanitize_text("Pop / Synth-wave — Vol. 1") == "Pop / Synth-wave — Vol. 1"

    def test_none_and_empty(self):
        assert sanitize_text("") == ""
        assert sanitize_text(None) == ""


class TestSafeDataPath:
    def test_valid_filename(self, tmp_path):
        base = tmp_path / "data"
        base.mkdir()
        result = safe_data_path("playlists.db", base_dir=str(base))
        assert result == (base / "playlists.db").resolve()
        assert result.is_relative_to(base.resolve())

    def test_valid_nested_filename(self, tmp_path):
        base = tmp_path / "data"
        base.mkdir()
        result = safe_data_path("sub/folder/file.csv", base_dir=str(base))
        assert result == (base / "sub/folder/file.csv").resolve()
        assert result.is_relative_to(base.resolve())

    def test_path_traversal_attempts_raise(self, tmp_path):
        base = tmp_path / "data"
        base.mkdir()
        with pytest.raises(ValueError, match="Path traversal detected"):
            safe_data_path("../etc/passwd", base_dir=str(base))

        with pytest.raises(ValueError, match="Path traversal detected"):
            safe_data_path("sub/../../etc/shadow", base_dir=str(base))

        with pytest.raises(ValueError, match="Path traversal detected"):
            safe_data_path("/etc/passwd", base_dir=str(base))

        with pytest.raises(ValueError, match="Path traversal detected"):
            safe_data_path(".", base_dir=str(base))

        with pytest.raises(ValueError, match="Path traversal detected"):
            safe_data_path("..", base_dir=str(base))

    def test_empty_or_invalid_filename(self, tmp_path):
        base = tmp_path / "data"
        base.mkdir()
        with pytest.raises(ValueError, match="Filename is empty or invalid"):
            safe_data_path("", base_dir=str(base))

        with pytest.raises(ValueError, match="Filename is empty or invalid"):
            safe_data_path("   ", base_dir=str(base))

        with pytest.raises(ValueError, match="Filename is empty or invalid"):
            safe_data_path("\x00\x08", base_dir=str(base))

        with pytest.raises(ValueError, match="Filename must be a string"):
            safe_data_path(12345, base_dir=str(base))  # type: ignore


class TestMaskSecret:
    def test_mask_standard_secret(self):
        assert mask_secret("abcdef1234", visible_chars=4) == "••••••1234"
        assert mask_secret("my_super_secret_token_9999", visible_chars=4) == "••••••••••••••••••••••9999"

    def test_mask_short_secret(self):
        # When length is less than or equal to visible_chars, full masking
        assert mask_secret("1234", visible_chars=4) == "••••"
        assert mask_secret("abc", visible_chars=4) == "•••"

    def test_mask_zero_or_negative_visible(self):
        assert mask_secret("secret", visible_chars=0) == "••••••"
        assert mask_secret("secret", visible_chars=-1) == "••••••"

    def test_mask_none_or_empty(self):
        assert mask_secret(None) == ""
        assert mask_secret("") == ""


class TestIsSafeServiceUrl:
    def test_valid_lan_and_homelab_urls(self):
        assert is_safe_service_url("http://192.168.1.100:8080") is True
        assert is_safe_service_url("http://10.0.0.5:5030") is True
        assert is_safe_service_url("http://172.16.0.2:9696") is True
        assert is_safe_service_url("http://slskd:5030") is True
        assert is_safe_service_url("http://prowlarr:9696/1/api") is True
        assert is_safe_service_url("http://localhost:8080") is True
        assert is_safe_service_url("http://127.0.0.1:8080") is True
        assert is_safe_service_url("https://api.spotify.com") is True

    def test_loopback_rejected_when_not_allow_lan(self):
        assert is_safe_service_url("http://127.0.0.1:8080", allow_lan=False) is False
        assert is_safe_service_url("http://localhost:8080", allow_lan=False) is False

    def test_dns_resolution_and_allow_lan(self, monkeypatch):
        # literal 10.0.0.5 rejected with allow_lan=False
        assert is_safe_service_url("http://10.0.0.5:5030", allow_lan=False) is False

        # hostname->127.0.0.1 rejected with allow_lan=True and False
        monkeypatch.setattr("trackseerr.security._resolve_host", lambda host: ["127.0.0.1"])
        assert is_safe_service_url("http://custom-host.local:8080", allow_lan=True) is False
        assert is_safe_service_url("http://custom-host.local:8080", allow_lan=False) is False

        # hostname->192.168.1.5 allowed with allow_lan=True, rejected with allow_lan=False
        monkeypatch.setattr("trackseerr.security._resolve_host", lambda host: ["192.168.1.5"])
        assert is_safe_service_url("http://custom-host.local:8080", allow_lan=True) is True
        assert is_safe_service_url("http://custom-host.local:8080", allow_lan=False) is False

    def test_unspecified_ip_rejected(self):
        assert is_safe_service_url("http://0.0.0.0:8080") is False
        assert is_safe_service_url("http://[::]:8080") is False

    def test_cloud_metadata_rejected(self):
        # IPv4 link-local and AWS / GCP metadata
        assert is_safe_service_url("http://169.254.169.254/latest/meta-data") is False
        assert is_safe_service_url("http://169.254.1.1:8080") is False
        # AWS IMDSv6
        assert is_safe_service_url("http://[fd00:ec2::254]:80") is False
        assert is_safe_service_url("http://metadata.google.internal/computeMetadata/v1/") is False
        assert is_safe_service_url("http://instance-data") is False

    def test_integer_hex_octal_numeric_ips_rejected(self):
        assert is_safe_service_url("http://2130706433:8080") is False
        assert is_safe_service_url("http://0x7f000001:8080") is False
        assert is_safe_service_url("http://017700000001:8080") is False
        assert is_safe_service_url("http://0") is False
        assert is_safe_service_url("http://0x7f.0.0.1") is False
        assert is_safe_service_url("http://0177.0.0.1") is False

    def test_dangerous_schemes_and_userinfo_rejected(self):
        assert is_safe_service_url("file:///etc/passwd") is False
        assert is_safe_service_url("ftp://192.168.1.1") is False
        assert is_safe_service_url("gopher://127.0.0.1") is False
        assert is_safe_service_url("http://user:pass@192.168.1.1:8080") is False
        assert is_safe_service_url("") is False
        assert is_safe_service_url(None) is False


class TestSanitizeCsvCell:
    def test_formula_injection_triggers_neutralized(self):
        assert sanitize_csv_cell("=1+1") == "'=1+1"
        assert sanitize_csv_cell("+cmd|' /C calc'!A0") == "'+cmd|' /C calc'!A0"
        assert sanitize_csv_cell("-5+5") == "'-5+5"
        assert sanitize_csv_cell("@SUM(A1:A10)") == "'@SUM(A1:A10)"
        assert sanitize_csv_cell("\t@SUM") == "'\t@SUM"
        assert sanitize_csv_cell("\r=1+1") == "'\r=1+1"
        assert sanitize_csv_cell("|calc.exe") == "'|calc.exe"

    def test_leading_whitespace_formula_injection_neutralized(self):
        assert sanitize_csv_cell(" =1+1") == "' =1+1"
        assert sanitize_csv_cell("   -2+3") == "'   -2+3"
        assert sanitize_csv_cell("  +cmd") == "'  +cmd"
        assert sanitize_csv_cell("  |pipe_injection") == "'  |pipe_injection"
        assert sanitize_csv_cell(" \t @SUM") == "' \t @SUM"

    def test_benign_text_preserved(self):
        assert sanitize_csv_cell("Normal Song Title") == "Normal Song Title"
        assert sanitize_csv_cell("Artist (feat. Guest)") == "Artist (feat. Guest)"
        assert sanitize_csv_cell("12345") == "12345"
        assert sanitize_csv_cell("") == ""
        assert sanitize_csv_cell(None) == ""


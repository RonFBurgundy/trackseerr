"""Comprehensive tests for Plex OAuth authentication and session token management."""

import base64
import os
import stat
from unittest.mock import MagicMock, patch

import pytest
import requests

from trackseerr.auth import (
    PlexAuthError,
    check_plex_pin,
    create_plex_pin,
    create_session_token,
    get_or_create_secret_key,
    get_plex_user,
    verify_server_access,
    verify_session_token,
)


class TestPlexPinCreation:
    """Tests for create_plex_pin."""

    @patch("trackseerr.auth.requests.post")
    def test_create_plex_pin_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "id": 12345,
            "code": "ABCD",
        }
        mock_post.return_value = mock_resp

        result = create_plex_pin()

        assert result["id"] == 12345
        assert result["code"] == "ABCD"
        assert result["auth_url"].startswith("https://app.plex.tv/auth#?")
        assert "clientID=trackseerr" in result["auth_url"]
        assert "code=ABCD" in result["auth_url"]
        assert "context%5Bdevice%5D%5Bproduct%5D=TrackSeerr" in result["auth_url"]

        # Verify headers sent
        call_headers = mock_post.call_args[1]["headers"]
        assert call_headers["X-Plex-Product"] == "TrackSeerr"
        assert call_headers["X-Plex-Client-Identifier"] == "trackseerr"
        assert call_headers["Accept"] == "application/json"

    @patch("trackseerr.auth.requests.post")
    def test_create_plex_pin_custom_client(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": 999, "code": "XYZ1"}
        mock_post.return_value = mock_resp

        result = create_plex_pin(client_identifier="custom-app")

        assert result["id"] == 999
        assert result["code"] == "XYZ1"
        assert "clientID=custom-app" in result["auth_url"]
        call_headers = mock_post.call_args[1]["headers"]
        assert call_headers["X-Plex-Client-Identifier"] == "custom-app"

    @patch("trackseerr.auth.requests.post")
    def test_create_plex_pin_with_forward_url(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": 12345, "code": "ABCD"}
        mock_post.return_value = mock_resp

        result = create_plex_pin(forward_url="https://trackseerr.local/callback?pin_id=12345")

        assert result["id"] == 12345
        assert result["code"] == "ABCD"
        assert "forwardUrl=https%3A%2F%2Ftrackseerr.local%2Fcallback%3Fpin_id%3D12345" in result["auth_url"]

    @patch("trackseerr.auth.requests.post")
    def test_create_plex_pin_missing_fields_raises(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": 12345}  # missing 'code'
        mock_post.return_value = mock_resp

        with pytest.raises(PlexAuthError, match="missing 'id' or 'code'"):
            create_plex_pin()

    @patch("trackseerr.auth.requests.post")
    def test_create_plex_pin_http_error_raises(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = requests.HTTPError("Server error")
        mock_post.return_value = mock_resp

        with pytest.raises(PlexAuthError, match="Failed to create Plex PIN"):
            create_plex_pin()


class TestCheckPlexPin:
    """Tests for check_plex_pin."""

    @patch("trackseerr.auth.requests.get")
    def test_check_plex_pin_claimed_returns_token(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": 12345,
            "code": "ABCD",
            "authToken": "test-plex-auth-token-12345",
        }
        mock_get.return_value = mock_resp

        token = check_plex_pin(12345)
        assert token == "test-plex-auth-token-12345"

    @patch("trackseerr.auth.requests.get")
    def test_check_plex_pin_pending_returns_none(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": 12345,
            "code": "ABCD",
            "authToken": None,
        }
        mock_get.return_value = mock_resp

        token = check_plex_pin(12345)
        assert token is None

    @patch("trackseerr.auth.requests.get")
    def test_check_plex_pin_not_found_returns_none(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        token = check_plex_pin(99999)
        assert token is None

    def test_check_plex_pin_invalid_id_raises(self):
        with pytest.raises(ValueError, match="Invalid pin_id"):
            check_plex_pin("not-an-int")  # type: ignore[arg-type]

    @patch("trackseerr.auth.requests.get")
    def test_check_plex_pin_http_error_raises(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = requests.HTTPError("Server 500")
        mock_get.return_value = mock_resp

        with pytest.raises(PlexAuthError, match="Error querying Plex PIN"):
            check_plex_pin(12345)


class TestGetPlexUser:
    """Tests for get_plex_user."""

    @patch("trackseerr.auth.requests.get")
    def test_get_plex_user_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": 54321,
            "username": "plexmaster",
            "email": "master@plex.tv",
            "thumb": "https://plex.tv/users/54321/avatar.png",
        }
        mock_get.return_value = mock_resp

        user = get_plex_user("valid-token")
        assert user["id"] == "54321"
        assert user["username"] == "plexmaster"
        assert user["email"] == "master@plex.tv"
        assert user["thumb"] == "https://plex.tv/users/54321/avatar.png"

        call_headers = mock_get.call_args[1]["headers"]
        assert call_headers["X-Plex-Token"] == "valid-token"

    @patch("trackseerr.auth.requests.get")
    def test_get_plex_user_unauthorized_raises(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.raise_for_status.side_effect = requests.HTTPError("401 Unauthorized")
        mock_get.return_value = mock_resp

        with pytest.raises(PlexAuthError, match="Failed to fetch Plex user info"):
            get_plex_user("expired-token")

    def test_get_plex_user_empty_token_raises(self):
        with pytest.raises(ValueError, match="auth_token must be a non-empty string"):
            get_plex_user("")


class TestVerifyServerAccess:
    """Tests for verify_server_access."""

    @patch("trackseerr.auth.requests.get")
    def test_verify_server_access_owner(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "name": "My Plex Server",
                "clientIdentifier": "server-machine-123",
                "owned": True,
            },
            {
                "name": "Friend Server",
                "clientIdentifier": "other-machine-456",
                "owned": False,
            },
        ]
        mock_get.return_value = mock_resp

        has_access, is_owner = verify_server_access("token", "server-machine-123")
        assert has_access is True
        assert is_owner is True

    @patch("trackseerr.auth.requests.get")
    def test_verify_server_access_shared_user(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "name": "Shared Plex Server",
                "clientIdentifier": "shared-machine-789",
                "owned": False,
            }
        ]
        mock_get.return_value = mock_resp

        has_access, is_owner = verify_server_access("token", "shared-machine-789")
        assert has_access is True
        assert is_owner is False

    @patch("trackseerr.auth.requests.get")
    def test_verify_server_access_unauthorized_outsider(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "name": "Unrelated Server",
                "clientIdentifier": "unrelated-machine-000",
                "owned": True,
            }
        ]
        mock_get.return_value = mock_resp

        has_access, is_owner = verify_server_access("token", "target-machine-id")
        assert has_access is False
        assert is_owner is False

    @patch("trackseerr.auth.requests.get")
    def test_verify_server_access_dict_resources_format(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "resources": [
                {
                    "name": "Home Server",
                    "clientIdentifier": "my-home-machine",
                    "owned": True,
                }
            ]
        }
        mock_get.return_value = mock_resp

        has_access, is_owner = verify_server_access("token", "my-home-machine")
        assert has_access is True
        assert is_owner is True

    @patch("trackseerr.auth.requests.get")
    def test_verify_server_access_http_error_returns_false(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_get.return_value = mock_resp

        has_access, is_owner = verify_server_access("bad-token", "server-machine-123")
        assert has_access is False
        assert is_owner is False

    def test_verify_server_access_empty_inputs(self):
        assert verify_server_access("", "server-id") == (False, False)
        assert verify_server_access("token", "") == (False, False)


class TestSessionSecretKey:
    """Tests for get_or_create_secret_key."""

    def test_get_or_create_secret_key_creates_new(self, tmp_path):
        key = get_or_create_secret_key(data_dir=str(tmp_path))
        assert isinstance(key, bytes)
        assert len(key) == 32

        secret_file = tmp_path / ".session_secret"
        assert secret_file.exists()
        assert secret_file.read_bytes() == key

        # Verify file permissions are 0600 (-rw-------)
        file_mode = stat.S_IMODE(os.stat(secret_file).st_mode)
        assert file_mode == 0o600

    def test_get_or_create_secret_key_persists_existing(self, tmp_path):
        secret_file = tmp_path / ".session_secret"
        known_key = b"A" * 32
        secret_file.write_bytes(known_key)
        secret_file.chmod(0o600)

        key = get_or_create_secret_key(data_dir=str(tmp_path))
        assert key == known_key

    def test_get_or_create_secret_key_regenerates_on_invalid_length(self, tmp_path):
        secret_file = tmp_path / ".session_secret"
        secret_file.write_bytes(b"too_short")

        key = get_or_create_secret_key(data_dir=str(tmp_path))
        assert len(key) == 32
        assert key != b"too_short"

    def test_get_or_create_secret_key_unwritable_directory_raises(self, tmp_path):
        with patch("os.access", return_value=False):
            with pytest.raises(PermissionError) as exc_info:
                get_or_create_secret_key(data_dir=str(tmp_path))
            assert "not writable" in str(exc_info.value)


class TestSessionTokens:
    """Tests for create_session_token and verify_session_token."""

    def test_create_and_verify_admin_token(self):
        secret = b"12345678901234567890123456789012"
        token = create_session_token(
            user_id="user-1",
            username="admin_user",
            is_admin=True,
            secret_key=secret,
        )
        assert isinstance(token, str)
        assert "." in token

        verified = verify_session_token(token, secret)
        assert verified is not None
        assert verified["user_id"] == "user-1"
        assert verified["username"] == "admin_user"
        assert verified["is_admin"] is True

    def test_create_and_verify_non_admin_token(self):
        secret = b"12345678901234567890123456789012"
        token = create_session_token(
            user_id="user-2",
            username="family_member",
            is_admin=False,
            secret_key=secret,
        )
        verified = verify_session_token(token, secret)
        assert verified is not None
        assert verified["user_id"] == "user-2"
        assert verified["username"] == "family_member"
        assert verified["is_admin"] is False

    def test_verify_session_token_expired(self):
        secret = b"12345678901234567890123456789012"
        token = create_session_token(
            user_id="user-1",
            username="admin_user",
            is_admin=True,
            secret_key=secret,
            expires_in_seconds=-10,  # Already expired
        )
        verified = verify_session_token(token, secret)
        assert verified is None

    def test_verify_session_token_tampered_payload(self):
        secret = b"12345678901234567890123456789012"
        token = create_session_token(
            user_id="user-1",
            username="normal_user",
            is_admin=False,
            secret_key=secret,
        )
        payload_b64, sig_b64 = token.split(".")
        # Tamper payload: replace normal_user with admin_user
        pad = (4 - len(payload_b64) % 4) % 4
        decoded = base64.urlsafe_b64decode(payload_b64 + "=" * pad).decode("utf-8")
        tampered_decoded = decoded.replace('"is_admin":false', '"is_admin":true')
        tampered_b64 = base64.urlsafe_b64encode(tampered_decoded.encode("utf-8")).decode("ascii").rstrip("=")
        tampered_token = f"{tampered_b64}.{sig_b64}"

        assert verify_session_token(tampered_token, secret) is None

    def test_verify_session_token_non_ascii_is_rejected_not_raised(self):
        secret = b"12345678901234567890123456789012"
        assert verify_session_token("p\u00e9yload.sig\u00e9", secret) is None
        assert verify_session_token("payload.sig\u00e9", secret) is None

    def test_verify_session_token_tampered_signature(self):
        secret = b"12345678901234567890123456789012"
        token = create_session_token(
            user_id="user-1",
            username="user",
            is_admin=False,
            secret_key=secret,
        )
        payload_b64, sig_b64 = token.split(".")
        # Change last char of signature
        altered_sig = sig_b64[:-1] + ("A" if sig_b64[-1] != "A" else "B")
        tampered_token = f"{payload_b64}.{altered_sig}"

        assert verify_session_token(tampered_token, secret) is None

    def test_verify_session_token_wrong_secret(self):
        secret1 = b"11111111111111111111111111111111"
        secret2 = b"22222222222222222222222222222222"
        token = create_session_token(
            user_id="user-1",
            username="user",
            is_admin=False,
            secret_key=secret1,
        )
        assert verify_session_token(token, secret2) is None

    def test_verify_session_token_malformed_inputs(self):
        secret = b"12345678901234567890123456789012"
        assert verify_session_token("", secret) is None
        assert verify_session_token("not-a-token", secret) is None
        assert verify_session_token("part1.part2.part3", secret) is None
        assert verify_session_token("invalid_b64%%.invalid_sig%%", secret) is None
        assert verify_session_token(12345, secret) is None  # type: ignore[arg-type]
        assert verify_session_token("token.sig", b"") is None

    def test_create_session_token_invalid_secret_raises(self):
        with pytest.raises(ValueError, match="secret_key must be non-empty bytes"):
            create_session_token("user-1", "admin", True, b"")

    def test_verify_session_token_non_dict_payload(self):
        secret = b"12345678901234567890123456789012"
        raw_json = b"[1, 2, 3]"
        payload_b64 = base64.urlsafe_b64encode(raw_json).decode("ascii").rstrip("=")
        import hashlib, hmac
        sig = hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
        token = f"{payload_b64}.{sig_b64}"
        assert verify_session_token(token, secret) is None

    def test_verify_session_token_missing_fields_or_invalid_exp(self):
        secret = b"12345678901234567890123456789012"
        import hashlib, hmac, json

        def sign(p):
            raw = json.dumps(p).encode("utf-8")
            pb64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
            s = hmac.new(secret, pb64.encode("ascii"), hashlib.sha256).digest()
            sb64 = base64.urlsafe_b64encode(s).decode("ascii").rstrip("=")
            return f"{pb64}.{sb64}"

        # Missing 'exp' or invalid exp type
        assert verify_session_token(sign({"user_id": "1", "username": "a", "is_admin": True}), secret) is None
        assert verify_session_token(sign({"user_id": "1", "username": "a", "is_admin": True, "exp": "not-int"}), secret) is None
        # Missing 'user_id'
        assert verify_session_token(sign({"username": "a", "is_admin": True, "exp": 9999999999}), secret) is None
        # Missing 'username'
        assert verify_session_token(sign({"user_id": "1", "is_admin": True, "exp": 9999999999}), secret) is None
        # Missing 'is_admin'
        assert verify_session_token(sign({"user_id": "1", "username": "a", "exp": 9999999999}), secret) is None


class TestAuthEdgeCases:
    """Tests for edge cases and input validation in auth module."""

    def test_sanitize_header_value_invalid_inputs(self):
        from trackseerr.auth import _sanitize_header_value

        with pytest.raises(ValueError, match="Header value must be a string"):
            _sanitize_header_value(123)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="Header value cannot be empty"):
            _sanitize_header_value("   \r\n\t  ")

    @patch("trackseerr.auth.requests.get")
    def test_check_plex_pin_network_error(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("Connection failed")
        with pytest.raises(PlexAuthError, match="Network error querying Plex PIN"):
            check_plex_pin(12345)

    @patch("trackseerr.auth.requests.get")
    def test_verify_server_access_network_error(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("Connection timeout")
        assert verify_server_access("token", "machine-id") == (False, False)

    def test_get_or_create_secret_key_file_write_failure(self, tmp_path):
        with patch("builtins.open", side_effect=OSError("Disk write error")):
            with pytest.raises(OSError, match="Disk write error"):
                get_or_create_secret_key(data_dir=str(tmp_path))

    @patch("trackseerr.auth.requests.get")
    def test_check_plex_pin_http_404_error(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_err = requests.HTTPError("404 Not Found")
        mock_err.response = mock_resp
        mock_resp.raise_for_status.side_effect = mock_err
        mock_get.return_value = mock_resp

        assert check_plex_pin(12345) is None

    def test_get_or_create_secret_key_oserror_reading(self, tmp_path):
        secret_file = tmp_path / ".session_secret"
        secret_file.write_bytes(b"A" * 32)
        with patch.object(type(secret_file), "read_bytes", side_effect=OSError("Permission denied")):
            key = get_or_create_secret_key(data_dir=str(tmp_path))
            assert len(key) == 32

    def test_verify_session_token_invalid_base64_sig(self):
        secret = b"12345678901234567890123456789012"
        # Signature that fails base64 decoding
        assert verify_session_token("eyJ1c2VyX2lkIjoiMSJ9.@@invalid-base64@@", secret) is None


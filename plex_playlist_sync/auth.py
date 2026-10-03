"""Plex OAuth PIN authentication and HMAC-SHA256 session token management."""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional, Tuple

import requests

from plex_playlist_sync.security import safe_data_path

logger = logging.getLogger(__name__)

DEFAULT_CLIENT_IDENTIFIER = "trackseerr"
DEFAULT_PRODUCT_NAME = "TrackSeerr"
PLEX_API_URL = "https://plex.tv/api/v2"
PLEX_AUTH_APP_URL = "https://app.plex.tv/auth#"


class PlexAuthError(Exception):
    """Raised when a Plex authentication or authorization operation fails."""


def _sanitize_header_value(value: str) -> str:
    """Sanitize header value against CRLF injection and control characters."""
    if not isinstance(value, str):
        raise ValueError("Header value must be a string")
    cleaned = "".join(ch for ch in value if ch.isprintable() and ch not in ("\r", "\n")).strip()
    if not cleaned:
        raise ValueError("Header value cannot be empty")
    return cleaned


def create_plex_pin(
    client_identifier: str = DEFAULT_CLIENT_IDENTIFIER,
    forward_url: Optional[str] = None,
) -> dict[str, Any]:
    """Request a new Plex PIN for OAuth authentication.

    Headers include X-Plex-Product and X-Plex-Client-Identifier.
    Returns:
        {'id': pin_id, 'code': pin_code, 'auth_url': 'https://app.plex.tv/auth#?...'}
    """
    clean_client_id = _sanitize_header_value(client_identifier)
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": DEFAULT_PRODUCT_NAME,
        "X-Plex-Client-Identifier": clean_client_id,
    }
    try:
        resp = requests.post(
            f"{PLEX_API_URL}/pins",
            headers=headers,
            params={"strong": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise PlexAuthError(f"Failed to create Plex PIN: {e}") from e

    pin_id = data.get("id")
    pin_code = data.get("code")
    if not pin_id or not pin_code:
        raise PlexAuthError("Plex PIN response missing 'id' or 'code'")

    params: dict[str, str] = {
        "clientID": clean_client_id,
        "code": pin_code,
        "context[device][product]": DEFAULT_PRODUCT_NAME,
    }
    if forward_url:
        f_url = str(forward_url).strip()
        parsed_f = urllib.parse.urlparse(f_url)
        q_params = urllib.parse.parse_qs(parsed_f.query)
        if "pin_id" not in q_params:
            q_params["pin_id"] = [str(pin_id)]
            new_query = urllib.parse.urlencode(q_params, doseq=True)
            f_url = urllib.parse.urlunparse((
                parsed_f.scheme,
                parsed_f.netloc,
                parsed_f.path,
                parsed_f.params,
                new_query,
                parsed_f.fragment,
            ))
        params["forwardUrl"] = f_url
    auth_params = urllib.parse.urlencode(params)
    auth_url = f"{PLEX_AUTH_APP_URL}?{auth_params}"

    return {
        "id": pin_id,
        "code": pin_code,
        "auth_url": auth_url,
    }


def check_plex_pin(
    pin_id: int, client_identifier: str = DEFAULT_CLIENT_IDENTIFIER
) -> Optional[str]:
    """Query Plex API for authentication token linked to the given PIN ID.

    Returns:
        authToken string if user has authorized the PIN, or None if still pending/expired/invalid.
    """
    try:
        clean_pin_id = int(pin_id)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid pin_id: {pin_id}") from e

    headers = {
        "Accept": "application/json",
        "X-Plex-Client-Identifier": _sanitize_header_value(client_identifier),
    }
    try:
        resp = requests.get(
            f"{PLEX_API_URL}/pins/{clean_pin_id}",
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as e:
        if getattr(e.response, "status_code", None) == 404:
            return None
        raise PlexAuthError(f"Error querying Plex PIN: {e}") from e
    except (requests.RequestException, ValueError) as e:
        raise PlexAuthError(f"Network error querying Plex PIN: {e}") from e

    token = data.get("authToken") or data.get("auth_token")
    return str(token) if token else None


def get_plex_user(
    auth_token: str, client_identifier: str = DEFAULT_CLIENT_IDENTIFIER
) -> dict[str, str]:
    """Retrieve user profile information (username, email, thumb, id) from Plex TV.

    Returns:
        {'id': ..., 'username': ..., 'email': ..., 'thumb': ...}
    """
    if not auth_token or not isinstance(auth_token, str):
        raise ValueError("auth_token must be a non-empty string")

    headers = {
        "Accept": "application/json",
        "X-Plex-Client-Identifier": _sanitize_header_value(client_identifier),
        "X-Plex-Token": _sanitize_header_value(auth_token),
    }
    try:
        resp = requests.get(f"{PLEX_API_URL}/user", headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise PlexAuthError(f"Failed to fetch Plex user info: {e}") from e

    return {
        "id": str(data.get("id", "")),
        "username": str(data.get("username", "") or ""),
        "email": str(data.get("email", "") or ""),
        "thumb": str(data.get("thumb", "") or ""),
    }


def verify_server_access(
    auth_token: str,
    target_machine_id: str,
    client_identifier: str = DEFAULT_CLIENT_IDENTIFIER,
) -> Tuple[bool, bool]:
    """Verify if the authenticated user has access to target_machine_id and whether they are owner.

    Queries https://plex.tv/api/v2/resources?includeHttps=1 with user's token.
    Checks if server with clientIdentifier == target_machine_id exists in resources.

    Returns:
        (has_access, is_owner): Tuple[bool, bool].
        If target_machine_id is not in resources or token is unauthorized, returns (False, False).
    """
    if not auth_token or not target_machine_id:
        return (False, False)

    try:
        headers = {
            "Accept": "application/json",
            "X-Plex-Client-Identifier": _sanitize_header_value(client_identifier),
            "X-Plex-Token": _sanitize_header_value(auth_token),
        }
        resp = requests.get(
            f"{PLEX_API_URL}/resources?includeHttps=1",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Plex resources check failed with HTTP %d", resp.status_code)
            return (False, False)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("Failed to query Plex resources: %s", e)
        return (False, False)

    resources: list[dict[str, Any]] = []
    if isinstance(data, list):
        resources = [r for r in data if isinstance(r, dict)]
    elif isinstance(data, dict):
        raw = data.get("resources", [])
        if isinstance(raw, list):
            resources = [r for r in raw if isinstance(r, dict)]

    target_id = str(target_machine_id).strip()
    for res in resources:
        res_id = str(res.get("clientIdentifier", "")).strip()
        if res_id == target_id:
            owned_val = res.get("owned")
            is_owner = (
                owned_val is True
                or owned_val == 1
                or str(owned_val).lower() in ("true", "1")
            )
            return (True, is_owner)

    return (False, False)


def get_or_create_secret_key(data_dir: str = "/data") -> bytes:
    """Read or generate 32-byte cryptographically secure random session secret key.

    Stored in {data_dir}/.session_secret with mode 0600.
    Enforces path traversal defense.
    """
    base = Path(data_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    if not os.access(base, os.W_OK):
        uid = os.getuid() if hasattr(os, "getuid") else "N/A"
        gid = os.getgid() if hasattr(os, "getgid") else "N/A"
        raise PermissionError(
            f"Data directory '{base}' is not writable (UID {uid}, GID {gid}). "
            f"Cannot create or access session secret key."
        )
    secret_path = safe_data_path(".session_secret", base_dir=str(base))

    # Check primary path and /config fallback for existing .session_secret
    candidate_paths: list[Path] = [secret_path]
    if os.path.isdir("/config"):
        candidate_paths.append(safe_data_path(".session_secret", base_dir="/config"))

    for cand in candidate_paths:
        if cand.exists():
            try:
                key = cand.read_bytes()
                if len(key) == 32:
                    try:
                        os.chmod(cand, 0o600)
                    except OSError:
                        pass
                    if cand != secret_path and not secret_path.exists():
                        try:
                            secret_path.write_bytes(key)
                            os.chmod(secret_path, 0o600)
                        except OSError:
                            pass
                    return key
            except OSError as e:
                logger.warning("Could not read existing session secret from %s: %s", cand, e)

    new_key = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(secret_path), flags, 0o600)
    try:
        with open(fd, "wb") as f:
            f.write(new_key)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise

    try:
        os.chmod(secret_path, 0o600)
    except OSError:
        pass

    return new_key


def create_session_token(
    user_id: str,
    username: str,
    is_admin: bool,
    secret_key: bytes,
    expires_in_seconds: int = 86400 * 7,
) -> str:
    """Create HMAC-SHA256 signed session token.

    Contains user_id, username, is_admin, and expiration timestamp.
    """
    if not isinstance(secret_key, (bytes, bytearray)) or len(secret_key) == 0:
        raise ValueError("secret_key must be non-empty bytes")

    now = int(time.time())
    payload = {
        "user_id": str(user_id),
        "username": str(username),
        "is_admin": bool(is_admin),
        "iat": now,
        "exp": now + expires_in_seconds,
        # Unique per issuance: two sessions minted in the same second must not share a token,
        # otherwise logging out / revoking one would silently act on the other.
        "jti": secrets.token_hex(8),
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_json).decode("ascii").rstrip("=")

    signature = hmac.new(secret_key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")

    return f"{payload_b64}.{sig_b64}"


def verify_session_token(token: str, secret_key: bytes) -> Optional[dict[str, Any]]:
    """Verify HMAC-SHA256 signed session token.

    Verifies signature and expiration, returns {'user_id': ..., 'username': ..., 'is_admin': ...} or None.
    """
    if not isinstance(token, str) or not token:
        return None
    if not isinstance(secret_key, (bytes, bytearray)) or len(secret_key) == 0:
        return None

    parts = token.split(".")
    if len(parts) != 2:
        return None

    payload_b64, sig_b64 = parts

    computed_sig = hmac.new(secret_key, payload_b64.encode("utf-8"), hashlib.sha256).digest()
    computed_sig_b64 = base64.urlsafe_b64encode(computed_sig).decode("ascii").rstrip("=")
    if not hmac.compare_digest(sig_b64.encode("utf-8"), computed_sig_b64.encode("utf-8")):
        return None

    try:
        payload_pad = (4 - len(payload_b64) % 4) % 4
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + "=" * payload_pad)
        data = json.loads(payload_bytes.decode("utf-8"))
    except (binascii.Error, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not isinstance(data, dict):
        return None

    exp = data.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    if time.time() > exp:
        return None

    if "user_id" not in data or "username" not in data or "is_admin" not in data:
        return None

    return {
        "user_id": str(data["user_id"]),
        "username": str(data["username"]),
        "is_admin": bool(data["is_admin"]),
    }

"""Security and input sanitization utilities for trackseerr."""

import ipaddress
import logging
import os
import re
import socket
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Optional, Sequence

# Strict regex matching for 22-char alphanumeric Spotify ID
_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_SPOTIFY_URI_RE = re.compile(r"^spotify:playlist:([A-Za-z0-9]{22})$")
_SPOTIFY_PATH_RE = re.compile(
    r"^(?:/intl-[a-z]{2}(?:-[a-z]{2})?)?(?:/user/[A-Za-z0-9_.-]+)?/playlist/([A-Za-z0-9]{22})/?$"
)

# Strict regex matching for 5-15 digit Deezer ID
_DEEZER_ID_RE = re.compile(r"^[0-9]{5,15}$")
_DEEZER_PATH_RE = re.compile(
    r"^(?:/[a-zA-Z]{2}(?:-[a-zA-Z]{2})?)?/playlist/([0-9]{5,15})/?$"
)

_HTML_TAG_RE = re.compile(r"<[^>]*>", flags=re.DOTALL)


def extract_spotify_id(input_str: Optional[str]) -> Optional[str]:
    """Strictly validate and extract a 22-character Spotify playlist ID.

    Accepts:
    - Raw 22-character alphanumeric Spotify ID
    - Spotify playlist URI (spotify:playlist:...)
    - Spotify playlist URL (https://open.spotify.com/playlist/...)

    Rejects all invalid or malicious URLs to prevent SSRF.
    """
    if not isinstance(input_str, str):
        return None
    val = input_str.strip()
    if not val:
        return None

    # 1. Raw ID match
    if _SPOTIFY_ID_RE.fullmatch(val):
        return val

    # 2. Spotify URI match
    uri_match = _SPOTIFY_URI_RE.fullmatch(val)
    if uri_match:
        return uri_match.group(1)

    # 3. Spotify URL match with SSRF prevention
    try:
        parsed = urllib.parse.urlparse(val)
    except ValueError:
        return None

    if parsed.scheme in ("http", "https"):
        # Enforce exact hostname, prevent userinfo bypass or non-standard ports
        if (
            parsed.hostname == "open.spotify.com"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 80, 443)
        ):
            path_match = _SPOTIFY_PATH_RE.fullmatch(parsed.path)
            if path_match:
                return path_match.group(1)

    return None


def extract_deezer_id(input_str: Optional[str]) -> Optional[str]:
    """Strictly validate and extract a 5-15 digit Deezer playlist ID.

    Accepts:
    - Raw 5-15 digit numeric ID
    - Deezer playlist URL (https://www.deezer.com/.../playlist/...)

    Rejects all invalid or malicious URLs to prevent SSRF.
    """
    if not isinstance(input_str, str):
        return None
    val = input_str.strip()
    if not val:
        return None

    # 1. Raw numeric ID match
    if _DEEZER_ID_RE.fullmatch(val):
        return val

    # 2. Deezer URL match with SSRF prevention
    try:
        parsed = urllib.parse.urlparse(val)
    except ValueError:
        return None

    if parsed.scheme in ("http", "https"):
        # Enforce exact hostname, prevent userinfo bypass or non-standard ports
        if (
            parsed.hostname in ("deezer.com", "www.deezer.com")
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 80, 443)
        ):
            path_match = _DEEZER_PATH_RE.fullmatch(parsed.path)
            if path_match:
                return path_match.group(1)

    return None


def sanitize_text(text: Optional[str]) -> str:
    """Strips HTML tags and control characters from text."""
    if not isinstance(text, str):
        return ""
    # Strip HTML tags
    cleaned = _HTML_TAG_RE.sub("", text)
    # Strip control characters (Unicode category C: Cc, Cf, Cs, Co, Cn)
    return "".join(ch for ch in cleaned if unicodedata.category(ch)[0] != "C")


def safe_data_path(filename: str, base_dir: str = "/data") -> Path:
    """Validate that filename resides strictly inside base_dir.

    Strips dangerous characters, resolves relative segments,
    and asserts that the final resolved path is strictly within base_dir.
    Raises ValueError on path traversal attempts or invalid targets.
    """
    if not isinstance(filename, str):
        raise ValueError("Filename must be a string")

    # Strip control characters and whitespace
    cleaned = "".join(ch for ch in filename if unicodedata.category(ch)[0] != "C").strip()
    if not cleaned:
        raise ValueError("Filename is empty or invalid")

    base = Path(base_dir).resolve()
    if os.path.isabs(cleaned):
        target = Path(cleaned).resolve()
    else:
        target = (base / cleaned).resolve()

    if not target.is_relative_to(base) or target == base:
        raise ValueError(f"Path traversal detected or invalid target: '{filename}'")

    return target


def mask_secret(secret: Optional[str], visible_chars: int = 4) -> str:
    """Return masked string revealing only the trailing visible_chars (e.g. ••••••••••••abcd)."""
    if not secret:
        return ""
    mask_char = "•"
    if visible_chars <= 0:
        return mask_char * len(secret)
    if len(secret) <= visible_chars:
        return mask_char * len(secret)
    return (mask_char * (len(secret) - visible_chars)) + secret[-visible_chars:]


def _mask_url_credentials(url: str) -> str:
    """Keep scheme://host[:port] visible and fully mask userinfo, path and query (tokens live there).

    No trailing characters are revealed. The output is deterministic, so a client echoing it back
    can be recognised by exact comparison with ``_mask_url_credentials(stored)``.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return mask_secret(url, visible_chars=0)
    if not parts.scheme or not host:
        return mask_secret(url, visible_chars=0)
    if ":" in host:  # IPv6 literal: urlsplit strips the brackets
        host = f"[{host}]"
    base = f"{parts.scheme}://{host}{port}"
    rest = url.split("://", 1)[1][len(parts.netloc) :]
    has_userinfo = bool(parts.username or parts.password)
    if has_userinfo:
        return f"{base}/{mask_secret(rest.lstrip('/') or 'xxx', visible_chars=0)}"
    if rest in ("", "/"):
        return base + rest
    return base + mask_secret(rest, visible_chars=0)


MASK_MARKER = "•••"


def resolve_masked_value(submitted: Any, stored: Any, masked_stored: Any, field: str = "value") -> Any:
    """Decide what to persist for a field a client echoed back.

    * ``submitted`` exactly equals the masked form of ``stored`` -> the client did not touch it; keep ``stored``.
    * Otherwise the submitted value is taken, unless it still contains the mask marker: that is a
      half-edited placeholder, rejected with ``ValueError`` rather than silently stored or reverted.
    """
    if not isinstance(submitted, str):
        return submitted
    if isinstance(stored, str) and stored and submitted == masked_stored:
        return stored
    if MASK_MARKER in submitted:
        raise ValueError(
            f"'{field}' contains a masked placeholder that does not match the stored value; "
            "enter the full value or leave it unchanged"
        )
    return submitted


def mask_channel_config(channel_type: str, config: dict[str, Any]) -> dict[str, Any]:  # noqa: C901
    """Masks sensitive credentials in notification channel configuration."""
    if not isinstance(config, dict):
        return {}
    c = dict(config)
    ctype = (channel_type or "").lower().strip()
    if ctype == "discord":
        url = c.get("webhook_url")
        if url and isinstance(url, str):
            c["webhook_url"] = _mask_url_credentials(url)
    elif ctype == "telegram":
        if c.get("bot_token"):
            c["bot_token"] = mask_secret(str(c["bot_token"]))
    elif ctype == "pushover":
        if c.get("user_key"):
            c["user_key"] = mask_secret(str(c["user_key"]))
        if c.get("app_token"):
            c["app_token"] = mask_secret(str(c["app_token"]))
        if c.get("token"):
            c["token"] = mask_secret(str(c["token"]))
    elif ctype == "webhook":
        for url_key in ("webhook_url", "url"):
            wurl = c.get(url_key)
            if wurl and isinstance(wurl, str):
                c[url_key] = _mask_url_credentials(wurl)
        if c.get("secret_header"):
            c["secret_header"] = mask_secret(str(c["secret_header"]))
        if c.get("secret"):
            c["secret"] = mask_secret(str(c["secret"]))
        if c.get("token"):
            c["token"] = mask_secret(str(c["token"]))
    elif ctype == "email":
        if c.get("password"):
            c["password"] = mask_secret(str(c["password"]))
    return c


_ALLOWED_IMAGE_HOSTS = {
    "i.scdn.co",
    "mosaic.scdn.co",
    "image-cdn-ak.spotifycdn.com",
    "image-cdn-fa.spotifycdn.com",
    "blend-playlist-covers.spotifycdn.com",
    "wrapped-images.spotifycdn.com",
    "e-cdns-images.dzcdn.net",
    "cdns-images.dzcdn.net",
    "coverartarchive.org",
    "archive.org",
}

_ALLOWED_IMAGE_SUFFIXES = (
    ".scdn.co",
    ".spotifycdn.com",
    ".dzcdn.net",
    ".coverartarchive.org",
    ".archive.org",
)


def is_safe_image_url(url: Optional[str]) -> bool:
    """Validates that an image URL is strictly HTTPS and originates from a trusted CDN or public domain.

    Rejects private, loopback, link-local, or cloud metadata IP addresses to prevent SSRF.
    """
    import ipaddress

    if not isinstance(url, str):
        return False
    val = url.strip()
    if not val:
        return False

    try:
        parsed = urllib.parse.urlparse(val)
    except ValueError:
        return False

    if parsed.scheme != "https":
        return False

    if not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        return False

    hostname = parsed.hostname.lower()

    # Reject localhost or internal domain names (.local, .internal, .lan, etc.)
    if hostname in ("localhost", "127.0.0.1", "::1") or hostname.endswith((".local", ".internal", ".lan", ".home")):
        return False

    # Check if hostname is an IP address
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
        return True
    except ValueError:
        # Not a raw IP, it's a domain name
        pass

    if hostname in _ALLOWED_IMAGE_HOSTS:
        return True

    for suffix in _ALLOWED_IMAGE_SUFFIXES:
        if hostname.endswith(suffix):
            return True

    if "." in hostname and not hostname.startswith(".") and not hostname.endswith("."):
        return True

    return False


_HEX_HOST_RE = re.compile(r"^0x[0-9a-fA-F]+$")
_OCTAL_HOST_RE = re.compile(r"^0[0-7]+$")
_INTEGER_HOST_RE = re.compile(r"^\d+$")
_AWS_IMDSV6_NET = ipaddress.ip_network("fd00:ec2::/64")
_AWS_IMDSV6_IP = ipaddress.ip_address("fd00:ec2::254")
_LINK_LOCAL_IPV4_NET = ipaddress.ip_network("169.254.0.0/16")
_ALIBABA_METADATA_IP = ipaddress.ip_address("100.100.100.200")  # inside CGNAT space, so not caught by is_link_local
_NUMERIC_DOTTED_RE = re.compile(r"^[0-9.]+$")


def _resolve_host(hostname: str) -> list[str]:
    """Every address ``hostname`` resolves to; empty when it does not resolve (e.g. a container that is not up yet)."""
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    return list(dict.fromkeys(str(info[4][0]).split("%", 1)[0] for info in infos))


def _is_forbidden_address(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """Addresses a service URL may never point at, whatever the LAN setting: unspecified, link-local, multicast,
    reserved, the cloud metadata endpoints (AWS IMDS v4/v6, Alibaba)."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_unspecified or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return True
    if ip in _LINK_LOCAL_IPV4_NET or ip == _ALIBABA_METADATA_IP:
        return True
    return isinstance(ip, ipaddress.IPv6Address) and (ip in _AWS_IMDSV6_NET or ip == _AWS_IMDSV6_IP)


def is_safe_service_url(url: Optional[str], allow_lan: bool = True) -> bool:  # noqa: C901
    """Validates that a service host URL (download client or indexer) is safe against SSRF.

    Accepts HTTP and HTTPS schemes on homelab LAN IPs (private, loopback if allow_lan=True, docker container names)
    and valid public domains.
    Strictly blocks:
    - Dangerous schemes (file://, ftp://, gopher://, etc.)
    - Link-local and cloud metadata addresses (169.254.169.254, 169.254.0.0/16, fd00:ec2::254)
    - Unspecified IP addresses (0.0.0.0, ::)
    - Loopback IP addresses unless allow_lan=True
    - Integer, hex, octal and shorthand (``127.1``) numeric IP representations
    - Hostnames that RESOLVE to a loopback, link-local, metadata (incl. 169.254.169.254 and 100.100.100.200) or
      unspecified address. RFC1918 ranges stay allowed (homelab LANs). A name that does not resolve is allowed (a
      container that is not up yet); the connection is made by the HTTP client later, so this is a configuration-time
      check, not protection against DNS that changes between the check and the request.
    - Cloud metadata hostnames (metadata.google.internal, instance-data)
    - Userinfo URL components (embedded username/password)
    - Malformed or illegal hostname characters
    """
    if not isinstance(url, str):
        return False
    val = url.strip()
    if not val:
        return False

    try:
        parsed = urllib.parse.urlparse(val)
    except ValueError:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    if not parsed.hostname or parsed.username or parsed.password:
        return False

    hostname = parsed.hostname.lower()

    # Reject empty hostname or integer, hex, and octal representations
    if _HEX_HOST_RE.fullmatch(hostname) or _OCTAL_HOST_RE.fullmatch(hostname) or _INTEGER_HOST_RE.fullmatch(hostname):
        return False

    # Also reject dotted-decimal strings with hex/octal components
    parts = hostname.split(".")
    for part in parts:
        if _HEX_HOST_RE.fullmatch(part) or (len(part) > 1 and _OCTAL_HOST_RE.fullmatch(part)):
            return False

    # Reject cloud metadata hostnames
    if hostname in ("metadata.google.internal", "instance-data", "metadata") or hostname.endswith(
        ("metadata.google.internal", "instance-data")
    ):
        return False

    # Check IP addresses
    try:
        ip = ipaddress.ip_address(hostname)
        if _is_forbidden_address(ip):
            return False
        if (ip.is_loopback or ip.is_private) and not allow_lan:
            return False
        return True
    except ValueError:
        pass

    # Digits and dots that are not a canonical dotted quad (``127.1``, ``10.1``, ``1.2.3``) are shorthand an HTTP
    # client may expand to a different address than a validator reads: refuse them outright.
    if _NUMERIC_DOTTED_RE.fullmatch(hostname):
        return False

    # Hostname syntax validation
    if hostname == "localhost":
        return allow_lan

    if not re.fullmatch(r"^[a-z0-9][a-z0-9_\.-]*[a-z0-9]$", hostname):
        return False

    # A name is only as safe as what it resolves to: refuse one that points at loopback, link-local or a metadata
    # address (every record counts, not only the first).
    for address in _resolve_host(hostname):
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError:
            return False
        if _is_forbidden_address(resolved) or resolved.is_loopback:
            return False
        if resolved.is_private and not allow_lan:
            return False

    return True


def sanitize_csv_cell(val: Any) -> str:
    """Sanitizes CSV cell to prevent formula injection attacks.

    Prefixes leading dangerous characters (=, +, -, @, tab, CR, |) with a single quote,
    even when preceded by whitespace.
    """
    text = str(val if val is not None else "")
    if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "|")):
        return f"'{text}"
    return text




# --------------------------------------------------------------------------- Plex sign-in forwardUrl

_LOG = logging.getLogger(__name__)
_DEFAULT_PORTS = {"http": 80, "https": 443}
_FORBIDDEN_URL_CHARS = re.compile(r"[\x00-\x20\x7f-\x9f\\]")


def _normalize_origin(url: str) -> Optional[tuple[str, str, int]]:
    """(scheme, idna-lowercase host without trailing dot, effective port) of an http(s) URL, else None."""
    try:
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in _DEFAULT_PORTS or parts.username is not None or parts.password is not None:
            return None
        host = (parts.hostname or "").rstrip(".").lower()
        if not host:
            return None
        port = parts.port or _DEFAULT_PORTS[scheme]
        host = host.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return None
    return scheme, host, port


def _log_forward_reject(candidate: str, reason: str) -> None:
    try:
        host = (urllib.parse.urlsplit(candidate).hostname or "")[:64]
    except ValueError:
        host = ""
    _LOG.warning("Rejected Plex forward_url (%s, host=%r)", reason, host)


def safe_forward_url(
    candidate: Optional[str],
    *,
    allowed_origins: Sequence[Optional[str]],
) -> Optional[str]:
    """Return ``candidate`` only if it points at one of ``allowed_origins``, else None (open-redirect guard).

    Callers pass the union of the configured APPLICATION_URL and the request's own (trusted-proxy
    aware) origin, so a user on a secondary hostname (e.g. a Tailscale name) is not stranded on
    plex.tv. The request-origin path already applied when APPLICATION_URL was unset, so the union
    does not weaken that case. Residual limitation: a client calling the endpoint directly with a
    spoofed Host header can obtain a forwardUrl for that host; this is defense-in-depth only, as the
    PIN flow never exposes the token through the redirect.

    A relative path (single leading ``/``) is resolved against the first allowed origin (callers list
    the request origin first). Absolute URLs must be http(s), carry no userinfo, and match scheme,
    host and port of an allowed origin after normalisation.
    """
    if not candidate or not isinstance(candidate, str):
        return None
    raw = candidate.strip()
    if not raw:
        return None
    sources = [str(o).strip() for o in allowed_origins if o and str(o).strip()]
    allowed = [(src, n) for src in sources if (n := _normalize_origin(src)) is not None]
    if not allowed:
        _log_forward_reject(raw, "no usable allowed origin")
        return None
    if _FORBIDDEN_URL_CHARS.search(raw):
        _log_forward_reject(raw, "control character, whitespace or backslash")
        return None
    if raw.startswith("/"):
        if raw.startswith("//"):
            _log_forward_reject(raw, "protocol-relative URL")
            return None
        base = allowed[0][0].split("?", 1)[0].split("#", 1)[0].rstrip("/")
        return base + raw
    got = _normalize_origin(raw)
    if got is None:
        _log_forward_reject(raw, "unsupported scheme, userinfo or malformed")
        return None
    if all(got != n for _, n in allowed):
        _log_forward_reject(raw, "origin mismatch")
        return None
    return raw

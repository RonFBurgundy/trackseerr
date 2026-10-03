"""Local (non-Plex) account primitives: password hashing, policy, TOTP, recovery codes, tokens.

Stdlib only. Nothing in this module logs a password, code, token, secret or hash, and every
comparison of secret material uses ``hmac.compare_digest`` on bytes. See
``docs/users-and-accounts.md`` for the contract.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import re
import secrets
import struct
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

# --------------------------------------------------------------------------- password hashing

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16
SCRYPT_MAXMEM = 64 * 1024 * 1024
_SCRYPT_PREFIX = "scrypt"
# Parameters read back from storage are bounded so a tampered row cannot demand unbounded memory.
_MAX_STORED_N = 2**16
_MAX_STORED_R = 16
_MAX_STORED_P = 4

# Bounds concurrent scrypt evaluations (each needs ~32 MiB) so a login flood cannot exhaust memory.
_SCRYPT_SLOTS = threading.BoundedSemaphore(4)

PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)


def _scrypt(password: bytes, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    with _SCRYPT_SLOTS:
        return hashlib.scrypt(password, salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=SCRYPT_MAXMEM)


def hash_password(password: str) -> str:
    """Returns ``scrypt$N$r$p$<salt_b64>$<hash_b64>`` for ``password`` with a fresh 16-byte salt."""
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    digest = _scrypt(password.encode("utf-8"), salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)
    return f"{_SCRYPT_PREFIX}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def _parse_hash(stored: str) -> Optional[tuple[int, int, int, bytes, bytes]]:
    if not isinstance(stored, str):
        return None
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != _SCRYPT_PREFIX:
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, digest = _b64d(parts[4]), _b64d(parts[5])
    except (ValueError, binascii.Error):
        return None
    if n < 2 or n & (n - 1) or n > _MAX_STORED_N or not (1 <= r <= _MAX_STORED_R) or not (1 <= p <= _MAX_STORED_P):
        return None
    if not salt or not digest or len(digest) > 128:
        return None
    return n, r, p, salt, digest


def verify_password(password: str, stored: Optional[str]) -> bool:
    """Constant-time check of ``password`` against ``stored``. Malformed hashes never verify."""
    parsed = _parse_hash(stored) if stored else None
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    try:
        actual = _scrypt(password.encode("utf-8"), salt, n, r, p, len(expected))
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    """True when ``stored`` was produced with parameters other than the current ones."""
    parsed = _parse_hash(stored)
    if parsed is None:
        return True
    n, r, p, _salt, digest = parsed
    return (n, r, p, len(digest)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)


_dummy_lock = threading.Lock()
_dummy_hash: Optional[str] = None


def _get_dummy_hash() -> str:
    global _dummy_hash
    with _dummy_lock:
        if _dummy_hash is None:
            _dummy_hash = hash_password(secrets.token_urlsafe(24))
        return _dummy_hash


def equalize_timing(password: str) -> None:
    """Runs one scrypt verification so unknown-user logins cost the same as known-user logins."""
    verify_password(password, _get_dummy_hash())


# --------------------------------------------------------------------------- password policy

_COMMON_PASSWORDS_PATH = Path(__file__).resolve().parent / "data" / "common_passwords.txt"


@lru_cache(maxsize=1)
def common_passwords() -> frozenset[str]:
    """The bundled list of common passwords (lowercase, one per line)."""
    try:
        text = _COMMON_PASSWORDS_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        # Fail closed: an unreadable list must not silently weaken the policy.
        raise RuntimeError(f"Common-password list is unreadable: {exc}") from exc
    return frozenset(line.strip().lower() for line in text.splitlines() if line.strip())


def validate_password(password: object, username: str = "") -> Optional[str]:
    """Returns a human-readable policy violation, or None when ``password`` is acceptable."""
    if not isinstance(password, str):
        return "Password is required"
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters"
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"Password must be at most {PASSWORD_MAX_LENGTH} characters"
    uname = (username or "").strip().lower()
    if uname and uname in password.lower():
        return "Password must not contain your username"
    if password.lower() in common_passwords():
        return "That password is too common"
    return None


# --------------------------------------------------------------------------- usernames

USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
RESERVED_USERNAMES = frozenset({"api_key", "gateway_service", "feed_subscriber", "internal_gateway", "api-key"})


def normalize_username(username: object) -> str:
    return str(username or "").strip().lower()


def validate_username(username: str) -> Optional[str]:
    if not USERNAME_RE.match(username):
        return "Username must be 3-32 characters of a-z, 0-9, '.', '_' or '-'"
    if username in RESERVED_USERNAMES:
        return "That username is reserved"
    return None


# --------------------------------------------------------------------------- TOTP (RFC 6238)

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_SECRET_BYTES = 20
TOTP_WINDOW = 1


def generate_totp_secret() -> str:
    """A random 20-byte secret as unpadded base32 (32 characters)."""
    return base64.b32encode(secrets.token_bytes(TOTP_SECRET_BYTES)).decode("ascii").rstrip("=")


def _decode_secret(secret_b32: str) -> bytes:
    cleaned = secret_b32.strip().replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8), casefold=True)


def hotp(key: bytes, counter: int, digits: int = TOTP_DIGITS) -> str:
    """RFC 4226 HOTP with HMAC-SHA1."""
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def totp_counter(for_time: Optional[float] = None) -> int:
    return int((time.time() if for_time is None else for_time) // TOTP_STEP_SECONDS)


def totp_code(secret_b32: str, for_time: Optional[float] = None, digits: int = TOTP_DIGITS) -> str:
    return hotp(_decode_secret(secret_b32), totp_counter(for_time), digits)


def normalize_code(code: object) -> str:
    return re.sub(r"[\s-]", "", str(code or ""))


def verify_totp(
    secret_b32: str,
    code: object,
    last_counter: Optional[int] = None,
    *,
    for_time: Optional[float] = None,
    window: int = TOTP_WINDOW,
) -> Optional[int]:
    """Returns the matched time-step counter, or None.

    Accepts codes within +-``window`` steps. A counter that is not strictly greater than
    ``last_counter`` is rejected, so a code can be used only once. All candidate counters are
    evaluated regardless of an early match so timing does not reveal which step matched.
    """
    candidate = normalize_code(code)
    if len(candidate) != TOTP_DIGITS or not candidate.isascii() or not candidate.isdigit():
        return None
    try:
        key = _decode_secret(secret_b32)
    except (ValueError, binascii.Error):
        return None
    now_counter = totp_counter(for_time)
    matched: Optional[int] = None
    cand_bytes = candidate.encode("ascii")
    for step in range(-window, window + 1):
        counter = now_counter + step
        if counter < 0:
            continue
        if hmac.compare_digest(hotp(key, counter).encode("ascii"), cand_bytes) and matched is None:
            matched = counter
    if matched is None:
        return None
    if last_counter is not None and matched <= int(last_counter):
        return None
    return matched


def otpauth_uri(secret_b32: str, username: str, issuer: str = "TrackSeerr") -> str:
    from urllib.parse import quote

    label = quote(f"{issuer}:{username}", safe="")
    return (
        f"otpauth://totp/{label}?secret={secret_b32}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}"
    )


# --------------------------------------------------------------------------- recovery codes

RECOVERY_CODE_COUNT = 10
# 32 symbols, no 'i', 'l', 'o' or 'u' (Crockford-style) so codes survive being read aloud.
RECOVERY_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    codes: set[str] = set()
    while len(codes) < count:
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(12))
        codes.add(f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}")
    return sorted(codes, key=lambda _c: secrets.randbits(32))


def normalize_recovery_code(code: object) -> Optional[str]:
    """Canonical ``xxxx-xxxx-xxxx`` form, or None when ``code`` cannot be a recovery code."""
    raw = re.sub(r"[\s-]", "", str(code or "")).lower()
    if len(raw) != 12 or any(ch not in RECOVERY_ALPHABET for ch in raw):
        return None
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


# --------------------------------------------------------------------------- invite / reset tokens

TOKEN_TTL_SECONDS = 48 * 3600
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def token_is_well_formed(raw: object) -> bool:
    return isinstance(raw, str) and bool(_TOKEN_RE.match(raw))


# --------------------------------------------------------------------------- client IP / TRUSTED_PROXIES

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


def parse_trusted_proxies(value: Optional[str]) -> tuple[Network, ...]:
    """Parses a comma-separated CIDR/IP list. Invalid entries are skipped (never widened)."""
    networks: list[Network] = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _parse_ip(value: str) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_trusted(addr: Union[ipaddress.IPv4Address, ipaddress.IPv6Address], trusted: Iterable[Network]) -> bool:
    return any(addr.version == net.version and addr in net for net in trusted)


def resolve_client_ip(
    peer: Optional[str],
    forwarded_for: Optional[str],
    trusted: Sequence[Network],
) -> str:
    """The client address used for throttling.

    ``X-Forwarded-For`` is honoured only when the direct peer is a trusted proxy, and is walked
    right to left so a client cannot spoof an address by prepending entries.
    """
    peer_addr = _parse_ip(peer or "")
    peer_text = str(peer_addr) if peer_addr is not None else (peer or "unknown")
    if peer_addr is None or not trusted or not forwarded_for or not _is_trusted(peer_addr, trusted):
        return peer_text
    hops = [h for h in (x.strip() for x in forwarded_for.split(",")) if h]
    for hop in reversed(hops):
        addr = _parse_ip(hop)
        if addr is None:
            # An unparseable hop means the chain is not trustworthy beyond this point.
            return peer_text
        if not _is_trusted(addr, trusted):
            return str(addr)
    return str(_parse_ip(hops[0])) if hops and _parse_ip(hops[0]) is not None else peer_text

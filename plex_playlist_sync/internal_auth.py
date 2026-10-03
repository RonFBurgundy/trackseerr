"""Signed user assertions for gateway-to-core calls (two-tier / DMZ model).

The gateway signs every call to core with an HMAC-SHA256 over the request line,
the asserted user identity, a timestamp, a single-use nonce and the body hash.
Core verifies the signature, the clock window and nonce freshness. See
``docs/two-tier-security.md`` for the contract. This module is the only place
that signs or verifies; it never logs the secret or a signature.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import quote, unquote

HEADER_USER_ID = "X-TS-User-Id"
HEADER_USER_NAME = "X-TS-User-Name"
HEADER_TIMESTAMP = "X-TS-Timestamp"
HEADER_NONCE = "X-TS-Nonce"
HEADER_SIGNATURE = "X-TS-Signature"
HEADER_SESSION_ISSUED_AT = "X-TS-Session-Issued-At"

MAX_CLOCK_SKEW_SECONDS = 60
NONCE_TTL_SECONDS = 120
MIN_SECRET_LENGTH = 32

logger = logging.getLogger(__name__)


class InvalidAssertion(Exception):
    """Raised when a signed assertion fails verification (maps to HTTP 401)."""


@dataclass(frozen=True)
class VerifiedAssertion:
    """The identity asserted by a verified gateway call. ``user_id`` is "" for a service call."""

    user_id: str
    user_name: str
    session_issued_at: Optional[int] = None  # epoch microseconds the gateway session was created; None if absent


def body_sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def encode_user_name(user_name: str) -> str:
    """Header-safe form of a username (ASCII usernames without '%' pass through unchanged)."""
    return quote(user_name or "", safe="")


def canonical_string(
    method: str,
    target: str,
    user_id: str,
    user_name: str,
    timestamp: str,
    nonce: str,
    body_sha256: str,
    session_issued_at: str = "",
) -> str:
    """METHOD \\n PATH?QUERY \\n user_id \\n user_name \\n timestamp \\n nonce \\n sha256_hex(body) \\n session_issued_at.

    ``session_issued_at`` is the gateway session's creation time in epoch microseconds ("" when
    there is no session, e.g. a service call). It is signed so core can enforce revocation.
    """
    return "\n".join(
        [method.upper(), target, user_id, user_name, timestamp, nonce, body_sha256, session_issued_at]
    )


def _compute(secret: str, canonical: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _reject_control(value: str, label: str) -> None:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{label} must not contain line breaks")


def sign_assertion(
    secret: str,
    method: str,
    target: str,
    user_id: str = "",
    user_name: str = "",
    body: bytes = b"",
    *,
    timestamp: Optional[int] = None,
    nonce: Optional[str] = None,
    session_issued_at: Optional[int] = None,
) -> dict[str, str]:
    """Returns the X-TS-* headers authenticating one request. ``target`` is path plus ``?query``."""
    if not secret:
        raise ValueError("INTERNAL_CORE_SECRET is required to sign gateway calls")
    if not validate_secret_strength(secret):
        logger.error("Refusing to sign: INTERNAL_CORE_SECRET is shorter than %d characters", MIN_SECRET_LENGTH)
        raise ValueError(f"INTERNAL_CORE_SECRET must be at least {MIN_SECRET_LENGTH} characters")
    user_id = str(user_id or "")
    wire_name = encode_user_name(str(user_name or ""))
    _reject_control(user_id, "user_id")
    _reject_control(target, "target")
    ts = str(int(time.time()) if timestamp is None else int(timestamp))
    nonce_val = nonce or secrets.token_hex(16)
    issued = "" if session_issued_at is None else str(int(session_issued_at))
    canonical = canonical_string(method, target, user_id, wire_name, ts, nonce_val, body_sha256_hex(body), issued)
    return {
        HEADER_USER_ID: user_id,
        HEADER_USER_NAME: wire_name,
        HEADER_TIMESTAMP: ts,
        HEADER_NONCE: nonce_val,
        HEADER_SIGNATURE: _compute(secret, canonical),
        HEADER_SESSION_ISSUED_AT: issued,
    }


class NonceCache:
    """Thread-safe in-memory single-use nonce set with a TTL."""

    def __init__(self, ttl_seconds: int = NONCE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def check_and_add(self, nonce: str, now: Optional[float] = None) -> bool:
        """Records ``nonce``; returns False if it was already seen within the TTL (a replay)."""
        current = time.time() if now is None else now
        with self._lock:
            expired = [n for n, exp in self._seen.items() if exp <= current]
            for n in expired:
                del self._seen[n]
            if nonce in self._seen:
                return False
            self._seen[nonce] = current + self._ttl
            return True

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


_nonce_cache = NonceCache()


def verify_assertion(
    secret: Optional[str],
    method: str,
    target: str,
    headers: Mapping[str, str],
    body_sha256: str,
    *,
    nonce_cache: Optional[NonceCache] = None,
    nonce_store: Any = None,
    now: Optional[float] = None,
) -> VerifiedAssertion:
    """Verifies the X-TS-* headers; raises InvalidAssertion on any failure.

    The signature is checked before the nonce is recorded so unauthenticated callers
    cannot fill the replay cache.
    """
    if not secret:
        raise InvalidAssertion("core has no internal secret configured")
    if not validate_secret_strength(secret):
        logger.error("Refusing to verify: INTERNAL_CORE_SECRET is shorter than %d characters", MIN_SECRET_LENGTH)
        raise InvalidAssertion("internal secret is too weak")

    user_id = headers.get(HEADER_USER_ID, "")
    wire_name = headers.get(HEADER_USER_NAME, "")
    ts_raw = headers.get(HEADER_TIMESTAMP, "")
    nonce = headers.get(HEADER_NONCE, "")
    signature = headers.get(HEADER_SIGNATURE, "")
    if not (ts_raw and nonce and signature):
        raise InvalidAssertion("incomplete assertion headers")

    try:
        ts = int(ts_raw)
    except ValueError as exc:
        raise InvalidAssertion("malformed timestamp") from exc
    current = time.time() if now is None else now
    if abs(current - ts) > MAX_CLOCK_SKEW_SECONDS:
        raise InvalidAssertion("timestamp outside the allowed window")

    if "\n" in user_id or "\r" in user_id or "\n" in target or "\r" in target:
        raise InvalidAssertion("illegal characters in assertion")

    issued_raw = headers.get(HEADER_SESSION_ISSUED_AT, "")
    expected = _compute(
        secret, canonical_string(method, target, user_id, wire_name, ts_raw, nonce, body_sha256, issued_raw)
    )
    if not hmac.compare_digest(expected.encode("ascii"), signature.strip().lower().encode("utf-8", "replace")):
        raise InvalidAssertion("signature mismatch")

    # In-memory front cache (cheap fast path), then the durable store, which is authoritative.
    cache = nonce_cache if nonce_cache is not None else _nonce_cache
    if not cache.check_and_add(nonce, current):
        raise InvalidAssertion("nonce replayed")
    if nonce_store is not None:
        try:
            fresh = nonce_store.record_nonce(nonce, int(current), NONCE_TTL_SECONDS)
        except Exception as exc:  # fail closed: an unverifiable nonce is treated as a replay
            logger.error("Nonce store failure, rejecting assertion: %s", exc)
            raise InvalidAssertion("nonce store unavailable") from exc
        if not fresh:
            raise InvalidAssertion("nonce replayed")

    issued_at: Optional[int] = None
    if issued_raw:
        if not (issued_raw.isascii() and issued_raw.isdigit()) or len(issued_raw) > 20:
            raise InvalidAssertion("malformed session issue time")
        issued_at = int(issued_raw)
    return VerifiedAssertion(user_id=user_id, user_name=unquote(wire_name), session_issued_at=issued_at)


def validate_secret_strength(secret: Optional[str]) -> bool:
    """True when the secret is present and at least MIN_SECRET_LENGTH characters."""
    return bool(secret) and len(str(secret)) >= MIN_SECRET_LENGTH

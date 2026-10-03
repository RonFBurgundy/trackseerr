"""Server-side verification of a local-account login (core and all-in-one).

One function, :func:`verify_local_login`, applies the contract's throttling, lockout, password
and MFA rules and raises :class:`LoginError` carrying the exact HTTP status and detail string.
Nothing here logs a username, password, code or hash.
"""

from __future__ import annotations

import threading
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from plex_playlist_sync import local_auth
from plex_playlist_sync.storage import Database

WINDOW_SECONDS = 15 * 60
USER_FAILURE_LIMIT = 5
IP_FAILURE_LIMIT = 20
IP_INFLIGHT_LIMIT = 4
_LOCK_STRIPES = 256

DETAIL_INVALID = "Invalid username or password"
DETAIL_MFA_REQUIRED = "mfa_required"
DETAIL_INVALID_CODE = "Invalid authentication code"
DETAIL_LOCKED = "Account temporarily locked"
DETAIL_THROTTLED = "Too many attempts. Please try again later."

PUBLIC_LOGIN_DETAILS = frozenset(
    {DETAIL_INVALID, DETAIL_MFA_REQUIRED, DETAIL_INVALID_CODE, DETAIL_LOCKED, DETAIL_THROTTLED}
)


class LoginError(Exception):
    """A login refusal: ``status_code`` and a fixed, non-revealing ``detail``."""

    def __init__(self, status_code: int, detail: str, retry_after: Optional[int] = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after


@dataclass
class LoginResult:
    user_id: str
    username: str
    mfa_enrollment_required: bool
    session_floor_us: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


_stripes = [threading.Lock() for _ in range(_LOCK_STRIPES)]
_inflight: dict[str, int] = {}
_inflight_guard = threading.Lock()


@contextmanager
def serialized_attempt(name: str) -> Iterator[None]:
    """Serializes check -> verify -> record for one subject (a normalized username or account id).

    Striped (bounded memory): two subjects may share a stripe, which only adds queueing, never skips it.
    """
    lock = _stripes[zlib.crc32(name.encode("utf-8")) % _LOCK_STRIPES]
    with lock:
        yield


@contextmanager
def inflight_slot(client_ip: str) -> Iterator[None]:
    """Admits at most ``IP_INFLIGHT_LIMIT`` concurrent attempts per IP; the next one gets 429 at once."""
    with _inflight_guard:
        current = _inflight.get(client_ip, 0)
        if current >= IP_INFLIGHT_LIMIT:
            raise LoginError(429, DETAIL_THROTTLED, retry_after=WINDOW_SECONDS)
        _inflight[client_ip] = current + 1
    try:
        yield
    finally:
        with _inflight_guard:
            remaining = _inflight.get(client_ip, 1) - 1
            if remaining <= 0:
                _inflight.pop(client_ip, None)
            else:
                _inflight[client_ip] = remaining


def throttle_key(prefix: str, value: str) -> str:
    return f"{prefix}:{value}"


def check_rate(db: Database, key: str, limit: int, now: Optional[int] = None) -> bool:
    """True when ``key`` has reached ``limit`` events inside the 15 minute window."""
    ts = int(time.time() if now is None else now)
    count, _latest = db.login_attempt_stats(key, ts - WINDOW_SECONDS)
    return count >= limit


def verify_local_login(
    db: Database,
    *,
    username: str,
    password: str,
    totp_code: Optional[str],
    recovery_code: Optional[str],
    client_ip: str,
    now: Optional[int] = None,
) -> LoginResult:
    """Throttle pre-check, password and MFA verification, failure recording, atomically per username.

    Attempts for one normalized username run one at a time, so a concurrent burst cannot get more than
    ``USER_FAILURE_LIMIT`` password verifications in before the lock trips. At most
    ``IP_INFLIGHT_LIMIT`` attempts per IP may be in flight at once (429 beyond that).
    """
    with inflight_slot(client_ip):
        with serialized_attempt("login:" + local_auth.normalize_username(username)):
            return _verify_local_login_locked(
                db,
                username=username,
                password=password,
                totp_code=totp_code,
                recovery_code=recovery_code,
                client_ip=client_ip,
                now=now,
            )


def _verify_local_login_locked(
    db: Database,
    *,
    username: str,
    password: str,
    totp_code: Optional[str],
    recovery_code: Optional[str],
    client_ip: str,
    now: Optional[int] = None,
) -> LoginResult:
    ts = int(time.time() if now is None else now)
    since = ts - WINDOW_SECONDS
    uname = local_auth.normalize_username(username)
    ip_key = throttle_key("ip", client_ip)
    user_key = throttle_key("user", uname)
    lock_key = throttle_key("lock", uname)

    ip_failures, _ = db.login_attempt_stats(ip_key, since)
    if ip_failures >= IP_FAILURE_LIMIT:
        raise LoginError(429, DETAIL_THROTTLED, retry_after=WINDOW_SECONDS)
    locked, locked_at = db.login_attempt_stats(lock_key, since)
    if locked:
        db.record_login_attempt(ip_key, ts, max_rows=IP_FAILURE_LIMIT)
        raise LoginError(423, DETAIL_LOCKED, retry_after=max(1, locked_at + WINDOW_SECONDS - ts))

    def fail(detail: str, user_id: Optional[str]) -> LoginError:
        db.record_login_attempt(ip_key, ts, max_rows=IP_FAILURE_LIMIT)
        db.record_login_attempt(user_key, ts, max_rows=USER_FAILURE_LIMIT)
        failures, _ = db.login_attempt_stats(user_key, since)
        lock_until_us: Optional[int] = None
        if failures >= USER_FAILURE_LIMIT:
            db.record_login_attempt(lock_key, ts, max_rows=1)
            lock_until_us = (ts + WINDOW_SECONDS) * 1_000_000
        if user_id:
            db.record_failed_login(user_id, lock_until_us)
        return LoginError(401, detail)

    if not password or not uname or len(uname) > 64:
        local_auth.equalize_timing(password or "x")
        raise fail(DETAIL_INVALID, None)

    user = db.get_user_by_username(uname)
    cred = None
    if user is not None and user.get("auth_type") == "local":
        cred = db.get_local_credentials(user["id"])
    stored = cred["password_hash"] if cred else None
    if stored:
        password_ok = local_auth.verify_password(password, stored)
    else:
        # Unknown user, Plex user, or an invite not yet redeemed: burn the same scrypt cost.
        local_auth.equalize_timing(password)
        password_ok = False
    if not password_ok or cred is None or cred["disabled"]:
        raise fail(DETAIL_INVALID, user["id"] if (user and cred) else None)

    assert user is not None  # password_ok implies a stored hash, which implies a user
    mfa_enabled = bool(cred["totp_secret"])
    if mfa_enabled:
        if not totp_code and not recovery_code:
            raise LoginError(401, DETAIL_MFA_REQUIRED)
        if recovery_code:
            normalized = local_auth.normalize_recovery_code(recovery_code)
            if normalized is None or not db.consume_recovery_code(
                user["id"], local_auth.hash_recovery_code(normalized)
            ):
                raise fail(DETAIL_INVALID_CODE, user["id"])
        else:
            counter = local_auth.verify_totp(cred["totp_secret"], totp_code, cred["totp_last_counter"])
            if counter is None or not db.advance_totp_counter(user["id"], counter):
                raise fail(DETAIL_INVALID_CODE, user["id"])

    if local_auth.needs_rehash(stored):
        db.update_password_hash(user["id"], local_auth.hash_password(password))
    db.record_successful_login(user["id"])
    db.clear_login_attempts(user_key)
    state = db.get_auth_state(user["id"])
    enrollment_required = (not mfa_enabled) and bool(db.get_account_settings()["require_mfa_local"])
    return LoginResult(
        user_id=user["id"],
        username=user["username"],
        mfa_enrollment_required=enrollment_required,
        session_floor_us=int(state["sessions_revoked_at_us"]),
    )

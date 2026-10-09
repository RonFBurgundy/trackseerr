"""Local accounts, invites, MFA, revocation, tombstones, and login throttling.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from trackseerr import local_auth
from trackseerr.models import (
    UserPermission,
)
from trackseerr.storage_common import (
    _KNOWN_PERMISSION_MASK,
    _now_us,
    _us_of,
    _utcnow,
    ts_to_us,
)

logger = logging.getLogger(__name__)


class AccountStoreMixin:
    # -------------------------------------------------------------------------
    # Local accounts, invites, MFA, revocation, tombstones, login throttling
    # -------------------------------------------------------------------------

    _CREDENTIAL_COLUMNS = (
        "password_hash, totp_secret, totp_last_counter, failed_logins, locked_until, auth_type, disabled"
    )

    def get_user_by_username(self, username: str) -> Optional[dict[str, Any]]:
        """Case-insensitive lookup across ALL users. When several share a name the result is deterministic: a local
        account wins (it is the one that can sign in with a password), then the oldest, then the lowest id."""
        with self._lock:
            row = self.conn.execute(
                "SELECT id FROM users WHERE lower(username) = lower(?) "
                "ORDER BY (auth_type = 'local') DESC, created_at ASC, id ASC LIMIT 1",
                (str(username or ""),),
            ).fetchone()
        return self.get_user(row["id"]) if row else None

    def list_users_by_username(self, username: str) -> list[dict[str, Any]]:
        """EVERY user whose name matches case-insensitively, in the same order as ``get_user_by_username``."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM users WHERE lower(username) = lower(?) "
                "ORDER BY (auth_type = 'local') DESC, created_at ASC, id ASC",
                (str(username or ""),),
            ).fetchall()
        return [u for u in (self.get_user(r["id"]) for r in rows) if u is not None]

    def get_local_credentials(self, user_id: str) -> Optional[dict[str, Any]]:
        """Secret-bearing columns for one user. Server-side use only; never serialise this dict."""
        with self._lock:
            row = self.conn.execute(
                f"SELECT {self._CREDENTIAL_COLUMNS} FROM users WHERE id = ?", (str(user_id),)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["disabled"] = bool(d["disabled"])
        return d

    def get_auth_state(self, user_id: str) -> dict[str, Any]:
        """Revocation inputs for one id: disabled, tombstoned, ``sessions_revoked_at_us`` and MFA state."""
        uid = str(user_id)
        with self._lock:
            row = self.conn.execute(
                "SELECT auth_type, disabled, sessions_revoked_at, totp_secret FROM users WHERE id = ?", (uid,)
            ).fetchone()
            tomb = self.conn.execute("SELECT 1 FROM user_tombstones WHERE user_id = ?", (uid,)).fetchone()
        state: dict[str, Any] = {
            "exists": row is not None,
            "tombstoned": tomb is not None,
            "auth_type": row["auth_type"] if row else None,
            "disabled": bool(row["disabled"]) if row else False,
            "sessions_revoked_at_us": ts_to_us(row["sessions_revoked_at"]) if row else 0,
            "mfa_enabled": bool(row["totp_secret"]) if row else False,
        }
        return state

    def create_local_user(
        self,
        username: str,
        email: Optional[str] = None,
        permissions: Optional[int] = None,
        created_by: Optional[str] = None,
    ) -> tuple[dict[str, Any], str]:
        """Creates a local user (no password yet) and its 48 h invite. Returns ``(user, raw_token)``.

        The raw token is returned exactly once and is never stored (only its sha256).
        Raises ``ValueError`` for an invalid or already-taken username, an invalid email, or unknown
        permission bits.
        """
        name = local_auth.normalize_username(username)
        problem = local_auth.validate_username(name)
        if problem:
            raise ValueError(problem)
        mail = str(email).strip() if email else None
        if mail is not None and (len(mail) > 254 or "@" not in mail or any(c in mail for c in "\r\n\x00")):
            raise ValueError("Invalid email address")
        perms = int(UserPermission.DEFAULT) if permissions is None else int(permissions)
        if perms < 0 or perms & ~_KNOWN_PERMISSION_MASK:
            raise ValueError("Unknown permission bits")
        user_id = "local-" + secrets.token_hex(12)
        with self._lock:
            clash = self.conn.execute(
                "SELECT 1 FROM users WHERE lower(username) = lower(?)", (name,)
            ).fetchone()
            if clash:
                raise ValueError("Username is already taken")
            self.conn.execute(
                """
                INSERT INTO users (id, username, email, is_admin, permissions, auth_type, updated_at)
                VALUES (?, ?, ?, ?, ?, 'local', CURRENT_TIMESTAMP)
                """,
                (user_id, name, mail, 1 if perms & int(UserPermission.ADMIN) else 0, perms),
            )
            self.conn.commit()
        raw = self.issue_token(user_id, "invite", created_by)
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("Failed to create local user")
        return user, raw

    def mirror_local_user(self, user_id: str, username: str) -> dict[str, Any]:
        """Gateway side: records the identity core verified so a session can reference it.

        Never grants admin, never stores credentials, and never touches an existing row's flags.
        Only called after core has authenticated the user.
        """
        uid = str(user_id or "")
        if not uid.startswith("local-") or self.is_tombstoned(uid):
            raise PermissionError("Invalid local user id")
        existing = self.get_user(uid)
        if existing is not None:
            return existing
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO users (id, username, email, is_admin, permissions, auth_type, updated_at)
                VALUES (?, ?, NULL, 0, 34, 'local', CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO NOTHING
                """,
                (uid, str(username)),
            )
            self.conn.commit()
        user = self.get_user(uid)
        if user is None:
            raise RuntimeError("Failed to mirror local user")
        return user

    # --- tokens (invite / reset) ---

    def issue_token(self, user_id: str, purpose: str, created_by: Optional[str] = None) -> str:
        """Issues a single-use token, voiding ALL of the user's unused tokens whatever their purpose."""
        if purpose not in ("invite", "reset"):
            raise ValueError("Invalid token purpose")
        raw = local_auth.generate_token()
        expires = _utcnow() + timedelta(seconds=local_auth.TOKEN_TTL_SECONDS)
        with self._lock:
            self.conn.execute(
                "DELETE FROM user_invites WHERE user_id = ? AND used_at IS NULL",
                (str(user_id),),
            )
            self.conn.execute(
                """
                INSERT INTO user_invites (token_hash, user_id, purpose, expires_at, created_by)
                VALUES (?, ?, ?, ?, ?)
                """,
                (local_auth.hash_token(raw), str(user_id), purpose, expires.isoformat(), created_by),
            )
            self.conn.commit()
        return raw

    def get_valid_token(self, raw: str) -> Optional[dict[str, Any]]:
        """``{username, purpose, expires_at, user_id}`` for an unused, unexpired token of a local user."""
        if not local_auth.token_is_well_formed(raw):
            return None
        with self._lock:
            row = self.conn.execute(
                """
                SELECT i.user_id, i.purpose, i.expires_at, i.used_at, u.username, u.auth_type
                FROM user_invites i JOIN users u ON u.id = i.user_id
                WHERE i.token_hash = ?
                """,
                (local_auth.hash_token(raw),),
            ).fetchone()
        if not row or row["used_at"] is not None or row["auth_type"] != "local":
            return None
        if ts_to_us(row["expires_at"]) <= _now_us():
            return None
        return {
            "user_id": row["user_id"],
            "username": row["username"],
            "purpose": row["purpose"],
            "expires_at": row["expires_at"],
        }

    def consume_token_set_password(self, raw: str, password_hash: str) -> Optional[str]:
        """Atomically marks the token used and sets the password. Returns the user id, or None.

        Concurrent redemptions of the same token cannot both succeed. Sessions are revoked.
        """
        if not local_auth.token_is_well_formed(raw):
            return None
        token_hash = local_auth.hash_token(raw)
        now = _utcnow()
        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT user_id, expires_at FROM user_invites WHERE token_hash = ? AND used_at IS NULL",
                    (token_hash,),
                ).fetchone()
                if not row or ts_to_us(row["expires_at"]) <= _us_of(now):
                    return None
                cur = self.conn.execute(
                    "UPDATE user_invites SET used_at = ? WHERE token_hash = ? AND used_at IS NULL",
                    (now.isoformat(), token_hash),
                )
                if cur.rowcount != 1:
                    self.conn.rollback()
                    return None
                upd = self.conn.execute(
                    """
                    UPDATE users SET password_hash = ?, password_changed_at = ?, sessions_revoked_at = ?,
                        failed_logins = 0, locked_until = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND auth_type = 'local'
                    """,
                    (password_hash, now.isoformat(), now.isoformat(), row["user_id"]),
                )
                if upd.rowcount != 1:
                    self.conn.rollback()
                    return None
                self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["user_id"],))
                uname = self._username_of(row["user_id"])
                self.conn.execute(
                    "DELETE FROM login_attempts WHERE key IN (?, ?)", ("user:" + uname, "lock:" + uname)
                )
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                raise
        return str(row["user_id"])

    def _username_of(self, user_id: str) -> str:
        row = self.conn.execute("SELECT username FROM users WHERE id = ?", (str(user_id),)).fetchone()
        return str(row["username"]).lower() if row else ""

    # --- passwords / sessions / revocation ---

    def set_password(self, user_id: str, password_hash: str) -> int:
        """Sets a new password and revokes every session. Returns the revocation instant (epoch us)."""
        now = _utcnow()
        with self._lock:
            self.conn.execute(
                """
                UPDATE users SET password_hash = ?, password_changed_at = ?, sessions_revoked_at = ?,
                    failed_logins = 0, locked_until = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND auth_type = 'local'
                """,
                (password_hash, now.isoformat(), now.isoformat(), str(user_id)),
            )
            self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (str(user_id),))
            self.conn.commit()
        return _us_of(now)

    def update_password_hash(self, user_id: str, password_hash: str) -> None:
        """Replaces the stored hash (parameter upgrade) without touching sessions."""
        with self._lock:
            self.conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ? AND auth_type = 'local'",
                (password_hash, str(user_id)),
            )
            self.conn.commit()

    def revoke_sessions(self, user_id: str) -> int:
        """Invalidates every session of ``user_id`` (sessions_revoked_at = now). Returns epoch us."""
        now = _utcnow()
        with self._lock:
            self.conn.execute(
                "UPDATE users SET sessions_revoked_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (now.isoformat(), str(user_id)),
            )
            self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (str(user_id),))
            self.conn.commit()
        return _us_of(now)

    def delete_user_sessions(self, user_id: str, keep_session: Optional[str] = None) -> int:
        with self._lock:
            if keep_session:
                cur = self.conn.execute(
                    "DELETE FROM sessions WHERE user_id = ? AND session_id != ?", (str(user_id), keep_session)
                )
            else:
                cur = self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (str(user_id),))
            self.conn.commit()
            return cur.rowcount

    def set_disabled(self, user_id: str, disabled: bool) -> bool:
        """Disables or enables a user. Disabling revokes every session. Returns False if no such user."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE users SET disabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (1 if disabled else 0, str(user_id)),
            )
            self.conn.commit()
            changed = cur.rowcount > 0
        if changed and disabled:
            self.revoke_sessions(user_id)
        return changed

    def record_successful_login(self, user_id: str) -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE users SET last_login_at = ?, failed_logins = 0, locked_until = NULL
                WHERE id = ?
                """,
                (_utcnow().isoformat(), str(user_id)),
            )
            self.conn.commit()

    def stamp_last_login(self, user_id: str) -> None:
        """Stamps ``last_login_at`` only. Unlike :meth:`record_successful_login` it never touches
        ``failed_logins`` / ``locked_until``, so a gateway-reported Plex sign-in cannot clear a lockout."""
        with self._lock:
            self.conn.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (_utcnow().isoformat(), str(user_id)),
            )
            self.conn.commit()

    def record_failed_login(self, user_id: str, locked_until_us: Optional[int] = None) -> None:
        locked = (
            datetime.fromtimestamp(locked_until_us / 1_000_000, tz=timezone.utc).isoformat()
            if locked_until_us
            else None
        )
        with self._lock:
            self.conn.execute(
                """
                UPDATE users SET failed_logins = failed_logins + 1,
                    locked_until = COALESCE(?, locked_until)
                WHERE id = ?
                """,
                (locked, str(user_id)),
            )
            self.conn.commit()

    # --- login attempt throttling ---

    _PRUNE_INTERVAL_SECONDS = 60

    def record_login_attempt(
        self, key: str, now: Optional[int] = None, max_rows: Optional[int] = None, window: int = 900
    ) -> bool:
        """Records one throttled event. Returns False (and inserts nothing) when ``key`` already has
        ``max_rows`` events inside ``window`` seconds, so a flood cannot grow the table without bound.

        Rows older than one hour are pruned at most once per minute.
        """
        ts = int(time.time()) if now is None else int(now)
        with self._lock:
            if max_rows is not None:
                count = self.conn.execute(
                    "SELECT COUNT(*) FROM login_attempts WHERE key = ? AND attempted_at >= ?",
                    (str(key), ts - int(window)),
                ).fetchone()[0]
                if int(count) >= int(max_rows):
                    return False
            if ts - getattr(self, "_last_attempt_prune", 0) >= self._PRUNE_INTERVAL_SECONDS:
                self.conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (ts - 3600,))
                self._last_attempt_prune = ts
            self.conn.execute("INSERT INTO login_attempts (key, attempted_at) VALUES (?, ?)", (str(key), ts))
            self.conn.commit()
        return True

    def login_attempt_stats(self, key: str, since: int) -> tuple[int, int]:
        """``(count, latest_attempted_at)`` of ``key`` events at or after ``since`` (unix seconds)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(attempted_at), 0) FROM login_attempts WHERE key = ? AND attempted_at >= ?",
                (str(key), int(since)),
            ).fetchone()
        return int(row[0]), int(row[1])

    def clear_login_attempts(self, key: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM login_attempts WHERE key = ?", (str(key),))
            self.conn.commit()

    # --- tombstones ---

    def add_tombstone(self, user_id: str, deleted_by: Optional[str] = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO user_tombstones (user_id, deleted_by) VALUES (?, ?)",
                (str(user_id), deleted_by),
            )
            self.conn.commit()

    def is_tombstoned(self, user_id: str) -> bool:
        with self._lock:
            return (
                self.conn.execute("SELECT 1 FROM user_tombstones WHERE user_id = ?", (str(user_id),)).fetchone()
                is not None
            )

    def remove_tombstone(self, user_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM user_tombstones WHERE user_id = ?", (str(user_id),))
            self.conn.commit()
            return cur.rowcount > 0

    # --- MFA ---

    def enable_totp(self, user_id: str, secret_b32: str, counter: int, recovery_hashes: list[str]) -> None:
        """Stores the confirmed secret (with its used counter) and replaces all recovery codes."""
        with self._lock:
            self.conn.execute(
                "UPDATE users SET totp_secret = ?, totp_last_counter = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (secret_b32, int(counter), str(user_id)),
            )
            self._replace_recovery_codes_locked(user_id, recovery_hashes)
            self.conn.commit()

    def clear_mfa(self, user_id: str) -> None:
        """Removes the TOTP secret and every recovery code."""
        with self._lock:
            self.conn.execute(
                "UPDATE users SET totp_secret = NULL, totp_last_counter = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (str(user_id),),
            )
            self.conn.execute("DELETE FROM user_recovery_codes WHERE user_id = ?", (str(user_id),))
            self.conn.commit()

    def advance_totp_counter(self, user_id: str, counter: int) -> bool:
        """Atomically records ``counter`` as used; False if it is not strictly newer (a replay)."""
        with self._lock:
            cur = self.conn.execute(
                """
                UPDATE users SET totp_last_counter = ?
                WHERE id = ? AND totp_secret IS NOT NULL
                  AND (totp_last_counter IS NULL OR totp_last_counter < ?)
                """,
                (int(counter), str(user_id), int(counter)),
            )
            self.conn.commit()
            return cur.rowcount == 1

    def _replace_recovery_codes_locked(self, user_id: str, hashes: list[str]) -> None:
        self.conn.execute("DELETE FROM user_recovery_codes WHERE user_id = ?", (str(user_id),))
        self.conn.executemany(
            "INSERT OR IGNORE INTO user_recovery_codes (user_id, code_hash) VALUES (?, ?)",
            [(str(user_id), h) for h in hashes],
        )

    def replace_recovery_codes(self, user_id: str, hashes: list[str]) -> None:
        with self._lock:
            self._replace_recovery_codes_locked(user_id, hashes)
            self.conn.commit()

    def consume_recovery_code(self, user_id: str, code_hash: str) -> bool:
        """Marks a recovery code used. Single use: a second call with the same hash returns False."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE user_recovery_codes SET used_at = ? WHERE user_id = ? AND code_hash = ? AND used_at IS NULL",
                (_utcnow().isoformat(), str(user_id), code_hash),
            )
            self.conn.commit()
            return cur.rowcount == 1

    def count_recovery_codes(self, user_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM user_recovery_codes WHERE user_id = ? AND used_at IS NULL", (str(user_id),)
            ).fetchone()
        return int(row[0])

    # --- account settings and quota reporting ---

    def get_account_settings(self) -> dict[str, Any]:
        gs = self.get_general_settings()
        return {
            "require_mfa_local": bool(gs.get("require_mfa_local")),
            "default_quota_tracks": int(gs.get("default_quota_tracks", 25)),
            "default_quota_albums": int(gs.get("default_quota_albums", 10)),
            "default_quota_discographies": int(gs.get("default_quota_discographies", 1)),
            "default_quota_window_days": int(gs.get("default_quota_window_days", 7)),
        }

    def get_user_quota_overrides(self, user_id: str) -> dict[str, Optional[int]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT quota_tracks, quota_albums, quota_discographies, quota_window_days FROM users WHERE id = ?",
                (str(user_id),),
            ).fetchone()
        if not row:
            return {"quota_tracks": None, "quota_albums": None, "quota_discographies": None, "quota_window_days": None}
        return {k: (int(row[k]) if row[k] is not None else None) for k in row.keys()}

    def count_user_requests_by_type(self, user_id: str, days: int) -> dict[str, int]:
        """Quota usage in the rolling window, excluding rejected and cancelled requests.

        ``track`` and ``album`` count individual requests; albums that belong to a discography batch are not
        album units. ``discography`` counts distinct discography batches. This is the single counting function
        used by enforcement and by every quota report.
        """
        counts = {"track": 0, "album": 0, "discography": 0}
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT item_type, COUNT(*) AS n FROM music_requests
                WHERE user_id = ? AND status NOT IN ('rejected', 'cancelled')
                  AND batch_kind IS NULL
                  AND datetime(created_at) >= datetime('now', '-' || ? || ' days')
                GROUP BY item_type
                """,
                (str(user_id), int(days)),
            ).fetchall()
            disco = self.conn.execute(
                """
                SELECT COUNT(DISTINCT batch_id) FROM music_requests
                WHERE user_id = ? AND status NOT IN ('rejected', 'cancelled')
                  AND batch_kind = 'discography' AND batch_id IS NOT NULL
                  AND datetime(created_at) >= datetime('now', '-' || ? || ' days')
                """,
                (str(user_id), int(days)),
            ).fetchone()
        for row in rows:
            if row["item_type"] in ("track", "album"):
                counts[row["item_type"]] = int(row["n"])
        counts["discography"] = int(disco[0]) if disco and disco[0] is not None else 0
        return counts


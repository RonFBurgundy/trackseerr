"""User persistence and session management.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional, Union

from trackseerr import local_auth
from trackseerr.models import (
    UserPermission,
)
from trackseerr.storage_common import (
    RESERVED_USER_IDS,
    _KNOWN_PERMISSION_MASK,
    _utcnow,
)

logger = logging.getLogger(__name__)


class UserStoreMixin:
    # -------------------------------------------------------------------------
    # Users CRUD
    # -------------------------------------------------------------------------

    def upsert_user(
        self,
        user_id: str,
        username: str,
        email: Optional[str] = None,
        is_admin: bool = False,
    ) -> dict[str, Any]:
        uid = str(user_id)
        uname = str(username)
        admin_val = 1 if is_admin else 0
        default_perms = 35 if is_admin else 34
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO users (id, username, email, is_admin, permissions, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    username = excluded.username,
                    email = excluded.email,
                    is_admin = excluded.is_admin,
                    permissions = CASE WHEN excluded.is_admin = 1 THEN users.permissions | 1 ELSE users.permissions END,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (uid, uname, email, admin_val, default_perms),
            )
            self.conn.commit()
        user = self.get_user(uid)
        if user is None:
            raise RuntimeError(f"Failed to upsert user {uid}")
        return user

    def import_media_server_user(
        self,
        user_id: str,
        username: str,
        email: Optional[str] = None,
        *,
        auth_type: str,
        grant_admin: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Records an account discovered on the media server (a playlist target), without letting it take over a name.

        Returns None, writing nothing, when the username (case-insensitive) already belongs to a DIFFERENT user id:
        an imported name must never shadow a local account (or another import), the same rule ``ensure_user`` applies
        to gateway identities. A new row gets ``auth_type`` (``plex`` / ``jellyfin`` / ...; imported accounts have no
        Trackseerr login except Plex's own OAuth) and default non-admin permissions. ``grant_admin`` only ever
        raises: an existing admin flag (for instance one granted in the UI) is never demoted. A row that is already
        a local account keeps ``local``.
        """
        uid = str(user_id)
        name = str(username or "").strip()
        if not uid or not name:
            return None
        with self._lock:
            clash = self.conn.execute(
                "SELECT 1 FROM users WHERE lower(username) = lower(?) AND id != ?", (name, uid)
            ).fetchone()
            if clash:
                return None
            if self.conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
                self.conn.execute(
                    """
                    UPDATE users SET
                        username = ?,
                        email = ?,
                        auth_type = CASE WHEN auth_type = 'local' THEN auth_type ELSE ? END,
                        is_admin = CASE WHEN ? THEN 1 ELSE is_admin END,
                        permissions = CASE WHEN ? THEN permissions | 1 ELSE permissions END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (name, email, auth_type, 1 if grant_admin else 0, 1 if grant_admin else 0, uid),
                )
            else:
                self.conn.execute(
                    """
                    INSERT INTO users (id, username, email, is_admin, permissions, auth_type, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (uid, name, email, 1 if grant_admin else 0, 35 if grant_admin else 34, auth_type),
                )
            self.conn.commit()
        return self.get_user(uid)

    def ensure_user(self, user_id: str, username: str) -> dict[str, Any]:
        """Creates a non-admin user with default permissions if absent; never modifies an existing row.

        Used for gateway-asserted identities. Raises ``PermissionError`` for a reserved or
        non-numeric id, or when the username (case-insensitive) belongs to a different user id.
        An existing row is returned as stored, so the asserted name can never rename it.
        """
        uid = str(user_id or "").strip()
        if uid.lower() in RESERVED_USER_IDS:
            raise PermissionError("Invalid asserted user id")
        if self.is_tombstoned(uid):
            raise PermissionError("Account has been removed")
        if uid.startswith("local-"):
            # Local accounts are created only by an admin on core; never implicitly here.
            local = self.get_user(uid)
            if local is None or local.get("auth_type") != "local":
                raise PermissionError("Unknown local user")
            return local
        if not (uid.isascii() and uid.isdigit()):
            raise PermissionError("Invalid asserted user id")
        existing = self.get_user(uid)
        if existing is not None:
            if existing.get("auth_type") == "jellyfin":
                raise PermissionError("Imported media-server accounts cannot sign in")
            return existing
        name = str(username or "").strip() or uid
        with self._lock:
            clash = self.conn.execute(
                "SELECT id FROM users WHERE lower(username) = lower(?) AND id != ?", (name, uid)
            ).fetchone()
            if clash:
                raise PermissionError("Username belongs to a different user")
            self.conn.execute(
                """
                INSERT INTO users (id, username, email, is_admin, permissions, updated_at)
                VALUES (?, ?, NULL, 0, 34, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO NOTHING
                """,
                (uid, name),
            )
            self.conn.commit()
        user = self.get_user(uid)
        if user is None:
            raise RuntimeError(f"Failed to ensure user {uid}")
        return user

    def get_user(self, user_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT id, username, email, is_admin, permissions,
                       quota_albums AS request_limit_quota,
                       COALESCE(quota_window_days,
                                (SELECT default_quota_window_days FROM general_settings WHERE id = 1), 7)
                           AS request_limit_days,
                       created_at, updated_at, auth_type, disabled
                FROM users WHERE id = ?
                """,
                (str(user_id),),
            )
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            d["is_admin"] = bool(d["is_admin"])
            d["disabled"] = bool(d.get("disabled"))
            d["permissions"] = int(d["permissions"]) if d.get("permissions") is not None else 34
            d["request_limit_quota"] = int(d["request_limit_quota"]) if d.get("request_limit_quota") is not None else None
            d["request_limit_days"] = int(d["request_limit_days"]) if d.get("request_limit_days") is not None else 7
            return d

    def list_users(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT id, username, email, is_admin, permissions,
                       quota_albums AS request_limit_quota,
                       COALESCE(quota_window_days,
                                (SELECT default_quota_window_days FROM general_settings WHERE id = 1), 7)
                           AS request_limit_days,
                       created_at, updated_at, auth_type, disabled
                FROM users ORDER BY username ASC
                """
            )
            results = []
            for row in cur.fetchall():
                d = dict(row)
                d["is_admin"] = bool(d["is_admin"])
                d["disabled"] = bool(d.get("disabled"))
                d["permissions"] = int(d["permissions"]) if d.get("permissions") is not None else 34
                d["request_limit_quota"] = int(d["request_limit_quota"]) if d.get("request_limit_quota") is not None else None
                d["request_limit_days"] = int(d["request_limit_days"]) if d.get("request_limit_days") is not None else 7
                results.append(d)
            return results

    def delete_user(self, user_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM users WHERE id = ?",
                (str(user_id),),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def update_user_governance(
        self,
        user_id: str,
        permissions: Optional[int] = None,
        request_limit_quota: Optional[int] = None,
        request_limit_days: Optional[int] = None,
        is_admin: Optional[bool] = None,
        *,
        clear_quota: bool = False,
    ) -> dict[str, Any]:
        """Updates governance permissions, quota limits, and role flags for a user."""
        user = self.get_user(user_id)
        if not user:
            raise KeyError(f"User {user_id} not found")

        updates: list[str] = []
        params: list[Any] = []

        if permissions is not None:
            updates.append("permissions = ?")
            params.append(int(permissions))

        # The legacy single quota is now the per-user track and album override (see migration v28).
        if request_limit_quota is not None:
            updates.append("quota_tracks = ?")
            params.append(int(request_limit_quota))
            updates.append("quota_albums = ?")
            params.append(int(request_limit_quota))
        elif clear_quota:
            updates.append("quota_tracks = NULL")
            updates.append("quota_albums = NULL")

        if request_limit_days is not None:
            updates.append("quota_window_days = ?")
            params.append(int(request_limit_days))

        if is_admin is not None:
            updates.append("is_admin = ?")
            params.append(1 if is_admin else 0)
            if permissions is None:
                # Keep the ADMIN permission bit in step with the flag.
                updates.append("permissions = COALESCE(permissions, 34) | 1" if is_admin else "permissions = COALESCE(permissions, 34) & ~1")

        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            sql = f"UPDATE users SET {', '.join(updates)} WHERE id = ?"
            params.append(str(user_id))
            with self._lock:
                self.conn.execute(sql, params)
                self.conn.commit()

        updated_user = self.get_user(user_id)
        if updated_user is None:
            raise RuntimeError(f"User {user_id} not found after update")
        return updated_user

    _ADMIN_EDITABLE_FIELDS = frozenset(
        {"permissions", "email", "quota_tracks", "quota_albums", "quota_discographies", "quota_window_days"}
    )

    def update_user_admin_fields(self, user_id: str, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Writes whitelisted admin-editable columns (None clears email and quota overrides).

        ``permissions`` also sets ``is_admin`` from the ADMIN bit. Returns the stored row, or None if the user
        does not exist. Raises ``ValueError`` for a field outside the whitelist.
        """
        cols: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in self._ADMIN_EDITABLE_FIELDS:
                raise ValueError(f"Field not editable: {key}")
            cols.append(f"{key} = ?")
            params.append(value)
            if key == "permissions":
                cols.append("is_admin = ?")
                params.append(1 if int(value) & int(UserPermission.ADMIN) else 0)
        if cols:
            cols.append("updated_at = CURRENT_TIMESTAMP")
            params.append(str(user_id))
            with self._lock:
                cur = self.conn.execute(f"UPDATE users SET {', '.join(cols)} WHERE id = ?", params)
                self.conn.commit()
                if cur.rowcount == 0:
                    return None
        return self.get_user(user_id)

    def get_last_seen_changelog_version(self, user_id: str) -> Optional[str]:
        """Returns the changelog version last seen by this user, or None if never recorded."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT last_seen_changelog_version FROM users WHERE id = ?",
                (str(user_id),),
            )
            row = cur.fetchone()
            if not row or row[0] is None:
                return None
            return str(row[0])

    def set_last_seen_changelog_version(self, user_id: str, version: str) -> None:
        """Stores the changelog version last seen by this user."""
        with self._lock:
            self.conn.execute(
                "UPDATE users SET last_seen_changelog_version = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (str(version), str(user_id)),
            )
            self.conn.commit()

    def count_active_admins(self, exclude_user_id: Optional[str] = None) -> int:
        """Admins that are not disabled, optionally excluding one user id."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND disabled = 0 AND id != ?",
                (str(exclude_user_id) if exclude_user_id is not None else "",),
            ).fetchone()
        return int(row[0])

    def count_local_login_admins(self) -> int:
        """Enabled admins that can sign in without a media server: local accounts holding a password hash."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND disabled = 0 AND auth_type = 'local' "
                "AND password_hash IS NOT NULL AND password_hash != ''"
            ).fetchone()
        return int(row[0])

    def create_local_user_with_password(
        self, username: str, password_hash: str, permissions: int, email: Optional[str] = None
    ) -> dict[str, Any]:
        """Creates a local user and its password in ONE transaction (no invite row). Either the whole row exists
        or nothing does. Raises ``ValueError`` for an invalid/taken username or unknown permission bits."""
        name = local_auth.normalize_username(username)
        problem = local_auth.validate_username(name)
        if problem:
            raise ValueError(problem)
        perms = int(permissions)
        if perms < 0 or perms & ~_KNOWN_PERMISSION_MASK:
            raise ValueError("Unknown permission bits")
        if not password_hash:
            raise ValueError("Password hash required")
        user_id = "local-" + secrets.token_hex(12)
        now = _utcnow().isoformat()
        with self._lock:
            try:
                if self.conn.execute("SELECT 1 FROM users WHERE lower(username) = lower(?)", (name,)).fetchone():
                    raise ValueError("Username is already taken")
                self.conn.execute(
                    """
                    INSERT INTO users (id, username, email, is_admin, permissions, auth_type, password_hash,
                                       password_changed_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'local', ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (user_id, name, email, 1 if perms & int(UserPermission.ADMIN) else 0, perms, password_hash, now),
                )
                self.conn.commit()
            except (ValueError, sqlite3.Error):
                self.conn.rollback()
                raise
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("Failed to create local user")
        return user

    def list_users_admin(self) -> list[dict[str, Any]]:
        """Admin listing rows: profile, state, quota overrides and MFA flag. Never selects any secret value."""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, username, email, is_admin, permissions, auth_type, disabled, last_login_at, created_at,
                       totp_secret IS NOT NULL AS mfa_enabled,
                       quota_tracks, quota_albums, quota_discographies, quota_window_days
                FROM users ORDER BY lower(username) ASC
                """
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            for flag in ("is_admin", "disabled", "mfa_enabled"):
                d[flag] = bool(d[flag])
            d["permissions"] = int(d["permissions"]) if d["permissions"] is not None else int(UserPermission.DEFAULT)
            out.append(d)
        return out

    def delete_user_cascade(self, user_id: str, deleted_by: Optional[str] = None) -> bool:
        """Deletes a user and everything they own, then writes a tombstone, in one transaction.

        Foreign keys cascade requests, sessions, invites, recovery codes, scrobbles, mixes and issues. Playlists the
        user created are removed unless another user is targeted by them (those are orphaned, creator cleared);
        match-override authorship and Plex mix snapshots are detached or removed. Returns False if no such user.
        """
        uid = str(user_id)
        with self._lock:
            row = self.conn.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()
            if row is None:
                return False
            username = str(row["username"])
            try:
                self.conn.execute(
                    """
                    DELETE FROM playlists WHERE creator_id = ? AND NOT EXISTS (
                        SELECT 1 FROM playlist_targets t WHERE t.playlist_id = playlists.id AND t.user_id != ?
                    )
                    """,
                    (uid, uid),
                )
                self.conn.execute("UPDATE playlists SET creator_id = NULL WHERE creator_id = ?", (uid,))
                self.conn.execute("UPDATE match_overrides SET created_by = NULL WHERE created_by = ?", (uid,))
                self.conn.execute("DELETE FROM plex_mix_snapshots WHERE created_by = ?", (uid,))
                if not uid.startswith("local-"):
                    self.conn.execute("DELETE FROM plex_playlist_registry WHERE plex_user = ?", (username,))
                self.conn.execute("DELETE FROM users WHERE id = ?", (uid,))
                self.conn.execute(
                    "INSERT OR REPLACE INTO user_tombstones (user_id, deleted_by) VALUES (?, ?)", (uid, deleted_by)
                )
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                raise
        return True

    def update_account_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Updates ``require_mfa_local`` and the ``default_quota_*`` settings (singleton row)."""
        allowed = {
            "require_mfa_local",
            "default_quota_tracks",
            "default_quota_albums",
            "default_quota_discographies",
            "default_quota_window_days",
        }
        cols: list[str] = []
        params: list[Any] = []
        for key, value in updates.items():
            if key not in allowed:
                raise ValueError(f"Setting not editable: {key}")
            cols.append(f"{key} = ?")
            params.append(1 if value is True else 0 if value is False else int(value))
        if cols:
            self.get_general_settings()  # make sure the singleton row exists
            cols.append("updated_at = CURRENT_TIMESTAMP")
            with self._lock:
                self.conn.execute(f"UPDATE general_settings SET {', '.join(cols)} WHERE id = 1", params)
                self.conn.commit()
        return self.get_account_settings()

    def get_user_active_request_count(
        self, user_id: str, days: Optional[int] = None
    ) -> int:
        """Counts active/recent requests for a user within a rolling day window or all time."""
        with self._lock:
            if days is not None and int(days) > 0:
                cur = self.conn.execute(
                    """
                    SELECT COUNT(*) FROM music_requests
                    WHERE user_id = ?
                      AND status IN ('pending', 'processing', 'approved')
                      AND datetime(created_at) >= datetime('now', '-' || ? || ' days')
                    """,
                    (str(user_id), int(days)),
                )
            else:
                cur = self.conn.execute(
                    """
                    SELECT COUNT(*) FROM music_requests
                    WHERE user_id = ?
                      AND status IN ('pending', 'processing', 'approved')
                    """,
                    (str(user_id),),
                )
            row = cur.fetchone()
            return int(row[0]) if (row and row[0] is not None) else 0

    # -------------------------------------------------------------------------
    # Sessions
    # -------------------------------------------------------------------------

    def create_session(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
        expires_at: Optional[Union[str, datetime]] = None,
        issued_at_us: Optional[int] = None,
    ) -> dict[str, Any]:
        s_id = str(session_id)
        issued = (
            datetime.fromtimestamp(issued_at_us / 1_000_000, tz=timezone.utc) if issued_at_us else _utcnow()
        )
        created_str = issued.isoformat()
        u_id = str(user_id) if user_id is not None else None
        data_str = json.dumps(data if data is not None else {})
        if isinstance(expires_at, datetime):
            exp_str = expires_at.isoformat()
        elif expires_at is not None:
            exp_str = str(expires_at)
        else:
            exp_str = None

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO sessions (session_id, user_id, data, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    data = excluded.data,
                    expires_at = excluded.expires_at
                """,
                (s_id, u_id, data_str, exp_str, created_str),
            )
            self.conn.commit()
        session = self.get_session(s_id)
        if session is None:
            raise RuntimeError(f"Failed to create session {s_id}")
        return session

    def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT session_id, user_id, data, expires_at, created_at
                FROM sessions
                WHERE session_id = ?
                """,
                (str(session_id),),
            )
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["data"] = json.loads(d["data"])
            except (ValueError, TypeError):
                d["data"] = {}
            return d

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM sessions WHERE session_id = ?",
                (str(session_id),),
            )
            self.conn.commit()
            return cur.rowcount > 0


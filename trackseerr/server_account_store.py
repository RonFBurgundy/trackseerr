"""Per-user media-server account store mixin for TrackSeerr database.

Provides CRUD for the ``user_server_accounts`` table (encrypted per-user server logins).
"""

from __future__ import annotations

from typing import Any, Optional


class ServerAccountStoreMixin:
    """Mixin for Database providing ``user_server_accounts`` store operations."""

    def get_server_account(self, user_id: str, server_id: str) -> Optional[dict[str, Any]]:
        """Return the saved account (``username``, ``secret_sealed``, ``updated_at``) or None."""
        if not user_id or not server_id:
            return None
        with self.reader() as conn:
            row = conn.execute(
                "SELECT username, secret_sealed, updated_at FROM user_server_accounts "
                "WHERE user_id = ? AND server_id = ?",
                (str(user_id), str(server_id)),
            ).fetchone()
        return dict(row) if row else None

    def set_server_account(
        self,
        user_id: str,
        server_id: str,
        username: str,
        secret_sealed: str,
    ) -> None:
        """Save or replace a user's encrypted login for ``server_id``."""
        uid = str(user_id or "").strip()
        sid = str(server_id or "").strip()
        uname = str(username or "").strip()
        sealed = str(secret_sealed or "").strip()
        if not uid:
            raise ValueError("user_id must not be blank")
        if not sid:
            raise ValueError("server_id must not be blank")
        if not uname:
            raise ValueError("username must not be blank")
        if not sealed:
            raise ValueError("secret_sealed must not be blank")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO user_server_accounts (user_id, server_id, username, secret_sealed, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, server_id) DO UPDATE SET
                    username = excluded.username,
                    secret_sealed = excluded.secret_sealed,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (uid, sid, uname, sealed),
            )
            self.conn.commit()

    def delete_server_account(self, user_id: str, server_id: str) -> bool:
        """Delete a user's saved login for ``server_id``. Returns True if a row was deleted."""
        if not user_id or not server_id:
            return False
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM user_server_accounts WHERE user_id = ? AND server_id = ?",
                (str(user_id), str(server_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def list_server_accounts_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """Return ``server_id``, ``username``, and ``updated_at`` for every server saved by ``user_id``."""
        if not user_id:
            return []
        with self.reader() as conn:
            rows = conn.execute(
                "SELECT server_id, username, updated_at FROM user_server_accounts "
                "WHERE user_id = ? ORDER BY server_id",
                (str(user_id),),
            ).fetchall()
        return [dict(row) for row in rows]

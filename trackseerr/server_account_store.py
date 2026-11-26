"""Per-user media-server account store mixin for TrackSeerr database.

Provides CRUD for the ``user_server_accounts`` table (encrypted per-user server logins).
"""

from __future__ import annotations

from typing import Any, Optional

_LINK_SOURCES = ("imported", "manual")


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

    # -------------------------------------------------------------------------
    # Server user links (server_user_links)
    # -------------------------------------------------------------------------

    def link_server_user(
        self,
        user_id: str,
        server_id: str,
        server_user_id: str,
        server_username: str,
        source: str = "imported",
    ) -> None:
        """Link Trackseerr ``user_id`` to ``server_user_id`` on ``server_id``.

        Manual links are never overwritten by subsequent automated imports.
        """
        uid = str(user_id or "").strip()
        sid = str(server_id or "").strip()
        suid = str(server_user_id or "").strip()
        suname = str(server_username or "").strip()
        src = str(source or "").strip()
        if not uid:
            raise ValueError("user_id must not be blank")
        if not sid:
            raise ValueError("server_id must not be blank")
        if not suid:
            raise ValueError("server_user_id must not be blank")
        if not suname:
            raise ValueError("server_username must not be blank")
        if src not in _LINK_SOURCES:
            raise ValueError(f"source must be one of {_LINK_SOURCES}")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO server_user_links (user_id, server_id, server_user_id, server_username, source)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, server_id) DO UPDATE SET
                    server_user_id = excluded.server_user_id,
                    server_username = excluded.server_username,
                    source = excluded.source
                WHERE excluded.source = 'manual' OR server_user_links.source != 'manual'
                """,
                (uid, sid, suid, suname, src),
            )
            self.conn.commit()

    def get_server_user_link(self, user_id: str, server_id: str) -> Optional[dict[str, Any]]:
        """Return the ``server_user_links`` row for ``(user_id, server_id)`` or None."""
        if not user_id or not server_id:
            return None
        with self.reader() as conn:
            row = conn.execute(
                "SELECT user_id, server_id, server_user_id, server_username, source "
                "FROM server_user_links WHERE user_id = ? AND server_id = ?",
                (str(user_id), str(server_id)),
            ).fetchone()
        return dict(row) if row else None

    def list_server_user_links(
        self,
        server_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Return ``server_user_links`` rows filtered optionally by ``server_id`` and/or ``user_id``."""
        clauses: list[str] = []
        params: list[str] = []
        if server_id:
            clauses.append("server_id = ?")
            params.append(str(server_id))
        if user_id:
            clauses.append("user_id = ?")
            params.append(str(user_id))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.reader() as conn:
            rows = conn.execute(
                f"SELECT user_id, server_id, server_user_id, server_username, source "
                f"FROM server_user_links{where} ORDER BY server_id, user_id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def unlink_server_user(self, user_id: str, server_id: str) -> bool:
        """Delete a user's link for ``server_id``. Returns True if a row was deleted."""
        if not user_id or not server_id:
            return False
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM server_user_links WHERE user_id = ? AND server_id = ?",
                (str(user_id), str(server_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------------------
    # Per-user server playlist IDs (server_playlist_ids)
    # -------------------------------------------------------------------------

    def get_server_playlist_id(self, playlist_id: str, server_id: str, user_id: str) -> Optional[str]:
        """Return the mapped ``server_playlist_id`` for ``(playlist_id, server_id, user_id)``, or None."""
        if not playlist_id or not server_id or not user_id:
            return None
        with self.reader() as conn:
            row = conn.execute(
                "SELECT server_playlist_id FROM server_playlist_ids "
                "WHERE playlist_id = ? AND server_id = ? AND user_id = ?",
                (str(playlist_id), str(server_id), str(user_id)),
            ).fetchone()
        return str(row["server_playlist_id"]) if row else None

    def set_server_playlist_id(
        self, playlist_id: str, server_id: str, user_id: str, server_playlist_id: str
    ) -> None:
        """Upsert the ``server_playlist_id`` mapping for ``(playlist_id, server_id, user_id)``."""
        pid = str(playlist_id or "").strip()
        sid = str(server_id or "").strip()
        uid = str(user_id or "").strip()
        spid = str(server_playlist_id or "").strip()
        if not (pid and sid and uid and spid):
            raise ValueError("playlist_id, server_id, user_id, and server_playlist_id must not be blank")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO server_playlist_ids (playlist_id, server_id, user_id, server_playlist_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(playlist_id, server_id, user_id) DO UPDATE SET
                    server_playlist_id = excluded.server_playlist_id
                """,
                (pid, sid, uid, spid),
            )
            self.conn.commit()

    def delete_server_playlist_id(self, playlist_id: str, server_id: str, user_id: str) -> bool:
        """Delete the ``server_playlist_ids`` row for ``(playlist_id, server_id, user_id)``."""
        if not playlist_id or not server_id or not user_id:
            return False
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM server_playlist_ids WHERE playlist_id = ? AND server_id = ? AND user_id = ?",
                (str(playlist_id), str(server_id), str(user_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

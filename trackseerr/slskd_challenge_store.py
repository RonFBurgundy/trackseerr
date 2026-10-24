"""slskd "human check" chat challenges and gated-peer state.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``). Challenges are stored for an admin to answer by
hand; nothing here parses or answers a challenge.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
import uuid

_COLUMNS = (
    "id, client_id, username, message_id, message, download_id, download_title, status, "
    "received_at, expires_at, replied_at, reply_text"
)
_STATUSES = frozenset({"open", "replied", "dismissed", "expired"})
_PEER_STATES = frozenset({"gated", "unlocked"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SlskdChallengeStoreMixin:
    def record_slskd_challenge(
        self,
        client_id: str,
        username: str,
        message_id: str,
        message: str,
        received_at: str,
        expires_at: Optional[str] = None,
        download_id: Optional[str] = None,
        download_title: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Stores a challenge; the new row, or None when (client_id, username, message_id) already exists."""
        chal_id = "chal-" + uuid.uuid4().hex[:12]
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO slskd_challenges (id, client_id, username, message_id, message, download_id, "
                "download_title, received_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chal_id, client_id, username, str(message_id), message, download_id, download_title,
                 received_at, expires_at),
            )
            self.conn.commit()
            if cur.rowcount == 0:
                return None
            row = self.conn.execute(f"SELECT {_COLUMNS} FROM slskd_challenges WHERE id = ?", (chal_id,)).fetchone()
        return dict(row) if row else None

    def list_slskd_challenges(self, status: Optional[str] = "open", limit: int = 100) -> list[dict[str, Any]]:
        """Challenges (optionally filtered by status), newest first."""
        sql = f"SELECT {_COLUMNS} FROM slskd_challenges"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY received_at DESC, rowid DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_slskd_challenge(self, challenge_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                f"SELECT {_COLUMNS} FROM slskd_challenges WHERE id = ?", (challenge_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_slskd_challenge_status(
        self, challenge_id: str, status: str, reply_text: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """Sets a challenge's status (stamping ``replied_at`` for 'replied'); None if it doesn't exist."""
        if status not in _STATUSES:
            raise ValueError(f"Unknown slskd challenge status: {status!r}")
        replied_at = _now_iso() if status == "replied" else None
        with self._lock:
            if status == "replied":
                self.conn.execute(
                    "UPDATE slskd_challenges SET status = ?, replied_at = ?, reply_text = ? WHERE id = ?",
                    (status, replied_at, reply_text, challenge_id),
                )
            else:
                self.conn.execute("UPDATE slskd_challenges SET status = ? WHERE id = ?", (status, challenge_id))
            self.conn.commit()
        return self.get_slskd_challenge(challenge_id)

    def expire_slskd_challenges(self, now_iso: str) -> int:
        """Marks open challenges whose ``expires_at`` is before ``now_iso`` as expired; returns the count."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE slskd_challenges SET status = 'expired' "
                "WHERE status = 'open' AND expires_at IS NOT NULL AND expires_at < ?",
                (now_iso,),
            )
            self.conn.commit()
            return cur.rowcount

    def set_slskd_peer_state(self, client_id: str, username: str, state: str, until_iso: str) -> None:
        """Upserts a peer's gated/unlocked state, lapsing after ``until_iso``."""
        if state not in _PEER_STATES:
            raise ValueError(f"Unknown slskd peer state: {state!r}")
        with self._lock:
            self.conn.execute(
                "INSERT INTO slskd_gated_peers (client_id, username_lower, state, until, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(client_id, username_lower) DO UPDATE SET "
                "state = excluded.state, until = excluded.until, updated_at = excluded.updated_at",
                (client_id, username.strip().lower(), state, until_iso, _now_iso()),
            )
            self.conn.commit()

    def excluded_slskd_peers(self, client_id: str, now_iso: str) -> set[str]:
        """Lower-cased usernames currently gated (state 'gated' and not lapsed)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT username_lower FROM slskd_gated_peers WHERE client_id = ? AND state = 'gated' AND until > ?",
                (client_id, now_iso),
            ).fetchall()
        return {r[0] for r in rows}

    def slskd_peer_state(self, client_id: str, username: str, now_iso: str) -> Optional[str]:
        """The peer's current non-lapsed state, or None."""
        with self._lock:
            row = self.conn.execute(
                "SELECT state FROM slskd_gated_peers WHERE client_id = ? AND username_lower = ? AND until > ?",
                (client_id, username.strip().lower(), now_iso),
            ).fetchone()
        return row[0] if row else None

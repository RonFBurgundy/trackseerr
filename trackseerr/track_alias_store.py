"""Track aliases: a source song (title, artist) mapped to a canonical song, or marked unmatchable.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``). Aliases are global (all playlists) and
matcher-agnostic; sync substitutes approved ones before any matcher runs.
"""

from __future__ import annotations

import re
from typing import Any, Optional

_COLUMNS = (
    "id, source_title, source_artist, source_key, kind, status, target_title, target_artist, target_album, "
    "discovery_id, playlist_id, issue_id, suggested_by, decided_by, created_at, decided_at"
)
KINDS = ("alias", "ignore")
STATUSES = ("pending", "approved", "rejected")


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def track_alias_key(artist: Any, title: Any) -> str:
    """Casefolded, whitespace-collapsed ``artist\\x1ftitle`` key identifying a source song."""
    return f"{_norm(artist)}\x1f{_norm(title)}"


class TrackAliasStoreMixin:
    def upsert_track_alias(
        self,
        source_title: str,
        source_artist: str,
        kind: str,
        status: str,
        target_title: Optional[str] = None,
        target_artist: Optional[str] = None,
        target_album: Optional[str] = None,
        discovery_id: Optional[str] = None,
        playlist_id: Optional[str] = None,
        issue_id: Optional[str] = None,
        suggested_by: Optional[str] = None,
        decided_by: Optional[str] = None,
    ) -> dict[str, Any]:
        """Inserts the alias for this source song, or replaces the existing one (same normalised key)."""
        if kind not in KINDS:
            raise ValueError(f"invalid alias kind: {kind}")
        if status not in STATUSES:
            raise ValueError(f"invalid alias status: {status}")
        key = track_alias_key(source_artist, source_title)
        pending = status == "pending"
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO track_aliases (
                    source_title, source_artist, source_key, kind, status, target_title, target_artist, target_album,
                    discovery_id, playlist_id, issue_id, suggested_by, decided_by, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? THEN NULL ELSE CURRENT_TIMESTAMP END)
                ON CONFLICT(source_key) DO UPDATE SET
                    source_title = excluded.source_title, source_artist = excluded.source_artist,
                    kind = excluded.kind, status = excluded.status,
                    target_title = excluded.target_title, target_artist = excluded.target_artist,
                    target_album = excluded.target_album, discovery_id = excluded.discovery_id,
                    playlist_id = excluded.playlist_id, issue_id = excluded.issue_id,
                    suggested_by = excluded.suggested_by, decided_by = excluded.decided_by,
                    decided_at = CASE WHEN ? THEN NULL ELSE CURRENT_TIMESTAMP END
                """,
                (
                    source_title.strip(), source_artist.strip(), key, kind, status, target_title, target_artist,
                    target_album, discovery_id, playlist_id, issue_id, suggested_by,
                    None if pending else decided_by, 1 if pending else 0, 1 if pending else 0,
                ),
            )
            self.conn.commit()
            row = self.conn.execute(
                f"SELECT {_COLUMNS} FROM track_aliases WHERE source_key = ?", (key,)
            ).fetchone()
        return dict(row)

    def get_track_alias(self, alias_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(f"SELECT {_COLUMNS} FROM track_aliases WHERE id = ?", (alias_id,)).fetchone()
        return dict(row) if row else None

    def list_track_aliases(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            if status is None:
                cur = self.conn.execute(f"SELECT {_COLUMNS} FROM track_aliases ORDER BY created_at DESC, id DESC")
            else:
                cur = self.conn.execute(
                    f"SELECT {_COLUMNS} FROM track_aliases WHERE status = ? ORDER BY created_at DESC, id DESC",
                    (status,),
                )
            return [dict(r) for r in cur.fetchall()]

    def set_track_alias_status(self, alias_id: int, status: str, decided_by: Optional[str]) -> Optional[dict[str, Any]]:
        """Moves an alias to ``status`` (stamping the decision); None when it does not exist."""
        if status not in STATUSES:
            raise ValueError(f"invalid alias status: {status}")
        with self._lock:
            cur = self.conn.execute(
                "UPDATE track_aliases SET status = ?, decided_by = ?, decided_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, decided_by, alias_id),
            )
            self.conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_track_alias(alias_id)

    def delete_track_alias(self, alias_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM track_aliases WHERE id = ?", (alias_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def approved_aliases_by_key(self) -> dict[str, dict[str, Any]]:
        """Approved aliases keyed by ``source_key`` (see ``track_alias_key``)."""
        with self._lock:
            cur = self.conn.execute(f"SELECT {_COLUMNS} FROM track_aliases WHERE status = 'approved'")
            return {r["source_key"]: dict(r) for r in cur.fetchall()}

"""Persistence for delay profiles and the pending-releases queue (migration v50).

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

PROTOCOL_CHOICES = ("usenet", "torrent", "soulseek")


def _loads_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _loads_obj(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


class DelayProfileMixin:
    # ------------------------------------------------------------------------------------------------------------
    # Migration v50
    # ------------------------------------------------------------------------------------------------------------

    def _migration_v50(self, cur: sqlite3.Cursor) -> None:
        """Delay profiles (seeded default: prefer usenet, no delays, bypass at highest quality) and pending releases."""
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS delay_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                order_idx INTEGER NOT NULL DEFAULT 0,
                preferred_protocol TEXT NOT NULL DEFAULT 'usenet',
                usenet_delay_min INTEGER NOT NULL DEFAULT 0,
                torrent_delay_min INTEGER NOT NULL DEFAULT 0,
                soulseek_delay_min INTEGER NOT NULL DEFAULT 0,
                bypass_if_highest_quality INTEGER NOT NULL DEFAULT 1,
                bypass_if_above_score INTEGER,
                tags_json TEXT NOT NULL DEFAULT '[]',
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_delay_profiles_default ON delay_profiles(is_default) WHERE is_default = 1")
        cur.execute("SELECT COUNT(*) FROM delay_profiles WHERE is_default = 1")
        if int(cur.fetchone()[0]) == 0:
            cur.execute(
                "INSERT INTO delay_profiles (name, order_idx, preferred_protocol, usenet_delay_min, torrent_delay_min, "
                "soulseek_delay_min, bypass_if_highest_quality, bypass_if_above_score, tags_json, is_default) "
                "VALUES ('Default', 0, 'usenet', 0, 0, 0, 1, NULL, '[]', 1)"
            )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_releases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                artist_name TEXT NOT NULL DEFAULT '',
                album TEXT,
                item_type TEXT NOT NULL DEFAULT 'track',
                album_id TEXT,
                track_id TEXT,
                request_id TEXT,
                protocol TEXT NOT NULL DEFAULT '',
                quality TEXT,
                format_score INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                rank_json TEXT NOT NULL DEFAULT '[]',
                delay_profile_id INTEGER,
                reason TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL,
                release_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_releases_release_at ON pending_releases(release_at)")

    # ------------------------------------------------------------------------------------------------------------
    # Delay profiles
    # ------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _delay_profile_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        return {
            "id": int(d["id"]),
            "order": int(d["order_idx"]),
            "name": d["name"],
            "preferred_protocol": d["preferred_protocol"],
            "delays": {
                "usenet": int(d["usenet_delay_min"]),
                "torrent": int(d["torrent_delay_min"]),
                "soulseek": int(d["soulseek_delay_min"]),
            },
            "bypass_if_highest_quality": bool(d["bypass_if_highest_quality"]),
            "bypass_if_above_score": int(d["bypass_if_above_score"]) if d["bypass_if_above_score"] is not None else None,
            "tags": _loads_list(d["tags_json"]),
            "is_default": bool(d["is_default"]),
        }

    def list_delay_profiles(self) -> list[dict[str, Any]]:
        """Ordered by ``order``; the default profile always sorts last."""
        with self._lock:
            rows = self.conn.execute("SELECT * FROM delay_profiles ORDER BY is_default, order_idx, id").fetchall()
        return [self._delay_profile_row(r) for r in rows]

    def get_delay_profile(self, profile_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM delay_profiles WHERE id = ?", (int(profile_id),)).fetchone()
        return self._delay_profile_row(row) if row else None

    def create_delay_profile(self, data: dict[str, Any]) -> dict[str, Any]:
        """Appends a non-default profile after the existing ones (the default stays last)."""
        delays = data["delays"]
        with self._lock:
            nxt = self.conn.execute(
                "SELECT COALESCE(MAX(order_idx), 0) + 1 FROM delay_profiles WHERE is_default = 0"
            ).fetchone()[0]
            cur = self.conn.execute(
                "INSERT INTO delay_profiles (name, order_idx, preferred_protocol, usenet_delay_min, torrent_delay_min, "
                "soulseek_delay_min, bypass_if_highest_quality, bypass_if_above_score, tags_json, is_default) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    data["name"],
                    int(nxt),
                    data["preferred_protocol"],
                    int(delays["usenet"]),
                    int(delays["torrent"]),
                    int(delays["soulseek"]),
                    1 if data["bypass_if_highest_quality"] else 0,
                    data.get("bypass_if_above_score"),
                    json.dumps(list(data.get("tags") or [])),
                ),
            )
            self.conn.commit()
            new_id = int(cur.lastrowid)
        result = self.get_delay_profile(new_id)
        assert result is not None
        return result

    def update_delay_profile(self, profile_id: int, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Updates a profile. The default profile keeps empty tags regardless of the payload."""
        delays = data["delays"]
        with self._lock:
            row = self.conn.execute("SELECT is_default FROM delay_profiles WHERE id = ?", (int(profile_id),)).fetchone()
            if not row:
                return None
            tags = [] if row[0] else list(data.get("tags") or [])
            self.conn.execute(
                "UPDATE delay_profiles SET name = ?, preferred_protocol = ?, usenet_delay_min = ?, torrent_delay_min = ?, "
                "soulseek_delay_min = ?, bypass_if_highest_quality = ?, bypass_if_above_score = ?, tags_json = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    data["name"],
                    data["preferred_protocol"],
                    int(delays["usenet"]),
                    int(delays["torrent"]),
                    int(delays["soulseek"]),
                    1 if data["bypass_if_highest_quality"] else 0,
                    data.get("bypass_if_above_score"),
                    json.dumps(tags),
                    int(profile_id),
                ),
            )
            self.conn.commit()
        return self.get_delay_profile(profile_id)

    def delete_delay_profile(self, profile_id: int) -> str:
        """Returns ``deleted``, ``not_found`` or ``default`` (the default profile cannot be deleted)."""
        with self._lock:
            row = self.conn.execute("SELECT is_default FROM delay_profiles WHERE id = ?", (int(profile_id),)).fetchone()
            if not row:
                return "not_found"
            if row[0]:
                return "default"
            self.conn.execute("DELETE FROM delay_profiles WHERE id = ?", (int(profile_id),))
            self.conn.commit()
        return "deleted"

    def reorder_delay_profiles(self, ids: list[int]) -> bool:
        """Applies ``ids`` as the order of the non-default profiles; False unless it names exactly those profiles."""
        with self._lock:
            default_ids = {int(r[0]) for r in self.conn.execute("SELECT id FROM delay_profiles WHERE is_default = 1")}
            current = {int(r[0]) for r in self.conn.execute("SELECT id FROM delay_profiles WHERE is_default = 0")}
            wanted = [int(i) for i in ids if int(i) not in default_ids]
            if len(wanted) != len(set(wanted)) or set(wanted) != current:
                return False
            for idx, pid in enumerate(wanted, start=1):
                self.conn.execute("UPDATE delay_profiles SET order_idx = ? WHERE id = ?", (idx, pid))
            self.conn.commit()
        return True

    # ------------------------------------------------------------------------------------------------------------
    # Pending releases
    # ------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _pending_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        rank = []
        try:
            parsed = json.loads(d.get("rank_json") or "[]")
            rank = parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            rank = []
        d["payload"] = _loads_obj(d.pop("payload_json", "{}"))
        d["rank"] = rank
        d.pop("rank_json", None)
        return d

    def get_pending_release(self, pending_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM pending_releases WHERE id = ?", (int(pending_id),)).fetchone()
        return self._pending_row(row) if row else None

    def get_pending_release_by_key(self, item_key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM pending_releases WHERE item_key = ?", (item_key,)).fetchone()
        return self._pending_row(row) if row else None

    def list_pending_releases(self, due_before: Optional[str] = None) -> list[dict[str, Any]]:
        """All pending releases by ``release_at``; with ``due_before`` only those whose window has elapsed."""
        with self._lock:
            if due_before is None:
                rows = self.conn.execute("SELECT * FROM pending_releases ORDER BY release_at, id").fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM pending_releases WHERE release_at <= ? ORDER BY release_at, id", (due_before,)
                ).fetchall()
        return [self._pending_row(r) for r in rows]

    def upsert_pending_release(self, row: dict[str, Any]) -> dict[str, Any]:
        """Inserts or replaces the pending release for ``item_key``, keeping its original ``added_at``."""
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO pending_releases (
                    item_key, title, artist_name, album, item_type, album_id, track_id, request_id, protocol, quality,
                    format_score, payload_json, rank_json, delay_profile_id, reason, added_at, release_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(item_key) DO UPDATE SET
                    title = excluded.title, artist_name = excluded.artist_name, album = excluded.album,
                    item_type = excluded.item_type, album_id = excluded.album_id, track_id = excluded.track_id,
                    request_id = excluded.request_id, protocol = excluded.protocol, quality = excluded.quality,
                    format_score = excluded.format_score, payload_json = excluded.payload_json,
                    rank_json = excluded.rank_json, delay_profile_id = excluded.delay_profile_id,
                    reason = excluded.reason, release_at = excluded.release_at, updated_at = CURRENT_TIMESTAMP
                """,
                (
                    row["item_key"],
                    row["title"],
                    row.get("artist_name") or "",
                    row.get("album"),
                    row.get("item_type") or "track",
                    row.get("album_id"),
                    row.get("track_id"),
                    row.get("request_id"),
                    row.get("protocol") or "",
                    row.get("quality"),
                    int(row.get("format_score") or 0),
                    json.dumps(row.get("payload") or {}),
                    json.dumps(row.get("rank") or []),
                    row.get("delay_profile_id"),
                    row.get("reason") or "",
                    row["added_at"],
                    row["release_at"],
                ),
            )
            self.conn.commit()
        result = self.get_pending_release_by_key(row["item_key"])
        assert result is not None
        return result

    def delete_pending_release(self, pending_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM pending_releases WHERE id = ?", (int(pending_id),))
            self.conn.commit()
            return cur.rowcount > 0

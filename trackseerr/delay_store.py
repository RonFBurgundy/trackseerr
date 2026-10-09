"""Persistence for delay profiles and the pending-releases queue (migration v50).

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import sqlite3
from typing import Any, Optional

from .tag_store import normalize_labels

logger = logging.getLogger(__name__)

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

    def _migration_v53(self, cur: sqlite3.Cursor) -> None:
        """Pending-release retry state (``attempts``, ``next_attempt_at``) and one canonical ``item_key`` per item.

        Keys become ``album:<id>`` > ``track:<id>`` > ``req:<id>`` (previously ``req:`` won). Rows that collapse onto the
        same key keep the earliest ``added_at`` (the delay window anchor); the others are deleted. Idempotent.
        """
        cols = {r[1] for r in cur.execute("PRAGMA table_info(pending_releases)").fetchall()}
        if "attempts" not in cols:
            cur.execute("ALTER TABLE pending_releases ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if "next_attempt_at" not in cols:
            cur.execute("ALTER TABLE pending_releases ADD COLUMN next_attempt_at TEXT")
        rows = cur.execute(
            "SELECT id, item_key, album_id, track_id, request_id FROM pending_releases ORDER BY added_at, id"
        ).fetchall()
        seen: set[str] = set()
        renames: list[tuple[str, int]] = []
        deletes: list[int] = []
        for rid, key, album_id, track_id, request_id in rows:
            if album_id:
                target = f"album:{album_id}"
            elif track_id:
                target = f"track:{track_id}"
            elif request_id:
                target = f"req:{request_id}"
            else:
                target = key
            if target in seen:
                deletes.append(int(rid))
                continue
            seen.add(target)
            if target != key:
                renames.append((target, int(rid)))
        for rid in deletes:
            cur.execute("DELETE FROM pending_releases WHERE id = ?", (rid,))
        for target, rid in renames:
            cur.execute("UPDATE pending_releases SET item_key = ? WHERE id = ?", (target, rid))

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
        """Appends a non-default profile after the existing ones (the default stays last).

        New tag labels are registered in the same transaction as the insert (rolled back together on failure).
        """
        delays = data["delays"]
        tags = normalize_labels(data.get("tags") or [])
        with self._lock:
            try:
                nxt = self.conn.execute(
                    "SELECT COALESCE(MAX(order_idx), 0) + 1 FROM delay_profiles WHERE is_default = 0"
                ).fetchone()[0]
                self._register_tag_labels(tags)  # type: ignore[attr-defined]
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
                        json.dumps(tags),
                    ),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                logger.exception("create_delay_profile failed; rolled back (no tags registered)")
                raise
            new_id = int(cur.lastrowid)
        result = self.get_delay_profile(new_id)
        assert result is not None
        return result

    def update_delay_profile(self, profile_id: int, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Updates a profile. The default profile keeps empty tags regardless of the payload.

        Tags are only registered once the profile is known to exist and is not the default, in the same transaction
        as the update (a missing or default profile registers nothing; a failed write rolls the tags back).
        """
        delays = data["delays"]
        new_tags = normalize_labels(data.get("tags") or [])
        with self._lock:
            row = self.conn.execute("SELECT is_default FROM delay_profiles WHERE id = ?", (int(profile_id),)).fetchone()
            if not row:
                return None
            tags = [] if row[0] else new_tags
            try:
                self._register_tag_labels(tags)  # type: ignore[attr-defined]
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
                self._recompute_pending_release_at(int(profile_id), delays)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                logger.exception("update_delay_profile(%s) failed; rolled back (no tags registered)", profile_id)
                raise
        return self.get_delay_profile(profile_id)

    def _recompute_pending_release_at(self, profile_id: int, delays: dict[str, Any]) -> None:
        """Re-anchors ``release_at`` of the rows parked under ``profile_id`` to ``added_at`` + the new delay.

        Caller holds ``self._lock`` and commits.
        """
        rows = self.conn.execute(
            "SELECT id, protocol, added_at FROM pending_releases WHERE delay_profile_id = ?", (profile_id,)
        ).fetchall()
        for rid, protocol, added_at in rows:
            minutes = max(0, int(delays.get(protocol or "", 0) or 0))
            try:
                base = datetime.strptime(added_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            release_at = (base + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.conn.execute(
                "UPDATE pending_releases SET release_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (release_at, int(rid)),
            )

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
        """All pending releases by ``release_at``; with ``due_before`` only those whose window has elapsed and whose retry backoff is over."""
        with self._lock:
            if due_before is None:
                rows = self.conn.execute("SELECT * FROM pending_releases ORDER BY release_at, id").fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM pending_releases WHERE release_at <= ? "
                    "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY release_at, id",
                    (due_before, due_before),
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
                    reason = excluded.reason, release_at = excluded.release_at, attempts = 0, next_attempt_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
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

    def find_pending_releases(
        self, request_id: Optional[str] = None, album_id: Optional[str] = None, track_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Pending rows matching ANY of the given identifiers, earliest ``added_at`` first."""
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("request_id", request_id), ("album_id", album_id), ("track_id", track_id)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(str(value))
        if not clauses:
            return []
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM pending_releases WHERE {' OR '.join(clauses)} ORDER BY added_at, id", params
            ).fetchall()
        return [self._pending_row(r) for r in rows]

    def clear_pending_for_item(
        self, request_id: Optional[str] = None, album_id: Optional[str] = None, track_id: Optional[str] = None
    ) -> int:
        """Deletes every pending row that shares a request, album or track id with the item; returns the count."""
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("request_id", request_id), ("album_id", album_id), ("track_id", track_id)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(str(value))
        if not clauses:
            return 0
        with self._lock:
            cur = self.conn.execute(f"DELETE FROM pending_releases WHERE {' OR '.join(clauses)}", params)
            self.conn.commit()
            return int(cur.rowcount)

    def claim_pending_release(self, pending_id: int) -> Optional[dict[str, Any]]:
        """Atomically removes and returns the row, or None when another grab path already claimed it.

        Every grab path (tick, endpoint, gate) must claim before dispatching so a release is grabbed once. On a failed
        grab call ``restore_pending_release``.
        """
        with self._lock:
            row = self.conn.execute("DELETE FROM pending_releases WHERE id = ? RETURNING *", (int(pending_id),)).fetchone()
            self.conn.commit()
        return self._pending_row(row) if row else None

    def restore_pending_release(
        self, row: dict[str, Any], attempts: Optional[int] = None, next_attempt_at: Optional[str] = None
    ) -> bool:
        """Re-inserts a claimed row after a failed grab. A newer row parked for the same item meanwhile wins."""
        with self._lock:
            cur = self.conn.execute(
                """
                INSERT OR IGNORE INTO pending_releases (
                    id, item_key, title, artist_name, album, item_type, album_id, track_id, request_id, protocol,
                    quality, format_score, payload_json, rank_json, delay_profile_id, reason, added_at, release_at,
                    attempts, next_attempt_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    row["id"],
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
                    int(row.get("attempts") if attempts is None else attempts),
                    row.get("next_attempt_at") if attempts is None else next_attempt_at,
                ),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def library_item_has_file(self, artist: str, title: str, album: Optional[str], item_type: str) -> bool:
        """True when the library already holds a file for the item (track by title, album by any track file)."""
        from trackseerr.storage import clean_library_name  # local: storage imports this mixin

        with self._lock:
            if item_type == "album":
                row = self.conn.execute(
                    """
                    SELECT 1 FROM library_albums al
                    JOIN library_artists a ON a.id = al.artist_id
                    JOIN library_tracks t ON t.album_id = al.id
                    JOIN library_files f ON f.track_id = t.id
                    WHERE a.clean_name = ? AND al.clean_title = ? LIMIT 1
                    """,
                    (clean_library_name(artist or ""), clean_library_name(album or title or "")),
                ).fetchone()
                return row is not None
        return self.library_track_has_file(artist, title)  # type: ignore[attr-defined]

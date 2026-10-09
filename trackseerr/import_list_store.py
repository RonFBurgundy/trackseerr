"""Playlist monitor mode and import lists persistence.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any, Optional

from trackseerr.library_monitoring import (
    validate_list_monitor_mode,
    validate_monitor_option,
)
from trackseerr.storage_common import (
    clean_library_name,
)
from trackseerr.tag_store import normalize_labels

logger = logging.getLogger(__name__)


class ImportListStoreMixin:
    # -------------------------------------------------------------------------
    # Playlist monitor mode and import lists
    # -------------------------------------------------------------------------

    def set_playlist_monitor_mode(self, playlist_id: str, mode: str) -> bool:
        """Sets a playlist's list monitor mode (``track``/``album``/``artist``/``none``); ValueError if invalid."""
        validated = validate_list_monitor_mode(mode)
        with self._lock:
            cur = self.conn.execute(
                "UPDATE playlists SET monitor_mode = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (validated, str(playlist_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def set_playlist_source(self, playlist_id: str, source_kind: Optional[str], source_ref: Optional[str]) -> bool:
        """Records where a listening playlist (Last.fm / ListenBrainz) pulls its tracks from."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE playlists SET source_kind = ?, source_ref = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (source_kind, source_ref, str(playlist_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def set_playlist_auto_request(self, playlist_id: str, auto_request: bool) -> bool:
        """Turns the per-playlist opt-in to automatically request missing tracks on or off."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE playlists SET auto_request = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (1 if auto_request else 0, str(playlist_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def mark_missing_tracks_list_applied(self, track_ids: list[int]) -> int:
        """Stamps missing tracks as handled by a list monitor mode so later syncs do not apply them again."""
        if not track_ids:
            return 0
        placeholders = ",".join("?" for _ in track_ids)
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE missing_tracks SET list_applied_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})",
                [int(t) for t in track_ids],
            )
            self.conn.commit()
            return cur.rowcount

    def mark_missing_track_artist_added(self, track_id: int) -> None:
        """Records that applying this missing track added its artist, so a retry finishes the artist's setup."""
        with self._lock:
            self.conn.execute("UPDATE missing_tracks SET artist_added_by_item = 1 WHERE id = ?", (int(track_id),))
            self.conn.commit()

    def has_open_track_request(self, artist: str, title: str) -> bool:
        """True when a pending/processing/approved request exists for the same artist and track title."""
        wanted = (title or "").strip().casefold()
        if not wanted:
            return False
        return any(
            str(r.get("title") or "").strip().casefold() == wanted
            for r in self.find_matching_processing_requests(artist, title=title)
        )

    def library_track_has_file(self, artist: str, title: str) -> bool:
        """True when the library holds a file for a track with this artist name and title."""
        with self._lock:
            row = self.conn.execute(
                """
                SELECT 1 FROM library_tracks t
                JOIN library_artists a ON a.id = t.artist_id
                JOIN library_files f ON f.track_id = t.id
                WHERE a.clean_name = ? AND t.clean_title = ? LIMIT 1
                """,
                (clean_library_name(artist or ""), clean_library_name(title or "")),
            ).fetchone()
        return row is not None

    _IMPORT_LIST_COLUMNS = (
        "id, name, provider, config_json, enabled, monitor_mode, artist_monitor_option, quality_profile_id, "
        "sync_interval_minutes, last_synced_at, last_status, last_error, created_at, updated_at, tags_json"
    )
    IMPORT_LIST_ITEM_STATUSES = ("pending", "applied", "unresolved", "skipped", "failed")

    @staticmethod
    def _map_import_list(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        try:
            cfg = json.loads(d.pop("config_json") or "{}")
        except ValueError:
            logger.warning("Import list %s has unreadable config_json; treating it as empty", d.get("id"))
            cfg = {}
        d["config"] = cfg if isinstance(cfg, dict) else {}
        try:
            tags = json.loads(d.pop("tags_json", None) or "[]")
        except ValueError:
            tags = []
        d["tags"] = [str(t) for t in tags] if isinstance(tags, list) else []
        return d

    def create_import_list(self, data: dict[str, Any]) -> dict[str, Any]:
        """Inserts an import list. ``data`` carries name, provider, config (dict) and the optional settings."""
        list_id = str(data.get("id") or uuid.uuid4())
        mode = validate_list_monitor_mode(data.get("monitor_mode", "track"))
        option = data.get("artist_monitor_option")
        if option is not None:
            option = validate_monitor_option(option)
        tags = normalize_labels(data.get("tags") or [])
        with self._lock:
            try:
                self._register_tag_labels(tags)
                self.conn.execute(
                    """
                    INSERT INTO import_lists (
                        id, name, provider, config_json, enabled, monitor_mode, artist_monitor_option,
                        quality_profile_id, sync_interval_minutes, tags_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        list_id,
                        str(data["name"]),
                        str(data["provider"]),
                        json.dumps(data.get("config") or {}),
                        1 if data.get("enabled", True) else 0,
                        mode,
                        option,
                        str(data["quality_profile_id"]) if data.get("quality_profile_id") else None,
                        int(data.get("sync_interval_minutes") or 1440),
                        json.dumps(tags),
                    ),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                logger.exception("create_import_list(%s) failed; rolled back (no tags registered)", list_id)
                raise
        created = self.get_import_list(list_id)
        if created is None:
            raise RuntimeError(f"Failed to create import list {list_id}")
        return created

    def get_import_list(self, list_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                f"SELECT {self._IMPORT_LIST_COLUMNS} FROM import_lists WHERE id = ?", (str(list_id),)
            ).fetchone()
        return self._map_import_list(row) if row else None

    def list_import_lists(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = f"SELECT {self._IMPORT_LIST_COLUMNS} FROM import_lists"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name COLLATE NOCASE ASC, created_at ASC"
        with self._lock:
            rows = self.conn.execute(sql).fetchall()
        return [self._map_import_list(r) for r in rows]

    def update_import_list(self, list_id: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Replaces the editable fields of an import list; returns the updated row or None if it does not exist."""
        mode = validate_list_monitor_mode(data.get("monitor_mode", "track"))
        option = data.get("artist_monitor_option")
        if option is not None:
            option = validate_monitor_option(option)
        tags = normalize_labels(data["tags"]) if "tags" in data else None
        with self._lock:
            prev = self.conn.execute("SELECT monitor_mode FROM import_lists WHERE id = ?", (str(list_id),)).fetchone()
            if prev is None:
                return None
            try:
                if tags is not None:
                    self._register_tag_labels(tags)
                self.conn.execute(
                    """
                    UPDATE import_lists SET name = ?, provider = ?, config_json = ?, enabled = ?, monitor_mode = ?,
                        artist_monitor_option = ?, quality_profile_id = ?, sync_interval_minutes = ?,
                        tags_json = COALESCE(?, tags_json), updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        str(data["name"]),
                        str(data["provider"]),
                        json.dumps(data.get("config") or {}),
                        1 if data.get("enabled", True) else 0,
                        mode,
                        option,
                        str(data["quality_profile_id"]) if data.get("quality_profile_id") else None,
                        int(data.get("sync_interval_minutes") or 1440),
                        json.dumps(tags) if tags is not None else None,
                        str(list_id),
                    ),
                )
                if str(prev[0]) != mode:
                    # A new mode may apply what "none" only recorded, so skipped items get another go.
                    self.conn.execute(
                        "UPDATE import_list_items SET status = 'pending', error = NULL, next_attempt_at = NULL "
                        "WHERE list_id = ? AND status = 'skipped'",
                        (str(list_id),),
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                logger.exception("update_import_list(%s) failed; rolled back (no tags registered)", list_id)
                raise
        return self.get_import_list(list_id)

    def delete_import_list(self, list_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM import_lists WHERE id = ?", (str(list_id),))
            self.conn.commit()
            return cur.rowcount > 0

    def set_import_list_sync_result(self, list_id: str, status: str, error: Optional[str] = None) -> None:
        """Records the outcome (``ok`` or ``error``) of a sync and stamps ``last_synced_at``."""
        if status not in ("ok", "error"):
            raise ValueError(f"Invalid import list status {status!r}")
        with self._lock:
            self.conn.execute(
                """
                UPDATE import_lists SET last_status = ?, last_error = ?, last_synced_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, error, str(list_id)),
            )
            self.conn.commit()

    def list_due_import_lists(self) -> list[dict[str, Any]]:
        """Enabled lists never synced, or whose interval has elapsed since ``last_synced_at``."""
        with self._lock:
            rows = self.conn.execute(
                f"""
                SELECT {self._IMPORT_LIST_COLUMNS} FROM import_lists
                WHERE enabled = 1 AND (
                    last_synced_at IS NULL
                    OR datetime(last_synced_at, '+' || sync_interval_minutes || ' minutes') <= datetime('now')
                )
                ORDER BY COALESCE(last_synced_at, '') ASC
                """
            ).fetchall()
        return [self._map_import_list(r) for r in rows]

    def import_list_item_counts(self, list_id: str) -> dict[str, int]:
        counts = {s: 0 for s in self.IMPORT_LIST_ITEM_STATUSES}
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) FROM import_list_items WHERE list_id = ? GROUP BY status", (str(list_id),)
            ).fetchall()
        for status, count in rows:
            counts[str(status)] = int(count)
        return counts

    def upsert_import_list_items(self, list_id: str, items: list[dict[str, Any]]) -> int:
        """Inserts new items as ``pending`` and refreshes ``last_seen_at`` of known ones; returns the new count.

        An existing item keeps its status and applied level, so an item already applied is never reset.
        """
        with self._lock:
            before = self.conn.execute(
                "SELECT COUNT(*) FROM import_list_items WHERE list_id = ?", (str(list_id),)
            ).fetchone()[0]
            for it in items:
                self.conn.execute(
                    """
                    INSERT INTO import_list_items (
                        list_id, kind, external_key, mbid, artist_mbid, artist_name, album_title, track_title
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(list_id, kind, external_key) DO UPDATE SET
                        last_seen_at = CURRENT_TIMESTAMP,
                        mbid = COALESCE(import_list_items.mbid, excluded.mbid),
                        artist_mbid = COALESCE(import_list_items.artist_mbid, excluded.artist_mbid)
                    """,
                    (
                        str(list_id),
                        str(it["kind"]),
                        str(it["external_key"]),
                        it.get("mbid"),
                        it.get("artist_mbid"),
                        str(it.get("artist_name") or ""),
                        str(it.get("album_title") or ""),
                        str(it.get("track_title") or ""),
                    ),
                )
            after = self.conn.execute(
                "SELECT COUNT(*) FROM import_list_items WHERE list_id = ?", (str(list_id),)
            ).fetchone()[0]
            self.conn.commit()
        return int(after - before)

    def list_import_list_items(
        self, list_id: str, status: Optional[str] = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        where = "list_id = ?"
        params: list[Any] = [str(list_id)]
        if status:
            where += " AND status = ?"
            params.append(status)
        with self._lock:
            total = self.conn.execute(f"SELECT COUNT(*) FROM import_list_items WHERE {where}", params).fetchone()[0]
            rows = self.conn.execute(
                f"""
                SELECT id, kind, mbid, artist_name, album_title, track_title, status, applied_level, error,
                       first_seen_at, last_seen_at
                FROM import_list_items WHERE {where} ORDER BY id ASC LIMIT ? OFFSET ?
                """,
                [*params, int(limit), int(offset)],
            ).fetchall()
        return [dict(r) for r in rows], int(total)

    # Failed items wait this long after the Nth failed attempt; after MAX_ATTEMPTS they stay failed.
    IMPORT_ITEM_FAILED_BACKOFF_HOURS = (1, 6, 24, 168, 168)
    IMPORT_ITEM_MAX_ATTEMPTS = 6
    IMPORT_ITEM_UNRESOLVED_RETRY_HOURS = 168

    def list_pending_import_items(self, list_id: str) -> list[dict[str, Any]]:
        """Items due for an apply attempt: pending ones, plus failed/unresolved ones whose retry time has come."""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, list_id, kind, external_key, mbid, artist_mbid, artist_name, album_title, track_title,
                       status, artist_added_by_item
                FROM import_list_items
                WHERE list_id = ? AND (
                    status = 'pending'
                    OR (status IN ('failed', 'unresolved') AND next_attempt_at IS NOT NULL
                        AND next_attempt_at <= datetime('now'))
                )
                ORDER BY id ASC
                """,
                (str(list_id),),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_import_item_artist_added(self, item_id: int) -> None:
        """Records that applying this item added its artist, so a retry finishes the artist's setup."""
        with self._lock:
            self.conn.execute("UPDATE import_list_items SET artist_added_by_item = 1 WHERE id = ?", (int(item_id),))
            self.conn.commit()

    def update_import_list_item(
        self,
        item_id: int,
        status: str,
        applied_level: Optional[str] = None,
        error: Optional[str] = None,
        mbid: Optional[str] = None,
    ) -> None:
        """Records an apply outcome and schedules the retry: see the retry policy in ``import_list_worker``."""
        if status not in self.IMPORT_LIST_ITEM_STATUSES:
            raise ValueError(f"Invalid import list item status {status!r}")
        with self._lock:
            row = self.conn.execute("SELECT attempts FROM import_list_items WHERE id = ?", (int(item_id),)).fetchone()
            if row is None:
                return
            attempts = int(row[0] or 0)
            delay_hours: Optional[int] = None
            if status == "failed":
                attempts += 1
                if attempts < self.IMPORT_ITEM_MAX_ATTEMPTS:
                    backoff = self.IMPORT_ITEM_FAILED_BACKOFF_HOURS
                    delay_hours = backoff[min(attempts, len(backoff)) - 1]
            elif status == "unresolved":
                delay_hours = self.IMPORT_ITEM_UNRESOLVED_RETRY_HOURS
            self.conn.execute(
                """
                UPDATE import_list_items
                SET status = ?, applied_level = ?, error = ?, mbid = COALESCE(?, mbid), attempts = ?,
                    next_attempt_at = CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', '+' || ? || ' hours') END
                WHERE id = ?
                """,
                (status, applied_level, error, mbid, attempts, delay_hours, delay_hours, int(item_id)),
            )
            self.conn.commit()


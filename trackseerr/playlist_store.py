"""Playlists, targets, Plex playlist control, sync results, missing tracks, and match overrides.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Union

from trackseerr.models import (
    Playlist,
    Track,
)
from trackseerr.storage_common import (
    LIDARR_WEEKLY_RETRY,
    _RETRY_TS_FORMAT,
    lidarr_retry_delay,
)

logger = logging.getLogger(__name__)


class PlaylistStoreMixin:
    # -------------------------------------------------------------------------
    # Playlists CRUD
    # -------------------------------------------------------------------------

    def upsert_playlist(
        self,
        playlist_id: Union[str, Playlist],
        name: Optional[str] = None,
        service: str = "spotify",
        description: str = "",
        poster_url: str = "",
        enabled: bool = True,
        sync_status: str = "never_synced",
        creator_id: Optional[str] = None,
        tracks_json: Optional[str] = None,
    ) -> dict[str, Any]:
        if hasattr(playlist_id, "id") and hasattr(playlist_id, "name"):
            p_id = str(playlist_id.id)
            p_name = str(playlist_id.name)
            p_desc = str(getattr(playlist_id, "description", "") or "")
            p_poster = str(getattr(playlist_id, "poster", "") or "")
        else:
            p_id = str(playlist_id)
            p_name = str(name or "")
            p_desc = str(description or "")
            p_poster = str(poster_url or "")

        enabled_val = 1 if enabled else 0
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO playlists (id, name, service, description, poster_url, enabled, sync_status, creator_id, tracks_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    service = excluded.service,
                    description = excluded.description,
                    poster_url = excluded.poster_url,
                    enabled = excluded.enabled,
                    creator_id = COALESCE(playlists.creator_id, excluded.creator_id),
                    tracks_json = COALESCE(excluded.tracks_json, playlists.tracks_json),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (p_id, p_name, service, p_desc, p_poster, enabled_val, sync_status, creator_id, tracks_json),
            )
            self.conn.commit()
        playlist = self.get_playlist(p_id)
        if playlist is None:
            raise RuntimeError(f"Failed to upsert playlist {p_id}")
        return playlist

    def get_playlist(self, playlist_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT id, name, service, description, poster_url, enabled, creator_id, tracks_json,
                       last_synced_at, sync_status, monitor_mode, source_kind, source_ref, auto_request, created_at, updated_at
                FROM playlists
                WHERE id = ?
                """,
                (str(playlist_id),),
            )
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            d["enabled"] = bool(d["enabled"])
            d["auto_request"] = bool(d["auto_request"])
            return d

    def list_playlists(
        self,
        user_id: Optional[str] = None,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            if user_id is not None:
                if enabled_only:
                    cur = self.conn.execute(
                        """
                        SELECT DISTINCT p.id, p.name, p.service, p.description, p.poster_url, p.enabled, p.creator_id, p.tracks_json,
                               p.last_synced_at, p.sync_status, p.monitor_mode, p.source_kind, p.source_ref, p.auto_request, p.created_at, p.updated_at
                        FROM playlists p
                        LEFT JOIN playlist_targets pt ON p.id = pt.playlist_id
                        WHERE (pt.user_id = ? OR p.creator_id = ?) AND p.enabled = 1
                        ORDER BY p.name ASC
                        """,
                        (str(user_id), str(user_id)),
                    )
                else:
                    cur = self.conn.execute(
                        """
                        SELECT DISTINCT p.id, p.name, p.service, p.description, p.poster_url, p.enabled, p.creator_id, p.tracks_json,
                               p.last_synced_at, p.sync_status, p.monitor_mode, p.source_kind, p.source_ref, p.auto_request, p.created_at, p.updated_at
                        FROM playlists p
                        LEFT JOIN playlist_targets pt ON p.id = pt.playlist_id
                        WHERE (pt.user_id = ? OR p.creator_id = ?)
                        ORDER BY p.name ASC
                        """,
                        (str(user_id), str(user_id)),
                    )
            else:
                if enabled_only:
                    cur = self.conn.execute(
                        """
                        SELECT id, name, service, description, poster_url, enabled, creator_id, tracks_json,
                               last_synced_at, sync_status, monitor_mode, source_kind, source_ref, auto_request, created_at, updated_at
                        FROM playlists
                        WHERE enabled = 1
                        ORDER BY name ASC
                        """
                    )
                else:
                    cur = self.conn.execute(
                        """
                        SELECT id, name, service, description, poster_url, enabled, creator_id, tracks_json,
                               last_synced_at, sync_status, monitor_mode, source_kind, source_ref, auto_request, created_at, updated_at
                        FROM playlists
                        ORDER BY name ASC
                        """
                    )

            results = []
            for row in cur.fetchall():
                d = dict(row)
                d["enabled"] = bool(d["enabled"])
                d["auto_request"] = bool(d["auto_request"])
                results.append(d)
            return results

    def delete_playlist(self, playlist_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM playlists WHERE id = ?",
                (str(playlist_id),),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def set_playlist_enabled(self, playlist_id: str, enabled: bool) -> bool:
        p_id = str(playlist_id)
        enabled_val = 1 if enabled else 0
        with self._lock:
            cur = self.conn.execute(
                "UPDATE playlists SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (enabled_val, p_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------------------
    # Playlist Targets
    # -------------------------------------------------------------------------

    def set_playlist_targets(self, playlist_id: str, user_ids: list[str]) -> None:
        p_id = str(playlist_id)
        unique_uids = list(dict.fromkeys(str(uid) for uid in user_ids))
        with self._lock:
            self.conn.execute(
                "DELETE FROM playlist_targets WHERE playlist_id = ?",
                (p_id,),
            )
            for u_id in unique_uids:
                self.conn.execute(
                    "INSERT INTO playlist_targets (playlist_id, user_id) VALUES (?, ?)",
                    (p_id, u_id),
                )
            self.conn.commit()

    def get_playlist_targets(self, playlist_id: str) -> list[str]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT user_id FROM playlist_targets WHERE playlist_id = ? ORDER BY user_id ASC",
                (str(playlist_id),),
            )
            return [row["user_id"] for row in cur.fetchall()]

    # -------------------------------------------------------------------------
    # Plex Playlist Control: registry + mix snapshots
    # -------------------------------------------------------------------------

    def get_plex_registry_row(self, plex_user: str, rating_key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM plex_playlist_registry WHERE plex_user = ? AND rating_key = ?",
                (str(plex_user).lower(), str(rating_key)),
            ).fetchone()
        return self._registry_to_dict(row) if row else None

    def list_plex_registry(self, plex_user: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM plex_playlist_registry WHERE plex_user = ? ORDER BY title COLLATE NOCASE ASC",
                (str(plex_user).lower(),),
            ).fetchall()
        return [self._registry_to_dict(r) for r in rows]

    def get_plex_registry_by_adopted(self, playlist_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM plex_playlist_registry WHERE trackseerr_playlist_id = ? LIMIT 1",
                (str(playlist_id),),
            ).fetchone()
        return self._registry_to_dict(row) if row else None

    @staticmethod
    def _registry_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["ignored"] = bool(d["ignored"])
        return d

    def upsert_plex_registry(
        self,
        plex_user: str,
        rating_key: str,
        title: str,
        kind: str,
        owner: str,
        ignored: Optional[bool] = None,
        trackseerr_playlist_id: Optional[str] = None,
        update_owner: bool = True,
    ) -> dict[str, Any]:
        """Insert or update a registry row.

        On conflict the title, kind and last_seen_at are refreshed. ``owner`` is only overwritten when
        ``update_owner`` is True; ``ignored`` and ``trackseerr_playlist_id`` only when provided.
        """
        user = str(plex_user).lower()
        key = str(rating_key)
        ignored_val = None if ignored is None else (1 if ignored else 0)
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO plex_playlist_registry
                    (plex_user, rating_key, title, kind, owner, ignored, trackseerr_playlist_id, last_seen_at)
                VALUES (?, ?, ?, ?, ?, COALESCE(?, 0), ?, CURRENT_TIMESTAMP)
                ON CONFLICT(plex_user, rating_key) DO UPDATE SET
                    title = excluded.title,
                    kind = excluded.kind,
                    owner = CASE WHEN ? = 1 THEN excluded.owner ELSE plex_playlist_registry.owner END,
                    ignored = COALESCE(?, plex_playlist_registry.ignored),
                    trackseerr_playlist_id = COALESCE(?, plex_playlist_registry.trackseerr_playlist_id),
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (
                    user,
                    key,
                    title,
                    kind,
                    owner,
                    ignored_val,
                    trackseerr_playlist_id,
                    1 if update_owner else 0,
                    ignored_val,
                    trackseerr_playlist_id,
                ),
            )
            self.conn.commit()
        row = self.get_plex_registry_row(user, key)
        if row is None:
            raise RuntimeError(f"Failed to upsert plex registry row {user}/{key}")
        return row

    def delete_plex_registry(self, plex_user: str, rating_key: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM plex_playlist_registry WHERE plex_user = ? AND rating_key = ?",
                (str(plex_user).lower(), str(rating_key)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def prune_plex_registry(self, plex_user: str, keep_rating_keys: list[str]) -> int:
        """Delete registry rows for playlists that no longer exist on Plex for this user."""
        keep = {str(k) for k in keep_rating_keys}
        removed = 0
        for row in self.list_plex_registry(plex_user):
            if row["rating_key"] not in keep:
                if self.delete_plex_registry(plex_user, row["rating_key"]):
                    removed += 1
        return removed

    def find_playlist_by_name_for_username(self, name: str, username: str) -> Optional[dict[str, Any]]:
        """Find a TrackSeerr playlist with this exact name that targets the given Plex username."""
        with self._lock:
            row = self.conn.execute(
                """
                SELECT p.id, p.name, p.service, p.last_synced_at, p.created_at
                FROM playlists p
                JOIN playlist_targets pt ON pt.playlist_id = p.id
                JOIN users u ON u.id = pt.user_id
                WHERE p.name = ? AND LOWER(u.username) = LOWER(?)
                LIMIT 1
                """,
                (name, username),
            ).fetchone()
        return dict(row) if row else None

    def set_playlist_tracks_json(self, playlist_id: str, tracks_json: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE playlists SET tracks_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (tracks_json, str(playlist_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _snapshot_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["auto_refresh"] = bool(d["auto_refresh"])
        return d

    def upsert_mix_snapshot(
        self,
        plex_user: str,
        mix_key: str,
        mix_title: str,
        playlist_title: str,
        rating_key: Optional[str],
        auto_refresh: bool,
        created_by: Optional[str] = None,
    ) -> dict[str, Any]:
        user = str(plex_user).lower()
        with self._lock:
            existing = self.conn.execute(
                "SELECT id FROM plex_mix_snapshots WHERE plex_user = ? AND mix_key = ?",
                (user, mix_key),
            ).fetchone()
            if existing:
                snap_id = existing["id"]
                self.conn.execute(
                    """
                    UPDATE plex_mix_snapshots
                    SET mix_title = ?, playlist_title = ?, rating_key = ?, auto_refresh = ?,
                        last_refreshed_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (mix_title, playlist_title, rating_key, 1 if auto_refresh else 0, snap_id),
                )
            else:
                snap_id = uuid.uuid4().hex
                self.conn.execute(
                    """
                    INSERT INTO plex_mix_snapshots
                        (id, plex_user, mix_key, mix_title, playlist_title, rating_key, auto_refresh,
                         last_refreshed_at, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                    """,
                    (snap_id, user, mix_key, mix_title, playlist_title, rating_key,
                     1 if auto_refresh else 0, created_by),
                )
            self.conn.commit()
        snap = self.get_mix_snapshot(snap_id)
        if snap is None:
            raise RuntimeError("Failed to upsert mix snapshot")
        return snap

    def get_mix_snapshot(self, snapshot_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM plex_mix_snapshots WHERE id = ?", (str(snapshot_id),)
            ).fetchone()
        return self._snapshot_to_dict(row) if row else None

    def list_mix_snapshots(
        self, plex_user: Optional[str] = None, auto_refresh_only: bool = False
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM plex_mix_snapshots"
        clauses: list[str] = []
        params: list[Any] = []
        if plex_user is not None:
            clauses.append("plex_user = ?")
            params.append(str(plex_user).lower())
        if auto_refresh_only:
            clauses.append("auto_refresh = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY mix_title COLLATE NOCASE ASC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._snapshot_to_dict(r) for r in rows]

    def set_mix_snapshot_auto_refresh(self, snapshot_id: str, auto_refresh: bool) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE plex_mix_snapshots SET auto_refresh = ? WHERE id = ?",
                (1 if auto_refresh else 0, str(snapshot_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def mark_mix_snapshot_refreshed(self, snapshot_id: str, rating_key: Optional[str] = None) -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE plex_mix_snapshots
                SET last_refreshed_at = CURRENT_TIMESTAMP, rating_key = COALESCE(?, rating_key)
                WHERE id = ?
                """,
                (rating_key, str(snapshot_id)),
            )
            self.conn.commit()

    def delete_mix_snapshot(self, snapshot_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM plex_mix_snapshots WHERE id = ?", (str(snapshot_id),))
            self.conn.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------------------
    # Sync Results & Missing Tracks
    # -------------------------------------------------------------------------

    def record_sync_result(
        self,
        playlist_id: str,
        status: str,
        missing_tracks: Optional[list[Union[Track, dict[str, Any]]]] = None,
    ) -> None:
        p_id = str(playlist_id)
        with self._lock:
            self.conn.execute(
                """
                UPDATE playlists
                SET sync_status = ?, last_synced_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (str(status), p_id),
            )
            # Preserve existing lidarr_status across sync cycles
            existing_lidarr_status: dict[tuple[str, str], str] = {}
            existing_applied: dict[tuple[str, str], str] = {}
            existing_retry: dict[tuple[str, str], tuple[int, Optional[str]]] = {}
            try:
                cur = self.conn.execute(
                    "SELECT title, artist, lidarr_status, list_applied_at, attempts, next_attempt_at "
                    "FROM missing_tracks WHERE playlist_id = ?",
                    (p_id,),
                )
                for r in cur.fetchall():
                    key = (str(r["title"]).strip().lower(), str(r["artist"]).strip().lower())
                    existing_lidarr_status[key] = str(r["lidarr_status"] or "unmonitored")
                    if r["list_applied_at"]:
                        existing_applied[key] = str(r["list_applied_at"])
                    existing_retry[key] = (int(r["attempts"] or 0), r["next_attempt_at"])
            except sqlite3.Error as exc:
                logger.warning("Could not read previous missing-track state for playlist %s: %s", p_id, exc)

            self.conn.execute(
                "DELETE FROM missing_tracks WHERE playlist_id = ?",
                (p_id,),
            )
            if missing_tracks:
                for item in missing_tracks:
                    if hasattr(item, "title") and hasattr(item, "artist"):
                        title = getattr(item, "title", "")
                        artist = getattr(item, "artist", "")
                        album = getattr(item, "album", "") or ""
                        url = getattr(item, "url", "") or ""
                    elif isinstance(item, dict):
                        title = item.get("title", "")
                        artist = item.get("artist", "")
                        album = item.get("album", "") or ""
                        url = item.get("url", "") or ""
                    else:
                        continue
                    key = (str(title).strip().lower(), str(artist).strip().lower())
                    l_status = existing_lidarr_status.get(key, "unmonitored")
                    attempts, next_at = existing_retry.get(key, (0, None))
                    self.conn.execute(
                        """
                        INSERT INTO missing_tracks (
                            playlist_id, title, artist, album, url, lidarr_status, list_applied_at, attempts, next_attempt_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            p_id, str(title), str(artist), str(album), str(url), l_status,
                            existing_applied.get(key), attempts, next_at,
                        ),
                    )
            self.conn.commit()

    def get_missing_tracks(
        self, playlist_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            if playlist_id is not None:
                cur = self.conn.execute(
                    """
                    SELECT id, playlist_id, title, artist, album, url, lidarr_status, list_applied_at, artist_added_by_item, attempts, next_attempt_at, created_at
                    FROM missing_tracks
                    WHERE playlist_id = ?
                    ORDER BY id ASC
                    """,
                    (str(playlist_id),),
                )
            else:
                cur = self.conn.execute(
                    """
                    SELECT id, playlist_id, title, artist, album, url, lidarr_status, list_applied_at, artist_added_by_item, attempts, next_attempt_at, created_at
                    FROM missing_tracks
                    ORDER BY id ASC
                    """
                )
            return [dict(row) for row in cur.fetchall()]

    def get_missing_track(self, track_id: int) -> Optional[dict[str, Any]]:
        """Retrieves a single missing track by ID."""
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT id, playlist_id, title, artist, album, url, lidarr_status, list_applied_at, artist_added_by_item, attempts, next_attempt_at, created_at
                FROM missing_tracks
                WHERE id = ?
                """,
                (int(track_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def update_missing_track_lidarr_status(self, track_id: int, status: str) -> bool:
        """Updates the Lidarr monitoring status for a specific missing track (and its retry schedule)."""
        return self.update_missing_tracks_lidarr_status_bulk([int(track_id)], status) > 0

    def update_missing_tracks_lidarr_status_bulk(self, track_ids: list[int], status: str) -> int:
        """Updates the Lidarr status for a list of track IDs and schedules their next attempt.

        ``error`` / ``rate_limited`` / ``unavailable`` / ``not_found`` count one more attempt and set
        ``next_attempt_at`` per ``lidarr_retry_delay``; any other status clears both.
        """
        if not track_ids:
            return 0
        status_val = str(status)
        ids = [int(tid) for tid in track_ids]
        now = datetime.now(timezone.utc)
        with self._lock:
            if lidarr_retry_delay(status_val, 1) is None:
                placeholders = ",".join("?" for _ in ids)
                cur = self.conn.execute(
                    "UPDATE missing_tracks SET lidarr_status = ?, attempts = 0, next_attempt_at = NULL "
                    f"WHERE id IN ({placeholders})",
                    [status_val, *ids],
                )
                self.conn.commit()
                return cur.rowcount
            updated = 0
            for tid in ids:
                row = self.conn.execute("SELECT attempts FROM missing_tracks WHERE id = ?", (tid,)).fetchone()
                if row is None:
                    continue
                attempts = int(row["attempts"] or 0) + 1
                delay = lidarr_retry_delay(status_val, attempts) or LIDARR_WEEKLY_RETRY
                cur = self.conn.execute(
                    "UPDATE missing_tracks SET lidarr_status = ?, attempts = ?, next_attempt_at = ? WHERE id = ?",
                    (status_val, attempts, (now + delay).strftime(_RETRY_TS_FORMAT), tid),
                )
                updated += cur.rowcount
            self.conn.commit()
            return updated

    # -------------------------------------------------------------------------
    # Match Overrides (Match Memory)
    # -------------------------------------------------------------------------

    def add_match_override(
        self,
        source_title: str,
        source_artist: str,
        plex_rating_key: str,
        plex_title: str,
        plex_artist: str,
        created_by: Optional[str] = None,
    ) -> dict[str, Any]:
        s_title = str(source_title).strip()
        s_artist = str(source_artist).strip()
        r_key = str(plex_rating_key).strip()
        p_title = str(plex_title).strip()
        p_artist = str(plex_artist).strip()
        c_by = str(created_by) if created_by else None

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO match_overrides (source_title, source_artist, plex_rating_key, plex_title, plex_artist, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source_title, source_artist) DO UPDATE SET
                    plex_rating_key = excluded.plex_rating_key,
                    plex_title = excluded.plex_title,
                    plex_artist = excluded.plex_artist,
                    created_by = excluded.created_by,
                    created_at = CURRENT_TIMESTAMP
                """,
                (s_title, s_artist, r_key, p_title, p_artist, c_by),
            )
            self.conn.commit()
            cur = self.conn.execute(
                "SELECT id, source_title, source_artist, plex_rating_key, plex_title, plex_artist, created_by, created_at FROM match_overrides WHERE source_title = ? AND source_artist = ?",
                (s_title, s_artist),
            )
            row = cur.fetchone()
            return dict(row) if row else {}

    def get_match_override(
        self, source_title: str, source_artist: str
    ) -> Optional[dict[str, Any]]:
        s_title = str(source_title).strip()
        s_artist = str(source_artist).strip()
        with self._lock:
            cur = self.conn.execute(
                "SELECT id, source_title, source_artist, plex_rating_key, plex_title, plex_artist, created_by, created_at FROM match_overrides WHERE LOWER(source_title) = LOWER(?) AND LOWER(source_artist) = LOWER(?)",
                (s_title, s_artist),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def list_match_overrides(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT id, source_title, source_artist, plex_rating_key, plex_title, plex_artist, created_by, created_at FROM match_overrides ORDER BY created_at DESC"
            )
            return [dict(r) for r in cur.fetchall()]

    def delete_match_override(self, override_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM match_overrides WHERE id = ?",
                (int(override_id),),
            )
            self.conn.commit()
            return cur.rowcount > 0


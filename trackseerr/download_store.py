"""Download clients, indexers, active downloads, and download blocklist.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any, Optional, Union

from trackseerr.models import (
    ActiveDownload,
    DownloadClientConfig,
    IndexerConfig,
)
from trackseerr.storage_common import (
    _opt_float,
    _opt_int,
    clean_library_name,
)

logger = logging.getLogger(__name__)


class DownloadStoreMixin:
    # -------------------------------------------------------------------------
    # Download Clients CRUD
    # -------------------------------------------------------------------------

    def create_download_client(
        self, client: Union[dict[str, Any], DownloadClientConfig]
    ) -> dict[str, Any]:
        """Creates or updates a download client in the database."""
        c = client.to_dict() if isinstance(client, DownloadClientConfig) else dict(client)
        cid = str(c.get("id") or "")
        name = str(c.get("name") or "")
        driver_type = str(c.get("driver_type") or "")
        host_url = str(c.get("host_url") or "")
        api_key = c.get("api_key")
        username = c.get("username")
        password = c.get("password")
        enabled = 1 if c.get("enabled", True) else 0
        priority = int(c.get("priority", 1))
        extra_settings_json = c.get("extra_settings_json")
        if isinstance(c.get("extra_settings"), dict):
            extra_settings_json = json.dumps(c["extra_settings"])

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO download_clients (
                    id, name, driver_type, host_url, api_key, username, password,
                    enabled, priority, extra_settings_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    driver_type = excluded.driver_type,
                    host_url = excluded.host_url,
                    api_key = excluded.api_key,
                    username = excluded.username,
                    password = excluded.password,
                    enabled = excluded.enabled,
                    priority = excluded.priority,
                    extra_settings_json = excluded.extra_settings_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    cid,
                    name,
                    driver_type,
                    host_url,
                    api_key,
                    username,
                    password,
                    enabled,
                    priority,
                    extra_settings_json,
                ),
            )
            self.conn.commit()
        return self.get_download_client(cid) or {}

    def get_download_client(self, client_id: str) -> Optional[dict[str, Any]]:
        """Retrieves a single download client by ID."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM download_clients WHERE id = ?", (str(client_id),)
            )
            row = cur.fetchone()
            if not row:
                return None
            res = dict(row)
            res["enabled"] = bool(res.get("enabled", 1))
            res["priority"] = int(res.get("priority", 1))
            return res

    def list_download_clients(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """Lists download clients, optionally filtering for enabled only."""
        with self._lock:
            if enabled_only:
                cur = self.conn.execute(
                    "SELECT * FROM download_clients WHERE enabled = 1 ORDER BY priority ASC, created_at ASC"
                )
            else:
                cur = self.conn.execute(
                    "SELECT * FROM download_clients ORDER BY priority ASC, created_at ASC"
                )
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["enabled"] = bool(r.get("enabled", 1))
            r["priority"] = int(r.get("priority", 1))
        return rows

    def update_download_client(
        self, client_id: str, updates: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Updates download client fields."""
        allowed = {
            "name",
            "driver_type",
            "host_url",
            "api_key",
            "username",
            "password",
            "enabled",
            "priority",
            "extra_settings_json",
        }
        filtered: dict[str, Any] = {}
        for k, v in updates.items():
            if k in allowed:
                if k == "enabled":
                    filtered[k] = 1 if v else 0
                elif k == "priority":
                    filtered[k] = int(v)
                else:
                    filtered[k] = v

        if not filtered:
            return self.get_download_client(client_id)

        set_clauses = [f"{k} = ?" for k in filtered.keys()]
        set_clauses.append("updated_at = CURRENT_TIMESTAMP")
        values = list(filtered.values())
        values.append(str(client_id))

        with self._lock:
            cur = self.conn.execute(
                f"UPDATE download_clients SET {', '.join(set_clauses)} WHERE id = ?",
                values,
            )
            self.conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_download_client(client_id)

    def delete_download_client(self, client_id: str) -> bool:
        """Deletes a download client by ID."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM download_clients WHERE id = ?", (str(client_id),)
            )
            self.conn.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------------------
    # Indexers CRUD
    # -------------------------------------------------------------------------

    def create_indexer(
        self, indexer: Union[dict[str, Any], IndexerConfig]
    ) -> dict[str, Any]:
        """Creates or updates an indexer."""
        idx = indexer.to_dict() if isinstance(indexer, IndexerConfig) else dict(indexer)
        iid = str(idx.get("id") or "")
        name = str(idx.get("name") or "")
        indexer_type = str(idx.get("indexer_type") or "torznab")
        host_url = str(idx.get("host_url") or "")
        api_key = idx.get("api_key")
        categories = str(idx.get("categories") or "3000,3010,3020,3030,3040")
        enabled = 1 if idx.get("enabled", True) else 0
        priority = int(idx.get("priority", 1))
        seed_ratio = _opt_float(idx.get("seed_ratio"))
        seed_time = _opt_int(idx.get("seed_time_minutes"))
        disco_time = _opt_int(idx.get("discography_seed_time_minutes"))
        min_seeders = _opt_int(idx.get("minimum_seeders"))

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO indexers (
                    id, name, indexer_type, host_url, api_key, categories,
                    enabled, priority, seed_ratio, seed_time_minutes,
                    discography_seed_time_minutes, minimum_seeders, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    indexer_type = excluded.indexer_type,
                    host_url = excluded.host_url,
                    api_key = excluded.api_key,
                    categories = excluded.categories,
                    enabled = excluded.enabled,
                    priority = excluded.priority,
                    seed_ratio = excluded.seed_ratio,
                    seed_time_minutes = excluded.seed_time_minutes,
                    discography_seed_time_minutes = excluded.discography_seed_time_minutes,
                    minimum_seeders = excluded.minimum_seeders,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    iid, name, indexer_type, host_url, api_key, categories, enabled, priority,
                    seed_ratio, seed_time, disco_time, min_seeders,
                ),
            )
            self.conn.commit()
        return self.get_indexer(iid) or {}

    def get_indexer(self, indexer_id: str) -> Optional[dict[str, Any]]:
        """Retrieves a single indexer by ID."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM indexers WHERE id = ?", (str(indexer_id),)
            )
            row = cur.fetchone()
            if not row:
                return None
            res = dict(row)
            res["enabled"] = bool(res.get("enabled", 1))
            res["priority"] = int(res.get("priority", 1))
            return res

    def list_indexers(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """Lists indexers, optionally filtering for enabled only."""
        with self._lock:
            if enabled_only:
                cur = self.conn.execute(
                    "SELECT * FROM indexers WHERE enabled = 1 ORDER BY priority ASC, created_at ASC"
                )
            else:
                cur = self.conn.execute(
                    "SELECT * FROM indexers ORDER BY priority ASC, created_at ASC"
                )
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["enabled"] = bool(r.get("enabled", 1))
            r["priority"] = int(r.get("priority", 1))
        return rows

    def update_indexer(
        self, indexer_id: str, updates: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Updates indexer fields."""
        allowed = {
            "name", "indexer_type", "host_url", "api_key", "categories", "enabled", "priority",
            "seed_ratio", "seed_time_minutes", "discography_seed_time_minutes", "minimum_seeders",
        }
        filtered: dict[str, Any] = {}
        for k, v in updates.items():
            if k in allowed:
                if k == "enabled":
                    filtered[k] = 1 if v else 0
                elif k == "priority":
                    filtered[k] = int(v)
                elif k == "seed_ratio":
                    filtered[k] = _opt_float(v)
                elif k in ("seed_time_minutes", "discography_seed_time_minutes", "minimum_seeders"):
                    filtered[k] = _opt_int(v)
                else:
                    filtered[k] = v

        if not filtered:
            return self.get_indexer(indexer_id)

        set_clauses = [f"{k} = ?" for k in filtered.keys()]
        set_clauses.append("updated_at = CURRENT_TIMESTAMP")
        values = list(filtered.values())
        values.append(str(indexer_id))

        with self._lock:
            cur = self.conn.execute(
                f"UPDATE indexers SET {', '.join(set_clauses)} WHERE id = ?",
                values,
            )
            self.conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_indexer(indexer_id)

    def delete_indexer(self, indexer_id: str) -> bool:
        """Deletes an indexer by ID."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM indexers WHERE id = ?", (str(indexer_id),)
            )
            self.conn.commit()
            return cur.rowcount > 0

    # Using SQLite rowid as integer ID for Lidarr compatibility
    # (stable: the app never VACUUMs and backups use the sqlite backup API)
    def get_indexer_by_rowid(self, rowid: int) -> Optional[dict[str, Any]]:
        """Retrieves a single indexer by SQLite rowid."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT rowid, * FROM indexers WHERE rowid = ?", (int(rowid),)
            )
            row = cur.fetchone()
            if not row:
                return None
            res = dict(row)
            res["enabled"] = bool(res.get("enabled", 1))
            res["priority"] = int(res.get("priority", 1))
            return res

    def get_indexer_rowid(self, indexer_id: str) -> Optional[int]:
        """Returns the SQLite rowid for an indexer by its UUID id."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT rowid FROM indexers WHERE id = ?", (str(indexer_id),)
            )
            row = cur.fetchone()
            return int(row[0]) if row else None

    def list_indexers_with_rowid(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """Lists indexers with rowid included, ordered by priority ASC, created_at ASC."""
        with self._lock:
            if enabled_only:
                cur = self.conn.execute(
                    "SELECT rowid, * FROM indexers WHERE enabled = 1 ORDER BY priority ASC, created_at ASC"
                )
            else:
                cur = self.conn.execute(
                    "SELECT rowid, * FROM indexers ORDER BY priority ASC, created_at ASC"
                )
            rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["enabled"] = bool(r.get("enabled", 1))
            r["priority"] = int(r.get("priority", 1))
        return rows

    def delete_indexer_by_rowid(self, rowid: int) -> bool:
        """Deletes an indexer by SQLite rowid."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM indexers WHERE rowid = ?", (int(rowid),)
            )
            self.conn.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------------------
    # Active Downloads CRUD
    # -------------------------------------------------------------------------

    def _map_active_download(self, row: sqlite3.Row) -> dict[str, Any]:
        res = dict(row)
        res["progress"] = float(res.get("progress") or 0.0)
        res["size_bytes"] = int(res.get("size_bytes") or 0)
        res["track_id"] = res.get("track_id")
        res["album_id"] = res.get("album_id")
        res["unmatched_files"] = self._parse_unmatched_files(res.get("unmatched_files"))
        res["placed_files"], res["placed_mode"] = self._parse_placed_files(res.get("placed_files"))
        res["cleanup_attempts"] = int(res.get("cleanup_attempts") or 0)
        return res

    @staticmethod
    def _parse_unmatched_files(raw: Any) -> list[str]:
        """The persisted JSON list of held file paths; a missing or corrupt value reads as an empty list."""
        if not raw:
            return []
        if isinstance(raw, list):
            return [str(p) for p in raw]
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.warning("Ignoring corrupt unmatched_files value: %s", type(exc).__name__)
            return []
        return [str(p) for p in data] if isinstance(data, list) else []

    @staticmethod
    def _parse_placed_files(raw: Any) -> tuple[list[str], Optional[str]]:
        """The persisted placed-files record ``{"mode": ..., "files": [...]}``; missing or corrupt reads as ([], None)."""
        if not raw:
            return [], None
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError) as exc:
            logger.warning("Ignoring corrupt placed_files value: %s", type(exc).__name__)
            return [], None
        if not isinstance(data, dict) or not isinstance(data.get("files"), list):
            return [], None
        mode = data.get("mode")
        return [str(p) for p in data["files"]], str(mode) if mode else None

    def set_download_unmatched_files(self, download_id: str, paths: list[str]) -> bool:
        """Persists the files a native download is holding for manual import (an empty list clears them)."""
        value = json.dumps([str(p) for p in paths]) if paths else None
        with self._lock:
            cur = self.conn.execute(
                "UPDATE active_downloads SET unmatched_files = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (value, str(download_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def set_download_placed_files(self, download_id: str, paths: list[str], mode: Optional[str] = None) -> bool:
        """Records the library files an import placed for a download and the import mode used (seed-cleanup safety gate)."""
        value = json.dumps({"mode": mode, "files": [str(p) for p in paths]}) if paths else None
        with self._lock:
            cur = self.conn.execute(
                "UPDATE active_downloads SET placed_files = ? WHERE id = ?", (value, str(download_id))
            )
            self.conn.commit()
            return cur.rowcount > 0

    def add_download_placed_files(self, download_id: str, paths: list[str], mode: Optional[str] = None) -> bool:
        """Appends to a download's placed-file record (manual import commits in batches); no duplicates.

        A ``move`` anywhere makes the whole record ``move``; an unknown mode on either side leaves it unknown; a
        hardlink/copy mix is recorded as ``hardlink`` (the stricter check).
        """
        row = self.get_active_download(download_id)
        if row is None:
            return False
        merged = list(dict.fromkeys([*row.get("placed_files", []), *[str(p) for p in paths]]))
        old_mode = row.get("placed_mode") if row.get("placed_files") else mode
        if "move" in (old_mode, mode):
            new_mode: Optional[str] = "move"
        elif old_mode and mode:
            new_mode = old_mode if old_mode == mode else "hardlink"  # strictest of the two safe modes
        else:
            new_mode = None
        return self.set_download_placed_files(download_id, merged, new_mode)

    def record_cleanup_result(self, download_id: str, attempts: int, error: Optional[str]) -> None:
        """Stores the seed-cleanup retry counter and last error for a download (``attempts=0, error=None`` resets)."""
        with self._lock:
            self.conn.execute(
                "UPDATE active_downloads SET cleanup_attempts = ?, cleanup_error = ? WHERE id = ?",
                (int(attempts), error, str(download_id)),
            )
            self.conn.commit()

    def get_active_download_by_hash(self, download_hash: str) -> Optional[dict[str, Any]]:
        """Newest download row (any status) whose client hash matches, case-insensitively."""
        with self._lock:
            row = self.conn.execute(
                """
                SELECT d.*, c.name AS client_name, c.driver_type AS client_driver_type
                FROM active_downloads d
                LEFT JOIN download_clients c ON d.client_id = c.id
                WHERE lower(d.download_hash) = ?
                ORDER BY d.created_at DESC LIMIT 1
                """,
                (str(download_hash).lower(),),
            ).fetchone()
            return self._map_active_download(row) if row else None

    def history_has_hash(self, download_hash: str) -> bool:
        """True when ``download_history`` knows a release with this torrent hash (import history match)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM download_history WHERE lower(info_hash) = ? LIMIT 1", (str(download_hash).lower(),)
            ).fetchone()
        return row is not None

    def create_active_download(
        self, download: Union[dict[str, Any], ActiveDownload]
    ) -> dict[str, Any]:
        """Creates or updates an active download record."""
        d = download.to_dict() if isinstance(download, ActiveDownload) else dict(download)
        did = str(d.get("id") or "")
        req_id = d.get("request_id")
        client_id = str(d.get("client_id") or "")
        download_hash = d.get("download_hash")
        title = str(d.get("title") or "")
        artist = str(d.get("artist") or "")
        item_type = str(d.get("item_type") or "track")
        status = str(d.get("status") or "queued")
        progress = float(d.get("progress") or 0.0)
        size_bytes = int(d.get("size_bytes") or 0)
        source_path = d.get("source_path")
        target_path = d.get("target_path")
        error_message = d.get("error_message")
        track_id = d.get("track_id")
        album_id = d.get("album_id")

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO active_downloads (
                    id, request_id, client_id, download_hash, title, artist,
                    item_type, status, progress, size_bytes, source_path,
                    target_path, error_message, track_id, album_id, updated_at, progress_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    request_id = excluded.request_id,
                    client_id = excluded.client_id,
                    download_hash = excluded.download_hash,
                    title = excluded.title,
                    artist = excluded.artist,
                    item_type = excluded.item_type,
                    status = excluded.status,
                    progress = excluded.progress,
                    size_bytes = excluded.size_bytes,
                    source_path = excluded.source_path,
                    target_path = excluded.target_path,
                    error_message = excluded.error_message,
                    track_id = excluded.track_id,
                    album_id = excluded.album_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    did,
                    req_id,
                    client_id,
                    download_hash,
                    title,
                    artist,
                    item_type,
                    status,
                    progress,
                    size_bytes,
                    source_path,
                    target_path,
                    error_message,
                    track_id,
                    album_id,
                ),
            )
            self.conn.commit()
        return self.get_active_download(did) or {}

    def get_active_download(self, download_id: str) -> Optional[dict[str, Any]]:
        """Retrieves active download by ID with client details joined."""
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT d.*, c.name AS client_name, c.driver_type AS client_driver_type
                FROM active_downloads d
                LEFT JOIN download_clients c ON d.client_id = c.id
                WHERE d.id = ?
                """,
                (str(download_id),),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._map_active_download(row)

    def list_active_downloads(
        self, statuses: Optional[list[str]] = None
    ) -> list[dict[str, Any]]:
        """Lists active downloads, optionally filtered by a list of statuses."""
        with self._lock:
            if statuses:
                placeholders = ", ".join(["?"] * len(statuses))
                cur = self.conn.execute(
                    f"""
                    SELECT d.*, c.name AS client_name, c.driver_type AS client_driver_type
                    FROM active_downloads d
                    LEFT JOIN download_clients c ON d.client_id = c.id
                    WHERE d.status IN ({placeholders})
                    ORDER BY d.created_at DESC
                    """,
                    [str(s).lower() for s in statuses],
                )
            else:
                cur = self.conn.execute(
                    """
                    SELECT d.*, c.name AS client_name, c.driver_type AS client_driver_type
                    FROM active_downloads d
                    LEFT JOIN download_clients c ON d.client_id = c.id
                    ORDER BY d.created_at DESC
                    """
                )
            return [self._map_active_download(r) for r in cur.fetchall()]

    def update_download_progress(
        self, download_id: str, progress: float, size_bytes: Optional[int] = None
    ) -> bool:
        """Updates download progress and optional size."""
        with self._lock:
            if size_bytes is not None:
                cur = self.conn.execute(
                    """
                    UPDATE active_downloads
                    SET progress_updated_at = CASE WHEN progress <> ? OR progress_updated_at IS NULL
                                                   THEN CURRENT_TIMESTAMP ELSE progress_updated_at END,
                        progress = ?, size_bytes = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (float(progress), float(progress), int(size_bytes), str(download_id)),
                )
            else:
                cur = self.conn.execute(
                    """
                    UPDATE active_downloads
                    SET progress_updated_at = CASE WHEN progress <> ? OR progress_updated_at IS NULL
                                                   THEN CURRENT_TIMESTAMP ELSE progress_updated_at END,
                        progress = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (float(progress), float(progress), str(download_id)),
                )
            self.conn.commit()
            return cur.rowcount > 0

    def update_download_status(
        self,
        download_id: str,
        status: str,
        error_message: Optional[str] = None,
        source_path: Optional[str] = None,
        target_path: Optional[str] = None,
    ) -> bool:
        """Updates status and paths / error for an active download."""
        new_status = str(status).lower()
        updates = [
            "status = ?",
            "updated_at = CURRENT_TIMESTAMP",
            # A status change restarts the stall clock (e.g. queued -> downloading).
            "progress_updated_at = CASE WHEN status <> ? THEN CURRENT_TIMESTAMP ELSE progress_updated_at END",
        ]
        params: list[Any] = [new_status, new_status]
        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)
        if source_path is not None:
            updates.append("source_path = ?")
            params.append(source_path)
        if target_path is not None:
            updates.append("target_path = ?")
            params.append(target_path)

        params.append(str(download_id))
        query = f"UPDATE active_downloads SET {', '.join(updates)} WHERE id = ?"
        with self._lock:
            previous = self.conn.execute(
                "SELECT status FROM active_downloads WHERE id = ?", (str(download_id),)
            ).fetchone()
            cur = self.conn.execute(query, params)
            self.conn.commit()
            changed = cur.rowcount > 0
            # Every terminal transition lands in the append-only history exactly once, whichever worker path made it.
            if (
                changed
                and previous is not None
                and previous["status"] != new_status
                and new_status in ("failed", "imported")
            ):
                try:
                    self.record_download_event(
                        "failed" if new_status == "failed" else "imported",
                        download_id=str(download_id),
                        message=error_message if new_status == "failed" else None,
                    )
                except sqlite3.Error as exc:
                    logger.warning("Failed to record download history for %s: %s", download_id, type(exc).__name__)
                if new_status == "failed":
                    self.record_download_item_event(
                        "download_failed", str(download_id), message=error_message or "", count_failures=True
                    )
            return changed

    def delete_active_download(self, download_id: str) -> bool:
        """Deletes an active download entry."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM active_downloads WHERE id = ?", (str(download_id),)
            )
            self.conn.commit()
            return cur.rowcount > 0

    # -------------------------------------------------------------------------
    # Download Blocklist CRUD
    # -------------------------------------------------------------------------

    def add_to_blocklist(
        self,
        source_title: str,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        release_guid: Optional[str] = None,
        info_hash: Optional[str] = None,
        protocol: Optional[str] = None,
        indexer: Optional[str] = None,
        reason: Optional[str] = None,
        download_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Adds a release to the persistent download blocklist.

        ``download_id`` (the download the release came from) lets the ``blocklisted`` item event name the item.
        """
        item_id = f"bl-{uuid.uuid4().hex[:12]}"
        clean_hash = info_hash.strip().lower() if info_hash else None
        clean_guid = release_guid.strip() if release_guid else None
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO download_blocklist (
                    id, source_title, artist, album, release_guid, info_hash,
                    protocol, indexer, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    item_id,
                    str(source_title).strip(),
                    str(artist).strip() if artist else None,
                    str(album).strip() if album else None,
                    clean_guid,
                    clean_hash,
                    str(protocol).strip().lower() if protocol else None,
                    str(indexer).strip() if indexer else None,
                    str(reason).strip() if reason else None,
                ),
            )
            self.conn.commit()
        item = self.get_blocklist_item(item_id)
        if item is None:
            raise RuntimeError(f"Failed to retrieve blocklist item {item_id}")
        try:
            self.record_download_event(
                "blocklisted",
                artist=item.get("artist"),
                album=item.get("album"),
                title=item.get("album") or item.get("source_title"),
                release_title=item.get("source_title"),
                indexer=item.get("indexer"),
                protocol=item.get("protocol"),
                info_hash=item.get("info_hash"),
                release_guid=item.get("release_guid"),
                message=item.get("reason"),
            )
        except sqlite3.Error as exc:
            logger.warning("Failed to record blocklist history event: %s", type(exc).__name__)
        details = {
            "release": item.get("source_title"), "reason": item.get("reason"), "indexer": item.get("indexer"),
            "protocol": item.get("protocol"), "info_hash": item.get("info_hash"),
            "release_guid": item.get("release_guid"),
        }
        message = f"Blocklisted '{item.get('source_title')}'"
        recorded = (
            self.record_download_item_event("blocklisted", download_id, message=message, details=details)
            if download_id else None
        )
        if recorded is None:
            try:
                ids = self.find_library_ids_by_name(item.get("artist"), item.get("album"))
                if ids["album_id"] or ids["artist_id"]:
                    self.record_item_event(
                        "blocklisted", album_id=ids["album_id"], artist_id=ids["artist_id"], message=message,
                        details=details,
                    )
            except sqlite3.Error:
                logger.exception("Could not record the blocklisted item event")
        return item

    def get_blocklist_item(self, blocklist_id: str) -> Optional[dict[str, Any]]:
        """Retrieves a single blocklist entry by ID."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM download_blocklist WHERE id = ?",
                (str(blocklist_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def is_blocklisted(
        self,
        release_title: Optional[str] = None,
        release_guid: Optional[str] = None,
        info_hash: Optional[str] = None,
    ) -> bool:
        """Checks if a release matches blocklist by info_hash (case-insensitive), guid, or title."""
        with self._lock:
            if info_hash:
                clean_hash = str(info_hash).strip()
                cur = self.conn.execute(
                    "SELECT 1 FROM download_blocklist WHERE LOWER(info_hash) = LOWER(?) LIMIT 1",
                    (clean_hash,),
                )
                if cur.fetchone():
                    return True

            if release_guid:
                clean_guid = str(release_guid).strip()
                cur = self.conn.execute(
                    "SELECT 1 FROM download_blocklist WHERE release_guid = ? LIMIT 1",
                    (clean_guid,),
                )
                if cur.fetchone():
                    return True

            if release_title:
                clean_rt = str(release_title).strip()
                cur = self.conn.execute(
                    "SELECT 1 FROM download_blocklist WHERE LOWER(source_title) = LOWER(?) LIMIT 1",
                    (clean_rt,),
                )
                if cur.fetchone():
                    return True

                target_clean = clean_library_name(clean_rt)
                if target_clean:
                    cur = self.conn.execute(
                        "SELECT source_title FROM download_blocklist WHERE source_title IS NOT NULL"
                    )
                    for row in cur.fetchall():
                        st = row["source_title"] or ""
                        if clean_library_name(st) == target_clean:
                            return True

        return False

    def list_blocklist(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Returns paginated download blocklist items ordered by created_at DESC."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM download_blocklist ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            )
            return [dict(r) for r in cur.fetchall()]

    def remove_from_blocklist(self, blocklist_id: str) -> bool:
        """Removes a download blocklist entry by ID."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM download_blocklist WHERE id = ?",
                (str(blocklist_id),),
            )
            self.conn.commit()
            return cur.rowcount > 0


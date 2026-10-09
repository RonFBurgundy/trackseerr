"""Activity queue/history, wanted lists, catalog wanted queries, and system events.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from trackseerr.item_history import GrabTrigger, trigger_kwargs
from trackseerr.list_index import SortDef, build_index
from trackseerr.storage_common import (
    REPLACEMENT_MESSAGE_PREFIX,
)

logger = logging.getLogger(__name__)


class ActivityStoreMixin:
    # -------------------------------------------------------------------------
    # Activity (queue / history) and Wanted
    # -------------------------------------------------------------------------

    # Sort-key whitelists: the key is looked up here and only the mapped SQL fragment is ever interpolated.
    QUEUE_SORT_KEYS: dict[str, str] = {
        "added_at": "d.created_at",
        "artist": "d.artist COLLATE NOCASE",
        "title": "item_title COLLATE NOCASE",
        "progress": "d.progress",
        "status": "d.status",
        "size_bytes": "d.size_bytes",
    }
    HISTORY_SORT_KEYS: dict[str, str] = {"date": "h.created_at"}
    HISTORY_EVENTS: tuple[str, ...] = ("grabbed", "imported", "failed", "deleted", "blocklisted", "upgraded")
    BLOCKLIST_SORT_KEYS: dict[str, str] = {"date": "b.created_at", "artist": "b.artist COLLATE NOCASE"}
    # Name sorts order by the persisted sort keys (articles and accents folded) so the scrubber index can group on
    # exactly the stored key the list is ordered by; ``WANTED_INDEX_SORTS`` below describes each key's grouping.
    _WANTED_RELEASE_DATE = "COALESCE(NULLIF(al.release_date, ''), CAST(al.year AS TEXT))"
    WANTED_SORT_KEYS: dict[str, str] = {
        "artist": "ar.sort_name",
        "album": "al.sort_title",
        "title": "t.sort_title",
        "release_date": _WANTED_RELEASE_DATE,
        "last_searched_at": "t.last_searched_at",
    }
    WANTED_INDEX_SORTS: dict[str, SortDef] = {
        "artist": SortDef("ar.sort_name", "name"),
        "album": SortDef("al.sort_title", "name"),
        "title": SortDef("t.sort_title", "name"),
        "release_date": SortDef(_WANTED_RELEASE_DATE, "date"),
        "last_searched_at": SortDef("t.last_searched_at", "date"),
    }
    # Downloads the native queue shows: everything that still needs the worker, plus an explicit ``warning`` state.
    NATIVE_QUEUE_STATUSES: tuple[str, ...] = ("queued", "downloading", "importing", "completed", "warning")

    _QUEUE_FROM = """
        FROM active_downloads d
        LEFT JOIN download_clients c ON d.client_id = c.id
        LEFT JOIN library_tracks lt ON lt.id = d.track_id
        LEFT JOIN library_albums al ON al.id = COALESCE(d.album_id, lt.album_id)
        LEFT JOIN music_requests r ON r.id = d.request_id
    """
    _QUEUE_SELECT = """
        SELECT d.*, c.name AS client_name, c.driver_type AS client_driver_type,
               COALESCE(lt.title, r.title, d.title) AS item_title,
               COALESCE(al.title, r.album, CASE WHEN d.item_type = 'album' THEN r.title END) AS album_title
    """

    @staticmethod
    def _order_clause(sort_map: dict[str, str], sort_key: str, sort_dir: str, tiebreak: str) -> str:
        if sort_key not in sort_map:
            raise ValueError(f"Unknown sort key: {sort_key!r}")
        direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"
        return f"ORDER BY {sort_map[sort_key]} {direction}, {tiebreak} {direction}"

    def record_download_event(
        self, event: str, download_id: Optional[str] = None, **fields: Any
    ) -> str:
        """Appends one ``download_history`` row; returns its id.

        When ``download_id`` names a live ``active_downloads`` row its artist/album/title/release/quality/indexer/
        protocol/client/hash context is copied in; any explicitly passed non-None ``fields`` override that context.
        """
        cols = (
            "request_id", "track_id", "album_id", "item_type", "artist", "album", "title", "release_title",
            "quality", "indexer", "protocol", "client", "info_hash", "release_guid", "message",
            "trigger", "trigger_ref", "trigger_label",
        )
        values: dict[str, Any] = {c: None for c in cols}
        with self._lock:
            if download_id:
                row = self.conn.execute(
                    self._QUEUE_SELECT + self._QUEUE_FROM + " WHERE d.id = ?", (str(download_id),)
                ).fetchone()
                if row is not None:
                    values.update(
                        request_id=row["request_id"], track_id=row["track_id"], album_id=row["album_id"],
                        item_type=row["item_type"], artist=row["artist"], album=row["album_title"],
                        title=row["item_title"], release_title=row["title"], quality=row["quality"],
                        indexer=row["indexer"], protocol=row["protocol"], client=row["client_name"],
                        info_hash=row["download_hash"], release_guid=row["id"],
                    )
            if download_id and event != "grabbed" and "trigger" not in fields:
                values.update(self.get_download_trigger(download_id))
            for key, val in fields.items():
                if key not in values:
                    raise ValueError(f"Unknown download history field: {key!r}")
                if val is not None:
                    values[key] = val
            event_id = f"dh-{uuid.uuid4().hex[:16]}"
            quoted_cols = ", ".join('"' + c + '"' for c in cols)  # "trigger" is an SQL keyword
            self.conn.execute(
                f"INSERT INTO download_history (id, event, download_id, {quoted_cols}, created_at) "
                f"VALUES (?, ?, ?, {', '.join('?' for _ in cols)}, CURRENT_TIMESTAMP)",
                (event_id, str(event), download_id, *[values[c] for c in cols]),
            )
            self.conn.commit()
        return event_id

    def set_download_release_meta(
        self,
        download_id: str,
        indexer: Optional[str] = None,
        quality: Optional[str] = None,
        protocol: Optional[str] = None,
    ) -> bool:
        """Stores the release's indexer / parsed quality / protocol on an active download."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE active_downloads SET indexer = COALESCE(?, indexer), quality = COALESCE(?, quality), "
                "protocol = COALESCE(?, protocol) WHERE id = ?",
                (indexer, quality, protocol, str(download_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def set_download_seed_rule(
        self,
        download_id: str,
        indexer_id: Optional[str],
        ratio_target: Optional[float],
        time_target_minutes: Optional[int],
        source: Optional[str],
    ) -> bool:
        """Snapshots the grab-time indexer id and effective seed targets onto a download (never re-resolved later)."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE active_downloads SET indexer_id = ?, seed_ratio_target = ?, seed_time_target_minutes = ?, "
                "seed_rule_source = ? WHERE id = ?",
                (
                    str(indexer_id) if indexer_id else None,
                    ratio_target,
                    time_target_minutes,
                    source,
                    str(download_id),
                ),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def record_seed_progress(self, download_id: str, ratio: float, seeding_seconds: int) -> None:
        """Stores the client's last-reported share ratio and seeding time for a torrent download."""
        with self._lock:
            self.conn.execute(
                "UPDATE active_downloads SET seed_ratio_current = ?, seeding_seconds = ? WHERE id = ?",
                (float(ratio), int(seeding_seconds), str(download_id)),
            )
            self.conn.commit()

    def get_imported_release_title(
        self, request_id: Optional[str] = None, track_id: Optional[str] = None, album_id: Optional[str] = None
    ) -> Optional[str]:
        """Release title of the most recent ``imported`` history event for a request, track or album (first key that
        has one wins), or None when unknown. Used to score the current file's format score exactly like a candidate."""
        with self._lock:
            for column, value in (("request_id", request_id), ("track_id", track_id), ("album_id", album_id)):
                if not value:
                    continue
                row = self.conn.execute(
                    f"SELECT release_title FROM download_history WHERE event = 'imported' AND {column} = ? "
                    "AND release_title IS NOT NULL AND release_title <> '' ORDER BY rowid DESC LIMIT 1",
                    (str(value),),
                ).fetchone()
                if row:
                    return str(row[0])
        return None

    def record_download_grab(
        self,
        download_id: str,
        indexer: Optional[str] = None,
        quality: Optional[str] = None,
        protocol: Optional[str] = None,
        upgrade: bool = False,
        replacement_issue_id: Optional[str] = None,
        trigger: Optional[GrabTrigger] = None,
    ) -> None:
        """Stores release metadata on a fresh download and writes its ``grabbed`` (and, for upgrades, ``upgraded``) event.

        ``trigger`` (why the grab happened) is stamped on the history rows and the ``grabbed`` item event.

        ``replacement_issue_id`` tags the ``grabbed`` row's message (``REPLACEMENT_MESSAGE_PREFIX`` + id) so the import
        can find the issue via ``get_download_replacement_issue``.
        """
        self.set_download_release_meta(download_id, indexer=indexer, quality=quality, protocol=protocol)
        history_trigger = {k: v for k, v in trigger_kwargs(trigger).items() if k != "actor_user_id"}
        self.record_download_event(
            "grabbed",
            download_id=download_id,
            message=f"{REPLACEMENT_MESSAGE_PREFIX}{replacement_issue_id}" if replacement_issue_id else None,
            **history_trigger,
        )
        if upgrade:
            self.record_download_event(
                "upgraded", download_id=download_id, message="Grabbed to replace a file below its quality cutoff",
                **history_trigger,
            )
        with self._lock:
            ctx = self.conn.execute(
                self._QUEUE_SELECT + self._QUEUE_FROM + " WHERE d.id = ?", (str(download_id),)
            ).fetchone()
        if ctx is not None:
            details = {
                "indexer": ctx["indexer"], "release": ctx["title"], "quality": ctx["quality"],
                "client": ctx["client_name"], "protocol": ctx["protocol"], "size_bytes": ctx["size_bytes"],
                "info_hash": ctx["download_hash"],
            }
            if replacement_issue_id:
                details["replacement_issue_id"] = replacement_issue_id
            self.record_download_item_event(
                "grabbed", download_id, message=f"Grabbed '{ctx['title']}'", details=details,
                **trigger_kwargs(trigger),
            )

    def get_download_replacement_issue(self, download_id: str) -> Optional[str]:
        """Issue id a download was grabbed to replace files for (see ``record_download_grab``), or None."""
        with self._lock:
            row = self.conn.execute(
                "SELECT message FROM download_history WHERE event = 'grabbed' AND download_id = ? "
                "AND message LIKE ? ORDER BY rowid DESC LIMIT 1",
                (str(download_id), f"{REPLACEMENT_MESSAGE_PREFIX}%"),
            ).fetchone()
        return str(row[0])[len(REPLACEMENT_MESSAGE_PREFIX):] if row else None

    def list_native_queue(
        self, page: int, page_size: int, sort_key: str, sort_dir: str
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of the native download queue (non-terminal, non-Lidarr rows) plus the total count."""
        order = self._order_clause(self.QUEUE_SORT_KEYS, sort_key, sort_dir, "d.id")
        where = (
            " WHERE d.status IN ({}) AND COALESCE(c.driver_type, '') <> 'lidarr'"
        ).format(", ".join("?" for _ in self.NATIVE_QUEUE_STATUSES))
        params = list(self.NATIVE_QUEUE_STATUSES)
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) " + self._QUEUE_FROM + where, params).fetchone()[0]
            cur = self.conn.execute(
                self._QUEUE_SELECT + self._QUEUE_FROM + where + f" {order} LIMIT ? OFFSET ?",
                [*params, int(page_size), (int(page) - 1) * int(page_size)],
            )
            return [self._map_active_download(r) for r in cur.fetchall()], int(total)

    def get_native_queue_item(self, download_id: str) -> Optional[dict[str, Any]]:
        """A single queue row with its resolved item/album titles, or None."""
        with self._lock:
            row = self.conn.execute(
                self._QUEUE_SELECT + self._QUEUE_FROM + " WHERE d.id = ?", (str(download_id),)
            ).fetchone()
            return self._map_active_download(row) if row else None

    # A grab can be marked failed only while it is the download's latest ``grabbed`` event and no later
    # ``failed`` / ``blocklisted`` event exists for that download (marking twice would duplicate rows).
    _CAN_MARK_FAILED_SQL = """(CASE WHEN h.event = 'grabbed' AND h.download_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM download_history x WHERE x.download_id = h.download_id
                        AND x.event IN ('failed', 'blocklisted') AND x.rowid > h.rowid)
        AND NOT EXISTS (SELECT 1 FROM download_history g WHERE g.download_id = h.download_id
                        AND g.event = 'grabbed' AND g.rowid > h.rowid)
        THEN 1 ELSE 0 END)"""

    # Library album / artist ids of a history row, only while that album still exists (so the UI never links to a
    # deleted entry). A track download carries ``track_id`` and no ``album_id``, hence the fallback.
    _HISTORY_LIBRARY_IDS_SQL = """
        (SELECT a.id FROM library_albums a
          WHERE a.id = COALESCE(h.album_id, (SELECT t.album_id FROM library_tracks t WHERE t.id = h.track_id))
        ) AS lib_album_id,
        (SELECT a.artist_id FROM library_albums a
          WHERE a.id = COALESCE(h.album_id, (SELECT t.album_id FROM library_tracks t WHERE t.id = h.track_id))
        ) AS lib_artist_id"""

    def list_download_history(
        self, page: int, page_size: int, sort_dir: str, event: Optional[str] = None
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of ``download_history`` (newest first by default) plus the total, optionally one event type."""
        order = self._order_clause(self.HISTORY_SORT_KEYS, "date", sort_dir, "h.rowid")
        where, params = "", []
        if event:
            where, params = " WHERE h.event = ?", [str(event)]
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) FROM download_history h" + where, params).fetchone()[0]
            cur = self.conn.execute(
                f"SELECT h.*, {self._CAN_MARK_FAILED_SQL} AS can_mark_failed, {self._HISTORY_LIBRARY_IDS_SQL}"
                " FROM download_history h"
                + where
                + f" {order} LIMIT ? OFFSET ?",
                [*params, int(page_size), (int(page) - 1) * int(page_size)],
            )
            return [dict(r) for r in cur.fetchall()], int(total)

    def get_download_history_item(self, history_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                f"SELECT h.*, {self._CAN_MARK_FAILED_SQL} AS can_mark_failed FROM download_history h WHERE h.id = ?",
                (str(history_id),),
            ).fetchone()
            return dict(row) if row else None

    def list_blocklist_page(
        self, page: int, page_size: int, sort_key: str, sort_dir: str
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of the download blocklist plus the total."""
        order = self._order_clause(self.BLOCKLIST_SORT_KEYS, sort_key, sort_dir, "b.id")
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) FROM download_blocklist").fetchone()[0]
            cur = self.conn.execute(
                f"SELECT b.* FROM download_blocklist b {order} LIMIT ? OFFSET ?",
                (int(page_size), (int(page) - 1) * int(page_size)),
            )
            return [dict(r) for r in cur.fetchall()], int(total)

    _WANTED_MISSING_FROM = """
        FROM library_tracks t
        JOIN library_albums al ON al.id = t.album_id
        JOIN library_artists ar ON ar.id = t.artist_id
        WHERE t.monitored = 1 AND al.monitored = 1 AND ar.monitored = 1
          AND NOT EXISTS (SELECT 1 FROM library_files f WHERE f.track_id = t.id)
    """
    _WANTED_CUTOFF_FROM = """
        FROM library_tracks t
        JOIN library_albums al ON al.id = t.album_id
        JOIN library_artists ar ON ar.id = t.artist_id
        JOIN library_files f ON f.id = (
            SELECT MIN(x.id) FROM library_files x WHERE x.track_id = t.id AND x.cutoff_met = 0
        )
        LEFT JOIN quality_profiles qp ON qp.id = ar.quality_profile_id
        LEFT JOIN quality_profiles dp ON dp.id = (SELECT id FROM quality_profiles WHERE is_default = 1 LIMIT 1)
        WHERE t.monitored = 1 AND al.monitored = 1 AND ar.monitored = 1
          AND COALESCE(qp.upgrade_allowed, dp.upgrade_allowed, 1) = 1
    """
    _WANTED_SELECT = """
        SELECT t.id AS id, t.id AS track_id, t.album_id AS album_id, ar.name AS artist, al.title AS album,
               t.title AS title, COALESCE(NULLIF(al.release_date, ''), CAST(al.year AS TEXT)) AS release_date,
               t.monitored AS monitored, t.last_searched_at AS last_searched_at,
               ar.quality_profile_id AS quality_profile_id
    """

    def list_wanted(
        self, kind: str, page: int, page_size: int, sort_key: str, sort_dir: str
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of native Wanted rows: ``kind`` is ``missing`` or ``cutoff``."""
        if kind not in ("missing", "cutoff"):
            raise ValueError(f"Unknown wanted list: {kind!r}")
        order = self._order_clause(self.WANTED_SORT_KEYS, sort_key, sort_dir, "t.id")
        frm = self._WANTED_MISSING_FROM if kind == "missing" else self._WANTED_CUTOFF_FROM
        select = self._WANTED_SELECT
        if kind == "cutoff":
            select += ", f.quality_name AS current_quality, COALESCE(qp.cutoff, dp.cutoff) AS cutoff_quality"
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) " + frm).fetchone()[0]
            cur = self.conn.execute(
                select + frm + f" {order} LIMIT ? OFFSET ?",
                (int(page_size), (int(page) - 1) * int(page_size)),
            )
            return [dict(r) for r in cur.fetchall()], int(total)

    def wanted_index(self, kind: str, sort_key: str, sort_dir: str) -> tuple[int, list[dict[str, Any]]]:
        """``(total, groups)`` for the native Wanted list, in the exact order ``list_wanted`` returns."""
        if kind not in ("missing", "cutoff"):
            raise ValueError(f"Unknown wanted list: {kind!r}")
        if sort_key not in self.WANTED_INDEX_SORTS:
            raise ValueError(f"Unknown sort key: {sort_key!r}")
        frm = self._WANTED_MISSING_FROM if kind == "missing" else self._WANTED_CUTOFF_FROM
        with self._lock:
            return build_index(self.conn, frm, [], self.WANTED_INDEX_SORTS[sort_key], sort_dir)

    def download_history_index(self, sort_dir: str, event: Optional[str] = None) -> tuple[int, list[dict[str, Any]]]:
        """``(total, groups)`` for ``download_history`` ordered by date, matching ``list_download_history``."""
        frm, params = "FROM download_history h", []
        if event:
            frm, params = frm + " WHERE h.event = ?", [str(event)]
        with self._lock:
            return build_index(self.conn, frm, params, SortDef("h.created_at", "date"), sort_dir)

    def list_wanted_search_targets(
        self, kind: Optional[str] = None, track_ids: Optional[list[str]] = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """Rows to search for: every Wanted row of ``kind`` (capped at ``limit``), or the given track ids.

        Rows carry ``current_quality`` when the track already has a file (so an upgrade search can be scored).
        """
        select = (
            self._WANTED_SELECT
            + ", (SELECT f2.quality_name FROM library_files f2 WHERE f2.track_id = t.id ORDER BY f2.cutoff_met ASC LIMIT 1)"
            " AS current_quality, (SELECT MIN(f3.cutoff_met) FROM library_files f3 WHERE f3.track_id = t.id) AS cutoff_met"
        )
        with self._lock:
            if track_ids is not None:
                ids = [str(i) for i in track_ids][:limit]
                if not ids:
                    return []
                marks = ", ".join("?" for _ in ids)
                cur = self.conn.execute(
                    select
                    + " FROM library_tracks t JOIN library_albums al ON al.id = t.album_id"
                    " JOIN library_artists ar ON ar.id = t.artist_id"
                    f" WHERE t.id IN ({marks})",
                    ids,
                )
            else:
                if kind not in ("missing", "cutoff"):
                    raise ValueError(f"Unknown wanted list: {kind!r}")
                frm = self._WANTED_MISSING_FROM if kind == "missing" else self._WANTED_CUTOFF_FROM
                cur = self.conn.execute(
                    select + frm + " ORDER BY ar.name COLLATE NOCASE ASC, t.id ASC LIMIT ?", (int(limit),)
                )
            return [dict(r) for r in cur.fetchall()]

    def mark_tracks_searched(self, track_ids: list[str]) -> int:
        """Stamps ``last_searched_at`` on the given library tracks."""
        ids = [str(i) for i in track_ids]
        if not ids:
            return 0
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE library_tracks SET last_searched_at = CURRENT_TIMESTAMP WHERE id IN ({', '.join('?' for _ in ids)})",
                ids,
            )
            self.conn.commit()
            return cur.rowcount

    # -------------------------------------------------------------------------
    # Catalog Wanted Queries
    # -------------------------------------------------------------------------

    def get_monitored_missing_catalog_tracks(
        self, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Queries tracks where artist, album, and track are monitored, but no file exists."""
        sql = """
            SELECT
                t.id AS track_id,
                t.title AS track_title,
                t.track_number AS track_number,
                t.disc_number AS disc_number,
                al.id AS album_id,
                al.title AS album_title,
                al.year AS year,
                ar.id AS artist_id,
                ar.name AS artist_name,
                ar.quality_profile_id AS quality_profile_id
            FROM library_tracks t
            JOIN library_albums al ON t.album_id = al.id
            JOIN library_artists ar ON t.artist_id = ar.id
            LEFT JOIN library_files f ON t.id = f.track_id
            WHERE t.monitored = 1
              AND al.monitored = 1
              AND ar.monitored = 1
              AND f.id IS NULL
            ORDER BY ar.name ASC, al.year ASC, t.disc_number ASC, t.track_number ASC
            LIMIT ?
        """
        with self._lock:
            cur = self.conn.execute(sql, (int(limit),))
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d["track_number"] = int(d.get("track_number") or 1)
                d["disc_number"] = int(d.get("disc_number") or 1)
                if d.get("year") is not None:
                    d["year"] = int(d["year"])
                rows.append(d)
            return rows

    def get_cutoff_unmet_catalog_tracks(
        self, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Queries tracks where artist, album, and track are monitored, but file cutoff_met is 0."""
        sql = """
            SELECT
                t.id AS track_id,
                t.title AS track_title,
                al.id AS album_id,
                al.title AS album_title,
                ar.id AS artist_id,
                ar.name AS artist_name,
                ar.quality_profile_id AS quality_profile_id,
                f.id AS file_id,
                f.quality_name AS quality_name,
                f.file_path AS file_path
            FROM library_tracks t
            JOIN library_albums al ON t.album_id = al.id
            JOIN library_artists ar ON t.artist_id = ar.id
            JOIN library_files f ON t.id = f.track_id
            WHERE t.monitored = 1
              AND al.monitored = 1
              AND ar.monitored = 1
              AND f.cutoff_met = 0
            ORDER BY ar.name ASC, al.year ASC, t.disc_number ASC, t.track_number ASC
            LIMIT ?
        """
        with self._lock:
            cur = self.conn.execute(sql, (int(limit),))
            return [dict(r) for r in cur.fetchall()]

    # -------------------------------------------------------------------------
    # System Events CRUD
    # -------------------------------------------------------------------------

    def record_event(
        self,
        event_type: str,
        message: str,
        source: str = "system",
        severity: str = "info",
        details: Optional[dict[str, Any]] = None,
    ) -> int:
        """Records a system lifecycle event and enforces 5,000-row ring-buffer capping."""
        details_json = json.dumps(details) if details is not None else None
        sev = (severity or "info").lower().strip()
        with self._lock:
            cur = self.conn.execute(
                """
                INSERT INTO system_events (event_type, severity, source, message, details_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(event_type), sev, str(source), str(message), details_json),
            )
            event_id = int(cur.lastrowid)
            cur.execute(
                """
                DELETE FROM system_events
                WHERE id NOT IN (
                    SELECT id FROM system_events ORDER BY id DESC LIMIT 5000
                )
                """
            )
            self.conn.commit()
            return event_id

    def list_events(
        self,
        limit: int = 50,
        offset: int = 0,
        event_type: Optional[str] = None,
        severity: Optional[str] = None,
        search: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Queries system events with dynamic filtering and returns (items, total_count)."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if event_type:
            where_clauses.append("event_type = ?")
            params.append(event_type)

        if severity and severity.lower() != "all":
            where_clauses.append("LOWER(severity) = ?")
            params.append(severity.lower().strip())

        if search:
            where_clauses.append("(message LIKE ? OR source LIKE ?)")
            s_param = f"%{search}%"
            params.extend([s_param, s_param])

        where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        with self._lock:
            count_cur = self.conn.execute(
                f"SELECT COUNT(*) FROM system_events{where_sql}",
                tuple(params),
            )
            total = int(count_cur.fetchone()[0])

            item_params = list(params) + [limit, offset]
            cur = self.conn.execute(
                f"""
                SELECT id, event_type, severity, source, message, details_json, created_at
                FROM system_events{where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                tuple(item_params),
            )
            rows = cur.fetchall()

        items: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d_json = d.get("details_json")
            if d_json:
                try:
                    d["details"] = json.loads(d_json)
                except Exception:
                    d["details"] = None
            else:
                d["details"] = None
            items.append(d)

        return items, total

    def clear_events(self) -> None:
        """Deletes all system events from the database."""
        with self._lock:
            self.conn.execute("DELETE FROM system_events")
            self.conn.commit()







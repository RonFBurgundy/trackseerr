"""Library health and media issues CRUD.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional, Union

from trackseerr.models import (
    MediaIssue,
)
from trackseerr.storage_common import (
    _utcnow,
)

logger = logging.getLogger(__name__)


class IssueStoreMixin:
    # -------------------------------------------------------------------------
    # Library health (server vs disk reconciliation, weak import matches)
    # -------------------------------------------------------------------------

    def get_media_server_path_mapping(self) -> Optional[dict[str, Any]]:
        """Saved ``{server_prefix, local_prefix, auto, server_kind}`` or None."""
        with self._lock:
            row = self.conn.execute("SELECT path_mapping_json FROM media_server_settings WHERE id = 1").fetchone()
        raw = (row["path_mapping_json"] if row else "") or ""
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Ignoring unreadable media-server path mapping")
            return None
        if not isinstance(data, dict):
            return None
        return {
            "server_prefix": str(data.get("server_prefix") or ""),
            "local_prefix": str(data.get("local_prefix") or ""),
            "auto": bool(data.get("auto")),
            "server_kind": str(data.get("server_kind") or ""),
        }

    def set_media_server_path_mapping(self, mapping: Optional[dict[str, Any]]) -> None:
        """Save (or with None clear) the path mapping without touching the rest of the media-server settings."""
        raw = json.dumps(mapping) if mapping else ""
        with self._lock:
            self.conn.execute("INSERT OR IGNORE INTO media_server_settings (id) VALUES (1)")
            self.conn.execute(
                "UPDATE media_server_settings SET path_mapping_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                (raw,),
            )
            self.conn.commit()

    def get_library_health_weekly(self) -> bool:
        return (self.get_kv("library_health_weekly") or "true").strip().lower() not in ("false", "0", "no", "off")

    def set_library_health_weekly(self, enabled: bool) -> None:
        self.set_kv("library_health_weekly", "true" if enabled else "false")

    def upsert_library_health_findings(self, rows: list[dict[str, Any]], seen_at: str) -> int:
        """Insert or refresh findings (unique on kind+path). ``first_seen`` and ``id`` survive; the rest is replaced."""
        if not rows:
            return 0
        fresh: list[dict[str, Any]] = []
        with self._lock:
            for r in rows:
                if self.conn.execute(
                    "SELECT 1 FROM library_health_findings WHERE kind = ? AND path = ?", (r["kind"], r["path"])
                ).fetchone() is None:
                    fresh.append(r)
                self.conn.execute(
                    """
                    INSERT INTO library_health_findings
                        (id, kind, server_kind, cause, group_key, path, detail_json, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(kind, path) DO UPDATE SET
                        server_kind = excluded.server_kind, cause = excluded.cause, group_key = excluded.group_key,
                        detail_json = excluded.detail_json, last_seen = excluded.last_seen
                    """,
                    (
                        str(uuid.uuid4()),
                        r["kind"],
                        r.get("server_kind"),
                        r["cause"],
                        r["group_key"],
                        r["path"],
                        json.dumps(r["detail"]) if r.get("detail") is not None else None,
                        seen_at,
                        seen_at,
                    ),
                )
            self.conn.commit()
            for r in fresh:
                self._record_health_finding_event(r)
        return len(rows)

    def _record_health_finding_event(self, finding: dict[str, Any]) -> None:
        """``health_finding`` on the item a NEW finding is about (its track, via the file path, tag match or download)."""
        detail = finding.get("detail") or {}
        message = f"Library health: {finding.get('cause')}"
        details = {"kind": finding["kind"], "cause": finding.get("cause"), "path": finding["path"]}
        track_id = str(detail["track_id"]) if detail.get("track_id") else None
        if track_id is None and finding["kind"] != "cleanup_failed":
            row = self.conn.execute(
                "SELECT track_id FROM library_files WHERE file_path = ? AND track_id IS NOT NULL LIMIT 1",
                (finding["path"],),
            ).fetchone()
            track_id = str(row["track_id"]) if row else None
        if track_id:
            self._safe_item_event(
                "health_finding", track_id=track_id, message=message, dedupe_last=True, details=details,
                trigger="system", trigger_label="Library health",
            )
        elif finding["kind"] == "cleanup_failed" and detail.get("download_id"):
            self.record_download_item_event(
                "health_finding", str(detail["download_id"]), message="Seed cleanup could not remove the download",
                details={"kind": finding["kind"], "error": detail.get("error"), "attempts": detail.get("attempts")},
                trigger="system", trigger_label="Library health",
            )

    def delete_library_health_findings_not_seen(self, kinds: list[str], seen_at: str) -> int:
        """Drop findings of ``kinds`` whose last_seen is not ``seen_at`` (the current run's stamp)."""
        if not kinds:
            return 0
        marks = ",".join("?" for _ in kinds)
        with self._lock:
            cur = self.conn.execute(
                f"DELETE FROM library_health_findings WHERE kind IN ({marks}) AND last_seen <> ?",
                (*kinds, seen_at),
            )
            self.conn.commit()
            return cur.rowcount

    def list_library_health_findings(self, include_dismissed: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM library_health_findings"
        if not include_dismissed:
            sql += " WHERE dismissed = 0"
        sql += " ORDER BY group_key, path"
        with self._lock:
            rows = self.conn.execute(sql).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d.pop("detail_json") or "null")
            except (TypeError, ValueError):
                d["detail"] = None
            out.append(d)
        return out

    def get_library_health_finding(self, finding_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM library_health_findings WHERE id = ?", (str(finding_id),)).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["detail"] = json.loads(d.pop("detail_json") or "null")
        except (TypeError, ValueError):
            d["detail"] = None
        return d

    def count_library_health_findings(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) FROM library_health_findings WHERE dismissed = 0").fetchone()
        return int(row[0]) if row else 0

    def delete_library_health_finding_by_path(self, path: str, kind: Optional[str] = None) -> int:
        sql, args = "DELETE FROM library_health_findings WHERE path = ?", [str(path)]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount

    def delete_library_health_findings_under(self, folder: str) -> int:
        """Delete findings at ``folder`` or beneath it (literal prefix match on a path boundary)."""
        folder = str(folder).rstrip("/") or "/"
        prefix = folder if folder.endswith("/") else folder + "/"
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM library_health_findings WHERE path = ? OR substr(path, 1, ?) = ?",
                (folder, len(prefix), prefix),
            )
            self.conn.commit()
            return cur.rowcount

    def add_library_health_dismissal(self, scope: str, path: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO library_health_dismissals (scope, path) VALUES (?, ?)", (scope, str(path))
            )
            self.conn.commit()

    def list_library_health_dismissals(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT id, scope, path, created_at FROM library_health_dismissals").fetchall()
        return [dict(r) for r in rows]

    def record_library_health_run(self, run: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO library_health_runs "
                "(started_at, finished_at, server_kind, disk_files, server_files, unindexed, stale, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run["started_at"],
                    run.get("finished_at"),
                    run.get("server_kind"),
                    int(run.get("disk_files") or 0),
                    int(run.get("server_files") or 0),
                    int(run.get("unindexed") or 0),
                    int(run.get("stale") or 0),
                    run.get("error"),
                ),
            )
            self.conn.commit()
            return {**run, "id": cur.lastrowid}

    def get_last_library_health_run(self) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM library_health_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # -------------------------------------------------------------------------
    # Media Issues CRUD
    # -------------------------------------------------------------------------

    ISSUE_STATUSES: tuple[str, ...] = ("open", "in_progress", "resolved", "wont_fix")
    ISSUE_FINAL_STATUSES: tuple[str, ...] = ("resolved", "wont_fix")

    _ISSUE_SELECT = """
        SELECT i.id, i.request_id, i.media_title, i.artist, i.issue_type,
               i.problem_details, i.status, i.user_id, i.created_at, i.updated_at,
               i.resolved_at, i.resolved_by AS resolved_by_id, i.album_id, i.track_id,
               i.discovery_id, i.item_type, i.reporter_seen_at, i.last_staff_activity_at,
               COALESCE(i.last_activity_at, i.updated_at) AS last_activity_at,
               u.username, ru.username AS resolved_by,
               (SELECT COUNT(*) FROM issue_comments c WHERE c.issue_id = i.id) AS comment_count_all,
               (SELECT COUNT(*) FROM issue_comments c WHERE c.issue_id = i.id AND c.is_system = 0)
                   AS comment_count_public,
               CASE WHEN i.last_staff_activity_at IS NOT NULL
                         AND (i.reporter_seen_at IS NULL OR i.last_staff_activity_at > i.reporter_seen_at)
                    THEN 1 ELSE 0 END AS unread
        FROM media_issues i
        LEFT JOIN users u ON i.user_id = u.id
        LEFT JOIN users ru ON i.resolved_by = ru.id
    """

    def create_issue(self, issue: Union[MediaIssue, dict[str, Any]]) -> dict[str, Any]:
        """Creates a new media issue report."""
        d = issue.to_dict() if isinstance(issue, MediaIssue) else dict(issue)
        issue_id = str(d.get("id"))
        issue_type = d.get("issue_type")
        if hasattr(issue_type, "value"):
            issue_type = issue_type.value
        issue_type = str(issue_type or "other")
        status_val = d.get("status")
        if hasattr(status_val, "value"):
            status_val = status_val.value
        status_val = str(status_val or "open")
        if status_val not in self.ISSUE_STATUSES:
            raise ValueError(f"Invalid issue status: {status_val!r}")
        now = _utcnow().isoformat()

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO media_issues (
                    id, request_id, media_title, artist, issue_type,
                    problem_details, status, user_id, album_id, track_id, discovery_id, item_type,
                    last_activity_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    issue_id,
                    d.get("request_id"),
                    str(d.get("media_title") or ""),
                    str(d.get("artist") or ""),
                    issue_type,
                    str(d.get("problem_details") or ""),
                    status_val,
                    str(d.get("user_id")),
                    d.get("album_id"),
                    d.get("track_id"),
                    d.get("discovery_id"),
                    d.get("item_type"),
                    now,
                ),
            )
            self.conn.commit()

        created = self.get_issue(issue_id)
        if created is None:
            raise RuntimeError(f"Failed to retrieve created issue {issue_id}")
        return created

    def get_issue(self, issue_id: str) -> Optional[dict[str, Any]]:
        """Retrieves a single media issue by ID with reporter username, comment counts and unread flag joined."""
        with self._lock:
            row = self.conn.execute(self._ISSUE_SELECT + " WHERE i.id = ?", (str(issue_id),)).fetchone()
            return dict(row) if row else None

    def count_recent_issues(self, user_id: str, hours: int = 24) -> int:
        """Counts issues created by ``user_id`` within the rolling window (from the DB, restart-safe)."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM media_issues WHERE user_id = ? AND created_at >= datetime('now', ?)",
                (str(user_id), f"-{int(hours)} hours"),
            )
            return int(cur.fetchone()[0])

    def find_active_duplicate_issue(
        self,
        user_id: str,
        media_title: str,
        artist: str,
        issue_type: str,
        request_id: Optional[str] = None,
        album_id: Optional[str] = None,
        track_id: Optional[str] = None,
        discovery_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """The user's open/in_progress issue of the same type that matches title+artist (case-insensitive) or
        shares the request / library album / library track / discovery reference, if any."""
        clauses = ["(lower(media_title) = lower(?) AND lower(artist) = lower(?))"]
        params: list[Any] = [str(media_title), str(artist)]
        for column, value in (
            ("request_id", request_id),
            ("album_id", album_id),
            ("track_id", track_id),
            ("discovery_id", discovery_id),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(str(value))
        with self._lock:
            cur = self.conn.execute(
                f"""
                SELECT id, status FROM media_issues
                WHERE user_id = ? AND status IN ('open', 'in_progress') AND issue_type = ?
                  AND ({' OR '.join(clauses)})
                ORDER BY created_at DESC LIMIT 1
                """,
                (str(user_id), str(issue_type).lower(), *params),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def list_issues(
        self,
        status: Optional[str] = None,
        user_id: Optional[str] = None,
        media_title: Optional[str] = None,
        artist: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Lists media issues filtered by status and/or user_id with username joined."""
        query = self._ISSUE_SELECT + " WHERE 1=1"
        params: list[Any] = []
        if status:
            query += " AND i.status = ?"
            params.append(str(status))
        if user_id:
            query += " AND i.user_id = ?"
            params.append(str(user_id))
        if media_title:
            query += " AND lower(i.media_title) = lower(?)"
            params.append(str(media_title))
        if artist:
            query += " AND lower(i.artist) = lower(?)"
            params.append(str(artist))
        query += " ORDER BY i.created_at DESC, i.rowid DESC"

        with self._lock:
            cur = self.conn.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    def update_issue(
        self, issue_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Updates an issue's status and/or problem details; any other key is ignored.

        A status change goes through ``set_issue_status`` so ``resolved_at`` / ``resolved_by`` stay consistent.
        """
        existing = self.get_issue(issue_id)
        if not existing:
            raise KeyError(f"Media issue {issue_id} not found")

        new_status = updates.get("status")
        if hasattr(new_status, "value"):
            new_status = new_status.value
        if new_status is not None:
            new_status = str(new_status)
            if new_status not in self.ISSUE_STATUSES:
                raise ValueError(f"Invalid issue status: {new_status!r}")
        details = updates.get("problem_details")
        if details is not None:
            with self._lock:
                self.conn.execute(
                    "UPDATE media_issues SET problem_details = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (str(details), str(issue_id)),
                )
                self.conn.commit()
        if new_status is not None and new_status != existing["status"]:
            self.set_issue_status(issue_id, new_status, actor_id=None, staff=False)

        updated = self.get_issue(issue_id)
        if updated is None:
            raise RuntimeError(f"Media issue {issue_id} disappeared after update")
        return updated

    def set_issue_status(
        self,
        issue_id: str,
        new_status: str,
        actor_id: Optional[str],
        staff: bool,
        expected_status: Optional[str] = None,
    ) -> bool:
        """Moves an issue to ``new_status``; True when a row changed.

        Terminal statuses stamp ``resolved_at`` / ``resolved_by``; leaving them clears both. ``staff`` marks the
        change as admin activity the reporter has not seen yet. ``expected_status`` makes it a compare-and-set so
        two concurrent transitions cannot both win.
        """
        if new_status not in self.ISSUE_STATUSES:
            raise ValueError(f"Invalid issue status: {new_status!r}")
        now = _utcnow().isoformat()
        final = new_status in self.ISSUE_FINAL_STATUSES
        sql = (
            "UPDATE media_issues SET status = ?, resolved_at = ?, resolved_by = ?, last_activity_at = ?, "
            "last_staff_activity_at = CASE WHEN ? THEN ? ELSE last_staff_activity_at END, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status <> ?"
        )
        params: list[Any] = [
            new_status, now if final else None, actor_id if final else None, now,
            1 if staff else 0, now, str(issue_id), new_status,
        ]
        if expected_status is not None:
            sql += " AND status = ?"
            params.append(expected_status)
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.rowcount > 0

    def add_issue_comment(
        self,
        issue_id: str,
        user_id: Optional[str],
        body: str,
        is_admin: bool,
        is_system: bool = False,
        staff: Optional[bool] = None,
        max_comments: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """Appends a comment and bumps the issue's activity stamps.

        ``staff`` (default: ``is_admin``) marks it as activity the reporter has not seen; a reporter's own comment
        instead marks the issue seen for them. Returns None when ``max_comments`` is already reached.
        """
        staff_activity = is_admin if staff is None else staff
        now = _utcnow().isoformat()
        comment_id = f"ic-{uuid.uuid4().hex[:12]}"
        with self._lock:
            if max_comments is not None:
                count = self.conn.execute(
                    "SELECT COUNT(*) FROM issue_comments WHERE issue_id = ?", (str(issue_id),)
                ).fetchone()[0]
                if int(count) >= max_comments:
                    return None
            self.conn.execute(
                "INSERT INTO issue_comments (id, issue_id, user_id, body, created_at, is_admin, is_system) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (comment_id, str(issue_id), user_id, str(body), now, 1 if is_admin else 0, 1 if is_system else 0),
            )
            if staff_activity:
                self.conn.execute(
                    "UPDATE media_issues SET last_activity_at = ?, last_staff_activity_at = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (now, now, str(issue_id)),
                )
            else:
                self.conn.execute(
                    "UPDATE media_issues SET last_activity_at = ?, reporter_seen_at = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (now, now, str(issue_id)),
                )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT c.id, c.issue_id, c.user_id, c.body, c.created_at, c.is_admin, c.is_system, u.username "
                "FROM issue_comments c LEFT JOIN users u ON c.user_id = u.id WHERE c.id = ?",
                (comment_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_issue_comments(self, issue_id: str, include_system: bool = True) -> list[dict[str, Any]]:
        """An issue's comments oldest first; ``include_system`` False hides admin action notes."""
        sql = (
            "SELECT c.id, c.issue_id, c.user_id, c.body, c.created_at, c.is_admin, c.is_system, u.username "
            "FROM issue_comments c LEFT JOIN users u ON c.user_id = u.id WHERE c.issue_id = ?"
        )
        if not include_system:
            sql += " AND c.is_system = 0"
        sql += " ORDER BY c.created_at ASC, c.rowid ASC"
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, (str(issue_id),)).fetchall()]

    def mark_issue_seen(self, issue_id: str) -> bool:
        """Stamps the reporter's last-seen time on an issue."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE media_issues SET reporter_seen_at = ? WHERE id = ?",
                (_utcnow().isoformat(), str(issue_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def count_unread_issues(self, user_id: str) -> int:
        """Issues the user reported that have admin activity newer than their last visit."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM media_issues WHERE user_id = ? AND last_staff_activity_at IS NOT NULL "
                "AND (reporter_seen_at IS NULL OR last_staff_activity_at > reporter_seen_at)",
                (str(user_id),),
            ).fetchone()
            return int(row[0])

    def count_issues_by_status(self, status: str) -> int:
        """Number of issues currently in ``status``."""
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) FROM media_issues WHERE status = ?", (str(status),)).fetchone()
            return int(row[0])

    def get_imported_release_source(
        self,
        album_id: str,
        track_ids: Optional[list[str]] = None,
        request_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """The newest ``imported`` history row that produced an album's files (matched by album, then its tracks,
        then the request), or None when no import is on record. Carries the release identity for blocklisting."""
        candidates: list[tuple[str, list[str]]] = [("album_id", [str(album_id)])]
        if track_ids:
            candidates.append(("track_id", [str(t) for t in track_ids]))
        if request_id:
            candidates.append(("request_id", [str(request_id)]))
        with self._lock:
            for column, values in candidates:
                marks = ", ".join("?" for _ in values)
                row = self.conn.execute(
                    f"SELECT * FROM download_history WHERE event = 'imported' AND {column} IN ({marks}) "
                    "AND release_title IS NOT NULL AND release_title <> '' ORDER BY rowid DESC LIMIT 1",
                    values,
                ).fetchone()
                if row:
                    return dict(row)
        return None

    def delete_issue(self, issue_id: str) -> bool:
        """Deletes a media issue by ID (its comments cascade)."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM media_issues WHERE id = ?",
                (str(issue_id),),
            )
            self.conn.commit()
            return cur.rowcount > 0


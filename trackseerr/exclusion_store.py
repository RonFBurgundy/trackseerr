"""Exclusion store mixin for TrackSeerr database.

Provides persistence, queries, and snapshot caching for the exclusions table.
"""

from __future__ import annotations

import uuid
from typing import Any

from trackseerr.exclusions import ExclusionSnapshot, match_key

_COLUMNS = "id, scope, mbid, artist_name, title, match_key, reason, created_by, created_at"


class ExclusionStoreMixin:
    """Mixin for Database providing exclusions store operations."""

    def _bump_exclusion_version(self) -> None:
        """Bump cache version to invalidate cached ExclusionSnapshot."""
        self._exclusion_version = getattr(self, "_exclusion_version", 0) + 1
        self._exclusion_snapshot_cache = None

    def add_exclusion(
        self,
        scope: str,
        artist_name: str,
        title: str | None = None,
        mbid: str | None = None,
        reason: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Compute match_key; INSERT; on unique (scope, match_key) conflict return

        the existing row (and fill in a missing mbid on it if one is given now).
        """
        scope_clean = str(scope).strip().lower()
        key = match_key(scope_clean, artist_name, title)
        mbid_clean = mbid.strip() if mbid and mbid.strip() else None
        reason_clean = reason.strip() if reason and reason.strip() else None
        created_by_clean = created_by.strip() if created_by and created_by.strip() else None
        title_clean = title.strip() if title and title.strip() else None
        artist_clean = artist_name.strip()

        with self._lock:
            existing = self.conn.execute(
                f"SELECT {_COLUMNS} FROM exclusions WHERE scope = ? AND match_key = ?",
                (scope_clean, key),
            ).fetchone()
            if existing:
                row = dict(existing)
                if mbid_clean and not row.get("mbid"):
                    self.conn.execute(
                        "UPDATE exclusions SET mbid = ? WHERE id = ?",
                        (mbid_clean, row["id"]),
                    )
                    self.conn.commit()
                    row["mbid"] = mbid_clean
                    self._bump_exclusion_version()
                return row

            excl_id = "excl-" + uuid.uuid4().hex[:12]
            self.conn.execute(
                "INSERT INTO exclusions (id, scope, mbid, artist_name, title, match_key, reason, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (excl_id, scope_clean, mbid_clean, artist_clean, title_clean, key, reason_clean, created_by_clean),
            )
            self.conn.commit()
            self._bump_exclusion_version()
            row = self.conn.execute(
                f"SELECT {_COLUMNS} FROM exclusions WHERE id = ?",
                (excl_id,),
            ).fetchone()
            return dict(row)

    def list_exclusions(
        self,
        scope: str | None = None,
        search: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """List exclusions (newest first) with optional scope and search filters.

        search matches artist_name/title case-insensitively.
        Returns (rows, total_matching_count).
        """
        where: list[str] = []
        params: list[Any] = []

        if scope is not None and scope.strip():
            where.append("scope = ?")
            params.append(scope.strip().lower())

        if search is not None and search.strip():
            term = f"%{search.strip().lower()}%"
            where.append("(LOWER(artist_name) LIKE ? OR LOWER(COALESCE(title, '')) LIKE ?)")
            params.extend([term, term])

        clause = (" WHERE " + " AND ".join(where)) if where else ""

        with self._lock:
            total_row = self.conn.execute(
                f"SELECT COUNT(*) FROM exclusions{clause}",
                params,
            ).fetchone()
            total = int(total_row[0]) if total_row else 0

            rows = self.conn.execute(
                f"SELECT {_COLUMNS} FROM exclusions{clause} "
                "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                [*params, int(limit), int(offset)],
            ).fetchall()

        return [dict(r) for r in rows], total

    def get_exclusion(self, exclusion_id: str) -> dict[str, Any] | None:
        """Retrieve an exclusion by ID, or None if not found."""
        if not exclusion_id:
            return None
        with self._lock:
            row = self.conn.execute(
                f"SELECT {_COLUMNS} FROM exclusions WHERE id = ?",
                (str(exclusion_id),),
            ).fetchone()
            return dict(row) if row else None

    def delete_exclusion(self, exclusion_id: str) -> bool:
        """Delete an exclusion by ID. Returns True if deleted, False if not found."""
        if not exclusion_id:
            return False
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM exclusions WHERE id = ?",
                (str(exclusion_id),),
            )
            self.conn.commit()
            if cur.rowcount > 0:
                self._bump_exclusion_version()
                return True
            return False

    def exclusion_snapshot(self) -> ExclusionSnapshot:
        """Cached in-memory snapshot of all exclusions.

        Holds dicts by_mbid[(scope, mbid)] and by_key[(scope, match_key)] -> row dict.
        Invalidated by add/delete via version bump.
        """
        with self._lock:
            current_version = getattr(self, "_exclusion_version", 0)
            cached = getattr(self, "_exclusion_snapshot_cache", None)
            if cached is not None and cached.version == current_version:
                return cached

            rows = self.conn.execute(
                f"SELECT {_COLUMNS} FROM exclusions"
            ).fetchall()

            by_mbid: dict[tuple[str, str], dict[str, Any]] = {}
            by_key: dict[tuple[str, str], dict[str, Any]] = {}

            for r in rows:
                row_dict = dict(r)
                scope = str(row_dict["scope"])
                key = str(row_dict["match_key"])
                by_key[(scope, key)] = row_dict
                mbid = row_dict.get("mbid")
                if mbid and str(mbid).strip():
                    by_mbid[(scope, str(mbid).strip().lower())] = row_dict

            snapshot = ExclusionSnapshot(
                version=current_version,
                by_mbid=by_mbid,
                by_key=by_key,
            )
            self._exclusion_snapshot_cache = snapshot
            return snapshot

"""Music requests CRUD and lifecycle management.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional, Union

from trackseerr.models import (
    MusicRequest,
    RequestStatus,
)
from trackseerr.storage_common import (
    REQUEST_FAST_RETRY_REASONS,
    _RETRY_TS_FORMAT,
    request_retry_delay,
)

logger = logging.getLogger(__name__)


class RequestStoreMixin:
    # -------------------------------------------------------------------------
    # Music Requests CRUD
    # -------------------------------------------------------------------------

    def create_request(self, request: MusicRequest) -> dict[str, Any]:
        """Creates a new music request."""
        status_val = request.status.value if isinstance(request.status, RequestStatus) else str(request.status)
        qp_id = getattr(request, "quality_profile_id", None)
        curr_q = getattr(request, "current_quality", None)
        cutoff_m = getattr(request, "cutoff_met", 1)
        cutoff_val = 1 if (cutoff_m is None or cutoff_m) else 0

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO music_requests (
                    id, user_id, item_type, title, artist, album,
                    cover_url, preview_url, status, release_date, foreign_id,
                    quality_profile_id, current_quality, cutoff_met,
                    batch_id, batch_kind, "trigger", trigger_ref, trigger_label, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    str(request.id),
                    str(request.user_id),
                    str(request.item_type),
                    str(request.title),
                    str(request.artist),
                    request.album,
                    request.cover_url,
                    request.preview_url,
                    status_val,
                    request.release_date,
                    request.foreign_id,
                    qp_id,
                    curr_q,
                    cutoff_val,
                    getattr(request, "batch_id", None),
                    getattr(request, "batch_kind", None),
                    getattr(request, "trigger", None),
                    getattr(request, "trigger_ref", None),
                    getattr(request, "trigger_label", None),
                ),
            )
            self.conn.commit()
        req = self.get_request(str(request.id))
        if req is None:
            raise RuntimeError(f"Failed to retrieve created request {request.id}")
        return req

    def get_request(self, request_id: str) -> Optional[dict[str, Any]]:
        """Retrieves a single request by ID with user info joined."""
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT r.id, r.user_id, r.item_type, r.title, r.artist, r.album,
                       r.cover_url, r.preview_url, r.status, r.release_date, r.foreign_id,
                       r.quality_profile_id, r.current_quality, r.cutoff_met,
                       r.created_at, r.updated_at, r.batch_id, r.batch_kind, r.status_reason, r.status_message,
                       r.next_attempt_at, r."trigger", r.trigger_ref, r.trigger_label, u.username
                FROM music_requests r
                LEFT JOIN users u ON r.user_id = u.id
                WHERE r.id = ?
                """,
                (str(request_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    _REQUEST_STATUS_ALIASES: dict[str, tuple[str, ...]] = {
        "pending": ("pending",),
        "approved": ("approved", "processing"),
        "fulfilled": ("available", "fulfilled"),
        "available": ("available", "fulfilled"),
        "processing": ("processing",),
        "rejected": ("rejected",),
    }

    def list_requests(
        self, user_id: Optional[str] = None, status: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Lists requests filtered by user_id and/or status with username joined."""
        query = """
            SELECT r.id, r.user_id, r.item_type, r.title, r.artist, r.album,
                   r.cover_url, r.preview_url, r.status, r.release_date, r.foreign_id,
                   r.quality_profile_id, r.current_quality, r.cutoff_met,
                   r.created_at, r.updated_at, r.batch_id, r.batch_kind, r.status_reason, r.status_message,
                   r.next_attempt_at, u.username
            FROM music_requests r
            LEFT JOIN users u ON r.user_id = u.id
            WHERE 1=1
        """
        params: list[Any] = []
        if user_id:
            query += " AND r.user_id = ?"
            params.append(str(user_id))
        if status:
            status_val = status.value if hasattr(status, "value") else str(status)
            # UI tab names are not stored values: "approved" rows are stored as 'processing' and
            # "fulfilled" rows as 'available'.
            stored = self._REQUEST_STATUS_ALIASES.get(status_val.strip().lower(), (status_val,))
            query += f" AND r.status IN ({','.join('?' * len(stored))})"
            params.extend(stored)

        query += " ORDER BY r.created_at DESC"

        with self._lock:
            cur = self.conn.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    def get_cutoff_unmet_requests(self) -> list[dict[str, Any]]:
        """Returns requests where status = 'available' AND cutoff_met = 0, excluding those whose profile forbids upgrades."""
        query = """
            SELECT r.id, r.user_id, r.item_type, r.title, r.artist, r.album,
                   r.cover_url, r.preview_url, r.status, r.release_date, r.foreign_id,
                   r.quality_profile_id, r.current_quality, r.cutoff_met,
                   r.created_at, r.updated_at, u.username
            FROM music_requests r
            LEFT JOIN users u ON r.user_id = u.id
            LEFT JOIN quality_profiles qp ON qp.id = r.quality_profile_id
            WHERE r.status = 'available' AND r.cutoff_met = 0
              AND COALESCE(
                    qp.upgrade_allowed,
                    (SELECT upgrade_allowed FROM quality_profiles WHERE is_default = 1 LIMIT 1),
                    1
                  ) = 1
            ORDER BY r.created_at ASC
        """
        with self._lock:
            cur = self.conn.execute(query)
            return [dict(row) for row in cur.fetchall()]

    def update_request_quality(
        self, request_id: str, current_quality: Optional[str], cutoff_met: int = 1
    ) -> bool:
        """Updates the current quality and cutoff_met status of a request."""
        with self._lock:
            cur = self.conn.execute(
                """
                UPDATE music_requests
                SET current_quality = ?, cutoff_met = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (current_quality, int(cutoff_met), str(request_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def update_request_status(
        self, request_id: str, status: Union[RequestStatus, str]
    ) -> bool:
        """Updates the status of a request."""
        status_val = status.value if isinstance(status, RequestStatus) else str(status)
        with self._lock:
            cur = self.conn.execute(
                """
                UPDATE music_requests
                SET status = ?, status_reason = NULL, status_message = NULL, retry_attempts = 0, next_attempt_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status_val, str(request_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def set_request_outcome(
        self,
        request_id: str,
        reason: Optional[str],
        message: Optional[str],
        retry_after: Optional[float] = None,
        schedule: bool = True,
    ) -> bool:
        """Records (or with ``None`` clears) why a request is stuck, leaving its status as it is.

        A reason with a retry policy also bumps ``retry_attempts`` and schedules ``next_attempt_at``; clearing
        the reason clears the schedule. ``schedule=False`` records the reason only (native mode: the backlog sweep,
        not this schedule, retries those requests, so no retry time is shown).
        """
        with self._lock:
            if reason is None or not schedule:
                cur = self.conn.execute(
                    """
                    UPDATE music_requests
                    SET status_reason = ?, status_message = ?, retry_attempts = 0, next_attempt_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (reason, message, str(request_id)),
                )
            else:
                row = self.conn.execute(
                    "SELECT retry_attempts FROM music_requests WHERE id = ?", (str(request_id),)
                ).fetchone()
                attempts = (int(row[0] or 0) if row else 0) + 1
                delay = request_retry_delay(reason, attempts, retry_after)
                due = (datetime.now(timezone.utc) + delay).strftime(_RETRY_TS_FORMAT) if delay else None
                cur = self.conn.execute(
                    """
                    UPDATE music_requests
                    SET status_reason = ?, status_message = ?, retry_attempts = ?, next_attempt_at = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (reason, message, attempts, due, str(request_id)),
                )
            self.conn.commit()
            return cur.rowcount > 0

    def defer_request_retry(self, request_id: str) -> bool:
        """Counts one more retry attempt and pushes ``next_attempt_at`` out, keeping the recorded reason and message."""
        with self._lock:
            row = self.conn.execute(
                "SELECT status_reason, retry_attempts FROM music_requests WHERE id = ?", (str(request_id),)
            ).fetchone()
            if not row or not row[0]:
                return False
            attempts = int(row[1] or 0) + 1
            delay = request_retry_delay(str(row[0]), attempts)
            due = (datetime.now(timezone.utc) + delay).strftime(_RETRY_TS_FORMAT) if delay else None
            cur = self.conn.execute(
                "UPDATE music_requests SET retry_attempts = ?, next_attempt_at = ? WHERE id = ?",
                (attempts, due, str(request_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def list_requests_due_for_retry(
        self, now: Optional[datetime] = None, limit: int = 25, ignore_schedule: bool = False
    ) -> list[dict[str, Any]]:
        """Approved requests stuck on a retryable Lidarr outcome whose ``next_attempt_at`` has passed.

        ``ignore_schedule`` (the manual "run now") returns every such request regardless of its retry time.
        """
        current = (now or datetime.now(timezone.utc)).strftime(_RETRY_TS_FORMAT)
        reasons = (*REQUEST_FAST_RETRY_REASONS, "not_in_metadata_profile")
        with self._lock:
            cur = self.conn.execute(
                f"""
                SELECT id, artist, album, title, item_type, status_reason, retry_attempts, next_attempt_at
                FROM music_requests
                WHERE status = 'processing' AND (? OR (next_attempt_at IS NOT NULL AND next_attempt_at <= ?))
                  AND status_reason IN ({','.join('?' * len(reasons))})
                ORDER BY next_attempt_at ASC LIMIT ?
                """,
                (1 if ignore_schedule else 0, current, *reasons, int(limit)),
            )
            return [dict(r) for r in cur.fetchall()]

    def delete_request(self, request_id: str) -> bool:
        """Deletes a request by ID."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM music_requests WHERE id = ?",
                (str(request_id),),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def find_matching_processing_requests(
        self, artist: str, album: Optional[str] = None, title: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Finds pending or processing requests matching an artist and optional album/title."""
        clean_artist = (artist or "").strip().lower()
        if not clean_artist:
            return []
        clean_album = (album or "").strip().lower() if album else None
        clean_title = (title or "").strip().lower() if title else None

        with self._lock:
            cur = self.conn.execute(
                """
                SELECT r.id, r.user_id, r.item_type, r.title, r.artist, r.album,
                       r.cover_url, r.preview_url, r.status, r.release_date, r.foreign_id,
                       r.quality_profile_id, r.current_quality, r.cutoff_met,
                       r.created_at, r.updated_at, u.username
                FROM music_requests r
                LEFT JOIN users u ON r.user_id = u.id
                WHERE r.status IN ('processing', 'pending', 'approved')
                  AND LOWER(r.artist) = ?
                """,
                (clean_artist,),
            )
            rows = [dict(r) for r in cur.fetchall()]

        matches: list[dict[str, Any]] = []
        for r in rows:
            req_item_type = r.get("item_type", "")
            req_title = (r.get("title") or "").strip().lower()
            req_album = (r.get("album") or "").strip().lower()

            if clean_album and clean_title:
                if req_item_type == "album":
                    if req_title == clean_album or req_album == clean_album:
                        matches.append(r)
                elif req_item_type == "track":
                    if req_title == clean_title and (not req_album or req_album == clean_album):
                        matches.append(r)
            elif clean_album:
                if req_item_type == "album" and (req_title == clean_album or req_album == clean_album):
                    matches.append(r)
                elif req_item_type == "track" and req_album == clean_album:
                    matches.append(r)
            elif clean_title:
                if req_title == clean_title or req_album == clean_title:
                    matches.append(r)
            else:
                matches.append(r)
        return matches


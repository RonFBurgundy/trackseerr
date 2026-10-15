"""AcoustID fingerprint + lookup cache, one row per library file.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``). A row is only trusted while the file's path,
size and mtime still match what was fingerprinted.
"""

from __future__ import annotations

from collections.abc import Iterator
import json
from typing import Any, Optional

_COLUMNS = "file_id, file_path, size_bytes, mtime_ns, duration, fingerprint, status, matches_json, error, looked_up_at"
_SUBMISSION_COLUMNS = (
    "file_id, recording_mbid, fingerprint_hash, submission_id, status, reason, submitted_at, checked_at"
)
_CHUNK = 500


def _row_to_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    raw = out.pop("matches_json", None) or "[]"
    try:
        out["matches"] = json.loads(raw)
    except json.JSONDecodeError:
        out["matches"] = []
    return out


class FingerprintStoreMixin:
    def get_file_fingerprint(self, file_id: str) -> Optional[dict[str, Any]]:
        """The cached fingerprint row for a library file (``matches`` parsed to a list), or None."""
        with self._lock:
            row = self.conn.execute(
                f"SELECT {_COLUMNS} FROM file_fingerprints WHERE file_id = ?", (str(file_id),)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def get_file_fingerprints(self, file_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Cached fingerprint rows keyed by file id (files without a row are absent)."""
        ids = [str(i) for i in file_ids]
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(ids), _CHUNK):
            chunk = ids[start : start + _CHUNK]
            marks = ",".join("?" * len(chunk))
            with self._lock:
                rows = self.conn.execute(
                    f"SELECT {_COLUMNS} FROM file_fingerprints WHERE file_id IN ({marks})", chunk
                ).fetchall()
            for r in rows:
                d = _row_to_dict(r)
                out[d["file_id"]] = d
        return out

    def upsert_file_fingerprint(self, row: dict[str, Any]) -> None:
        """Inserts or replaces the cached fingerprint for ``row["file_id"]``; ``matches`` (list) is stored as JSON."""
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO file_fingerprints (
                    file_id, file_path, size_bytes, mtime_ns, duration, fingerprint, status, matches_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    file_path = excluded.file_path, size_bytes = excluded.size_bytes,
                    mtime_ns = excluded.mtime_ns, duration = excluded.duration,
                    fingerprint = excluded.fingerprint, status = excluded.status,
                    matches_json = excluded.matches_json, error = excluded.error,
                    looked_up_at = CURRENT_TIMESTAMP
                """,
                (
                    str(row["file_id"]), str(row["file_path"]), int(row["size_bytes"]), int(row["mtime_ns"]),
                    row.get("duration"), row.get("fingerprint"), str(row["status"]),
                    json.dumps(row.get("matches") or []), row.get("error"),
                ),
            )
            self.conn.commit()

    def get_acoustid_submission(self, file_id: str) -> Optional[dict[str, Any]]:
        """The AcoustID submission row for a library file, or None."""
        with self._lock:
            row = self.conn.execute(
                f"SELECT {_SUBMISSION_COLUMNS} FROM acoustid_submissions WHERE file_id = ?",
                (str(file_id),),
            ).fetchone()
        return dict(row) if row else None

    def upsert_acoustid_submission(self, row: dict[str, Any]) -> None:
        """Inserts or replaces the AcoustID submission record for ``row["file_id"]``."""
        with self._lock:
            self.conn.execute(
                f"""
                INSERT INTO acoustid_submissions ({_SUBMISSION_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    recording_mbid = excluded.recording_mbid,
                    fingerprint_hash = excluded.fingerprint_hash,
                    submission_id = excluded.submission_id,
                    status = excluded.status,
                    reason = excluded.reason,
                    submitted_at = excluded.submitted_at,
                    checked_at = excluded.checked_at
                """,
                (
                    str(row["file_id"]),
                    str(row["recording_mbid"]),
                    str(row["fingerprint_hash"]),
                    row.get("submission_id"),
                    str(row["status"]),
                    row.get("reason"),
                    row.get("submitted_at"),
                    row.get("checked_at"),
                ),
            )
            self.conn.commit()

    def list_acoustid_submissions(self, status: str, limit: int = 100) -> list[dict[str, Any]]:
        """Submissions filtered by status, up to limit."""
        with self._lock:
            cur = self.conn.execute(
                f"SELECT {_SUBMISSION_COLUMNS} FROM acoustid_submissions WHERE status = ? "
                "ORDER BY rowid ASC LIMIT ?",
                (str(status), int(limit)),
            )
            return [dict(r) for r in cur.fetchall()]

    def count_acoustid_submissions(self) -> dict[str, int]:
        """Counts submissions grouped by status."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM acoustid_submissions GROUP BY status"
            ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def iter_acoustid_candidates(self, batch: int = 200) -> Iterator[dict[str, Any]]:
        """Yields file rows eligible for AcoustID submission, keyset-paginated by file id.

        Eligible files have a track with a non-empty mb_recording_id and either no
        acoustid_submissions record or an error record with checked_at older than 7 days.
        """
        last_id: Optional[str] = None
        while True:
            params: list[Any] = []
            after_clause = ""
            if last_id is not None:
                after_clause = " AND f.id > ?"
                params.append(str(last_id))
            params.append(int(batch))

            with self._lock:
                rows = self.conn.execute(
                    f"""
                    SELECT
                        f.id,
                        f.file_path,
                        f.codec,
                        f.bitrate,
                        f.track_id,
                        t.mb_recording_id,
                        t.duration_seconds
                    FROM library_files f
                    JOIN library_tracks t ON f.track_id = t.id
                    LEFT JOIN acoustid_submissions s ON f.id = s.file_id
                    WHERE t.mb_recording_id IS NOT NULL AND TRIM(t.mb_recording_id) != ''
                      AND (
                          s.file_id IS NULL
                          OR (s.status = 'error' AND (s.checked_at IS NULL OR datetime(s.checked_at) < datetime('now', '-7 days')))
                      )
                      {after_clause}
                    ORDER BY f.id ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()

            if not rows:
                return

            for row in rows:
                yield dict(row)

            last_id = rows[-1]["id"]
            if len(rows) < batch:
                return


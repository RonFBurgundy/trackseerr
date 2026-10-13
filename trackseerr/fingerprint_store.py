"""AcoustID fingerprint + lookup cache, one row per library file.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``). A row is only trusted while the file's path,
size and mtime still match what was fingerprinted.
"""

from __future__ import annotations

import json
from typing import Any, Optional

_COLUMNS = "file_id, file_path, size_bytes, mtime_ns, duration, fingerprint, status, matches_json, error, looked_up_at"
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

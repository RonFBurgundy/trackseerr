"""Search log: one row per indexer/Soulseek search with what triggered it.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``). Rows older than 30 days are pruned on write.
"""

from __future__ import annotations

from typing import Any

from trackseerr.list_index import SortDef, build_index

_COLUMNS = (
    "id", "created_at", "source_name", "source_type", "query_artist", "query_title", "query_album", "rung",
    "result_count", "duration_ms", "error", "trigger_kind", "trigger_ref", "trigger_label", "request_id",
    "track_id", "album_id",
)
_WRITABLE = _COLUMNS[2:]
RETENTION = "-30 days"


class SearchLogStoreMixin:
    def record_search(self, **fields: Any) -> None:
        """Inserts one search row (unknown keys ignored) and prunes rows past retention. Errors propagate."""
        data = {k: v for k, v in fields.items() if k in _WRITABLE}
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        with self._lock:
            self.conn.execute(f"INSERT INTO search_log ({cols}) VALUES ({marks})", tuple(data.values()))
            self.conn.execute("DELETE FROM search_log WHERE created_at < datetime('now', ?)", (RETENTION,))
            self.conn.commit()

    def _search_log_where(
        self,
        source_type: str | None = None,
        request_id: str | None = None,
    ) -> tuple[str, list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if source_type:
            where.append("source_type = ?")
            params.append(source_type)
        if request_id:
            where.append("request_id = ?")
            params.append(request_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        return clause, params

    def list_search_log(
        self,
        page: int,
        page_size: int,
        source_type: str | None = None,
        request_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Newest-first page of search rows plus the total matching count."""
        clause, params = self._search_log_where(source_type=source_type, request_id=request_id)
        with self._lock:
            total = self.conn.execute(f"SELECT COUNT(*) FROM search_log{clause}", params).fetchone()[0]
            rows = self.conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM search_log{clause} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return [dict(r) for r in rows], int(total)

    def count_item_searches(
        self,
        *,
        track_id: str | None = None,
        album_id: str | None = None,
        request_id: str | None = None,
    ) -> int:
        if track_id:
            col, val = "track_id", track_id
        elif album_id:
            col, val = "album_id", album_id
        elif request_id:
            col, val = "request_id", request_id
        else:
            return 0
        with self._lock:
            row = self.conn.execute(
                f"SELECT COUNT(*) FROM search_log WHERE rung = 1 AND {col} = ?",
                (str(val),),
            ).fetchone()
        return int(row[0]) if row else 0

    def search_log_index(
        self,
        sort_dir: str = "desc",
        source_type: str | None = None,
        request_id: str | None = None,
    ) -> tuple[int, list[dict[str, Any]]]:
        """(total, groups) for search_log ordered by created_at DESC, matching list_search_log."""
        clause, params = self._search_log_where(source_type=source_type, request_id=request_id)
        with self._lock:
            return build_index(self.conn, f"FROM search_log{clause}", params, SortDef("created_at", "event"), sort_dir)

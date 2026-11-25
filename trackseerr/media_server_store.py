"""Media servers store mixin for TrackSeerr database.

Provides CRUD and login-server selection for the ``media_servers`` table.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

MEDIA_SERVER_KINDS = ("plex", "subsonic", "jellyfin")
_SOURCES = ("settings", "env")
_EDITABLE = (
    "name",
    "url",
    "username",
    "password",
    "api_key",
    "credentials_type",
    "music_section",
    "verify_ssl",
    "enabled",
    "use_refresh",
    "use_playlists",
    "use_users",
    "use_scrobbles",
    "sort_order",
)
_BOOL_COLUMNS = (
    "verify_ssl",
    "enabled",
    "use_refresh",
    "use_playlists",
    "use_users",
    "use_scrobbles",
    "is_login",
)
_COLUMNS = (
    "id, kind, source, name, url, username, password, api_key, credentials_type, "
    "music_section, verify_ssl, enabled, use_refresh, use_playlists, use_users, "
    "use_scrobbles, is_login, path_mapping_json, sort_order, created_at, updated_at"
)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a ``media_servers`` SQLite row into a typed dictionary."""
    data = dict(row)
    for col in _BOOL_COLUMNS:
        data[col] = bool(data[col])
    data["sort_order"] = int(data["sort_order"])
    raw_mapping = data.pop("path_mapping_json", "") or ""
    mapping: Optional[dict[str, Any]] = None
    if raw_mapping:
        try:
            parsed = json.loads(raw_mapping)
            if isinstance(parsed, dict):
                mapping = parsed
            else:
                logger.warning("Ignoring non-dict path mapping for media server %s", data.get("id"))
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid JSON path mapping for media server %s", data.get("id"))
    data["path_mapping"] = mapping
    return data


def _coerce_editable(key: str, value: Any) -> Any:
    """Validate and coerce an editable ``media_servers`` column value for SQLite storage."""
    if key == "name":
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("Media server name must not be blank")
        return cleaned
    if key in _BOOL_COLUMNS:
        return int(bool(value))
    if key == "sort_order":
        return 0 if value is None else int(value)
    return str(value if value is not None else "")


class MediaServerStoreMixin:
    """Mixin for Database providing ``media_servers`` store operations."""

    def list_media_server_rows(self) -> list[dict[str, Any]]:
        """Return all media server rows ordered by sort_order, created_at, id."""
        with self.reader() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM media_servers ORDER BY sort_order, created_at, id"
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def get_media_server_row(self, server_id: str) -> Optional[dict[str, Any]]:
        """Return a single media server row by ID, or None if not found."""
        if not server_id:
            return None
        with self.reader() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM media_servers WHERE id = ?",
                (str(server_id),),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def insert_media_server_row(self, values: dict[str, Any]) -> dict[str, Any]:
        """Insert a new media server row and return the stored dictionary."""
        kind = str(values.get("kind") or "").strip()
        if kind not in MEDIA_SERVER_KINDS:
            raise ValueError(f"Unsupported media server kind: {kind!r}")

        name = _coerce_editable("name", values.get("name"))

        raw_source = values.get("source")
        source = "settings" if raw_source is None else str(raw_source).strip()
        if source not in _SOURCES:
            raise ValueError(f"Unsupported media server source: {source!r}")

        raw_id = values.get("id")
        cleaned_id = str(raw_id).strip() if raw_id is not None else ""
        server_id = cleaned_id or uuid.uuid4().hex

        cols = ["id", "kind", "source", "name"]
        params: list[Any] = [server_id, kind, source, name]
        for key in _EDITABLE:
            if key != "name" and key in values and values[key] is not None:
                cols.append(key)
                params.append(_coerce_editable(key, values[key]))

        placeholders = ", ".join("?" for _ in cols)
        with self._lock:
            self.conn.execute(
                f"INSERT INTO media_servers ({', '.join(cols)}) VALUES ({placeholders})",
                params,
            )
            self.conn.commit()
            row = self.conn.execute(
                f"SELECT {_COLUMNS} FROM media_servers WHERE id = ?",
                (server_id,),
            ).fetchone()
        assert row is not None
        return _row_to_dict(row)

    def update_media_server_row(self, server_id: str, values: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Update editable columns present in ``values`` and return the updated row, or None if missing."""
        if not server_id:
            return None

        set_clauses: list[str] = []
        params: list[Any] = []
        for key in _EDITABLE:
            if key in values:
                set_clauses.append(f"{key} = ?")
                params.append(_coerce_editable(key, values[key]))
        set_clauses.append("updated_at = CURRENT_TIMESTAMP")
        params.append(str(server_id))

        with self._lock:
            cur = self.conn.execute(
                f"UPDATE media_servers SET {', '.join(set_clauses)} WHERE id = ?",
                params,
            )
            self.conn.commit()
            if cur.rowcount == 0:
                return None
            row = self.conn.execute(
                f"SELECT {_COLUMNS} FROM media_servers WHERE id = ?",
                (str(server_id),),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def delete_media_server_row(self, server_id: str) -> bool:
        """Delete a media server row by ID. Returns True if a row was deleted."""
        if not server_id:
            return False
        with self._lock:
            cur = self.conn.execute("DELETE FROM media_servers WHERE id = ?", (str(server_id),))
            self.conn.commit()
            return cur.rowcount > 0

    def set_login_media_server(self, server_id: Optional[str]) -> None:
        """Select the unique Plex login server (or clear all when ``server_id`` is None) in one transaction."""
        with self._lock:
            if server_id is not None:
                row = self.conn.execute(
                    "SELECT kind FROM media_servers WHERE id = ?",
                    (str(server_id),),
                ).fetchone()
                if row is None:
                    raise ValueError(f"Media server {server_id!r} does not exist")
                if row["kind"] != "plex":
                    raise ValueError("Only Plex media servers can be used for login")
            try:
                self.conn.execute("UPDATE media_servers SET is_login = 0")
                if server_id is not None:
                    self.conn.execute(
                        "UPDATE media_servers SET is_login = 1 WHERE id = ?",
                        (str(server_id),),
                    )
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                raise

    def set_media_server_row_path_mapping(self, server_id: str, mapping: Optional[dict[str, Any]]) -> None:
        """Save (or with None clear) the path mapping for a media server row."""
        raw = json.dumps(mapping) if mapping else ""
        with self._lock:
            self.conn.execute(
                "UPDATE media_servers SET path_mapping_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (raw, str(server_id)),
            )
            self.conn.commit()

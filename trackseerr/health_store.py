"""System health check persistence and health settings store.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``). Persists probe states,
health item alerts, and health settings kv.
"""

from __future__ import annotations

import json
from typing import Any

DEFAULT_HEALTH_SETTINGS: dict[str, int] = {
    "indexer_fail_minutes": 15,
    "client_fail_minutes": 5,
    "media_server_fail_minutes": 10,
    "disk_free_min_gb": 20,
    "download_stall_minutes": 60,
    "renotify_hours": 24,
}

_HEALTH_CHECK_COLUMNS = (
    "check_key, kind, label, state, message, failing_since, last_checked_at, notified_state, notified_at"
)


class HealthStoreMixin:
    """Database mixin for health check state, item alerts, and health settings."""

    def get_health_check(self, key: str) -> dict[str, Any] | None:
        """Returns the health check row for ``key``, or None if not found."""
        with self._lock:
            row = self.conn.execute(
                f"SELECT {_HEALTH_CHECK_COLUMNS} FROM health_checks WHERE check_key = ?",
                (str(key),),
            ).fetchone()
            return dict(row) if row else None

    def list_health_checks(self) -> list[dict[str, Any]]:
        """Returns all health check rows ordered by kind, label."""
        with self._lock:
            rows = self.conn.execute(
                f"SELECT {_HEALTH_CHECK_COLUMNS} FROM health_checks ORDER BY kind ASC, label ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def upsert_health_check(
        self,
        key: str,
        kind: str,
        label: str,
        state: str,
        message: str | None,
        failing_since: str | None,
        last_checked_at: str,
        notified_state: str | None = None,
        notified_at: str | None = None,
    ) -> None:
        """Inserts or updates a health check row."""
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO health_checks (
                    check_key, kind, label, state, message, failing_since,
                    last_checked_at, notified_state, notified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(check_key) DO UPDATE SET
                    kind = excluded.kind,
                    label = excluded.label,
                    state = excluded.state,
                    message = excluded.message,
                    failing_since = excluded.failing_since,
                    last_checked_at = excluded.last_checked_at,
                    notified_state = excluded.notified_state,
                    notified_at = excluded.notified_at
                """,
                (
                    str(key),
                    str(kind),
                    str(label),
                    str(state),
                    str(message) if message is not None else None,
                    str(failing_since) if failing_since is not None else None,
                    str(last_checked_at),
                    str(notified_state) if notified_state is not None else None,
                    str(notified_at) if notified_at is not None else None,
                ),
            )
            self.conn.commit()

    def delete_health_checks_except(self, kind: str, keep_keys: list[str]) -> int:
        """Deletes health checks of ``kind`` whose check_key is not in ``keep_keys``."""
        with self._lock:
            if not keep_keys:
                cur = self.conn.execute(
                    "DELETE FROM health_checks WHERE kind = ?",
                    (str(kind),),
                )
            else:
                placeholders = ", ".join(["?"] * len(keep_keys))
                cur = self.conn.execute(
                    f"DELETE FROM health_checks WHERE kind = ? AND check_key NOT IN ({placeholders})",
                    [str(kind)] + [str(k) for k in keep_keys],
                )
            self.conn.commit()
            return int(cur.rowcount or 0)

    def has_health_item_alert(self, event: str, ref: str) -> bool:
        """Returns True if an item alert has been recorded for (event, ref)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM health_item_alerts WHERE event = ? AND ref = ? LIMIT 1",
                (str(event), str(ref)),
            ).fetchone()
            return row is not None

    def record_health_item_alert(self, event: str, ref: str, at: str) -> None:
        """Records an item alert for (event, ref)."""
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO health_item_alerts (event, ref, notified_at)
                VALUES (?, ?, ?)
                ON CONFLICT(event, ref) DO UPDATE SET notified_at = excluded.notified_at
                """,
                (str(event), str(ref), str(at)),
            )
            self.conn.commit()

    def prune_health_item_alerts(self, active_refs: list[str]) -> int:
        """Deletes item alerts whose ref is not in ``active_refs``."""
        with self._lock:
            if not active_refs:
                cur = self.conn.execute("DELETE FROM health_item_alerts")
            else:
                placeholders = ", ".join(["?"] * len(active_refs))
                cur = self.conn.execute(
                    f"DELETE FROM health_item_alerts WHERE ref NOT IN ({placeholders})",
                    [str(r) for r in active_refs],
                )
            self.conn.commit()
            return int(cur.rowcount or 0)

    def get_health_settings(self) -> dict[str, int]:
        """Returns current health settings merged with defaults."""
        raw = self.get_kv("health_settings")
        stored: dict[str, Any] = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    stored = parsed
            except (ValueError, TypeError):
                stored = {}
        out: dict[str, int] = {}
        for k, default_val in DEFAULT_HEALTH_SETTINGS.items():
            if k in stored:
                try:
                    val = int(stored[k])
                    out[k] = max(0, val)
                except (ValueError, TypeError):
                    out[k] = default_val
            else:
                out[k] = default_val
        return out

    def set_health_settings(self, settings: dict[str, Any]) -> dict[str, int]:
        """Updates health settings; unknown keys dropped, ints clamped >= 0."""
        current = self.get_health_settings()
        for k in DEFAULT_HEALTH_SETTINGS:
            if k in settings:
                try:
                    val = int(settings[k])
                    current[k] = max(0, val)
                except (ValueError, TypeError):
                    pass
        self.set_kv("health_settings", json.dumps(current))
        return current

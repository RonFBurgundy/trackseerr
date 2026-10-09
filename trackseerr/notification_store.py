"""Notification channels and user in-app notifications, preferences, and web push.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Union

from trackseerr.models import (
    NotificationChannel,
    NotificationChannelType,
    NotificationEvent,
)

logger = logging.getLogger(__name__)


class NotificationStoreMixin:
    # -------------------------------------------------------------------------
    # Notification Channels CRUD
    # -------------------------------------------------------------------------

    def create_notification_channel(
        self, channel: Union[dict[str, Any], NotificationChannel]
    ) -> dict[str, Any]:
        """Creates or updates a notification channel."""
        if isinstance(channel, NotificationChannel):
            c_id = channel.id
            name = channel.name
            channel_type = (
                channel.channel_type.value
                if isinstance(channel.channel_type, NotificationChannelType)
                else str(channel.channel_type)
            )
            enabled = 1 if channel.enabled else 0
            config_json = json.dumps(channel.config if isinstance(channel.config, dict) else {})
            events_list = [
                e.value if isinstance(e, NotificationEvent) else str(e)
                for e in (channel.events or [])
            ]
            events_json = json.dumps(events_list)
            owner_user_id = channel.owner_user_id
        else:
            c = dict(channel)
            c_id = str(c.get("id") or "")
            name = str(c.get("name") or "")
            ctype_raw = c.get("channel_type", "")
            channel_type = (
                ctype_raw.value
                if isinstance(ctype_raw, NotificationChannelType)
                else str(ctype_raw)
            )
            enabled = 1 if c.get("enabled", True) else 0
            cfg = c.get("config")
            config_json = json.dumps(cfg if isinstance(cfg, dict) else {})
            raw_events = c.get("events") or []
            events_list = [
                e.value if isinstance(e, NotificationEvent) else str(e)
                for e in raw_events
            ]
            events_json = json.dumps(events_list)
            owner_user_id = c.get("owner_user_id")

        with self._lock:
            self.conn.execute(
                """
                INSERT INTO notification_channels (
                    id, name, channel_type, enabled, config_json, events_json, owner_user_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    channel_type = excluded.channel_type,
                    enabled = excluded.enabled,
                    config_json = excluded.config_json,
                    events_json = excluded.events_json,
                    owner_user_id = excluded.owner_user_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (c_id, name, channel_type, enabled, config_json, events_json, owner_user_id),
            )
            self.conn.commit()

        ret = self.get_notification_channel(c_id)
        if not ret:
            raise sqlite3.OperationalError(f"Failed to fetch saved notification channel '{c_id}'")
        return ret

    def get_notification_channel(self, channel_id: str) -> Optional[dict[str, Any]]:
        """Retrieves a notification channel by ID."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM notification_channels WHERE id = ?", (str(channel_id),)
            )
            row = cur.fetchone()
        if not row:
            return None
        return self._row_to_channel_dict(row)

    def list_notification_channels(
        self,
        enabled_only: bool = False,
        owner_user_id: Optional[str] = None,
        global_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Lists notification channels, optionally filtering by enabled status and owner."""
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if global_only:
            clauses.append("owner_user_id IS NULL")
        elif owner_user_id is not None:
            clauses.append("owner_user_id = ?")
            params.append(str(owner_user_id))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM notification_channels {where} ORDER BY created_at ASC"
        with self._lock:
            cur = self.conn.execute(query, params)
            rows = cur.fetchall()
        return [self._row_to_channel_dict(r) for r in rows]

    def count_user_notification_channels(self, user_id: str) -> int:
        """Returns the number of notification channels owned by a user."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM notification_channels WHERE owner_user_id = ?",
                (str(user_id),),
            )
            return int(cur.fetchone()[0])

    def update_notification_channel(
        self, channel_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Updates fields of an existing notification channel."""
        allowed = {"name", "channel_type", "enabled", "config", "events", "owner_user_id"}
        filtered: dict[str, Any] = {}
        for k, v in updates.items():
            if k in allowed:
                if k == "enabled":
                    filtered["enabled"] = 1 if v else 0
                elif k == "config":
                    filtered["config_json"] = json.dumps(v if isinstance(v, dict) else {})
                elif k == "events":
                    ev_list = [
                        e.value if isinstance(e, NotificationEvent) else str(e)
                        for e in (v or [])
                    ]
                    filtered["events_json"] = json.dumps(ev_list)
                elif k == "channel_type":
                    filtered["channel_type"] = (
                        v.value if isinstance(v, NotificationChannelType) else str(v)
                    )
                else:
                    filtered[k] = v

        if filtered:
            set_clauses = [f"{k} = ?" for k in filtered.keys()]
            set_clauses.append("updated_at = CURRENT_TIMESTAMP")
            values = list(filtered.values())
            values.append(str(channel_id))

            with self._lock:
                self.conn.execute(
                    f"UPDATE notification_channels SET {', '.join(set_clauses)} WHERE id = ?",
                    values,
                )
                self.conn.commit()

        ret = self.get_notification_channel(channel_id)
        if not ret:
            raise KeyError(f"Notification channel '{channel_id}' not found")
        return ret

    def delete_notification_channel(self, channel_id: str) -> bool:
        """Deletes a notification channel by ID."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM notification_channels WHERE id = ?", (str(channel_id),)
            )
            self.conn.commit()
            return cur.rowcount > 0

    def _row_to_channel_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["enabled"] = bool(d.get("enabled", 1))
        config_raw = d.pop("config_json", "{}")
        try:
            d["config"] = json.loads(config_raw) if config_raw else {}
        except (json.JSONDecodeError, TypeError):
            d["config"] = {}
        events_raw = d.pop("events_json", "[]")
        try:
            d["events"] = json.loads(events_raw) if events_raw else []
        except (json.JSONDecodeError, TypeError):
            d["events"] = []
        return d

    # -------------------------------------------------------------------------
    # User In-App Notifications & Preferences & Web Push
    # -------------------------------------------------------------------------

    def create_user_notification(
        self,
        user_id: str,
        event: str,
        title: str,
        message: str,
        link: Optional[str] = None,
    ) -> dict[str, Any]:
        """Creates an in-app user notification and prunes to newest 200 per user."""
        notif_id = f"notif-{uuid.uuid4().hex[:12]}"
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO user_notifications (id, user_id, event, title, message, link, created_at, read_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (notif_id, str(user_id), str(event), str(title), str(message), link, created_at),
            )
            # Prune to newest 200 per user
            self.conn.execute(
                """
                DELETE FROM user_notifications
                WHERE user_id = ?
                  AND id NOT IN (
                      SELECT id FROM user_notifications
                      WHERE user_id = ?
                      ORDER BY created_at DESC, id DESC
                      LIMIT 200
                  )
                """,
                (str(user_id), str(user_id)),
            )
            self.conn.commit()
            cur = self.conn.execute("SELECT * FROM user_notifications WHERE id = ?", (notif_id,))
            row = cur.fetchone()
        return dict(row)

    def list_user_notifications(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Returns (items, total, unread_count) for a user's notifications, newest first."""
        page = max(1, page)
        page_size = max(1, min(200, page_size))
        offset = (page - 1) * page_size
        with self._lock:
            total = self.conn.execute(
                "SELECT COUNT(*) FROM user_notifications WHERE user_id = ?",
                (str(user_id),),
            ).fetchone()[0]
            unread = self.conn.execute(
                "SELECT COUNT(*) FROM user_notifications WHERE user_id = ? AND read_at IS NULL",
                (str(user_id),),
            ).fetchone()[0]
            cur = self.conn.execute(
                """
                SELECT * FROM user_notifications
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (str(user_id), page_size, offset),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows], int(total), int(unread)

    def get_user_notification_unread_count(self, user_id: str) -> int:
        """Returns the count of unread notifications for a user."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM user_notifications WHERE user_id = ? AND read_at IS NULL",
                (str(user_id),),
            ).fetchone()
            return int(row[0]) if row else 0

    def mark_user_notification_read(self, notification_id: str, user_id: str) -> Optional[dict[str, Any]]:
        """Marks a single user notification as read."""
        read_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self.conn.execute(
                """
                UPDATE user_notifications
                SET read_at = COALESCE(read_at, ?)
                WHERE id = ? AND user_id = ?
                """,
                (read_at, str(notification_id), str(user_id)),
            )
            self.conn.commit()
            if cur.rowcount == 0:
                check = self.conn.execute(
                    "SELECT 1 FROM user_notifications WHERE id = ? AND user_id = ?",
                    (str(notification_id), str(user_id)),
                ).fetchone()
                if not check:
                    return None
            row = self.conn.execute(
                "SELECT * FROM user_notifications WHERE id = ?",
                (str(notification_id),),
            ).fetchone()
            return dict(row) if row else None

    def mark_all_user_notifications_read(self, user_id: str) -> int:
        """Marks all notifications for a user as read."""
        read_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self.conn.execute(
                """
                UPDATE user_notifications
                SET read_at = COALESCE(read_at, ?)
                WHERE user_id = ? AND read_at IS NULL
                """,
                (read_at, str(user_id)),
            )
            self.conn.commit()
            return int(cur.rowcount)

    def delete_user_notification(self, notification_id: str, user_id: str) -> bool:
        """Deletes a single notification owned by user_id."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM user_notifications WHERE id = ? AND user_id = ?",
                (str(notification_id), str(user_id)),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def create_or_update_web_push_subscription(
        self,
        user_id: str,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: Optional[str] = None,
    ) -> dict[str, Any]:
        """Upserts a Web Push subscription for a user."""
        sub_id = f"wps-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO web_push_subscriptions (
                    id, user_id, endpoint, p256dh, auth, user_agent, created_at, failure_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(endpoint) DO UPDATE SET
                    user_id = excluded.user_id,
                    p256dh = excluded.p256dh,
                    auth = excluded.auth,
                    user_agent = excluded.user_agent,
                    failure_count = 0
                """,
                (sub_id, str(user_id), str(endpoint), str(p256dh), str(auth), user_agent, now),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM web_push_subscriptions WHERE endpoint = ?",
                (str(endpoint),),
            ).fetchone()
            return dict(row)

    def count_user_web_push_subscriptions(self, user_id: str) -> int:
        """Returns count of active push subscriptions for user."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM web_push_subscriptions WHERE user_id = ?",
                (str(user_id),),
            )
            return int(cur.fetchone()[0])

    def get_user_web_push_subscriptions(self, user_id: str) -> list[dict[str, Any]]:
        """Returns all push subscriptions for user."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM web_push_subscriptions WHERE user_id = ? ORDER BY created_at ASC",
                (str(user_id),),
            )
            return [dict(r) for r in cur.fetchall()]

    def delete_web_push_subscription(self, endpoint: str, user_id: Optional[str] = None) -> bool:
        """Deletes a push subscription by endpoint, optionally scoping to user."""
        with self._lock:
            if user_id is not None:
                cur = self.conn.execute(
                    "DELETE FROM web_push_subscriptions WHERE endpoint = ? AND user_id = ?",
                    (str(endpoint), str(user_id)),
                )
            else:
                cur = self.conn.execute(
                    "DELETE FROM web_push_subscriptions WHERE endpoint = ?",
                    (str(endpoint),),
                )
            self.conn.commit()
            return cur.rowcount > 0

    def delete_web_push_subscription_by_id(self, sub_id: str) -> bool:
        """Deletes a push subscription by ID."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM web_push_subscriptions WHERE id = ?",
                (str(sub_id),),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def increment_web_push_failure(self, sub_id: str) -> int:
        """Increments failure count for a subscription and returns the new count."""
        with self._lock:
            self.conn.execute(
                "UPDATE web_push_subscriptions SET failure_count = failure_count + 1 WHERE id = ?",
                (str(sub_id),),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT failure_count FROM web_push_subscriptions WHERE id = ?",
                (str(sub_id),),
            ).fetchone()
            return int(row["failure_count"]) if row else 0

    def record_web_push_success(self, sub_id: str) -> None:
        """Resets failure count and updates last_success_at."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.conn.execute(
                "UPDATE web_push_subscriptions SET failure_count = 0, last_success_at = ? WHERE id = ?",
                (now, str(sub_id)),
            )
            self.conn.commit()

    def get_user_notification_pref(self, user_id: str, event: str) -> tuple[bool, bool]:
        """Returns (in_app, push) for (user_id, event). Missing row defaults to (True, True)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT in_app, push FROM user_notification_prefs WHERE user_id = ? AND event = ?",
                (str(user_id), str(event)),
            ).fetchone()
            if not row:
                return True, True
            return bool(row["in_app"]), bool(row["push"])

    def get_all_user_notification_prefs(self, user_id: str) -> dict[str, dict[str, bool]]:
        """Returns user notification prefs by event for user_id."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT event, in_app, push FROM user_notification_prefs WHERE user_id = ?",
                (str(user_id),),
            )
            return {
                row["event"]: {"in_app": bool(row["in_app"]), "push": bool(row["push"])}
                for row in cur.fetchall()
            }

    def set_user_notification_pref(
        self, user_id: str, event: str, in_app: bool, push: bool
    ) -> dict[str, bool]:
        """Upserts notification prefs for (user_id, event)."""
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO user_notification_prefs (user_id, event, in_app, push)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, event) DO UPDATE SET
                    in_app = excluded.in_app,
                    push = excluded.push
                """,
                (str(user_id), str(event), 1 if in_app else 0, 1 if push else 0),
            )
            self.conn.commit()
        return {"in_app": in_app, "push": push}

    def get_or_create_vapid_keys(self) -> tuple[str, str, str]:
        """Returns (public_key_b64, private_key_pem, vapid_sub) from general_settings, generating if missing."""
        with self._lock:
            self._ensure_general_row()
            row = self.conn.execute(
                "SELECT vapid_public_key, vapid_private_key, vapid_sub FROM general_settings WHERE id = 1"
            ).fetchone()
            pub = str(row["vapid_public_key"] or "").strip() if row and "vapid_public_key" in row.keys() else ""
            priv = str(row["vapid_private_key"] or "").strip() if row and "vapid_private_key" in row.keys() else ""
            sub = str(row["vapid_sub"] or "").strip() if row and "vapid_sub" in row.keys() else ""

            if not pub or not priv:
                import base64
                from cryptography.hazmat.primitives import serialization
                from pywebpush import Vapid

                v = Vapid()
                v.generate_keys()
                raw_pub = v.public_key.public_bytes(
                    encoding=serialization.Encoding.X962,
                    format=serialization.PublicFormat.UncompressedPoint,
                )
                pub = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode("utf-8")
                priv = v.private_pem().decode("utf-8")
                if not sub:
                    sub = "mailto:admin@trackseerr.local"

                self.conn.execute(
                    """
                    UPDATE general_settings
                    SET vapid_public_key = ?, vapid_private_key = ?, vapid_sub = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                    """,
                    (pub, priv, sub),
                )
                self.conn.commit()
            elif not sub:
                sub = "mailto:admin@trackseerr.local"
                self.conn.execute(
                    "UPDATE general_settings SET vapid_sub = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                    (sub,),
                )
                self.conn.commit()

            return pub, priv, sub


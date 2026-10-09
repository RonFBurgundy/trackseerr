"""Scrobbling settings, per-user scrobble configs, Last.fm auth states, user listens, and tailored mixes.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union

from trackseerr.storage_common import (
    _now_us,
    ts_to_us,
)

logger = logging.getLogger(__name__)


class ScrobbleStoreMixin:
    # -------------------------------------------------------------------------
    # Scrobbling: server-level settings
    # -------------------------------------------------------------------------

    def _ensure_general_row(self) -> None:
        """Creates the singleton ``general_settings`` row when missing, and commits that insert at once.

        Must not leave a transaction open: a bare ``INSERT OR IGNORE`` that is never committed (the usual case, the row
        exists) keeps the write lock on this connection until some other thread happens to commit, and every other
        connection's write then fails with "database is locked".
        """
        if self.conn.execute("SELECT 1 FROM general_settings WHERE id = 1").fetchone() is None:
            self.conn.execute("INSERT OR IGNORE INTO general_settings (id, application_url) VALUES (1, '')")
            self.conn.commit()

    def get_plex_webhook_secret(self) -> str:
        """Return the Plex webhook secret, generating and persisting one on first read."""
        with self._lock:
            self._ensure_general_row()
            row = self.conn.execute("SELECT plex_webhook_secret FROM general_settings WHERE id = 1").fetchone()
            secret = str(row["plex_webhook_secret"] or "").strip() if row else ""
            if not secret:
                secret = secrets.token_urlsafe(32)
                self.conn.execute(
                    "UPDATE general_settings SET plex_webhook_secret = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                    (secret,),
                )
                self.conn.commit()
            return secret

    def rotate_plex_webhook_secret(self) -> str:
        """Generate, persist and return a fresh Plex webhook secret."""
        secret = secrets.token_urlsafe(32)
        with self._lock:
            self._ensure_general_row()
            self.conn.execute(
                "UPDATE general_settings SET plex_webhook_secret = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                (secret,),
            )
            self.conn.commit()
        return secret

    def get_lastfm_settings(self) -> dict[str, Any]:
        """Return the DB-stored Last.fm API key/secret (internal use only; never expose the secret)."""
        with self._lock:
            self._ensure_general_row()
            row = self.conn.execute(
                "SELECT lastfm_api_key, lastfm_api_secret FROM general_settings WHERE id = 1"
            ).fetchone()
            return {
                "lastfm_api_key": str(row["lastfm_api_key"] or "").strip() if row else "",
                "lastfm_api_secret": str(row["lastfm_api_secret"] or "").strip() if row else "",
            }

    def set_lastfm_settings(
        self, api_key: Optional[str] = None, api_secret: Optional[str] = None
    ) -> dict[str, Any]:
        """Persist Last.fm API credentials. ``None`` leaves a field unchanged."""
        sets: list[str] = []
        vals: list[Any] = []
        if api_key is not None:
            sets.append("lastfm_api_key = ?")
            vals.append(api_key.strip())
        if api_secret is not None:
            sets.append("lastfm_api_secret = ?")
            vals.append(api_secret.strip())
        if sets:
            with self._lock:
                self._ensure_general_row()
                self.conn.execute(
                    f"UPDATE general_settings SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                    vals,
                )
                self.conn.commit()
        return self.get_lastfm_settings()

    def get_plex_history_poll_minutes(self) -> int:
        with self._lock:
            self._ensure_general_row()
            row = self.conn.execute("SELECT plex_history_poll_minutes FROM general_settings WHERE id = 1").fetchone()
            if not row or row["plex_history_poll_minutes"] is None:
                return 15
            return int(row["plex_history_poll_minutes"])

    def set_plex_history_poll_minutes(self, minutes: int) -> int:
        value = max(0, int(minutes))
        with self._lock:
            self._ensure_general_row()
            self.conn.execute(
                "UPDATE general_settings SET plex_history_poll_minutes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                (value,),
            )
            self.conn.commit()
        return value

    def get_scrobble_state(self, key: str) -> Optional[str]:
        with self._lock:
            row = self.conn.execute("SELECT value FROM scrobble_state WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else None

    def set_scrobble_state(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO scrobble_state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (key, str(value)),
            )
            self.conn.commit()

    # -------------------------------------------------------------------------
    # Scrobbling: per-user config
    # -------------------------------------------------------------------------

    _SCROBBLE_CONFIG_FIELDS = (
        "scrobbling_enabled",
        "lastfm_username",
        "lastfm_session_key",
        "listenbrainz_token",
        "listenbrainz_username",
    )

    @staticmethod
    def _scrobble_config_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["scrobbling_enabled"] = bool(d.get("scrobbling_enabled"))
        return d

    def get_scrobble_config(self, user_id: str) -> Optional[dict[str, Any]]:
        """Raw scrobble config row for a user (includes secrets; internal use), or None."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM user_scrobble_configs WHERE user_id = ?", (str(user_id),)
            ).fetchone()
            return self._scrobble_config_row(row) if row else None

    def upsert_scrobble_config(self, user_id: str, **fields: Any) -> dict[str, Any]:
        """Create or update a user's scrobble config. Unknown field names raise ValueError."""
        unknown = set(fields) - set(self._SCROBBLE_CONFIG_FIELDS)
        if unknown:
            raise ValueError(f"Unknown scrobble config fields: {sorted(unknown)}")
        uid = str(user_id)
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            updates[k] = (1 if v else 0) if k == "scrobbling_enabled" else v
        with self._lock:
            self.conn.execute("INSERT OR IGNORE INTO user_scrobble_configs (user_id) VALUES (?)", (uid,))
            if updates:
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                self.conn.execute(
                    f"UPDATE user_scrobble_configs SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    [*updates.values(), uid],
                )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM user_scrobble_configs WHERE user_id = ?", (uid,)).fetchone()
            return self._scrobble_config_row(row)

    def list_scrobble_configs(self) -> list[dict[str, Any]]:
        """Every user joined with their scrobble config (defaults when unset). Includes secrets; internal use."""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT u.id AS user_id, u.username AS username,
                       COALESCE(c.scrobbling_enabled, 1) AS scrobbling_enabled,
                       c.lastfm_username, c.lastfm_session_key,
                       c.listenbrainz_token, c.listenbrainz_username, c.updated_at
                FROM users u LEFT JOIN user_scrobble_configs c ON c.user_id = u.id
                ORDER BY u.username COLLATE NOCASE ASC
                """
            ).fetchall()
            return [self._scrobble_config_row(r) for r in rows]

    # -------------------------------------------------------------------------
    # Scrobbling: Last.fm auth states
    # -------------------------------------------------------------------------

    def create_lastfm_auth_state(self, user_id: str, forward_url: Optional[str] = None) -> str:
        """Create a single-use state valid for 10 minutes; expired states are purged."""
        state = secrets.token_urlsafe(24)
        with self._lock:
            self.conn.execute("DELETE FROM lastfm_auth_states WHERE datetime(created_at) < datetime('now', '-10 minutes')")
            self.conn.execute(
                "INSERT INTO lastfm_auth_states (state, user_id, forward_url) VALUES (?, ?, ?)",
                (state, str(user_id), forward_url),
            )
            self.conn.commit()
        return state

    def take_lastfm_auth_state(self, state: str) -> tuple[Optional[dict[str, Any]], str]:
        """Atomically delete a state and report why it was or was not usable.

        Returns ``(record, "ok")`` with ``{user_id, forward_url}``, or ``(None, "unknown")`` for a missing,
        reused or empty state, or ``(None, "expired")`` when it is older than 10 minutes. Freshness is computed
        in Python so any stored timestamp shape (space or ``T`` separator, ``Z`` or an offset) is understood;
        SQLite's ``datetime()`` returns NULL for some of those, which would make every state look stale.
        """
        if not state:
            return None, "unknown"
        with self._lock:
            row = self.conn.execute(
                "SELECT user_id, forward_url, created_at FROM lastfm_auth_states WHERE state = ?",
                (state,),
            ).fetchone()
            if not row:
                return None, "unknown"
            self.conn.execute("DELETE FROM lastfm_auth_states WHERE state = ?", (state,))
            self.conn.commit()
        created_us = ts_to_us(row["created_at"])
        if created_us <= 0 or _now_us() - created_us > 10 * 60 * 1_000_000:
            return None, "expired"
        return {"user_id": row["user_id"], "forward_url": row["forward_url"]}, "ok"

    def consume_lastfm_auth_state(self, state: str) -> Optional[dict[str, Any]]:
        """Atomically delete and return ``{user_id, forward_url}``; None if unknown, reused or older than 10 minutes."""
        return self.take_lastfm_auth_state(state)[0]

    # -------------------------------------------------------------------------
    # Scrobbling: listens
    # -------------------------------------------------------------------------

    @staticmethod
    def _normalize_played_at(value: Optional[Union[str, datetime]]) -> str:
        """Return an ISO-8601 UTC string (seconds precision, +00:00)."""
        if value is None:
            dt = datetime.now(timezone.utc)
        elif isinstance(value, datetime):
            dt = value
        else:
            text = str(value).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")

    def insert_listen(
        self,
        user_id: str,
        artist: str,
        title: str,
        album: Optional[str] = None,
        rating_key: Optional[str] = None,
        duration_ms: Optional[int] = None,
        played_at: Optional[Union[str, datetime]] = None,
        source: str = "plex_webhook",
    ) -> Optional[int]:
        """Insert a listen; returns its id, or None when deduplicated (+/-10 min window)."""
        uid = str(user_id)
        played = self._normalize_played_at(played_at)
        rk = str(rating_key) if rating_key not in (None, "") else None
        with self._lock:
            if rk is not None:
                dup = self.conn.execute(
                    """
                    SELECT 1 FROM user_listens
                    WHERE user_id = ? AND rating_key = ?
                      AND ABS(julianday(played_at) - julianday(?)) * 1440.0 <= 10.0
                    LIMIT 1
                    """,
                    (uid, rk, played),
                ).fetchone()
            else:
                dup = self.conn.execute(
                    """
                    SELECT 1 FROM user_listens
                    WHERE user_id = ? AND lower(artist) = lower(?) AND lower(title) = lower(?)
                      AND ABS(julianday(played_at) - julianday(?)) * 1440.0 <= 10.0
                    LIMIT 1
                    """,
                    (uid, artist, title, played),
                ).fetchone()
            if dup:
                return None
            cfg = self.conn.execute(
                "SELECT * FROM user_scrobble_configs WHERE user_id = ?", (uid,)
            ).fetchone()
            enabled = bool(cfg["scrobbling_enabled"]) if cfg else True
            lf_status = "pending" if (cfg and enabled and cfg["lastfm_session_key"]) else "skipped"
            lb_status = "pending" if (cfg and enabled and cfg["listenbrainz_token"]) else "skipped"
            cur = self.conn.execute(
                """
                INSERT INTO user_listens
                    (user_id, artist, title, album, rating_key, duration_ms, played_at, source,
                     lastfm_status, listenbrainz_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (uid, artist, title, album, rk, duration_ms, played, source, lf_status, lb_status),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def get_listen(self, listen_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM user_listens WHERE id = ?", (int(listen_id),)).fetchone()
            return dict(row) if row else None

    def list_listens(self, user_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT * FROM user_listens WHERE user_id = ?
                ORDER BY played_at DESC, id DESC LIMIT ? OFFSET ?
                """,
                (str(user_id), max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_pending_forwards(
        self, max_age_days: int = 14, limit: int = 200, max_attempts: int = 5
    ) -> list[dict[str, Any]]:
        """Listens with a pending/failed service, younger than ``max_age_days`` and under ``max_attempts``."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(max_age_days))).isoformat(timespec="seconds")
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT * FROM user_listens
                WHERE (lastfm_status IN ('pending', 'failed') OR listenbrainz_status IN ('pending', 'failed'))
                  AND forward_attempts < ?
                  AND julianday(played_at) >= julianday(?)
                ORDER BY played_at ASC, id ASC LIMIT ?
                """,
                (int(max_attempts), cutoff, int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_forward_result(
        self, listen_id: int, service: str, status: str, error: Optional[str] = None
    ) -> None:
        """Record a forward outcome for ``service`` ('lastfm' | 'listenbrainz'). A 'failed' status bumps attempts."""
        if service not in ("lastfm", "listenbrainz"):
            raise ValueError(f"Unknown scrobble service: {service}")
        if status not in ("skipped", "pending", "sent", "failed"):
            raise ValueError(f"Invalid forward status: {status}")
        column = f"{service}_status"
        bump = ", forward_attempts = forward_attempts + 1" if status == "failed" else ""
        with self._lock:
            self.conn.execute(
                f"UPDATE user_listens SET {column} = ?, last_forward_error = ?{bump} WHERE id = ?",
                (status, (error[:500] if error else None), int(listen_id)),
            )
            self.conn.commit()

    def top_artists(self, user_id: str, since_iso: str, limit: int = 10) -> list[dict[str, Any]]:
        since = self._normalize_played_at(since_iso)
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT MIN(artist) AS artist, COUNT(*) AS plays FROM user_listens
                WHERE user_id = ? AND julianday(played_at) >= julianday(?)
                GROUP BY lower(artist) ORDER BY plays DESC, lower(artist) ASC LIMIT ?
                """,
                (str(user_id), since, int(limit)),
            ).fetchall()
            return [{"artist": r["artist"], "plays": int(r["plays"])} for r in rows]

    def top_tracks(self, user_id: str, since_iso: str, limit: int = 50) -> list[dict[str, Any]]:
        since = self._normalize_played_at(since_iso)
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT MIN(artist) AS artist, MIN(title) AS title, MAX(album) AS album, COUNT(*) AS plays
                FROM user_listens
                WHERE user_id = ? AND julianday(played_at) >= julianday(?)
                GROUP BY lower(artist), lower(title)
                ORDER BY plays DESC, lower(artist) ASC, lower(title) ASC LIMIT ?
                """,
                (str(user_id), since, int(limit)),
            ).fetchall()
            return [
                {"artist": r["artist"], "title": r["title"], "album": r["album"], "plays": int(r["plays"])}
                for r in rows
            ]

    def heard_track_keys(self, user_id: str) -> set[tuple[str, str]]:
        """Lowercased (artist, title) of everything the user has ever listened to."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT lower(artist) AS a, lower(title) AS t FROM user_listens WHERE user_id = ?",
                (str(user_id),),
            ).fetchall()
            return {(r["a"], r["t"]) for r in rows}

    # -------------------------------------------------------------------------
    # Tailored mixes
    # -------------------------------------------------------------------------

    MIX_TYPES = ("discover_weekly", "daily_blend", "artist_radio")
    _MIX_UPDATABLE = (
        "mix_type",
        "name",
        "seed_artist",
        "track_count",
        "discovery_ratio",
        "seed_window_days",
        "excluded_genres",
        "auto_acquire_missing",
        "max_weekly_acquisitions",
        "quality_profile_id",
        "enabled",
    )

    @staticmethod
    def _mix_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        raw = d.pop("excluded_genres_json", "[]") or "[]"
        try:
            genres = json.loads(raw)
        except json.JSONDecodeError:
            genres = []
        d["excluded_genres"] = [str(g) for g in genres] if isinstance(genres, list) else []
        d["auto_acquire_missing"] = bool(d.get("auto_acquire_missing"))
        d["enabled"] = bool(d.get("enabled"))
        d["discovery_ratio"] = float(d.get("discovery_ratio") if d.get("discovery_ratio") is not None else 0.7)
        return d

    @classmethod
    def _validate_mix_fields(cls, f: dict[str, Any]) -> None:
        if "mix_type" in f and f["mix_type"] not in cls.MIX_TYPES:
            raise ValueError(f"mix_type must be one of {cls.MIX_TYPES}")
        if "name" in f and not str(f["name"] or "").strip():
            raise ValueError("name is required")
        if "track_count" in f and not (5 <= int(f["track_count"]) <= 100):
            raise ValueError("track_count must be between 5 and 100")
        if "discovery_ratio" in f and not (0.0 <= float(f["discovery_ratio"]) <= 1.0):
            raise ValueError("discovery_ratio must be between 0.0 and 1.0")
        if "seed_window_days" in f and not (1 <= int(f["seed_window_days"]) <= 90):
            raise ValueError("seed_window_days must be between 1 and 90")
        if "max_weekly_acquisitions" in f and not (0 <= int(f["max_weekly_acquisitions"]) <= 100):
            raise ValueError("max_weekly_acquisitions must be between 0 and 100")

    def create_mix_config(
        self,
        user_id: str,
        mix_type: str,
        name: str,
        seed_artist: Optional[str] = None,
        track_count: int = 30,
        discovery_ratio: float = 0.7,
        seed_window_days: int = 14,
        excluded_genres: Optional[list[str]] = None,
        auto_acquire_missing: bool = False,
        max_weekly_acquisitions: int = 10,
        quality_profile_id: Optional[str] = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Create a tailored mix config; raises ValueError on out-of-range values or a missing artist_radio seed."""
        fields: dict[str, Any] = {
            "mix_type": mix_type,
            "name": name,
            "track_count": track_count,
            "discovery_ratio": discovery_ratio,
            "seed_window_days": seed_window_days,
            "max_weekly_acquisitions": max_weekly_acquisitions,
        }
        self._validate_mix_fields(fields)
        seed = (seed_artist or "").strip() or None
        if mix_type == "artist_radio" and not seed:
            raise ValueError("seed_artist is required for artist_radio")
        if mix_type != "artist_radio":
            seed = None
        mix_id = str(uuid.uuid4())
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO tailored_mix_configs
                    (id, user_id, mix_type, name, seed_artist, track_count, discovery_ratio, seed_window_days,
                     excluded_genres_json, auto_acquire_missing, max_weekly_acquisitions, quality_profile_id, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mix_id,
                    str(user_id),
                    mix_type,
                    name.strip(),
                    seed,
                    int(track_count),
                    float(discovery_ratio),
                    int(seed_window_days),
                    json.dumps(list(excluded_genres or [])),
                    1 if auto_acquire_missing else 0,
                    int(max_weekly_acquisitions),
                    quality_profile_id or None,
                    1 if enabled else 0,
                ),
            )
            self.conn.commit()
        created = self.get_mix_config(mix_id)
        if created is None:
            raise RuntimeError(f"Failed to create mix config {mix_id}")
        return created

    def get_mix_config(self, mix_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM tailored_mix_configs WHERE id = ?", (str(mix_id),)).fetchone()
            return self._mix_row(row) if row else None

    def list_mix_configs(self, user_id: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            if user_id is None:
                rows = self.conn.execute("SELECT * FROM tailored_mix_configs ORDER BY created_at ASC, id ASC").fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM tailored_mix_configs WHERE user_id = ? ORDER BY created_at ASC, id ASC",
                    (str(user_id),),
                ).fetchall()
            return [self._mix_row(r) for r in rows]

    def update_mix_config(self, mix_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        """Partial update; returns the updated config or None when missing. ValueError on invalid values."""
        unknown = set(fields) - set(self._MIX_UPDATABLE)
        if unknown:
            raise ValueError(f"Unknown mix config fields: {sorted(unknown)}")
        current = self.get_mix_config(mix_id)
        if current is None:
            return None
        self._validate_mix_fields(fields)
        merged_type = fields.get("mix_type", current["mix_type"])
        merged_seed = fields["seed_artist"] if "seed_artist" in fields else current["seed_artist"]
        merged_seed = (merged_seed or "").strip() or None
        if merged_type == "artist_radio" and not merged_seed:
            raise ValueError("seed_artist is required for artist_radio")
        updates: dict[str, Any] = {}
        for k, v in fields.items():
            if k == "excluded_genres":
                updates["excluded_genres_json"] = json.dumps(list(v or []))
            elif k in ("auto_acquire_missing", "enabled"):
                updates[k] = 1 if v else 0
            elif k == "seed_artist":
                updates[k] = merged_seed if merged_type == "artist_radio" else None
            elif k == "name":
                updates[k] = str(v).strip()
            else:
                updates[k] = v
        if "mix_type" in fields and merged_type != "artist_radio":
            updates["seed_artist"] = None
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            with self._lock:
                self.conn.execute(
                    f"UPDATE tailored_mix_configs SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [*updates.values(), str(mix_id)],
                )
                self.conn.commit()
        return self.get_mix_config(mix_id)

    def delete_mix_config(self, mix_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM tailored_mix_configs WHERE id = ?", (str(mix_id),))
            self.conn.commit()
            return cur.rowcount > 0

    def record_mix_result(self, mix_id: str, result_json: str) -> None:
        """Persist the serialized TailoredMixResult and stamp ``last_generated_at`` (ISO-8601 UTC)."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self.conn.execute(
                """
                UPDATE tailored_mix_configs
                SET last_result_json = ?, last_generated_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (result_json, now, str(mix_id)),
            )
            self.conn.commit()

    def count_mix_acquisitions_since(self, user_id: str, since_iso: str) -> int:
        """Acquisitions across all of the user's mixes since ``since_iso`` (per-user weekly quota)."""
        since = self._normalize_played_at(since_iso)
        with self._lock:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS n FROM mix_acquisitions a
                JOIN tailored_mix_configs c ON c.id = a.mix_id
                WHERE c.user_id = ? AND julianday(a.created_at) >= julianday(?)
                """,
                (str(user_id), since),
            ).fetchone()
            return int(row["n"]) if row else 0

    def add_mix_acquisition(self, mix_id: str, request_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO mix_acquisitions (mix_id, request_id) VALUES (?, ?)",
                (str(mix_id), str(request_id)),
            )
            self.conn.commit()

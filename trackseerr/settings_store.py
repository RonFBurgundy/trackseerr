"""Media management, general settings, deployment role, task run history, Lidarr settings, media server settings, and quality profiles.

Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Union

from trackseerr.import_quality_check import CHECK_MODES, normalize_check_mode
from trackseerr.library_monitoring import (
    DEFAULT_MONITOR_OPTION,
    validate_monitor_option,
)
from trackseerr.models import (
    QualityProfile,
)
from trackseerr.storage_common import (
    SEED_COMPLETE_ACTIONS,
    _LEGACY_LIDARR_OVERRIDE_COLUMNS,
)

logger = logging.getLogger(__name__)


class SettingsStoreMixin:
    # -------------------------------------------------------------------------
    # Media Management Settings CRUD
    # -------------------------------------------------------------------------

    def _ensure_naming_formats(self, cur: sqlite3.Cursor) -> None:
        """Lidarr-style naming (idempotent, runs on every start): adds multi_disc_track_format and folds album/disc folders into the track formats.

        standard_track_format / multi_disc_track_format become '/'-separated paths relative to the artist
        folder. Existing rows are converted so every install keeps its current output paths.
        """
        from trackseerr.naming import legacy_to_track_formats

        cur.execute("PRAGMA table_info(media_management_settings);")
        mm_cols = {row[1] for row in cur.fetchall()}
        if "multi_disc_track_format" in mm_cols:
            return
        cur.execute("ALTER TABLE media_management_settings ADD COLUMN multi_disc_track_format TEXT NOT NULL DEFAULT '';")
        cur.execute(
            "SELECT album_folder_format, standard_track_format, multi_disc_folder_format "
            "FROM media_management_settings WHERE id = 1"
        )
        row = cur.fetchone()
        if row:
            std, multi = legacy_to_track_formats(
                {
                    "album_folder_format": row[0],
                    "standard_track_format": row[1],
                    "multi_disc_folder_format": row[2],
                }
            )
            cur.execute(
                "UPDATE media_management_settings SET standard_track_format = ?, multi_disc_track_format = ? WHERE id = 1",
                (std, multi),
            )

    def get_media_management_settings(self) -> dict[str, Any]:
        """Retrieves media management settings (singleton row id=1)."""
        with self._lock:
            cur = self.conn.execute("SELECT * FROM media_management_settings WHERE id = 1")
            row = cur.fetchone()
            if not row:
                self.conn.execute(
                    "INSERT OR IGNORE INTO media_management_settings (id, add_monitor_option) VALUES (1, ?)",
                    (DEFAULT_MONITOR_OPTION,),
                )
                self.conn.commit()
                cur = self.conn.execute("SELECT * FROM media_management_settings WHERE id = 1")
                row = cur.fetchone()
            res = dict(row)
            res["clean_artist_names"] = bool(res.get("clean_artist_names", 1))
            res["write_audio_tags"] = bool(res.get("write_audio_tags", 1))
            res["embed_artwork"] = bool(res.get("embed_artwork", 1))
            res["save_cover_art_file"] = bool(res.get("save_cover_art_file", 1))
            # An empty value is a deliberate "no extra import folder": download folders come from the clients.
            res["staging_folder_path"] = str(res.get("staging_folder_path") or "")
            res["import_mode"] = str(res.get("import_mode") or "move")
            res["torrent_hardlink_tags"] = str(res.get("torrent_hardlink_tags") or "copy_and_tag")
            action = str(res.get("seed_complete_action") or "remove")
            res["seed_complete_action"] = action if action in SEED_COMPLETE_ACTIONS else "remove"
            res["delete_completed_transfers"] = res["seed_complete_action"] != "keep"  # legacy read-only mirror
            res["enable_quality_upgrades"] = bool(res.get("enable_quality_upgrades", 1))
            res["library_mode"] = str(res.get("library_mode") or "native")
            res["seed_ratio_limit"] = (
                float(res["seed_ratio_limit"]) if res.get("seed_ratio_limit") is not None else None
            )
            res["seed_time_limit_minutes"] = (
                int(res["seed_time_limit_minutes"]) if res.get("seed_time_limit_minutes") is not None else None
            )
            res["enrich_mbids"] = bool(res.get("enrich_mbids", 1))
            res["acoustid_api_key"] = (
                str(res["acoustid_api_key"]) if res.get("acoustid_api_key") is not None else None
            )
            res["fingerprint_on_weak_match"] = bool(res.get("fingerprint_on_weak_match", 0))
            res["mb_mirror_url"] = str(res.get("mb_mirror_url") or "https://api.brainzmash.cc")
            res["prefer_local_artwork"] = bool(res.get("prefer_local_artwork", 1))
            res["scan_monitor_option"] = str(res.get("scan_monitor_option") or "existing")
            res["add_monitor_option"] = str(res.get("add_monitor_option") or DEFAULT_MONITOR_OPTION)
            res["import_bitrate_check"] = normalize_check_mode(res.get("import_bitrate_check"))
            res["recycle_bin_path"] = str(res.get("recycle_bin_path") or "")
            res["recycle_bin_cleanup_days"] = (
                int(res["recycle_bin_cleanup_days"]) if res.get("recycle_bin_cleanup_days") is not None else 30
            )
            res["recycle_bin_permanent_delete"] = bool(res.get("recycle_bin_permanent_delete", 0))
            res["quarantine_folder_path"] = str(res.get("quarantine_folder_path") or "")
            res["add_metadata_profile_id"] = (
                int(res["add_metadata_profile_id"]) if res.get("add_metadata_profile_id") is not None else None
            )
            return res

    def update_media_management_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Updates media management settings (singleton row id=1)."""
        allowed_keys = {
            "artist_folder_format",
            "album_folder_format",
            "standard_track_format",
            "compilation_track_format",
            "multi_disc_folder_format",
            "multi_disc_track_format",
            "root_folder_path",
            "colon_replacement_format",
            "clean_artist_names",
            "staging_folder_path",
            "import_mode",
            "torrent_hardlink_tags",
            "write_audio_tags",
            "embed_artwork",
            "save_cover_art_file",
            "delete_completed_transfers",
            "seed_complete_action",
            "enable_quality_upgrades",
            "library_mode",
            "seed_ratio_limit",
            "seed_time_limit_minutes",
            "enrich_mbids",
            "acoustid_api_key",
            "fingerprint_on_weak_match",
            "mb_mirror_url",
            "prefer_local_artwork",
            "scan_monitor_option",
            "add_monitor_option",
            "add_metadata_profile_id",
            "import_bitrate_check",
            "recycle_bin_path",
            "recycle_bin_cleanup_days",
            "recycle_bin_permanent_delete",
            "quarantine_folder_path",
        }
        if settings.get("recycle_bin_cleanup_days") is not None and int(settings["recycle_bin_cleanup_days"]) < 0:
            raise ValueError("recycle_bin_cleanup_days must be 0 or greater")
        if settings.get("import_bitrate_check") is not None and str(settings["import_bitrate_check"]).strip().lower() not in CHECK_MODES:
            raise ValueError("import_bitrate_check must be one of: off, warn, reject")
        if settings.get("torrent_hardlink_tags") is not None and settings["torrent_hardlink_tags"] not in (
            "copy_and_tag",
            "keep_hardlink",
        ):
            raise ValueError("torrent_hardlink_tags must be one of: copy_and_tag, keep_hardlink")
        for opt_key in ("scan_monitor_option", "add_monitor_option"):
            if settings.get(opt_key) is not None:
                validate_monitor_option(settings[opt_key])
        if settings.get("seed_complete_action") is not None and settings["seed_complete_action"] not in SEED_COMPLETE_ACTIONS:
            raise ValueError("seed_complete_action must be one of: " + ", ".join(SEED_COMPLETE_ACTIONS))
        settings = dict(settings)
        legacy_delete = settings.pop("delete_completed_transfers", None)
        if legacy_delete is not None and settings.get("seed_complete_action") is None:
            # Deprecated boolean from older clients: true -> remove, false -> keep.
            settings["seed_complete_action"] = "remove" if legacy_delete else "keep"
        updates: dict[str, Any] = {}
        for k, v in settings.items():
            if k in allowed_keys:
                if k in (
                    "clean_artist_names",
                    "write_audio_tags",
                    "embed_artwork",
                    "save_cover_art_file",
                    "enable_quality_upgrades",
                    "enrich_mbids",
                    "fingerprint_on_weak_match",
                    "prefer_local_artwork",
                    "recycle_bin_permanent_delete",
                ):
                    if v is not None:
                        updates[k] = 1 if bool(v) else 0
                elif k == "recycle_bin_cleanup_days":
                    if v is not None:
                        updates[k] = int(v)
                elif k in ("recycle_bin_path", "quarantine_folder_path"):
                    if v is not None:
                        updates[k] = str(v).strip()
                elif k == "add_metadata_profile_id":
                    if v is not None and self.get_metadata_profile(int(v)) is None:
                        raise ValueError(f"Metadata profile {v} does not exist")
                    updates[k] = int(v) if v is not None else None
                elif k == "seed_ratio_limit":
                    updates[k] = float(v) if v is not None else None
                elif k == "seed_time_limit_minutes":
                    updates[k] = int(v) if v is not None else None
                elif k == "acoustid_api_key":
                    updates[k] = str(v) if v is not None else None
                elif k == "import_bitrate_check":
                    if v is not None:
                        updates[k] = normalize_check_mode(v)
                elif v is not None:
                    updates[k] = str(v)

        # Legacy clients send a file-name-only standard_track_format plus separate album/disc folder
        # formats and no multi_disc_track_format; fold those into the Lidarr-style full-path formats.
        legacy_std = settings.get("standard_track_format")
        if (
            isinstance(legacy_std, str)
            and "/" not in legacy_std
            and "\\" not in legacy_std
            and settings.get("multi_disc_track_format") is None
        ):
            from trackseerr.naming import legacy_to_track_formats

            merged = {**self.get_media_management_settings(), **{k: v for k, v in settings.items() if v is not None}}
            updates["standard_track_format"], updates["multi_disc_track_format"] = legacy_to_track_formats(merged)

        if updates:
            set_clauses = [f"{k} = ?" for k in updates.keys()]
            set_clauses.append("updated_at = CURRENT_TIMESTAMP")
            values = list(updates.values())
            query = f"UPDATE media_management_settings SET {', '.join(set_clauses)} WHERE id = 1"
            with self._lock:
                self.conn.execute(query, values)
                self.conn.commit()

        return self.get_media_management_settings()

    # -------------------------------------------------------------------------
    # General Settings CRUD
    # -------------------------------------------------------------------------

    def get_general_settings(self) -> dict[str, Any]:
        """Retrieves general system settings (singleton row id=1), falling back to env."""
        with self._lock:
            cur = self.conn.execute("SELECT * FROM general_settings WHERE id = 1")
            row = cur.fetchone()
            if not row:
                self.conn.execute("INSERT OR IGNORE INTO general_settings (id, application_url) VALUES (1, '')")
                self.conn.commit()
                cur = self.conn.execute("SELECT * FROM general_settings WHERE id = 1")
                row = cur.fetchone()
            res = dict(row) if row else {"id": 1, "application_url": "", "updated_at": None}
            res["application_url"] = str(res.get("application_url") or "").strip().rstrip("/")
            if not res["application_url"]:
                env_url = (os.getenv("APPLICATION_URL") or os.getenv("APP_URL") or "").strip().rstrip("/")
                res["application_url"] = env_url
            return res

    def update_general_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Updates general system settings (singleton row id=1)."""
        allowed_keys = {"application_url"}
        updates: dict[str, Any] = {}
        for k, v in settings.items():
            if k in allowed_keys and v is not None:
                updates[k] = str(v).strip().rstrip("/")

        if updates:
            set_clauses = [f"{k} = ?" for k in updates.keys()]
            set_clauses.append("updated_at = CURRENT_TIMESTAMP")
            values = list(updates.values())
            query = f"UPDATE general_settings SET {', '.join(set_clauses)} WHERE id = 1"
            with self._lock:
                self.conn.execute(query, values)
                self.conn.commit()

        return self.get_general_settings()

    def get_update_check_state(self) -> dict[str, Any]:
        """Returns the current update check configuration and cached result."""
        with self._lock:
            self._ensure_general_row()
            cur = self.conn.execute(
                "SELECT update_check_enabled, update_latest_version, update_release_url, "
                "update_published_at, update_checked_at, update_error "
                "FROM general_settings WHERE id = 1"
            )
            row = cur.fetchone()
            if not row:
                return {
                    "enabled": True,
                    "latest_version": None,
                    "release_url": None,
                    "published_at": None,
                    "checked_at": None,
                    "error": None,
                }
            enabled_val = row["update_check_enabled"]
            return {
                "enabled": bool(enabled_val if enabled_val is not None else 1),
                "latest_version": row["update_latest_version"],
                "release_url": row["update_release_url"],
                "published_at": row["update_published_at"],
                "checked_at": row["update_checked_at"],
                "error": row["update_error"],
            }

    def set_update_check_enabled(self, enabled: bool) -> None:
        """Updates the update_check_enabled flag in general_settings."""
        val = 1 if enabled else 0
        with self._lock:
            self._ensure_general_row()
            self.conn.execute(
                "UPDATE general_settings SET update_check_enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                (val,),
            )
            self.conn.commit()

    def save_update_check_result(
        self,
        *,
        latest_version: Optional[str],
        release_url: Optional[str],
        published_at: Optional[str],
        checked_at: Optional[str],
        error: Optional[str] = None,
        clear_release: bool = False,
    ) -> None:
        """Persists the outcome of an update check."""
        with self._lock:
            self._ensure_general_row()
            if clear_release:
                self.conn.execute(
                    "UPDATE general_settings SET "
                    "update_latest_version = NULL, "
                    "update_release_url = NULL, "
                    "update_published_at = NULL, "
                    "update_checked_at = ?, "
                    "update_error = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                    (checked_at, error),
                )
            elif latest_version is not None:
                self.conn.execute(
                    "UPDATE general_settings SET "
                    "update_latest_version = ?, "
                    "update_release_url = ?, "
                    "update_published_at = ?, "
                    "update_checked_at = ?, "
                    "update_error = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                    (latest_version, release_url, published_at, checked_at, error),
                )
            else:
                self.conn.execute(
                    "UPDATE general_settings SET "
                    "update_checked_at = ?, "
                    "update_error = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                    (checked_at, error),
                )
            self.conn.commit()

    # -------------------------------------------------------------------------
    # Deployment role / instance identity / key-value (migration v30)
    # -------------------------------------------------------------------------

    def get_instance_id(self) -> str:
        """Random id for this database, created on first use and then stable."""
        with self._lock:
            self._ensure_general_row()
            row = self.conn.execute("SELECT instance_id FROM general_settings WHERE id = 1").fetchone()
            current = str(row["instance_id"] or "") if row else ""
            if not current:
                current = uuid.uuid4().hex
                self.conn.execute(
                    "UPDATE general_settings SET instance_id = ? WHERE id = 1 AND instance_id = ''", (current,)
                )
                self.conn.commit()
                row = self.conn.execute("SELECT instance_id FROM general_settings WHERE id = 1").fetchone()
                current = str(row["instance_id"])
            return current

    def get_last_role(self) -> str:
        with self._lock:
            row = self.conn.execute("SELECT last_role FROM general_settings WHERE id = 1").fetchone()
            return str(row["last_role"] or "") if row else ""

    def set_last_role(self, role: str) -> None:
        with self._lock:
            self.conn.execute("INSERT OR IGNORE INTO general_settings (id, application_url) VALUES (1, '')")
            self.conn.execute("UPDATE general_settings SET last_role = ? WHERE id = 1", (str(role),))
            self.conn.commit()

    def get_role_change_notice(self) -> Optional[dict[str, Any]]:
        """The pending role-change record ({from_role, to_role, changed_at, dismissed}) or None."""
        with self._lock:
            row = self.conn.execute("SELECT role_change_notice FROM general_settings WHERE id = 1").fetchone()
            raw = str(row["role_change_notice"] or "") if row else ""
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def set_role_change_notice(self, notice: Optional[dict[str, Any]]) -> None:
        with self._lock:
            self.conn.execute("INSERT OR IGNORE INTO general_settings (id, application_url) VALUES (1, '')")
            self.conn.execute(
                "UPDATE general_settings SET role_change_notice = ? WHERE id = 1",
                (json.dumps(notice) if notice else "",),
            )
            self.conn.commit()

    def has_any_users(self) -> bool:
        with self._lock:
            return self.conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    # -------------------------------------------------------------------------
    # Task run history (task manager)
    # -------------------------------------------------------------------------

    def start_task_run(self, task_id: str, trigger: str, started_at: str) -> int:
        """Inserts a ``running`` row and returns its id."""
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO task_runs (task_id, trigger, started_at, status) VALUES (?, ?, ?, 'running')",
                (str(task_id), str(trigger), str(started_at)),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def finish_task_run(
        self, run_id: int, status: str, finished_at: str, message: Optional[str] = None, duration_ms: Optional[int] = None
    ) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE task_runs SET status = ?, finished_at = ?, message = ?, duration_ms = ? WHERE id = ?",
                (str(status), str(finished_at), message, duration_ms, int(run_id)),
            )
            self.conn.commit()

    def delete_task_run(self, run_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM task_runs WHERE id = ?", (int(run_id),))
            self.conn.commit()

    def list_task_runs(self, task_id: str, since: str, limit: int = 200) -> list[dict[str, Any]]:
        """Runs of one task started at or after ``since`` (ISO), newest first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? AND started_at >= ? ORDER BY started_at DESC, id DESC LIMIT ?",
                (str(task_id), str(since), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_task_runs(self) -> dict[str, dict[str, Any]]:
        """The newest run row of every task that has one, keyed by task id (one query)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM task_runs WHERE id IN (SELECT MAX(id) FROM task_runs GROUP BY task_id)"
            ).fetchall()
        return {str(r["task_id"]): dict(r) for r in rows}

    def latest_finished_task_runs(self) -> dict[str, dict[str, Any]]:
        """The newest finished (non-running) run row of every task, keyed by task id (one query)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM task_runs WHERE id IN "
                "(SELECT MAX(id) FROM task_runs WHERE status != 'running' GROUP BY task_id)"
            ).fetchall()
        return {str(r["task_id"]): dict(r) for r in rows}

    def list_running_task_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM task_runs WHERE status = 'running' ORDER BY started_at ASC, id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_recent_finished_task_runs(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM task_runs WHERE status != 'running' ORDER BY finished_at DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def fail_interrupted_task_runs(self, finished_at: str, message: str = "interrupted by restart") -> int:
        """Startup cleanup: any run still ``running`` belongs to a dead process. Returns how many were closed."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE task_runs SET status = 'failed', finished_at = ?, message = ? WHERE status = 'running'",
                (str(finished_at), str(message)),
            )
            self.conn.commit()
            return int(cur.rowcount or 0)

    def prune_task_runs(self, older_than: str) -> int:
        """Deletes finished runs that started before ``older_than`` (ISO). Returns the number removed."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM task_runs WHERE started_at < ? AND status != 'running'", (str(older_than),)
            )
            self.conn.commit()
            return int(cur.rowcount or 0)

    def get_kv(self, key: str) -> Optional[str]:
        with self._lock:
            row = self.conn.execute("SELECT value FROM kv_store WHERE key = ?", (str(key),)).fetchone()
            return str(row["value"]) if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO kv_store (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                (str(key), str(value)),
            )
            self.conn.commit()

    def delete_kv(self, key: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM kv_store WHERE key = ?", (str(key),))
            self.conn.commit()

    def get_last_finished_task_run(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? AND status != 'running' ORDER BY id DESC LIMIT 1",
                (str(task_id),),
            ).fetchone()
        return dict(row) if row else None

    def list_kv_prefix(self, prefix: str) -> dict[str, str]:
        """All kv_store entries whose key starts with ``prefix`` (literal match, no LIKE wildcards)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT key, value FROM kv_store WHERE substr(key, 1, ?) = ?", (len(prefix), str(prefix))
            ).fetchall()
            return {str(r["key"]): str(r["value"]) for r in rows}

    def count_active_sessions(self) -> int:
        """Number of unexpired sessions (a count only; nothing identifying)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE expires_at IS NULL OR expires_at > ?", (now_iso,)
            ).fetchone()
            return int(row["n"]) if row else 0

    def get_api_key(self) -> str:
        """Fetches api_key from general_settings. If empty, generates secrets.token_hex(16), saves it, and returns it."""
        with self._lock:
            cur = self.conn.execute("SELECT api_key FROM general_settings WHERE id = 1")
            row = cur.fetchone()
            key = str(row["api_key"] or "").strip() if row and "api_key" in row.keys() else ""
            if not key:
                key = secrets.token_hex(16)
                check = self.conn.execute("SELECT 1 FROM general_settings WHERE id = 1").fetchone()
                if check:
                    self.conn.execute(
                        "UPDATE general_settings SET api_key = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                        (key,),
                    )
                else:
                    self.conn.execute(
                        "INSERT INTO general_settings (id, application_url, api_key) VALUES (1, '', ?)",
                        (key,),
                    )
                self.conn.commit()
            return key

    def regenerate_api_key(self) -> str:
        """Generates new 32-character hexadecimal API key, saves it to general_settings, and returns it."""
        new_key = secrets.token_hex(16)
        with self._lock:
            check = self.conn.execute("SELECT 1 FROM general_settings WHERE id = 1").fetchone()
            if check:
                self.conn.execute(
                    "UPDATE general_settings SET api_key = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                    (new_key,),
                )
            else:
                self.conn.execute(
                    "INSERT INTO general_settings (id, application_url, api_key) VALUES (1, '', ?)",
                    (new_key,),
                )
            self.conn.commit()
        return new_key

    def validate_api_key(self, candidate_key: Optional[str]) -> bool:
        """Returns False if candidate_key is empty/None; compares candidate_key against stored api_key with secrets.compare_digest."""
        if not candidate_key or not isinstance(candidate_key, str) or not candidate_key.strip():
            return False
        stored_key = self.get_api_key()
        if not stored_key:
            return False
        return hmac.compare_digest(candidate_key.strip().encode("utf-8"), stored_key.encode("utf-8"))

    # -------------------------------------------------------------------------
    # Lidarr Settings CRUD
    # -------------------------------------------------------------------------

    def get_lidarr_settings(self) -> dict[str, Any]:
        """Retrieves Lidarr automation settings (singleton row id=1)."""
        with self._lock:
            cur = self.conn.execute("SELECT * FROM lidarr_settings WHERE id = 1")
            row = cur.fetchone()
            if not row:
                self.conn.execute("INSERT OR IGNORE INTO lidarr_settings (id) VALUES (1)")
                self.conn.commit()
                cur = self.conn.execute("SELECT * FROM lidarr_settings WHERE id = 1")
                row = cur.fetchone()
            res = dict(row)
            res["auto_search"] = bool(res.get("auto_search", 1))
            res["auto_trickle"] = bool(res.get("auto_trickle", 0))
            res["prefer_singles"] = bool(res.get("prefer_singles", 1))
            res["trickle_rate_seconds"] = float(res.get("trickle_rate_seconds") or 3.0)
            res["trickle_batch_size"] = int(res.get("trickle_batch_size") or 25)
            res["auto_trickle_interval_minutes"] = int(
                res.get("auto_trickle_interval_minutes") or 30
            )
            # Lidarr's own root-folder defaults decide profiles, monitoring and tags; the legacy override columns stay
            # in the table (no migration) but are never surfaced or read.
            for legacy in _LEGACY_LIDARR_OVERRIDE_COLUMNS:
                res.pop(legacy, None)
            res["search_on_add"] = res["auto_search"]
            return res

    def update_lidarr_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Updates Lidarr automation settings (singleton row id=1)."""
        allowed_keys = {
            "url",
            "api_key",
            "auto_search",
            "root_folder",
            "trickle_rate_seconds",
            "trickle_batch_size",
            "auto_trickle",
            "auto_trickle_interval_minutes",
            "search_on_add",
            "prefer_singles",
        }
        updates: dict[str, Any] = {}
        for k, v in settings.items():
            if k in allowed_keys:
                if k == "search_on_add":
                    if v is not None:  # wins over a stale auto_search echoed back by a client
                        updates["auto_search"] = 1 if v else 0
                elif k in ("auto_search", "auto_trickle", "prefer_singles"):
                    if k == "auto_search" and settings.get("search_on_add") is not None:
                        continue
                    if v is not None:
                        updates[k] = 1 if v else 0
                elif k in ("trickle_batch_size", "auto_trickle_interval_minutes"):
                    updates[k] = int(v) if v is not None else None
                elif k == "trickle_rate_seconds":
                    updates[k] = float(v) if v is not None else 3.0
                else:
                    updates[k] = str(v) if v is not None else None

        if updates:
            set_clauses = [f"{k} = ?" for k in updates.keys()]
            set_clauses.append("updated_at = CURRENT_TIMESTAMP")
            values = list(updates.values())
            query = f"UPDATE lidarr_settings SET {', '.join(set_clauses)} WHERE id = 1"
            with self._lock:
                self.conn.execute(query, values)
                self.conn.commit()

        return self.get_lidarr_settings()

    # -------------------------------------------------------------------------
    # Media server settings (Settings page)
    # -------------------------------------------------------------------------

    _MEDIA_SERVER_SETTING_KEYS = ("type", "url", "username", "password", "api_key", "credentials_type")

    def get_media_server_settings(self) -> dict[str, str]:
        """The saved media-server choice; every key is present, all empty when nothing was ever saved."""
        with self._lock:
            row = self.conn.execute("SELECT * FROM media_server_settings WHERE id = 1").fetchone()
        stored = dict(row) if row else {}
        return {k: str(stored.get(k) or "") for k in self._MEDIA_SERVER_SETTING_KEYS}

    def save_media_server_settings(self, settings: dict[str, str]) -> dict[str, str]:
        """Replace the saved media-server choice (all keys; missing ones are stored empty)."""
        values = [str(settings.get(k) or "") for k in self._MEDIA_SERVER_SETTING_KEYS]
        with self._lock:
            self.conn.execute(
                "INSERT INTO media_server_settings (id, type, url, username, password, api_key, credentials_type) "
                "VALUES (1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET type = excluded.type, url = excluded.url, username = excluded.username, "
                "password = excluded.password, api_key = excluded.api_key, credentials_type = excluded.credentials_type, "
                "updated_at = CURRENT_TIMESTAMP",
                values,
            )
            self.conn.commit()
        return self.get_media_server_settings()

    # -------------------------------------------------------------------------
    # Quality Profiles CRUD
    # -------------------------------------------------------------------------

    def list_quality_profiles(self) -> list[dict[str, Any]]:
        """Lists all quality profiles ordered by default first, then name."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM quality_profiles ORDER BY is_default DESC, name ASC"
            )
            rows = cur.fetchall()
            return [self._format_quality_profile_row(r) for r in rows]

    def create_quality_profile(
        self, profile: Union[QualityProfile, dict[str, Any]]
    ) -> dict[str, Any]:
        """Creates a quality profile (alias to upsert_quality_profile)."""
        return self.upsert_quality_profile(profile)

    def update_quality_profile(
        self, profile_id: str, updates: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Updates an existing quality profile by ID."""
        existing = self.get_quality_profile(profile_id)
        if not existing:
            return None
        existing.update(updates)
        return self.upsert_quality_profile(existing)

    def delete_quality_profile(self, profile_id: str) -> bool:
        """Deletes a quality profile. Raises ValueError if the profile is default."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT is_default FROM quality_profiles WHERE id = ?",
                (str(profile_id),),
            )
            row = cur.fetchone()
            if not row:
                return False
            if bool(row[0]):
                raise ValueError("Cannot delete the default quality profile")

            cur = self.conn.execute(
                "DELETE FROM quality_profiles WHERE id = ?", (str(profile_id),)
            )
            self.conn.commit()
            return cur.rowcount > 0


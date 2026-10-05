"""Persistence for the Arr-style quality system: migration v49, quality definitions, custom formats, release
profiles and the v2 quality-profile row shape. Mixed into ``storage.Database`` (uses ``self._lock`` / ``self.conn``)."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Optional, Union

from plex_playlist_sync.models import QualityProfile
from plex_playlist_sync.quality_defaults import (
    DEFAULT_CUSTOM_FORMATS,
    DEFAULT_QUALITY_DEFINITIONS,
    DEFAULT_RELEASE_PROFILE,
    QUALITY_ORDER,
    legacy_format_name,
    legacy_tag_format_spec,
    legacy_items_to_entries_with_weight,
    legacy_tag_term,
    is_v2_items,
    normalize_entries,
)

logger = logging.getLogger(__name__)

LEGACY_PREFERRED_SCORE = 50
# Migrated profiles keep accepting everything the old engine accepted: the default formats carry soft penalties
# (Vinyl -50, Mono -10, Censored/Clean -15) that a floor of 0 would turn into hard rejections.
MIGRATED_MIN_FORMAT_SCORE = -100


def _loads(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (json.JSONDecodeError, TypeError):
        return default
    return value if value is not None else default


class QualityCatalogMixin:
    # ------------------------------------------------------------------------------------------------------------
    # Migration v49
    # ------------------------------------------------------------------------------------------------------------

    def _migration_v49(self, cur: sqlite3.Cursor) -> None:
        """Arr-style profiles: quality definitions, custom formats, release profiles, v2 quality-profile items.

        Existing weight-based profiles are re-expressed as ordered items (weight desc), keeping allowed qualities and
        the cutoff. Legacy ``preferred_tags`` become per-tag custom formats (+50, same matching as before) and
        ``ignored_tags`` become release profiles scoped to the owning quality profile, so behaviour is unchanged.
        """
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS quality_definitions (
                quality TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                min_kbps REAL,
                preferred_kbps REAL,
                max_kbps REAL,
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_formats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                include_in_rename INTEGER NOT NULL DEFAULT 0,
                specifications_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS release_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                required_json TEXT NOT NULL DEFAULT '[]',
                ignored_json TEXT NOT NULL DEFAULT '[]',
                indexer_ids_json TEXT NOT NULL DEFAULT '[]',
                tags_json TEXT NOT NULL DEFAULT '[]',
                quality_profile_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute("PRAGMA table_info(quality_profiles);")
        cols = {row[1] for row in cur.fetchall()}
        for col, ddl in (
            ("format_items_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("min_format_score", "INTEGER NOT NULL DEFAULT 0"),
            ("cutoff_format_score", "INTEGER NOT NULL DEFAULT 0"),
            ("min_upgrade_format_score", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if col not in cols:
                cur.execute(f"ALTER TABLE quality_profiles ADD COLUMN {col} {ddl};")

        for quality, title, lo, pref, hi in DEFAULT_QUALITY_DEFINITIONS:
            cur.execute(
                "INSERT OR IGNORE INTO quality_definitions (quality, title, min_kbps, preferred_kbps, max_kbps) "
                "VALUES (?, ?, ?, ?, ?)",
                (quality, title, lo, pref, hi),
            )
        default_scores: list[dict[str, Any]] = []
        for name, specs, score in DEFAULT_CUSTOM_FORMATS:
            cur.execute(
                "INSERT OR IGNORE INTO custom_formats (name, specifications_json) VALUES (?, ?)",
                (name, json.dumps(specs)),
            )
            cur.execute("SELECT id FROM custom_formats WHERE name = ?", (name,))
            default_scores.append({"format_id": cur.fetchone()[0], "score": score})
        rp = DEFAULT_RELEASE_PROFILE
        cur.execute(
            "INSERT OR IGNORE INTO release_profiles (name, enabled, required_json, ignored_json) VALUES (?, ?, ?, ?)",
            (rp["name"], 1 if rp["enabled"] else 0, json.dumps(rp["required"]), json.dumps(rp["ignored"])),
        )

        cur.execute(
            "SELECT id, name, items_json, preferred_tags_json, ignored_tags_json, min_score FROM quality_profiles"
        )
        for pid, pname, items_json, pref_json, ign_json, min_score in cur.fetchall():
            raw_items = _loads(items_json, [])
            if min_score is not None and not is_v2_items(raw_items):
                # The legacy min_score is compared against weight + score, so keep the weights for that check.
                entries = legacy_items_to_entries_with_weight(raw_items)
            else:
                entries = normalize_entries(raw_items)
            fmt_items = [dict(d) for d in default_scores]
            fmt_items = self._apply_legacy_tags(
                cur, str(pid), str(pname), _loads(pref_json, []), _loads(ign_json, []), fmt_items
            )
            cur.execute(
                "UPDATE quality_profiles SET items_json = ?, format_items_json = ?, min_format_score = ? WHERE id = ?",
                (json.dumps(entries), json.dumps(fmt_items), MIGRATED_MIN_FORMAT_SCORE, pid),
            )

    def _apply_legacy_tags(
        self,
        cur: sqlite3.Cursor,
        profile_id: str,
        profile_name: str,
        preferred: list[Any],
        ignored: list[Any],
        format_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Folds legacy tag lists into custom formats / a scoped release profile. Idempotent. Returns format_items."""
        scores = {int(fi["format_id"]): int(fi["score"]) for fi in format_items}
        for tag in preferred or []:
            tag = str(tag).strip()
            if not tag:
                continue
            name = legacy_format_name(tag)
            cur.execute(
                "INSERT OR IGNORE INTO custom_formats (name, specifications_json) VALUES (?, ?)",
                (name, json.dumps(legacy_tag_format_spec(tag))),
            )
            cur.execute("SELECT id FROM custom_formats WHERE name = ?", (name,))
            scores[int(cur.fetchone()[0])] = LEGACY_PREFERRED_SCORE
        terms = [legacy_tag_term(str(t)) for t in ignored or [] if str(t).strip()]
        if terms:
            rp_name = f"Ignored tags: {profile_name}"
            cur.execute(
                "INSERT INTO release_profiles (name, enabled, ignored_json, quality_profile_ids_json) "
                "VALUES (?, 1, ?, ?) ON CONFLICT(name) DO UPDATE SET ignored_json = excluded.ignored_json, "
                "quality_profile_ids_json = excluded.quality_profile_ids_json, updated_at = CURRENT_TIMESTAMP",
                (rp_name, json.dumps(terms), json.dumps([profile_id])),
            )
        return [{"format_id": k, "score": v} for k, v in scores.items()]

    def apply_legacy_tags(
        self, profile_id: str, preferred: list[str], ignored: list[str]
    ) -> Optional[dict[str, Any]]:
        """Compatibility for old API clients that still post ``preferred_tags`` / ``ignored_tags``."""
        with self._lock:
            row = self.conn.execute(
                "SELECT name, format_items_json FROM quality_profiles WHERE id = ?", (str(profile_id),)
            ).fetchone()
            if not row:
                return None
            cur = self.conn.cursor()
            items = self._apply_legacy_tags(
                cur, str(profile_id), row[0], preferred, ignored, _loads(row[1], [])
            )
            cur.execute(
                "UPDATE quality_profiles SET format_items_json = ? WHERE id = ?", (json.dumps(items), str(profile_id))
            )
            self.conn.commit()
        return self.get_quality_profile(profile_id)

    # ------------------------------------------------------------------------------------------------------------
    # Quality definitions
    # ------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _definition_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        default = next((x for x in DEFAULT_QUALITY_DEFINITIONS if x[0] == d["quality"]), None)
        for key in ("min_kbps", "preferred_kbps", "max_kbps"):
            d[key] = float(d[key]) if d.get(key) is not None else None
        d["default_min_kbps"], d["default_preferred_kbps"], d["default_max_kbps"] = (
            (default[2], default[3], default[4]) if default else (None, None, None)
        )
        d["is_default"] = bool(
            default and (d["min_kbps"], d["preferred_kbps"], d["max_kbps"]) == (default[2], default[3], default[4])
        )
        return d

    def list_quality_definitions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM quality_definitions").fetchall()
        out = [self._definition_row(r) for r in rows]
        out.sort(key=lambda d: QUALITY_ORDER.index(d["quality"]) if d["quality"] in QUALITY_ORDER else 99)
        return out

    def get_quality_definition(self, quality: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM quality_definitions WHERE quality = ?", (quality,)).fetchone()
        return self._definition_row(row) if row else None

    def update_quality_definition(
        self,
        quality: str,
        min_kbps: Optional[float],
        preferred_kbps: Optional[float],
        max_kbps: Optional[float],
        title: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE quality_definitions SET min_kbps = ?, preferred_kbps = ?, max_kbps = ?, "
                "title = COALESCE(?, title), updated_at = CURRENT_TIMESTAMP WHERE quality = ?",
                (min_kbps, preferred_kbps, max_kbps, title, quality),
            )
            self.conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_quality_definition(quality)

    def reset_quality_definitions(self, quality: Optional[str] = None) -> list[dict[str, Any]]:
        """Resets one (or all) definitions to the shipped defaults."""
        with self._lock:
            for q, title, lo, pref, hi in DEFAULT_QUALITY_DEFINITIONS:
                if quality is not None and q != quality:
                    continue
                self.conn.execute(
                    "INSERT INTO quality_definitions (quality, title, min_kbps, preferred_kbps, max_kbps) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(quality) DO UPDATE SET title = excluded.title, "
                    "min_kbps = excluded.min_kbps, preferred_kbps = excluded.preferred_kbps, "
                    "max_kbps = excluded.max_kbps, updated_at = CURRENT_TIMESTAMP",
                    (q, title, lo, pref, hi),
                )
            self.conn.commit()
        return self.list_quality_definitions()

    # ------------------------------------------------------------------------------------------------------------
    # Custom formats
    # ------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _format_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        specs = _loads(d.pop("specifications_json", "[]"), [])
        d["include_in_rename"] = bool(d.get("include_in_rename"))
        d["specifications"] = specs if isinstance(specs, list) else []
        d["unsupported"] = any(bool(s.get("unsupported")) for s in d["specifications"] if isinstance(s, dict))
        return d

    def list_custom_formats(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM custom_formats ORDER BY name COLLATE NOCASE").fetchall()
        return [self._format_row(r) for r in rows]

    def get_custom_format(self, format_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM custom_formats WHERE id = ?", (int(format_id),)).fetchone()
        return self._format_row(row) if row else None

    def get_custom_format_by_name(self, name: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM custom_formats WHERE name = ?", (name,)).fetchone()
        return self._format_row(row) if row else None

    def create_custom_format(self, fmt: dict[str, Any]) -> dict[str, Any]:
        """Inserts a normalized format; raises ``sqlite3.IntegrityError`` on a duplicate name."""
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO custom_formats (name, include_in_rename, specifications_json) VALUES (?, ?, ?)",
                (fmt["name"], 1 if fmt.get("include_in_rename") else 0, json.dumps(fmt["specifications"])),
            )
            self.conn.commit()
            new_id = int(cur.lastrowid)
        result = self.get_custom_format(new_id)
        assert result is not None
        return result

    def update_custom_format(self, format_id: int, fmt: dict[str, Any]) -> Optional[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE custom_formats SET name = ?, include_in_rename = ?, specifications_json = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (fmt["name"], 1 if fmt.get("include_in_rename") else 0, json.dumps(fmt["specifications"]), int(format_id)),
            )
            self.conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_custom_format(format_id)

    def delete_custom_format(self, format_id: int) -> bool:
        """Deletes a format and prunes its score from every quality profile."""
        with self._lock:
            cur = self.conn.execute("DELETE FROM custom_formats WHERE id = ?", (int(format_id),))
            if cur.rowcount == 0:
                self.conn.commit()
                return False
            for pid, raw in self.conn.execute("SELECT id, format_items_json FROM quality_profiles").fetchall():
                items = _loads(raw, [])
                kept = [i for i in items if int(i.get("format_id", -1)) != int(format_id)]
                if len(kept) != len(items):
                    self.conn.execute(
                        "UPDATE quality_profiles SET format_items_json = ? WHERE id = ?", (json.dumps(kept), pid)
                    )
            self.conn.commit()
            return True

    # ------------------------------------------------------------------------------------------------------------
    # Release profiles
    # ------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _release_profile_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        return {
            "id": d["id"],
            "name": d["name"],
            "enabled": bool(d["enabled"]),
            "required": _loads(d["required_json"], []),
            "ignored": _loads(d["ignored_json"], []),
            "indexer_ids": _loads(d["indexer_ids_json"], []),
            "tags": _loads(d["tags_json"], []),
            "quality_profile_ids": _loads(d["quality_profile_ids_json"], []),
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
        }

    def list_release_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM release_profiles ORDER BY id").fetchall()
        return [self._release_profile_row(r) for r in rows]

    def get_release_profile(self, profile_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM release_profiles WHERE id = ?", (int(profile_id),)).fetchone()
        return self._release_profile_row(row) if row else None

    def create_release_profile(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO release_profiles (name, enabled, required_json, ignored_json, indexer_ids_json, "
                "tags_json, quality_profile_ids_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    data["name"],
                    1 if data.get("enabled", True) else 0,
                    json.dumps(data.get("required", [])),
                    json.dumps(data.get("ignored", [])),
                    json.dumps(data.get("indexer_ids", [])),
                    json.dumps(data.get("tags", [])),
                    json.dumps(data.get("quality_profile_ids", [])),
                ),
            )
            self.conn.commit()
            new_id = int(cur.lastrowid)
        result = self.get_release_profile(new_id)
        assert result is not None
        return result

    def update_release_profile(self, profile_id: int, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE release_profiles SET name = ?, enabled = ?, required_json = ?, ignored_json = ?, "
                "indexer_ids_json = ?, tags_json = ?, quality_profile_ids_json = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (
                    data["name"],
                    1 if data.get("enabled", True) else 0,
                    json.dumps(data.get("required", [])),
                    json.dumps(data.get("ignored", [])),
                    json.dumps(data.get("indexer_ids", [])),
                    json.dumps(data.get("tags", [])),
                    json.dumps(data.get("quality_profile_ids", [])),
                    int(profile_id),
                ),
            )
            self.conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get_release_profile(profile_id)

    def delete_release_profile(self, profile_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM release_profiles WHERE id = ?", (int(profile_id),))
            self.conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------------------------------------------------
    # Decision catalog and durations
    # ------------------------------------------------------------------------------------------------------------

    def get_decision_catalog(self) -> dict[str, Any]:
        return {
            "definitions": self.list_quality_definitions(),
            "formats": self.list_custom_formats(),
            "release_profiles": [rp for rp in self.list_release_profiles() if rp["enabled"]],
        }

    def get_album_track_durations(self, album_id: str) -> tuple[list[Optional[float]], Optional[int]]:
        """(per-track durations, album total_tracks) for the duration/estimate logic."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT duration_seconds FROM library_tracks WHERE album_id = ?", (str(album_id),)
            ).fetchall()
            alb = self.conn.execute("SELECT total_tracks FROM library_albums WHERE id = ?", (str(album_id),)).fetchone()
        total = int(alb[0]) if alb and alb[0] is not None else None
        return [float(r[0]) if r[0] is not None else None for r in rows], total

    def get_track_duration(self, track_id: str) -> Optional[float]:
        with self._lock:
            row = self.conn.execute(
                "SELECT duration_seconds FROM library_tracks WHERE id = ?", (str(track_id),)
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    # ------------------------------------------------------------------------------------------------------------
    # Quality profiles (v2 shape)
    # ------------------------------------------------------------------------------------------------------------

    def _format_quality_profile_row(self, row: sqlite3.Row) -> dict[str, Any]:
        res = dict(row)
        res["is_default"] = bool(res.get("is_default", 0))
        res["upgrade_allowed"] = bool(res.get("upgrade_allowed", 1))
        for key in ("min_size_mb", "max_size_mb"):
            res[key] = float(res[key]) if res.get(key) is not None else None
        res["items"] = normalize_entries(_loads(res.get("items_json"), []))
        res["preferred_tags"] = _loads(res.get("preferred_tags_json"), [])
        res["ignored_tags"] = _loads(res.get("ignored_tags_json"), [])
        res["custom_formats"] = _loads(res.get("custom_formats_json"), [])
        if not isinstance(res["custom_formats"], list):
            res["custom_formats"] = []
        fi = _loads(res.get("format_items_json"), [])
        res["format_items"] = [
            {"format_id": int(i["format_id"]), "score": int(i.get("score", 0))}
            for i in fi
            if isinstance(i, dict) and i.get("format_id") is not None
        ]
        res["min_score"] = int(res["min_score"]) if res.get("min_score") is not None else None
        res["min_format_score"] = int(res.get("min_format_score") or 0)
        res["cutoff_format_score"] = int(res.get("cutoff_format_score") or 0)
        res["min_upgrade_format_score"] = int(res.get("min_upgrade_format_score") or 1)
        return res

    def get_quality_profile(self, profile_id: str, include_catalog: bool = True) -> Optional[dict[str, Any]]:
        """One quality profile; ``include_catalog`` attaches the resolved decision catalog under ``catalog``."""
        with self._lock:
            row = self.conn.execute("SELECT * FROM quality_profiles WHERE id = ?", (str(profile_id),)).fetchone()
        if not row:
            return None
        res = self._format_quality_profile_row(row)
        if include_catalog:
            res["catalog"] = self.get_decision_catalog()
        return res

    def get_default_quality_profile(self, include_catalog: bool = True) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM quality_profiles WHERE is_default = 1 LIMIT 1").fetchone()
            if not row:
                row = self.conn.execute("SELECT * FROM quality_profiles ORDER BY name ASC LIMIT 1").fetchone()
        if not row:
            raise ValueError("No quality profiles configured in the database")
        res = self._format_quality_profile_row(row)
        if include_catalog:
            res["catalog"] = self.get_decision_catalog()
        return res

    def set_default_quality_profile(self, profile_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            if not self.conn.execute("SELECT 1 FROM quality_profiles WHERE id = ?", (str(profile_id),)).fetchone():
                return None
            self.conn.execute("UPDATE quality_profiles SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END", (str(profile_id),))
            self.conn.commit()
        return self.get_quality_profile(profile_id, include_catalog=False)

    def upsert_quality_profile(self, profile: Union[QualityProfile, dict[str, Any]]) -> dict[str, Any]:
        """Creates or updates a quality profile. If is_default is set, clears it on all others."""
        if isinstance(profile, QualityProfile):
            src: dict[str, Any] = {
                "id": profile.id,
                "name": profile.name,
                "cutoff": profile.cutoff,
                "items": profile.entries or [i.to_dict() for i in profile.items],
                "preferred_tags": profile.preferred_tags,
                "ignored_tags": profile.ignored_tags,
                "min_size_mb": profile.min_size_mb,
                "max_size_mb": profile.max_size_mb,
                "is_default": profile.is_default,
                "custom_formats": profile.custom_formats,
                "min_score": profile.min_score,
                "upgrade_allowed": profile.upgrade_allowed,
                "format_items": profile.format_items,
                "min_format_score": profile.min_format_score,
                "cutoff_format_score": profile.cutoff_format_score,
                "min_upgrade_format_score": profile.min_upgrade_format_score,
            }
        else:
            src = profile
        p_id = str(src.get("id"))
        entries = normalize_entries(
            [i.to_dict() if hasattr(i, "to_dict") else i for i in (src.get("items") or [])]
        )
        custom_formats = src.get("custom_formats", [])
        min_score = src.get("min_score")
        format_items = [
            {"format_id": int(i["format_id"]), "score": int(i.get("score", 0))}
            for i in (src.get("format_items") or [])
            if isinstance(i, dict) and i.get("format_id") is not None
        ]
        is_default = bool(src.get("is_default", False))
        with self._lock:
            existing = self.conn.execute(
                "SELECT format_items_json, min_format_score, cutoff_format_score, min_upgrade_format_score "
                "FROM quality_profiles WHERE id = ?",
                (p_id,),
            ).fetchone()
            if "format_items" not in src and existing:
                format_items = _loads(existing[0], [])
            min_fs = src.get("min_format_score", existing[1] if existing else 0)
            cut_fs = src.get("cutoff_format_score", existing[2] if existing else 0)
            min_up = src.get("min_upgrade_format_score", existing[3] if existing else 1)
            if is_default:
                self.conn.execute("UPDATE quality_profiles SET is_default = 0 WHERE id != ?", (p_id,))
            self.conn.execute(
                """
                INSERT INTO quality_profiles (
                    id, name, cutoff, items_json, preferred_tags_json, ignored_tags_json,
                    min_size_mb, max_size_mb, is_default, custom_formats_json, min_score, upgrade_allowed,
                    format_items_json, min_format_score, cutoff_format_score, min_upgrade_format_score, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    cutoff = excluded.cutoff,
                    items_json = excluded.items_json,
                    preferred_tags_json = excluded.preferred_tags_json,
                    ignored_tags_json = excluded.ignored_tags_json,
                    min_size_mb = excluded.min_size_mb,
                    max_size_mb = excluded.max_size_mb,
                    is_default = excluded.is_default,
                    custom_formats_json = excluded.custom_formats_json,
                    min_score = excluded.min_score,
                    upgrade_allowed = excluded.upgrade_allowed,
                    format_items_json = excluded.format_items_json,
                    min_format_score = excluded.min_format_score,
                    cutoff_format_score = excluded.cutoff_format_score,
                    min_upgrade_format_score = excluded.min_upgrade_format_score,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    p_id,
                    str(src.get("name")),
                    str(src.get("cutoff")),
                    json.dumps(entries),
                    json.dumps(src.get("preferred_tags", [])),
                    json.dumps(src.get("ignored_tags", [])),
                    src.get("min_size_mb"),
                    src.get("max_size_mb"),
                    1 if is_default else 0,
                    json.dumps(custom_formats if isinstance(custom_formats, list) else []),
                    int(min_score) if min_score is not None else None,
                    1 if bool(src.get("upgrade_allowed", True)) else 0,
                    json.dumps(format_items),
                    int(min_fs),
                    int(cut_fs),
                    int(min_up),
                ),
            )
            if self.conn.execute("SELECT COUNT(*) FROM quality_profiles WHERE is_default = 1").fetchone()[0] == 0:
                self.conn.execute("UPDATE quality_profiles SET is_default = 1 WHERE id = ?", (p_id,))
            self.conn.commit()
        result = self.get_quality_profile(p_id, include_catalog=False)
        if not result:
            raise sqlite3.OperationalError(f"Failed to retrieve upserted profile {p_id}")
        return result

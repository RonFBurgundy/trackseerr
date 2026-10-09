"""Schema migrations for Trackseerr storage.

Mixed into storage.Database (uses self._lock / self.conn).
SCHEMA_VERSION (in storage_common) is the baseline version (v71) or the head of the migration list in _migrate.
"""

from __future__ import annotations

import logging
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Optional

from trackseerr.storage_common import (
    _TRACK_DURATION_TOLERANCE,
    SCHEMA_VERSION,
)

logger = logging.getLogger("trackseerr.storage")


class MigrationsMixin:
    def _migrate(self) -> None:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
                )
                """
            )
            cur.execute("SELECT MAX(version) FROM schema_migrations")
            row = cur.fetchone()
            current_version = row[0] if (row and row[0] is not None) else 0
            self._fresh_install = current_version == 0

            if current_version == 0:
                schema_path = Path(__file__).resolve().parent / "storage_schema.sql"
                cur.executescript(schema_path.read_text(encoding="utf-8"))
                self._seed_baseline(cur)
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
                logger.info("[boot] migrations: initialized fresh database at baseline v%d", SCHEMA_VERSION)
                current_version = SCHEMA_VERSION
            elif 0 < current_version < SCHEMA_VERSION:
                msg = (
                    f"Database schema v{current_version} predates the v{SCHEMA_VERSION} baseline; "
                    "this build cannot upgrade it. Delete the database file and start fresh."
                )
                logger.error(msg)
                raise RuntimeError(msg)

            migrations: list[tuple[int, Any]] = [
                # Append (72, self._migration_v72) here for future versions.
            ]

            applied = 0
            latest_version = current_version
            for version, migration_fn in migrations:
                if current_version < version:
                    migration_fn(cur)
                    cur.execute(
                        "INSERT INTO schema_migrations (version) VALUES (?)",
                        (version,),
                    )
                    applied += 1
                    latest_version = version
            if applied:
                logger.info(
                    "[boot] migrations: applied %d (schema v%d -> v%d)", applied, current_version, latest_version
                )
            elif not getattr(self, "_fresh_install", False):
                logger.info("[boot] migrations: schema up to date (v%d)", current_version)

            # Idempotent (column-existence guarded), so it is deliberately not version-numbered.
            self._ensure_naming_formats(cur)
            self.conn.commit()

    def _seed_baseline(self, cur: sqlite3.Cursor) -> None:
        """Seed reference rows and runtime settings for the v71 baseline database."""
        # 1. Native metadata profiles
        cur.executemany(
            "INSERT INTO native_metadata_profiles (id, name, primary_types, secondary_types) VALUES (?, ?, ?, ?)",
            [
                (1, "Studio Albums", '["album"]', '["studio"]'),
                (2, "Studio Albums, EPs & Singles", '["album", "ep", "single"]', '["studio"]'),
                (
                    3,
                    "Everything",
                    '["album", "ep", "single", "broadcast", "other"]',
                    '["studio", "compilation", "soundtrack", "spokenword", "interview", "audiobook", "audio drama", "live", "remix", "dj-mix", "mixtape/street", "demo", "field recording"]',
                ),
            ],
        )

        # 2. Custom formats
        cur.executemany(
            "INSERT INTO custom_formats (id, name, include_in_rename, specifications_json) VALUES (?, ?, ?, ?)",
            [
                (1, "Preferred Groups", 0, '[{"name": "DeVOiD", "implementation": "ReleaseGroupSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bDeVOiD\\\\b"}}, {"name": "PERFECT", "implementation": "ReleaseGroupSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bPERFECT\\\\b"}}, {"name": "ENRiCH", "implementation": "ReleaseGroupSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bENRiCH\\\\b"}}, {"name": "BigFLAC", "implementation": "ReleaseGroupSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bBigFLAC\\\\b"}}, {"name": "GalaxyLossless", "implementation": "ReleaseGroupSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bGalaxyLossless\\\\b"}}, {"name": "MusiCHI", "implementation": "ReleaseGroupSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bMusiCHI\\\\b"}}, {"name": "LoRD", "implementation": "ReleaseGroupSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bLoRD\\\\b"}}]'),
                (2, "CD", 0, '[{"name": "CD", "implementation": "ReleaseTitleSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bCD(?:DA|-?Rip)?\\\\b"}}]'),
                (3, "Lossless", 0, '[{"name": "Lossless", "implementation": "ReleaseTitleSpecification", "negate": false, "required": false, "fields": {"value": "\\\\b(?:FLAC|Lossless|ALAC|APE|WavPack)\\\\b"}}]'),
                (4, "Hi-Res 24bit", 0, '[{"name": "Hi-Res 24bit", "implementation": "ReleaseTitleSpecification", "negate": false, "required": false, "fields": {"value": "24.?bit|Hi.?Res"}}]'),
                (5, "WEB", 0, '[{"name": "WEB", "implementation": "ReleaseTitleSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bWEB\\\\b"}}]'),
                (6, "Vinyl", 0, '[{"name": "Vinyl", "implementation": "ReleaseTitleSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bVinyl\\\\b"}}]'),
                (7, "Mono", 0, '[{"name": "Mono", "implementation": "ReleaseTitleSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bMono\\\\b"}}]'),
                (8, "Censored/Clean", 0, '[{"name": "Censored/Clean", "implementation": "ReleaseTitleSpecification", "negate": false, "required": false, "fields": {"value": "\\\\b(?:Censored|Clean)\\\\b"}}]'),
                (9, "Remastered", 0, '[{"name": "Remastered", "implementation": "ReleaseTitleSpecification", "negate": false, "required": false, "fields": {"value": "[Rr]emaster(?:ed)?"}}]'),
                (10, "Deluxe", 0, '[{"name": "Deluxe", "implementation": "ReleaseTitleSpecification", "negate": false, "required": false, "fields": {"value": "[Dd]eluxe"}}]'),
                (11, "Preferred: cd", 0, '[{"name": "Preferred: cd", "implementation": "ReleaseTitleSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bcd\\\\b|\\\\b(?:cd|cdda|cd-?rip|retail)\\\\b"}}]'),
                (12, "Preferred: web", 0, '[{"name": "Preferred: web", "implementation": "ReleaseTitleSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bweb\\\\b|\\\\b(?:web(?:-?dl|-?rip)?|qobuz|tidal|deezer|itunes|bandcamp|amazon(?:-hd)?)\\\\b"}}]'),
                (13, "Preferred: vinyl", 0, '[{"name": "Preferred: vinyl", "implementation": "ReleaseTitleSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bvinyl\\\\b|\\\\b(?:vinyl|lp|12-inch|record)\\\\b"}}]'),
                (14, "Preferred: remaster", 0, '[{"name": "Preferred: remaster", "implementation": "ReleaseTitleSpecification", "negate": false, "required": false, "fields": {"value": "\\\\bremaster\\\\b|\\\\b(?:remaster(?:ed)?)\\\\b"}}]'),
            ],
        )

        # 3. Release profiles
        cur.executemany(
            "INSERT INTO release_profiles (id, name, enabled, required_json, ignored_json, indexer_ids_json, tags_json, quality_profile_ids_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "Reject bad sources", 1, "[]", '["/transcode(?:d)?/", "/up[-_ ]?(?:conver\\\\w+|sampl\\\\w+)/", "/fake[-_ .]?flac/", "/lossy[-_ ]?(?:master|web|flac)/", "/\\\\bMQA\\\\b/"]', "[]", "[]", "[]"),
                (2, "Ignored tags: Lossless (FLAC)", 1, "[]", '["/\\\\blive\\\\b|\\\\b(?:live)\\\\b/", "/\\\\bbootleg\\\\b|\\\\b(?:bootleg)\\\\b/", "/\\\\btribute\\\\b|\\\\b(?:tribute)\\\\b/", "/\\\\bkaraoke\\\\b|\\\\b(?:karaoke)\\\\b/"]', "[]", "[]", '["profile-lossless"]'),
                (3, "Ignored tags: High Quality (Any)", 1, "[]", '["/\\\\blive\\\\b|\\\\b(?:live)\\\\b/", "/\\\\bbootleg\\\\b|\\\\b(?:bootleg)\\\\b/"]', "[]", "[]", '["profile-high-quality"]'),
                (4, "Ignored tags: Standard MP3", 1, "[]", '["/\\\\blive\\\\b|\\\\b(?:live)\\\\b/", "/\\\\bbootleg\\\\b|\\\\b(?:bootleg)\\\\b/"]', "[]", "[]", '["profile-standard-mp3"]'),
            ],
        )

        # 4. Delay profiles
        cur.executemany(
            "INSERT INTO delay_profiles (id, name, order_idx, preferred_protocol, usenet_delay_min, torrent_delay_min, soulseek_delay_min, bypass_if_highest_quality, bypass_if_above_score, tags_json, is_default) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "Default", 0, "usenet", 0, 0, 0, 1, None, "[]", 1),
            ],
        )

        # Align custom_formats autoincrement sequence with pre-squash migration behavior
        cur.execute("UPDATE sqlite_sequence SET seq = 16 WHERE name = 'custom_formats'")

        # 5. Quality definitions
        cur.executemany(
            "INSERT INTO quality_definitions (quality, title, min_kbps, preferred_kbps, max_kbps) VALUES (?, ?, ?, ?, ?)",
            [
                ("FLAC 24bit", "FLAC 24bit", 0.0, 2000.0, 9500.0),
                ("FLAC 16bit", "FLAC 16bit", 0.0, 895.0, 1400.0),
                ("ALAC", "ALAC", 0.0, 900.0, 1600.0),
                ("WAV/AIFF", "WAV/AIFF", 1300.0, 1411.0, 5000.0),
                ("MP3 320", "MP3 320", 290.0, 320.0, 400.0),
                ("MP3 V0", "MP3 V0", 160.0, 245.0, 400.0),
                ("MP3 V1", "MP3 V1", 150.0, 225.0, 320.0),
                ("AAC 256", "AAC 256", 200.0, 256.0, 280.0),
                ("Opus", "Opus", 64.0, 160.0, 256.0),
                ("OGG Vorbis", "OGG Vorbis", 96.0, 192.0, 320.0),
                ("AAC (other)", "AAC (other)", 96.0, 192.0, 320.0),
                ("MP3 192", "MP3 192", 150.0, 192.0, 210.0),
                ("MP3 V2", "MP3 V2", 130.0, 190.0, 280.0),
                ("Unknown", "Unknown", 0.0, 195.0, 350.0),
            ],
        )

        # 6. Quality profiles (inserted in specific order to match rowid ordering in reference dump)
        cur.executemany(
            "INSERT INTO quality_profiles (id, name, cutoff, items_json, preferred_tags_json, ignored_tags_json, min_size_mb, max_size_mb, is_default, custom_formats_json, min_score, upgrade_allowed, format_items_json, min_format_score, cutoff_format_score, min_upgrade_format_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "profile-lossless",
                    "Lossless (FLAC)",
                    "FLAC 16bit",
                    '[{"type": "quality", "quality": "FLAC 24bit", "allowed": true}, {"type": "quality", "quality": "FLAC 16bit", "allowed": true}, {"type": "quality", "quality": "ALAC", "allowed": true}, {"type": "quality", "quality": "MP3 320", "allowed": false}, {"type": "quality", "quality": "AAC 256", "allowed": false}, {"type": "quality", "quality": "MP3 V0", "allowed": false}, {"type": "quality", "quality": "MP3 192", "allowed": false}, {"type": "quality", "quality": "MP3 V2", "allowed": false}, {"type": "quality", "quality": "Unknown", "allowed": false}, {"type": "quality", "quality": "WAV/AIFF", "allowed": false}, {"type": "quality", "quality": "MP3 V1", "allowed": false}, {"type": "quality", "quality": "AAC (other)", "allowed": false}, {"type": "quality", "quality": "Opus", "allowed": false}, {"type": "quality", "quality": "OGG Vorbis", "allowed": false}]',
                    '["cd", "web", "vinyl", "remaster"]',
                    '["live", "bootleg", "tribute", "karaoke"]',
                    None,
                    None,
                    1,
                    None,
                    None,
                    1,
                    '[{"format_id": 1, "score": 100}, {"format_id": 2, "score": 10}, {"format_id": 3, "score": 10}, {"format_id": 4, "score": 15}, {"format_id": 5, "score": 5}, {"format_id": 6, "score": -50}, {"format_id": 7, "score": -10}, {"format_id": 8, "score": -15}, {"format_id": 9, "score": 0}, {"format_id": 10, "score": 0}, {"format_id": 11, "score": 50}, {"format_id": 12, "score": 50}, {"format_id": 13, "score": 50}, {"format_id": 14, "score": 50}]',
                    -100,
                    0,
                    1,
                ),
                (
                    "profile-high-quality",
                    "High Quality (Any)",
                    "FLAC 16bit",
                    '[{"type": "quality", "quality": "FLAC 24bit", "allowed": true}, {"type": "quality", "quality": "FLAC 16bit", "allowed": true}, {"type": "quality", "quality": "ALAC", "allowed": true}, {"type": "quality", "quality": "MP3 320", "allowed": true}, {"type": "quality", "quality": "AAC 256", "allowed": true}, {"type": "quality", "quality": "MP3 V0", "allowed": true}, {"type": "quality", "quality": "MP3 192", "allowed": false}, {"type": "quality", "quality": "MP3 V2", "allowed": false}, {"type": "quality", "quality": "Unknown", "allowed": false}, {"type": "quality", "quality": "WAV/AIFF", "allowed": false}, {"type": "quality", "quality": "MP3 V1", "allowed": false}, {"type": "quality", "quality": "AAC (other)", "allowed": false}, {"type": "quality", "quality": "Opus", "allowed": false}, {"type": "quality", "quality": "OGG Vorbis", "allowed": false}]',
                    '["cd", "web"]',
                    '["live", "bootleg"]',
                    None,
                    None,
                    0,
                    None,
                    None,
                    1,
                    '[{"format_id": 1, "score": 100}, {"format_id": 2, "score": 10}, {"format_id": 3, "score": 10}, {"format_id": 4, "score": 15}, {"format_id": 5, "score": 5}, {"format_id": 6, "score": -50}, {"format_id": 7, "score": -10}, {"format_id": 8, "score": -15}, {"format_id": 9, "score": 0}, {"format_id": 10, "score": 0}, {"format_id": 11, "score": 50}, {"format_id": 12, "score": 50}]',
                    -100,
                    0,
                    1,
                ),
                (
                    "profile-standard-mp3",
                    "Standard MP3",
                    "MP3 320",
                    '[{"type": "quality", "quality": "FLAC 24bit", "allowed": false}, {"type": "quality", "quality": "FLAC 16bit", "allowed": false}, {"type": "quality", "quality": "ALAC", "allowed": false}, {"type": "quality", "quality": "MP3 320", "allowed": true}, {"type": "quality", "quality": "MP3 V0", "allowed": true}, {"type": "quality", "quality": "AAC 256", "allowed": true}, {"type": "quality", "quality": "MP3 192", "allowed": true}, {"type": "quality", "quality": "MP3 V2", "allowed": false}, {"type": "quality", "quality": "Unknown", "allowed": false}, {"type": "quality", "quality": "WAV/AIFF", "allowed": false}, {"type": "quality", "quality": "MP3 V1", "allowed": false}, {"type": "quality", "quality": "AAC (other)", "allowed": false}, {"type": "quality", "quality": "Opus", "allowed": false}, {"type": "quality", "quality": "OGG Vorbis", "allowed": false}]',
                    "[]",
                    '["live", "bootleg"]',
                    None,
                    None,
                    0,
                    None,
                    None,
                    1,
                    '[{"format_id": 1, "score": 100}, {"format_id": 2, "score": 10}, {"format_id": 3, "score": 10}, {"format_id": 4, "score": 15}, {"format_id": 5, "score": 5}, {"format_id": 6, "score": -50}, {"format_id": 7, "score": -10}, {"format_id": 8, "score": -15}, {"format_id": 9, "score": 0}, {"format_id": 10, "score": 0}]',
                    -100,
                    0,
                    1,
                ),
            ],
        )

        # 7. General settings
        cur.execute(
            "INSERT INTO general_settings (id, api_key) VALUES (1, ?)",
            (secrets.token_hex(16),),
        )

        # 8. Media management settings
        cur.execute(
            """
            INSERT INTO media_management_settings (
                id, standard_track_format, multi_disc_track_format, mb_mirror_url, add_monitor_option, seed_complete_action
            ) VALUES (
                1,
                '{Album Title} ({Release Year}){[ - Album Type]}/{track:00} - {Track Title}{[ (Quality Full)]}',
                '{Album Title} ({Release Year}){[ - Album Type]}/{Medium Format} {medium:00}/{track:00} - {Track Title}{[ (Quality Full)]}',
                'https://api.brainzmash.cc',
                'existing',
                'keep'
            )
            """
        )

        # 9. Lidarr settings
        cur.execute("INSERT INTO lidarr_settings (id) VALUES (1)")

        # 10. KV store
        cur.execute("INSERT INTO kv_store (key, value) VALUES ('library_health_weekly', 'true')")

    def _dedupe_library_tracks(self, cur: sqlite3.Cursor) -> int:
        """Merges duplicate track rows into one survivor per group and returns how many rows were removed.

        Two rows are duplicates when they share an album and a non-empty foreign track id, or share album, disc,
        number and a NON-EMPTY clean title and both have a known duration within ``_TRACK_DURATION_TOLERANCE``
        seconds (disc and number default to 1 in the schema, so they alone prove nothing; an unknown duration never
        merges on the tuple). The survivor is the row that owns files, then the oldest. ``library_files`` /
        ``active_downloads`` / ``download_history`` are repointed first so nothing cascades away; a monitored
        duplicate keeps the survivor monitored, and the survivor's missing foreign_track_id / mb_recording_id /
        isrc are backfilled from the duplicates.
        """
        logger.info("[boot] migrations: scanning library_tracks for duplicate rows")
        removed = 0
        groups: list[list[str]] = []
        cur.execute(
            "SELECT GROUP_CONCAT(id, char(31)) FROM library_tracks WHERE foreign_track_id IS NOT NULL "
            "AND foreign_track_id != '' GROUP BY album_id, foreign_track_id HAVING COUNT(*) > 1"
        )
        groups += [str(r[0]).split("\x1f") for r in cur.fetchall()]
        cur.execute(
            "SELECT id, album_id, disc_number, track_number, clean_title, duration_seconds FROM library_tracks "
            "WHERE clean_title != '' AND duration_seconds IS NOT NULL "
            "ORDER BY album_id, disc_number, track_number, clean_title, duration_seconds"
        )
        cluster: list[str] = []
        anchor: Optional[tuple[Any, ...]] = None
        for tid, alb, disc, num, clean, dur in cur.fetchall() + [(None, None, None, None, None, None)]:
            same = (
                anchor is not None
                and tid is not None
                and anchor[:4] == (alb, disc, num, clean)
                and abs(float(dur) - anchor[4]) <= _TRACK_DURATION_TOLERANCE
            )
            if same:
                cluster.append(str(tid))
                continue
            if len(cluster) > 1:
                groups.append(cluster)
            cluster = [str(tid)] if tid is not None else []
            anchor = (alb, disc, num, clean, float(dur)) if tid is not None else None
        gone: set[str] = set()
        for ids in groups:
            ids = [i for i in ids if i not in gone]
            if len(ids) < 2:
                continue
            marks = ",".join("?" for _ in ids)
            cur.execute(
                f"SELECT t.id, t.monitored, t.foreign_track_id, t.mb_recording_id, t.isrc, "
                f"(SELECT COUNT(*) FROM library_files f WHERE f.track_id = t.id) AS nfiles "
                f"FROM library_tracks t WHERE t.id IN ({marks}) ORDER BY nfiles DESC, t.created_at ASC, t.rowid ASC",
                ids,
            )
            rows = cur.fetchall()
            keep = str(rows[0][0])
            dupes = [str(r[0]) for r in rows[1:]]
            any_monitored = any(int(r[1] or 0) for r in rows)
            dmarks = ",".join("?" for _ in dupes)
            for table in ("library_files", "active_downloads", "download_history"):
                cur.execute(f"UPDATE {table} SET track_id = ? WHERE track_id IN ({dmarks})", [keep, *dupes])
            if any_monitored:
                cur.execute("UPDATE library_tracks SET monitored = 1 WHERE id = ?", (keep,))
            # Survivor keeps its own identifiers; any it lacks come from the first duplicate that has one.
            for offset, column in enumerate(("foreign_track_id", "mb_recording_id", "isrc"), start=2):
                value = next((r[offset] for r in rows if r[offset] not in (None, "")), None)
                if value is not None:
                    cur.execute(
                        f"UPDATE library_tracks SET {column} = ? WHERE id = ? AND COALESCE({column}, '') = ''",
                        (value, keep),
                    )
            cur.execute(f"DELETE FROM library_tracks WHERE id IN ({dmarks})", dupes)
            gone.update(dupes)
            removed += len(dupes)
        return removed

    @staticmethod
    def _seed_download_history(cur: sqlite3.Cursor) -> int:
        """Backfills ``download_history`` from terminal ``active_downloads`` rows (grab + terminal event).

        Idempotent: seeded ids are deterministic (``seed-<download id>-<event>``) and inserted with
        ``INSERT OR IGNORE``, so running it again adds nothing. Returns the number of rows inserted.
        """
        before = cur.execute("SELECT COUNT(*) FROM download_history").fetchone()[0]
        cur.execute(
            """
            INSERT OR IGNORE INTO download_history (
                id, event, download_id, request_id, track_id, album_id, item_type, artist, title, release_title,
                quality, indexer, protocol, client, info_hash, message, created_at
            )
            SELECT 'seed-' || d.id || '-grabbed', 'grabbed', d.id, d.request_id, d.track_id, d.album_id, d.item_type,
                   d.artist, d.title, d.title, d.quality, d.indexer, d.protocol, c.name, d.download_hash,
                   'Grabbed (backfilled)', COALESCE(d.created_at, CURRENT_TIMESTAMP)
            FROM active_downloads d LEFT JOIN download_clients c ON c.id = d.client_id
            WHERE d.status IN ('failed', 'imported')
            """
        )
        cur.execute(
            """
            INSERT OR IGNORE INTO download_history (
                id, event, download_id, request_id, track_id, album_id, item_type, artist, title, release_title,
                quality, indexer, protocol, client, info_hash, message, created_at
            )
            SELECT 'seed-' || d.id || '-' || d.status,
                   CASE d.status WHEN 'imported' THEN 'imported' ELSE 'failed' END,
                   d.id, d.request_id, d.track_id, d.album_id, d.item_type,
                   d.artist, d.title, d.title, d.quality, d.indexer, d.protocol, c.name, d.download_hash,
                   COALESCE(d.error_message, CASE d.status WHEN 'imported' THEN 'Imported (backfilled)' END),
                   COALESCE(d.updated_at, d.created_at, CURRENT_TIMESTAMP)
            FROM active_downloads d LEFT JOIN download_clients c ON c.id = d.client_id
            WHERE d.status IN ('failed', 'imported')
            """
        )
        after = cur.execute("SELECT COUNT(*) FROM download_history").fetchone()[0]
        return int(after - before)

    def seed_download_history(self) -> int:
        """Public, idempotent re-run of the history backfill (see ``_seed_download_history``)."""
        with self._lock:
            cur = self.conn.cursor()
            inserted = self._seed_download_history(cur)
            self.conn.commit()
            return inserted

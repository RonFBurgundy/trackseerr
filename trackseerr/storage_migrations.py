"""Schema migrations for Trackseerr storage.

Mixed into storage.Database (uses self._lock / self.conn).
SCHEMA_VERSION (in storage_common) is the head of the list in _migrate.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
from typing import Any, Optional

from trackseerr.library_monitoring import (
    DEFAULT_MONITOR_OPTION,
    RELEASE_PRIMARY_TYPES,
    RELEASE_SECONDARY_TYPES,
)
from trackseerr.list_index import fold_search_text, library_sort_key
from trackseerr.models import UserPermission
from trackseerr.storage_common import (
    _TRACK_DURATION_TOLERANCE,
    clean_library_name,
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

            migrations = [
                (1, self._migration_v1),
                (2, self._migration_v2),
                (3, self._migration_v3),
                (4, self._migration_v4),
                (5, self._migration_v5),
                (6, self._migration_v6),
                (7, self._migration_v7),
                (8, self._migration_v8),
                (9, self._migration_v9),
                (10, self._migration_v10),
                (11, self._migration_v11),
                (12, self._migration_v12),
                (13, self._migration_v13),
                (14, self._migration_v14),
                (15, self._migration_v15),
                (16, self._migration_v16),
                (17, self._migration_v17),
                (18, self._migration_v18),
                (19, self._migration_v19),
                (20, self._migration_v20),
                (21, self._migration_v21),
                (22, self._migration_v22),
                (23, self._migration_v23),
                (24, self._migration_v24),
                (25, self._migration_v25),
                (26, self._migration_v26),
                (27, self._migration_v27),
                (28, self._migration_v28),
                (29, self._migration_v29),
                (30, self._migration_v30),
                (31, self._migration_v31),
                (32, self._migration_v32),
                (33, self._migration_v33),
                (34, self._migration_v34),
                (35, self._migration_v35),
                (36, self._migration_v36),
                (37, self._migration_v37),
                (38, self._migration_v38),
                (39, self._migration_v39),
                (40, self._migration_v40),
                (41, self._migration_v41),
                (42, self._migration_v42),
                (43, self._migration_v43),
                (44, self._migration_v44),
                (45, self._migration_v45),
                (46, self._migration_v46),
                (47, self._migration_v47),
                (48, self._migration_v48),
                (49, self._migration_v49),
                (50, self._migration_v50),
                (51, self._migration_v51),
                (52, self._migration_v52),
                (53, self._migration_v53),
                (54, self._migration_v54),
                (55, self._migration_v55),
                (56, self._migration_v56),
                (57, self._migration_v57),
                (58, self._migration_v58),
                (59, self._migration_v59),
                (60, self._migration_v60),
                (61, self._migration_v61),
                (62, self._migration_v62),
                (63, self._migration_v63),
                (64, self._migration_v64),
                (65, self._migration_v65),
                (66, self._migration_v66),
                (67, self._migration_v67),
                (68, self._migration_v68),
                (69, self._migration_v69),
                (70, self._migration_v70),
                (71, self._migration_v71),
            ]

            applied = 0
            latest_version = migrations[-1][0]
            for version, migration_fn in migrations:
                if current_version < version:
                    migration_fn(cur)
                    cur.execute(
                        "INSERT INTO schema_migrations (version) VALUES (?)",
                        (version,),
                    )
                    applied += 1
            if applied:
                logger.info(
                    "[boot] migrations: applied %d (schema v%d -> v%d)", applied, current_version, latest_version
                )
            else:
                logger.info("[boot] migrations: schema up to date (v%d)", current_version)
            # Idempotent (column-existence guarded), so it is deliberately not version-numbered.
            self._ensure_naming_formats(cur)
            self.conn.commit()

    def _migration_v1(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                email TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS playlists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                service TEXT NOT NULL DEFAULT 'spotify',
                description TEXT NOT NULL DEFAULT '',
                poster_url TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_synced_at TEXT,
                sync_status TEXT NOT NULL DEFAULT 'never_synced',
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS playlist_targets (
                playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                PRIMARY KEY (playlist_id, user_id)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_playlist_targets_user ON playlist_targets(user_id)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS missing_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                album TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_missing_tracks_playlist ON missing_tracks(playlist_id)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
                data TEXT NOT NULL DEFAULT '{}',
                expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)"
        )

    def _migration_v2(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            ALTER TABLE playlists ADD COLUMN creator_id TEXT REFERENCES users(id)
            """
        )

    def _migration_v3(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            ALTER TABLE playlists ADD COLUMN tracks_json TEXT
            """
        )

    def _migration_v4(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS match_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_title TEXT NOT NULL,
                source_artist TEXT NOT NULL,
                plex_rating_key TEXT NOT NULL,
                plex_title TEXT NOT NULL,
                plex_artist TEXT NOT NULL,
                created_by TEXT REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                UNIQUE(source_title, source_artist)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_match_overrides_lookup ON match_overrides(source_title, source_artist)"
        )

    def _migration_v5(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            ALTER TABLE missing_tracks ADD COLUMN lidarr_status TEXT NOT NULL DEFAULT 'unmonitored'
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_missing_tracks_lidarr_status ON missing_tracks(lidarr_status)"
        )

    def _migration_v6(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS music_requests (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                item_type TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                album TEXT,
                cover_url TEXT,
                preview_url TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                release_date TEXT,
                foreign_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_requests_user ON music_requests(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON music_requests(status)")

    def _migration_v7(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS media_management_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                artist_folder_format TEXT NOT NULL DEFAULT '{Artist Name}',
                album_folder_format TEXT NOT NULL DEFAULT '{Album Title} ({Release Year}){[ - Album Type]}',
                standard_track_format TEXT NOT NULL DEFAULT '{track:00} - {Track Title}{[ (Quality Full)]}',
                compilation_track_format TEXT NOT NULL DEFAULT '{track:00} - {Artist Name} - {Track Title}{[ (Quality Full)]}',
                multi_disc_folder_format TEXT NOT NULL DEFAULT '{Medium Format} {medium:00}',
                root_folder_path TEXT NOT NULL DEFAULT '/data/media/music',
                colon_replacement_format TEXT NOT NULL DEFAULT ' - ',
                clean_artist_names INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            """
            INSERT OR IGNORE INTO media_management_settings (id) VALUES (1);
            """
        )

    def _migration_v8(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS download_clients (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                driver_type TEXT NOT NULL,
                host_url TEXT NOT NULL,
                api_key TEXT,
                username TEXT,
                password TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 1,
                extra_settings_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS indexers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                indexer_type TEXT NOT NULL,
                host_url TEXT NOT NULL,
                api_key TEXT,
                categories TEXT NOT NULL DEFAULT '3000,3010,3020,3030,3040',
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS active_downloads (
                id TEXT PRIMARY KEY,
                request_id TEXT,
                client_id TEXT NOT NULL,
                download_hash TEXT,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                item_type TEXT NOT NULL DEFAULT 'track',
                status TEXT NOT NULL DEFAULT 'queued',
                progress REAL NOT NULL DEFAULT 0.0,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                source_path TEXT,
                target_path TEXT,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_id) REFERENCES download_clients(id) ON DELETE CASCADE,
                FOREIGN KEY (request_id) REFERENCES music_requests(id) ON DELETE SET NULL
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_active_downloads_status ON active_downloads(status);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_active_downloads_client ON active_downloads(client_id);"
        )

    def _migration_v9(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            ALTER TABLE media_management_settings ADD COLUMN staging_folder_path TEXT NOT NULL DEFAULT ''
            """
        )
        cur.execute(
            """
            ALTER TABLE media_management_settings ADD COLUMN import_mode TEXT NOT NULL DEFAULT 'move'
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS lidarr_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                url TEXT,
                api_key TEXT,
                auto_search INTEGER NOT NULL DEFAULT 1,
                root_folder TEXT,
                quality_profile_id INTEGER,
                metadata_profile_id INTEGER,
                trickle_rate_seconds REAL NOT NULL DEFAULT 3.0,
                trickle_batch_size INTEGER NOT NULL DEFAULT 25,
                auto_trickle INTEGER NOT NULL DEFAULT 0,
                auto_trickle_interval_minutes INTEGER NOT NULL DEFAULT 30,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            """
            INSERT OR IGNORE INTO lidarr_settings (id) VALUES (1);
            """
        )

    def _migration_v10(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS quality_profiles (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                cutoff TEXT NOT NULL,
                items_json TEXT NOT NULL,
                preferred_tags_json TEXT NOT NULL DEFAULT '[]',
                ignored_tags_json TEXT NOT NULL DEFAULT '[]',
                min_size_mb REAL,
                max_size_mb REAL,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        cur.execute("SELECT COUNT(*) FROM quality_profiles")
        row = cur.fetchone()
        count = row[0] if (row and row[0] is not None) else 0
        if count == 0:
            # Seed Profile 1: Lossless (FLAC)
            p1_items = [
                {"quality": "FLAC 24bit", "allowed": True, "weight": 1000},
                {"quality": "FLAC 16bit", "allowed": True, "weight": 900},
                {"quality": "MP3 320", "allowed": False, "weight": 800},
                {"quality": "AAC 256", "allowed": False, "weight": 700},
                {"quality": "MP3 V0", "allowed": False, "weight": 600},
                {"quality": "MP3 192", "allowed": False, "weight": 500},
                {"quality": "MP3 V2", "allowed": False, "weight": 400},
                {"quality": "Unknown", "allowed": False, "weight": 100},
            ]
            cur.execute(
                """
                INSERT INTO quality_profiles (
                    id, name, cutoff, items_json, preferred_tags_json, ignored_tags_json,
                    min_size_mb, max_size_mb, is_default
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "profile-lossless",
                    "Lossless (FLAC)",
                    "FLAC 16bit",
                    json.dumps(p1_items),
                    json.dumps(["cd", "web", "vinyl", "remaster"]),
                    json.dumps(["live", "bootleg", "tribute", "karaoke"]),
                    None,
                    None,
                    1,
                ),
            )

            # Seed Profile 2: High Quality (Any)
            p2_items = [
                {"quality": "FLAC 24bit", "allowed": True, "weight": 1000},
                {"quality": "FLAC 16bit", "allowed": True, "weight": 900},
                {"quality": "MP3 320", "allowed": True, "weight": 800},
                {"quality": "AAC 256", "allowed": True, "weight": 700},
                {"quality": "MP3 V0", "allowed": True, "weight": 600},
                {"quality": "MP3 192", "allowed": False, "weight": 500},
                {"quality": "MP3 V2", "allowed": False, "weight": 400},
                {"quality": "Unknown", "allowed": False, "weight": 100},
            ]
            cur.execute(
                """
                INSERT INTO quality_profiles (
                    id, name, cutoff, items_json, preferred_tags_json, ignored_tags_json,
                    min_size_mb, max_size_mb, is_default
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "profile-high-quality",
                    "High Quality (Any)",
                    "FLAC 16bit",
                    json.dumps(p2_items),
                    json.dumps(["cd", "web"]),
                    json.dumps(["live", "bootleg"]),
                    None,
                    None,
                    0,
                ),
            )

            # Seed Profile 3: Standard MP3
            p3_items = [
                {"quality": "FLAC 24bit", "allowed": False, "weight": 1000},
                {"quality": "FLAC 16bit", "allowed": False, "weight": 900},
                {"quality": "MP3 320", "allowed": True, "weight": 800},
                {"quality": "MP3 V0", "allowed": True, "weight": 700},
                {"quality": "AAC 256", "allowed": True, "weight": 600},
                {"quality": "MP3 192", "allowed": True, "weight": 500},
                {"quality": "MP3 V2", "allowed": False, "weight": 400},
                {"quality": "Unknown", "allowed": False, "weight": 100},
            ]
            cur.execute(
                """
                INSERT INTO quality_profiles (
                    id, name, cutoff, items_json, preferred_tags_json, ignored_tags_json,
                    min_size_mb, max_size_mb, is_default
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "profile-standard-mp3",
                    "Standard MP3",
                    "MP3 320",
                    json.dumps(p3_items),
                    json.dumps([]),
                    json.dumps(["live", "bootleg"]),
                    None,
                    None,
                    0,
                ),
            )

    def _migration_v11(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            ALTER TABLE media_management_settings ADD COLUMN write_audio_tags INTEGER NOT NULL DEFAULT 1
            """
        )
        cur.execute(
            """
            ALTER TABLE media_management_settings ADD COLUMN embed_artwork INTEGER NOT NULL DEFAULT 1
            """
        )
        cur.execute(
            """
            ALTER TABLE media_management_settings ADD COLUMN save_cover_art_file INTEGER NOT NULL DEFAULT 1
            """
        )

    def _migration_v12(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_channels (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                channel_type TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                config_json TEXT NOT NULL DEFAULT '{}',
                events_json TEXT NOT NULL DEFAULT '["request_created","request_approved","request_rejected","download_started","item_available","download_failed"]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_notification_channels_enabled ON notification_channels(enabled);
            """
        )

    def _migration_v13(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            "ALTER TABLE users ADD COLUMN permissions INTEGER NOT NULL DEFAULT 34"
        )
        cur.execute(
            "ALTER TABLE users ADD COLUMN request_limit_quota INTEGER"
        )
        cur.execute(
            "ALTER TABLE users ADD COLUMN request_limit_days INTEGER DEFAULT 7"
        )
        cur.execute(
            "UPDATE users SET permissions = permissions | 1 WHERE is_admin = 1"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS media_issues (
                id TEXT PRIMARY KEY,
                request_id TEXT REFERENCES music_requests(id) ON DELETE SET NULL,
                media_title TEXT NOT NULL,
                artist TEXT NOT NULL,
                issue_type TEXT NOT NULL,
                problem_details TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_issues_status ON media_issues(status);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_issues_user ON media_issues(user_id);"
        )

    def _migration_v14(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            "ALTER TABLE music_requests ADD COLUMN quality_profile_id TEXT REFERENCES quality_profiles(id);"
        )
        cur.execute(
            "ALTER TABLE music_requests ADD COLUMN current_quality TEXT;"
        )
        cur.execute(
            "ALTER TABLE music_requests ADD COLUMN cutoff_met INTEGER NOT NULL DEFAULT 1;"
        )
        cur.execute(
            "ALTER TABLE media_management_settings ADD COLUMN delete_completed_transfers INTEGER NOT NULL DEFAULT 0;"
        )
        cur.execute(
            "ALTER TABLE media_management_settings ADD COLUMN enable_quality_upgrades INTEGER NOT NULL DEFAULT 1;"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_music_requests_cutoff ON music_requests(status, cutoff_met);"
        )

    def _migration_v15(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            "ALTER TABLE media_management_settings ADD COLUMN library_mode TEXT NOT NULL DEFAULT 'native';"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS library_artists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                clean_name TEXT NOT NULL,
                foreign_artist_id TEXT,
                path TEXT,
                monitored INTEGER NOT NULL DEFAULT 1,
                quality_profile_id TEXT REFERENCES quality_profiles(id),
                metadata_json TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lib_artists_clean_name ON library_artists(clean_name);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lib_artists_foreign ON library_artists(foreign_artist_id);"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS library_albums (
                id TEXT PRIMARY KEY,
                artist_id TEXT NOT NULL REFERENCES library_artists(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                clean_title TEXT NOT NULL,
                foreign_album_id TEXT,
                release_date TEXT,
                year INTEGER,
                album_type TEXT NOT NULL DEFAULT 'album',
                monitored INTEGER NOT NULL DEFAULT 1,
                path TEXT,
                cover_url TEXT,
                total_tracks INTEGER,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lib_albums_artist ON library_albums(artist_id);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lib_albums_clean_title ON library_albums(clean_title);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lib_albums_foreign ON library_albums(foreign_album_id);"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS library_tracks (
                id TEXT PRIMARY KEY,
                album_id TEXT NOT NULL REFERENCES library_albums(id) ON DELETE CASCADE,
                artist_id TEXT NOT NULL REFERENCES library_artists(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                clean_title TEXT NOT NULL,
                track_number INTEGER NOT NULL DEFAULT 1,
                disc_number INTEGER NOT NULL DEFAULT 1,
                duration_seconds REAL,
                monitored INTEGER NOT NULL DEFAULT 1,
                foreign_track_id TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lib_tracks_album ON library_tracks(album_id);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lib_tracks_artist ON library_tracks(artist_id);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lib_tracks_clean_title ON library_tracks(clean_title);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lib_tracks_foreign ON library_tracks(foreign_track_id);"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS library_files (
                id TEXT PRIMARY KEY,
                track_id TEXT NOT NULL REFERENCES library_tracks(id) ON DELETE CASCADE,
                file_path TEXT NOT NULL UNIQUE,
                relative_path TEXT NOT NULL,
                codec TEXT NOT NULL,
                bitrate INTEGER,
                sample_rate INTEGER,
                bits_per_sample INTEGER,
                quality_name TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                cutoff_met INTEGER NOT NULL DEFAULT 1,
                date_added TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lib_files_track ON library_files(track_id);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lib_files_path ON library_files(file_path);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lib_files_cutoff ON library_files(cutoff_met);"
        )

    def _migration_v16(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS general_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                application_url TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            "INSERT OR IGNORE INTO general_settings (id, application_url) VALUES (1, '');"
        )

    def _migration_v17(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS download_blocklist (
                id TEXT PRIMARY KEY,
                source_title TEXT NOT NULL,
                artist TEXT,
                album TEXT,
                release_guid TEXT,
                info_hash TEXT,
                protocol TEXT,
                indexer TEXT,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_blocklist_hash ON download_blocklist(info_hash);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_blocklist_title ON download_blocklist(source_title);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_blocklist_guid ON download_blocklist(release_guid);"
        )

        cur.execute("PRAGMA table_info(active_downloads);")
        columns = [row[1] for row in cur.fetchall()]
        if "track_id" not in columns:
            cur.execute(
                "ALTER TABLE active_downloads ADD COLUMN track_id TEXT REFERENCES library_tracks(id);"
            )
        if "album_id" not in columns:
            cur.execute(
                "ALTER TABLE active_downloads ADD COLUMN album_id TEXT REFERENCES library_albums(id);"
            )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_active_downloads_track ON active_downloads(track_id);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_active_downloads_album ON active_downloads(album_id);"
        )

    def _migration_v18(self, cur: sqlite3.Cursor) -> None:
        cur.execute("PRAGMA table_info(general_settings);")
        columns = [row[1] for row in cur.fetchall()]
        if "api_key" not in columns:
            cur.execute(
                "ALTER TABLE general_settings ADD COLUMN api_key TEXT NOT NULL DEFAULT '';"
            )

        cur.execute("SELECT api_key FROM general_settings WHERE id = 1")
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT OR IGNORE INTO general_settings (id, application_url, api_key) VALUES (1, '', ?)",
                (secrets.token_hex(16),),
            )
        elif not row[0]:
            cur.execute(
                "UPDATE general_settings SET api_key = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                (secrets.token_hex(16),),
            )

    def _migration_v19(self, cur: sqlite3.Cursor) -> None:
        cur.execute("PRAGMA table_info(quality_profiles);")
        qp_cols = [row[1] for row in cur.fetchall()]
        if "custom_formats_json" not in qp_cols:
            cur.execute(
                "ALTER TABLE quality_profiles ADD COLUMN custom_formats_json TEXT;"
            )
        if "min_score" not in qp_cols:
            cur.execute(
                "ALTER TABLE quality_profiles ADD COLUMN min_score INTEGER;"
            )

        cur.execute("PRAGMA table_info(media_management_settings);")
        mm_cols = [row[1] for row in cur.fetchall()]
        if "seed_ratio_limit" not in mm_cols:
            cur.execute(
                "ALTER TABLE media_management_settings ADD COLUMN seed_ratio_limit REAL;"
            )
        if "seed_time_limit_minutes" not in mm_cols:
            cur.execute(
                "ALTER TABLE media_management_settings ADD COLUMN seed_time_limit_minutes INTEGER;"
            )

    def _migration_v54(self, cur: sqlite3.Cursor) -> None:
        """AcoustID fingerprint fallback toggle for weak tag matches on download import (off by default)."""
        cur.execute("PRAGMA table_info(media_management_settings);")
        mm_cols = [row[1] for row in cur.fetchall()]
        if "fingerprint_on_weak_match" not in mm_cols:
            cur.execute(
                "ALTER TABLE media_management_settings ADD COLUMN fingerprint_on_weak_match INTEGER NOT NULL DEFAULT 0;"
            )

    def _migration_v55(self, cur: sqlite3.Cursor) -> None:
        """Native downloads hold files that could not be matched to a catalog track (JSON list of paths)."""
        cur.execute("PRAGMA table_info(active_downloads);")
        if "unmatched_files" not in {row[1] for row in cur.fetchall()}:
            cur.execute("ALTER TABLE active_downloads ADD COLUMN unmatched_files TEXT;")

    def _migration_v58(self, cur: sqlite3.Cursor) -> None:
        """How hardlinked torrent files are tagged: 'copy_and_tag' (private copy) or 'keep_hardlink' (skip tags)."""
        cur.execute("PRAGMA table_info(media_management_settings);")
        if "torrent_hardlink_tags" not in {row[1] for row in cur.fetchall()}:
            cur.execute(
                "ALTER TABLE media_management_settings ADD COLUMN torrent_hardlink_tags TEXT NOT NULL DEFAULT 'copy_and_tag'"
            )

    def _migration_v62(self, cur: sqlite3.Cursor) -> None:
        """Recycle bin and quarantine settings. An empty path means the default folder under the library root; a replaced
        file is only ever deleted instead of recycled when ``recycle_bin_permanent_delete`` is explicitly on."""
        cur.execute("PRAGMA table_info(media_management_settings);")
        have = {row[1] for row in cur.fetchall()}
        for column, ddl in (
            ("recycle_bin_path", "TEXT NOT NULL DEFAULT ''"),
            ("recycle_bin_cleanup_days", "INTEGER NOT NULL DEFAULT 30"),
            ("recycle_bin_permanent_delete", "INTEGER NOT NULL DEFAULT 0"),
            ("quarantine_folder_path", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in have:
                cur.execute(f"ALTER TABLE media_management_settings ADD COLUMN {column} {ddl};")

    def _migration_v61(self, cur: sqlite3.Cursor) -> None:
        """Issue lifecycle: validated statuses (``wont_fix`` replaces ``closed``), resolution stamps, media refs,
        reporter-seen tracking and the ``issue_comments`` thread."""
        cur.execute("PRAGMA table_info(media_issues);")
        have = {row[1] for row in cur.fetchall()}
        for column in (
            "resolved_at", "resolved_by", "album_id", "track_id", "discovery_id", "item_type",
            "reporter_seen_at", "last_activity_at", "last_staff_activity_at",
        ):
            if column not in have:
                cur.execute(f"ALTER TABLE media_issues ADD COLUMN {column} TEXT;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS issue_comments (
                id TEXT PRIMARY KEY,
                issue_id TEXT NOT NULL REFERENCES media_issues(id) ON DELETE CASCADE,
                user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_system INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_issue_comments_issue ON issue_comments(issue_id, created_at);")
        cur.execute("UPDATE media_issues SET status = 'resolved' WHERE status = 'closed';")
        cur.execute(
            "UPDATE media_issues SET status = 'open' WHERE status NOT IN ('open', 'in_progress', 'resolved', 'wont_fix');"
        )
        cur.execute(
            "UPDATE media_issues SET resolved_at = replace(updated_at, ' ', 'T') "
            "WHERE status IN ('resolved', 'wont_fix') AND resolved_at IS NULL;"
        )
        cur.execute(
            "UPDATE media_issues SET last_activity_at = replace(updated_at, ' ', 'T') WHERE last_activity_at IS NULL;"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_media_issues_album ON media_issues(album_id);")

    def _migration_v60(self, cur: sqlite3.Cursor) -> None:
        """Discovery <-> library artist identity cache (``artist_links``); one row per resolved side, each key unique."""
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS artist_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                library_artist_id TEXT,
                discovery_id TEXT,
                mbid TEXT,
                name TEXT,
                confidence TEXT NOT NULL DEFAULT 'none' CHECK (confidence IN ('mbid', 'name', 'none')),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_artist_links_library ON artist_links(library_artist_id) "
            "WHERE library_artist_id IS NOT NULL;"
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_artist_links_discovery ON artist_links(discovery_id) "
            "WHERE discovery_id IS NOT NULL;"
        )

    def _migration_v59(self, cur: sqlite3.Cursor) -> None:
        """Seed cleanup: ``seed_complete_action`` replaces ``delete_completed_transfers`` (kept readable, no longer
        used), per-download cleanup bookkeeping and placed-file record, and the two new Needs-review finding kinds."""
        cur.execute("PRAGMA table_info(media_management_settings);")
        if "seed_complete_action" not in {row[1] for row in cur.fetchall()}:
            cur.execute(
                "ALTER TABLE media_management_settings ADD COLUMN seed_complete_action TEXT NOT NULL DEFAULT 'remove'"
            )
            cur.execute(
                "UPDATE media_management_settings SET seed_complete_action = "
                "CASE WHEN COALESCE(delete_completed_transfers, 0) = 0 THEN 'keep' ELSE 'remove' END"
            )
        cur.execute("PRAGMA table_info(active_downloads);")
        dl_cols = {row[1] for row in cur.fetchall()}
        for name, ddl in (
            ("cleanup_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("cleanup_error", "TEXT"),
            ("placed_files", "TEXT"),
        ):
            if name not in dl_cols:
                cur.execute(f"ALTER TABLE active_downloads ADD COLUMN {name} {ddl};")
        # SQLite cannot alter a CHECK constraint: rebuild the findings table with the widened kind list.
        cur.execute(
            """
            CREATE TABLE library_health_findings_new (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('server_unindexed', 'server_stale', 'weak_match', 'orphan_torrent', 'cleanup_failed')),
                server_kind TEXT,
                cause TEXT NOT NULL,
                group_key TEXT NOT NULL,
                path TEXT NOT NULL,
                detail_json TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                dismissed INTEGER NOT NULL DEFAULT 0,
                UNIQUE (kind, path)
            );
            """
        )
        cur.execute(
            "INSERT INTO library_health_findings_new "
            "(id, kind, server_kind, cause, group_key, path, detail_json, first_seen, last_seen, dismissed) "
            "SELECT id, kind, server_kind, cause, group_key, path, detail_json, first_seen, last_seen, dismissed "
            "FROM library_health_findings"
        )
        cur.execute("DROP TABLE library_health_findings")
        cur.execute("ALTER TABLE library_health_findings_new RENAME TO library_health_findings")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_health_findings_group ON library_health_findings(group_key);")

    def _migration_v57(self, cur: sqlite3.Cursor) -> None:
        """Library health: findings (server vs disk diff, weak import matches), run history, dismissals, and the
        media-server path mapping (``media_server_settings.path_mapping_json``). The weekly-check toggle lives in
        ``kv_store`` (``library_health_weekly``, default on)."""
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS library_health_findings (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('server_unindexed', 'server_stale', 'weak_match')),
                server_kind TEXT,
                cause TEXT NOT NULL,
                group_key TEXT NOT NULL,
                path TEXT NOT NULL,
                detail_json TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                dismissed INTEGER NOT NULL DEFAULT 0,
                UNIQUE (kind, path)
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_health_findings_group ON library_health_findings(group_key);")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS library_health_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                server_kind TEXT,
                disk_files INTEGER NOT NULL DEFAULT 0,
                server_files INTEGER NOT NULL DEFAULT 0,
                unindexed INTEGER NOT NULL DEFAULT 0,
                stale INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS library_health_dismissals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL CHECK (scope IN ('file', 'folder')),
                path TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                UNIQUE (scope, path)
            );
            """
        )
        cur.execute("PRAGMA table_info(media_server_settings);")
        if "path_mapping_json" not in {row[1] for row in cur.fetchall()}:
            cur.execute("ALTER TABLE media_server_settings ADD COLUMN path_mapping_json TEXT NOT NULL DEFAULT '';")
        cur.execute(
            "INSERT OR IGNORE INTO kv_store (key, value) VALUES ('library_health_weekly', 'true')"
        )

    def _migration_v56(self, cur: sqlite3.Cursor) -> None:
        """Per-indexer seed rules (NULL = inherit global) and the seed-rule snapshot taken on each download at grab time."""
        cur.execute("PRAGMA table_info(indexers);")
        idx_cols = {row[1] for row in cur.fetchall()}
        for col, decl in (
            ("seed_ratio", "REAL"),
            ("seed_time_minutes", "INTEGER"),
            ("discography_seed_time_minutes", "INTEGER"),
            ("minimum_seeders", "INTEGER"),
        ):
            if col not in idx_cols:
                cur.execute(f"ALTER TABLE indexers ADD COLUMN {col} {decl};")
        cur.execute("PRAGMA table_info(active_downloads);")
        ad_cols = {row[1] for row in cur.fetchall()}
        for col, decl in (
            ("indexer_id", "TEXT"),
            ("seed_ratio_target", "REAL"),
            ("seed_time_target_minutes", "INTEGER"),
            ("seed_rule_source", "TEXT"),
            # Last ratio / seeding time the worker saw from the client, so the queue can show seeding progress
            # without calling the client on every page load.
            ("seed_ratio_current", "REAL"),
            ("seeding_seconds", "INTEGER"),
        ):
            if col not in ad_cols:
                cur.execute(f"ALTER TABLE active_downloads ADD COLUMN {col} {decl};")

    def _migration_v20(self, cur: sqlite3.Cursor) -> None:
        cur.execute("PRAGMA table_info(media_management_settings);")
        mm_cols = [row[1] for row in cur.fetchall()]
        if "enrich_mbids" not in mm_cols:
            cur.execute(
                "ALTER TABLE media_management_settings ADD COLUMN enrich_mbids INTEGER NOT NULL DEFAULT 1;"
            )
        if "acoustid_api_key" not in mm_cols:
            cur.execute(
                "ALTER TABLE media_management_settings ADD COLUMN acoustid_api_key TEXT;"
            )
        if "mb_mirror_url" not in mm_cols:
            cur.execute(
                "ALTER TABLE media_management_settings ADD COLUMN mb_mirror_url TEXT NOT NULL DEFAULT 'https://api.brainzmash.org';"
            )

        cur.execute("PRAGMA table_info(library_artists);")
        art_cols = [row[1] for row in cur.fetchall()]
        if "mbid" not in art_cols:
            cur.execute("ALTER TABLE library_artists ADD COLUMN mbid TEXT;")
        if "image_url" not in art_cols:
            cur.execute("ALTER TABLE library_artists ADD COLUMN image_url TEXT;")
        if "banner_url" not in art_cols:
            cur.execute("ALTER TABLE library_artists ADD COLUMN banner_url TEXT;")
        if "bio" not in art_cols:
            cur.execute("ALTER TABLE library_artists ADD COLUMN bio TEXT;")
        if "genres" not in art_cols:
            cur.execute("ALTER TABLE library_artists ADD COLUMN genres TEXT;")
        if "country" not in art_cols:
            cur.execute("ALTER TABLE library_artists ADD COLUMN country TEXT;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_artists_mbid ON library_artists(mbid);")

        cur.execute("PRAGMA table_info(library_albums);")
        alb_cols = [row[1] for row in cur.fetchall()]
        if "mb_release_group_id" not in alb_cols:
            cur.execute("ALTER TABLE library_albums ADD COLUMN mb_release_group_id TEXT;")
        if "mb_release_id" not in alb_cols:
            cur.execute("ALTER TABLE library_albums ADD COLUMN mb_release_id TEXT;")
        if "genres" not in alb_cols:
            cur.execute("ALTER TABLE library_albums ADD COLUMN genres TEXT;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_albums_mb_rg ON library_albums(mb_release_group_id);")

        cur.execute("PRAGMA table_info(library_tracks);")
        trk_cols = [row[1] for row in cur.fetchall()]
        if "mb_recording_id" not in trk_cols:
            cur.execute("ALTER TABLE library_tracks ADD COLUMN mb_recording_id TEXT;")
        if "isrc" not in trk_cols:
            cur.execute("ALTER TABLE library_tracks ADD COLUMN isrc TEXT;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_tracks_mb_rec ON library_tracks(mb_recording_id);")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS library_collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                clean_name TEXT NOT NULL,
                summary TEXT,
                poster_url TEXT,
                monitored INTEGER NOT NULL DEFAULT 1,
                foreign_id TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_collections_clean_name ON library_collections(clean_name);")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS library_collection_albums (
                collection_id TEXT NOT NULL REFERENCES library_collections(id) ON DELETE CASCADE,
                album_id TEXT NOT NULL REFERENCES library_albums(id) ON DELETE CASCADE,
                order_index INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (collection_id, album_id)
            );
            """
        )

    def _migration_v21(self, cur: sqlite3.Cursor) -> None:
        cur.execute("PRAGMA table_info(media_management_settings);")
        mm_cols = [row[1] for row in cur.fetchall()]
        if "prefer_local_artwork" not in mm_cols:
            cur.execute(
                "ALTER TABLE media_management_settings ADD COLUMN prefer_local_artwork INTEGER NOT NULL DEFAULT 1;"
            )
        cur.execute(
            "UPDATE media_management_settings SET mb_mirror_url = 'https://api.brainzmash.cc' WHERE mb_mirror_url IN ('https://api.brainzmash.org', 'https://musicbrainz.org');"
        )

    def _migration_v22(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                source TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_system_events_created_at ON system_events (created_at DESC);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_system_events_type ON system_events (event_type);"
        )

    def _migration_v23(self, cur: sqlite3.Cursor) -> None:
        cur.execute("PRAGMA table_info(library_artists);")
        art_cols = [row[1] for row in cur.fetchall()]
        if "monitor_option" not in art_cols:
            cur.execute("ALTER TABLE library_artists ADD COLUMN monitor_option TEXT NOT NULL DEFAULT 'all';")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_artists_monitored ON library_artists(monitored);")

    def _migration_v24(self, cur: sqlite3.Cursor) -> None:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS plex_playlist_registry (
                plex_user TEXT NOT NULL,
                rating_key TEXT NOT NULL,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                owner TEXT NOT NULL,
                ignored INTEGER NOT NULL DEFAULT 0,
                trackseerr_playlist_id TEXT REFERENCES playlists(id) ON DELETE SET NULL,
                last_seen_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                PRIMARY KEY (plex_user, rating_key)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS plex_mix_snapshots (
                id TEXT PRIMARY KEY,
                plex_user TEXT NOT NULL,
                mix_key TEXT NOT NULL,
                mix_title TEXT NOT NULL,
                playlist_title TEXT NOT NULL,
                rating_key TEXT,
                auto_refresh INTEGER NOT NULL DEFAULT 0,
                last_refreshed_at TEXT,
                created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
                UNIQUE (plex_user, mix_key)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_plex_registry_adopted ON plex_playlist_registry(trackseerr_playlist_id);"
        )

    def _migration_v26(self, cur: sqlite3.Cursor) -> None:
        """Durable single-use nonces for signed gateway assertions (replay protection across restarts)."""
        cur.execute(
            "CREATE TABLE IF NOT EXISTS internal_nonces (nonce TEXT PRIMARY KEY, seen_at INTEGER NOT NULL)"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_internal_nonces_seen_at ON internal_nonces(seen_at)")

    def _migration_v27(self, cur: sqlite3.Cursor) -> None:
        """Local accounts, MFA, invites, tombstones, login throttling, per-user quotas and account settings."""
        cur.execute("PRAGMA table_info(users);")
        user_cols = {row[1] for row in cur.fetchall()}
        for col, ddl in (
            ("auth_type", "TEXT NOT NULL DEFAULT 'plex'"),
            ("password_hash", "TEXT"),
            ("password_changed_at", "TEXT"),
            ("disabled", "INTEGER NOT NULL DEFAULT 0"),
            ("sessions_revoked_at", "TEXT"),
            ("last_login_at", "TEXT"),
            ("totp_secret", "TEXT"),
            ("totp_last_counter", "INTEGER"),
            ("failed_logins", "INTEGER NOT NULL DEFAULT 0"),
            ("locked_until", "TEXT"),
            ("quota_tracks", "INTEGER"),
            ("quota_albums", "INTEGER"),
            ("quota_discographies", "INTEGER"),
            ("quota_window_days", "INTEGER"),
        ):
            if col not in user_cols:
                cur.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl};")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_invites (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                purpose TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_invites_user ON user_invites(user_id, purpose);")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_recovery_codes (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                code_hash TEXT NOT NULL,
                used_at TEXT,
                PRIMARY KEY (user_id, code_hash)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_tombstones (
                user_id TEXT PRIMARY KEY,
                deleted_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                deleted_by TEXT
            )
            """
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS login_attempts (key TEXT NOT NULL, attempted_at INTEGER NOT NULL)"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(key, attempted_at);")
        cur.execute("PRAGMA table_info(general_settings);")
        gs_cols = {row[1] for row in cur.fetchall()}
        for col, ddl in (
            ("require_mfa_local", "INTEGER NOT NULL DEFAULT 0"),
            ("default_quota_tracks", "INTEGER NOT NULL DEFAULT 25"),
            ("default_quota_albums", "INTEGER NOT NULL DEFAULT 10"),
            ("default_quota_discographies", "INTEGER NOT NULL DEFAULT 1"),
            ("default_quota_window_days", "INTEGER NOT NULL DEFAULT 7"),
        ):
            if col not in gs_cols:
                cur.execute(f"ALTER TABLE general_settings ADD COLUMN {col} {ddl};")

    def _migration_v29(self, cur: sqlite3.Cursor) -> None:
        """Index ``login_attempts.attempted_at`` so the periodic prune is a range scan, not a table scan."""
        cur.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_at ON login_attempts(attempted_at);")

    def _migration_v30(self, cur: sqlite3.Cursor) -> None:
        """DMZ ergonomics: role-change tracking, a stable instance id and a small key-value table.

        Everything is column/table-existence guarded so it is idempotent across role flips and re-runs.
        """
        cur.execute("INSERT OR IGNORE INTO general_settings (id, application_url) VALUES (1, '')")
        cur.execute("PRAGMA table_info(general_settings);")
        gs_cols = {row[1] for row in cur.fetchall()}
        for col in ("last_role", "instance_id", "role_change_notice"):
            if col not in gs_cols:
                cur.execute(f"ALTER TABLE general_settings ADD COLUMN {col} TEXT NOT NULL DEFAULT '';")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP))"
        )

    def _migration_v31(self, cur: sqlite3.Cursor) -> None:
        """Persist the per-profile "upgrade allowed" flag (default on: upgrades were previously ungated)."""
        cur.execute("PRAGMA table_info(quality_profiles);")
        qp_cols = {row[1] for row in cur.fetchall()}
        if "upgrade_allowed" not in qp_cols:
            cur.execute("ALTER TABLE quality_profiles ADD COLUMN upgrade_allowed INTEGER NOT NULL DEFAULT 1;")

    def _migration_v32(self, cur: sqlite3.Cursor) -> None:
        """Lidarr request front-end settings: monitor option and tag ids (``auto_search`` doubles as search-on-add)."""
        cur.execute("PRAGMA table_info(lidarr_settings);")
        cols = {row[1] for row in cur.fetchall()}
        if "monitor_option" not in cols:
            cur.execute("ALTER TABLE lidarr_settings ADD COLUMN monitor_option TEXT NOT NULL DEFAULT 'all';")
        if "tag_ids" not in cols:
            cur.execute("ALTER TABLE lidarr_settings ADD COLUMN tag_ids TEXT NOT NULL DEFAULT '[]';")

    def _migration_v33(self, cur: sqlite3.Cursor) -> None:
        """Activity + Wanted: stall tracking, release metadata on downloads, append-only download history."""
        cur.execute("PRAGMA table_info(active_downloads);")
        ad_cols = {row[1] for row in cur.fetchall()}
        for col in ("progress_updated_at", "indexer", "quality", "protocol"):
            if col not in ad_cols:
                cur.execute(f"ALTER TABLE active_downloads ADD COLUMN {col} TEXT;")
        cur.execute("PRAGMA table_info(library_tracks);")
        if "last_searched_at" not in {row[1] for row in cur.fetchall()}:
            cur.execute("ALTER TABLE library_tracks ADD COLUMN last_searched_at TEXT;")
        # Rows already in flight start their stall clock now, so an upgrade never flags them stalled on first read.
        # Only NULLs are touched, which keeps a re-run from resetting clocks the workers have since maintained.
        cur.execute(
            "UPDATE active_downloads SET progress_updated_at = CURRENT_TIMESTAMP "
            "WHERE progress_updated_at IS NULL AND status NOT IN ('failed', 'imported')"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS download_history (
                id TEXT PRIMARY KEY,
                event TEXT NOT NULL,
                download_id TEXT,
                request_id TEXT,
                track_id TEXT,
                album_id TEXT,
                item_type TEXT,
                artist TEXT,
                album TEXT,
                title TEXT,
                release_title TEXT,
                quality TEXT,
                indexer TEXT,
                protocol TEXT,
                client TEXT,
                info_hash TEXT,
                release_guid TEXT,
                message TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_download_history_created ON download_history(created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_download_history_event ON download_history(event, created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_download_history_download ON download_history(download_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_active_downloads_created ON active_downloads(created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_blocklist_created ON download_blocklist(created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_tracks_monitored ON library_tracks(monitored);")
        self._seed_download_history(cur)

    def _migration_v34(self, cur: sqlite3.Cursor) -> None:
        """Persisted, indexed sort keys for the virtualized library lists and the scrubber.

        ``sort_name`` (artists) / ``sort_title`` (albums, tracks) hold ``library_sort_key(name)``. Column-existence
        guarded and the backfill only touches rows still holding the empty default, so a re-run changes nothing.
        """
        for table, source, column in (
            ("library_artists", "name", "sort_name"),
            ("library_albums", "title", "sort_title"),
            ("library_tracks", "title", "sort_title"),
        ):
            cur.execute(f"PRAGMA table_info({table});")
            if column not in {row[1] for row in cur.fetchall()}:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT '';")
            pending = cur.execute(f"SELECT id, {source} FROM {table} WHERE {column} = ''").fetchall()
            for start in range(0, len(pending), 1000):
                cur.executemany(
                    f"UPDATE {table} SET {column} = ? WHERE id = ?",
                    [(library_sort_key(name), row_id) for row_id, name in pending[start : start + 1000]],
                )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_artists_sort_name ON library_artists(sort_name, id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_albums_sort_title ON library_albums(sort_title, id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_tracks_sort_title ON library_tracks(sort_title, id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_artists_created ON library_artists(created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_albums_created ON library_albums(created_at);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_tracks_created ON library_tracks(created_at);")

    def _migration_v35(self, cur: sqlite3.Cursor) -> None:
        """Persisted folded search columns for the native library lists.

        ``search_text`` is ``fold_search_text(name/title)`` and ``search_clean`` is ``fold_search_text(clean_name/
        clean_title)``, so list search is a plain ``LIKE`` instead of a per-row Python UDF. Related names (an
        album's artist, a track's album and artist) are deliberately NOT denormalized: the list queries join the
        related row's own columns, so renaming an artist is reflected without touching albums or tracks. Column
        guarded; the backfill only touches rows whose folded columns are still empty, so a re-run changes nothing.
        """
        for table, raw, clean in (
            ("library_artists", "name", "clean_name"),
            ("library_albums", "title", "clean_title"),
            ("library_tracks", "title", "clean_title"),
        ):
            cur.execute(f"PRAGMA table_info({table});")
            existing = {row[1] for row in cur.fetchall()}
            for column in ("search_text", "search_clean"):
                if column not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT '';")
            pending = cur.execute(
                f"SELECT id, {raw}, {clean} FROM {table} WHERE search_text = '' AND search_clean = ''"
            ).fetchall()
            for start in range(0, len(pending), 1000):
                cur.executemany(
                    f"UPDATE {table} SET search_text = ?, search_clean = ? WHERE id = ?",
                    [
                        (fold_search_text(name or ""), fold_search_text(cleaned or ""), row_id)
                        for row_id, name, cleaned in pending[start : start + 1000]
                    ],
                )

    def _migration_v36(self, cur: sqlite3.Cursor) -> None:
        """Default monitor options for newly scanned (``existing``) and newly added (``all``) native artists."""
        cur.execute("PRAGMA table_info(media_management_settings);")
        cols = {row[1] for row in cur.fetchall()}
        if "scan_monitor_option" not in cols:
            cur.execute(
                "ALTER TABLE media_management_settings ADD COLUMN scan_monitor_option TEXT NOT NULL DEFAULT 'existing';"
            )
        if "add_monitor_option" not in cols:
            cur.execute(
                "ALTER TABLE media_management_settings ADD COLUMN add_monitor_option TEXT NOT NULL DEFAULT 'all';"
            )

    def _migration_v44(self, cur: sqlite3.Cursor) -> None:
        """Native monitoring defaults to ``existing`` (track-level: monitor exactly the tracks you have files for).

        SQLite cannot change a column DEFAULT without a table rebuild, so the column defaults stay ``'all'`` and the
        code paths supply ``DEFAULT_MONITOR_OPTION`` for new rows. This migration only seeds the saved add option on a
        fresh database; an existing install's saved ``add_monitor_option`` and every artist's option are untouched.
        """
        if not getattr(self, "_fresh_install", False):
            return
        cur.execute("UPDATE media_management_settings SET add_monitor_option = ? WHERE id = 1", (DEFAULT_MONITOR_OPTION,))

    def _migration_v45(self, cur: sqlite3.Cursor) -> None:
        """Optional native release profiles: they only shape automatic monitoring, never hide releases.

        Adds ``native_release_profiles`` (seeded with three editable presets), ``library_albums.secondary_types``
        (JSON list, NULL = unknown, treated as studio), ``library_artists.release_profile_id`` (NULL = no profile)
        and ``media_management_settings.add_release_profile_id`` (NULL = new artists get no profile).
        """
        # Historical migration: keeps the pre-v48 names. A database that already carries the v48 names (migrations
        # re-run over an upgraded schema) has nothing to add, and must not regrow the old table or columns.
        cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'native_metadata_profiles'")
        if cur.fetchone():
            return
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS native_release_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                primary_types TEXT NOT NULL,
                secondary_types TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        for table, column, ddl in (
            ("library_albums", "secondary_types", "TEXT"),
            (
                "library_artists",
                "release_profile_id",
                "INTEGER REFERENCES native_release_profiles(id) ON DELETE SET NULL",
            ),
            ("media_management_settings", "add_release_profile_id", "INTEGER"),
        ):
            cur.execute(f"PRAGMA table_info({table});")
            if column not in {row[1] for row in cur.fetchall()}:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl};")
        presets = (
            ("Studio Albums", ["album"], ["studio"]),
            ("Studio Albums, EPs & Singles", ["album", "ep", "single"], ["studio"]),
            ("Everything", list(RELEASE_PRIMARY_TYPES), list(RELEASE_SECONDARY_TYPES)),
        )
        for name, primary, secondary in presets:
            cur.execute(
                "INSERT OR IGNORE INTO native_release_profiles (name, primary_types, secondary_types) VALUES (?, ?, ?)",
                (name, json.dumps(primary), json.dumps(secondary)),
            )

    def _migration_v46(self, cur: sqlite3.Cursor) -> None:
        """``library_artists.pending_profile_recompute``: set when an artist was added with a metadata profile before
        MusicBrainz secondary types were known; the first refresh that persists them recomputes monitoring once."""
        cur.execute("PRAGMA table_info(library_artists);")
        if "pending_profile_recompute" not in {row[1] for row in cur.fetchall()}:
            cur.execute(
                "ALTER TABLE library_artists ADD COLUMN pending_profile_recompute INTEGER NOT NULL DEFAULT 0;"
            )

    def _migration_v47(self, cur: sqlite3.Cursor) -> None:
        """``art_version`` on library artists and albums: a token (local art file mtime + size) that rides in the art URL
        as ``?v=`` so the browser can cache it as immutable. NULL until the art is first seen or backfilled."""
        for table in ("library_artists", "library_albums"):
            cur.execute(f"PRAGMA table_info({table});")
            if "art_version" not in {row[1] for row in cur.fetchall()}:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN art_version TEXT;")

    def _migration_v48(self, cur: sqlite3.Cursor) -> None:
        """Renames the native "release profiles" feature to "metadata profiles" (Lidarr's name for the release-type
        filter), freeing "release profile" for a term-based feature. Data, ids and the ON DELETE SET NULL foreign key
        are preserved: SQLite (>= 3.25, ``legacy_alter_table`` OFF) rewrites the FK reference on table rename."""
        cur.execute("PRAGMA table_info(media_management_settings);")
        if "add_release_profile_id" in {row[1] for row in cur.fetchall()}:
            cur.execute(
                "ALTER TABLE media_management_settings RENAME COLUMN add_release_profile_id TO add_metadata_profile_id;"
            )
        cur.execute("PRAGMA table_info(library_artists);")
        if "release_profile_id" in {row[1] for row in cur.fetchall()}:
            cur.execute("ALTER TABLE library_artists RENAME COLUMN release_profile_id TO metadata_profile_id;")
        cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'native_release_profiles'")
        if cur.fetchone():
            cur.execute("ALTER TABLE native_release_profiles RENAME TO native_metadata_profiles;")

    def _migration_v43(self, cur: sqlite3.Cursor) -> None:
        """Collapse duplicate ``library_tracks`` rows (same album + foreign id, or same album/disc/number/title).

        Refresh paths matched existing tracks by title only, so a re-hydration that carried a foreign id or a
        differently-cased title inserted a second row. File links survive on the kept row.
        """
        merged = self._dedupe_library_tracks(cur)
        if merged:
            logger.info("[boot] migrations: merged %d duplicate library track rows", merged)

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

    def _migration_v42(self, cur: sqlite3.Cursor) -> None:
        """Composite ``library_files(track_id, cutoff_met, id)`` index.

        The Wanted "cutoff unmet" query resolves ``MIN(id) ... WHERE track_id = t.id AND cutoff_met = 0`` per track.
        With only the single-column ``cutoff_met`` index SQLite picked that low-selectivity index and scanned every
        unmet file for every track (quadratic); this index makes each lookup a point seek.
        """
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_lib_files_track_cutoff ON library_files(track_id, cutoff_met, id);"
        )

    def _migration_v41(self, cur: sqlite3.Cursor) -> None:
        """``media_server_settings.credentials_type``: which server type the saved url / username / password / api_key
        belong to. ``type`` is the ACTIVE choice, which can be ``none`` while the credentials are kept, so a masked
        secret can only be resolved for the type it was saved for."""
        cur.execute("PRAGMA table_info(media_server_settings);")
        if "credentials_type" not in {row[1] for row in cur.fetchall()}:
            cur.execute("ALTER TABLE media_server_settings ADD COLUMN credentials_type TEXT NOT NULL DEFAULT '';")
        cur.execute(
            "UPDATE media_server_settings SET credentials_type = type WHERE credentials_type = '' "
            "AND type IN ('subsonic', 'jellyfin')"
        )

    def _migration_v40(self, cur: sqlite3.Cursor) -> None:
        """``media_server_settings``: the media server chosen on the Settings page (singleton row; Plex stays env-only).

        ``password`` / ``api_key`` are stored the same way ``lidarr_settings.api_key`` is; they are never returned by
        the API (masked) and a gateway database must not hold them (see ``role_guard``).
        """
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS media_server_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                type TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                password TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    def _migration_v39(self, cur: sqlite3.Cursor) -> None:
        """Recompute ``clean_name`` / ``clean_title`` (and the folded ``search_clean``) for rows containing '_'.

        ``clean_library_name`` now treats '_' as whitespace (iTunes' stand-in for characters illegal on Windows), so
        stored keys such as ``daft punk_ pharrell williams`` must be rebuilt to stay findable. Idempotent: only rows
        whose stored key still holds '_' are touched, and a recompute never reintroduces one.
        """
        for table, raw, clean in (
            ("library_artists", "name", "clean_name"),
            ("library_albums", "title", "clean_title"),
            ("library_tracks", "title", "clean_title"),
        ):
            cur.execute(f"PRAGMA table_info({table});")
            if not {raw, clean, "search_clean"} <= {row[1] for row in cur.fetchall()}:
                continue
            pending = cur.execute(
                f"SELECT id, {raw} FROM {table} WHERE {clean} LIKE '%\\_%' ESCAPE '\\'"
            ).fetchall()
            cur.executemany(
                f"UPDATE {table} SET {clean} = ?, search_clean = ? WHERE id = ?",
                [
                    (clean_library_name(name or ""), fold_search_text(clean_library_name(name or "")), row_id)
                    for row_id, name in pending
                ],
            )

    def _migration_v38(self, cur: sqlite3.Cursor) -> None:
        """Request outcomes (why a request is stuck) and retry scheduling for Lidarr dispatch.

        - ``music_requests.status_reason`` / ``status_message``: why a request is stuck; the status is unchanged.
        - ``lidarr_settings.prefer_singles``: monitor a song's single rather than the album it appears on (default on).
        - ``missing_tracks.attempts`` / ``next_attempt_at``: back-off bookkeeping so a failing item is retried on a
          schedule instead of every trickle interval.
        """
        for table, columns in (
            (
                "music_requests",
                (
                    ("status_reason", "TEXT"),
                    ("status_message", "TEXT"),
                ),
            ),
            ("missing_tracks", (("attempts", "INTEGER NOT NULL DEFAULT 0"), ("next_attempt_at", "TEXT"))),
        ):
            cur.execute(f"PRAGMA table_info({table});")
            have = {row[1] for row in cur.fetchall()}
            for name, decl in columns:
                if name not in have:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl};")
        cur.execute("PRAGMA table_info(lidarr_settings);")
        if "prefer_singles" not in {row[1] for row in cur.fetchall()}:
            cur.execute("ALTER TABLE lidarr_settings ADD COLUMN prefer_singles INTEGER NOT NULL DEFAULT 1;")

    def _migration_v65(self, cur: sqlite3.Cursor) -> None:
        """Listening playlists: the source (kind/ref) of a Last.fm / ListenBrainz playlist and its auto-request opt-in."""
        cur.execute("PRAGMA table_info(playlists);")
        have = {row[1] for row in cur.fetchall()}
        if "source_kind" not in have:
            cur.execute("ALTER TABLE playlists ADD COLUMN source_kind TEXT;")
        if "source_ref" not in have:
            cur.execute("ALTER TABLE playlists ADD COLUMN source_ref TEXT;")
        if "auto_request" not in have:
            cur.execute("ALTER TABLE playlists ADD COLUMN auto_request INTEGER NOT NULL DEFAULT 0;")

    def _migration_v66(self, cur: sqlite3.Cursor) -> None:
        """Request retry schedule: attempts so far and when a stuck Lidarr request is next re-sent."""
        cur.execute("PRAGMA table_info(music_requests);")
        have = {row[1] for row in cur.fetchall()}
        if "retry_attempts" not in have:
            cur.execute("ALTER TABLE music_requests ADD COLUMN retry_attempts INTEGER NOT NULL DEFAULT 0;")
        if "next_attempt_at" not in have:
            cur.execute("ALTER TABLE music_requests ADD COLUMN next_attempt_at TEXT;")

    def _migration_v67(self, cur: sqlite3.Cursor) -> None:
        """Persisted background task run history (task manager): one row per execution of a registered task."""
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                trigger TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                message TEXT,
                duration_ms INTEGER
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_task_started ON task_runs(task_id, started_at)")

    def _migration_v68(self, cur: sqlite3.Cursor) -> None:
        """Track last seen changelog version per user."""
        cur.execute("PRAGMA table_info(users);")
        have = {row[1] for row in cur.fetchall()}
        if "last_seen_changelog_version" not in have:
            cur.execute("ALTER TABLE users ADD COLUMN last_seen_changelog_version TEXT;")

    def _migration_v69(self, cur: sqlite3.Cursor) -> None:
        """Per-user notifications, in-app inbox, and Web Push subscriptions."""
        cur.execute("PRAGMA table_info(notification_channels);")
        have_channels = {row[1] for row in cur.fetchall()}
        if "owner_user_id" not in have_channels:
            cur.execute("ALTER TABLE notification_channels ADD COLUMN owner_user_id TEXT;")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_notification_channels_owner ON notification_channels(owner_user_id);"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_notifications (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                event TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                link TEXT,
                created_at TEXT NOT NULL,
                read_at TEXT
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_notifications_user_read ON user_notifications(user_id, read_at);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_notifications_user_created ON user_notifications(user_id, created_at);"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS web_push_subscriptions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                user_agent TEXT,
                created_at TEXT NOT NULL,
                last_success_at TEXT,
                failure_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_push_subs_user ON web_push_subscriptions(user_id);"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_notification_prefs (
                user_id TEXT NOT NULL,
                event TEXT NOT NULL,
                in_app INTEGER NOT NULL DEFAULT 1,
                push INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, event)
            );
            """
        )

        cur.execute("PRAGMA table_info(general_settings);")
        have_general = {row[1] for row in cur.fetchall()}
        if "vapid_public_key" not in have_general:
            cur.execute("ALTER TABLE general_settings ADD COLUMN vapid_public_key TEXT;")
        if "vapid_private_key" not in have_general:
            cur.execute("ALTER TABLE general_settings ADD COLUMN vapid_private_key TEXT;")
        if "vapid_sub" not in have_general:
            cur.execute("ALTER TABLE general_settings ADD COLUMN vapid_sub TEXT;")

    def _migration_v70(self, cur: sqlite3.Cursor) -> None:
        """Update check: enable toggle and cached latest GitHub release result."""
        cur.execute("PRAGMA table_info(general_settings);")
        have_general = {row[1] for row in cur.fetchall()}
        columns = (
            ("update_check_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("update_latest_version", "TEXT"),
            ("update_release_url", "TEXT"),
            ("update_published_at", "TEXT"),
            ("update_checked_at", "TEXT"),
            ("update_error", "TEXT"),
        )
        for name, decl in columns:
            if name not in have_general:
                cur.execute(f"ALTER TABLE general_settings ADD COLUMN {name} {decl};")

    def _migration_v71(self, cur: sqlite3.Cursor) -> None:
        """MusicBrainz persistent metadata store and ID redirects."""
        cur.execute(
            """CREATE TABLE IF NOT EXISTS mb_metadata_cache (
                cache_key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );"""
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_mb_cache_expires ON mb_metadata_cache(expires_at);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_mb_cache_kind ON mb_metadata_cache(kind);"
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS mb_id_redirects (
                old_id TEXT PRIMARY KEY,
                new_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                seen_at TEXT NOT NULL
            );"""
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_mb_redirects_new ON mb_id_redirects(new_id);"
        )

    def _migration_v37(self, cur: sqlite3.Cursor) -> None:
        """Import lists, per-playlist monitor mode and the missing-track "already applied" marker."""
        cur.execute("PRAGMA table_info(playlists);")
        if "monitor_mode" not in {row[1] for row in cur.fetchall()}:
            cur.execute("ALTER TABLE playlists ADD COLUMN monitor_mode TEXT NOT NULL DEFAULT 'track';")
        cur.execute("PRAGMA table_info(missing_tracks);")
        if "list_applied_at" not in {row[1] for row in cur.fetchall()}:
            cur.execute("ALTER TABLE missing_tracks ADD COLUMN list_applied_at TEXT;")
        cur.execute("PRAGMA table_info(missing_tracks);")
        if "artist_added_by_item" not in {row[1] for row in cur.fetchall()}:
            cur.execute("ALTER TABLE missing_tracks ADD COLUMN artist_added_by_item INTEGER NOT NULL DEFAULT 0;")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS import_lists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                provider TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                monitor_mode TEXT NOT NULL DEFAULT 'track',
                artist_monitor_option TEXT,
                quality_profile_id TEXT,
                sync_interval_minutes INTEGER NOT NULL DEFAULT 1440,
                last_synced_at TEXT,
                last_status TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS import_list_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id TEXT NOT NULL REFERENCES import_lists(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                external_key TEXT NOT NULL,
                mbid TEXT,
                artist_mbid TEXT,
                artist_name TEXT NOT NULL DEFAULT '',
                album_title TEXT NOT NULL DEFAULT '',
                track_title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                applied_level TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                artist_added_by_item INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                last_seen_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                UNIQUE (list_id, kind, external_key)
            )
            """
        )
        cur.execute("PRAGMA table_info(import_list_items);")
        item_cols = {row[1] for row in cur.fetchall()}
        for col, ddl in (
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("next_attempt_at", "TEXT"),
            ("artist_added_by_item", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in item_cols:
                cur.execute(f"ALTER TABLE import_list_items ADD COLUMN {col} {ddl};")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_import_list_items_list_status ON import_list_items(list_id, status)")

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

    def _migration_v28(self, cur: sqlite3.Cursor) -> None:
        """Discography batch markers, legacy single-quota migration and per-type auto-approve bits.

        - ``music_requests.batch_id`` / ``batch_kind`` mark the albums of a discography batch so they count as one
          discography unit and no album units.
        - ``users.request_limit_quota`` (one cap across all types) becomes the per-user track and album overrides;
          ``request_limit_days`` (default 7) becomes ``quota_window_days`` when it was customised. Existing
          new-style overrides are never overwritten. The legacy columns stay in place, unread.
        - ``USER_REQUEST_QUOTA`` (legacy env, one cap across all types) seeds the track and album defaults when it
          is set to something other than the old default of 25 and the defaults are still untouched.
        - Bit 4 used to approve every request type; it now means tracks only, so holders also get bit 8 (albums)
          and bit 64 (discographies) and lose nothing.
        """
        cur.execute("PRAGMA table_info(music_requests);")
        req_cols = {row[1] for row in cur.fetchall()}
        if "batch_id" not in req_cols:
            cur.execute("ALTER TABLE music_requests ADD COLUMN batch_id TEXT;")
        if "batch_kind" not in req_cols:
            cur.execute("ALTER TABLE music_requests ADD COLUMN batch_kind TEXT;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_requests_batch ON music_requests(user_id, batch_kind, batch_id);")

        cur.execute("PRAGMA table_info(users);")
        user_cols = {row[1] for row in cur.fetchall()}
        if "request_limit_quota" in user_cols:
            cur.execute(
                """
                UPDATE users SET quota_tracks = request_limit_quota
                WHERE request_limit_quota IS NOT NULL AND quota_tracks IS NULL
                """
            )
            cur.execute(
                """
                UPDATE users SET quota_albums = request_limit_quota
                WHERE request_limit_quota IS NOT NULL AND quota_albums IS NULL
                """
            )
        if "request_limit_days" in user_cols:
            cur.execute(
                """
                UPDATE users SET quota_window_days = request_limit_days
                WHERE request_limit_days IS NOT NULL AND request_limit_days > 0 AND request_limit_days != 7
                  AND quota_window_days IS NULL
                """
            )

        cur.execute("INSERT OR IGNORE INTO general_settings (id, application_url) VALUES (1, '')")
        legacy_env = (os.getenv("USER_REQUEST_QUOTA") or "").strip()
        if legacy_env.isdigit() and int(legacy_env) not in (0, 25):
            cur.execute(
                """
                UPDATE general_settings SET default_quota_tracks = ?, default_quota_albums = ?
                WHERE id = 1 AND default_quota_tracks = 25 AND default_quota_albums = 10
                """,
                (int(legacy_env), int(legacy_env)),
            )

        cur.execute(
            "UPDATE users SET permissions = permissions | ? WHERE (permissions & ?) != 0",
            (int(UserPermission.AUTO_APPROVE_ALBUM) | int(UserPermission.AUTO_APPROVE_DISCOGRAPHY), int(UserPermission.AUTO_APPROVE)),
        )


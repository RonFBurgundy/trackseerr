CREATE TABLE artist_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                library_artist_id TEXT,
                discovery_id TEXT,
                mbid TEXT,
                name TEXT,
                confidence TEXT NOT NULL DEFAULT 'none' CHECK (confidence IN ('mbid', 'name', 'none')),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
CREATE TABLE custom_formats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                include_in_rename INTEGER NOT NULL DEFAULT 0,
                specifications_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
CREATE TABLE delay_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                order_idx INTEGER NOT NULL DEFAULT 0,
                preferred_protocol TEXT NOT NULL DEFAULT 'usenet',
                usenet_delay_min INTEGER NOT NULL DEFAULT 0,
                torrent_delay_min INTEGER NOT NULL DEFAULT 0,
                soulseek_delay_min INTEGER NOT NULL DEFAULT 0,
                bypass_if_highest_quality INTEGER NOT NULL DEFAULT 1,
                bypass_if_above_score INTEGER,
                tags_json TEXT NOT NULL DEFAULT '[]',
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
CREATE TABLE download_blocklist (
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
CREATE TABLE download_clients (
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
CREATE TABLE download_history (
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
            , "trigger" TEXT, "trigger_ref" TEXT, "trigger_label" TEXT);
CREATE TABLE general_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                application_url TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            , api_key TEXT NOT NULL DEFAULT '', lastfm_api_key TEXT NOT NULL DEFAULT '', lastfm_api_secret TEXT NOT NULL DEFAULT '', plex_webhook_secret TEXT NOT NULL DEFAULT '', plex_history_poll_minutes INTEGER NOT NULL DEFAULT 15, require_mfa_local INTEGER NOT NULL DEFAULT 0, default_quota_tracks INTEGER NOT NULL DEFAULT 25, default_quota_albums INTEGER NOT NULL DEFAULT 10, default_quota_discographies INTEGER NOT NULL DEFAULT 1, default_quota_window_days INTEGER NOT NULL DEFAULT 7, last_role TEXT NOT NULL DEFAULT '', instance_id TEXT NOT NULL DEFAULT '', role_change_notice TEXT NOT NULL DEFAULT '', vapid_public_key TEXT, vapid_private_key TEXT, vapid_sub TEXT, update_check_enabled INTEGER NOT NULL DEFAULT 1, update_latest_version TEXT, update_release_url TEXT, update_published_at TEXT, update_checked_at TEXT, update_error TEXT);
CREATE TABLE import_lists (
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
            , tags_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE indexers (
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
            , seed_ratio REAL, seed_time_minutes INTEGER, discography_seed_time_minutes INTEGER, minimum_seeders INTEGER);
CREATE TABLE internal_nonces (nonce TEXT PRIMARY KEY, seen_at INTEGER NOT NULL);
CREATE TABLE item_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                artist_id TEXT,
                album_id TEXT,
                track_id TEXT,
                artist_name TEXT NOT NULL DEFAULT '',
                album_title TEXT NOT NULL DEFAULT '',
                track_title TEXT NOT NULL DEFAULT '',
                "trigger" TEXT,
                trigger_ref TEXT,
                trigger_label TEXT,
                actor_user_id TEXT,
                request_id TEXT,
                download_id TEXT,
                message TEXT NOT NULL DEFAULT '',
                details_json TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
CREATE TABLE kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP));
CREATE TABLE library_collections (
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
CREATE TABLE library_health_dismissals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL CHECK (scope IN ('file', 'folder')),
                path TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                UNIQUE (scope, path)
            );
CREATE TABLE "library_health_findings" (
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
CREATE TABLE library_health_runs (
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
CREATE TABLE lidarr_settings (
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
            , monitor_option TEXT NOT NULL DEFAULT 'all', tag_ids TEXT NOT NULL DEFAULT '[]', prefer_singles INTEGER NOT NULL DEFAULT 1);
CREATE TABLE login_attempts (key TEXT NOT NULL, attempted_at INTEGER NOT NULL);
CREATE TABLE mb_id_redirects (
                old_id TEXT PRIMARY KEY,
                new_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                seen_at TEXT NOT NULL
            );
CREATE TABLE mb_metadata_cache (
                cache_key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
CREATE TABLE media_management_settings (
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
            , staging_folder_path TEXT NOT NULL DEFAULT '', import_mode TEXT NOT NULL DEFAULT 'move', write_audio_tags INTEGER NOT NULL DEFAULT 1, embed_artwork INTEGER NOT NULL DEFAULT 1, save_cover_art_file INTEGER NOT NULL DEFAULT 1, delete_completed_transfers INTEGER NOT NULL DEFAULT 0, enable_quality_upgrades INTEGER NOT NULL DEFAULT 1, library_mode TEXT NOT NULL DEFAULT 'native', seed_ratio_limit REAL, seed_time_limit_minutes INTEGER, enrich_mbids INTEGER NOT NULL DEFAULT 1, acoustid_api_key TEXT, mb_mirror_url TEXT NOT NULL DEFAULT 'https://api.brainzmash.org', prefer_local_artwork INTEGER NOT NULL DEFAULT 1, scan_monitor_option TEXT NOT NULL DEFAULT 'existing', add_monitor_option TEXT NOT NULL DEFAULT 'all', add_metadata_profile_id INTEGER, import_bitrate_check TEXT NOT NULL DEFAULT 'warn', fingerprint_on_weak_match INTEGER NOT NULL DEFAULT 0, torrent_hardlink_tags TEXT NOT NULL DEFAULT 'copy_and_tag', seed_complete_action TEXT NOT NULL DEFAULT 'remove', recycle_bin_path TEXT NOT NULL DEFAULT '', recycle_bin_cleanup_days INTEGER NOT NULL DEFAULT 30, recycle_bin_permanent_delete INTEGER NOT NULL DEFAULT 0, quarantine_folder_path TEXT NOT NULL DEFAULT '', multi_disc_track_format TEXT NOT NULL DEFAULT '');
CREATE TABLE media_server_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                type TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                password TEXT NOT NULL DEFAULT '',
                api_key TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            , credentials_type TEXT NOT NULL DEFAULT '', path_mapping_json TEXT NOT NULL DEFAULT '');
CREATE TABLE "native_metadata_profiles" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                primary_types TEXT NOT NULL,
                secondary_types TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
CREATE TABLE notification_channels (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                channel_type TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                config_json TEXT NOT NULL DEFAULT '{}',
                events_json TEXT NOT NULL DEFAULT '["request_created","request_approved","request_rejected","download_started","item_available","download_failed"]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            , owner_user_id TEXT);
CREATE TABLE pending_releases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                artist_name TEXT NOT NULL DEFAULT '',
                album TEXT,
                item_type TEXT NOT NULL DEFAULT 'track',
                album_id TEXT,
                track_id TEXT,
                request_id TEXT,
                protocol TEXT NOT NULL DEFAULT '',
                quality TEXT,
                format_score INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                rank_json TEXT NOT NULL DEFAULT '[]',
                delay_profile_id INTEGER,
                reason TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL,
                release_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            , attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT);
CREATE TABLE quality_definitions (
                quality TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                min_kbps REAL,
                preferred_kbps REAL,
                max_kbps REAL,
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
CREATE TABLE quality_profiles (
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
            , custom_formats_json TEXT, min_score INTEGER, upgrade_allowed INTEGER NOT NULL DEFAULT 1, format_items_json TEXT NOT NULL DEFAULT '[]', min_format_score INTEGER NOT NULL DEFAULT 0, cutoff_format_score INTEGER NOT NULL DEFAULT 0, min_upgrade_format_score INTEGER NOT NULL DEFAULT 1);
CREATE TABLE release_profiles (
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
            );
CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
                );
CREATE TABLE scrobble_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
CREATE TABLE system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                source TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
CREATE TABLE tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                trigger TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                message TEXT,
                duration_ms INTEGER
            );
CREATE TABLE user_notification_prefs (
                user_id TEXT NOT NULL,
                event TEXT NOT NULL,
                in_app INTEGER NOT NULL DEFAULT 1,
                push INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, event)
            );
CREATE TABLE user_notifications (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                event TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                link TEXT,
                created_at TEXT NOT NULL,
                read_at TEXT
            );
CREATE TABLE user_tombstones (
                user_id TEXT PRIMARY KEY,
                deleted_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                deleted_by TEXT
            );
CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                email TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            , permissions INTEGER NOT NULL DEFAULT 34, request_limit_quota INTEGER, request_limit_days INTEGER DEFAULT 7, auth_type TEXT NOT NULL DEFAULT 'plex', password_hash TEXT, password_changed_at TEXT, disabled INTEGER NOT NULL DEFAULT 0, sessions_revoked_at TEXT, last_login_at TEXT, totp_secret TEXT, totp_last_counter INTEGER, failed_logins INTEGER NOT NULL DEFAULT 0, locked_until TEXT, quota_tracks INTEGER, quota_albums INTEGER, quota_discographies INTEGER, quota_window_days INTEGER, last_seen_changelog_version TEXT);
CREATE TABLE web_push_subscriptions (
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
CREATE TABLE import_list_items (
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
            );
CREATE TABLE library_artists (
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
            , mbid TEXT, image_url TEXT, banner_url TEXT, bio TEXT, genres TEXT, country TEXT, monitor_option TEXT NOT NULL DEFAULT 'all', sort_name TEXT NOT NULL DEFAULT '', search_text TEXT NOT NULL DEFAULT '', search_clean TEXT NOT NULL DEFAULT '', metadata_profile_id INTEGER REFERENCES "native_metadata_profiles"(id) ON DELETE SET NULL, pending_profile_recompute INTEGER NOT NULL DEFAULT 0, art_version TEXT, artist_type TEXT, member_count INTEGER, begin_year INTEGER, end_year INTEGER, popularity INTEGER);
CREATE TABLE lastfm_auth_states (
                state TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                forward_url TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
CREATE TABLE match_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_title TEXT NOT NULL,
                source_artist TEXT NOT NULL,
                plex_rating_key TEXT NOT NULL,
                plex_title TEXT NOT NULL,
                plex_artist TEXT NOT NULL,
                created_by TEXT REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                UNIQUE(source_title, source_artist)
            );
CREATE TABLE music_requests (
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
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, quality_profile_id TEXT REFERENCES quality_profiles(id), current_quality TEXT, cutoff_met INTEGER NOT NULL DEFAULT 1, batch_id TEXT, batch_kind TEXT, status_reason TEXT, status_message TEXT, "trigger" TEXT, "trigger_ref" TEXT, "trigger_label" TEXT, retry_attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
CREATE TABLE playlists (
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
            , creator_id TEXT REFERENCES users(id), tracks_json TEXT, monitor_mode TEXT NOT NULL DEFAULT 'track', source_kind TEXT, source_ref TEXT, auto_request INTEGER NOT NULL DEFAULT 0);
CREATE TABLE plex_mix_snapshots (
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
            );
CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
                data TEXT NOT NULL DEFAULT '{}',
                expires_at TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
CREATE TABLE tailored_mix_configs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                mix_type TEXT NOT NULL,
                name TEXT NOT NULL,
                seed_artist TEXT,
                track_count INTEGER NOT NULL DEFAULT 30,
                discovery_ratio REAL NOT NULL DEFAULT 0.7,
                seed_window_days INTEGER NOT NULL DEFAULT 14,
                excluded_genres_json TEXT NOT NULL DEFAULT '[]',
                auto_acquire_missing INTEGER NOT NULL DEFAULT 0,
                max_weekly_acquisitions INTEGER NOT NULL DEFAULT 10,
                quality_profile_id TEXT REFERENCES quality_profiles(id) ON DELETE SET NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_generated_at TEXT,
                last_result_json TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
CREATE TABLE user_invites (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                purpose TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
CREATE TABLE user_listens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                artist TEXT NOT NULL,
                title TEXT NOT NULL,
                album TEXT,
                rating_key TEXT,
                duration_ms INTEGER,
                played_at TEXT NOT NULL,
                source TEXT NOT NULL,
                lastfm_status TEXT NOT NULL DEFAULT 'skipped',
                listenbrainz_status TEXT NOT NULL DEFAULT 'skipped',
                forward_attempts INTEGER NOT NULL DEFAULT 0,
                last_forward_error TEXT
            );
CREATE TABLE user_recovery_codes (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                code_hash TEXT NOT NULL,
                used_at TEXT,
                PRIMARY KEY (user_id, code_hash)
            );
CREATE TABLE user_scrobble_configs (
                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                scrobbling_enabled INTEGER NOT NULL DEFAULT 1,
                lastfm_username TEXT,
                lastfm_session_key TEXT,
                listenbrainz_token TEXT,
                listenbrainz_username TEXT,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
CREATE TABLE artist_tags (
                artist_id TEXT NOT NULL REFERENCES library_artists(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (artist_id, tag_id)
            );
CREATE TABLE library_albums (
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
            , mb_release_group_id TEXT, mb_release_id TEXT, genres TEXT, sort_title TEXT NOT NULL DEFAULT '', search_text TEXT NOT NULL DEFAULT '', search_clean TEXT NOT NULL DEFAULT '', secondary_types TEXT, art_version TEXT);
CREATE TABLE media_issues (
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
            , resolved_at TEXT, resolved_by TEXT, album_id TEXT, track_id TEXT, discovery_id TEXT, item_type TEXT, reporter_seen_at TEXT, last_activity_at TEXT, last_staff_activity_at TEXT);
CREATE TABLE missing_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                album TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            , lidarr_status TEXT NOT NULL DEFAULT 'unmonitored', list_applied_at TEXT, artist_added_by_item INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT);
CREATE TABLE playlist_targets (
                playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                PRIMARY KEY (playlist_id, user_id)
            );
CREATE TABLE plex_playlist_registry (
                plex_user TEXT NOT NULL,
                rating_key TEXT NOT NULL,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                owner TEXT NOT NULL,
                ignored INTEGER NOT NULL DEFAULT 0,
                trackseerr_playlist_id TEXT REFERENCES playlists(id) ON DELETE SET NULL,
                last_seen_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                PRIMARY KEY (plex_user, rating_key)
            );
CREATE TABLE mix_acquisitions (
                mix_id TEXT NOT NULL REFERENCES tailored_mix_configs(id) ON DELETE CASCADE,
                request_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                PRIMARY KEY (mix_id, request_id)
            );
CREATE TABLE library_collection_albums (
                collection_id TEXT NOT NULL REFERENCES library_collections(id) ON DELETE CASCADE,
                album_id TEXT NOT NULL REFERENCES library_albums(id) ON DELETE CASCADE,
                order_index INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (collection_id, album_id)
            );
CREATE TABLE library_tracks (
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
            , mb_recording_id TEXT, isrc TEXT, last_searched_at TEXT, sort_title TEXT NOT NULL DEFAULT '', search_text TEXT NOT NULL DEFAULT '', search_clean TEXT NOT NULL DEFAULT '');
CREATE TABLE issue_comments (
                id TEXT PRIMARY KEY,
                issue_id TEXT NOT NULL REFERENCES media_issues(id) ON DELETE CASCADE,
                user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_system INTEGER NOT NULL DEFAULT 0
            );
CREATE TABLE active_downloads (
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
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, track_id TEXT REFERENCES library_tracks(id), album_id TEXT REFERENCES library_albums(id), progress_updated_at TEXT, indexer TEXT, quality TEXT, protocol TEXT, unmatched_files TEXT, indexer_id TEXT, seed_ratio_target REAL, seed_time_target_minutes INTEGER, seed_rule_source TEXT, seed_ratio_current REAL, seeding_seconds INTEGER, cleanup_attempts INTEGER NOT NULL DEFAULT 0, cleanup_error TEXT, placed_files TEXT,
                FOREIGN KEY (client_id) REFERENCES download_clients(id) ON DELETE CASCADE,
                FOREIGN KEY (request_id) REFERENCES music_requests(id) ON DELETE SET NULL
            );
CREATE TABLE library_files (
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
CREATE INDEX idx_playlist_targets_user ON playlist_targets(user_id);
CREATE INDEX idx_missing_tracks_playlist ON missing_tracks(playlist_id);
CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_match_overrides_lookup ON match_overrides(source_title, source_artist);
CREATE INDEX idx_missing_tracks_lidarr_status ON missing_tracks(lidarr_status);
CREATE INDEX idx_requests_user ON music_requests(user_id);
CREATE INDEX idx_requests_status ON music_requests(status);
CREATE INDEX idx_active_downloads_status ON active_downloads(status);
CREATE INDEX idx_active_downloads_client ON active_downloads(client_id);
CREATE INDEX idx_notification_channels_enabled ON notification_channels(enabled);
CREATE INDEX idx_media_issues_status ON media_issues(status);
CREATE INDEX idx_media_issues_user ON media_issues(user_id);
CREATE INDEX idx_music_requests_cutoff ON music_requests(status, cutoff_met);
CREATE INDEX idx_lib_artists_clean_name ON library_artists(clean_name);
CREATE INDEX idx_lib_artists_foreign ON library_artists(foreign_artist_id);
CREATE INDEX idx_lib_albums_artist ON library_albums(artist_id);
CREATE INDEX idx_lib_albums_clean_title ON library_albums(clean_title);
CREATE INDEX idx_lib_albums_foreign ON library_albums(foreign_album_id);
CREATE INDEX idx_lib_tracks_album ON library_tracks(album_id);
CREATE INDEX idx_lib_tracks_artist ON library_tracks(artist_id);
CREATE INDEX idx_lib_tracks_clean_title ON library_tracks(clean_title);
CREATE INDEX idx_lib_tracks_foreign ON library_tracks(foreign_track_id);
CREATE INDEX idx_lib_files_track ON library_files(track_id);
CREATE INDEX idx_lib_files_path ON library_files(file_path);
CREATE INDEX idx_lib_files_cutoff ON library_files(cutoff_met);
CREATE INDEX idx_blocklist_hash ON download_blocklist(info_hash);
CREATE INDEX idx_blocklist_title ON download_blocklist(source_title);
CREATE INDEX idx_blocklist_guid ON download_blocklist(release_guid);
CREATE INDEX idx_active_downloads_track ON active_downloads(track_id);
CREATE INDEX idx_active_downloads_album ON active_downloads(album_id);
CREATE INDEX idx_lib_artists_mbid ON library_artists(mbid);
CREATE INDEX idx_lib_albums_mb_rg ON library_albums(mb_release_group_id);
CREATE INDEX idx_lib_tracks_mb_rec ON library_tracks(mb_recording_id);
CREATE INDEX idx_lib_collections_clean_name ON library_collections(clean_name);
CREATE INDEX idx_lib_artists_begin_year ON library_artists(begin_year);
CREATE INDEX idx_lib_artists_popularity ON library_artists(popularity);
CREATE INDEX idx_system_events_created_at ON system_events (created_at DESC);
CREATE INDEX idx_system_events_type ON system_events (event_type);
CREATE INDEX idx_lib_artists_monitored ON library_artists(monitored);
CREATE INDEX idx_plex_registry_adopted ON plex_playlist_registry(trackseerr_playlist_id);
CREATE INDEX idx_user_listens_lookup ON user_listens(user_id, played_at DESC);
CREATE INDEX idx_user_listens_artist ON user_listens(user_id, artist);
CREATE INDEX idx_user_listens_pending ON user_listens(lastfm_status, listenbrainz_status);
CREATE INDEX idx_tailored_mix_user ON tailored_mix_configs(user_id);
CREATE INDEX idx_internal_nonces_seen_at ON internal_nonces(seen_at);
CREATE INDEX idx_user_invites_user ON user_invites(user_id, purpose);
CREATE INDEX idx_login_attempts ON login_attempts(key, attempted_at);
CREATE INDEX idx_requests_batch ON music_requests(user_id, batch_kind, batch_id);
CREATE INDEX idx_login_attempts_at ON login_attempts(attempted_at);
CREATE INDEX idx_download_history_created ON download_history(created_at);
CREATE INDEX idx_download_history_event ON download_history(event, created_at);
CREATE INDEX idx_download_history_download ON download_history(download_id);
CREATE INDEX idx_active_downloads_created ON active_downloads(created_at);
CREATE INDEX idx_blocklist_created ON download_blocklist(created_at);
CREATE INDEX idx_lib_tracks_monitored ON library_tracks(monitored);
CREATE INDEX idx_lib_artists_sort_name ON library_artists(sort_name, id);
CREATE INDEX idx_lib_albums_sort_title ON library_albums(sort_title, id);
CREATE INDEX idx_lib_tracks_sort_title ON library_tracks(sort_title, id);
CREATE INDEX idx_lib_artists_created ON library_artists(created_at);
CREATE INDEX idx_lib_albums_created ON library_albums(created_at);
CREATE INDEX idx_lib_tracks_created ON library_tracks(created_at);
CREATE INDEX idx_import_list_items_list_status ON import_list_items(list_id, status);
CREATE INDEX idx_lib_files_track_cutoff ON library_files(track_id, cutoff_met, id);
CREATE UNIQUE INDEX idx_delay_profiles_default ON delay_profiles(is_default) WHERE is_default = 1;
CREATE INDEX idx_pending_releases_release_at ON pending_releases(release_at);
CREATE INDEX idx_lib_health_findings_group ON library_health_findings(group_key);
CREATE UNIQUE INDEX idx_artist_links_library ON artist_links(library_artist_id) WHERE library_artist_id IS NOT NULL;
CREATE UNIQUE INDEX idx_artist_links_discovery ON artist_links(discovery_id) WHERE discovery_id IS NOT NULL;
CREATE INDEX idx_issue_comments_issue ON issue_comments(issue_id, created_at);
CREATE INDEX idx_media_issues_album ON media_issues(album_id);
CREATE INDEX idx_item_events_track ON item_events(track_id, created_at);
CREATE INDEX idx_item_events_album ON item_events(album_id, created_at);
CREATE INDEX idx_item_events_artist ON item_events(artist_id, created_at);
CREATE INDEX idx_item_events_request ON item_events(request_id);
CREATE INDEX idx_artist_tags_tag ON artist_tags(tag_id);
CREATE INDEX idx_task_runs_task_started ON task_runs(task_id, started_at);
CREATE INDEX idx_notification_channels_owner ON notification_channels(owner_user_id);
CREATE INDEX idx_user_notifications_user_read ON user_notifications(user_id, read_at);
CREATE INDEX idx_user_notifications_user_created ON user_notifications(user_id, created_at);
CREATE INDEX idx_web_push_subs_user ON web_push_subscriptions(user_id);
CREATE INDEX idx_mb_cache_expires ON mb_metadata_cache(expires_at);
CREATE INDEX idx_mb_cache_kind ON mb_metadata_cache(kind);
CREATE INDEX idx_mb_redirects_new ON mb_id_redirects(new_id);

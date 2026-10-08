# Changelog

All notable changes to TrackSeerr are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

Each release has up to four groups: **Added**, **Changed**, **Fixed** and
**Removed**. Write entries for users, not developers: say what changed in the
app, in one line each. New entries go under `[Unreleased]`; on release, that
heading becomes the version and date.

## [Unreleased]

### Added

- Backup and restore: scheduled and on-demand backups of the database and settings, with restore from the System page.
- Changelog: a "What's new" section on the System page and a one-time notice for admins after an upgrade.

## [1.0.0] - first TrackSeerr release (not yet tagged)

TrackSeerr began as a fork of plex-playlist-sync. The earlier `v1.0.0`–`v1.0.3`
tags belong to that project.

### Added

- Native library management: monitored artists and albums, missing and cutoff-unmet lists, upgrades, rename preview and apply, tag writing on import.
- Acquisition through Torznab and Newznab indexers, qBittorrent, SABnzbd and slskd, with RSS sync, interactive search, a blocklist and failed-download handling.
- Quality, metadata, delay and release profiles, and custom formats.
- Per-indexer seeding rules and seed cleanup.
- Import security: file type checks, quarantine and archive limits.
- AcoustID fingerprint matching and manual import.
- Library health checks against the media server, an issues tracker and a recycle bin.
- Requests with approvals, quotas and auto-approve, plus per-item history.
- Discovery with unified artist profiles and artist tags.
- Import lists from Last.fm, ListenBrainz and MusicBrainz collections; iTunes library import.
- Playlist sync from Spotify and Deezer, listening playlists, tailored mixes and scrobbling.
- Plex, Jellyfin and Subsonic servers (Navidrome, Gonic, Airsonic), or no media server at all.
- Local accounts with invites and two-factor authentication.
- A two-container setup for exposing TrackSeerr to the internet.
- Notifications through Discord, Telegram, Pushover, webhooks and email.
- A Tasks page for scheduled jobs, log rotation and a mobile-first web UI.
- Optional Lidarr mode with migration to the native library.

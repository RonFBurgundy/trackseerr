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
- Lidarr-compatible API, so Prowlarr can sync its indexers to TrackSeerr.
- Transmission, Deluge and NZBGet download clients.
- Per-user notifications: an in-app inbox and browser push notifications, with per-user preferences.
- Release calendar for monitored artists, with a private iCal feed for calendar apps.
- Bulk retag: preview tag changes across many albums, then apply them in one step.
- Update check: the System page shows when a newer TrackSeerr release is available, and the check can be turned off.
- Library filters: narrow artists, albums and tracks by genre (include or exclude), decade or year range, country, release type, solo or group, band size, year formed, popularity and tags, with new Formed, Year and Popularity sorts.
- Artist facts: band type, current member count, years active and Deezer fan count are fetched for library artists on refresh.
- Smart collections: playlists built from library filters, previewed live and pushed to your media server, either auto-updated on every sync or one-time; build them from Playlists, from a filtered Library view, or under Library → Collections.

### Changed

- The Lidarr library import now shows as a running task in the task drawer, with progress and a cancel button, and the logo animates while it runs.
- The "Smart collection" button is greyed out in Lidarr mode, since smart collections need TrackSeerr as the library manager.
- Phone navigation is tidier: the header shows only the essential shortcuts and no longer scrolls, the Calendar now lives under Wanted, and the notification bell is in the menu drawer (desktop keeps its header bell too).
- Issues now live under Requests → Issues for admins as well as users; Activity no longer has an Issues tab and notification links open the new page.
- Library and artist-page tools (select, manual import, rename, retag, scan, refresh, monitored and profile filters) are grouped in a single ⋯ tools menu on every screen size.
- Databases and backups from before this release can no longer be upgraded or restored: TrackSeerr now starts from a single v71 schema. Start with a fresh database.
- The app now runs as `python -m trackseerr` (was `python -m plex_playlist_sync`), including the `init-dmz` command.
- Artist and album metadata from MusicBrainz and Deezer is now stored locally and refreshed on a schedule, so artist refreshes make far fewer requests to those services. Refreshing an artist by hand always fetches fresh data.
- When the MusicBrainz mirror is down, TrackSeerr switches to musicbrainz.org without waiting on the mirror for every request. When both are down, scheduled refreshes pause and retry the skipped artists on the next run.
- Deezer requests are paced to stay under Deezer's rate limit, and a "quota exceeded" reply is retried instead of being treated as missing data.

### Fixed

- Admins can connect Last.fm from the Core admin interface; the connection no longer bounces to the public Requests site and fails with "Authentication required".
- The request quota badge in the header and menu drawer now reads "N left of M", so an unused quota no longer looks used up.
- The download queue table is readable on desktop: artist, album and title no longer break letter by letter.
- Plex sign-in now works through TrackSeerr Requests in the two-container setup; it gets your Plex server's identity from Core instead of failing with "No Plex machine identifier configured".
- Unraid two-container setup: the templates, install guide and `init-dmz` now list both networks in Extra Parameters (Unraid ignores the Network Type dropdown once a `--network` flag is there), so Requests can reach your tunnel/proxy and Core can reach Plex. The docs now explain which network Core should join: your app network when Cloudflare Tunnel runs on the host, or its own `trackseerr-core-lan` network when a reverse proxy or tunnel runs as a container. The internal network is created with `--internal`.
- Import from Lidarr is back: switching the library manager from Lidarr to TrackSeerr offers to copy your Lidarr library first, and the Library page shows live progress with a Stop button.
- Import from Lidarr now works on large libraries: it copies albums, tracks and files artist by artist, so your Wanted list carries over, and if Lidarr fails partway it stops without switching the library manager.
- Featured Charts and .m3u upload are back in Add Playlist.
- With Spotify API keys set, Spotify's own playlists (such as Today's Top Hits) now sync instead of coming back empty.
- Tapping a tab on a phone no longer lands on a different tab because the row jumped back while you were scrolling it.
- Artists with more than 100 releases now show their complete discography.
- Albums and artists whose MusicBrainz IDs were merged or changed are relinked automatically.
- The MusicBrainz mirror setting is now used everywhere; some background tasks ignored it.
- The artist page now shows how many of the artist's tracks are actually in your library (it read 0 for local libraries), and artist status lights show complete or missing tracks for local libraries.

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

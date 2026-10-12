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

- Activity has a Searches tab listing every indexer and Soulseek search Trackseerr ran: the query, how many results came back, how long it took, and what triggered it (request, wanted sweep, manual search, retry...). Kept for 30 days.
- Soulseek downloads in the Activity queue show the peer, whether it is a whole folder, and slskd's own state (e.g. "Queued, Remotely"). A new "Blocklist and search again" action drops the current release and looks for another.
- The slskd download client has a Soulseek tuning section: peer queue and speed limits, ignored users, search timeout and spacing, and how long a stuck transfer may wait before Trackseerr tries another peer.
- Indexer priority (1–50): breaks ties between otherwise equal releases, so a preferred indexer's copy wins; a better release from a lower-priority indexer still wins. Soulseek results use their slskd client's priority.
- Download client priority (1–50) and an Enabled switch: grabs go to the highest-priority enabled client for their protocol, clients with equal priority take turns, and if a client can't take a grab the next one is tried (the failed client is skipped for 5 minutes).
- Reject current release: from an album's or track's history, or from Manual Search, blocklist the release behind the current files and search for another; the current files stay until the replacement imports.
- Manual Search has a History tab for the album or track, shows when the automatic search last ran and what it found, and flags releases that are blocklisted (with the reason and date), already downloading, or held by a delay profile.
- Album and track history shows each release's name and quality, says what an upgrade replaced, and has a Releases filter for the download, import, replacement and blocklist trail.
- Manual search: search every indexer for an album or track from the album view or the Missing and Cutoff Unmet pages, see each release's quality, size, seeders, age and score with the reasons it would be rejected, and download the one you want.
- Download clients can be edited.
- Fix unmatched playlist songs from the tracklist: users search the catalog for the right song and suggest it (an admin approves it from the Issue in one click), request it if it's not in the library, or report it. Admins can also mark a song unmatchable so it stops counting as missing. Approved fixes apply to every playlist on the next sync, for the TrackSeerr library, Plex and Jellyfin.
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
- Lidarr import report: every import saves a report comparing Lidarr's counts with what was imported, per artist, and listing anything skipped; download it from Settings → Library manager.

### Changed

- A failed download (torrent, Usenet or Soulseek) now searches for the track again right away instead of waiting for the next wanted sweep; a failed album download re-searches its tracks.
- A torrent that has downloaded nothing after 2 hours (no seeders) is removed, blocklisted and searched again. Torrents with any progress, or seeding, are never touched. Set `TORRENT_DEAD_MINUTES` or a client's `dead_torrent_minutes` to change the wait.
- A Soulseek download still queued by the peer is given up after 15 minutes (was 30) and another source is searched at once.
- Soulseek album requests now grab a whole folder from one peer instead of a single file. The folder must have the album's track count (up to 2 bonus tracks), at least 80% of its track titles, and a matching total length when the album's tracks are known. Multi-disc albums are not grabbed from Soulseek yet.
- Soulseek: among files of the same quality, peers with a free upload slot, then a shorter queue, then a faster connection are preferred; peers with a queue over 50 are skipped; files whose length is far from the track's known length are skipped (catches the wrong mix or edit); searches to slskd are spaced at least 5 seconds apart.
- Automatic searches that find nothing retry with simpler queries: the main artist only, without "feat." credits, then the bare title with any remix name kept. Results from these retries must match the artist, title and remixer, so a remix request never grabs the original mix. Every indexer and Soulseek search is now logged with its query, result count and time.
- Download clients must pass a connection test before they can be saved, and the add/edit form has its own Test button.
- The Lidarr library import is much faster: it asks Lidarr for each artist's tracks in one go instead of album by album, and database writes are quicker everywhere.
- Adding a Spotify or Deezer playlist loads its tracks right away. Each playlist on the Sync tab shows its track count and a Tracks button listing every song, with missing tracks highlighted after a sync.
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

- Importing from Lidarr or refreshing an artist no longer merges a single into an album with the same title (e.g. "R.A.M.O.N.E.S." into "Ramones", the "Wild Ones" single into the album), which had renamed the album, changed its year and moved its tracks. Albums imported from Lidarr also keep their type (single, EP, live, compilation). Wipe and re-import an affected library.
- A library scan files tracks under the album matching their MusicBrainz release group tag before trying the album title, so a single is no longer filed onto an album with the same name.
- File names now treat characters that aren't allowed in them the way Lidarr does: "*" becomes "-" (D*Minds → D-Minds), "?" becomes "!", "\" becomes "+", and a colon without a space after it becomes "-" (45:33 → 45-33), so renaming a Lidarr library no longer moves these files.
- A library-wide rename preview no longer times out on large libraries: it checks only tracks that have files and no longer touches the disk to compare paths.
- The rename preview shows full paths (scroll sideways on narrow screens) and highlights the part that changes.
- Naming tokens {Artist CleanName}, {Album CleanTitle}, {Track CleanTitle} and {Track ArtistCleanName} now work like Lidarr's: they remove characters that aren't allowed in file names and keep a leading "The" (The Offspring stays The Offspring; AC/DC becomes ACDC). The CleanNameThe/CleanTitleThe variants clean the name and move the article to the end. Elsewhere a "/" becomes "+" as in Lidarr.
- A library-wide rename preview now goes artist by artist and album by album, so a capped preview shows whole albums instead of scattered tracks from across the library.
- Downloads that match a library track are filed under that track's artist, album and year, even when the file is tagged as part of a compilation (they no longer land in "Various Artists").
- Album folders no longer end in empty brackets like "Closer ()" when the release year is unknown.
- Library → Rename files now checks the whole library (not just the first 500 tracks), lists only files that need moving, and carries album artwork along so emptied folders are removed.
- The ⋯ menus (e.g. Library tools on mobile) no longer open behind the page content.
- Artist and album art imported from Lidarr no longer shows as broken images. Imports now keep each image's original web address, and existing Lidarr paths are cleared on upgrade; run the artist metadata refresh from Tasks to fill the art back in.
- The wanted sweep no longer re-searches the same alphabetical first 100 tracks every hour; it works through the whole list, least recently searched first.
- Soulseek no longer grabs the wrong song from a folder named after the album (e.g. a track called "AKUMA" from an "AKUMA" album folder); the file name itself must match the track title.
- Soulseek searches that are still collecting results are no longer cut off early, so fewer searches come back empty.
- Soulseek results now show their real quality (FLAC 16/24-bit, MP3 320/V0/V2, AAC, ALAC, OGG, Opus) instead of Unknown, so quality profiles rank and accept them properly.
- Soulseek downloads that a peer never starts ("Queued, Remotely") for 30 minutes, that stop moving for 15 minutes, or that vanish from slskd for 10 minutes are now cancelled and blocklisted, so the next search tries another peer (adjustable per slskd client in Settings, or with `SLSKD_QUEUE_TIMEOUT_MINUTES`, `SLSKD_STALL_MINUTES`, `SLSKD_MISSING_MINUTES`). Failed Soulseek transfers (errored, rejected, timed out) are no longer treated as finished downloads, and cancelling a Soulseek download works again.
- Soulseek searches now wait for peers to answer before reading results (they almost always came back empty), and each finished search is removed from slskd instead of piling up in its Searches list.
- Editing a download client or indexer no longer resets its priority or re-enables it.
- Editing a download client no longer drops settings the form does not show (such as a custom download folder or remote path mappings).
- Removing a Soulseek download from the queue with "blocklist" now actually blocklists that peer's file, so it is not grabbed again.
- slskd connection tests failed with HTTP 404 even with a valid API key; slskd now connects with its URL and API key only.
- qBittorrent 5 logins were reported as invalid credentials; a rejected login now also mentions the URL port, which must match qBittorrent's own WebUI port.
- Download client tests say when the client could not be reached at all, instead of blaming the credentials, and failed tests are written to the TrackSeerr log.
- Testing a saved download client used the masked password, so qBittorrent, Transmission, Deluge and NZBGet reported invalid credentials; SABnzbd's test now actually checks the API key, and qBittorrent says when it has banned the IP after failed logins.
- Users can delete playlists they added themselves.
- Copy buttons (API key, feed and webhook links, recovery codes) work over plain HTTP, and the revealed API key can be selected on phones.
- Lidarr import: tracks are only monitored when their album and artist are monitored in Lidarr, so unmonitored albums no longer show up as missing after switching.
- Missing-track counts skip unmonitored artists and match Lidarr's numbers, and artists with files on unmonitored albums no longer look complete when monitored tracks are missing.
- Artist refresh no longer adds duplicate tracks when MusicBrainz spells a title differently from an imported track.
- Tracks without a file on unmonitored albums show "Unmonitored" instead of "Missing".
- Importing from Lidarr now links the music files you already have, so owned tracks no longer show as missing.
- Importing from Lidarr keeps Lidarr's monitoring: only the albums you monitored there are monitored, the back catalog stays unmonitored, and re-running the import unmonitors albums Lidarr doesn't have.
- Adding a playlist that another user already added now adds it to your playlists instead of failing.
- Without Spotify API keys, playlists longer than 100 tracks are now read in full (up to 10,000 tracks); before, only the first 100 came through.
- Signing in to the Requests site with an admin account now says to use TrackSeerr Core instead of letting you in to a page where nothing loads.
- Keyless Spotify playlists match more songs: tracks with several artists match on the main artist, and titles Spotify censors with asterisks ("F**k You") still match the uncensored song in your library or Plex.
- Scheduled syncs pick up tracks added to or removed from public Spotify playlists when no Spotify API keys are set; before, the first fetched list was reused until restart.
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

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

- An album's track list shows "On <album>" instead of "Missing" for a song you already own on another release. Tap it to open that album.
- Media Management → Monitoring has a "Keep the same song from more than one release" switch. It is off by default, so a song you own on one release isn't downloaded again for another; turn it on to collect every release's copy.
- Library Health has a "Shorten name" button on each "Names too long" finding. It gives a library track its normal Rename name, or shortens any other file or folder in place, without deleting anything. Rename also marks these files with a "Name too long" reason and selects them by default.
- Library Health lists file and folder names that are too long (over 200 bytes) under "Names too long". These names can make the Unraid mover and renames fail. Run Rename to shorten them. This check works even without a media server.
- System → Status shows how many MusicBrainz requests went to the mirror and to musicbrainz.org in the last hour, the request limit, and whether TrackSeerr is pausing either one.
- New page, External services, lists every outside server TrackSeerr talks to, when, and how much.
- Add YouTube Music playlists by pasting their link, just like Spotify and Deezer. No account or API key is needed. The playlist must be Public or Unlisted; private playlists and Liked Music are rejected with a message saying so. Playlists up to 10,000 tracks are read, and music-video titles like "Artist - Song (Official Video)" are cleaned up before matching.
- Compilation files are named after each track's own artist: `Various Artists/Saturday Night Fever (1977)/03 - Yvonne Elliman - If I Can't Have You.flac`. Media Management adds the compilation artist folder name, who a remix is credited to (original artist, remixer, or as MusicBrainz credits it) and where featured artists go; every naming preset sets them, and a new "beets / Picard" preset files compilations under Compilations.
- The Requests page and the Activity Blocklist, Searches and Queue tabs have the fast-scroll rail used in the Library. Date rails for events (requests, history, blocklist, searches, date added) read Today, Yesterday, the last few weekdays, then months and years, in your own timezone; release-date rails group by year, month or day depending on the spread, Blocklist sorted by artist jumps by letter, and lists with nothing to group by (the Queue) are labelled by position.
- "Library Retag" and "MusicBrainz Release Match" show how far along they are on the Tasks page.
- Contribute to AcoustID: add your AcoustID user key in Media Management, then run "AcoustID Contribution" from the Tasks page to submit fingerprints of your files that have MusicBrainz recording IDs, credited to you. Files AcoustID already knows, files that fingerprint as a different recording, and files whose length doesn't match the track are skipped. Media Management shows how many were submitted, imported and skipped.
- Contribute to MusicBrainz: "Not on MusicBrainz…" in the Library ⋯ menu lists albums you own that MusicBrainz lacks (or lacks this edition of). "Submit to MusicBrainz" opens MusicBrainz's Add Release form prefilled with the tracklist, track lengths, label, barcode and Deezer link; log in there, check it and submit, and you come back with the album linked to the new release. You can also paste a MusicBrainz release link to link an album by hand.
- Admins can add and monitor an artist directly: "Add artist…" in the Library ⋯ menu, an "Add as artist" button when a library search finds little or nothing, and an "Add / monitor" button on a Discover artist page for artists not yet in the library (it becomes "Open in Library" once added).
- Each request shows how it was made (manual, playlist with its name, mix, Wanted, discography…), and the Requests page can be filtered by that source.
- "Retag entire library" (retag dialog, or the "Library Retag" task) rewrites tags in the background on every file whose tags differ from the catalog, now including the MusicBrainz album-artist ID, so Plex, Jellyfin and Picard match reliably.
- "MusicBrainz Release Match" task (Tasks page) picks the MusicBrainz release for each album from its release group, so files can carry release and recording IDs. Resumable; runs at MusicBrainz's ~1 request per second.
- The rename dialog can relink all selected audio-mismatch files to their suggested tracks in one go ("Relink suggested"), reporting any it skipped because the right track already has a file.
- The rename preview labels tracks that would move between a Various Artists compilation folder and an artist folder ("Compilation link") and leaves them unticked.
- The rename dialog warns when Plex's "Empty trash automatically after every scan" is on, since a scan during the rename could delete play counts and ratings for moved tracks.
- Scheduled tasks (playlist sync, Wanted sweep, RSS sync, artist refresh, art backfill, mixes, seed cleanup, recycle bin, backups, update check) can be paused and resumed from the Tasks page without changing their schedule.
- Manual runs of the track credits backfill, artwork backfill and artist refresh can be cancelled from the Tasks page; they stop after the current item. Shutting down also stops manual runs instead of waiting for them.
- Wanted lists show when each item was added and can be sorted by it, newest first (not available when Lidarr manages the library).
- Missing tracks from Spotify and Deezer playlists set to "track" monitoring are now requested automatically as the playlist's owner, so they show up as request cards you can follow (quota and approval rules apply). Each sync logs how many were requested, already owned or waiting.
- The library Tracks tab has an "Owned only" switch, on by default, listing just the tracks you have files for; turn it off to include missing catalog tracks.
- Rename preview: a file whose audio AcoustID identifies as a different track in your library gets a "Relink to this track" button. If that track already has a file, you choose which to keep (or switch on "Keep the higher-quality file"); the other goes to the recycle bin.
- The rename preview can check risky moves with AcoustID: "Verify risky" fingerprints those files (about 3 per second, in the background) and compares the audio with the track each file is linked to. Confirmed files are marked "Audio verified" and pre-selected; files whose audio is a different track show what it really is and can't be renamed until relinked. Results are cached, so re-checking is instant. Needs an AcoustID API key in Media Management.
- Renames keep each artist's existing folder, as Lidarr does, and new downloads for that artist go into it too; only album folders and file names are tidied. A new "Rename artist folders" switch in the naming settings renames artist folders as well when you want them consistent.
- The rename preview says why each file would move (artist folder, loose file, format only, album year or title changed, disc layout, track number or title changed, name collision), lets you filter by reason, and pre-selects only the safe, format-only moves. It shows up to 5,000 moves.
- Tracks now record their own artists (performer, featured artists, remixers) from MusicBrainz, file tags and remix titles, so a compilation track knows it is by the Bee Gees rather than "Various Artists". Run "Track Artist Credits Backfill" on the Tasks page once to fill this in for an existing library; scans and refreshes keep it up to date.
- Media Management has a "Replace illegal characters" switch, as in Lidarr: on, "?" becomes "!" and "*" becomes "-"; off, they are removed. The switch sits in the naming settings and is applied by presets.
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

- TrackSeerr is gentler on the MusicBrainz mirror: at most 3 requests per second and 2 at a time (set with `MUSICBRAINZ_MIRROR_RPS` and `MUSICBRAINZ_MIRROR_CONCURRENCY`). When the mirror asks it to slow down, it waits as long as asked, and it stops asking a mirror that keeps refusing it.
- On phones, task rows on the Tasks page show icon-only Run and Cancel buttons and drop the Running badge while the progress bar shows, so task names have room (they wrap to two lines instead of being cut off).
- The Requests page loads in pages and scrolls smoothly with thousands of requests, instead of loading every request at once.
- Approved requests (manual, automatic, playlist and discography) now add the artist to the library and monitor exactly what was requested, so they appear under Wanted → Missing and in the library's missing counts before anything downloads. Existing approved requests are picked up by the next Wanted sweep.
- Relinking, verifying or applying in the rename dialog now refreshes only the affected rows instead of rescanning the whole library.
- With "Rename artist folders" off, renames and imports keep an artist in the folder where most of their files already are, instead of a folder rendered from a differently spelled artist name.
- Disc folders follow the discs you actually own: a single-disc rip of a multi-disc release no longer gets a "Disc 01" folder, in renames and new imports.
- The library-wide rename preview says how many files need a rename in total when it shows only the first 5,000.
- Every remaining browser checkbox, radio button and slider (permissions, rename/retag, profiles, notifications, logs, calendar, mixes, user presets) now uses the app's own switches, selection boxes and sliders.
- Activity (Queue, Searches, History, Blocklist, Needs Review), Wanted and album cards use one compact layout on every screen: details get the full width, all actions sit in one ⋯ menu, extra details open with a chevron, and items needing attention get a tappable badge and an amber dot on the menu.
- Lists, panels and modals show the tape-deck loader with rotating messages while loading, instead of a plain spinner.
- The Tasks page groups tasks by purpose (Library, Acquisition, Metadata & art, Maintenance, Playlists & listening) in collapsible sections, pins running tasks at the top, adds a filter box, and shows one compact row per task with details and run history a tap away.
- Artist discographies follow the artist's metadata profile: releases outside it are hidden by default (the toolbox toggle still shows them), empty tabs disappear, and remixes get their own tab instead of landing in Studio.
- Opening a section in the navigation menu now closes the others and scrolls it into view, so its pages are visible on a phone.
- Playlist cards fold their sync-target users behind a "N of M users" toggle, flag playlists that have no users assigned (so they are not pushed to the media server), and explain what each monitor mode does with missing songs.
- Playlist sync skips playlists whose source list, users and your library haven't changed since the last sync, so big lists no longer rematch every 10 minutes. Newly imported music still triggers a rematch, and every playlist is fully resynced once a day; "Sync now" always syncs everything.
- Sorting and searching the library Tracks list is now near-instant on large libraries (hundreds of thousands of catalog tracks), and no longer stalls other pages while it runs. The first start after updating builds a new search index, which can take a minute on a big library.
- New installs use the Trackseerr naming preset: artist and album names exactly as written (accents and & kept), Lidarr's handling of characters a filesystem can't store (? → !, * → -, / → +), a "?" at the end of a track title dropped as Lidarr does, and "..." in titles kept. "Lidarr Standard" is now "Lidarr Default" with Lidarr's real out-of-the-box naming, every preset explains who it is for, and the unused "clean artist names" setting is gone.
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

- Add Artist finds artists again. Searching in the Library's Add Artist tool always came back empty.
- Retrying a held download no longer searches the whole download folder when the file is already gone (which could stall all downloads for minutes and blocklist a good release), and only one of two downloads of the same file is retried.
- A downloaded song that doesn't fit the album it was meant for is now imported onto the same song elsewhere in your library when there's exactly one match with the same title and length, including under a related artist name ("Elvis Costello & The Attractions" and "Elvis Costello"). Downloads already waiting for manual import get one automatic retry.
- Clearing duplicate downloads now checks the song you asked for. A wrong-song file that happens to be another song you own is discarded, but the song you wanted stays monitored and requested. A download whose file has disappeared is marked failed so it is searched again.
- Playlist and list syncs no longer request a song again while its earlier request is still in progress, so the same song is no longer downloaded twice. They also skip songs you already own on another release.
- Downloads of a song you already own are cleared on their own, as Lidarr or Sonarr would: the extra file is deleted, the transfer is removed from slskd, and the empty copy of the song is unmonitored so it leaves Missing. Downloads already stuck in the queue for this reason are cleared too. Files from torrents that are still seeding are never deleted.
- Soulseek downloads for a song must be that song. A search for "Time" no longer grabs "All of the Time", and a file from an album named like the song (Logic's "Under Pressure" album giving "Bounce") is no longer taken.
- Songs you already own on another release ("Play in the Sunshine" when a playlist asks for "Play In The Sunshine - 2020 Remaster") count as owned, so TrackSeerr no longer searches for and downloads them again.
- When a download replaces a track whose old file has a very long name, the old file now goes to the recycle bin under a shortened name instead of being left behind in your library.
- TrackSeerr no longer asks your media server to rescan its whole library after every import. Like Sonarr, Radarr and Lidarr, it now asks Plex or Jellyfin to rescan only the album folders that changed, about a minute after imports, renames and retags. When it can't match a folder to the server's path, it falls back to one full music rescan at most every 10 minutes (set `MEDIA_SERVER_REFRESH_MINUTES`; `0` turns rescans off). Jellyfin full rescans now cover only music libraries. This stops large memory spikes in Plex and Jellyfin during big download batches.
- Very long track or album titles no longer make file and folder names too long to save or move. Names are shortened to 200 bytes, extension included, so imports, renames and the Unraid mover stop failing on them. Run the rename preview once to shorten files already in your library.
- Adding a playlist that fails (bad link, unreadable playlist) now shows an error message instead of doing nothing.
- Installs still set to the old BrainzMash address (api.brainzmash.org) move to api.brainzmash.cc automatically, and entering the old address saves the new one.
- Retag and import wrote the album artist into every track's ARTIST tag, so compilation tracks were tagged "Various Artists" and featured credits were lost. ARTIST now holds the track's credited artist and ALBUMARTIST the album artist; run a retag to repair files already tagged.
- History and Missing (sorted by date added) showed no fast-scroll rail when all their dates fell in the same month.
- The "Add / monitor" button on a Discover artist page now switches to "Open in Library" as soon as the artist is added, instead of staying until the next day.
- Albums no longer show a year of 1 (Lidarr's placeholder for "no date"); existing placeholder dates are cleared on upgrade.
- Downloads that don't match a catalog album now land in the artist's existing folder (matched by MusicBrainz ID or name) instead of a second, differently named folder.
- Editing an administrator no longer shows permission toggles that look editable but don't save; they show as held and locked.
- The Wanted sweep, RSS sync and manual Wanted searches no longer run when no indexer has a matching download client (or no Soulseek client is set up); the Tasks page shows why they were skipped.
- Playlist "find in library" on Plex now finds tracks by artist name (and "artist title" searches), and returns results faster.
- Library scans no longer merge two tracks that share a title on the same album (such as repeated "[silence]" bonus tracks); each keeps its own file.
- Playlists with no users assigned are still matched against your library, so their missing tracks are recorded and can be requested; they just aren't pushed to Plex until you assign someone.
- Requesting a track that's already in your library is refused ("Already in your library"), and search results now show the In library badge for tracks deep in a large artist's catalog, on another edition, or on a compilation credited to the artist. Playlists, mixes and import lists skip such tracks instead of requesting them.
- The retag preview scrolls long file paths and tag values sideways instead of cutting them off.
- Album folders keep the album's original release year. An artist refresh no longer replaces it with a reissue or remaster date, and impossible years (such as 1884) are ignored, so new downloads no longer land in a second, wrongly dated album folder. Re-run the Lidarr import once to restore original years on albums that were already changed.
- The Lidarr import no longer merges different tracks that share a title (a dub on disc 2, dozens of "[silence]" tracks), which made renames propose wrong numbers and collisions. Re-run the Lidarr import once to repair an existing library; no wipe needed.
- Checkboxes in the rename and retag previews use the amber accent like the rest of the library instead of the browser's blue.
- Clean naming tokens ({Artist CleanName}, {Album CleanTitle}, ...) now produce exactly what Lidarr does: "&" becomes "and", "/" becomes a space (AC DC), accents are removed (Husker Du), and the punctuation Lidarr drops is dropped (Lightnin Luke). A leading "." is removed from folder and file names.
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

- Missing tracks from Spotify and Deezer playlists set to "track" monitoring are now requested automatically as the playlist's owner, so they show up as request cards you can follow (quota and approval rules apply). Each sync logs how many were requested, already owned or waiting.
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

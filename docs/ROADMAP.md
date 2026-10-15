# Roadmap

What we plan to build next. This is a plan, not a promise. Order and scope can change.

To ask for something, [open a feature request](https://github.com/RonFBurgundy/trackseerr/issues/new?template=feature_request.md).

## Planned

- **First tagged release.** Stable settings, a stable API, and versioned images.
- **Spotify account sign-in.** With your own Spotify API keys, sign in to Spotify to sync your private and collaborative playlists and Liked Songs. Public playlists already work with or without keys.
- **Emby.** Emby shares most of its API with Jellyfin, so support will reuse the Jellyfin adapter: per-user playlists, user import, rescans, and library health.

## Being considered

- **Tidal playlists.** Import Tidal playlists by link, like Spotify and Deezer.
- **More playlist sources.** Apple Music and YouTube Music playlist links.

## Not planned

- **Downloading from streaming services** (Tidal, Spotify, Deezer, and others). This breaks their terms of service. TrackSeerr downloads only through the download clients and indexers you set up.

## Recently done

- Add and monitor artists straight from the Library or a Discover artist page.
- Approved requests show up as Missing right away; the Requests page is paged and shows where each request came from (manual, playlist, discography, and so on).
- MusicBrainz release matching and library-wide retagging with MusicBrainz IDs.
- Smarter renames: AcoustID checks for risky moves, relinking misidentified files, Lidarr-style artist folders.
- Soulseek (slskd) as a download source, with whole-folder album grabs and peer tuning.
- Manual search, release history, and rejecting a bad release.
- Smart collections and library filters (genre, decade, country, and more).
- Release calendar with an iCal feed.
- Per-user notifications: in-app inbox and browser push.
- Backup and restore, a "What's new" changelog, and update checks.
- Tasks page with pause, resume and cancel.
- Playlist missing tracks are requested automatically, and users can fix unmatched songs.
- Lidarr-compatible API so Prowlarr can sync indexers; fast Lidarr library import with a report.
- More download clients (Transmission, Deluge, NZBGet).
- Jellyfin and Subsonic servers (Navidrome, Gonic, Airsonic), and running with no media server.
- Local accounts with invites and two-factor authentication.
- Two-container setup for internet exposure.
- Quality, metadata, delay, and release profiles, and custom formats.
- Per-indexer seeding rules and seed cleanup.
- Library health check against the media server.
- AcoustID fingerprint matching on import.

# Roadmap

What we plan to build next. This is a plan, not a promise. Order and scope can change.

To ask for something, [open a feature request](https://github.com/RonFBurgundy/trackseerr/issues/new?template=feature_request.md).

## Planned

- **First tagged release.** Stable settings, a stable API, and versioned images.
- **Emby.** Emby shares most of its API with Jellyfin, so support will reuse the Jellyfin adapter: per-user playlists, user import, rescans, and library health.

## Being considered

- **More download clients.** NZBGet, Transmission, and Deluge.
- **Tidal playlists.** Import Tidal playlists by link, like Spotify and Deezer.
- **More playlist sources.** Apple Music and YouTube Music playlist links.

## Not planned

- **Downloading from streaming services** (Tidal, Spotify, Deezer, and others). This breaks their terms of service. TrackSeerr downloads only through the download clients and indexers you set up.

## Recently done

- Jellyfin and Subsonic servers (Navidrome, Gonic, Airsonic), and running with no media server.
- Local accounts with invites and two-factor authentication.
- Two-container setup for internet exposure.
- Quality, metadata, delay, and release profiles, and custom formats.
- Per-indexer seeding rules and seed cleanup.
- Library health check against the media server.
- AcoustID fingerprint matching on import.

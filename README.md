<p align="center">
  <img src="unraid/trackseerr.png" alt="TrackSeerr Logo" width="180">
</p>

# TrackSeerr

[![CI](https://github.com/RonFBurgundy/trackseerr/actions/workflows/ci.yml/badge.svg)](https://github.com/RonFBurgundy/trackseerr/actions/workflows/ci.yml)
[![Docker](https://github.com/RonFBurgundy/trackseerr/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/RonFBurgundy/trackseerr/actions/workflows/docker-publish.yml)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE.md)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![Status: Work in Progress](https://img.shields.io/badge/status-active%20development-orange.svg)](#status)

TrackSeerr is a self-hosted music request and library manager. Your users find music and ask for it. TrackSeerr gets it, files it, and puts it on your media server, along with their playlists.

It works with Plex, Jellyfin, and Subsonic servers such as Navidrome. It can download music on its own, or hand the work to Lidarr if you already use it.

## Status

TrackSeerr is in active development. Features, settings, and the API still change between builds. Wait for a tagged release before you rely on it.

## What it does

- **Discover.** Search artists, albums, and tracks. Browse charts and new releases. Play 30-second previews. No API keys needed.
- **Request.** Users request a track, an album, or a whole discography. You set quotas and approve requests, or let trusted users skip approval.
- **Playlists.** Import playlists from Spotify, Deezer, Last.fm, ListenBrainz, or plain text. TrackSeerr matches them to your library, keeps them in sync, and pushes them to your media server for each user.
- **Get the music.** Search Usenet and torrent indexers, or Soulseek, and download through SABnzbd, qBittorrent, or slskd. Or send requests to Lidarr.
- **Manage the library.** Scan, tag, rename, and import files. Track quality and upgrade it over time. Find files your media server missed.
- **Users.** Sign in with Plex or with a local account. Each user has their own requests, playlists, quotas, and scrobbling.
- **Notify.** Send alerts to Discord, Telegram, Pushover, email, or a webhook.

## How it works

```
   Users (web, mobile)
          |
          v
 +-----------------------+
 |      TrackSeerr       |   discover, request, playlists, users
 |                       |
 |   library manager:    |
 |   TrackSeerr or Lidarr|
 +-----------------------+
     |              |
     v              v
 Indexers and    Your music folder
 download clients      |
                       v
                 Media server
          (Plex, Jellyfin, Navidrome...)
```

1. A user requests music or imports a playlist.
2. TrackSeerr checks what is already in your library.
3. For anything missing, the library manager searches your indexers and sends the release to a download client.
4. When the download finishes, TrackSeerr checks the files, tags them, renames them, and moves them into your music folder.
5. Your media server picks up the new files. TrackSeerr adds the tracks to the right playlists and notifies the user.

## Why it is built this way

**You choose the media server.** Media servers plug in as adapters. TrackSeerr runs fine with no media server at all; it then manages files and finds missing tracks but pushes nothing.

**One library manager at a time.** TrackSeerr has its own library manager, built to do what Lidarr does with more control at the track level. If you prefer Lidarr, switch to it in one setting and TrackSeerr becomes the request front end. Only one can manage the library at a time, so the two never fight over the same files. You can also import an existing Lidarr library into TrackSeerr.

**Two ways to deploy.** Run one container on your home network. If you put TrackSeerr on the internet, split it into two containers: a public one that holds no files and no secrets, and a private core that holds everything else. If the public container is compromised, the attacker gets request data, not your library or admin controls. See [Architecture and security](docs/ARCHITECTURE_AND_SECURITY.md).

## Install

You need Docker and a folder for your music. Pick one setup:

| Setup | Use it when | Guide |
|---|---|---|
| Single container | TrackSeerr is only reachable on your LAN, a VPN, or Tailscale | [Install guide](docs/INSTALL.md#single-container) |
| Two containers | TrackSeerr is reachable from the internet | [Install guide](docs/INSTALL.md#two-containers) |
| Unraid | You run Unraid | [Unraid guide](docs/UNRAID_INSTALL_GUIDE.md) |

Minimal single-container example:

```yaml
services:
  trackseerr:
    image: ghcr.io/ronfburgundy/trackseerr:latest
    container_name: trackseerr
    restart: unless-stopped
    ports:
      - "5250:5250"
    volumes:
      - ./appdata:/config
      - /path/to/data:/data   # holds media/music and downloads
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
      # With Plex: the server owner signs in with Plex
      - PLEX_URL=http://192.168.1.100:32400
      - PLEX_TOKEN=your_plex_token
      # Without Plex: sign in as "admin" with this password instead
      # - ADMIN_PASSWORD=change-me-to-12-or-more-characters
```

Start it with `docker compose up -d` and open `http://<server-ip>:5250`. Jellyfin, Subsonic servers, download clients, indexers, and Lidarr are set up in the web UI.

## Documentation

Setup:

- [Install guide](docs/INSTALL.md): Docker Compose, folders, networking, reverse proxies, the two-container setup
- [Unraid guide](docs/UNRAID_INSTALL_GUIDE.md)
- [Configuration reference](docs/CONFIGURATION.md): every environment variable
- [Media servers](docs/MEDIA_SERVERS.md): Plex, Jellyfin, Subsonic, or none

Using TrackSeerr:

- [Users and requests](docs/USERS_AND_REQUESTS.md): sign-in, local accounts, permissions, quotas, approval
- [Playlists](docs/PLAYLISTS.md): importing, syncing, and per-user playlists
- [Downloading music](docs/ACQUISITION.md): download clients, indexers, profiles, seeding, Lidarr
- [Library management](docs/LIBRARY.md): scanning, importing, naming, quality, cleanup

Reference:

- [Architecture and security](docs/ARCHITECTURE_AND_SECURITY.md)
- [Roadmap](docs/ROADMAP.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## Acknowledgments

TrackSeerr started from [rnagabhyrava/plex-playlist-sync](https://github.com/rnagabhyrava/plex-playlist-sync). Thanks to its author for the original playlist matching idea.

It has since grown into its own project. It owes a lot to:

- [Overseerr](https://github.com/sct/overseerr) and its successors, for the discovery, request, and quota model.
- [Lidarr](https://github.com/Lidarr/Lidarr), [Sonarr](https://github.com/Sonarr/Sonarr), and the rest of the *arr projects, for naming templates, quality profiles, and the download lifecycle.
- [Mutagen](https://github.com/quodlibet/mutagen), for reading and writing audio tags.
- [slskd](https://github.com/slskd/slskd), for a clean API to the Soulseek network.

## License

GNU General Public License v3.0. See [LICENSE.md](LICENSE.md).

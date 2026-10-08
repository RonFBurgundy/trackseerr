# Configuration reference

Most settings live in the web UI under **Settings**. Environment variables cover what must be known at startup: the container role, secrets, and the media server connection.

Some settings can be set in both places. Which one wins depends on the setting, and each one says so below.

For on/off variables, `1`, `true`, `yes`, and `on` mean on. Anything else means off.

- [Container](#container)
- [Two-container setup](#two-container-setup)
- [First admin](#first-admin)
- [Media server](#media-server)
- [Playlist sync](#playlist-sync)
- [Spotify and Deezer](#spotify-and-deezer)
- [Requests](#requests)
- [Background jobs](#background-jobs)
- [Lidarr](#lidarr)
- [Scrobbling](#scrobbling)
- [Other](#other)

## Container

| Variable | Default | Description |
|---|---|---|
| `PUID` / `PGID` | `1000` / `1000` | User and group that own the files TrackSeerr writes. Use `99` / `100` on Unraid. |
| `UMASK` | `022` | Permission mask for new files. |
| `TZ` | unset (UTC) | Time zone for logs and schedules. |
| `PORT` | `5250` | Web port inside the container. |
| `HOST` | `0.0.0.0` | Address the web server binds to. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `FORCE_CHOWN` | `0` | `1` sets the owner of everything in `/config` and `/data` to `PUID:PGID` on this start. It runs once on its own the first time. Slow on large libraries. |
| `APPLICATION_URL` | unset | Public URL, for example `https://music.example.com`. Used in notification links, Plex sign-in, and invite links. Can also be set in **Settings > General**; the saved value there wins. |
| `TRUSTED_PROXIES` | unset | Comma-separated IPs or subnets of your reverse proxies, for example `172.18.0.0/16`. TrackSeerr trusts `X-Forwarded-For` only from these addresses. Set it on the container your proxy talks to. |

## Two-container setup

See [Install guide: two containers](INSTALL.md#two-containers).

| Variable | Default | Description |
|---|---|---|
| `ROLE` | `all-in-one` | `all-in-one`, `gateway`, or `core`. |
| `INTERNAL_CORE_SECRET` | unset | Shared secret that signs gateway-to-core requests. At least 32 characters. Must be the same on both containers. Required for `gateway` and `core`. |
| `TRACKSEERR_CORE_URL` | unset | Address of the core, for example `http://trackseerr-core:5251`. Required on the gateway. |
| `CORE_LAN_BIND` | `127.0.0.1` | Compose only. LAN IP that publishes the core's admin port. Never `0.0.0.0`. |

The gateway refuses to start if any of these are set on it: `PLEX_TOKEN`, `SUBSONIC_PASSWORD`, `SUBSONIC_API_KEY`, `JELLYFIN_API_KEY`, `LASTFM_API_KEY`, `LASTFM_API_SECRET`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `LIDARR_API_KEY`, `FEED_TOKEN`, `ADMIN_PASSWORD`, or any download client or indexer credential.

## First admin

| Variable | Default | Description |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | Name of the first local admin. |
| `ADMIN_PASSWORD` | unset | Creates the first local admin if none exists. At least 12 characters. Needed when there is no Plex server to sign in with. Ignored once an admin exists. |

## Media server

See [Media servers](MEDIA_SERVERS.md) for what each server supports.

| Variable | Default | Description |
|---|---|---|
| `MEDIA_SERVER` | automatic | `plex`, `jellyfin`, `subsonic`, or `none`. If unset: `plex` when `PLEX_URL` and `PLEX_TOKEN` are set, otherwise whatever you choose in **Settings > Media Management > Media Server**. When any media server variable is set, the environment wins and the page is read-only. |
| `PLEX_URL` | unset | Plex address, for example `http://192.168.1.100:32400`. |
| `PLEX_TOKEN` | unset | Plex token of the server owner. |
| `PLEX_MACHINE_IDENTIFIER` | automatic | Plex server ID. Only needed if your account can reach more than one server and the wrong one is picked. |
| `PLEX_MUSIC_SECTION` | first music library | Name of the Plex music library to use, for example `Music`. Not case-sensitive. If no music library has that name, TrackSeerr logs a warning and uses the first one. |
| `PLEX_VERIFY_SSL` | `1` | `0` turns off certificate checks for a self-signed Plex certificate. `IGNORE_SSL=1` does the same. |
| `JELLYFIN_URL` | unset | Jellyfin address, including any base path, for example `http://jellyfin:8096`. |
| `JELLYFIN_API_KEY` | unset | Jellyfin API key, from **Dashboard > API Keys** in Jellyfin. |
| `JELLYFIN_USER` | first admin | Jellyfin account that gets playlists with no target user. Name or ID. |
| `SUBSONIC_URL` | unset | Subsonic server address, for example `http://navidrome:4533`. |
| `SUBSONIC_USER` / `SUBSONIC_PASSWORD` | unset | Account TrackSeerr signs in as. The password is never sent; TrackSeerr uses salted tokens. |
| `SUBSONIC_API_KEY` | unset | OpenSubsonic API key, used instead of user and password if the server supports it. |

Set both `PLEX_URL` and `PLEX_TOKEN`, or neither. With only one set, TrackSeerr stops at startup and logs which one is missing. The same goes for `JELLYFIN_*` or `SUBSONIC_*` variables without `MEDIA_SERVER`.

## Playlist sync

| Variable | Default | Description |
|---|---|---|
| `SECONDS_TO_WAIT` | `86400` | Seconds between automatic playlist syncs. |
| `RUN_ONCE` / `CRON` | `0` | `1` runs one sync and exits. For use with an outside scheduler. |
| `SEARCH_SIMILARITY_THRESHOLD` | `0.9` | How close a track must be to count as a match, from `0.0` to `1.0`. |
| `APPEND_SERVICE_SUFFIX` | `1` | Add ` - Spotify` or ` - Deezer` to playlist names. |
| `ADD_PLAYLIST_POSTER` | `1` | Copy playlist artwork to the media server. Plex only. |
| `ADD_PLAYLIST_DESCRIPTION` | `1` | Copy playlist descriptions to the media server. Plex only. |
| `APPEND_INSTEAD_OF_SYNC` | `0` | `1` only adds tracks. Tracks removed from the source stay in the playlist. |
| `WRITE_MISSING_AS_CSV` | `0` | `1` writes a CSV of missing tracks to `/data` after each sync. |

## Spotify and Deezer

None of these are required. Public Spotify playlists import without any keys.

| Variable | Default | Description |
|---|---|---|
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | unset | Spotify developer app keys. Without them, TrackSeerr reads public playlist pages instead. |
| `SPOTIFY_USER_ID` | unset | Sync every public playlist of this Spotify user. |
| `SPOTIFY_PLAYLIST_ID` | unset | Comma-separated Spotify playlist IDs, URLs, or URIs to sync. |
| `DEEZER_USER_ID` | unset | Sync every public playlist of this Deezer user. |
| `DEEZER_PLAYLIST_ID` | unset | Comma-separated Deezer playlist IDs or URLs to sync. |

## Requests

| Variable | Default | Description |
|---|---|---|
| `AUTO_APPROVE_REQUESTS` | `0` | `1` approves every request on submit. To do this per user instead, give them the auto-approve permission. |
| `USER_REQUEST_QUOTA` | unset | Legacy. Read once, when the database is upgraded from an older version, to seed the default track and album quotas. Has no effect after that. |

Quotas are set in **Settings > Requests > Users**. See [Users and requests](USERS_AND_REQUESTS.md).

## Background jobs

| Variable | Default | Description |
|---|---|---|
| `ENABLE_RSS_SYNC` | `1` | Check indexer RSS feeds for new releases. |
| `RSS_SYNC_INTERVAL_MINUTES` | `15` | Minutes between RSS checks. |
| `ENABLE_BACKLOG_SEARCH` | `1` | Search for missing and below-cutoff music on a schedule. |
| `BACKLOG_SEARCH_INTERVAL_MINUTES` | `60` | Minutes between backlog searches. |
| `ENABLE_IMPORT_LISTS` | `1` | Sync import lists (Last.fm, ListenBrainz, MusicBrainz collections) when they are due. `0` syncs them only when you click sync. |

You can see and run every background job in **Settings > System > Tasks**.

## Lidarr

These only matter when Lidarr is the library manager. All of them can also be set in **Settings > Lidarr**. Values saved there win; these variables are the fallback. See [Downloading music: Lidarr](ACQUISITION.md#use-lidarr).

| Variable | Default | Description |
|---|---|---|
| `LIDARR_URL` | unset | Lidarr address, for example `http://lidarr:8686`. |
| `LIDARR_API_KEY` | unset | From Lidarr **Settings > General > Security**. |
| `LIDARR_ROOT_FOLDER` | automatic | Lidarr root folder for new artists. |
| `LIDARR_AUTO_SEARCH` | `1` | Search in Lidarr as soon as an album is added. |
| `LIDARR_AUTO_TRICKLE` | `0` | `1` sends missing playlist tracks to Lidarr on a schedule. |
| `LIDARR_AUTO_TRICKLE_INTERVAL_MINUTES` | `30` | Minutes between scheduled sends. |
| `LIDARR_TRICKLE_BATCH_SIZE` | `25` | Tracks per send. |
| `LIDARR_TRICKLE_RATE_SECONDS` | `3.0` | Seconds between artist lookups during a send. |

## Scrobbling

| Variable | Default | Description |
|---|---|---|
| `LASTFM_API_KEY` / `LASTFM_API_SECRET` | unset | Last.fm API account. Lets users connect their own Last.fm accounts. Also the default key for Last.fm import lists. Can be set in **Settings > Requests > Scrobbling** instead. When set here, these win and the fields in the UI are locked. |

## Other

| Variable | Default | Description |
|---|---|---|
| `FEED_TOKEN` | unset | Protects the missing-track feeds and the sync webhook. Callers pass it as `?token=`, `X-Api-Key`, or `Authorization: Bearer`. |
| `ITUNES_IMPORT_MAX_MB` | `100` | Largest iTunes library XML file accepted for import. |
| `MUSICBRAINZ_URL` | `https://api.brainzmash.cc` | MusicBrainz API used to look up IDs. Point it at your own mirror if you run one. |
| `ENABLE_API_DOCS` | `0` | `1` serves the interactive API docs at `/api/docs`. |
| `CORS_ORIGINS` | local addresses | Comma-separated origins allowed to call the API from a browser. Leave it alone unless you run the frontend on another address. |
| `CONFIG_DIR` / `DATA_DIR` | `/config` / `/data` | Change the container paths. Leave them alone in Docker. |

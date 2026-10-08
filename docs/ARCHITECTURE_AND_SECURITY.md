# Architecture and security

This page explains how TrackSeerr is put together and how it protects your library and accounts.

- [Parts](#parts)
- [How a request flows](#how-a-request-flows)
- [Two-container setup](#two-container-setup)
- [Security controls](#security-controls)
- [Reporting a vulnerability](#reporting-a-vulnerability)

## Parts

TrackSeerr is one Docker image. It contains:

- **A web app.** A React single-page app, served by the backend.
- **An API.** A Python FastAPI server. The web app uses it, and so can scripts.
- **Background workers.** Playlist sync, RSS sync, backlog search, download monitoring, import lists, scrobbling, seed cleanup, and others. See them in **Settings > System > Tasks**.
- **A SQLite database** in `/config`.

It talks to these outside services:

| Service | Used for |
|---|---|
| Media server (Plex, Jellyfin, Subsonic) | Playlists, rescans, users, library health |
| Download clients (qBittorrent, SABnzbd, slskd) | Downloading |
| Indexers (Torznab, Newznab) | Finding releases |
| Lidarr | Optional library manager |
| MusicBrainz, Deezer, Apple Music, Last.fm, ListenBrainz, AcoustID | Metadata, discovery, scrobbling, fingerprints |
| Spotify, Deezer | Playlist import |

## How a request flows

1. A user requests an album in the web app.
2. The request is checked against the user's quota and permissions. It is approved at once or waits for an admin.
3. Once approved, the library manager takes over:
   - **TrackSeerr:** it searches the indexers, filters and ranks the results with your profiles, and sends the best release to a download client.
   - **Lidarr:** TrackSeerr adds the album to Lidarr and Lidarr does the rest.
4. TrackSeerr watches the download. When it finishes, the files are checked, tagged, renamed, and moved into the music folder.
5. TrackSeerr asks the media server to rescan, adds the tracks to waiting playlists, marks the request available, and sends notifications.

## Two-container setup

In the single-container setup, the web app shares a process with every secret and with write access to your music. A flaw in the web app exposes all of it. For a private network that is an acceptable trade. For the internet it is not.

The two-container setup splits the app:

| | Gateway (`ROLE=gateway`) | Core (`ROLE=core`) |
|---|---|---|
| Faces | The internet, through your proxy | Your LAN only |
| Volumes | None | `/config`, `/data` |
| Secrets | Only `INTERNAL_CORE_SECRET` | All of them |
| Users | Sign in, discover, request, manage their own playlists and account | Admins |
| Admin pages | Not available | Available |

```
Internet
   |
Reverse proxy  (proxy network)
   |
Gateway        (proxy network + internal network)
   |
   |  signed requests, internal network, no internet access
   |
Core           (internal network + LAN)
   |
Media server, download clients, indexers, music folder
```

How the gateway and core trust each other:

- **Signed requests.** The gateway signs each call to the core with HMAC-SHA256 using `INTERNAL_CORE_SECRET`. The signature covers the method, path, user, a timestamp, a one-time nonce, and a hash of the body. The core rejects calls more than 60 seconds old and nonces it has already seen.
- **Never admin.** The core treats every forwarded call as a normal user, using the core's own record of that user's permissions and quota. The admin flag is always stripped. Admin access exists only on the core itself.
- **Deny by default.** The gateway only serves an explicit list of user endpoints. Every other API path returns 404.
- **No secrets on the gateway.** The gateway refuses to start if it finds a media server token, download client password, Lidarr key, Last.fm key, `FEED_TOKEN`, or `ADMIN_PASSWORD` in its environment. It also refuses to start on a core's database.
- **Limited view of the library.** The gateway can ask whether music is in the library and at what quality. It never sees file paths, sizes, or download details.

If the gateway is fully compromised, the attacker can act as an ordinary user on the allowed endpoints. They cannot reach your files, your download clients, your secrets, or admin functions.

Setup steps are in the [Install guide](INSTALL.md#two-containers).

## Security controls

### Outbound requests

TrackSeerr checks every address before it connects to it.

- **Service addresses** (download clients, indexers, media servers, Lidarr) must use `http` or `https`. LAN addresses are allowed, since these services usually run on your network. Cloud metadata addresses such as `169.254.169.254`, other schemes such as `file://`, and credentials inside the URL are refused.
- **Images** are only fetched over HTTPS on the standard port. Local, private, and cloud metadata addresses are refused.
- **Playlist links** are parsed down to a Spotify or Deezer playlist ID. TrackSeerr then builds the request itself; it never fetches the URL a user typed.

### Files

- **Fixed roots.** Any path from a user or admin is resolved and must fall inside an approved root: the music folder, the download clients' folders, the extra import folder, `/data`, or `/config`. Paths with `..` are refused.
- **Import checks.** Each downloaded file must start with the bytes of a real audio format that matches its extension, and must parse as audio. Files that fail go to quarantine.
- **Archive limits.** Archives with more than 5,000 files, more than 10 GB unpacked, or a compression ratio above 200 are refused. Members with absolute paths, `..`, or links are refused.
- **No silent deletes.** Deleted and replaced files go to the recycle bin unless you turn that off. Seed cleanup only deletes download files after several safety checks. See [Downloading music: seeding](ACQUISITION.md#seeding).
- **No shell.** TrackSeerr runs no shell commands or external programs. Everything is done in Python.

### Accounts

- **Passwords** are hashed with scrypt and a random salt. They must be 12 to 128 characters, must not contain the username, and must not be a common password.
- **Two-factor authentication** uses standard TOTP codes, with single-use recovery codes. Codes cannot be reused.
- **Lockout.** Five failures in 15 minutes lock an account for 15 minutes. Twenty failures from one IP in 15 minutes block the IP. Error messages do not reveal whether an account exists.
- **Invite and reset links** are random, single use, and expire after 48 hours. Only their hash is stored.
- **Sessions** are signed cookies (`HttpOnly`, `SameSite=Lax`). An admin can sign a user out everywhere.
- **Plex sign-in** only admits Plex accounts with access to your server.

### Secrets

- Secrets are never returned by the API after they are saved. The UI shows a placeholder instead.
- Tokens, API keys, and passwords are removed from log lines and error messages.
- `ADMIN_PASSWORD` is removed from the process environment after first use.

### API access

Scripts can call the API with an API key in the `X-Api-Key` header. Admins can read the key at `GET /api/settings/api-key` and replace it with `POST /api/settings/api-key/regenerate`. The interactive API docs are off unless you set `ENABLE_API_DOCS=1`.

`FEED_TOKEN` protects the missing-track feeds and the sync webhook.

### Container

- Based on `python:3.12-slim`.
- Starts as root only to set file ownership, then runs as `PUID:PGID`.
- Needs no Docker socket and no extra privileges.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).

The design notes behind these features are in [docs/design](design/).

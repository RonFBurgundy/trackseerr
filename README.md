<p align="center">
  <img src="unraid/trackseerr.png" alt="TrackSeerr Logo" width="180">
</p>

# TrackSeerr

[![CI](https://github.com/RonFBurgundy/trackseerr/actions/workflows/ci.yml/badge.svg)](https://github.com/RonFBurgundy/trackseerr/actions/workflows/ci.yml)
[![Docker](https://github.com/RonFBurgundy/trackseerr/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/RonFBurgundy/trackseerr/actions/workflows/docker-publish.yml)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE.md)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![Status: Work in Progress](https://img.shields.io/badge/status-active%20development-orange.svg)](#overview)

---

> [!WARNING]
> **Work in Progress: Active Development**
> TrackSeerr is currently under active development. Core features, API schemas, and deployment topologies are undergoing rapid implementation and refinement. Please stand by for upcoming tagged releases and milestone announcements before relying on this software in production homelab environments.

---

## Overview

**TrackSeerr** is a self-hosted music discovery, request, and library management suite for **Plex Media Server**, **Plexamp**, and homelab music pipelines. It combines an **Overseerr-style frontend** for family discovery and request management with an **Arr-style media management and acquisition backend**.

TrackSeerr allows Plex Home users to discover new music, listen to previews, submit requests within configured quotas, and import playlists from Spotify and Deezer directly into their personal Plexamp libraries. Administrators can fulfill requests using an existing Lidarr instance or using native acquisition drivers (slskd, SABnzbd, qBittorrent, and Torznab/Newznab indexers) with built-in Mutagen audio tagging and token template file organization.

> [!IMPORTANT]
> TrackSeerr matches existing media in your local Plex music library and manages acquisitions through configured download clients or Lidarr. It does not rip or scrape audio streams from third-party services.

---

## System Architecture

```
+-----------------------------------------------------------------------+
|                            Frontend Layer                             |
|  - Plex OAuth Authentication & Plex Home Multi-User Routing           |
|  - Zero-Key Music Discovery (Apple Music / iTunes & Deezer APIs)       |
|  - 30-Second Audio Previews & Trending Charts                         |
|  - Multi-User Request Engine (Quotas, Approval Queue, Lifecycle)      |
|  - Spotify & Deezer Playlist Sync (Keyless Scraper & Bookmarklet)     |
+-----------------------------------------------------------------------+
                                    |
                                    v
+-----------------------------------------------------------------------+
|                            Backend Layer                              |
|                                                                       |
|  Option A: Lidarr Mode              Option B: Native Library Mode     |
|  - Paced Trickle Worker             - 3-Tier Catalog & Monitoring     |
|  - Targeted Album Searches          - slskd, SABnzbd, qBittorrent     |
|  - Automated Webhook Loop           - Scanner, Importer & Renamer     |
+-----------------------------------------------------------------------+
                                    |
                                    v
+-----------------------------------------------------------------------+
|                    Media Management Pipeline                          |
|  - Mutagen Audio Inspection (FLAC, MP3 ID3, M4A/AAC, Ogg/Opus)        |
|  - Arr-Grade Token Naming ({Artist Name}/{Album Title}/{track:00})    |
|  - Cross-Device Safe Atomic Moves & Collision Protection              |
|  - Automated Plex Media Server Library Refresh Notifications          |
+-----------------------------------------------------------------------+
```

### Frontend: Discovery, Requests & Family Governance
- **Zero-Key Discovery**: Search albums and tracks, browse trending releases, and inspect Deezer and Apple Music charts without API keys or developer accounts.
- **Deep Album & Artist Browsing**: Click any album to view full tracklists with 30-second audio previews, or browse an artist's entire discography organized by Studio Albums, EPs/Singles, and Compilations.
- **Flexible Requests**: Request complete albums or cherry-pick specific individual tracks in a single batch submission.
- **Family Governance & Quotas**: Overseerr-style rolling request quotas (e.g. 5 requests per 7 days) and granular permission controls for family members.
- **Issue Reporting**: Family members can report audio glitches, corrupted files, or wrong versions directly into an admin triage queue.
- **Outbound Notifications**: Instant notifications via Discord (with rich embeds and cover art), Telegram, Pushover, generic Webhooks, and Email when music is requested, grabbed, or ready in Plex.
- **Personal & Household Playlists**: Sync public playlists, user playlists, or import Liked Songs using the 1-click browser bookmarklet. Target playlists to individual users, groups, or the whole household.

### Backend: Autonomous Acquisition & Media Management
TrackSeerr gives administrators the choice between two acquisition and library management workflows via the **Operational Mode Switch** (`LIBRARY_MODE=native|lidarr`):

- **`native` (Default)**: Standalone Arr-grade library coordinator managing full cataloging, monitoring, disk scanning, manual importing, batch renaming, and autonomous acquisition.
- **`lidarr`**: Overseerr-style gateway mode delegating file handling and download orchestration to Lidarr with zero split-brain collisions.

#### 1. Native Library Management & Catalog Engine
When running in `native` mode, TrackSeerr acts as a complete music library system:
- **Three-Tier Catalog Schema**: Backed by persistent SQLite tables (`library_artists`, `library_albums`, `library_tracks`, `library_files`) with granular monitoring toggles at artist, album, or individual track levels, supporting cascading monitoring inheritance.
- **Native Artist & Discography Ingestion**: Add artists directly from discovery into your native catalog prior to acquiring files, choose monitoring presets (`all`, `albums`, `singles_eps`, `none`), and trigger background discography refreshes when artists release new music.
- **Multi-Threaded Scanner & Smart Caching**: High-throughput `/music` scanner powered by a parallel Mutagen worker pool (up to 8 threads), file size/mtime change detection to skip unchanged files, and batched database writes for large collections.
- **Automated Catalog Synchronization**: Incoming downloads are atomically cataloged into `library_artists`, `library_albums`, `library_tracks`, and `library_files` upon placement, with multi-track album reconciliation (matching disc/track numbers, title similarity >= 0.85, and duration tolerances).
- **Interactive Manual Import Queue**: Staging directory scanner with fuzzy candidate matching, confidence ratings (0–100%), standardized audio tag writing, and configurable import modes (`move`, `hardlink`, `copy`).
- **Token Template Batch Renamer**: Scans existing library paths against your active Arr naming template, previews side-by-side path diffs, and executes atomic cross-mount renames.
- **1-Click Lidarr API Migration**: Background migration job pulling an existing Lidarr instance's artists, albums, tracks, track files, and MusicBrainz IDs (`MBIDs`) via REST API, automatically transitioning the instance into `native` mode upon completion.

#### 2. Native Autonomous Acquisition
Operate TrackSeerr as an independent downloader coordinator without running Lidarr:
- **Download Drivers**: Native connections to slskd (Soulseek P2P for surgical single/EP matching), SABnzbd (Usenet via Newznab), and qBittorrent (BitTorrent via Torznab).
- **15-Minute RSS Sync**: Automatically polls indexer RSS feeds to snatch new releases the moment they are uploaded.
- **Catalog-Driven Wanted Sweeps**: Background sweeps periodically check both user requests and monitored missing or below-cutoff tracks in your library until matching releases appear.
- **Persistent Download Blocklist**: Corrupt, password-protected, or stalled releases are automatically blacklisted (by hash, title, and release GUID) to prevent infinite re-snatch loops.
- **Custom Formats (CF) Regex Scoring**: Score releases with regex bonus and penalty weights (e.g. `Remaster`, `Vinyl`, `Web-DL`, `Censored`) and enforce minimum score thresholds (`min_score`).
- **Seeding Governance**: Configurable `seed_ratio_limit` and `seed_time_limit_minutes`. In `hardlink` mode, torrents remain seeding in qBittorrent until your ratio or time targets are met before cleanup.
- **Universal Machine API Key (`X-Api-Key`)**: Standardized API key authentication across all REST routes for external automation (Prowlarr, scripts).
- **Archive Extraction**: Automatically unpacks `.zip`, `.tar.gz`, and multi-part archives in download staging.
- **Quality Upgrades**: If music was grabbed in lower quality (e.g. MP3 320), TrackSeerr keeps looking for FLAC releases and upgrades your library files automatically when found.
- **Media Management**: Inspect tags with Mutagen, format destination folders using customizable token templates (e.g. `{Artist Name}/{Album Title} ({Release Year})/{track:00} - {Track Title}`), handle collisions safely, and notify Plex when imports finish.

#### 3. Lidarr Integration Mode
Connect to an existing Lidarr server. TrackSeerr groups missing tracks by artist and feeds Lidarr through a rate-limited background trickle worker, avoiding full discography downloads and protecting MusicBrainz from API rate limits. All file movement and organization is delegated to Lidarr.

---

## Deployment Topologies

TrackSeerr supports two deployment models depending on your infrastructure:

### 1. Single-Container Mode (`ROLE=all-in-one`)
The default homelab configuration. The web UI, API, sync scheduler, and download organizer run within a single container. Suitable for private LANs, VPN access, and standard Unraid setups.

### 2. Hardened Two-Tier DMZ Mode (`docker-compose.hardened.yml`)
For exposed environments, TrackSeerr can be deployed across two isolated network tiers:

- **Tier 1 (Public Gateway - `trackseerr-gateway`)**: Placed on the reverse proxy network (`proxynet`). Completely stateless with zero volume mounts, zero media storage access, and zero downloader credentials. Handles public ingress, Plex OAuth, discovery queries, and request submissions.
- **Tier 2 (Internal Core - `trackseerr-core`)**: Isolated on the internal network with no public port exposure. Mounts `/data`, `/music`, and `/downloads`. Manages downloader credentials, indexers, file organization, Mutagen inspection, and Plex library refresh pings.

*For complete details on threat boundaries and configuration, see the [Architecture and Security Reference](docs/ARCHITECTURE_AND_SECURITY.md).*

---

### Choosing a deployment

Use **all-in-one** (`ROLE=all-in-one`) when TrackSeerr is only reachable over your LAN, a VPN, or Tailscale. Use **TrackSeerr Core + TrackSeerr Requests** whenever the request app faces the internet (reverse proxy, Cloudflare Tunnel, port forward).

In all-in-one mode the public web process shares a container with your Plex token, downloader credentials, Last.fm keys, and read/write access to your library, so any exploit of the web layer reaches all of it. In the split setup the public container (Requests) holds none of that: no volumes, no secrets beyond a signing key, and it can only perform requester actions on Core. A compromise of Requests exposes request data, not your library or admin controls.

| Template | `ROLE` | Holds | Faces |
|---|---|---|---|
| `unraid/trackseerr.xml` | `all-in-one` | Everything | LAN/VPN only |
| `unraid/trackseerr-core.xml` | `core` | DB, `/config`, `/data`, Plex, Last.fm, downloaders | LAN admin port only |
| `unraid/trackseerr-requests.xml` | `gateway` | Core URL, shared secret, public URL | Internet via proxy/tunnel |

### Networking

The two containers talk over a dedicated **internal network** (default name `trackseerr-internal`, override with `TRACKSEERR_INTERNAL_NETWORK` in the compose file; on Unraid just pick any name and use it on both containers). It coexists with your existing networks rather than replacing them:

- **Requests** joins the internal network *and* the network your reverse proxy lives on (`proxynet` or a custom bridge; `PROXY_NETWORK` in the compose file, declared `external: true`).
- **Core** joins the internal network *and* its normal LAN/bridge/`br0` network so it can reach Plex and your downloaders and publish its admin port (`CORE_LAN_BIND`, default `127.0.0.1`).
- **Cloudflare Tunnel:** `cloudflared` must share a network with **Requests**, never with Core. Point the tunnel at `http://trackseerr-gateway:5250` (your Requests container name).
- **Reverse proxies** (SWAG, NPM, Traefik, Caddy) only need to reach Requests. Set `TRUSTED_PROXIES` on Requests to the proxy's IP/CIDR.

On Unraid, create the network once (`docker network create trackseerr-internal`), then add `--network=trackseerr-internal` to Extra Parameters of both containers (needs Docker 25+, Unraid 7). On older versions, run `docker network connect trackseerr-internal <container>` after the container starts.

#### Multi-network verification

Verified on Docker 29.8.1 with `alpine`. This was not tested on Unraid itself.

```bash
docker network create --internal ts-verify-internal
docker network create ts-verify-lan
# core-like container on BOTH networks in a single `docker run` (Docker 25+)
docker run -d --name ts-verify-core --network ts-verify-internal --network ts-verify-lan alpine:latest \
  sh -c 'while true; do printf "HTTP/1.0 200 OK\r\n\r\ncore-ok\n" | nc -l -p 5251; done'
# gateway-like container on the internal network only
docker run -d --name ts-verify-gw --network ts-verify-internal alpine:latest sleep 300

docker exec ts-verify-gw wget -q -T5 -O- http://ts-verify-core:5251/   # -> core-ok (name resolution + traffic over internal net)
docker exec ts-verify-gw wget -q -T5 -O- http://1.1.1.1/               # -> fails: Network unreachable (no egress)
docker exec ts-verify-core wget -q -T5 -O- http://1.1.1.1/             # -> succeeds (core egress via LAN network)

# fallback for hosts whose UI allows only one network:
docker run -d --name ts-verify-late --network ts-verify-lan alpine:latest sleep 300
docker network connect ts-verify-internal ts-verify-late                # -> ts-verify-late can now reach ts-verify-core:5251
```

### Migrating from all-in-one

The core is your existing container with a role change: same image, `/config` and database, nothing to export or import.

1. Generate a secret: `openssl rand -hex 32`.
2. Edit the existing container (or its compose service): set `ROLE=core`, `INTERNAL_CORE_SECRET=<secret>`, and `APPLICATION_URL` to the **public Requests URL**. Map the admin port (5251) and add the internal network.
3. Apply. On first boot as core, TrackSeerr logs a one-time checklist and shows it as a dismissible banner in the admin UI.
4. Create the **TrackSeerr Requests** container (template or compose) with the same secret, `TRACKSEERR_CORE_URL=http://<core container>:5251`, the same `APPLICATION_URL`, on the proxy network and the internal network.
5. Point your reverse proxy or tunnel at Requests instead of the old container.
6. The Plex webhook URL is unchanged. Users sign in once more on Requests. Reverting is the same edit back to `ROLE=all-in-one`.

### `init-dmz`

`init-dmz` generates the secret, a `docker-compose.dmz.yml` and a `.env` (mode 0600) and prints the exact Unraid values for both templates. It never overwrites existing files (it writes `<name>.new` instead) and makes no network calls.

```bash
python -m plex_playlist_sync init-dmz \
  [--from-existing] [--env-file PATH] [--public-url URL] \
  [--core-lan-bind IP] [--network NAME] [--out DIR]
```

- `--from-existing`: carry non-secret settings from the running all-in-one container's environment (or `--env-file PATH`) into the core service; secrets such as `PLEX_TOKEN` are referenced as `${PLEX_TOKEN}` with the value only in `.env`.
- `--env-file PATH`: read the existing settings from this file instead.
- `--public-url URL`: the public Requests URL (`APPLICATION_URL`).
- `--core-lan-bind IP`: LAN IP for Core's admin port (default `127.0.0.1`; `0.0.0.0` is rejected).
- `--network NAME`: internal network name (default `trackseerr-internal`).
- `--out DIR`: output directory (default `.`).

---

## Quick Start

### Unraid Deployment

Open your Unraid Terminal and download the template:

```bash
curl -o /boot/config/plugins/dockerMan/templates-user/my-trackseerr.xml \
  https://raw.githubusercontent.com/RonFBurgundy/trackseerr/main/unraid/trackseerr.xml
```

For the split setup, install `trackseerr-core.xml` and `trackseerr-requests.xml` the same way (see [Choosing a deployment](#choosing-a-deployment)). They create containers named `TrackSeerr-Core` and `TrackSeerr-Requests` (Docker names cannot contain spaces), so on Unraid `TRACKSEERR_CORE_URL` is `http://TrackSeerr-Core:5251`. The compose file names them `trackseerr-core` / `trackseerr-gateway` instead, so there it is `http://trackseerr-core:5251`. If you rename either container, update the URL to match.

Navigate to **Docker** -> **Add Container** -> select **my-trackseerr** from the **Template** dropdown, verify your Plex server IP address, and click **Apply**.

*Read the [Unraid Installation Guide](docs/UNRAID_INSTALL_GUIDE.md) for full walkthroughs and storage mount instructions.*

---

### Docker Compose: Single-Container Mode

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
      # TRaSH Guides unified data share (holds /data/media/music and /data/downloads)
      - /path/to/data:/data
    environment:
      - ROLE=all-in-one
      - PORT=5250
      - PUID=1000
      - PGID=1000
      - UMASK=022
      - PLEX_URL=http://192.168.1.100:32400
      - PLEX_TOKEN=your_plex_token_here
      - PLEX_MUSIC_SECTION=Music
      - PLEX_VERIFY_SSL=1
      - SECONDS_TO_WAIT=14400 # 4 hours
      - LOG_LEVEL=INFO
      # Spotify: Leave blank to use the built-in keyless web scraper
      - SPOTIFY_CLIENT_ID=
      - SPOTIFY_CLIENT_SECRET=
      # Note: Lidarr, download clients, indexers, and quotas are configured in the WebUI Settings
```

Run with:
```bash
docker compose up -d
```

Access the dashboard at `http://<your-server-ip>:5250`.

---

### Docker Compose: Hardened Two-Tier DMZ Mode

```yaml
services:
  trackseerr-gateway:
    image: ghcr.io/ronfburgundy/trackseerr:latest
    container_name: trackseerr-gateway
    restart: unless-stopped
    ports:
      - "5250:5250"
    environment:
      - ROLE=gateway
      - INTERNAL_CORE_SECRET=${INTERNAL_CORE_SECRET:?set a 32+ char secret (openssl rand -hex 32)}
      - TRACKSEERR_CORE_URL=http://trackseerr-core:5251
      - APPLICATION_URL=https://trackseerr.yourdomain.com
      - PORT=5250
      - PUID=1000
      - PGID=1000
      - UMASK=022
      - LOG_LEVEL=INFO
    networks:
      - proxynet
      - internal

  trackseerr-core:
    image: ghcr.io/ronfburgundy/trackseerr:latest
    container_name: trackseerr-core
    restart: unless-stopped
    ports:
      - "${CORE_LAN_BIND:-127.0.0.1}:5251:5251"
    volumes:
      - ./appdata:/config
      - /path/to/data:/data
    environment:
      - ROLE=core
      - CORE_LAN_BIND=${CORE_LAN_BIND:-127.0.0.1}
      - INTERNAL_CORE_SECRET=${INTERNAL_CORE_SECRET:?set a 32+ char secret (openssl rand -hex 32)}
      - PORT=5251
      - PUID=1000
      - PGID=1000
      - UMASK=022
      - PLEX_URL=http://192.168.1.100:32400
      - PLEX_TOKEN=your_plex_token_here
      - PLEX_MUSIC_SECTION=Music
      - PLEX_VERIFY_SSL=1
      - SECONDS_TO_WAIT=14400
      - SEARCH_SIMILARITY_THRESHOLD=0.9
      - LOG_LEVEL=INFO
    networks:
      - internal
      - core-lan

networks:
  proxynet:
    name: ${PROXY_NETWORK:-proxynet}
    external: true
  internal:
    name: ${TRACKSEERR_INTERNAL_NETWORK:-trackseerr-internal}
    internal: true
  core-lan:
    name: trackseerr-core-lan
    driver: bridge
```

#### Hardened two-tier (DMZ) deployment

The split mirrors Seerr and Lidarr. The **gateway** is Seerr-style: internet-facing, stateless, no volumes, and no `PLEX_TOKEN`. The **core** is Lidarr-style: it holds your library, downloaders, and Plex token, and its admin UI is LAN-only.

**Signed least-privilege model.** The gateway signs every request it sends to core with `INTERNAL_CORE_SECRET`, and core only grants it the narrow, non-admin actions of a requester (discover, request, sign in). Admin routes are never reachable through the gateway, so a compromised gateway can never act as admin.

1. Generate the secret once and put it in the same `.env` for both services: `echo "INTERNAL_CORE_SECRET=$(openssl rand -hex 32)" >> .env`
2. Set `CORE_LAN_BIND` to the host's LAN IP to reach the admin UI from your LAN or VPN (default is `127.0.0.1`). Never expose port 5251 to the internet.
3. Admins use the core UI at `http://<CORE_LAN_BIND>:5251`; everyone else uses the gateway.
4. **Plex webhook:** copy the URL from Core's **Settings -> Scrobbling**. It points at the core LAN address, so Plex must be able to reach core on the LAN. Webhooks require Plex Pass; without it, history polling covers scrobbling.
5. **Last.fm keys (`LASTFM_API_KEY` / `LASTFM_API_SECRET`) go on core only.** The gateway must never receive `PLEX_TOKEN`, `LASTFM_API_SECRET`, or volumes.

---

### Internet Exposure, Reverse Proxies & Application URL

TrackSeerr includes a first-class **Application URL** setting (configurable in the WebUI under **Settings -> General**, or via the `APPLICATION_URL` environment variable). This setting defines the canonical public-facing domain (e.g. `https://trackseerr.yourdomain.com`) used for:
- **Notification Link-backs**: Outbound alerts on Discord, Telegram, Pushover, Email, and Webhooks include clickable links directing users back to TrackSeerr on your external domain.
- **Plex OAuth Redirects**: Sets the Plex `forwardUrl` callback so users are redirected straight back to your custom domain after authenticating with Plex.
- **Reverse Proxy / Cloudflare Tunnel Routing**: Coordinates external routing, preventing broken redirects and mismatched landing paths.

> [!WARNING]
> **Security Advisory: Internet Exposure & Reverse Proxies**
> If you are pointing a public domain or Cloudflare Tunnel to TrackSeerr, **we strongly advise deploying in Hardened Two-Tier DMZ Mode (`docker-compose.hardened.yml`)** rather than the monolithic `all-in-one` mode.
> 
> In monolithic `all-in-one` mode, a single container holds both the public web gateway and direct filesystem access to `/music`, `/downloads`, and downloader API keys. In contrast, Two-Tier DMZ mode splits TrackSeerr into:
> - **Tier 1 (`trackseerr-gateway`)**: Exposed to the reverse proxy / DMZ network with **zero filesystem volume mounts**, **zero media access**, and **zero downloader credentials**.
> - **Tier 2 (`trackseerr-core`)**: Isolated on a private internal bridge with no public ports, holding your media libraries, Mutagen scanner, and download clients safely behind the network boundary.

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `ROLE` | `all-in-one` | Container execution mode: `all-in-one`, `gateway`, or `core` |
| `APPLICATION_URL` | *Optional* | Canonical external URL (e.g. `https://trackseerr.yourdomain.com`) for notifications, Plex OAuth redirects, and reverse proxies |
| `TRACKSEERR_CORE_URL` | *None* | Core endpoint URL required when running in `gateway` mode |
| `TRUSTED_PROXIES` | *None* | Comma-separated IPs/CIDRs of reverse proxies (e.g. `172.18.0.0/16,10.0.0.5`). `X-Forwarded-For` is used for the client IP (local-login throttling and lockout) only when the direct peer is in this list; otherwise the peer address is used. Set it on the gateway (or all-in-one) when running behind a proxy, otherwise every user shares the proxy's IP |
| `INTERNAL_CORE_SECRET` | *Required for gateway/core* | Shared secret (at least 32 characters, e.g. `openssl rand -hex 32`) used to sign gateway-to-core requests. Must be identical on both tiers |
| `CORE_LAN_BIND` | `127.0.0.1` | Compose-only: host address core publishes port `5251` on. Set to the host's LAN IP; never expose to the internet |
| `LIBRARY_MODE` | `native` | Operational mode: `native` for full TrackSeerr catalog & library management, or `lidarr` for external Lidarr delegation |
| `PORT` | `5250` | Port for the web service |
| `HOST` | `0.0.0.0` | Host binding interface |
| `PUID` / `PGID` | `1000` / `1000` | User and group ID for filesystem operations (`99`/`100` on Unraid) |
| `UMASK` | `022` | File creation permissions mask |
| `PLEX_URL` | *Required* | Base URL to your Plex Media Server (e.g. `http://192.168.1.100:32400`) |
| `PLEX_TOKEN` | *Required* | Plex administrator `X-Plex-Token` |
| `PLEX_MUSIC_SECTION` | `Music` | Plex music library section name |
| `PLEX_MACHINE_IDENTIFIER` | *Auto* | Plex machine ID for pinning multi-user access |
| `PLEX_VERIFY_SSL` | `1` | Set to `0` or `false` to disable SSL certificate verification |
| `SECONDS_TO_WAIT` | `86400` | Seconds between automated background playlist sync cycles |
| `RUN_ONCE` / `CRON` | `0` | Set to `1` to run a single sync pass and exit |
| `SEARCH_SIMILARITY_THRESHOLD` | `0.9` | Fuzzy match ratio (0.0 to 1.0) for track matching |
| `APPEND_SERVICE_SUFFIX` | `1` | Append ` - Spotify` or ` - Deezer` to synced playlist names |
| `ADD_PLAYLIST_POSTER` | `1` | Sync playlist artwork to Plex playlists |
| `ADD_PLAYLIST_DESCRIPTION` | `1` | Sync playlist descriptions to Plex playlists |
| `APPEND_INSTEAD_OF_SYNC` | `0` | Set to `1` to append tracks without removing deleted ones |
| `WRITE_MISSING_AS_CSV` | `0` | Export missing track CSV reports to `/data` |
| `AUTO_APPROVE_REQUESTS` | `0` | Set to `1` to automatically approve user music requests |
| `USER_REQUEST_QUOTA` | `25` | Maximum number of active/pending requests allowed per user |
| `SPOTIFY_CLIENT_ID` | *Optional* | Spotify Developer Client ID (leave blank for keyless scraper) |
| `SPOTIFY_CLIENT_SECRET` | *Optional* | Spotify Developer Client Secret |
| `SPOTIFY_USER_ID` | *Optional* | Spotify user profile ID to mirror all playlists |
| `SPOTIFY_PLAYLIST_ID` | *Optional* | List of Spotify playlist IDs, URLs, or URIs to sync |
| `DEEZER_USER_ID` | *Optional* | Deezer numerical user ID to mirror playlists |
| `DEEZER_PLAYLIST_ID` | *Optional* | List of Deezer numerical playlist IDs or URLs |
| `LIDARR_URL` | *Optional* | Base URL to Lidarr server (e.g. `http://192.168.1.100:8686`) |
| `LIDARR_API_KEY` | *Optional* | Lidarr API Key |
| `LIDARR_AUTO_SEARCH` | `1` | Trigger interactive search when queuing missing tracks |
| `LIDARR_TRICKLE_RATE_SECONDS` | `3.0` | Seconds between artist lookups during trickle push |
| `LIDARR_TRICKLE_BATCH_SIZE` | `25` | Number of tracks per manual chunk or scheduled drip |
| `LIDARR_AUTO_TRICKLE` | `0` | Set to `1` to enable scheduled background trickle runs |
| `LIDARR_AUTO_TRICKLE_INTERVAL_MINUTES` | `30` | Interval in minutes between scheduled trickle runs |
| `LIDARR_ROOT_FOLDER` | *Auto* | Custom Lidarr root folder path override |
| `LIDARR_QUALITY_PROFILE_ID` | *Auto* | Custom Lidarr quality profile ID override |
| `LIDARR_METADATA_PROFILE_ID` | *Auto* | Custom Lidarr metadata profile ID override |
| `ENABLE_BACKLOG_SEARCH` | `1` | Periodically sweep unfulfilled requests and missing tracks |
| `BACKLOG_SEARCH_INTERVAL_MINUTES` | `60` | Interval in minutes between automated backlog search sweeps |
| `ENABLE_RSS_SYNC` | `1` | Periodically poll Torznab/Newznab indexers for new releases |
| `RSS_SYNC_INTERVAL_MINUTES` | `15` | Interval in minutes between indexer RSS sync loops |
| `LASTFM_API_KEY` | *Optional* | Last.fm API key enabling one-click per-user Last.fm scrobbling. When set (together with the secret) it overrides the value saved in Settings and locks those fields |
| `LASTFM_API_SECRET` | *Optional* | Last.fm API shared secret paired with `LASTFM_API_KEY`; used only server-side to sign requests and never returned by the API |
| `FEED_TOKEN` | *Optional* | Secret token protecting RSS feeds, plain text lists, and webhooks |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## Documentation Guides

- [Architecture and Security Reference](docs/ARCHITECTURE_AND_SECURITY.md): Deep dive into the two-tier DMZ isolation, SSRF defenses, token engine, and Mutagen pipeline.
- [Unraid Installation Guide](docs/UNRAID_INSTALL_GUIDE.md): Step-by-step setup using Unraid templates and community plugins.
- [Acquisition and Automation Guide](docs/LIDARR_AND_AUTOMATION_GUIDE.md): Detailed configuration for native drivers (slskd, SABnzbd, qBittorrent) and Lidarr trickle mode.
- [Spotify Import & Keyless Sync Guide](docs/SPOTIFY_IMPORT_GUIDE.md): Importing personal Spotify playlists, Liked Songs, and using the 1-click browser bookmarklet.

---

## Security Policy

Security and least privilege are central to TrackSeerr's design. All user-supplied URLs are strictly validated against whitelists before dispatch, input file operations are sandboxed against path traversal, and no shell commands or subprocesses are executed from API endpoints.

See [SECURITY.md](SECURITY.md) and the [Architecture and Security Reference](docs/ARCHITECTURE_AND_SECURITY.md) for full details.

---

## Lineage and Open Source Acknowledgments

TrackSeerr was originally conceived from [rnagabhyrava/plex-playlist-sync](https://github.com/rnagabhyrava/plex-playlist-sync). We extend our sincere gratitude to the original author for the foundational playlist matching concept.

TrackSeerr has been re-architected and expanded into an independent music discovery and acquisition manager. We gratefully acknowledge the design inspiration and open-source foundations provided by:

- **Overseerr**: Architectural inspiration for discovery, quotas, and multi-user request management.
- **Lidarr and Sonarr (*arr Stack)**: Standards for token template naming, quality tracking, and downloader lifecycle management.
- **Mutagen**: Cross-platform audio stream tag inspection and metadata extraction.
- **slskd**: REST API implementation of the Soulseek network, enabling single-track and album surgical discovery.

---

## License

GNU General Public License v3 (GPL-3.0). See [LICENSE.md](LICENSE.md) for details.

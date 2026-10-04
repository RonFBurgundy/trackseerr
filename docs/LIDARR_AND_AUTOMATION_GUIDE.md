# Acquisition and Automation Guide

TrackSeerr provides two distinct methods for acquiring missing music and fulfilling requests:

1. **Native Acquisition Drivers**: TrackSeerr acts as its own acquisition manager, connecting directly to slskd (Soulseek P2P), SABnzbd (Usenet), qBittorrent (BitTorrent), and Torznab/Newznab indexers. TrackSeerr manages the transfer queue, inspects audio files with Mutagen, formats file paths using an Arr-grade token engine, and places files into `/music`.
2. **Lidarr Integration**: TrackSeerr connects to an existing Lidarr instance via its REST API, using a paced trickle worker to monitor specific missing albums without triggering full artist discography downloads or overwhelming MusicBrainz.

Both approaches support automated self-healing, where newly acquired tracks are matched in Plex and added directly to user playlists.

---

## Architecture Comparison

| Capability | Native Library Mode (TrackSeerr) | Lidarr Integration Mode |
|---|---|---|
| Library cataloging & browsing | Yes (Built-in Library tab & REST API) | Via Lidarr WebUI |
| Granular monitoring | Full hierarchy (Artist, Album, & Track level) | Artist & Album level only |
| Native artist & discography ingestion | Yes (Add artist & fetch discography pre-acquisition) | Yes |
| Filesystem scanner & drift detection | Yes (Recursive `/music` Mutagen scanner with caching) | Delegated to Lidarr disk scan |
| Interactive manual import | Yes (Glass modal with confidence ratings) | Via Lidarr Manual Import |
| 1-click Lidarr migration importer | Yes (Extracts catalog, MBIDs & files via API) | N/A |
| Single-track surgical matching | Yes (via slskd) | No (Lidarr operates at album level) |
| Usenet acquisition | Yes (via SABnzbd + Newznab) | Yes (via Lidarr download clients) |
| Torrent acquisition | Yes (via qBittorrent + Torznab) | Yes (via Lidarr download clients) |
| Custom Formats (CF) regex scoring | Yes (Score bonuses, penalties & min score) | Yes |
| Seeding ratio & time governance | Yes (`seed_ratio_limit` & `seed_time_limit_minutes`) | Yes |
| Persistent download blocklist | Yes (Blocks corrupt/stalled releases from re-snatch) | Yes |
| Tag inspection | Mutagen (FLAC, MP3, M4A, Opus) | Lidarr internal tagger |
| File renaming and moving | Built-in token template engine | Lidarr media management |
| External dependencies | Downloader daemons only | Full Lidarr container and database |
| Volume mounts required | `/music` and `/downloads` | None on TrackSeerr (handled by Lidarr) |

---

## Operational Modes: Native TrackSeerr vs External Lidarr

TrackSeerr's operational behavior is controlled by the `LIBRARY_MODE` setting (`native` or `lidarr`), configurable via environment variable or in the Web UI under **Settings** -> **Media Management** -> **Library Management Mode**.

### Mode Behavior Comparison

| Dimension | `LIBRARY_MODE=native` (Default) | `LIBRARY_MODE=lidarr` |
|---|---|---|
| **Role** | Standalone Arr-grade media manager & coordinator | Request & discovery gateway for Lidarr |
| **Catalog Authority** | TrackSeerr SQLite (`library_artists`, `albums`, `tracks`, `files`) | Lidarr SQLite / database |
| **Acquisition Routing** | Native drivers (slskd, SABnzbd, qBittorrent) | Paced trickle worker pushes to Lidarr |
| **Media Operations** | Internal scanner, manual importer, batch renamer | Delegated exclusively to Lidarr |
| **Plex Integration** | Direct refresh notifications from TrackSeerr | Triggered by Lidarr or TrackSeerr webhook |

### Tab Visibility and User Experience
- **Native Mode**: The **Library** tab is unlocked across desktop transport bays and mobile navigation drawers for all authenticated users (and administrators). Family members and administrators can browse existing catalog artists, albums, and tracks with live playback quality indicators and cutoff status badges.
- **Lidarr Mode**: The Library tab is concealed from standard users in navigation drawers to streamline the experience into an Overseerr-style request and discovery portal. Administrators retain access to management and settings, while missing music is routed directly through Lidarr's acquisition pipeline.

### Split-Brain Collision Prevention
Running two autonomous media organizers against the same filesystem leads to critical race conditions:
1. **File Locking & Incomplete Scans**: If TrackSeerr and Lidarr simultaneously attempt to rename, tag, or move incoming files, cross-process write locks will fail or corrupt audio tags.
2. **Naming Discrepancies**: Different token formatting templates cause ping-pong renames between tools.
3. **Database Drift**: Files moved or deleted by one tool become ghost records in the other.

When set to `LIBRARY_MODE=lidarr`, TrackSeerr automatically disables native background filesystem scans, locks disk mutation routes (`/manual-import/commit`, `/rename/apply`), and operates purely as an ingress and trickle gateway. This boundary guarantees zero split-brain collisions.

---

## Native Library Management Suite

In `native` mode, TrackSeerr exposes an Arr-grade media management interface via the **Library** dashboard.

### 1. Library Dashboard & Metric Ribbon
The top of the Library tab displays a real-time 6-metric summary ribbon:
- **Artists**: Total catalog artists, annotated with monitored count.
- **Albums**: Total indexed albums.
- **Tracks**: Total cataloged tracks, annotated with monitored count.
- **Disk Files**: Total physical audio files linked in `library_files`.
- **Below Cutoff**: Tracks currently below the active Quality Profile cutoff, flagged with amber badges indicating available upgrade opportunities.
- **Total Storage**: Aggregated physical byte footprint formatted with human-readable binary prefixes (GB/TB).

A transport toolbar provides sub-tab switching between **Artists**, **Albums**, and **Tracks**, a 300ms debounced search filter, a monitoring filter dropdown (`All` vs `Monitored Only`), and administrative action triggers: **Scan Disk**, **Manual Import**, **Rename Files**, and **Import Lidarr**.

### 2. Hierarchical Catalog Browsing & Monitoring
TrackSeerr maintains a 3-tier catalog hierarchy:
- **Artists Sub-Tab**: Rendered as responsive cards displaying artist names, album and track counts, and an interactive tactile toggle switch. Toggling artist monitoring cascades through all child albums and tracks.
- **Albums Sub-Tab**: Card grid featuring lazy-loaded cover art (with `/static/placeholder.svg` fallbacks), release year, track counts, drill-down buttons, and album-level monitoring toggles.
- **Tracks Sub-Tab**: Tabular view displaying track numbers, track titles, artist and album links, audio format badges (FLAC, MP3, AAC, Missing), quality cutoff indicators (**Meets Cutoff**, **Below Cutoff**, **Missing File**), inline monitoring toggles, and manual upgrade triggers opening the interactive release browser.

### 3. Native Artist Ingestion & Discography Ingestion
In addition to scanning existing files on disk, TrackSeerr allows administrators to ingest artists directly from discovery into the native catalog before downloading media:
- **Adding an Artist**: Query discovery by name or provider ID, choose the desired root folder, and select a monitoring preset:
  - `all`: Monitors full discography (studio albums, singles, EPs, compilations).
  - `albums`: Monitors studio albums only, leaving singles and compilations unmonitored.
  - `singles_eps`: Monitors only singles and EP releases.
  - `none`: Ingests the artist and discography for browsing without initiating automated downloads.
- **Tracklist Population**: Monitored releases fetch full tracklists into `library_tracks`. Unacquired tracks immediately enter the "Missing" catalog queue.
- **Discography Refresh**: When an artist drops new music, dispatching `POST /api/library/artists/{artist_id}/refresh` scans upstream discovery, detects new releases, and appends them to your catalog according to the artist's monitoring rules without overwriting existing files.

### 4. High-Throughput Filesystem Scanner (`/music`)
TrackSeerr includes a non-destructive recursive filesystem scanner designed to index and synchronize large media libraries:
- **Triggering**: Click **Scan Disk** in the Library toolbar or dispatch `POST /api/library/scan`.
- **Multi-Threaded Mutagen Pool**: Audio tag and stream metric extraction runs across a parallel worker pool (up to 8 threads), dramatically accelerating scans on multi-core processors.
- **Size & mtime Caching**: Files already registered in the catalog whose file size and modification timestamps match are skipped, cutting subsequent scan times down to seconds.
- **Batched Transactions**: Catalog writes are buffered in batches of 100 within atomic SQLite transactions, preventing lock contention and disk churn.
- **Catalog Synchronization**: Automatically creates or links records across `library_artists`, `library_albums`, `library_tracks`, and `library_files`.
- **Quality Cutoff Evaluation**: Compares technical stream metrics against the configured Quality Profile (e.g. FLAC 16-bit / 24-bit, MP3 320), marking `cutoff_met` accordingly.
- **Pruning Missing Files**: When triggered with `prune_missing=True`, the scanner removes orphaned `library_files` and cleans up empty albums or artists if disk files were deleted externally.
- **Plex Library Refresh**: Once the scan cycle completes, TrackSeerr automatically signals Plex Media Server to refresh the music library section.
- **Async Execution & Cancellation**: Runs in a managed background thread with live status polling via `GET /api/library/scan/status` and instant cancellation support via `POST /api/library/scan/cancel`.

### 5. Interactive Manual Import Queue
For media from external downloads, CD rips, or unorganized staging directories:
1. Click **Manual Import** in the Library toolbar to open the glass modal.
2. Enter the folder path within `/downloads` or approved media mounts and click **Scan Folder** (`POST /api/library/manual-import/scan`).
3. TrackSeerr reads the audio tags of all files and executes fuzzy matching against your catalog, assigning a match confidence score (0–100%).
4. The candidate table displays detected artist, album, track, format badge, and confidence rating. Operators can reassign metadata or select candidate rows.
5. Select the **Import Mode**:
   - `move`: Safely relocates the file into the library structure.
   - `hardlink`: Creates hardlinks on supported filesystems (TRaSH Guides single-share structure).
   - `copy`: Duplicates the file, preserving original downloads for seeding.
6. Optional **Write Standardized Tags**: Check the box to rewrite normalized ID3v2.4 or Vorbis tags using Mutagen before moving.
7. Click **Import Selected Files** (`POST /api/library/manual-import/commit`) to execute atomic cross-mount moves and register the files in the catalog.

### 6. Token Template Batch Renamer
To fix non-standard filenames or migrate to a new naming convention:
1. Click **Rename Files** in the Library toolbar to open the renamer modal (`POST /api/library/rename/preview`).
2. TrackSeerr compares disk paths for all cataloged files against your configured Arr naming template (e.g. `{Artist Name}/{Album Title} ({Release Year})/{track:00} - {Track Title}{[ (Quality Full)]}`).
3. A diff table renders existing paths alongside proposed target paths, highlighting modified directories or filenames.
4. Filter by specific artist or album, or select all files requiring rename.
5. Click **Apply Renames** (`POST /api/library/rename/apply`). TrackSeerr validates target paths against directory traversal, applies collision protection, executes cross-device safe atomic moves, updates database records, and notifies Plex.

### 7. Quality Cutoffs, Custom Formats & Automated Upgrades
TrackSeerr prevents stagnant low-quality audio using Arr-grade release evaluation:
- **Quality Cutoff Tracking**: When a track is acquired in lower quality (e.g. MP3 128kbps or 320kbps), it is tagged `cutoff_unmet=True`. It remains monitored in the background while remaining fully playable in Plex.
- **Custom Formats (CF) Regex Scoring**: Score releases with regex bonuses and penalties. Prefer `Remaster`, `Vinyl`, or `Web-DL`, penalize `Live` or `Censored`, and enforce strict minimum score thresholds (`min_score`).
- **Catalog-Driven Upgrades**: During scheduled 15-minute RSS indexer syncs and hourly backlog sweeps, TrackSeerr prioritizes releases that satisfy the configured Quality Profile cutoff (such as lossless FLAC) or yield higher Custom Format scores.
- **Atomic Upgrade Replacement**: When a higher-quality release is imported, TrackSeerr replaces the lower-quality file, updates `library_files`, and recalculates cutoff status.

---

## Method 1: Native Acquisition Drivers

Native drivers allow you to fulfill requests and missing tracks without running Lidarr. TrackSeerr queries downloaders directly and handles post-processing.

### Supported Clients

- **slskd (Soulseek P2P)**: Ideal for rare tracks, b-sides, single-track missing items, and complete album transfers.
- **SABnzbd (Usenet)**: Connects to your SABnzbd instance to download NZBs sourced from Newznab indexers.
- **qBittorrent (BitTorrent)**: Connects to your qBittorrent WebUI to download torrents sourced from Torznab indexers.

### Volume Mount Requirements

When using native drivers, TrackSeerr needs access to both download staging and your media library:

```yaml
volumes:
  - ./data:/data
  - /path/to/music:/music
  - /path/to/downloads:/downloads
```

- `/downloads`: The folder where slskd, SABnzbd, or qBittorrent stores completed transfers.
- `/music`: The destination music library directory scanned by Plex Media Server.

### Configuring Download Clients in the Web UI

1. Open the TrackSeerr web dashboard at `http://<your-server-ip>:5250`.
2. Navigate to **Settings** -> **Download Clients**.
3. Click **Add Download Client**.
4. Select your driver type:
   - **slskd**: Set the Host URL (e.g. `http://192.168.1.50:5030` or `http://slskd:5030`), API key, and staging download path.
   - **SABnzbd**: Set the Host URL (e.g. `http://192.168.1.50:8080`) and API key.
   - **qBittorrent**: Set the Host URL (e.g. `http://192.168.1.50:8088`), username, and password.
5. Click **Test Connection** to verify reachability, then **Save Client**.

### Configuring Indexers (Torznab & Newznab)

For SABnzbd and qBittorrent, TrackSeerr searches releases through Torznab and Newznab compatible indexers (Prowlarr, Jackett, NZBHydra2, or private indexers):

1. Navigate to **Settings** -> **Indexers**.
2. Click **Add Indexer**.
3. Select the protocol (`Torznab` or `Newznab`).
4. Enter the indexer URL (e.g. `http://192.168.1.50:9696/1/api` for Prowlarr) and your API key.
5. Test connectivity and save.

### The Activity Queue and Organizer Lifecycle

When a track or album is queued for download:

1. **Queueing**: The download appears in the **Activity** tab with real-time transfer progress, estimated file size, and client name.
2. **Monitoring**: The `AcquisitionWorker` polls the download client every 5 seconds.
3. **Completion & Archive Extraction**: Once the downloader marks the file complete, TrackSeerr moves it to the `Importing` state. If the release contains compressed archives (`.zip` or `.tar.gz`), TrackSeerr unpacks them safely in staging before looking for audio files.
4. **Tag Inspection**: Mutagen inspects audio tags (artist, album, track number, disc number, audio codec, bit depth, sample rate).
5. **Path Formatting**: The destination path is generated according to your configured naming template (e.g., `{Artist Name}/{Album Title} ({Release Year})/{track:00} - {Track Title}`).
6. **Collision Check & Atomic Move**: If the file already exists, TrackSeerr appends a safe counter (`Title (1).flac`). The file is moved into `/music` using cross-filesystem safe atomic moves.
7. **Automated Catalog Synchronization**: In native mode, the worker immediately updates `library_artists`, `library_albums`, `library_tracks`, and `library_files`. For multi-track albums, files are reconciled against canonical tracklists by disc/track numbers, title similarity (>=0.85), and duration tolerances.
8. **Plex Scan**: TrackSeerr pings Plex Media Server to scan the updated artist folder.
9. **Seeding Governance & Client Cleanup**: If `import_mode == "hardlink"` and seeding ratio or time limits are configured (`seed_ratio_limit`, `seed_time_limit_minutes`), TrackSeerr keeps the torrent seeding in qBittorrent. Once ratio or seeding time targets are reached, the transfer is removed from the client without touching your media files.
10. **Quality Cutoff & Upgrade Monitoring**: TrackSeerr compares the imported audio format against your Quality Profile cutoff (e.g., FLAC 16-bit). If grabbed in a lower quality (like MP3 320), the item is marked available for playback, but stays monitored in the background so TrackSeerr can automatically upgrade it when a lossless release appears.
11. **Notifications**: An alert is dispatched to your configured Discord, Telegram, Pushover, Webhook, or Email channels.

---

## Autonomous Search: RSS Sync, Catalog Sweeps & Anti-Stall

TrackSeerr does not just search once and give up. It operates autonomous background search loops to keep your music library complete:

1. **15-Minute Indexer RSS Sync (`RSSSyncWorker`)**:
   - Polls your Torznab and Newznab indexers every 15 minutes for recently posted releases.
   - Snatches newly uploaded releases that match pending requests or missing playlist tracks without hammering the indexer search API.
   - Snaps up higher-quality releases for existing music that hasn't met your Quality Profile cutoff yet.
2. **Periodic Wanted Backlog Sweeps (`WantedBacklogWorker`)**:
   - Sweeps your unfulfilled user requests, playlist misses, and **monitored missing catalog tracks/albums** on a scheduled interval (default: every 60 minutes).
   - Spaces queries safely with a 2.5-second pacing delay to respect indexer rate limits.
   - Automatically enqueues the top-ranked match when a release becomes available.
3. **Persistent Download Blocklist & Anti-Stall**:
   - Corrupt archives, password-protected releases, or stalled transfers with zero audio files are automatically added to the persistent `download_blocklist` table (indexed by info hash, release GUID, and title).
   - Blocklisted releases are excluded from subsequent RSS snatches and backlog searches, preventing infinite re-snatch loops. Operators can view and remove blocklist items anytime under **Activity -> Blocklist** or via the REST API (`/api/acquisition/blocklist`).

---

## Outbound Notifications

TrackSeerr keeps both homelab admins and family members in the loop using instant notifications across popular platforms:

- **Discord**: Rich embed messages with cover art, status colors, and release details.
- **Telegram**: Instant bot messages to personal chats or group channels.
- **Pushover**: High-priority push notifications directly to mobile devices.
- **Generic Webhook**: Clean JSON payloads for Home Assistant, Node-RED, or custom homelab scripts.
- **Email (SMTP)**: Direct email notifications via TLS/SSL.

You can configure channels and test connectivity under **Settings** -> **Notifications**. Events include music requested, approved, rejected, download started, item available in Plex, download failed, and user issues reported.

---

## Method 2: Lidarr Integration and Paced Trickle Worker

If you already run Lidarr and prefer it to manage downloaders and file renaming, TrackSeerr integrates directly via Lidarr's REST API.

```
+-----------------------------------+
|  Spotify / Deezer Playlist / Web  |
+-----------------------------------+
                  |
                  v
+-----------------------------------+
|            TrackSeerr             |
+-----------------------------------+
         |                  ^
         | (Paced Trickle)  | (Webhook on download)
         v                  |
+-------------------+       |
|      Lidarr       |-------+
+-------------------+
         |
         v
+-------------------+
| Downloaders & NZB |
+-------------------+
         |
         v
+-------------------+
| Plex Media Server |
+-------------------+
```

### Why Direct REST API Instead of Lidarr Custom Lists?

Lidarr's built-in "Custom List" feature only imports artists by MusicBrainz Artist UUID (`musicBrainzId`). It does not support track-level or single-album scoping. Using Custom Lists often leads to empty list errors or causes Lidarr to monitor and download entire discographies for an artist when you only wanted a single song.

TrackSeerr's direct API integration searches Lidarr's metadata lookup for the exact artist, adds the artist with root monitoring set to `none`, monitors only the specific missing album, and triggers a targeted `AlbumSearch`.

### The Paced Trickle Worker

When importing playlists with hundreds or thousands of missing tracks, sending immediate batched requests to Lidarr can overload Lidarr's SQLite database, trigger rate limits on MusicBrainz (`api.lidarr.audio`), or overwhelm indexers.

TrackSeerr includes a background trickle worker that safely spaces requests:

- **Artist Deduplication**: Missing tracks are grouped by artist. The artist lookup is executed once per artist rather than once per track, cutting API calls significantly.
- **Targeted Monitoring**: Only missing albums are flagged for download; discographies are left unmonitored.
- **Configurable Drip Delay**: Requests are sent with a configurable delay (default: 3.0 seconds) plus random jitter.
- **Rate-Limit Backoff**: If Lidarr or MusicBrainz returns HTTP 429 or 503, the worker pauses for 60 seconds before retrying.
- **Queue Controls**: You can monitor progress live in the dashboard, pause, resume, or cancel active runs.
- **State Persistence**: Tracks submitted to Lidarr are marked as `Monitored` in SQLite so duplicate requests are avoided across future playlist syncs.

### Lidarr Environment Configuration

Add the following environment variables to your compose configuration:

```yaml
services:
  trackseerr:
    image: ghcr.io/ronfburgundy/trackseerr:latest
    environment:
      - LIDARR_URL=http://192.168.1.100:8686
      - LIDARR_API_KEY=your_lidarr_api_key_here
      - LIDARR_AUTO_SEARCH=1
      - LIDARR_TRICKLE_RATE_SECONDS=3.0
      - LIDARR_TRICKLE_BATCH_SIZE=25
      - LIDARR_AUTO_TRICKLE=0
      - LIDARR_AUTO_TRICKLE_INTERVAL_MINUTES=30
      # Optional Lidarr settings (profiles, monitor option and tags come from the root folder in Lidarr):
      # - LIDARR_ROOT_FOLDER=/music
```

*Note: Your Lidarr API key is located in Lidarr under **Settings** -> **General** -> **Security** -> **API Key**.*

### Triggering Pushes from the Dashboard

1. Navigate to the **Unmatched Tracks** section in the web interface.
2. Confirm the **Direct API Connected** status indicator.
3. Select your push action:
   - **Push Next 25**: Queues the next batch of 25 unmonitored tracks into the trickle worker.
   - **Push All (Paced)**: Queues all unmonitored tracks into the background worker for paced delivery.
   - **+ Lidarr (Row-level)**: Queues a single track or album immediately.

---

## Migrating from Lidarr to Native TrackSeerr

If you are currently running Lidarr, you can seamlessly migrate your entire library catalog, monitored artist preferences, track files, and MusicBrainz identifiers into TrackSeerr using the built-in 1-click migration engine.

### Prerequisites
1. Ensure your Lidarr container is running and reachable from TrackSeerr.
2. Configure your Lidarr connection in TrackSeerr under **Settings** -> **Lidarr**:
   - **Host URL**: e.g. `http://192.168.1.100:8686` or `http://lidarr:8686`
   - **API Key**: Lidarr API key from Lidarr **Settings** -> **General** -> **Security**
3. Click **Test Connection** to confirm HTTP 200 connectivity.

### Executing the 1-Click Migration

#### Via the Web Dashboard
1. Navigate to the **Library** tab.
2. In the toolbar, click the amber **Import Lidarr** action button.
3. Confirm the dialog prompt: *"Migrate your full Lidarr catalog, artists, albums, tracks, and physical files into TrackSeerr? This will automatically switch operational mode to native."*
4. A progress indicator will display the migration status live.

#### Via the REST API
Dispatch a POST request to the migration endpoint:
```bash
curl -X POST http://localhost:5250/api/library/migrate-lidarr \
  -H "Content-Type: application/json" \
  -d '{"auto_switch_mode": true}'
```

### Migration Pipeline Lifecycle
The background `LidarrMigrationJob` executes the following steps:
1. **Catalog Extraction**:
   - Queries `GET /api/v1/artist` to retrieve all artists, their monitoring status, and MusicBrainz Artist IDs (`foreign_artist_id`).
   - Queries `GET /api/v1/album` to fetch all albums, release dates, types (Studio, EP, Single), and MusicBrainz Release Group IDs (`foreign_album_id`).
   - Queries `GET /api/v1/track` to ingest all track numbers, disc numbers, titles, and MusicBrainz Recording IDs (`foreign_track_id`).
   - Queries `GET /api/v1/trackfile` to extract physical file locations, audio codecs, sample rates, bit depths, bitrates, and file sizes.
2. **Schema Ingestion & Quality Mapping**:
   - Upserts records into `library_artists`, `library_albums`, `library_tracks`, and `library_files`.
   - Normalizes audio quality profiles (e.g. Lidarr's quality definitions are mapped to `FLAC 24bit`, `FLAC 16bit`, `MP3 320`, etc.).
   - Evaluates cutoff compliance against TrackSeerr's configured Quality Profiles.
3. **Automatic Mode Transition**:
   - When `auto_switch_mode=true` (the default), TrackSeerr updates the `library_mode` setting in SQLite to `native`.
   - The frontend reactively exposes the Library tab, and native background acquisition and monitoring engines take over immediately.
   - Lidarr can subsequently be stopped or kept as a secondary reference without causing media collisions.

### Monitoring and Cancellation
- **Check Status**: `GET /api/library/migrate-lidarr/status` returns current counts (`artists_migrated`, `albums_migrated`, `tracks_migrated`, `files_migrated`) and lifecycle phase (`idle`, `running`, `completed`, `failed`, `cancelled`).
- **Cancel Migration**: Dispatch `POST /api/library/migrate-lidarr/cancel` to safely halt ingestion between batches.

---

## Automated Self-Healing Webhook Loop

To update user playlists immediately when Lidarr completes a download, configure a webhook in Lidarr pointing to TrackSeerr.

### Setting Up the Webhook in Lidarr

1. In Lidarr, go to **Settings** -> **Connect**.
2. Click the **+** button and select **Webhook**.
3. Configure the fields:
   - **Name**: `TrackSeerr Re-Sync`
   - **Notification Triggers**: Check **On Download** and **On Upgrade**
   - **URL**: `http://<your-trackseerr-ip>:5250/api/sync/webhook`
   - **Method**: `POST`
4. Click **Test**, then **Save**.

### How Self-Healing Works

When Lidarr finishes downloading and organizing a track:
1. Lidarr notifies Plex to scan the updated directory.
2. Lidarr sends a POST request to TrackSeerr at `/api/sync/webhook`.
3. TrackSeerr triggers a targeted re-sync of all playlists containing missing tracks.
4. Newly discovered tracks in Plex are matched and inserted directly into users' Plexamp playlists.
5. Resolved tracks are removed from the missing tracks list.

---

## Universal Feeds for External Downloaders

TrackSeerr also provides standard RSS and text feeds for users who prefer custom download scripts, Prowlarr sync, or external download managers.

### RSS 2.0 Feed
- **URL**: `http://<your-server-ip>:5250/api/missing/rss`
- **Format**: Standard RSS 2.0 XML containing track title, artist, album, and timestamp.
- **Filter by Playlist**: Append `?playlist_id=<id>` to scope the feed to a specific playlist.

### Plain Text Feed
- **URL**: `http://<your-server-ip>:5250/api/missing/text`
- **Format**: One track per line formatted as `Artist - Title` or `Artist - Title (Album: ...)`.

### Securing Feeds with a Token

If TrackSeerr is exposed outside a protected local network, define `FEED_TOKEN` in your environment:

```yaml
environment:
  - FEED_TOKEN=your_secure_feed_token_here
```

When set, feed and webhook endpoints require authentication via:
- URL query parameter: `?token=your_secure_feed_token_here`
- Header: `X-Api-Key: your_secure_feed_token_here`
- Header: `Authorization: Bearer your_secure_feed_token_here`

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `LIBRARY_MODE` | `native` | Operational mode: `native` for full TrackSeerr catalog & library management, or `lidarr` for external Lidarr delegation |
| `LIDARR_URL` | *None* | Base URL to your Lidarr server (e.g. `http://192.168.1.100:8686`) |
| `LIDARR_API_KEY` | *None* | Lidarr API Key |
| `LIDARR_AUTO_SEARCH` | `1` | Automatically trigger interactive searches when pushing to Lidarr (`1` or `0`) |
| `LIDARR_TRICKLE_RATE_SECONDS` | `3.0` | Delay between artist queries during background trickle |
| `LIDARR_TRICKLE_BATCH_SIZE` | `25` | Number of missing tracks pushed per manual batch or scheduled drip |
| `LIDARR_AUTO_TRICKLE` | `0` | Enable scheduled background trickle runs (`1` or `0`) |
| `LIDARR_AUTO_TRICKLE_INTERVAL_MINUTES` | `30` | Interval in minutes between automated drip runs |
| `LIDARR_ROOT_FOLDER` | *Auto* | Custom Lidarr root folder path |
| `FEED_TOKEN` | *None* | Secret token protecting RSS feeds, plain text lists, and webhooks |

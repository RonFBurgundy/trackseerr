# Downloading music

TrackSeerr can find and download music on its own, or pass the work to Lidarr. This guide covers both.

- [Choose a library manager](#choose-a-library-manager)
- [Before you start](#before-you-start)
- [Download clients](#download-clients)
- [Indexers](#indexers)
- [Quality and profiles](#quality-and-profiles)
- [How searching works](#how-searching-works)
- [How importing works](#how-importing-works)
- [Seeding](#seeding)
- [The Activity page](#the-activity-page)
- [Use Lidarr](#use-lidarr)
- [Move from Lidarr to TrackSeerr](#move-from-lidarr-to-trackseerr)

## Choose a library manager

Open **Settings > General > Library manager** and pick one:

| Manager | Who does the work |
|---|---|
| TrackSeerr (default) | TrackSeerr searches indexers, sends releases to your download clients, and imports the files. |
| Lidarr | TrackSeerr sends requests to Lidarr. Lidarr downloads and imports. |

Only one manager runs at a time. Two managers working on one music folder would grab the same releases twice and rename each other's files.

When you switch, the settings of the other side are kept, so switching back loses nothing. TrackSeerr refuses to switch while downloads are in progress.

TrackSeerr's own manager can do everything below. It also works at the track level: you can request and monitor a single song, not just a whole album.

## Before you start

- Mount your music and downloads under one `/data` folder. See [Install guide: folders](INSTALL.md#folders).
- Set the music folder in **Settings > Media Management > Root Folders & Naming**, for example `/data/media/music`.
- Your download clients must save to a path TrackSeerr can see under the same name.

## Download clients

Add clients in **Settings > Media Management > Clients**.

| Client | Network | What you need |
|---|---|---|
| qBittorrent | BitTorrent | Web UI address, username, password |
| Transmission | BitTorrent | RPC address, RPC path, username (optional), password (optional) |
| Deluge | BitTorrent | Web UI address, password |
| SABnzbd | Usenet | Address, API key |
| NZBGet | Usenet | Address, username, password |
| slskd | Soulseek | Address, API key |

Use the client's LAN IP or container name in the address, never `localhost`. Click test before you save.

TrackSeerr asks each client where it saves files. If TrackSeerr cannot see that folder, **Root Folders & Naming** shows a warning under **Download folders**.

### Client setup notes

- **Transmission:** Enable RPC in Transmission preferences. The default RPC path is `/transmission/rpc`. Basic authentication is optional. TrackSeerr passes its category as torrent labels.
- **Deluge:** Enable the Deluge Web UI. TrackSeerr connects with the Web UI password and attaches to the Deluge daemon. Enable the Label plugin in Deluge preferences if you want category labels.
- **NZBGet:** Enter the NZBGet web address and your ControlUsername and ControlPassword. Category defaults to `music`. NZBGet leaves files on disk (TrackSeerr's import step moves them).
- **qBittorrent:** Enable the Web UI. Enter the address, username, and password. Category defaults to `trackseerr`.
- **SABnzbd:** Enter the address and API key from SABnzbd General settings.
- **slskd:** slskd is good for single tracks, rare releases, and B-sides. It does not use indexers.

## Indexers

Add indexers in **Settings > Media Management > Indexers**. TrackSeerr supports Torznab (torrents) and Newznab (Usenet). Prowlarr, Jackett, and NZBHydra2 all provide these.

For a Prowlarr indexer, the address looks like `http://prowlarr:9696/1/api`. Copy the API key from Prowlarr.

Torrent indexers have seeding rules. Leave a field empty to use the global value.

| Field | Meaning |
|---|---|
| Seed ratio | Seed until this upload ratio. |
| Seed time (minutes) | Seed for at least this long. |
| Discography seed time (minutes) | Seed time for grabs that contain more than one album. Many trackers set this separately. |
| Minimum seeders | Skip releases with fewer seeders. |

A torrent has met its goal when it reaches either the ratio or the seed time. The rules are also sent to qBittorrent, so they hold even while TrackSeerr is stopped. Changing a rule later does not affect torrents already seeding.

## Quality and profiles

These work like they do in Lidarr. You can import custom formats from Lidarr or TRaSH Guides JSON.

| Setting | Where | What it does |
|---|---|---|
| Quality definitions | **Media Management > Quality** | Allowed bitrate range for each quality, for example FLAC 16bit or MP3 320. Releases outside the range are rejected. |
| Quality profiles | **Media Management > Profiles** | Which qualities you accept, in order of preference, and the cutoff. Below the cutoff, TrackSeerr keeps looking for a better release. |
| Metadata profiles | **Media Management > Profiles** | Which release types to monitor for an artist: albums, EPs, singles, live, compilations. |
| Delay profiles | **Media Management > Profiles** | Wait a set time before grabbing, so a better release has time to show up. You can prefer Usenet, torrents, or Soulseek. |
| Release profiles | **Media Management > Profiles** | Words a release title must contain or must not contain. Wrap a term in `/slashes/` to use a regular expression. |
| Custom formats | **Media Management > Custom Formats** | Score releases by title, group, size, and other rules. For example, prefer certain groups or avoid vinyl rips. |
| Tags | **Media Management > Profiles** | Link profiles, indexers, and artists to each other. |

How a release is chosen:

1. Releases that break a rule are rejected: wrong quality, wrong size, a blocked word, too few seeders, or a custom format score below the profile minimum.
2. Of the rest, the highest quality in your profile wins.
3. Within the same quality, the higher custom format score wins.

Use the **Release tester** in **Profiles** to paste a release title and see how it scores.

## How searching works

TrackSeerr searches in three ways:

- **On request.** When a request is approved, TrackSeerr searches at once.
- **RSS.** Every 15 minutes it reads the newest releases from each indexer and grabs anything you want.
- **Backlog.** Every 60 minutes it searches for monitored music that is still missing or below the cutoff.

You can change the intervals with [environment variables](CONFIGURATION.md#background-jobs) and run each job by hand in **Settings > System > Tasks**.

A release that fails (corrupt, password protected, no audio, stalled) is added to the blocklist and is not grabbed again. Manage the blocklist in **Activity > Blocklist**.

## How importing works

When a download finishes, TrackSeerr:

1. Unpacks archives, if any.
2. Checks every file. Files that are not real audio, or that do not match their extension, go to the quarantine folder. Archives with too many files, files that are too large, or unsafe paths are refused.
3. Reads the tags and matches the files to the album and tracks.
4. If the match is weak and you set an AcoustID key, it identifies the files by their audio fingerprint.
5. Writes tags and cover art, if turned on.
6. Renames the files with your naming template and moves them into the music folder.
7. Asks the media server to rescan and adds the tracks to any playlists waiting for them.

Files TrackSeerr cannot match with confidence wait in **Activity > Needs review**. You can import them by hand there.

The import settings are in **Settings > Media Management > Root Folders & Naming**:

| Setting | Options |
|---|---|
| Import mode (torrents) | **Move** (default), **Hardlink**, or **Copy**. Use Hardlink to keep seeding without using extra space. Usenet and Soulseek downloads are always moved. |
| Tagging hardlinked torrent files | **Write tags** makes a temporary copy while the torrent seeds. **Keep hardlink** leaves the tags as they came. Writing tags to a hardlink would change the file the torrent is seeding. |
| Write Tags, Embed Artwork, Normalize Audio Tags | Turn tag writing on or off. |
| AcoustID API key | Turns on fingerprint matching. Get a free key at [acoustid.org](https://acoustid.org/new-application). |

**Move** breaks seeding. If a torrent indexer has seeding rules, TrackSeerr warns you when Move is selected.

## Seeding

**When seeding is done** in **Root Folders & Naming** decides what happens once a torrent meets its seed goal:

| Option | Effect |
|---|---|
| Keep seeding | Leave the torrent alone. |
| Remove torrent (keep files) | Remove it from qBittorrent. Files on disk stay. |
| Remove torrent and its files | Remove it and delete its download files. |

**Remove torrent and its files** only deletes files when it is safe: the import used Hardlink or Copy, every imported file is in the library, nothing is waiting for review, and the download is inside the client's download folder. Otherwise it only removes the torrent and logs why.

A daily task also finds torrents in TrackSeerr's category that it no longer tracks, for example after a restart, and applies the same rule. Results show in **Activity > Needs review**.

## The Activity page

| Tab | Shows |
|---|---|
| Queue | Downloads in progress and torrents seeding. |
| History | Every grab, import, upgrade, and failure. |
| Blocklist | Releases that will not be grabbed again. |
| Needs review | Imports that need a decision, library health results, and cleanup problems. |
| Issues | Problems reported by users. |

## Use Lidarr

1. In Lidarr, copy the API key from **Settings > General > Security**.
2. In TrackSeerr, open **Settings > Lidarr** and enter the Lidarr address and API key. Test and save.
3. Pick a root folder.
4. In **Settings > General > Library manager**, choose Lidarr.

How TrackSeerr uses Lidarr:

- For each request, TrackSeerr adds the artist to Lidarr without monitoring the whole discography, then monitors only the album it needs.
- With **Search on Add**, Lidarr searches as soon as the album is added.
- With **Prefer singles for song requests**, a request for one song looks for the single before the full album.
- Missing playlist tracks are sent to Lidarr in small batches, a few seconds apart, so Lidarr and MusicBrainz are not flooded. Turn on **Auto Trickle** to do this on a schedule. Set the batch size and delay on the same page.

Lidarr handles downloading, importing, and renaming. TrackSeerr still handles discovery, requests, users, and playlists.

To update playlists as soon as Lidarr imports something, add a webhook in Lidarr under **Settings > Connect**. Use `http://<trackseerr>:5250/api/sync/webhook` with method POST, on download and on upgrade. If `FEED_TOKEN` is set, add `?token=<FEED_TOKEN>` to the URL.

## Move from Lidarr to TrackSeerr

TrackSeerr can copy your Lidarr library: artists, albums, tracks, files, monitoring, and MusicBrainz IDs. Your files are not moved.

1. Connect Lidarr in **Settings > Lidarr** as above.
2. Open **Library** and click **Import from Lidarr**.
3. Wait for the import to finish. Progress is shown on the page.

When it finishes, TrackSeerr becomes the library manager. You can then stop Lidarr. Do not let both manage the same folder.

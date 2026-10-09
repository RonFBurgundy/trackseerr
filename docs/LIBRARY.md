# Library management

This page covers TrackSeerr's own library manager. If Lidarr manages your library, Lidarr does most of this instead. See [Downloading music](ACQUISITION.md#choose-a-library-manager).

- [The Library page](#the-library-page)
- [Scanning](#scanning)
- [Adding artists](#adding-artists)
- [Monitoring](#monitoring)
- [Wanted](#wanted)
- [Calendar](#calendar)
- [Importing files by hand](#importing-files-by-hand)
- [File naming](#file-naming)
- [Retagging files](#retagging-files)
- [Deleted and rejected files](#deleted-and-rejected-files)
- [History](#history)
- [Library health](#library-health)

## The Library page

**Library** is for admins. It has four tabs:

| Tab | Shows |
|---|---|
| Artists | Every artist, with album and track counts. Filter by tag or genre. |
| Albums | Every album, with cover art and how many tracks you have. |
| Tracks | Every track, with its format and whether it meets your quality cutoff. |
| Collections | Your own groups of albums, such as box sets or themes. |

Open an artist to see their whole discography, including releases you do not have. From there you can change monitoring, profiles, and tags, refresh the discography, or search for a release.

Select several artists to change their monitoring, profiles, or tags at once.

## Scanning

A scan reads every audio file in the music folder and adds it to the library. Run the first scan after you set the music folder. Click **Scan library** on the **Library** page.

- Files that have not changed since the last scan are skipped, so later scans are fast.
- Files are matched to artists, albums, and tracks by their tags.
- System trash folders such as `.Trash` and `@Recycle` are skipped.
- You can cancel a scan at any time.

## Adding artists

An artist joins the library when:

- a scan finds their files
- someone requests their music
- an import list adds them

TrackSeerr loads the artist's discography from MusicBrainz. Monitored tracks you do not have yet show up in **Wanted**.

On an artist's page, **Rest of discography** lists the releases you do not have. Select some and click **Add & monitor** to start looking for them. **Refresh discography** checks for new releases; they are monitored according to the artist's monitor option.

Each artist has a monitor option, a quality profile, a metadata profile, and tags. The metadata profile decides which release types count: albums, EPs, singles, and so on. Change these on the artist's page, or select several artists to change them at once.

## Monitoring

Monitored means TrackSeerr will search for it. You can monitor an artist, an album, or a single track. Changing an artist or album changes everything under it.

When you add an artist, you pick one of these options:

| Option | What it monitors |
|---|---|
| All albums | Every album and track. |
| Albums only | Studio albums only. |
| Singles & EPs only | Singles and EPs only. |
| Existing tracks | Only the tracks you already have files for. |
| Future releases only | Only releases dated after the artist was added. |
| None | Nothing. |

The defaults are set in **Settings > Media Management > Root Folders & Naming**. There are two: one for artists found by a library scan, and one for artists you add by hand. Both start at **Existing tracks**, so a first scan never starts a flood of downloads.

## Wanted

**Wanted** lists what TrackSeerr is looking for.

| Tab | Shows |
|---|---|
| Missing | Monitored tracks with no file. |
| Cutoff Unmet | Tracks you have, but below the cutoff in their quality profile. TrackSeerr keeps looking for a better copy. |

You can search for any item by hand from here. TrackSeerr also searches on a schedule. See [How searching works](ACQUISITION.md#how-searching-works).

## Calendar

The **Calendar** page shows album releases across the month for monitored artists. On desktop, it displays a standard month grid. On mobile, it displays a chronological agenda list.

Each release displays a status chip:
- **Downloaded**: Every track file is present.
- **Partial**: Some track files are present, but not all.
- **Missing**: The album is released, but no track files are present.
- **Upcoming**: The release date is in the future.

Use the unmonitored toggle to show or hide releases from artists that are not monitored.

### Subscribing to the iCal feed

Click **Subscribe** on the Calendar page to get an RFC 5545 iCal feed URL. You can paste this feed URL into Apple Calendar, Google Calendar, Outlook, or Thunderbird to view your music releases alongside your personal schedule. The feed uses your TrackSeerr feed token or API key for secure read-only access.

## Importing files by hand

Use this for files TrackSeerr did not import on its own: a CD rip, a download from somewhere else, or files it could not match.

- From an album, click **Import files for this album**.
- Files that failed to match during a download wait in **Activity > Needs review**.

TrackSeerr reads the tags, suggests a match for each file, and shows how confident it is. Fix any wrong matches, then import. Files are tagged, renamed, and moved like any other import.

To import from a folder outside your download clients' folders, set **Extra import folder** in **Root Folders & Naming**.

## File naming

Set the naming in **Settings > Media Management > Root Folders & Naming**. There are three formats:

| Format | Example result |
|---|---|
| Artist Folder Format | `Beatles` |
| Standard Track Format | `The White Album (1968)/01 - Back in the U.S.S.R..flac` |
| Multi Disc Track Format | `The White Album (1968)/Disc 02/01 - Revolution 1.flac` |

The track formats include the album folder. Use `/` to make folders. The file extension is added for you. An optional **Compilation File Name** overrides the file name for various-artists releases.

The page shows a live preview. Click **Naming Tokens** for the full list of tokens.

Syntax:

| You type | You get |
|---|---|
| `{Album Title}` | The value, or nothing if it is unknown. |
| `{ (Release Year)}` | ` (1968)`. The text around the token is dropped if the token is empty. |
| `{ - [{Album Type}]}` | ` - [EP]`. Same rule, with the brackets kept. |
| `{track:00}` | Track number with two digits: `01`. |

Presets:

| Preset | Layout |
|---|---|
| Trackseerr | Clean artist folders, `Album - [Type] (Year)` folders, `Disc NN` subfolders. The default. |
| TRaSH Guides | Artist and album repeated in every file name, as the Lidarr community recommends. |
| Plex | Plex's layout: `Artist/Album/NN - Title`. Multi-disc albums stay in one folder with `101`, `201` numbering. |
| Lidarr Standard | The old TrackSeerr default, with the quality in the file name. |
| Clean Minimal | Drops a leading The, A, or An from names. |
| Audiophile / Detailed | Adds codec, bit depth, and sample rate to every file name. |

New imports use the current format. To rename files already in the library, click **Rename files** on the **Library** page, an artist page, or an album. TrackSeerr shows each current path next to its new path. Pick the files to rename and apply.

## Retagging files

Admins can write catalog metadata to existing audio files. Click **Retag files** on the **Library** page, an artist page, or an album.

TrackSeerr compares tags on each file with catalog metadata. The preview lists each differing field with its current value and proposed value. Files that already match are omitted.

Select the files you want to update and apply the changes. You can also embed album artwork.

TrackSeerr protects seeding torrents. If a library file shares data with a seeding torrent through a hardlink, TrackSeerr checks your media management settings:

- With **Copy and tag**, TrackSeerr makes a private copy of the file before writing tags. The original seeding file remains untouched.
- With **Keep hardlink**, TrackSeerr skips tagging the file so the torrent stays intact.

## Deleted and rejected files

TrackSeerr does not delete files right away.

| Folder | What goes there | Setting |
|---|---|---|
| Recycle bin | Files you delete and files replaced by an upgrade. Emptied after a set number of days. | **Recycle bin folder**, **Delete recycled files after (days)**, **Delete replaced files permanently** |
| Quarantine | Downloaded files that failed the safety checks, for example a file that is not really audio. | **Quarantine folder** |

All of these are in **Root Folders & Naming**. Files stay in these folders until you deal with them or the recycle bin empties.

## History

Every artist, album, and track has a history: when it was grabbed, imported, upgraded, renamed, or deleted, and why. Open it with the history button on the item's page.

## Library health

The library health check compares your files with what your media server sees, and lists files the server skipped. See [Media servers: library health](MEDIA_SERVERS.md#library-health).

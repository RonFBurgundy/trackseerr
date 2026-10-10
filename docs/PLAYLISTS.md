# Playlists

TrackSeerr imports playlists from outside sources, matches each track to your library, and writes the playlist to your media server for each user who wants it. It keeps the playlists in sync and can request the tracks you do not have yet.

- [Add a playlist](#add-a-playlist)
- [Smart collections](#smart-collections)
- [Choose who gets a playlist](#choose-who-gets-a-playlist)
- [Missing tracks](#missing-tracks)
- [Syncing](#syncing)
- [Import lists](#import-lists)
- [iTunes and Apple Music libraries](#itunes-and-apple-music-libraries)
- [Plex extras](#plex-extras)
- [Playlists from environment variables](#playlists-from-environment-variables)

## Add a playlist

Open **Playlists** and click **Add Playlist**. There are six ways to add one.

### By Link

Paste a Spotify or Deezer playlist URL, for example:

```
https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M
https://www.deezer.com/playlist/1313621735
```

No Spotify API key is needed. TrackSeerr reads the public playlist page. Private playlists and Spotify Liked Songs cannot be read this way; use **Paste Tracks** for those.

### Featured

The **Featured** tab lists popular charts, such as Billboard Hot 100 and Deezer Top Worldwide. Click **Add** to sync one like any linked playlist.

### Paste Tracks

Give the playlist a name and paste one track per line, in the form `Artist - Title`:

```
Kavinsky - Nightcall
Gunship - Tech Noir
Carpenter Brut - Turbo Killer
```

This works for any source you can copy text from.

### Upload .m3u

Pick an `.m3u` or `.m3u8` file (up to 2 MB) and give the playlist a name. Tracks are matched by the `#EXTINF` artist and title, or by file path against your library. Use this for playlists exported from a media player or another library manager.

### 1-Click Helper

On the **1-Click Helper** tab, drag **Send to TrackSeerr** to your browser's bookmarks bar. Then, on any Spotify or Deezer playlist page, click the bookmark. TrackSeerr opens with the **By Link** tab filled in. Check it and click **Add Playlist**. If you are not signed in, TrackSeerr keeps the link through sign-in.

### My Listening

Build a playlist from your own Last.fm or ListenBrainz account: loved tracks, top tracks, or generated playlists such as Weekly Jams. Connect your account first in **Settings > Requests > Scrobbling**.

## Smart collections

Smart collections are rule-based library playlists evaluated from your own local tracks using library facet filters (genres, release years, artist country, artist type, member count, formed year, popularity, and tags). They sync to target media server users just like any other playlist:

- **Admin-only & Native library only**: Smart collections require admin privileges and operate only when TrackSeerr manages the native library (not available in Lidarr mode).
- **Auto-update vs One-time**: Smart collections created with *keep in sync* enabled (`enabled = 1`) are re-evaluated and re-pushed automatically on every background sync cycle. One-time collections (`enabled = 0`) are evaluated on creation and on explicit "sync now", never on background sync cycles.
- **Library tracks only**: Because every track is already present in your local files, smart collections use monitor mode `none` and cannot auto-request tracks.
- **Empty result keeps last snapshot**: If a scheduled sync evaluation returns 0 matching tracks, TrackSeerr logs a skip and keeps the last good snapshot to prevent clearing the playlist on the media server.

## Choose who gets a playlist

Each playlist card shows the users it is synced to. Click a user to add or remove them.

- Regular users can only add playlists for themselves.
- Admins can send a playlist to any user.
- On Plex and Jellyfin, each user gets their own copy on the media server.
- On Subsonic servers, every playlist goes to the one account TrackSeerr signs in with. See [Media servers](MEDIA_SERVERS.md).

## Missing tracks

A track that is not in your library is listed as missing. Each playlist has a monitor mode that decides what happens to its missing tracks:

| Mode | What happens |
|---|---|
| Track only | Request just the missing songs. |
| Album | Monitor the album each missing song is on. |
| Artist | Add the artist, using the artist monitor option you choose. |
| None | Match only. Never search. |

Regular users can choose **Track only** or **None**. Requests made this way count against the user's quota and need approval unless the user has the **Auto-request playlist tracks** permission. See [Users and requests](USERS_AND_REQUESTS.md#permissions).

When a missing track arrives in the library, the next sync adds it to the playlist.

**Fix a wrong miss.** Sometimes a track is in your library but TrackSeerr did not match it, for example because the titles differ. Admins can click **Missing (N)** on a playlist card, then **Match** on the track. Search your library, pick the right track, and TrackSeerr remembers the match for every playlist. **Match Memory** on the Playlists page lists saved matches; undo any of them there.

Admins see every missing and below-quality item in **Wanted**. Missing playlist tracks can also be read from these addresses:

| Format | Address |
|---|---|
| CSV | `/api/missing/csv` |
| RSS | `/api/missing/rss` |
| Plain text, one `Artist - Title` per line | `/api/missing/text` |

Add `?playlist_id=<id>` to limit the RSS feed to one playlist. If `FEED_TOKEN` is set, add `?token=<FEED_TOKEN>` to each address.

## Syncing

TrackSeerr syncs all playlists on a schedule. The default is once a day; change it with `SECONDS_TO_WAIT`. Admins can start a sync at any time with the sync button on the **Playlists** page.

By default, a sync makes the media server playlist match the source: new tracks are added and removed tracks are taken out. Set `APPEND_INSTEAD_OF_SYNC=1` to only add tracks.

Other playlist options are in the [configuration reference](CONFIGURATION.md#playlist-sync).

If you use Lidarr, it can trigger a sync when it finishes a download. In Lidarr, add a webhook under **Settings > Connect** that posts to `http://<trackseerr>:5250/api/sync/webhook` on download and upgrade.

## Import lists

Import lists are for admins. They add music to the library from an outside list on a schedule, without creating a playlist. Set them up in **Settings > Media Management > Import Lists**.

| Provider | Lists |
|---|---|
| Last.fm | Loved tracks, top artists, top albums, top tracks |
| ListenBrainz | A playlist, playlists created for a user, top artists, top albums |
| MusicBrainz | A collection |

Each list has a monitor mode (the same four modes as playlists), a quality profile, tags, and a sync interval. TrackSeerr records every item it has applied, so an item you later unmonitor stays unmonitored.

## iTunes and Apple Music libraries

You can import the playlists from an iTunes or Apple Music library.

1. In iTunes or Music, choose **File > Library > Export Library**. This saves an XML file.
2. In TrackSeerr, open **Settings > Media Management > Root Folders & Naming** and find **Import iTunes / Apple Music library**.
3. Upload the XML file and review the preview.
4. Check the path mapping. iTunes stores file paths from the computer it ran on, for example `C:/Users/you/Music/iTunes/iTunes Media/Music`. Map that to your library folder, for example `/data/media/music`. TrackSeerr suggests a mapping.
5. Commit the import.

Notes:

- Regular and smart playlists are imported as fixed lists. Built-in playlists, folders, and empty playlists are skipped.
- Importing the same library again updates the same playlists.
- Tracks are matched by file path first, then by artist, title, album, and length.
- Missing tracks default to monitor mode **None**, so a large library does not flood your downloader.
- Play counts and ratings are not imported.
- The largest accepted file is 100 MB. Change it with `ITUNES_IMPORT_MAX_MB`.

## Plex extras

These only work with Plex.

**Your Plex playlists.** The **Playlists** page also lists the playlists you already have in Plex, including ones made in Plexamp. Admins can see the playlists of every Plex Home user.

**Mixes.** TrackSeerr can build playlists from listening history: **Daily Blend**, **Discover Weekly**, and **Artist Radio** from a seed artist. Save a mix as a playlist, or replace an existing one. Plexamp's own mixes are shown too, and you can save a snapshot of any of them as a normal playlist.

Mixes work best with scrobbling turned on. See [Media servers: Plex](MEDIA_SERVERS.md#plex).

## Playlists from environment variables

You can also list playlists to sync in the container's environment. These sync like any other playlist.

```yaml
environment:
  - SPOTIFY_PLAYLIST_ID=37i9dQZF1DXcBWIGoYBM5M,https://open.spotify.com/playlist/...
  - DEEZER_PLAYLIST_ID=1313621735
  - SPOTIFY_USER_ID=spotify_username   # every public playlist of this user
  - DEEZER_USER_ID=123456              # every public playlist of this user
```

`SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` are optional. Without them, TrackSeerr reads public pages instead of the Spotify API.

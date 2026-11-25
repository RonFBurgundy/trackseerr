# Media servers

TrackSeerr pushes playlists to your media server, tells it to rescan after imports, and checks that it sees every file in your library.

You can use several media servers at once, for example Plex for your users and Navidrome for yourself, or none at all.

## What each server supports

| Feature | Plex | Jellyfin | Subsonic (Navidrome, Gonic, Airsonic) | None |
|---|---|---|---|---|
| Push playlists | Yes | Yes | Yes | No |
| Separate playlists per user | Yes | Yes | No, one account only | No |
| Import server users as playlist targets | Yes | Yes | No | No |
| Sign in to TrackSeerr with the server account | Yes | No | No | No |
| Rescan after import | Yes | Yes | Yes | No |
| Library health check | Yes | Yes | Yes | No |
| Playlist artwork and descriptions | Yes | No | No | No |
| Manage existing playlists on the server | Yes | No | No | No |
| Mixes built from listening history | Yes | No | No | No |
| Scrobbling from server webhooks | Yes (needs Plex Pass) | No | No | No |

Without a media server, TrackSeerr still manages your library, downloads music, and imports playlists. It matches playlists against its own library to find missing tracks, but it pushes nothing.

Users without a media server account sign in with a local TrackSeerr account. See [Users and requests](USERS_AND_REQUESTS.md).

## Several servers

Add each server under **Settings > Media Management > Media Server**. Every server gets its own switches:

- **Refresh library after imports**: the server rescans after TrackSeerr imports music.
- **Push playlists**: playlists are sent to this server.
- **Import users**: the server's accounts show up as playlist targets.
- **Read play history**: listens on this server are sent to Last.fm and ListenBrainz. Only Plex can do this today.

Every playlist goes to every server that has **Push playlists** on. To keep a playlist off a server, open the playlist and turn that server off under **Servers**. The same place shows how many songs each server matched.

One Plex server is the one people sign in with. Pick it with **Use for sign-in** on its card. A server set with environment variables is in the list too. You can rename it, turn it off, or change its switches there, but its address and token stay in your environment.

If your Subsonic server, such as Navidrome, already sends plays to Last.fm or ListenBrainz, leave **Read play history** off for it, or every play is counted twice.

## Plex

Add Plex in **Settings > Media Management > Media Server** with its address and token, or with environment variables:

```yaml
environment:
  - PLEX_URL=http://192.168.1.100:32400
  - PLEX_TOKEN=your_plex_token
```

- Use the token of the Plex server owner. Plex explains how to find it: [Finding an authentication token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
- TrackSeerr uses the first music library on the server. To pick another one, set `PLEX_MUSIC_SECTION` to its name.
- The server owner signs in with **Sign in with Plex** and is the TrackSeerr admin. Anyone else with access to your Plex server can sign in the same way. Plex accounts without access are refused.
- For a self-signed certificate, set `PLEX_VERIFY_SSL=0`.

**Webhook.** With Plex Pass, Plex can tell TrackSeerr what users play. Copy the webhook URL from **Settings > Requests > Scrobbling** and add it in Plex under **Settings > Webhooks**. Plex must be able to reach that URL. Without Plex Pass, TrackSeerr reads play history on a schedule instead.

## Jellyfin

Set it in **Settings > Media Management > Media Server**, or with environment variables:

```yaml
environment:
  - MEDIA_SERVER=jellyfin
  - JELLYFIN_URL=http://jellyfin:8096
  - JELLYFIN_API_KEY=your_api_key
```

1. In Jellyfin, open **Dashboard > API Keys** and create a key for TrackSeerr.
2. Enter the URL and key in TrackSeerr, test the connection, and save.
3. TrackSeerr imports your Jellyfin accounts when it connects: on save and on every start. They show up as playlist targets.

How playlists work on Jellyfin:

- Each target user gets their own private playlist.
- TrackSeerr matches its users to Jellyfin accounts by name or ID.
- A playlist with no target goes to `JELLYFIN_USER`, or to the first Jellyfin admin if that is not set.
- TrackSeerr never edits a playlist owned by another Jellyfin user. It creates a new one instead.
- A track listed twice in the source is added once.

Imported Jellyfin users are not TrackSeerr admins, and they cannot sign in to TrackSeerr with their Jellyfin password. Give them a local account if they need to sign in.

## Subsonic servers

This covers any server with the Subsonic API: Navidrome, Gonic, Airsonic, Airsonic-Advanced, and Subsonic itself.

Set it in **Settings > Media Management > Media Server**, or with environment variables:

```yaml
environment:
  - MEDIA_SERVER=subsonic
  - SUBSONIC_URL=http://navidrome:4533
  - SUBSONIC_USER=trackseerr
  - SUBSONIC_PASSWORD=your_password
```

- TrackSeerr writes every playlist to this one account. The Subsonic API has no way to write playlists for other users.
- Create a separate account for TrackSeerr so its playlists are easy to tell apart, and share them from the server if your users need them.
- If the server supports OpenSubsonic API keys, you can set `SUBSONIC_API_KEY` instead of a user and password.

## No media server

Leave the server list empty and set no `PLEX_*`, `SUBSONIC_*` or `JELLYFIN_*` variables. Set `ADMIN_PASSWORD` before the first start so you can sign in.

## Library health

TrackSeerr compares the files on disk with the files your media server knows about. It lists files the server skipped, for example because of bad tags or an unsupported format.

Run the check from **Activity > Needs review**. You can set it to run weekly. With several servers, the check looks at the main one: the sign-in Plex, or else the first server in the list.

The media server and TrackSeerr often see the library at different paths, for example `/data/media/music` in TrackSeerr and `/music` in Jellyfin. TrackSeerr guesses the mapping from a sample of files. You can check and change it under **Path mapping** on the same page.

## Switching servers

You can add, turn off, or remove servers at any time. Your library, requests, and playlists are stored in TrackSeerr, not on the server, so nothing is lost. On the next sync, playlists are written to every server that has **Push playlists** on. If you had a server saved before this version, it shows up in the list on its own.

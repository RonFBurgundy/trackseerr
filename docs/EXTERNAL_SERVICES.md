# External services

This page lists every outside server TrackSeerr talks to, when it does, and how much. It is for people who run TrackSeerr and want to know what leaves their network, and for the people who run those servers.

If anything here looks wrong, the code in `trackseerr/clients/` is the final word. Please open an issue.

## MusicBrainz data

TrackSeerr gets artist, album and track data from MusicBrainz. Each install has one client for this, in `trackseerr/clients/mbid_enricher.py`.

### Which servers

- **BrainzMash mirror.** By default TrackSeerr asks `https://api.brainzmash.cc`, a MusicBrainz mirror run by the community. You can change it in Media Management, or with the `MUSICBRAINZ_URL` environment variable.
- **musicbrainz.org.** Used only as a backup: when the mirror returns an error, asks us to slow down, or is paused (see below).

### What a request looks like

- Every request is a `GET` that asks for JSON. TrackSeerr never sends changes.
- It sends `User-Agent: Lidarr/2.0.0 (TrackSeerr; https://github.com/trackseerr)`. The mirror only accepts Lidarr-style clients right now. We have asked its operators to allow TrackSeerr under its own name.
- Connections are reused, and a request gives up after 10 seconds.

### What TrackSeerr asks for

| Purpose | Request |
|---|---|
| Find an artist by name | `/ws/2/artist?query=artist:"<name>"` |
| Artist details | `/ws/2/artist/<mbid>?inc=genres+tags+url-rels+artist-rels` |
| An artist's albums | `/ws/2/release-group?artist=<mbid>&limit=100&offset=N` (25 pages at most) |
| Find an album | `/ws/2/release-group?query=releasegroup:"…" AND artist:"…"` |
| Album details | `/ws/2/release-group/<id>` |
| One edition of an album | `/ws/2/release/<id>?inc=release-groups` |
| An album's tracks | `/ws/2/release?release-group=<id>&inc=recordings+artist-credits&limit=1` |
| All editions of an album | `/ws/2/release?release-group=<id>&inc=recordings+media&limit=…&offset=N` |
| Find a track | `/ws/2/recording?query=isrc:<isrc>`, or `query=recording:"…" AND artist:"…"` |
| Find the artist behind a link | `/ws/2/url?resource=<url>&inc=artist-rels` |

### How much it sends

These limits apply to the whole install. User actions, imports and background tasks all share them.

| | Mirror | musicbrainz.org |
|---|---|---|
| Requests per second | 3 at most. Change it with `MUSICBRAINZ_MIRROR_RPS` (0.5 to 20). | 1 at most |
| Requests at the same time | 2 at most. Change it with `MUSICBRAINZ_MIRROR_CONCURRENCY` (1 to 8). | 1 |
| If the server answers 429 or 503 | TrackSeerr stops asking for as long as the `Retry-After` header says (30 seconds if there is none, 10 minutes at most). It does not retry the mirror. Requests go to musicbrainz.org until the pause ends. | Same pause, plus one retry after at most 30 seconds. |
| If the server keeps failing | After 5 failures in a row (no answer, 429 or a 5xx error), TrackSeerr stops asking for 5 minutes, then tries a single request to see if it is back. A 403 (refused) from the mirror counts as a failure too, so a mirror that blocks TrackSeerr stops getting requests. | Same, except 403. |

### What TrackSeerr remembers

Answers are saved in the install's own database, so the same question is not asked again until the saved answer expires:

| Data | Kept for |
|---|---|
| Search results and lookups | 30 days |
| Artist details | 7 days |
| An artist's album list | 20 hours |
| An album's tracks | 30 days |
| "Not found" answers | 1 day |

Expiry times are spread out by up to 15% at random, so a big library does not refresh all at once. Answers are also kept in memory for an hour.

### What triggers requests

- People using the app: searching, requesting and adding artists.
- Importing files: each new file is matched against MusicBrainz.
- The "Artist Metadata & Discography Refresh" task. It runs once a day by default, or every 6 hours at the most. It only refreshes artists whose saved data has expired, and waits 1.5 seconds between artists.
- Bulk tasks an admin starts by hand, such as MusicBrainz Release Match.

### Checking it

System → Status shows, for the mirror and for musicbrainz.org: requests in the last hour, the request limit, and whether TrackSeerr is currently pausing that server.

## Other services

| Server | What for | When | Account needed |
|---|---|---|---|
| `api.deezer.com`, `www.deezer.com`, `e-cdns-images.dzcdn.net` | Searching the catalog, artist and album details, Deezer playlists, artwork | Browsing, requests, playlist sync | No |
| `open.spotify.com`, `api-partner.spotify.com`, `image-cdn-ak.spotifycdn.com` | Public Spotify playlists | Adding and syncing playlists | No (or your own Spotify API keys) |
| `music.youtube.com` | Public and unlisted YouTube Music playlists | Adding and syncing playlists. Answers are kept for 5 minutes, and requests are retried after a 429 or 5xx error. | No |
| `coverartarchive.org` | Album artwork | Imports, artwork refresh | No |
| `api.acoustid.org` | Identifying files by their sound, and sending fingerprints if you turn that on | Imports that match poorly, AcoustID tasks | Your AcoustID keys |
| `itunes.apple.com` | Backup catalog search, iTunes library import | Browsing, imports | No |
| `ws.audioscrobbler.com`, `api.listenbrainz.org` | Import lists and scrobbling | Scheduled import lists, sending your plays | Your keys |
| `api.github.com` | Checking for a new TrackSeerr version | Every 12 hours by default | No |
| `api.telegram.org`, `api.pushover.net`, browser push servers | Notifications | Only for the events you turn on | Your tokens |

TrackSeerr also talks to the services you set up yourself (Plex, Jellyfin, Subsonic, Lidarr, indexers, download clients, slskd) at the addresses you enter.

# Plex Playlist Control — design contract

Status: in implementation on branch `feat/plex-playlist-control`.

TrackSeerr can see and manage the Plex playlists of (a) the logged-in TrackSeerr user and (b) for admins, any Plex Home / managed user. That covers playlists created by hand in Plex/Plexamp, Plexamp smart playlists, and Plexamp's generated mixes (read-only hubs that can be saved as a playlist).

## Identity and permissions

- A TrackSeerr user maps to a Plex user by `users.username` (Plex OAuth login), compared case-insensitively. That is how `api/routes/sync.py` already resolves targets.
- The admin Plex account is `server.myPlexAccount().username`. Use `self.server` for it and `self.server.switchUser(username)` for any other user. This is the same pattern as `PlexClient.sync_playlist_to_users`.
- Non-admins may only pass `user=<their own username>`. Any other value returns **403**.
- Admins may pass the admin username or any username returned by `PlexClient.get_home_users()`. Unknown username returns **404**. A `switchUser` failure (e.g. a friend who is not in Plex Home) returns **502** with the Plex error message.
- Writing into another user's profile (copying to users other than the caller) is **admin-only**. A non-admin may copy only into their own profile.
- If no Plex client is configured, every endpoint returns **503**.

## Playlist kinds and ownership

`kind`:
- `regular`: a static playlist (`playlist.smart` is false). Full control.
- `smart`: `playlist.smart` is true. Allowed: rename, delete, copy (as a static snapshot), adopt (read-only source). Any track-level edit returns **409**.

Only audio playlists are in scope (`playlist.playlistType == "audio"`). Video and photo playlists are filtered out.

`owner` is stored in the registry and is one of:
- `trackseerr`: TrackSeerr created or manages it. Sync may overwrite it.
- `user`: created by the person. **Sync never overwrites it.**
- `plexamp`: a smart playlist. **Sync never overwrites it.**

Classification when a playlist is first seen (inventory or sync):
1. If `smart` → `plexamp`.
2. Else, if the title equals the `name` of a TrackSeerr `playlists` row whose targets include this user, **and** that row has `last_synced_at` set (TrackSeerr has actually written it before), **and** (when the Plex playlist exposes `addedAt`) the Plex playlist's `addedAt` is not earlier than that row's `created_at` → `trackseerr`. This adopts legacy playlists TrackSeerr created before the registry existed, but never claims a pre-existing hand-made playlist that happens to share the name.
3. Else → `user`.

The user can flip a regular playlist between `user` and `trackseerr` with the flags endpoint. That is the protection toggle.

### Overwrite protection in the sync path

`PlexClient.update_or_create_playlist` gets a name collision check. It needs a DB handle and the target username, passed through `sync_playlist_to_users`, which already receives `db`. When `srv.playlist(name)` finds an existing playlist:
- registry row exists with `owner` in (`user`, `plexamp`), or the playlist is smart → **do not touch it**. That user's `SyncResult` gets `success=False` and `error="Protected: '<name>' is owned by <owner> in <username>'s profile"`. Log at WARNING.
- Before the first write for a user in a sync run, `sync_playlist_to_users` calls `refresh_playlist_registry` for that user (once per user per call), so every existing playlist is classified by the rules above before any write. A collision is therefore only overwritten when its registry owner is `trackseerr`.
- With `db` present, `username` is mandatory (raise `ValueError` otherwise). With `db` None (CLI path) the legacy behavior applies: a non-smart collision is overwritten.
- After every successful create or update, upsert the registry row as `owner='trackseerr'` with the playlist's `ratingKey`.

When `db` is None (CLI path) the behavior is unchanged, apart from the smart-playlist guard, which always applies.

## Storage (migration v24)

```sql
CREATE TABLE IF NOT EXISTS plex_playlist_registry (
    plex_user TEXT NOT NULL,              -- lowercased Plex username
    rating_key TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,                   -- regular | smart
    owner TEXT NOT NULL,                  -- trackseerr | user | plexamp
    ignored INTEGER NOT NULL DEFAULT 0,
    trackseerr_playlist_id TEXT REFERENCES playlists(id) ON DELETE SET NULL,
    last_seen_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    PRIMARY KEY (plex_user, rating_key)
);
CREATE TABLE IF NOT EXISTS plex_mix_snapshots (
    id TEXT PRIMARY KEY,                  -- uuid4 hex
    plex_user TEXT NOT NULL,
    mix_key TEXT NOT NULL,                -- Plex `key` of the hub item
    mix_title TEXT NOT NULL,
    playlist_title TEXT NOT NULL,
    rating_key TEXT,                      -- created playlist
    auto_refresh INTEGER NOT NULL DEFAULT 0,
    last_refreshed_at TEXT,
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE (plex_user, mix_key)
);
```

On each inventory call, rows for playlists that no longer exist on Plex for that user are deleted.

## REST API: `/api/plex-playlists` (new router `api/routes/plex_playlists.py`)

All endpoints require `get_current_user`. `user` is a query parameter. When omitted it defaults to the caller's username.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/users` | — | `[{username, is_admin_account, is_self}]`. Non-admin: only self. |
| GET | `?user=&include_ignored=false` | — | `PlexPlaylistSummary[]` (also refreshes the registry) |
| GET | `/{rating_key}/items?user=` | — | `PlexPlaylistItem[]` in playlist order |
| PATCH | `/{rating_key}?user=` | `{title}` | `PlexPlaylistSummary` |
| DELETE | `/{rating_key}?user=` | — | 204 (removes the registry row too) |
| POST | `/{rating_key}/items?user=` | `{track_rating_keys: string[]}` | `PlexPlaylistItem[]`. Appends library tracks fetched via `server.fetchItem(int(key))`. 409 if smart. |
| DELETE | `/{rating_key}/items/{playlist_item_id}?user=` | — | 204. 409 if smart. |
| POST | `/{rating_key}/items/{playlist_item_id}/move?user=` | `{after_playlist_item_id: int \| null}` | `PlexPlaylistItem[]`. `null` = move to top. Uses `playlist.moveItem(item, after=...)`. 409 if smart. |
| POST | `/{rating_key}/copy?user=` | `{target_users: string[], title?: string}` | `[{username, success, rating_key?, error?, copied_tracks: int, omitted_tracks: int}]`. Tracks not visible in the target's library are counted in `omitted_tracks`; any per-target Plex error fails only that target. Creates a static playlist with the source's items in each target profile. A name collision with a non-`trackseerr` playlist in the target fails that target with an error (no overwrite). New copies are registered as `trackseerr`. |
| POST | `/{rating_key}/adopt?user=` | — | the created TrackSeerr playlist dict (shape of `GET /api/playlists` items) |
| PUT | `/{rating_key}/flags?user=` | `{ignored?: bool, owner?: "user" \| "trackseerr"}` | `PlexPlaylistSummary`. Owner change on a smart playlist returns 409. |
| GET | `/mixes?user=` | — | `PlexMix[]` |
| POST | `/mixes/snapshot?user=` | `{mix_key, title?: string, auto_refresh: bool}` | `PlexMixSnapshot` |
| GET | `/mixes/snapshots?user=` | — | `PlexMixSnapshot[]` |
| PUT | `/mixes/snapshots/{id}` | `{auto_refresh: bool}` | `PlexMixSnapshot` |
| DELETE | `/mixes/snapshots/{id}` | — | 204. Removes the snapshot record only; the Plex playlist is kept. |

Shapes (snake_case JSON):

```
PlexPlaylistSummary { rating_key: string, title: string, kind: "regular"|"smart", owner: "trackseerr"|"user"|"plexamp",
  ignored: bool, track_count: int, duration_ms: int, thumb_url: string|null, updated_at: string|null,
  trackseerr_playlist_id: string|null, plex_user: string }
PlexPlaylistItem { playlist_item_id: int, rating_key: string, title: string, artist: string, album: string, duration_ms: int }
PlexMix { mix_key: string, title: string, hub_title: string, track_count: int|null, thumb_url: string|null, snapshot_id: string|null }
PlexMixSnapshot { id, plex_user, mix_key, mix_title, playlist_title, rating_key: string|null, auto_refresh: bool, last_refreshed_at: string|null }
```

`thumb_url` must go through the existing image-proxy / MediaCover mechanism if there is one. **Never put the Plex token in a URL sent to the browser.** If there is no proxy, return `null`.

### Adopt semantics

Adopting creates a `playlists` row:
- `id = f"plex_{plex_user}_{rating_key}"`
- `service = "plex"`
- `name` = the playlist title
- `tracks_json` = a snapshot of `[{title, artist, album}]`
- `creator_id` = the caller

Targets start empty. The registry row gets `trackseerr_playlist_id` set, and the owner is left unchanged, because the source playlist is a *source*, not a sync target. In `api/routes/sync.py`, a `service == "plex"` playlist first refreshes `tracks_json` from the source Plex playlist (resolved through the registry by `trackseerr_playlist_id`). If the source is gone, it falls back to the stored snapshot and logs a warning. It is then synced to its targets as usual. Missing-track handling and acquisition work unchanged because they key off `tracks_json`. The sync must skip writing to the source playlist itself, whether matched by (user, title) or by rating_key.

### Mixes

Plexamp mixes are not stored playlists. They are recommendation hubs on the music library section. Implementation in `PlexClient.get_user_mixes(username)`:
- Get the user's server (admin or `switchUser`).
- For each music library section (`section.type == "artist"`), call `section.hubs()`.
- Keep hubs whose `hubIdentifier` or `title` contains "mix" (case-insensitive).
- Each hub item is a mix. Its `key` is the `mix_key`.
- Tracks come from `item.items()` when available. Otherwise fetch via `server.fetchItems(item.key)` and filter to `type == "track"`.
- Wrap every Plex call in specific exception handling (`NotFound`, `BadRequest`, `requests.exceptions.RequestException`). Log the root cause. Return `[]` on failure rather than raising.

A snapshot creates or replaces a regular playlist (default title `"<mix title> (Saved)"`) in that user's profile through the protected `update_or_create_playlist` path, registered as `trackseerr`. When the main sync run starts, it refreshes every snapshot with `auto_refresh = 1` by re-resolving `mix_key`. If the mix disappeared, it leaves the playlist alone and logs a warning.

## Frontend

A new **Plex** tab/section inside `PlaylistsView`:
- user picker (only rendered when `/users` returns more than one)
- list of playlists with kind badge (Regular / Smart) and owner badge (TrackSeerr / Yours / Plexamp), plus an ignored toggle and a "show ignored" filter
- detail modal: track list with remove and move up/down (disabled for smart), rename, delete with in-modal confirmation, copy-to-users (admin), adopt, and protection toggle
- **Mixes** sub-section: list mixes, "Save as playlist" with title and auto-refresh switch, manage existing snapshots

The design follows the `trackseerr_frontend_practices` skill.

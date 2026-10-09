# Per-User Scrobbling & Tailored Mixes — design contract

Status: in implementation on branch `feat/scrobbling-and-mixes`. This supersedes `~/.gemini/antigravity/handoffs/handoff_trackseerr_per_user_scrobbling_and_mixes.md` wherever the two disagree. Corrections against that handoff are marked **[corrected]**.

## Goals

1. Capture listens per TrackSeerr user. Sources:
   - a Plex webhook (needs Plex Pass on the server owner)
   - Plex server history polling as a fallback; this needs no Plex Pass
2. Forward each listen to Last.fm and/or ListenBrainz in the background.
3. One-click Last.fm connect. End users never see API keys. A sign-up link (https://www.last.fm/join) is shown to users without an account.
4. Admins can manage any user's scrobbling configuration.
5. Tailored mixes (`discover_weekly`, `daily_blend`, `artist_radio`):
   - compiled from listens plus Deezer related artists
   - optionally auto-acquire missing tracks within a weekly quota
   - written into the user's Plex profile through the existing **protected** `PlexClient.sync_playlist_to_users(..., db=db)` path, which never overwrites user- or Plexamp-owned playlists

No external daemons. The webhook ingestion route must never call external APIs synchronously.

## Identity mapping (Plex → TrackSeerr user) [corrected]

`users.id` is the plex.tv account id (set at Plex OAuth login, `api/routes/auth.py`). `users.username` is the plex.tv username.

Resolve a webhook or history account like this:
1. `str(account_id) == users.id` → that user.
2. `account_id` is `1` (or `"1"`): the server owner's local id in webhooks and history → the user with `is_admin = 1` whose username equals `plex_client` admin username (`PlexClient._get_admin_username()`). If that can't be resolved, use the first admin user.
3. Otherwise, a case-insensitive match of the account title/name against `users.username`.
4. No match → drop the event and log at INFO (`"listen from unknown Plex account <title>; user has not logged into TrackSeerr"`). Never create users.

For history polling, `PlexServer.systemAccounts()` gives local `accountID` → `name`. Feed id and name through the same resolver.

## Storage — migration **v25** [corrected: v24 is taken by plex playlist control]

```sql
CREATE TABLE IF NOT EXISTS user_scrobble_configs (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    scrobbling_enabled INTEGER NOT NULL DEFAULT 1,
    lastfm_username TEXT,
    lastfm_session_key TEXT,
    listenbrainz_token TEXT,
    listenbrainz_username TEXT,
    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
);
CREATE TABLE IF NOT EXISTS user_listens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    album TEXT,
    rating_key TEXT,
    duration_ms INTEGER,
    played_at TEXT NOT NULL,                 -- ISO-8601 UTC
    source TEXT NOT NULL,                    -- plex_webhook | plex_history
    lastfm_status TEXT NOT NULL DEFAULT 'skipped',       -- skipped | pending | sent | failed
    listenbrainz_status TEXT NOT NULL DEFAULT 'skipped',
    forward_attempts INTEGER NOT NULL DEFAULT 0,
    last_forward_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_listens_lookup ON user_listens(user_id, played_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_listens_artist ON user_listens(user_id, artist);
CREATE INDEX IF NOT EXISTS idx_user_listens_pending ON user_listens(lastfm_status, listenbrainz_status);
CREATE TABLE IF NOT EXISTS lastfm_auth_states (
    state TEXT PRIMARY KEY,                  -- secrets.token_urlsafe(24)
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    forward_url TEXT,
    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)  -- valid 10 minutes, single use
);
CREATE TABLE IF NOT EXISTS tailored_mix_configs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mix_type TEXT NOT NULL,                  -- discover_weekly | daily_blend | artist_radio
    name TEXT NOT NULL,
    seed_artist TEXT,                        -- required for artist_radio, else NULL
    track_count INTEGER NOT NULL DEFAULT 30,           -- 5..100
    discovery_ratio REAL NOT NULL DEFAULT 0.7,         -- 0.0..1.0
    seed_window_days INTEGER NOT NULL DEFAULT 14,      -- 1..90
    excluded_genres_json TEXT NOT NULL DEFAULT '[]',
    auto_acquire_missing INTEGER NOT NULL DEFAULT 0,
    max_weekly_acquisitions INTEGER NOT NULL DEFAULT 10,  -- 0..100
    quality_profile_id TEXT REFERENCES quality_profiles(id) ON DELETE SET NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_generated_at TEXT,
    last_result_json TEXT,                   -- TailoredMixResult of the last run
    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
);
CREATE INDEX IF NOT EXISTS idx_tailored_mix_user ON tailored_mix_configs(user_id);
CREATE TABLE IF NOT EXISTS mix_acquisitions (
    mix_id TEXT NOT NULL REFERENCES tailored_mix_configs(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    PRIMARY KEY (mix_id, request_id)
);
ALTER TABLE general_settings ADD COLUMN lastfm_api_key TEXT NOT NULL DEFAULT '';
ALTER TABLE general_settings ADD COLUMN lastfm_api_secret TEXT NOT NULL DEFAULT '';
ALTER TABLE general_settings ADD COLUMN plex_webhook_secret TEXT NOT NULL DEFAULT '';
ALTER TABLE general_settings ADD COLUMN plex_history_poll_minutes INTEGER NOT NULL DEFAULT 15;  -- 0 = off
```

`plex_webhook_secret` is generated (`secrets.token_urlsafe(32)`) the first time it is read empty, and then persisted.

**Env precedence:** `LASTFM_API_KEY` and `LASTFM_API_SECRET` env vars override the DB values. Add them to `Config` and document them alongside the other env vars.

**Dedup:** a listen is not inserted if the same `user_id` and `rating_key` already has a listen with `played_at` within ±10 minutes. This merges webhook and history events for the same play. When `rating_key` is null, dedup on (user_id, lower(artist), lower(title)) instead.

Required storage method names, so the mixes phase can rely on them:
- `get_scrobble_config(user_id) -> dict|None`
- `upsert_scrobble_config(user_id, **fields) -> dict`
- `create_lastfm_auth_state(user_id, forward_url) -> str`
- `consume_lastfm_auth_state(state) -> dict|None` (single use, expires after 10 min)
- `insert_listen(...) -> Optional[int]` (None when deduped)
- `list_listens(user_id, limit, offset) -> list[dict]`
- `list_pending_forwards(max_age_days=14, limit=200) -> list[dict]`
- `mark_forward_result(listen_id, service, status, error=None)`
- `top_artists(user_id, since_iso, limit) -> list[{artist, plays}]`
- `top_tracks(user_id, since_iso, limit) -> list[{artist, title, album, plays}]`
- `heard_track_keys(user_id) -> set[tuple[str,str]]` (lowercased (artist, title))
- `list_mix_configs(user_id=None)`, `get_mix_config(id)`, `create_mix_config(...)`, `update_mix_config(id, **fields)`, `delete_mix_config(id)`, `record_mix_result(id, result_json)`
- `count_mix_acquisitions_since(user_id, since_iso) -> int`: counts across all of that user's mixes, so the quota is per user per week
- `add_mix_acquisition(mix_id, request_id)`

## Outbound clients — `trackseerr/clients/scrobbler.py`

**`LastFmClient(api_key, api_secret, session=requests.Session(), timeout=10)`**
- **[corrected] HTTPS everywhere.** Auth URL: `https://www.last.fm/api/auth/?api_key=<key>&cb=<quote_plus(callback)>`. API: `https://ws.audioscrobbler.com/2.0/`.
- Signature: sort every param except `format` and `callback` by key, concatenate `key+value`, append the secret, then take the md5 hexdigest. This is the standard Last.fm rule. The handoff's hand-written concatenation is just the special case for getSession.
- Methods:
  - `exchange_token_for_session(token) -> (username, session_key)` using `auth.getSession`
  - `scrobble(artist, track, timestamp, session_key, album=None)` as a signed POST to `track.scrobble`
  - `now_playing(...)` as a signed POST to `track.updateNowPlaying`
- Last.fm returns HTTP 200 with `{"error": N, "message": ...}` on failure. Treat that as a failure: raise `LastFmError(code, message)`. Errors 9 (invalid session) and 4/14/15 (bad token) are permanent. On error 9, clear the user's session key and log at WARNING.

**`ListenBrainzClient`**
- `submit_listen(token, artist, track, release=None, timestamp=None)` POSTs to `https://api.listenbrainz.org/1/submit-listens` with `Authorization: Token <token>`.
- `validate_token(token) -> username|None` calls `/1/validate-token`.

**Resilience:** retry with backoff on 429/502/503/504 (repo invariant 3), max 3 attempts. Log every failure with its root cause. No bare `except`.

## Forwarding

- On insert, each service is set to `pending` when the user has `scrobbling_enabled` and credentials for that service; otherwise `skipped`.
- The route hands the listen id to FastAPI `BackgroundTasks`, which calls `forward_listen(listen_id)`.
- `ScrobbleWorker` (a new background thread, same pattern as `artist_refresh_worker.py`, started and stopped wherever that worker is) handles two jobs:
  - (a) Every `plex_history_poll_minutes`, polls Plex history: `server.history(mindate=last_poll)`, music tracks only (`type == "track"`). The cursor is persisted in the `general_settings` table or a key-value row.
  - (b) Every 5 minutes, retries `pending`/`failed` forwards younger than 14 days (Last.fm's limit), at most 5 attempts.

## API — `/api/scrobbles` (`api/routes/scrobbles.py`)

| Method | Path | Auth | Behavior |
|---|---|---|---|
| POST | `/plex?token=<plex_webhook_secret>` | token only (no session) | See the webhook notes below. |
| GET | `/webhook-url` | admin | `{url}`: full URL including the token, built from `application_url`. |
| POST | `/webhook-secret/rotate` | admin | Regenerate the secret and return `{url}`. |
| GET | `/lastfm/auth-url?forward_url=` | user | `{url}`. 503 `{detail:"Last.fm is not configured by the server admin"}` when no key/secret. `cb` = `<application_url or request base>/api/scrobbles/lastfm/callback?state=<state>`. `forward_url` must be a same-origin relative path, or it is dropped. |
| GET | `/lastfm/callback?state=&token=` | session cookie + state | See the callback notes below. |
| GET | `/config` | user | `ScrobbleConfig` (masked) for self. |
| PUT | `/config` | user | Body `{scrobbling_enabled?, listenbrainz_token?: string\|null, unlink_lastfm?: bool}`. Validates a ListenBrainz token via `validate_token`; an invalid one returns 400. |
| GET | `/users` | admin | `ScrobbleConfig[]` for every user, joined with `username`. |
| PUT | `/users/{user_id}/config` | admin | Same body as PUT `/config`, plus `lastfm_username?` and `lastfm_session_key?` (admin on-behalf). |
| GET | `/listens?limit=50&offset=0` | user | `UserListen[]` for self. Admins may pass `user_id`. |
| GET | `/server-config` | admin | `{lastfm_configured: bool, lastfm_api_key_masked: string, lastfm_from_env: bool, plex_history_poll_minutes}`. The secret is never returned. |
| PUT | `/server-config` | admin | `{lastfm_api_key?, lastfm_api_secret?, plex_history_poll_minutes?}`. Ignored with 409 when the env vars are set. |

**Webhook (`POST /plex`):**
- The token is compared with `hmac.compare_digest`. A mismatch returns 401.
- Body formats: Plex sends `multipart/form-data` with a `payload` JSON field, and also accept `application/json`.
- **[corrected] Do not add python-multipart.** Parse the multipart body with the stdlib (`email.parser.BytesParser` + `email.policy.HTTP`, after prepending the Content-Type header). The test image does not have python-multipart.
- Only `event == "media.scrobble"` with `Metadata.type == "track"` inserts a listen. `played_at` is now, in UTC.
- `media.play` / `media.resume` for a track trigger Last.fm `now_playing` in the background. They are never stored.
- The response is always 200 `{status: "ok"|"ignored"}` for well-formed requests. That way Plex does not disable the webhook.

**Callback (`GET /lastfm/callback`):**
- Consume the state. It must belong to the session user. On failure, redirect to `/?scrobble_error=state`.
- Exchange the token and save `lastfm_username` and `lastfm_session_key`.
- Redirect 303 to `forward_url or "/"` with `connected=lastfm` appended. On an exchange failure, redirect with `scrobble_error=lastfm`.

**Shapes:**

```
ScrobbleConfig { user_id, username, scrobbling_enabled: bool, lastfm_connected: bool, lastfm_username: string|null,
  listenbrainz_connected: bool, listenbrainz_username: string|null, updated_at: string|null }
UserListen { id: int, artist, title, album: string|null, played_at: string, source: "plex_webhook"|"plex_history",
  lastfm_status: "skipped"|"pending"|"sent"|"failed", listenbrainz_status: (same) }
```

Session keys and tokens are never returned, masked or otherwise. Only the `*_connected` booleans are exposed.

## Tailored mixes — `trackseerr/tailored_mixes.py`, `/api/mixes` (`api/routes/mixes.py`)

**Deezer additions to `DiscoveryClient`** (keyless, cached like the existing methods):
- `get_related_artists(deezer_artist_id, limit=20) -> list[{id, name}]` from `https://api.deezer.com/artist/{id}/related`
- `get_artist_top_tracks(deezer_artist_id, limit=10) -> list[{title, artist, album}]` from `/artist/{id}/top`

Resolve an artist name to an id with the existing `search_artist`.

**`compile_user_mix(db, discovery, config) -> list[MixTrack]`** (pure apart from the injected deps, so it is testable):
- **Seed artists:**
  - `artist_radio`: the `seed_artist`.
  - Otherwise, `top_artists` over `seed_window_days`, limit 10. `daily_blend` uses `min(seed_window_days, 3)`.
- **Familiar pool:** `top_tracks` over the window. For `artist_radio`, filter to the seed artist and the related artists.
- **Discovery pool:** top tracks of related artists for each seed, round-robin across seeds for variety. Exclude anything in `heard_track_keys`, any artist in the seed set (`discover_weekly` only), and duplicates by lowercased (artist, title).
- **Excluded genres:** apply only when genre data is available. Deezer related/top carries none, so log at DEBUG and skip silently. Do not fake it.
- **Blend:**
  - `n_new = round(track_count * discovery_ratio)`
  - fill from discovery, then from familiar
  - top up from the other pool if one is short
  - deterministic interleave
- Empty listen history with `mix_type != "artist_radio"` → raise `InsufficientHistoryError`, which the API turns into 409.

**`generate_and_sync(db, plex_client, discovery, config_row) -> TailoredMixResult`:**
1. Compile.
2. For each track, run `get_item_availability(db, artist_name=, track_title=)`.
3. Available tracks go into the playlist.
4. Missing tracks:
   - If `auto_acquire_missing` and `count_mix_acquisitions_since(user, now-7d) < max_weekly_acquisitions`:
     - create a `MusicRequest(item_type="track", status=APPROVED, user_id=..., quality_profile_id=config's)` through the existing `db.create_request` path. Read how `api/routes/requests.py` creates approved requests and mirror it, so the acquisition coordinator picks it up.
     - call `add_mix_acquisition`
   - Never exceed the quota.
5. Sync through `plex_client.sync_playlist_to_users(playlist, [username], db=db, ...)`, using the same config flags as `api/routes/sync.py`. The playlist name is `config.name`. Default names:
   - `"Discover Weekly · TrackSeerr"`
   - `"Daily Blend · TrackSeerr"`
   - `"<Artist> Radio · TrackSeerr"`

   A `Protected:` SyncResult error is reported in the result, not raised.
6. `record_mix_result`.

```
TailoredMixResult { mix_id, generated_at, total: int, available: int, missing: int, acquisitions_queued: int,
  quota_remaining: int, synced: bool, sync_error: string|null,
  tracks: [{artist, title, album: string|null, origin: "familiar"|"discovery", status: "available"|"missing"|"queued"}] }
```

**Scheduling:** a `MixWorker` thread (same pattern) checks hourly and regenerates enabled configs that are due:
- `discover_weekly`: every 7 days
- `daily_blend` and `artist_radio`: every 1 day

A due time is `last_generated_at + interval`. Each mix's errors are logged and do not stop the others.

**API `/api/mixes`** — users manage their own mixes. Admins may also pass `user_id` (query or body) to manage anyone's.

| Method | Path | Behavior |
|---|---|---|
| GET | `` | `MixConfig[]` |
| POST | `` | Create; returns 201 `MixConfig`. Validate the ranges; `artist_radio` requires `seed_artist`. |
| PUT | `/{id}` | Partial update → `MixConfig` |
| DELETE | `/{id}` | 204 |
| POST | `/{id}/preview` | Compile only, with no availability/acquire/sync → `{tracks:[{artist,title,album,origin}]}` |
| POST | `/{id}/generate` | Run `generate_and_sync` in BackgroundTasks → 202 `{status:"queued"}` |
| GET | `/{id}/result` | Last `TailoredMixResult`, or 404 |

```
MixConfig { id, user_id, mix_type, name, seed_artist: string|null, track_count, discovery_ratio, seed_window_days,
  excluded_genres: string[], auto_acquire_missing: bool, max_weekly_acquisitions, quality_profile_id: string|null,
  enabled: bool, last_generated_at: string|null }
```

Another user's mix returns 404 for a non-admin. That avoids leaking which mixes exist.

## Frontend

**Settings → "Scrobbling" subtab (all users):**
- Music Identity card:
  - Connected: shows the Last.fm username, a "Scrobbling Active" indicator and a Disconnect button.
  - Disconnected: a "Connect Last.fm" `TapeDeckButton`, which fetches `/lastfm/auth-url` and sets `window.location.href`, plus the text "Don't have a Last.fm account? Create one in 30 seconds" with the https://www.last.fm/join link (`target="_blank" rel="noopener noreferrer"`).
  - If 503 comes back, show "Your server admin hasn't enabled Last.fm yet" instead of the button.
- ListenBrainz accordion: a token input, a save button, and the connected username.
- Scrobbling enabled switch and a recent listens list (latest 20, with per-service status dots).
- On load, read `?connected=lastfm` or `?scrobble_error=` and show a toast/banner, then strip the parameters with `history.replaceState`.

**Admin-only additions in the same subtab:**
- Server Last.fm key/secret (write-only secret field). Read-only when `lastfm_from_env`.
- History poll minutes.
- Webhook URL with copy and rotate. Copy uses `navigator.clipboard.writeText`; also show the URL as selectable text.
- A users table (username, Last.fm, ListenBrainz, enabled) with an edit modal implementing PUT `/users/{id}/config`.

**Playlists view → "Mixes" segment** (next to Sync | Plex):
- One card per mix config, with: mix type, name, track count, a discovery ratio slider labeled "Familiar hits ↔ Deep discoveries", seed window, auto-acquire switch and weekly quota, enabled switch.
- "Preview" (list of tracks with origin badge) and "Generate & sync to Plexamp" (then poll `/result` every 3s, up to 60s).
- The last result summary.
- A "New mix" form. `artist_radio` asks for the seed artist.
- Admins get a user selector.

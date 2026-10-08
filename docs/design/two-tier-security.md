# Two-Tier (DMZ) Security Model — design contract

Status: in implementation on branch `feat/scrobbling-and-mixes`.

## Roles

- **gateway** (`ROLE=gateway`): the internet-facing Seerr-style app. Users sign in, browse and discover, request music, manage their own Plex playlists, connect their own Last.fm, and manage their own mixes. It holds **no** Plex token, **no** Last.fm API secret, **no** user session keys or listens, and **no** library or acquisition state.
- **core** (`ROLE=core`): the Lidarr-style admin engine. It runs on the internal Docker network and is reachable for admins only through a LAN/VPN-bound port. It holds all state and all secrets.
- **all-in-one** (default): both in one process. Behavior is unchanged except where noted.

## Model: Backend-for-Frontend with signed, least-privilege user assertions

This is the pattern used by hardened BFF/API-gateway deployments. Overseerr/Jellyseerr hold full admin API keys to Radarr/Sonarr, so a compromised Seerr owns the backends; this model deliberately avoids that.

- The gateway authenticates the end user. It then calls core **on that user's behalf** with an HMAC-signed assertion.
- Core verifies the assertion and authorizes as that user, using **core's** stored permissions and quotas.
- **A forwarded request can never be admin.** Admin is only possible through a direct core session (the LAN admin UI) or a core API key. A fully compromised gateway can therefore, at worst, act as an ordinary user on the allow-listed user endpoints.

### Signed assertion: `CoreClient` → core

Headers on every gateway→core call:

```
X-TS-User-Id:   <users.id, or "" for a service call>
X-TS-User-Name: <username, or "">
X-TS-Timestamp: <unix seconds>
X-TS-Nonce:     <secrets.token_hex(16)>
X-TS-Signature: hex(HMAC-SHA256(INTERNAL_CORE_SECRET,
                  METHOD \n PATH?QUERY \n user_id \n user_name \n timestamp \n nonce \n sha256_hex(body)))
```

Core verification is a new first branch in `get_current_user_or_api_key` / `get_current_user`. It runs whenever `X-TS-Signature` is present.

1. Reject (401) if any of the following holds:
   - core has no secret
   - the timestamp is more than 60s off the core clock
   - the nonce was already seen within the last 120s (in-memory TTL set)
   - `hmac.compare_digest` on the signature fails
2. If `user_id` is empty, return the **service principal**: `{id: "gateway_service", username: "gateway_service", is_admin: False, permissions: 0, forwarded: True}`.
3. If the user does not exist on core, upsert it with `is_admin=False` and default permissions. Upsert never grants admin and never changes the admin flag of an existing row.
4. Return core's user row with `is_admin` forced to `False`, the `ADMIN` permission bit stripped, and `forwarded: True`. `require_admin` therefore always returns 403 for forwarded calls.

The old **raw `X-Internal-Token` / `Bearer <secret>` → admin path is removed**. `CoreClient` stops sending `X-Api-Key: <secret>`. Today `X-Api-Key` is checked first, so it 401s unless the secret happens to equal an API key, and `X-User-Id` is ignored by core. The result is that forwarded requests either fail outright or run as admin, bypassing user quotas. The new scheme fixes both.

### Secret requirements

`INTERNAL_CORE_SECRET` is **required** when `ROLE` is `gateway` or `core`. It must be at least 32 characters. If it is missing or too short, startup (`cli.py`) logs an error and exits non-zero. all-in-one does not need it.

### Gateway: deny by default

In gateway role, a middleware allows only an explicit allowlist. Every other `/api/*` path returns **404**; it does not reveal that the endpoint exists. Static UI assets and the SPA fallback stay served.

The allowlist has two parts:
- the endpoints the gateway already serves locally today: auth/login/session, `/api/users/me`, discovery, request create/list-own, library availability. The implementer must enumerate these from the existing code and keep them working.
- the forwarded user endpoints below.

Forwarded via a generic `CoreClient.proxy(method, path, query, body, user)`. Status, JSON body and `Location` (for 3xx) are passed through. Request bodies are capped at 1 MiB.

| Prefix | Methods |
|---|---|
| `/api/plex-playlists` and below | all |
| `/api/mixes` and below | all |
| `/api/scrobbles/config` | GET, PUT |
| `/api/scrobbles/listens` | GET |
| `/api/scrobbles/lastfm/auth-url` | GET |
| `/api/scrobbles/lastfm/callback` | GET (the 303 `Location` is passed through) |

**Never on the gateway:**
- `/api/scrobbles/plex` (the webhook; Plex talks to core on the LAN)
- `/api/scrobbles/users*`, `/server-config`, `/webhook-url`, `/webhook-secret/*`
- settings, download clients, indexers, Lidarr, quality profiles, system, sync, library mutations, issues admin, and every other admin route

The Last.fm callback on core must accept the forwarded principal in place of the session cookie. State ownership is still checked against that user.

`GET /api/scrobbles/webhook-url` builds the URL from the **request's base URL**, which is the admin's LAN URL to core, not `APPLICATION_URL`. Plex must reach core on the LAN, not the internet.

### Tier exposure to the UI

`GET /api/auth/me` (or the existing current-user endpoint) adds `tier: "gateway" | "core" | "all-in-one"`. When `tier == "gateway"`, the frontend hides every admin UI, even for admin users. It shows a note: "Admin settings are available on the TrackSeerr Core admin interface."

## Deployment (`docker-compose.hardened.yml`)

- Both services get `INTERNAL_CORE_SECRET=${INTERNAL_CORE_SECRET:?set a 32+ char secret}`.
- Core joins a second, non-internal network `core-lan`. It publishes `"${CORE_LAN_BIND:-127.0.0.1}:5251:5251"` (LAN-IP or loopback bound, never `0.0.0.0` by default). It stays on `trackseerr-internal-net` for the gateway.
- The gateway must not get `PLEX_TOKEN`, `LASTFM_API_SECRET` or a data volume. Comments say so.
- Document in the README:
  - the model
  - how to generate the secret (`openssl rand -hex 32`)
  - point the Plex webhook at the core LAN URL
  - admins use the core LAN UI

## Access policy (applies on every tier, enforced server-side)

Non-admin users may only use:
- **Discover**
- **Requests**: create, list their own, cancel their own pending requests.
- **Account**: their own profile, plus scrobbling (Last.fm and ListenBrainz connect, their own listens).
- **Their own playlists**:
  - TrackSeerr sync playlists they created or that target them (`playlists.creator_id`, `playlist_targets`)
  - their own Plex playlists
  - their own tailored mixes

Everything else is **admin-only** and returns 403 for non-admins (404 on the gateway):
- Library and its management: artists, albums, tracks, stats, scan, collections, manual import, Lidarr migration. One exception stays open to users: `GET /api/library/availability`, which discover and requests use to show "in library".
- Activity: system events, logs and the log stream, plus the queue, acquisition, backlog and missing tracks (including the Lidarr push and queue controls).
- Global sync trigger and status, the user list, settings, download clients, indexers, quality profiles and notifications.
- Media issues: user-facing issue reporting (create, list/get their own, comments, unread-count, seen, reopen/close via `POST /{id}/status`) stays open to users and is forwarded by the gateway; view-all, `PUT`, `DELETE`, `open-count` and `actions/*` are core-only admin (`open-count` is on `GATEWAY_FORWARD_DENYLIST` because `/api/issues/{}` would otherwise match it).

Rules for user-scoped routes:
- **Ownership is checked on the server.** A non-admin acting on another user's object gets **404**, not 403, so the response does not reveal that the object exists.
- **The UI mirrors the policy.** Non-admins see only the tabs Discover, Requests, Playlists and Account/Settings → Scrobbling.

## Accepted residual risks (documented 2026-10-03)

- **A compromised gateway can impersonate any non-admin user.**
  - The gateway signs assertions, so it can claim any user id and send a fresh `X-TS-Session-Issued-At`. That lets it evade `sessions_revoked_at`.
  - Core still enforces `disabled` and tombstones on every forwarded call.
  - Admin accounts are always refused through the gateway.
- **Targeted lockout.** Anyone who knows a username can keep that local account locked: 5 failures per 15 minutes trips the lockout. This is inherent to lockout-based throttling. Plex sign-in is unaffected, and the admin works on the core LAN UI.
- **TOTP secrets are stored unencrypted in the core DB.** They are never returned after enrollment. The DB is reachable only on core, which sits on the internal network. Encrypting them with a key held in the same environment would add little.
- **The gateway checks the session with core and caches the answer for up to 60 s.**
  - Disabling, deleting or revoking a user takes effect on gateway-local routes within 60 seconds.
  - Forwarded routes reject immediately.
  - The gateway fails closed: when core can't be reached, it returns 503.
- **The Plex server owner is always admin** (owner decision). This matches Overseerr and Jellyseerr.
- **Run one worker process per tier.** Per-username login serialization and the per-IP in-flight cap are in-process. With several uvicorn workers, those two limits multiply by the worker count. The DB-backed failure counts and the lockout still hold.

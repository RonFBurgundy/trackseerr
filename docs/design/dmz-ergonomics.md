# DMZ Setup Ergonomics — design contract

Status: in implementation on branch `feat/dmz-ergonomics`. Builds on `docs/design/two-tier-security.md`. Owner decisions (2026-10-03):
- one codebase and one image
- two roles that are impossible to confuse
- three Unraid templates
- all-in-one → DMZ is a role change on the existing container, with no data migration

## 1. Startup guardrails (`cli.py` and `create_app`, run before serving)

**Gateway** (`ROLE=gateway`) **refuses to start** (exit 1, with one clear stderr line per problem, then a hint to see the README "Two-tier deployment" section) when any of these is true:
- `PLEX_TOKEN`, `LASTFM_API_SECRET`, `LASTFM_API_KEY`, Spotify/Deezer secrets, download-client or indexer env credentials are set. Read `config.py` for the full secret list, and keep that list in one constant `GATEWAY_FORBIDDEN_ENV`.
- `/config` or `/data` is a mount point containing a TrackSeerr DB (`os.path.ismount`, or the DB file exists at the configured path).
- `INTERNAL_CORE_SECRET` is missing or under 32 characters (already enforced; keep it).
- `TRACKSEERR_CORE_URL` is missing or not `http(s)://`.
- `APPLICATION_URL` is missing.

**Core** (`ROLE=core`) **warns** (it does not refuse) when:
- `APPLICATION_URL` resolves to the core's own listen address/port, which suggests core is being exposed publicly.
- It binds `0.0.0.0` with no `CORE_LAN_BIND` hint. Log this once at WARNING.

**Core refuses** when `INTERNAL_CORE_SECRET` is missing or weak (already enforced).

**all-in-one**: no new checks.

Guardrails must be unit-testable: a pure function `check_role_environment(role, env, fs) -> list[Problem]`, with `fs` injectable.

## 2. Version/protocol handshake

- Add `PROTOCOL_VERSION = 1` in `internal_auth.py`. This is the gateway↔core contract version, independent of the package version.
- `pyproject.toml` (`0.1.0.dev1`) and `plex_playlist_sync/__init__.py` (`1.0.0`) disagree. Make `__init__.__version__` read the installed package metadata (`importlib.metadata.version("trackseerr")` or the actual dist name), with a fallback. Set `pyproject` to the real version (`1.0.0`) so they agree.
- Core `GET /api/internal/hello` is service principal only, and every other caller gets 404. It returns `{protocol: 1, version: "x.y.z", role: "core", instance_id}`. `instance_id` is a random id persisted in `general_settings` on first use.
- **Gateway at startup:**
  - Calls `hello` and retries with backoff for up to 60 s. The core may still be booting.
  - On a protocol mismatch, it refuses to start: `"Gateway protocol 1 != core protocol 2 — run the same TrackSeerr version on both containers"`.
  - On a package version mismatch with the same protocol, it logs a WARNING and starts.
  - If core is unreachable after 60 s, it starts and fails closed (503), as it does today. It logs the error and keeps retrying the handshake every 30 s.
- **Gateway heartbeat:** every 60 s it calls core `POST /api/internal/gateway-heartbeat` (service principal) with `{version, protocol, gateway_id, started_at, active_sessions}`. `gateway_id` is random per process; `active_sessions` is a count only. Core stores the last heartbeat in memory, plus a `gateway_status` key-value row so the status survives restarts.

## 3. Request-portal status (core admin UI)

- `GET /api/admin/gateway-status` is admin-only and core-only. It returns:
  ```
  {configured: bool, last_seen_at: string|null, version: string|null, protocol: int|null,
   version_match: bool|null, active_sessions: int|null, state: "online"|"stale"|"never_seen"|"not_used"}
  ```
  - `stale` means last seen more than 3 minutes ago.
  - `not_used` means the role is all-in-one.
- Frontend: Settings → System gets a "Request portal" card. It shows the state badge, last seen, version, the version-match warning, active sessions and the public URL (`APPLICATION_URL`). It is hidden when the state is `not_used`.

## 4. Distinct identity

- `GET /api/auth/me` and `/api/health` already expose `tier`. On the gateway, the frontend:
  - titles the page and header **"TrackSeerr Requests"**
  - shows the footer note "Settings are managed in TrackSeerr Core"
  - updates the `<title>` and the PWA name dynamically where feasible
- On core, the header shows a small "Core" badge.
- **Unraid**: three templates in `unraid/`:
  - `trackseerr.xml`: all-in-one. Keep it, and tidy the descriptions.
  - `trackseerr-core.xml`:
    - `ROLE=core`, fixed: hidden or with a description saying "do not change".
    - `INTERNAL_CORE_SECRET` is required and masked.
    - `APPLICATION_URL` is the public gateway URL.
    - The port is mapped for LAN admin.
    - The `/config` and `/data` volumes are mounted.
    - All Plex, Last.fm and download settings are present.
    - The overview says "Pair with TrackSeerr Requests", with network setup steps.
  - `trackseerr-requests.xml`:
    - `ROLE=gateway`, fixed.
    - Only `TRACKSEERR_CORE_URL`, `INTERNAL_CORE_SECRET` (masked, required), `APPLICATION_URL`, the port, `TRUSTED_PROXIES` and the TZ/PUID/PGID basics.
    - **No** Plex, Last.fm or download fields, and **no** volumes.
    - A distinct `<Name>` and `<Icon>`, e.g. a variant icon generated by `unraid/make-icon.py` if it supports that.
  - Both split templates' overviews include the one-time `docker network create trackseerr-internal` step. They also include the Extra Parameters line, `--network=trackseerr-internal`, for joining the second network, with the `docker network connect` fallback.
- **Network name is configurable:** env `TRACKSEERR_INTERNAL_NETWORK` is documentation-only. The compose files use `${TRACKSEERR_INTERNAL_NETWORK:-trackseerr-internal}`.

## 5. `init-dmz` command

`python -m plex_playlist_sync init-dmz [--from-existing] [--public-url URL] [--core-lan-bind IP] [--network NAME] [--out DIR]`:
- Generates a 64-hex-character secret with `secrets.token_hex(32)`.
- Writes `docker-compose.dmz.yml` and `.env` into `--out` (default: the current directory). `.env` gets mode 0600 and holds the secret. It **never overwrites** existing files: it adds a `.new` suffix and prints a notice.
- Prints the exact Unraid values for both templates: the secret, URLs and network.
- `--from-existing` reads the running all-in-one container's env from `/proc/self/environ`, or `--env-file PATH`, and carries the non-secret settings into the core service. Secrets such as `PLEX_TOKEN` are carried over **by reference** (`${PLEX_TOKEN}` in compose, with the value only in `.env`) and never printed to stdout.
- No network calls, and nothing goes to stdout apart from the setup instructions and the Unraid values, which include the new secret. That is the one moment it's shown.

## 6. All-in-one → DMZ migration

- The core is the existing all-in-one container with `ROLE=core`: same image, `/config` and DB. Nothing in the DB is role-specific.
- **First boot as core:** if `general_settings.last_role` differs from the current role, core logs a one-time checklist at WARNING:
  - set `APPLICATION_URL` to the public gateway URL
  - point the proxy or tunnel at the gateway
  - the Plex webhook URL is unchanged
  - users sign in once on the gateway

  The admin UI shows the same checklist as a dismissible banner. Then `last_role` is updated.
- Going back (`ROLE=all-in-one`) works the same way.
- **Tests:** a DB populated as all-in-one (users, a playlist, requests, settings) boots as core with every row intact and every admin route working. The same holds core → all-in-one. Migrations are idempotent across role flips.

## 7. Docs and compose

- `docker-compose.hardened.yml` uses the configurable internal network name. The gateway attaches to the **user's existing proxy network**, external and named by `${PROXY_NETWORK:-proxynet}`. Core attaches to the LAN or default bridge plus the internal network.
- **README:**
  - which mode to choose: all-in-one for LAN/VPN/Tailscale; two containers whenever the request app faces the internet
  - how the internal network coexists with proxynet, br0, Cloudflare Tunnel (`cloudflared` must share a network with the gateway, not with core) and reverse proxies
  - the migration steps
  - `init-dmz`
  - verified multi-network notes (see below)
- **Multi-network verification:** on the dev Docker (29.x), verify that one container can be started on two networks with repeated `--network` flags in a single `docker run`, and that `internal: true` blocks egress while gateway↔core traffic works. Record the exact commands and results in the README. Unraid itself can't be tested here, so say so.

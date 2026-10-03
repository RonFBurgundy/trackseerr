# Backlog (queued 2026-10-03, in order)

1. ~~**Issue reporting UI**~~ (done): "Report issue" on album/artist detail in Discover, plus a "My issues" list. The server API already exists and is user-accessible.
2. (done) **User management (Settings → Users, admin, core-only)**:
   - list, view and delete users
   - edit the request quota per user
   - granular RBAC permission editing (the existing permission bitflags)
3. (done) **Local accounts**: username/password users for people without Plex. Plex OAuth stays the default. Needs:
   - password hashing (argon2/bcrypt)
   - admin create/reset
   - user self-service password change
   - login rate limiting
   - gateway-safe login flow
4. (done) **Expanded request quotas**:
   - separate limits for albums, tracks and discographies
   - per-user auto-approve vs admin approval, per request type
5. (done) **Leak hardening in `clients/plex.py`**: `sync_playlist_to_users` / `update_or_create_playlist` build `SyncResult.error` strings and log lines from `str(e)`. These can carry Plex URLs. Mixes redact them downstream, but the sync route and the CLI do not.
6. ~~Split-port single-container mode~~ (dropped 2026-10-03, owner decision: all-in-one for LAN/VPN, two containers for internet exposure)
7. (done) **DMZ setup ergonomics** (agreed 2026-10-03; next after the leak fix):
   - (1) gateway/core startup guardrails
   - (2) gateway↔core version/protocol handshake
   - (3) "Request portal" status panel in the core admin UI
   - (4) distinct gateway identity ("TrackSeerr Requests") plus three Unraid templates:
     - all-in-one
     - Core (ROLE=core fixed)
     - Requests (ROLE=gateway fixed; no Plex, Last.fm or volume fields)
   - (5) `trackseerr init-dmz`, which generates the secret plus a filled-in compose file
   - Acceptance criteria (added 2026-10-03):
     - The internal network is additive and isolated. It never replaces proxynet, br0 or tunnel networks, and its name is configurable.
     - The gateway stays on the user's proxy or tunnel network.
     - Core stays on br0/LAN and also joins the internal network.
     - Verify multi-network attachment on real Docker (Unraid: a repeated `--network` in Extra Parameters needs Docker 25+). If it doesn't work, document `docker network connect` as the fallback.
     - All-in-one → DMZ is a ROLE change on the existing container: same image, same /config and DB, with library, users and config intact.
       - Test that an all-in-one DB boots unchanged as core, and back again.
       - `init-dmz --from-existing`.
       - A first-boot-as-core checklist.
       - A README migration section, noting that users sign in once more on the gateway.
8. **Low: admin-only connection-test errors.** Lidarr (`settings.py` ~508), download-client, indexer and quality-profile/settings DB errors return `str(e)` to the admin. Route them through `redaction.safe_exc`/`redact_text` for consistency (logs are already redacted by the root filter).

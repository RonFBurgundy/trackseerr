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
5. **Leak hardening in `clients/plex.py`**: `sync_playlist_to_users` / `update_or_create_playlist` build `SyncResult.error` strings and log lines from `str(e)`. These can carry Plex URLs. Mixes redact them downstream, but the sync route and the CLI do not.
6. **Split-port single-container mode**, a middle ground between all-in-one and the two-container setup. The public port gets the gateway deny-by-default allowlist; admin is reachable only on a LAN-bound port. README guidance: all-in-one for LAN/VPN only; two containers whenever the request app faces the internet.

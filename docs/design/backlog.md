# Backlog

Persistent working list (orchestrator-maintained). Items here are NOT part of `various-artists.md`; that plan covers
per-track credits, VA naming, Appears On/Remixes, single-compilation requests, misfiled detection and MB/AcoustID
contributions. Priority: P1 = user-facing bug or data risk, P2 = noticeable friction, P3 = polish.

## User QA list (2026-10-12)

1. **P2 · Searches run with no indexers or clients configured.** Wanted sweep / RSS / backlog search monitored tracks
   right after onboarding or a Lidarr import, burning CPU and filling logs. Gate every search task on "≥1 enabled
   indexer AND ≥1 enabled download client for that protocol"; skip with one INFO line ("no indexers configured —
   skipping") instead of per-item logs. Show the reason on the Tasks page. Start: `trackseerr/backlog_worker.py`,
   RSS sync, `task_manager.py`.
2. **P2 · Tasks can't be paused, cancelled or ended gracefully.** Add cancel (and pause/resume for interval tasks) to
   the task manager: a per-run stop event checked between items, a "Cancel" button on the Tasks page, and a graceful
   shutdown that signals all running tasks to stop at the next item instead of waiting minutes (e.g. switching to
   Trackseerr mid-run). Manual tasks currently pass `should_stop=None` (credits backfill, art backfill) — wire them.
   Start: `trackseerr/task_manager.py`, `trackseerr/api/routes/system/tasks.py` (`_run_manual`, `_running_tasks`).
3. **P1 · Requesting a track that's already in the library.** Discover/search results don't always show the
   "In library" badge, so the request goes through; the track page does detect it. Two fixes: (a) server-side guard
   in `create_request` (`trackseerr/api/routes/requests.py` ~L89) — reject/short-circuit when the library has a
   matching track (recording MBID, ISRC, or artist+clean title), returning "already in library"; (b) find why the
   search results miss the badge on a 30k-track library (likely a capped/paginated in-library lookup) and fix it.
4. **P1 · Library sorted by tracks takes minutes; track search slow (30k tracks, 317k catalog tracks).** Profile the
   tracks paged/index endpoints (`trackseerr/api/routes/library/browse.py` `/tracks/paged`, `/tracks/index`): likely
   OFFSET paging + sort over all catalog tracks instead of tracks-with-files, missing indexes on sort/search columns,
   or N+1 lookups. Same fix pattern as the rename preview (keyset paging, drive from library_files when "owned only").
5. **P3 · Use the tape-deck loader + messages instead of plain spinners.** Library modal, track/artist pages, Wanted
   and its sub-pages, Activity sub-pages, etc. Idea: tape deck with rotating messages overlaid on the skeleton while
   content loads. Find the existing tape-deck loader/messages (App.tsx / views use them) and make one shared
   `<PageLoader>` used everywhere a spinner is shown. Frontend skill applies.
6. **P3 · Nav drawer: opening a section should collapse the others** (accordion). With Library expanded, tapping
   Settings leaves its sub-pages below the fold. Frontend skill applies; find the drawer under
   `frontend/src/components/layout`.

## Carried over from the 2026-10-12 rename/naming session

7. **P2 · Rename preview leftovers after the naming fixes:** ~130 disc/track renumberings that come from Lidarr's
   own track lists (e.g. Baby Keem "The Melodic Blue" now 2 discs; Miami Horror "I Look to You" mapped to a remix
   track) — investigate whether the file→track mapping from the Lidarr import is wrong or Lidarr's release choice
   changed; ~30 bad Lidarr metadata values (Rod Stewart "Every Picture Tells a Story" dated 1884, "Miley Cyrus" →
   "MILEY", Brent Cobb 2006) — an artist refresh from MusicBrainz should correct them; 9 collisions (two files →
   same path). The preview now labels these and leaves them unticked.
8. **P2 · Library-wide retag pass writing MusicBrainz IDs** into every file (recording, release, release group,
   artist, album artist) once the data is clean — this is what makes Plex/Jellyfin/Picard match reliably.
9. **P2 · Plex play history across folder moves.** Before the user's one-time "Rename artist folders" cleanup,
   check how Trackseerr's Plex integration handles moved files (Plex tracks by path; moves can reset play counts/
   ratings) and whether a Plex rescan/refresh call after rename keeps them.
10. **P3 · New downloads with no catalog match** still render the artist folder from the format (no artist row);
    look up the library artist by name/MBID so they land in the existing folder too.
11. **P3 · Rename preview response has no total** — the UI counts returned rows; add a total if the 5,000 cap is hit.
12. **P3 · Housekeeping:** an 8-day-old `tsc -w` process from another session is running on the host.

## Earlier backlog (queued 2026-10-03; all done or dropped)

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
8. ~~**Low: admin-only connection-test errors.** Lidarr (`settings.py` ~508), download-client, indexer and quality-profile/settings DB errors return `str(e)` to the admin. Route them through `redaction.safe_exc`/`redact_text` for consistency (logs are already redacted by the root filter).~~ (done 2026-10-03)

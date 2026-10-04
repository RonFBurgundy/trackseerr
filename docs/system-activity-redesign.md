# System / Activity / Library-manager redesign — design contract

Owner decisions, 2026-10-04. Delivered in phases. Each phase gets its own branch and merges to `main` when green.

## Library manager mode (interlock)
- A single setting, `library_manager`, set to `trackseerr` or `lidarr`. It is mutually exclusive, because two managers working one library would double-grab and fight over imports and renames.
- The settings of the inactive side stay visible, greyed out and **preserved**, so switching back loses nothing.
- A switch is refused while downloads are in flight.
- **Lidarr mode:**
  - TrackSeerr acts as a request front-end and monitoring UI for Lidarr, through the Lidarr API v1:
    - `/queue`, `/history`, `/blocklist`
    - `/wanted/missing`, `/wanted/cutoff`
    - `/system/status`, `/health`, `/command`
  - Supported actions:
    - remove or blocklist a queue item
    - trigger a search
    - retry
  - Lidarr's *configuration* (profiles, indexers, clients, its own logs and tasks) is **not** mirrored. Duplicating its config UI is high-maintenance and breaks on Lidarr upgrades, so those pages show a one-line notice with a link to Lidarr instead.
  - The Lidarr API key stays server-side, and every route is admin-only.
- Lidarr settings gain these fields:
  - root folder
  - quality profile
  - metadata profile
  - monitor option
  - search-on-add
  - tags
- **No dead pages.** A page with nothing to show in the current mode shows one line of explanation with a link. It never renders blank.

## Settings structure
The order is: General · Media Management · Lidarr · Requests · System · Account.
- **Media Management** (TrackSeerr mode): Root Folders & Naming (the current "Media" tab, renamed so it doesn't clash), Profiles, Clients, Indexers.
- **Requests**: Users, Scrobbling.
- **System**, a new page with a new icon: Status, Queue (scheduled tasks and running processes, as a flat list), Tasks, Events, Logs. This absorbs the old Settings Tasks and Status tabs.

## Top-level pages
- **Activity**: Queue, History and Blocklist, Arr-style. These are flat, sortable lists with columns for track, release, indexer, client, progress and status. A stalled item gets a flag and manual actions. The source is TrackSeerr's own data or Lidarr's, depending on the mode.
- **Wanted**, a new page:
  - **Missing** lists monitored items that aren't in the library.
  - **Cutoff Unmet** lists files below their profile cutoff. This needs per-file quality recorded by a scan pass.

## Lists, site-wide
- Infinite scroll with lazy loading replaces page numbers.
- A tall list uses a scrubber rail in place of the scrollbar, in the tape-deck style:
  - Its groupings follow the active sort: A–Z and # for names, years or months for dates, and size buckets for sizes.
  - On desktop, clicking a group jumps to it, and the current position balloons with a subtle magnifier.
  - On mobile, the rail is thin and draggable.
  - The server provides group offsets so a jump lands without loading every row.

## Phases
1. **Bugs:**
   - the profile `upgrade_allowed` toggle isn't persisted
   - Plex sign-in doesn't set `last_login_at`
   - the Requests Pending filter misses pending requests
   - the startup boot-log and loading page
2. Library-manager mode, the settings restructure and the System page.
3. The Activity rebuild, plus Wanted (Missing and Cutoff Unmet).
4. Infinite scroll and the scrubber.

## Phase 2 API contract (frozen for parallel backend/frontend work)
The existing `media_management_settings.library_mode` (`native` | `lidarr`) is **the** interlock. The UI labels these "TrackSeerr" and "Lidarr".

- `GET /api/settings/library-manager` (admin) returns:
  `{mode: "native"|"lidarr", lidarr_configured: bool, native_configured: bool, can_switch: bool, blocking_reason: string|null}`
  - `native_configured` means at least one enabled non-Lidarr download client and at least one enabled indexer (or slskd).
- `PUT /api/settings/library-manager {mode}` (admin) returns the same shape.
  - It returns **409** with `blocking_reason` when work is in flight: non-terminal native acquisition queue items, or a running Lidarr trickle.
  - It returns **422** when switching to `lidarr` while Lidarr is not configured.
  - It writes an event, `library_manager_changed`.
- The existing media-settings PUT must **not** be able to change `library_mode` any more; the field is ignored there with a logged warning. The Lidarr importer (`lidarr_migration`) may still flip the mode to `native`.
- `GET /api/settings/lidarr/options` (admin, Lidarr mode or configured) returns:
  `{root_folders:[{path, free_space}], quality_profiles:[{id,name}], metadata_profiles:[{id,name}], tags:[{id,label}]}`
  - It is live from Lidarr and returns 502 with a redacted message on failure.
- `lidarr_settings` gains `monitor_option` (`all`|`future`|`missing`|`existing`|`first`|`latest`|`none`, default `all`), `search_on_add` (bool; reuse `auto_search` if that is what it is) and `tag_ids` (a JSON list of int). Root folder and the profile ids already exist. GET and PUT on the Lidarr settings route expose them.
- `GET /api/system/queue` (admin) returns `{running:[Job], queued:[Job], recent:[Job]}`, where `Job = {id, task_id, name, state:"queued"|"running"|"completed"|"failed"|"cancelled", started_at, finished_at, duration_ms, message}`.
  - `recent` holds the last 50 jobs, in memory.
  - Every task execution, scheduled or manual, records a Job.
- `GET /api/system/lidarr-health` (admin) returns `{mode, reachable, version, health:[{source, type:"ok"|"notice"|"warning"|"error", message, wiki_url}]}`.
  - It returns `{mode:"native", reachable:null, health:[]}` in native mode, and it never 500s.
- **Enforcement:**
  - In `lidarr` mode:
    - request submission never uses native acquisition, and approved requests go to Lidarr using `lidarr_settings`
    - the acquisition, backlog and RSS workers and the filesystem scanner do nothing natively, and log the skip once per cycle at DEBUG
  - In `native` mode:
    - no requests or grabs go to Lidarr, including the Lidarr trickle and auto-trickle and any Lidarr `driver_type` download client
    - the Lidarr importer stays available as a one-way migration tool

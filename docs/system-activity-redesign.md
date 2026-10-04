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

## Phase 3 API contract (Activity + Wanted), frozen for parallel work
Every list endpoint is admin-only and core-only, and every one dispatches on `library_mode`:
- **native:** from TrackSeerr tables
- **lidarr:** proxied live from Lidarr API v1, with Lidarr's own paging and sort

**List query:** `?page=1&page_size=50&sort_key=<key>&sort_dir=asc|desc` (`page_size` 1–200). **List response:**
`{mode, page, page_size, total, sort_key, sort_dir, records: [...]}`
Unknown sort keys return 422.
Lidarr failures return 502, with the message passed through `redact_text`.

### Activity
- **`GET /api/activity/queue`**
  - **Record:** `{id, source:"native"|"lidarr", artist, album, title, item_type, quality, protocol, indexer, client, status, progress (0-1), size_bytes, sizeleft_bytes, eta_seconds, added_at, stalled: bool, stalled_reason, messages:[string], request_id: string | null}`
  - **Sort keys:** `added_at`, `artist`, `title`, `progress`, `status`, `size_bytes`
  - **Stalled:**
    - native: `downloading` with no progress change for 30 min (needs `progress_updated_at`), or `status = warning`
    - lidarr: `trackedDownloadStatus` is `warning` or `error`
- **`DELETE /api/activity/queue/{id}?remove_from_client=bool&blocklist=bool`**
- **`POST /api/activity/queue/{id}/retry`**
  - native: re-search for the request. The stuck row is dropped (and cancelled at its client) only when the search actually grabbed a release; otherwise the row stays and the body is `{success:false, message}` (HTTP 200)
  - lidarr: `AlbumSearch` command for that album
- **`GET /api/activity/history`**
  - **Record:** `{id, source, event:"grabbed"|"imported"|"failed"|"deleted"|"blocklisted"|"upgraded", artist, album, title, quality, indexer, client, date, message, can_mark_failed: bool}`
  - **Sort key:** `date`
  - **Filter:** `?event=`
  - **Native:** a new append-only `download_history` table, written at grab, import, fail, blocklist and upgrade. Seed it once from terminal `active_downloads` rows.
- **`POST /api/activity/history/{id}/failed`**: mark a grab as failed, which blocklists it and re-searches
  - **Idempotent:** only the latest `grabbed` event of a download with no later `failed`/`blocklisted` event qualifies (`can_mark_failed`); anything else is a 409 and writes nothing. In lidarr mode `can_mark_failed` is true for `grabbed` events and Lidarr arbitrates.
  - **Result:** the release is always blocklisted ("failed" means that). The body is `{success:true, message}`: "Marked as failed and blocklisted. Grabbed '...'" when a replacement was grabbed, "Marked failed; no replacement found yet" when not.
  - **Mutation bodies:** every Activity mutation returns `{success, message}`; clients must show `message` as an error when `success` is false.
- **`GET /api/activity/blocklist`**
  - **Record:** `{id, source, artist, album, title, release_title, quality, indexer, protocol, reason, date}`
  - **Sort keys:** `date`, `artist`
- **`DELETE /api/activity/blocklist/{id}`**

### Wanted
- **`GET /api/wanted/missing`**
  - **Record:** `{id, source, artist, album, title, item_type:"track"|"album", release_date, monitored, last_searched_at}`
  - **Sort keys:** `artist`, `album`, `title`, `release_date`, `last_searched_at`
  - **Native:** monitored `library_tracks` with no `library_files` row
  - **Lidarr:** `/wanted/missing`, at album level
- **`GET /api/wanted/cutoff`**: same record shape plus `{current_quality, cutoff_quality}`
  - **Native:** `library_files.cutoff_met = 0` for monitored tracks, with profiles that allow upgrades only
  - **Lidarr:** `/wanted/cutoff`
- **`POST /api/wanted/search {ids:[...]} | {all:true, list:"missing"|"cutoff"}`**
  - native: queue searches through the existing backlog machinery, under `work_guard`
  - lidarr: `AlbumSearch` or `MissingAlbumSearch` / `CutoffUnmetAlbumSearch` commands
  - returns `{queued: n, message?}`. Native mode queues at most 1000 per call, runs one batch at a time (a request during a running batch returns `{queued:0, message:"A search batch is already running"}`, HTTP 200) and skips tracks whose `last_searched_at` is within 10 minutes (the message says how many were skipped).

#### Sort-key mapping to Lidarr API v1
| Our `sort_key` | Lidarr `sortKey` |
|---|---|
| queue `added_at` | `added` |
| queue `artist` | `artists.sortName` |
| queue `title` | `albums.title` |
| queue `progress` | `progress` |
| queue `status` | `status` |
| queue `size_bytes` | `size` (not in the verified Lidarr list; Lidarr falls back to its default order for a key it does not know) |
| history `date` | `date` |
| blocklist `date` / `artist` | `date` / `artists.sortName` |
| wanted `artist` | `artists.sortName` |
| wanted `album`, `title` | `albums.title` |
| wanted `release_date` | `albums.releaseDate` |
| wanted `last_searched_at` | none: Lidarr cannot sort by it, so in lidarr mode the API answers 422 and the UI disables that column's sort (native sorts it normally) |

`timeleft`, `estimatedCompletionTime` and `quality` are valid Lidarr queue sort keys that we do not expose.

#### Dates and migration notes
Release dates are calendar dates (`YYYY`, `YYYY-MM`, `YYYY-MM-DD`, or Lidarr's `...T00:00:00Z`) and render without timezone conversion; timestamps render in local time. Migration v33 starts the stall clock (`progress_updated_at = now`) for non-terminal rows that have none, only where it is NULL.

Every mutating action runs under `work_guard` for the active mode.

### Frontend
- **Top-level Activity:** Queue · History · Blocklist
- **New top-level Wanted (admin):** Missing · Cutoff Unmet, with a distinct icon
- **Lists:** shared flat, sortable list components with infinite scroll and lazy-loaded pages. The phase 4 scrubber attaches to these same components.

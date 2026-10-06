# Per-indexer seed rules — design contract

Owner request, 2026-10-05. Branch `feat/indexer-seed-rules`, which follows `feat/acoustid-fingerprinting`. That branch introduces the shared post-import governance function this one extends.

## Problem
Seeding limits are global today: `media_management_settings.seed_ratio_limit` and `seed_time_limit_minutes`. Private trackers each have their own rules, for example "1.0 ratio or 72 h", or a hit-and-run window, and a single global limit either under-seeds a strict tracker or over-seeds everything else. `indexers` has no seed columns, and `active_downloads.indexer` stores only a name, not an id.

## Model
- `indexers` gains these nullable columns:
  - `seed_ratio REAL`
  - `seed_time_minutes INTEGER`
  - `discography_seed_time_minutes INTEGER`: applies to multi-album or discography grabs, which trackers often treat separately.
  - `minimum_seeders INTEGER`: decision-engine rejection below this; default 1.
  - `NULL` means "inherit the global limit". `0` means "no requirement".
- `active_downloads` gains `indexer_id TEXT`, set at grab time. The existing `indexer` name column stays for display, and old rows without an id fall back to the global limits.
- Effective limits are resolved once, at grab time, and snapshotted onto the download row (`seed_ratio_target`, `seed_time_target_minutes`). Editing an indexer later doesn't retroactively change a torrent that is already seeding.

## Enforcement — belt and braces
1. **At the client.** When a torrent is added, push the limits to the client, so seeding is honoured even while TrackSeerr is down. For qBittorrent that's `torrents/setShareLimits` (`ratioLimit`, `seedingTimeLimit`), or the equivalent add parameters. A client without share-limit support logs once, and only (2) applies.
2. **In TrackSeerr governance.** The shared post-import function from the previous branch uses the download's snapshotted targets instead of the global settings. A target is met when **either** the ratio or the seed time is reached, whichever comes first. That matches Lidarr and the usual "1.0 ratio or 72 h" tracker wording. A target left unset (`0`) never counts as met on its own, so a ratio-only rule waits for the ratio. Removal always uses `delete_files=False` while a source-preserving mode (hardlink/copy) is in use.

## Guard rails
- **Move mode vs seed rules.** If `import_mode = move` and any enabled torrent indexer has a seed rule, the Settings → Media Management page shows a persistent warning: "Move breaks seeding for indexers with seed rules — use Hardlink or Copy." It also appears as a system health item. Downloads are never silently moved in a way that defeats a rule the user set.
- **"Delete completed transfers" never overrides an unmet indexer rule.**
- **Usenet/Soulseek indexers** hide the seed fields; they don't apply.

## UI
The indexer edit form gets a "Seeding" group: ratio, seed time, discography seed time and minimum seeders, each with an "inherit global" placeholder showing the current global value. The Queue shows "Seeding · 0.62 / 1.0 · 31 h / 72 h" on completed torrents still held for seeding.

## Done means
- Migration with a `SCHEMA_VERSION` bump.
- Grab-time resolution and snapshot tested.
- qBittorrent share limits pushed, tested with a mocked API.
- Governance uses the snapshot, with both-must-be-met for indexer rules and the existing behaviour for globals.
- The move-mode warning shows.
- The full containerised suite is green.

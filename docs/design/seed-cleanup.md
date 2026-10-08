# Seed cleanup — design contract

Owner decision, 2026-10-06. Branch `feat/seed-cleanup`, which follows `feat/library-health`.

## Problem
The *arrs remove a finished torrent only while they are still tracking it. After a restart, a manual import outside the app, a category change or a single failed removal call, the torrent stays in the client for good, holding disk space. TrackSeerr today removes the torrent when its seed goal is met (`delete_completed_transfers`), but it always keeps the files (`delete_files=False`), and it has no sweep for transfers it lost track of.

## Setting: "When seeding is done"
`media_management_settings.seed_complete_action` replaces the `delete_completed_transfers` boolean. The migration maps `false → keep` and `true → remove`.

| Value | Effect once the seed goal is met |
|---|---|
| `keep` | Leave the torrent seeding. TrackSeerr never touches it. |
| `remove` | Remove the torrent from the client and keep its files. This is today's behaviour. |
| `remove_and_delete` | Remove the torrent **and its files**, reclaiming the space a copy-and-tag import used while seeding. |

The seed goal is the download's snapshotted indexer rule (`docs/design/indexer-seed-rules.md`), falling back to the global ratio and time limits. An indexer rule that hasn't been met always blocks removal.

### File-deletion safety gate (`remove_and_delete` only)
`delete_files=True` is sent only when **all** of these hold. Otherwise the action falls back to `remove` and logs why:
- The download's effective import mode was `hardlink` or `copy`, never `move`.
- Every library file recorded for the download exists and is not the torrent's own path.
- The download has no held unmatched files (`unmatched_files` empty), so nothing is still waiting for manual import.
- The torrent's content path lies inside the client's download directory, never inside the music root.

## Seed cleanup task (sweep)
This is a scheduled job registered with the job tracker, so it shows in System → Tasks with "Run now". It runs daily by default, plus once at startup after a delay, and only one runs at a time.

1. **Tracked downloads.** Re-evaluate every download in `completed` (held for seeding) and apply the setting. Retry removals that failed earlier. Keep a per-download `cleanup_attempts` counter and `cleanup_error`.
2. **Client reconciliation.** For each torrent client, list the torrents in TrackSeerr's category (a qBittorrent `torrents/info?category=` call; add a driver `list_category()` that defaults to unsupported). Match each torrent by hash to `active_downloads` or import history.
   - **Matched but forgotten** (no live row, or a row already in a terminal status while the torrent is still in the client): evaluate the seed goal using the recorded indexer and the global rules, and apply the setting with the safety gate.
   - **Unmatched** (in our category, never grabbed or imported by TrackSeerr): **never auto-removed.** It becomes a Needs review finding, `kind='orphan_torrent'`, with name, size, ratio and seed time, plus actions "Remove torrent" and "Remove torrent + files". These actions require explicit confirmation in the UI.
3. **Failures surface.** After 3 failed cleanup attempts, a download becomes a Needs review finding (`kind='cleanup_failed'`, with the error) instead of retrying forever silently.

## UI
- **Media Management:** the "When seeding is done" select replaces the checkbox. It has help text, and a warning appears when `remove_and_delete` is combined with `move` mode, explaining that files are never deleted in that case.
- **Queue:** the seeding line gains "removes in ~3 d" when a goal and an action other than `keep` are configured.
- **Needs review:** an "Orphaned torrents" section and a "Cleanup failed" section.

## Non-goals
- Torrents outside TrackSeerr's category are never listed or touched.
- No usenet or Soulseek cleanup. Nothing seeds from those, and their files are always moved.

## Done means
- Migration with a `SCHEMA_VERSION` bump.
- A truth-table test of the safety gate.
- Sweep tests covering tracked, forgotten-matched and orphan cases, and the retry-then-finding path, with qBittorrent mocked.
- The UI pieces are in place.
- The full containerised suite is green.

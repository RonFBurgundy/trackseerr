# Library ↔ media-server reconciliation — design contract

Owner decision, 2026-10-05. Next branch after `feat/acoustid-fingerprinting` (proposed name `feat/library-health`). Merges to `main` when green.

## Problem
Users with large libraries can't see that their media server silently skipped files. "Plex shows 11,000 tracks, the disk has 12,000 files" is invisible in either UI, and the server's total is not comparable anyway: it counts tracks, merges or splits albums, and collapses multiple versions. Lidarr never looks at what the server actually indexed, so this is a capability it doesn't have.

## Approach: diff by file path, never by count
Totals are shown for context only. The finding unit is a **file**: every audio file under the music root that the selected media server does not index, and every server entry whose file no longer exists.

### Adapter capability
`MediaServer` (`plex_playlist_sync/media_servers/base.py`) gains one optional method and a capability flag:

- `iter_library_files() -> Iterator[ServerFileRef]` — `ServerFileRef(server_id, path, title, artist, album, container)`. Paged, generator-based, so a 50k-track library never sits in memory twice.
- `ServerCapabilities.file_paths: bool` — false when the server can't expose paths. The feature then shows a one-line notice instead of a blank page (no dead pages).

| Server | Source of the path | Caveat |
|---|---|---|
| Plex | `track.media[].parts[].file` across every music section | Section must be readable by the configured token |
| Jellyfin | `/Items?IncludeItemTypes=Audio&Recursive=true&Fields=Path`, paged | Needs an admin API key; a user key returns no `Path` → `file_paths=false` |
| Subsonic / Navidrome | `search3` with an empty query, paged on `songOffset`; the `path` field | Path is relative to the music folder; servers that omit it → `file_paths=false` |

### Path mapping
The server and TrackSeerr usually mount the library at different paths (`/data/music` vs `/music`). A per-server prefix mapping is stored with the media-server settings and **auto-suggested** by reusing `itunes_import.suggest_mappings` on a sample of server paths against the on-disk tree. The user confirms or edits it once. Matching is NFC + casefold, identical to the iTunes importer.

### Disk side
Reuse the library scanner's file inventory (`library_scanner.py`), not a fresh walk, so a check costs one server listing plus one DB read.

## Cause classification
Each unindexed file is checked in this order. The first rule that fires wins. "Not scanned yet" goes first because a freshly imported album folder is unindexed only because the server has not scanned it yet, and would otherwise be misread as a folder outside the library. Files are **grouped** by cause and folder, so 300 files in one excluded folder show as one finding, not 300.

| Cause | Detection | Suggested action |
|---|---|---|
| Not scanned yet | File (or its folder's newest file) modified after the server's last library scan; with no known scan time, within the last 24 h | "Refresh server library" (existing `refresh_library`) |
| Folder outside the server's library roots / wrong mapping | Whole directory subtree unindexed while siblings are indexed, or zero overlap at all | Add the folder to the server library, or fix the path mapping |
| Unsupported format | Extension/codec not in the per-server support table (e.g. `.ape`, `.wv`, `.dsf` on Plex) | Convert, or accept it as unplayable |
| Corrupt / unreadable audio | The import-hardening probes fail (`import_security.check_magic`, mutagen parse) | Re-download; offer a Wanted search for the matched track |
| Missing core tags | No artist, album or title tag | Retag (link to manual import / rename) |
| Server can't read the file | No world/group read bit for the server's uid | Fix permissions |
| Ignored by rule | Matched by a `.plexignore` (Plex) in an ancestor directory | Remove the ignore rule |
| Unknown | None of the above | Show tags + path; the user investigates |

Reverse direction: **on the server, missing on disk** → stale server entries. Suggest emptying the server's trash or rescanning.

Artist-level drift (the server shows N artists, TrackSeerr shows M) is reported as context only, with the top differing names. It's usually `albumartist` vs `artist` tagging, which the file-level findings already explain.

## Where it surfaces: Activity → Needs review
A new **Needs review** tab beside Queue, History and Blocklist, with a count badge in the nav. Queue keeps downloads stuck at import; Wanted keeps missing music. Needs review is everything TrackSeerr thinks the user should look at that has no other home:

- library-health findings from this check, grouped by cause;
- weak import matches: files auto-import placed on a weak tag match. These were deferred from `feat/acoustid-fingerprinting`, and get a **Re-match** action that opens the existing Manual Import modal on that file.

Each finding can be dismissed ("ignore this file/folder") and the dismissal persists, so a known-unplayable `.dsf` collection doesn't nag forever.

## Running it
- A background job via the existing job tracker: on demand ("Check now" on the tab), plus a weekly schedule (setting, default on when a server with `file_paths` is configured).
- One check at a time; paged server requests with the adapters' existing retry/backoff.
- Admin-only, core tier. Server paths can reveal the host layout, so they never reach non-admin responses.

## Non-goals
- No automatic fixes: no moving files, retagging or editing server config. Findings suggest; the user acts. Existing tools (manual import, rename, search) are linked where they apply.
- No per-track play-state or metadata comparison. That's scrobbling territory.

## Data
- `library_health_findings(id, server_kind, cause, group_key, path, detail_json, first_seen, last_seen, dismissed)`: upserted per run; rows not seen in the latest run are deleted.
- `library_health_runs(id, started_at, finished_at, server_kind, disk_files, server_files, unindexed, stale, error)`.
- `media_server_settings` gains `path_mapping_json`.

## Done means
- The adapter method is implemented for Plex, Jellyfin and Subsonic, with mocked-response tests covering paging, the missing-path fallback and mapping.
- Every cause rule is covered by a fixture test, including grouping and the first-rule-wins order.
- The Needs review tab shows grouped findings, dismiss, Check now and last-run stats; weak-match findings open the Manual Import modal.
- The full containerised suite is green.

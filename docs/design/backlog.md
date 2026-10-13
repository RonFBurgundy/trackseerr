# Backlog

Persistent working list (orchestrator-maintained). Items here are NOT part of `various-artists.md`; that plan covers
per-track credits, VA naming, Appears On/Remixes, single-compilation requests, misfiled detection and MB/AcoustID
contributions. Priority: P1 = user-facing bug or data risk, P2 = noticeable friction, P3 = polish.

## Next up

0. **DONE 2026-10-13 (573f60e) · P1 · AcoustID verification in the rename preview** (after the Lidarr track-identity fix lands). When an AcoustID
   key is set: a "Verify with AcoustID" action on risky rows (track_number, file_title, album_title, album_year,
   disc_layout, collision). Fingerprint the file (existing manual-import AcoustID code), compare the identified
   recording MBID with the target track's `mb_recording_id`: match → `verified` reason, pre-selected; mismatch →
   "Audio is actually X" with the correct track linked, apply blocked for that row; no result → unchanged. Cache
   fingerprint + result per file (by path + size + mtime) so reruns are instant and the fingerprints feed the later
   AcoustID submission work (various-artists.md phase 6). Background job with progress; respect AcoustID's ~3 req/s.

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
   Also **sort/group the Tasks page better**: running tasks pinned at the top; then grouped by purpose (Library:
   scan, Lidarr import, credits backfill, rename/retag · Acquisition: wanted sweep, RSS, download queue, retries ·
   Metadata & art: artist refresh, art backfill · Maintenance: health, seed cleanup, recycle bin, backups, update
   check · Playlists & listening: playlist sync, scrobbles, mixes); within a group, scheduled tasks by next run, manual
   tasks last. Add the group to `TaskSpec` in `task_manager.py` so the order is defined server-side. Goal (user): easy to navigate, find a task fast, manage it quickly and cleanly, not cluttered — e.g. collapsible
   groups, a search/filter box, one compact row per task (name, status, last/next run) with actions (Run · Cancel ·
   Pause · interval) in the row, details/history behind a click instead of always shown. Mobile-first and fully within the frontend skill (tape-deck controls, amber accent, sharp fillets, full-width
   mobile layout) — shared primitives only (`TactileSwitch`, `SelectionCheckbox`), no browser-default controls.
3. **DONE 2026-10-13 · P1 · Requesting a track that's already in the library.** Discover/search results don't always show the
   "In library" badge, so the request goes through; the track page does detect it. Two fixes: (a) server-side guard
   in `create_request` (`trackseerr/api/routes/requests.py` ~L89) — reject/short-circuit when the library has a
   matching track (recording MBID, ISRC, or artist+clean title), returning "already in library"; (b) find why the
   search results miss the badge on a 30k-track library (likely a capped/paginated in-library lookup) and fix it.
4. **DONE 2026-10-13 · P1 · Library sorted by tracks takes minutes; track search slow (30k tracks, 317k catalog tracks).** Profile the
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
12. **P2 · Guard against browser-default controls regressing.** Two layers: (a) a global safety net in
    `frontend/src/index.css` — `input[type=checkbox], input[type=radio], input[type=range] { accent-color:
    var(--accent-amber) }` so a stray native control can never render blue; (b) a CI check (small node script next to
    `frontend/scripts/gen-api.mjs`, run in the CI workflow beside `check:api`) failing on `blue-|indigo-|sky-` Tailwind
    classes or a raw checkbox/radio outside `components/ui`. Rule already added to the frontend skill (Phase 4d #5,
    Phase 6 gate 7).
13. **DONE 2026-10-13 · P3 · Housekeeping:** an 8-day-old `tsc -w` process from another session is running on the host.
14. **P2 · Scanner can still merge same-title tracks on one disc.** `_pick_title_match`
    (`trackseerr/library_catalog_store.py`) returns the first same-disc row with that title when no row has the
    requested track number, so a fresh scan of two new "[silence]" files (#90, #91) lands both on one track. The Lidarr
    import is guarded by an exact disc+number+title check. Fix: with a track number given, cross numbers only when the
    candidate row has no file yet (keeps renumbered catalog rows matching) — test with the scanner two-file case.
15. **DONE 2026-10-13 · P2 · Retag modal doesn't scroll file paths horizontally** (desktop and mobile), so long file names are
    truncated or unreachable. Apply the same fix the rename modal got: each row's path block in its own
    `overflow-x-auto` container with a `w-max min-w-full whitespace-nowrap` inner wrapper, `tabIndex={0}` +
    `role="group"` + `aria-label` for keyboard scrolling, full path in `title` (see
    `frontend/src/components/library/RenameModal.tsx` ~L450-470). Target: `frontend/src/components/library/RetagModal.tsx`
    file rows and the per-field diff values. Frontend skill applies.
16. **P2 · Activity, Wanted and album item cards are cluttered (mobile first, desktop too).** On phones the per-item
    action buttons fill the right half of every card and squeeze the track details into a narrow left column (user
    screenshot `Screenshot_20261012-214949.png`, Queue: a status word + Retry/Blocklist/Delete icon buttons take
    ~55% of the width, so the artist truncates to "OK …"/"Her…"/"Ha…", the progress % clips to "1(", and a stalled
    row's WARNING/STALLED badge + Manual Import button wrap the actions onto a second row).
    **Decided with user 2026-10-13:** one shared item-card component, same layout on every viewport (desktop only
    gets slightly tighter spacing, so switching devices never feels different):
    row 1 = full-width details (title · artist · album, truncating) + monitor state as a compact icon toggle (not a
    labelled switch) + one ⋯ `OverflowMenu` (`frontend/src/components/ui/OverflowMenu.tsx`) at the right edge holding
    ALL actions; row 2 = the existing status badges, quality/source chips and progress bar; a chevron expands
    secondary details (indexer, size, paths, errors, history) inline. Attention state (stalled, failed, needs review):
    the badge stays in row 2 and is tappable to run the recommended action; that action (e.g. Manual Import) is the
    first menu item, amber-highlighted; the ⋯ trigger gets a small amber dot. No per-row banners.
    **Constraint (user):** nothing new visually — keep the current colours, badges, chips, progress bars and
    tape-deck controls; this is a re-layout plus slight compaction only.
    Scope: every Activity sub-page (Queue, Searches, History, Blocklist, Needs Review), every Wanted sub-page
    (incl. Calendar agenda rows), and the Library album cards — `ArtistAlbumCard.tsx` currently splits by design
    (`sm:hidden` chevron + ⋯ on phones, `hidden sm:flex` inline TactileSwitch + Search/Import/Info on desktop,
    ~L184-240); unify it onto the shared card. Page-level toolboxes (`LibraryView.tsx`, `ArtistDetail.tsx`) are
    already ⋯ on all viewports. Reuse the card for the Tasks page compact rows in #2.
    Files: `frontend/src/components/activity/Activity*Panel.tsx`, `NeedsReviewPanel.tsx`/`ReviewMappingCard.tsx`,
    `SoulseekQueueDetail.tsx`, `wanted/WantedPanel.tsx`, `calendar/CalendarAgenda.tsx`,
    `library/ArtistAlbumCard.tsx`. Frontend skill applies. Decompose: (a) shared card + Queue, (b) remaining
    Activity panels, (c) Wanted + Calendar, (d) album cards.

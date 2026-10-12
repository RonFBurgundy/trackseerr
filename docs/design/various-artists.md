# Various Artists, remixes and per-track artists

Status: planned 2026-10-12. Lidarr can't do any of this; it is a headline differentiator.

## Problem

Library tracks have no artist of their own: `library_tracks.artist_id` is the album artist, so every track on a
compilation is credited to "Various Artists" (e.g. Saturday Night Fever: "Stayin' Alive" → Various Artists).
Consequences: compilation files can't carry the performer in their name, artist pages can't show compilation
appearances or remixes, and requesting one compilation means monitoring the whole Various Artists catalog.

## Rules (user-confirmed)

- A track's **primary artist** is the original performer. For a remix that is the original artist, not the
  remixer: "Closer (Adriatique remix)" → primary Adriatique only if Adriatique made the original.
- The **remixer** is kept in the title (as MusicBrainz writes it) and stored as a separate credit.
- A remix shows on **both** artist pages: the original artist (as their track) and the remixer (as a remix).
- Featured artists are credits too ("feat." stays in the title as MusicBrainz has it).

## Phases (one branch each, backend before frontend)

1. **Track credits data** — new `library_track_artists(track_id, artist_name, artist_mbid, role
   primary|featured|remixer, position)`; populate from the MB release tracklist artist-credit (already fetched
   for RG tracks; extend the cached payload if the credit is missing) plus recording `remixer` relations;
   fall back to file tags (ARTIST / MusicBrainz Artist Id) on scan. Backfill task for the existing library.
   **Files MusicBrainz doesn't know (SoundCloud bootlegs, flips, edits):** parse remix roles from the title —
   "(X Remix|Bootleg|Flip|Edit|Rework|Mix)" → X is remixer; "VIP" → the original artist's own version. Credit the
   original artist only when it is present in the tags ("Original - Song (X Bootleg)"); otherwise keep the file as
   tagged and show it on the remixer's page. Never guess, and never move an album folder because of a re-credit.
   Library artists are NOT auto-created for every credit — credits link to a library artist when one with the
   MBID exists.
2. **Naming + settings** — everything below is configurable in Media Management and carried by the presets;
   the **Trackseerr preset = the user's rules** (original artist credited, remixer in title + credit, track
   artist in compilation file names).
   - `remix_credit`: `original` (Trackseerr) | `remixer` | `as_credited` (whatever MusicBrainz credits).
   - `featured_artists`: `in_title` (Trackseerr, MB style "feat.") | `in_artist` | `drop`.
   - `compilation_artist_folder`: folder name for VA albums, default "Various Artists" (alt "VA", "Compilations").
   - `compilation_track_format`: already exists; Trackseerr preset fills it.
   - Presets: Trackseerr (above), Lidarr Standard (as_credited, no track artist in file names), Plex
     (album artist "Various Artists", track artist in the tag and file name), Beets/Picard-style
     ("Compilations/<Album>", `as_credited`).
   Tokens: `{Track ArtistName}` / `{Track ArtistCleanName}` resolve to the primary credit; Trackseerr
   preset gets a compilation format `{Album Title}{ (Release Year)}/{track:00} - {Track ArtistCleanName} -
   {Track Title}` (exact shape to confirm against the user's VA folders).
3. **Artist pages** — "Appears On" (compilations, other artists' albums) and "Remixes" sections, owned/missing
   counts, play/link to the album.
4. **Request one compilation** — request/monitor a single VA release group; the Various Artists artist exists
   unmonitored with only requested albums monitored; discography refresh never auto-monitors VA albums.
5. **Misfiled detection** — library-health checks: album under Various Artists whose tracks share one primary
   artist (→ move to that artist), and a single-artist album whose MB release group is credited to Various
   Artists (→ move to VA). One-click fix through the existing rename/move path.

6. **Contribute back** — for files MusicBrainz doesn't know (after tagging, renaming and AcoustID):
   - AcoustID: submit fingerprint + duration (+ recording MBID once matched) through the AcoustID submit API with
     the user's own AcoustID user key (opt-in setting; Picard does the same).
   - MusicBrainz: no bot-created releases (MB policy). Build a **seeded "Add release" form** (POST to
     musicbrainz.org/release/add, the documented seeding format Picard/Harmony use) from the album's tags,
     tracklist, durations and links (e.g. the SoundCloud URL); the user reviews and submits it in their browser
     under their own MB account. ISRCs can be submitted via the MB web service with the user's MB login.
   - Afterwards re-run matching so the new MBIDs flow back into the library.
   - Plex/media-server safety: tags written keep albumartist / artist / MusicBrainz IDs consistent with the folder
     layout, so servers group albums the same way before and after.

Order: 1 → 2 → 3, then 4, 5 and 6 independently.

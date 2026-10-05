# Arr-Style Profiles — Native Core Spec

Status: approved for build (2026-10-05). Defaults: see `docs/QUALITY_RESEARCH.md`.

Research decisions applied here:
- TRaSH publishes no Lidarr custom formats; import targets the **Lidarr custom-format JSON schema** (Servarr wiki / Tubifarry examples) — `implementation` = C# class name, regex case-insensitive.
- Lidarr semantics kept: **quality order beats score**; format score breaks ties within a quality; `min_format_score` rejects; `cutoff_format_score` keeps a release wanted while below it. Default min score **0** (Servarr's example of 1 makes untagged releases ungrabbable).
- Release profiles: required terms OR within a profile, AND across profiles; any ignored term rejects; `/regex/` vs substring.
- Default metadata profile "Standard" = Album + Studio (+ official only where status is known).
- Default kbps (min/preferred/max): FLAC 24 0/2000/9500 · FLAC 16 0/895/1400 · MP3 320 290/320/350 · V0 160/245/350 · AAC 256 200/256/280 · MP3 192 150/192/210 · V2 130/190/280. Unknown duration → track-count estimate enforcing max only; never hard-reject on unknown duration (deliberate deviation from Lidarr).
- Default custom formats + scores: Preferred Groups +100 (DeVOiD, PERFECT, ENRiCH, BigFLAC, GalaxyLossless, MusiCHI, LoRD), CD +10, Lossless +10, Hi-Res 24bit +15, WEB +5, Vinyl −50, Mono −10, Censored/Clean −15, Remastered 0, Deluxe 0. Default release profile "Reject bad sources" ignores: transcode, upconvert, fake flac, lossy master, MQA (marked "our suggestion" in UI hint). Never require log/cue.

Goal: absolute, Lidarr-grade control over what the native acquisition engine grabs, without Lidarr's rigidity. Everything is editable, every default is resettable, and TRaSH/Lidarr custom-format JSON imports directly.

## Settings layout (Settings → Media Management)

| Leaf | Contents |
|---|---|
| Quality | Quality Definitions table: one row per quality, min / preferred / max **kbps** (size-per-length), title, reset-to-default. |
| Profiles | One page, four stacked sections: Quality Profiles, Metadata Profiles, Delay Profiles, Release Profiles. |
| Custom Formats | Custom format list, editor, JSON import/export (TRaSH-compatible). |

The existing native "Release Profiles" (release-type filter, `native_release_profiles`) is renamed **Metadata Profiles** everywhere user-facing and in code (`native_metadata_profiles`, `library_artists.metadata_profile_id`, `add_metadata_profile_id`). Lidarr-mode code that already says `metadata_profile` refers to Lidarr's own id; native code uses the `native_` prefix to avoid collision. The route leaf `release-profiles` is reused for the new term-based feature only after the rename lands, with a hash redirect from the old leaf to `profiles`.

## 1. Quality Definitions

- Table `quality_definitions(quality TEXT PK, title TEXT, min_kbps REAL NULL, preferred_kbps REAL NULL, max_kbps REAL NULL, updated_at)`; seeded from research defaults; `NULL` = unbounded.
- Qualities: existing `AudioQuality` values (FLAC 24bit, FLAC 16bit, MP3 320, MP3 V0, AAC 256, MP3 192, MP3 V2, Unknown). Adding qualities (ALAC, Opus, OGG, WAV) is phase 2 and needs parser patterns.
- Evaluation: `kbps = size_bytes * 8 / 1000 / duration_seconds`. Duration = sum of known track durations for the target album (library_tracks), else `total_tracks × 240s` estimate flagged as estimated (estimated duration only enforces max, never min, to avoid false rejections), else skip with reason "duration unknown".
- Rejection reasons are human-readable and surfaced in the manual-search / history UI.
- Preferred kbps: releases closer to preferred within the same quality rank higher (tie-breaker after custom-format score).

## 2. Quality Profiles (full editor)

Existing table `quality_profiles` (v10) already has `items_json`, `cutoff`, `custom_formats`, `min_score`, `upgrade_allowed`. Extend:
- `items_json`: ordered list supporting groups: `{type:"quality", quality, allowed}` or `{type:"group", name, allowed, items:[quality…]}`; order = preference (top = best). Replaces weights (migrate weights → order).
- `cutoff`: a quality or group name.
- `upgrade_allowed`, `min_format_score`, `cutoff_format_score` (upgrade-until), `min_upgrade_format_score`.
- `format_items_json`: `[{format_id, score}]` — scores per custom format.
- Drop `preferred_tags`/`ignored_tags`/`min_size_mb`/`max_size_mb` from the UI (migrated into a Release Profile + Quality Definitions respectively); keep columns for backward compat, read-only.
- UI: list with copy/delete/set-default; editor modal: name, upgrades toggle, drag-reorder qualities, create/ungroup groups, allowed checkboxes, cutoff select, format score table with min/upgrade-until scores.

## 3. Custom Formats

- Table `custom_formats(id, name UNIQUE, include_in_rename INTEGER, specifications_json, created_at, updated_at)`.
- Specification implementations (Lidarr-compatible names so TRaSH JSON imports): `ReleaseTitleSpecification` (regex), `ReleaseGroupSpecification` (regex), `SizeSpecification` (min/max MB), `IndexerFlagSpecification`, `QualitySpecification` (our quality). Plus native extras: `PhraseSpecification` (fuzzy phrase, token-set ratio ≥ threshold), `SourceSpecification` (CD/WEB/Vinyl/Unknown), `ProtocolSpecification` (torrent/usenet/soulseek).
- Each spec: `negate`, `required`. Format matches when all required specs pass and at least one non-required spec of each implementation type passes (Lidarr semantics — confirm in research doc).
- Import: paste or upload TRaSH/Lidarr JSON; unknown implementations are kept but flagged and ignored by the engine. Export per format.
- Regexes compiled with a timeout guard (the `regex` module with timeout, or length limits + precompile validation) — user-supplied regex must not hang the decision engine.

## 4. Release Profiles (term-based)

- Table `release_profiles(id, name, enabled, required_json, ignored_json, indexer_ids_json, tags_json)`.
- `required`: release must contain at least one term; `ignored`: release must contain none. Terms are plain substrings (case-insensitive) or `/regex/`.
- Optional indexer restriction. Applied before scoring; rejection reason names the profile and term.

## 5. Delay Profiles

- Table `delay_profiles(id, order_idx, preferred_protocol, usenet_delay_min, torrent_delay_min, soulseek_delay_min, bypass_if_highest_quality, bypass_if_above_score, tags_json)`; default profile seeded (no delay, prefer usenet? — take research default).
- Pending releases queue: a grab decision within the delay window is stored as pending; released when the window expires or a better release appears. Requires a small `pending_releases` table and worker tick.

## 6. Decision order

1. Parse → quality. 2. Release Profiles (required/ignored). 3. Quality allowed in profile. 4. Quality Definition size-per-length. 5. Custom formats → score; reject below `min_format_score`. 6. Rank: quality order, then format score, then preferred-kbps distance, then protocol preference, then seeders. 7. Delay profile gate. 8. Upgrade logic: upgrade while below cutoff quality or below `cutoff_format_score`, and only if new score > current + `min_upgrade_format_score`.

## 7. Phases

1. Backend: schema + migrations + engine + API + tests (largest).
2. Frontend: Quality page, Profiles page sections, Custom Formats page with import.
3. Metadata Profiles rename (code + UI) — can run alongside 2 if file scopes are split.
4. Decision visibility: manual search shows per-release score breakdown and rejection reasons.

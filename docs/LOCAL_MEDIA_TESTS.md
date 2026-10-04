# Local-media tests

`tests/local_media/` runs the library scanner, manual import, renamer, cover-art, M3U, MusicBrainz and fingerprint
code against a **real, local music library** (an old iTunes collection, Daft Punk / *Discovery* and its collaboration
folders). They are opt-in, read-only against the source, and never part of the normal suite.

## Rules

- The audio is copyrighted. It is **never** committed, and never copied into the repo. Fixtures copy the few files
  they need into pytest's `tmp_path`; the source is only ever read (mount it `:ro`).
- Without `RUN_LOCAL_MEDIA=1` or `TRACKSEERR_LOCAL_MEDIA`, pytest does not collect the directory at all
  (`collect_ignore_glob` in `tests/local_media/conftest.py`), so the normal suite command is unchanged.
- If enabled but `TRACKSEERR_LOCAL_MEDIA` is not an existing directory, every media test skips.
- `.gitignore` also blocks `tests/local_media/media/`, `local-media/` and audio files under `tests/local_media/`.

## Run

`TRACKSEERR_LOCAL_MEDIA` must point at the directory that contains the `Daft Punk/` artist folder
(for iTunes: `.../iTunes/iTunes Media/Music`).

```bash
docker run --rm -v "$PWD":/app -v /mnt/music:/media:ro -w /app \
  -e PYTHONPATH=/app \
  -e TRACKSEERR_LOCAL_MEDIA=/media/iTunes/iTunes\ Media/Music \
  -e RUN_LOCAL_MEDIA=1 \
  trackseerr:test pytest tests/local_media -q -rxXs
```

Useful variants:

| Goal | Add |
|---|---|
| Skip everything that touches the network | `-m "not network"` |
| Only network tests | `-m network` |
| See why each xfail fails today | `--runxfail --tb=short` |
| Use a different MusicBrainz endpoint | `-e TRACKSEERR_MB_URL=https://your-mirror` (default `https://musicbrainz.org`, <= 1 req/s) |
| Enable AcoustID lookup | `-e ACOUSTID_API_KEY=...` (also needs `fpcalc` + `pyacoustid`) |

## Markers

- `local_media` (all tests here) and `network` (MusicBrainz / AcoustID) are registered in `pytest.ini`.
- Network tests probe MusicBrainz first (with retries, it returns 503 when busy) and **skip** if it is unreachable.
- The scanner launches a background artist-hydration thread; the harness patches it out. Cover Art Archive is never contacted.

## What is covered

| File | Behaviour |
|---|---|
| `test_scan.py` | scan hierarchy, iTunes duplicates, title variants, monitoring options, rescan idempotency, quality (320 kbps MP3), `2001` vs `2001-01-01` |
| `test_import_rename.py` | `/manual-import/scan` + `/commit` (default naming, tags, hardlink, duplicates), `/rename/preview` + `/apply` |
| `test_cover_art.py` | embedded art detection/extraction, scanner `cover_url`, cover route, iTunes `AlbumArt_{GUID}_Large.jpg` |
| `test_m3u.py` | M3U with absolute / relative / Windows paths and EXTINF; route; resolution against the scanned library; the real `iTunes/Playlists/Kavinsky.m3u` (CRLF, `C:\\...` paths, 28 of 29 files exist) |
| `test_itunes_import.py` | the real `iTunes Library.xml` (mount `/mnt/music` at `/media`, read-only): 31,487 tracks, 68 playlists of which 57 import (7 built-in, 4 folders, 0 empty; 15 smart), suggested `D:/iTunes/iTunes Media/Music` mapping, Discovery tracks of a real playlist matching the scanned mini library |
| `test_artist_matching.py` | `clean_library_name`, collaboration folders (`_`, `feat`, `ft`, `&`, truncated names), tagless fallbacks |
| `test_mb_enrichment.py` | live MusicBrainz: artist MBID, Discovery release group, 14-track hydration, monitoring |
| `test_formats.py` | real M4A (AAC 128), untagged WAV, FLAC (opt-in: `TRACKSEERR_LOCAL_FLAC=<file>`; the iTunes library has none): tags, quality, scan, rename |
| `test_fingerprint.py` | fingerprint orchestration (offline), generation (needs `fpcalc`), AcoustID lookup (needs key) |

## xfail(strict=True) = known bug

A test that asserts the **correct** behaviour but fails today is marked `xfail(strict=True, reason="BUG: ...")`
with the responsible file:line in the reason. When the bug is fixed the test XPASSes, strict mode fails the run,
and you delete the marker. Run with `-rxX` to list them.

The first round found 20 bugs (scanner artist keying, untagged track merging, manual-import defaults, naming,
quality labels, `_` in names, folder art, title lookup, hydration monitoring under "existing", fingerprinting
dependencies, AAC bitrate, iTunes M3U orientation). All are fixed and their markers removed; each fix also has a
synthetic test in `tests/test_library_quality_fixes.py` so CI covers it without this library. New findings use the
same convention.

## Adding fixtures

Copy, never reference in place: `copy_discovery([...names], dst)` (from `conftest.py`) for a single album,
or `copy_media(src_dir, names, dst)` for anything else. Use `library_copy` for a private, mutable mini library and
`scanned_pristine` (session-scoped, read-only) when a test only inspects a scan. `set_id3(path, TPE1=...)` rewrites
tags on a **copy** to isolate one defect from another.

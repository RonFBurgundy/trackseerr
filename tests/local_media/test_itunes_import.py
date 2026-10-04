"""iTunes import against the REAL export (read-only mount) and a mini library scanned from copied Discovery files.

The export is only ever opened for reading; nothing from it is written or committed. Enable with
``TRACKSEERR_LOCAL_MEDIA`` / ``RUN_LOCAL_MEDIA=1`` and mount the iTunes folder at /media/iTunes
(see docs/LOCAL_MEDIA_TESTS.md).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from plex_playlist_sync import itunes_import as imp

from .conftest import MINI_SET, run_scan

pytestmark = pytest.mark.local_media

EXPORT = Path("/media/iTunes/iTunes Library.xml")

# Counts found in the real export (verified against the plist, not taken from the task description):
#   68 raw playlists = 57 importable (23 smart overall, 15 of them importable) + 7 built-in (Library master, Music,
#   Movies, TV Shows, Podcasts, iTunes U, Books) + 4 folders + 0 empty.
# The task text expected 45 importable; the export actually holds 57.
EXPECTED_TRACKS = 31487
EXPECTED_PLAYLISTS = 57
EXPECTED_SKIPPED = {"builtin": 7, "folders": 4, "empty": 0}
EXPECTED_SMART = 15
WIN_ROOT = "D:/iTunes/iTunes Media/Music"


@pytest.fixture(scope="module")
def export_bytes() -> bytes:
    if not EXPORT.is_file():
        pytest.skip(f"{EXPORT} not mounted")
    return EXPORT.read_bytes()


@pytest.fixture(scope="module")
def parsed(export_bytes: bytes) -> dict:
    return imp.parse_library(export_bytes)


def test_real_export_counts(parsed):
    assert len(parsed["tracks"]) == EXPECTED_TRACKS
    assert len(parsed["playlists"]) == EXPECTED_PLAYLISTS
    assert parsed["skipped"] == EXPECTED_SKIPPED
    assert sum(1 for p in parsed["playlists"] if p["is_smart"]) == EXPECTED_SMART
    assert all(p["items"] for p in parsed["playlists"])
    names = {p["name"] for p in parsed["playlists"]}
    assert not names & {"Library", "Music", "Movies", "TV Shows", "Podcasts", "Books"}


def test_real_locations_are_windows_urls(parsed):
    paths = [p for p in (imp.location_to_path(t["location"]) for t in parsed["tracks"].values()) if p]  # streamed/podcast URLs have none
    assert paths and sum(p.startswith("D:/iTunes/iTunes Media/Music/") for p in paths) > 0.9 * len(paths)


def _mini_discovery_ids(parsed: dict) -> set[str]:
    wanted = {f"Daft Punk/Discovery/{n}" for n in MINI_SET}
    ids = set()
    for tid, t in parsed["tracks"].items():
        path = imp.location_to_path(t["location"]) or ""
        if any(path.endswith(w) for w in wanted):
            ids.add(tid)
    return ids


def test_preview_and_commit_match_discovery_tracks(api, parsed, export_bytes, pristine_root):
    run_scan(api.db, pristine_root)
    prev = api.client.post("/api/import/itunes/preview", content=export_bytes, headers=api.headers)
    assert prev.status_code == 200, prev.text
    body = prev.json()
    assert body["track_count"] == EXPECTED_TRACKS and len(body["playlists"]) == EXPECTED_PLAYLISTS
    sug = body["suggested_mappings"]
    assert sug, "expected a suggested mapping from the mini library"
    assert sug[0]["from"] == WIN_ROOT and sug[0]["to"] == str(pristine_root) and sug[0]["sample_matches"] >= 5

    discovery_ids = _mini_discovery_ids(parsed)
    assert discovery_ids
    # a real playlist holding at least one of the mini-library Discovery tracks
    target = next(p for p in parsed["playlists"] if discovery_ids & set(p["items"]) and len(p["items"]) < 2000)
    expected_in_playlist = discovery_ids & set(p for p in target["items"])

    r = api.client.post(
        f"/api/import/itunes/{body['import_id']}/commit",
        json={"playlists": [target["key"]], "path_mappings": [{"from": sug[0]["from"], "to": sug[0]["to"]}]},
        headers=api.headers,
    )
    assert r.status_code == 202, r.text
    deadline = time.time() + 120
    while time.time() < deadline:
        st = api.get(f"/api/import/itunes/{body['import_id']}/status").json()
        if st["state"] in ("completed", "failed"):
            break
        time.sleep(0.2)
    assert st["state"] == "completed", st
    (result,) = st["playlists"]
    assert result["matched"] >= len(expected_in_playlist) > 0
    assert result["missing"] > 0  # the rest of a big real playlist is not in the 8-file mini library
    pl = api.db.get_playlist(result["created_playlist_id"])
    assert pl["service"] == "itunes" and pl["monitor_mode"] == "none"
    missing_titles = {m["title"] for m in api.db.get_missing_tracks(result["created_playlist_id"])}
    assert not {parsed["tracks"][t]["name"] for t in expected_in_playlist} & missing_titles

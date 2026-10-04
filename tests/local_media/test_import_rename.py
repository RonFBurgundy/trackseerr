"""Manual import (scan / commit with rename + tagging) and rename preview/apply on real Discovery files.

Everything runs on COPIES under tmp_path. The default naming formats are asserted as shipped:
    artist folder   {Artist Name}
    album folder    {Album Title} ({Release Year}){[ - Album Type]}
    track file      {track:00} - {Track Title}{[ (Quality Full)]}
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plex_playlist_sync.library import inspect_audio_file
from plex_playlist_sync.naming import sanitize_component

from .conftest import audio_files, configure_roots, run_scan, set_id3, snapshot

pytestmark = pytest.mark.local_media

QUALITY = "(MP3 320)"  # catalog quality name, the same source rename uses


@pytest.fixture
def env(api, tmp_path):
    """root = empty library root, staging = empty staging dir, both under tmp_path."""
    root, staging = tmp_path / "library", tmp_path / "staging"
    root.mkdir()
    staging.mkdir()
    configure_roots(api.db, root, staging)
    return api, root, staging


def _item(path: Path, **kw) -> dict:
    return {
        "source_path": str(path),
        "artist_name": "Daft Punk",
        "album_title": "Discovery",
        "year": 2001,
        "write_tags": False,
        **kw,
    }


# --------------------------------------------------------------------------------------------------
# 5a. Manual import: scan
# --------------------------------------------------------------------------------------------------

def test_manual_scan_lists_every_audio_file_with_tags_and_no_match_on_empty_library(env, copy_discovery):
    api, _, staging = env
    copy_discovery(["02 Aerodynamic.mp3", "06 Night Vision.mp3", "06 Nightvision.mp3"], staging)

    resp = api.post("/api/library/manual-import/scan", {"folder_path": str(staging)})
    assert resp.status_code == 200
    cands = {c["filename"]: c for c in resp.json()}
    assert sorted(cands) == ["02 Aerodynamic.mp3", "06 Night Vision.mp3", "06 Nightvision.mp3"]
    aero = cands["02 Aerodynamic.mp3"]
    assert aero["confidence"] == 0.0 and aero["matched_artist_id"] is None
    assert aero["tags"]["title"] == "Aerodynamic" and aero["tags"]["track_number"] == 2
    assert aero["tags"]["quality_full"] == "MP3 320kbps"


def test_manual_scan_ignores_non_audio_sidecar_files(env, copy_discovery, discovery_src):
    api, _, staging = env
    copy_discovery(["02 Aerodynamic.mp3"], staging)
    (staging / "09 Something About Us.mp3.asd").write_bytes(b"iTunes/WMP analysis sidecar")
    (staging / "AlbumArt_{E0B5F6EB-9E7A-4290-A301-FD4994D78C20}_Large.jpg").write_bytes(b"\xff\xd8\xff\xd9")

    resp = api.post("/api/library/manual-import/scan", {"folder_path": str(staging)})
    assert [c["filename"] for c in resp.json()] == ["02 Aerodynamic.mp3"]


def test_manual_scan_matches_existing_catalog_with_full_confidence(env, copy_discovery, tmp_path):
    api, root, staging = env
    seed = root / "Daft Punk" / "Discovery"
    copy_discovery(["02 Aerodynamic.mp3"], seed)
    run_scan(api.db, root)
    copy_discovery(["02 Aerodynamic.mp3"], staging)

    cand = api.post("/api/library/manual-import/scan", {"folder_path": str(staging)}).json()[0]
    assert cand["matched_artist_name"] == "Daft Punk"
    assert cand["matched_album_title"] == "Discovery"
    assert cand["matched_track_title"] == "Aerodynamic"
    assert cand["confidence"] == 1.0


def test_manual_scan_proposes_album_artist_for_combined_artist_tag(env, copy_discovery):
    api, _, staging = env
    copy_discovery(["01 One More Time.mp3"], staging)
    cand = api.post("/api/library/manual-import/scan", {"folder_path": str(staging)}).json()[0]
    assert cand["matched_artist_name"] == "Daft Punk"


def test_manual_scan_rejects_path_traversal_and_missing_dir(env):
    api, _, staging = env
    assert api.post("/api/library/manual-import/scan", {"folder_path": str(staging / ".." / "x")}).status_code == 400
    assert api.post("/api/library/manual-import/scan", {"folder_path": str(staging / "nope")}).status_code == 400


# --------------------------------------------------------------------------------------------------
# 5b. Manual import: commit, rename with default formats, tags
# --------------------------------------------------------------------------------------------------

def test_commit_uses_default_naming_formats(env, copy_discovery):
    api, root, staging = env
    (src,) = copy_discovery(["02 Aerodynamic.mp3"], staging)

    res = api.post("/api/library/manual-import/commit", {"items": [_item(src, track_number=2)]}).json()
    assert (res["imported_count"], res["failed_count"]) == (1, 0), res
    dest = Path(res["results"][0]["destination_path"])
    assert dest == root / "Daft Punk" / "Discovery (2001)" / f"02 - Aerodynamic {QUALITY}.mp3"
    assert dest.is_file() and not src.exists(), "mode=move must consume the staging file"


def test_commit_with_custom_artist_album_track_layout(env, copy_discovery):
    """The 'Artist/Artist - Album (Year)/NN - Title' layout expressed in this engine's token syntax."""
    api, root, staging = env
    api.db.update_media_management_settings({
        "artist_folder_format": "{Artist Name}",
        "standard_track_format": "{Artist Name} - {Album Title} ({Release Year})/{track:00} - {Track Title}",
    })
    (src,) = copy_discovery(["02 Aerodynamic.mp3"], staging)

    res = api.post("/api/library/manual-import/commit", {"items": [_item(src, track_number=2)]}).json()
    dest = Path(res["results"][0]["destination_path"])
    assert dest == root / "Daft Punk" / "Daft Punk - Discovery (2001)" / "02 - Aerodynamic.mp3"


def test_commit_registers_catalog_rows_and_file(env, copy_discovery):
    api, root, staging = env
    (src,) = copy_discovery(["02 Aerodynamic.mp3"], staging)
    res = api.post("/api/library/manual-import/commit", {"items": [_item(src, track_number=2)]}).json()["results"][0]

    snap = snapshot(api.db)
    assert [a["name"] for a in snap["artists"]] == ["Daft Punk"]
    assert [(a["title"], a["year"]) for a in snap["albums"]] == [("Discovery", 2001)]
    assert [(t["title"], t["track_number"]) for t in snap["tracks"]] == [("Aerodynamic", 2)]
    (f,) = snap["files"]
    assert f["file_path"] == res["destination_path"]
    assert f["relative_path"] == f"Daft Punk/Discovery (2001)/02 - Aerodynamic {QUALITY}.mp3"
    assert f["quality_name"] == "MP3 320" and f["bitrate"] == 320000
    api.plex.refresh_music_library.assert_called_once()


def test_commit_writes_tags_when_requested(env, copy_discovery):
    api, _, staging = env
    (src,) = copy_discovery(["01 One More Time.mp3"], staging)
    assert inspect_audio_file(src)["artist"] == "Daft Punk/Romanthony"

    res = api.post(
        "/api/library/manual-import/commit",
        {"items": [_item(src, track_number=1, write_tags=True)]},
    ).json()["results"][0]
    tags = inspect_audio_file(res["destination_path"])
    assert (tags["artist"], tags["album_artist"], tags["album"]) == ("Daft Punk", "Daft Punk", "Discovery")
    assert (tags["title"], tags["track_number"], tags["year"]) == ("One More Time", 1, 2001)
    assert tags["bitrate"] == 320000, "tag rewrite must not touch the audio stream"


def test_commit_leaves_tags_alone_when_write_tags_false(env, copy_discovery):
    api, _, staging = env
    (src,) = copy_discovery(["01 One More Time.mp3"], staging)
    res = api.post("/api/library/manual-import/commit", {"items": [_item(src, track_number=1)]}).json()["results"][0]
    assert inspect_audio_file(res["destination_path"])["artist"] == "Daft Punk/Romanthony"


def test_commit_honours_global_write_audio_tags_setting(env, copy_discovery):
    api, _, staging = env
    api.db.update_media_management_settings({"write_audio_tags": False})
    (src,) = copy_discovery(["01 One More Time.mp3"], staging)
    item = _item(src, track_number=1)
    del item["write_tags"]  # client relies on the server-side setting
    res = api.post("/api/library/manual-import/commit", {"items": [item]}).json()["results"][0]
    assert inspect_audio_file(res["destination_path"])["artist"] == "Daft Punk/Romanthony"


def test_commit_without_track_number_uses_the_files_tag(env, copy_discovery):
    api, root, staging = env
    (src,) = copy_discovery(["02 Aerodynamic.mp3"], staging)
    item = _item(src)  # no track_number
    res = api.post("/api/library/manual-import/commit", {"items": [item]}).json()["results"][0]
    assert Path(res["destination_path"]).name.startswith("02 - Aerodynamic")


def test_commit_hardlink_mode_keeps_the_source(env, copy_discovery):
    api, _, staging = env
    (src,) = copy_discovery(["02 Aerodynamic.mp3"], staging)
    res = api.post(
        "/api/library/manual-import/commit", {"items": [_item(src, track_number=2, mode="hardlink")]}
    ).json()["results"][0]
    assert src.is_file() and Path(res["destination_path"]).is_file()


def test_commit_missing_source_fails_item_without_aborting_batch(env, copy_discovery):
    api, _, staging = env
    (src,) = copy_discovery(["02 Aerodynamic.mp3"], staging)
    res = api.post(
        "/api/library/manual-import/commit",
        {"items": [_item(staging / "ghost.mp3", track_number=9), _item(src, track_number=2)]},
    ).json()
    assert (res["imported_count"], res["failed_count"]) == (1, 1)
    assert res["results"][0]["status"] == "failed"


def test_commit_itunes_duplicates_become_one_track_with_three_distinct_files(env, copy_discovery):
    api, root, staging = env
    srcs = copy_discovery(["01 One More Time.mp3", "01 One More Time 2.mp3", "01 One More Time 4.mp3"], staging)
    res = api.post(
        "/api/library/manual-import/commit", {"items": [_item(s, track_number=1) for s in srcs]}
    ).json()
    assert (res["imported_count"], res["failed_count"]) == (3, 0)

    dests = sorted(Path(r["destination_path"]).name for r in res["results"])
    assert dests == [
        f"01 - One More Time {QUALITY} (1).mp3",
        f"01 - One More Time {QUALITY} (2).mp3",
        f"01 - One More Time {QUALITY}.mp3",
    ]
    assert len(audio_files(root)) == 3, "collision handling must never overwrite"
    snap = snapshot(api.db)
    assert len(snap["tracks"]) == 1 and len(snap["files"]) == 3


def test_commit_night_vision_variants_land_on_one_track(env, copy_discovery):
    api, root, staging = env
    srcs = copy_discovery(["06 Night Vision.mp3", "06 Nightvision.mp3"], staging)
    items = [_item(srcs[0], track_number=6, track_title="Night Vision"), _item(srcs[1], track_number=6)]
    items[1]["track_title"] = "Nightvision"
    res = api.post("/api/library/manual-import/commit", {"items": items}).json()
    assert res["failed_count"] == 0
    snap = snapshot(api.db)
    assert len(snap["tracks"]) == 1 and len(snap["files"]) == 2


def test_combined_artist_tag_folder_name_is_not_glued_together(env, copy_discovery):
    api, root, staging = env
    (src,) = copy_discovery(["01 One More Time.mp3"], staging)
    item = _item(src, track_number=1)
    del item["artist_name"]
    res = api.post("/api/library/manual-import/commit", {"items": [item]}).json()["results"][0]
    artist_dir = Path(res["destination_path"]).relative_to(root).parts[0]
    assert "PunkRomanthony" not in artist_dir, artist_dir


def test_sanitize_component_does_not_glue_words_across_slash():
    assert "PunkRomanthony" not in sanitize_component("Daft Punk/Romanthony")


# --------------------------------------------------------------------------------------------------
# 6. Rename preview / apply on a scanned library
# --------------------------------------------------------------------------------------------------

@pytest.fixture
def scanned_env(api, library_copy):
    configure_roots(api.db, library_copy)
    run_scan(api.db, library_copy)
    return api, library_copy


def _preview(api) -> dict[str, dict]:
    res = api.post("/api/library/rename/preview", {})
    assert res.status_code == 200
    return {Path(d["current_path"]).name: d for d in res.json()}


def test_preview_proposes_default_layout_without_touching_disk(scanned_env):
    api, root = scanned_env
    before = audio_files(root)
    prev = _preview(api)

    aero = prev["02 Aerodynamic.mp3"]
    assert aero["needs_rename"] is True
    # Rename uses the catalog's quality_name ("MP3 320"), not the tag string ("MP3 320kbps") import uses.
    assert aero["proposed_path"] == str(root / "Daft Punk" / "Discovery (2001)" / "02 - Aerodynamic (MP3 320).mp3")
    assert audio_files(root) == before


def test_freshly_imported_file_needs_no_rename(env, copy_discovery):
    api, _, staging = env
    (src,) = copy_discovery(["02 Aerodynamic.mp3"], staging)
    api.post("/api/library/manual-import/commit", {"items": [_item(src, track_number=2)]})
    prev = api.post("/api/library/rename/preview", {}).json()
    assert [d["needs_rename"] for d in prev] == [False]


def test_preview_lists_one_file_per_track_so_extra_duplicates_are_not_offered(scanned_env):
    api, _ = scanned_env
    snap = snapshot(api.db)
    prev = api.post("/api/library/rename/preview", {}).json()
    assert len(prev) == len(snap["tracks"]) < len(snap["files"])


def test_preview_scoped_to_album(scanned_env):
    api, _ = scanned_env
    snap = snapshot(api.db)
    album = next(a for a in snap["albums"] if a["title"] == "Discovery")
    scoped = api.post("/api/library/rename/preview", {"album_id": album["id"]}).json()
    expected = [t for t in snap["tracks"] if t["album_id"] == album["id"]]
    assert len(scoped) == len(expected)


def test_apply_moves_files_updates_catalog_and_is_idempotent(scanned_env):
    api, root = scanned_env
    prev = api.post("/api/library/rename/preview", {}).json()
    ids = [d["file_id"] for d in prev]

    res = api.post("/api/library/rename/apply", {"file_ids": ids}).json()
    assert res["errors"] == [], res
    assert res["renamed_count"] == len(ids)

    # Files really moved, catalog paths follow, no duplicate content lost.
    for d in prev:
        assert not Path(d["current_path"]).exists()
        assert Path(d["proposed_path"]).is_file()
        assert api.db.get_library_file(d["file_id"])["file_path"] == d["proposed_path"]

    # A second preview finds nothing left to do; a second apply renames nothing.
    again = api.post("/api/library/rename/preview", {}).json()
    assert [d["needs_rename"] for d in again] == [False] * len(again)
    assert api.post("/api/library/rename/apply", {"file_ids": ids}).json()["renamed_count"] == 0


def test_apply_to_every_file_including_duplicates_never_overwrites(scanned_env):
    api, root = scanned_env
    snap = snapshot(api.db)
    n_files = len(audio_files(root))
    res = api.post("/api/library/rename/apply", {"file_ids": [f["id"] for f in snap["files"]]}).json()
    assert res["errors"] == [], res
    assert len(audio_files(root)) == n_files
    paths = [f["file_path"] for f in snapshot(api.db)["files"]]
    assert len(set(paths)) == len(paths) and all(Path(p).is_file() for p in paths)


def test_apply_removes_emptied_source_folder(scanned_env):
    api, root = scanned_env
    snap = snapshot(api.db)
    api.post("/api/library/rename/apply", {"file_ids": [f["id"] for f in snap["files"]]})
    assert not (root / "Daft Punk" / "Discovery").exists()


def test_rescan_after_rename_creates_no_new_catalog_rows(scanned_env):
    api, root = scanned_env
    before = snapshot(api.db)
    api.post("/api/library/rename/apply", {"file_ids": [f["id"] for f in before["files"]]})
    status = run_scan(api.db, root, prune_missing=True)
    after = snapshot(api.db)
    assert (status["artists_created"], status["albums_created"], status["tracks_created"]) == (0, 0, 0)
    assert len(after["files"]) == len(before["files"])


def test_apply_unknown_file_id_is_reported_not_raised(scanned_env):
    api, _ = scanned_env
    res = api.post("/api/library/rename/apply", {"file_ids": ["does-not-exist"]}).json()
    assert res["renamed_count"] == 0 and len(res["errors"]) == 1


def test_rename_requires_admin(api):
    resp = api.client.post("/api/library/rename/preview", json={})
    assert resp.status_code in (401, 403)

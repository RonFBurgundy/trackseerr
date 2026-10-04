"""Library scan, rescan, quality and tag-date behaviour against real iTunes-era Daft Punk / Discovery files.

xfail(strict=True) tests assert the CORRECT behaviour and document a real bug; when the bug is fixed the
test XPASSes, strict mode turns that into a failure, and the marker must be removed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from plex_playlist_sync.library import _extract_year, inspect_audio_file
from plex_playlist_sync.models import AudioQuality, QualityProfile, QualityProfileItem
from plex_playlist_sync.quality import evaluate_release, parse_release_title

from .conftest import run_scan, set_id3, snapshot

pytestmark = pytest.mark.local_media


def _tracks_titled(snap: dict, title: str) -> list[dict]:
    return [t for t in snap["tracks"] if t["title"].lower() == title.lower()]


# --------------------------------------------------------------------------------------------------
# 1. Scan: hierarchy, duplicates, title variants, monitoring
# --------------------------------------------------------------------------------------------------

def test_scan_completes_and_indexes_every_file(scanned_pristine):
    _, status = scanned_pristine
    assert status["status"] == "completed", status
    assert status["total_files_found"] == 8
    assert status["files_indexed"] == 8


def test_itunes_duplicates_become_one_track_with_three_files(scanned_pristine):
    db, _ = scanned_pristine
    snap = snapshot(db)
    omt = _tracks_titled(snap, "One More Time")
    assert len(omt) == 1, "'01 One More Time', ' 2' and ' 4' must collapse to one track"
    names = sorted(Path(f["file_path"]).name for f in snap["files_by_track"][omt[0]["id"]])
    assert names == ["01 One More Time 2.mp3", "01 One More Time 4.mp3", "01 One More Time.mp3"]


def test_night_vision_and_nightvision_share_one_track(scanned_pristine):
    """Works only because track lookup matches on (clean title OR track number); see test_untagged_track_number_*."""
    db, _ = scanned_pristine
    snap = snapshot(db)
    track6 = [t for t in snap["tracks"] if t["track_number"] == 6]
    assert len(track6) == 1
    assert len(snap["files_by_track"][track6[0]["id"]]) == 2


def test_artist_case_variant_merges(scanned_pristine):
    """'Daft punk' (Around The World's TPE1) must not create a second artist next to 'Daft Punk'."""
    db, _ = scanned_pristine
    artists = [a for a in snapshot(db)["artists"] if a["name"].lower() == "daft punk"]
    assert len(artists) == 1


def test_scanned_artists_albums_tracks_are_monitored_under_existing_option(scanned_pristine):
    db, _ = scanned_pristine
    assert db.get_media_management_settings()["scan_monitor_option"] == "existing"
    snap = snapshot(db)
    assert all(a["monitored"] and a["monitor_option"] == "existing" for a in snap["artists"])
    assert all(a["monitored"] for a in snap["albums"])
    assert all(t["monitored"] for t in snap["tracks"])


@pytest.mark.parametrize("option", ["none", "all"])
def test_scan_follows_scan_monitor_option(db, library_copy, option):
    db.update_media_management_settings({"scan_monitor_option": option})
    run_scan(db, library_copy)
    snap = snapshot(db)
    assert {a["monitor_option"] for a in snap["artists"]} == {option}
    # 'none' monitors nothing; 'all' monitors everything. Either way scanned albums follow the option.
    assert {a["monitored"] for a in snap["albums"]} == {option == "all"}


@pytest.mark.xfail(
    strict=True,
    reason="BUG: scanner prefers the TRACK artist over album artist, so 'Daft Punk/Romanthony' becomes its own "
    "artist (library_scanner.py:457, artist_name = metadata.artist or album_artist)",
)
def test_combined_artist_tag_does_not_create_bogus_artist(scanned_pristine):
    db, _ = scanned_pristine
    names = [a["name"] for a in snapshot(db)["artists"]]
    assert not any("/" in n for n in names), names


@pytest.mark.xfail(
    strict=True,
    reason="BUG: same root cause as above - album is keyed by (track artist, title), so Discovery is split into "
    "3 albums under 3 artists (library_scanner.py:457 and :542 album_key)",
)
def test_discovery_is_one_album_under_daft_punk(scanned_pristine):
    db, _ = scanned_pristine
    snap = snapshot(db)
    assert [a["name"] for a in snap["artists"]] == ["Daft Punk"]
    assert [a["title"] for a in snap["albums"]] == ["Discovery"]


@pytest.mark.xfail(
    strict=True,
    reason="BUG: inspect_audio_file defaults a MISSING track number to 1 (library.py:269) and "
    "get_library_track_by_title matches 'clean title OR track number' (storage.py:6198), so an untagged "
    "'Around The World' is silently merged into track 1 'One More Time' when they share an album",
)
def test_untagged_track_number_does_not_merge_into_track_one(db, tmp_path, copy_discovery):
    root = tmp_path / "music"
    album = root / "Daft Punk" / "Discovery"
    omt, atw = copy_discovery(["01 One More Time.mp3", "Around The World.mp3"], album)
    # Same artist tag on both so the artist/album split bug does not mask this one.
    set_id3(omt, TPE1="Daft Punk")
    set_id3(atw, TPE1="Daft Punk")
    assert inspect_audio_file(atw)["track_number"] == 1  # no TRCK tag -> defaulted

    run_scan(db, root)
    snap = snapshot(db)
    assert {t["title"] for t in snap["tracks"]} == {"One More Time", "Around The World"}


# --------------------------------------------------------------------------------------------------
# 2. Rescan idempotency
# --------------------------------------------------------------------------------------------------

def test_rescan_creates_nothing_new(db, library_copy):
    first = run_scan(db, library_copy)
    before = snapshot(db)
    second = run_scan(db, library_copy)
    after = snapshot(db)

    assert first["status"] == second["status"] == "completed"
    assert (second["artists_created"], second["albums_created"], second["tracks_created"]) == (0, 0, 0)
    for key in ("artists", "albums", "tracks", "files"):
        assert {r["id"] for r in before[key]} == {r["id"] for r in after[key]}, key


def test_rescan_preserves_user_monitored_flags(db, library_copy):
    run_scan(db, library_copy)
    snap = snapshot(db)
    artist, album, track = snap["artists"][0], snap["albums"][0], snap["tracks"][0]
    db.set_artist_monitored(artist["id"], False, cascade_children=False)
    db.set_album_monitored(album["id"], False, cascade_tracks=False)
    db.set_track_monitored(track["id"], False)

    run_scan(db, library_copy)
    assert db.get_library_artist(artist["id"])["monitored"] is False
    assert db.get_library_album(album["id"])["monitored"] is False
    assert db.get_library_track(track["id"])["monitored"] is False


def test_rescan_of_modified_file_reinspects_without_resetting_monitoring(db, library_copy):
    """A size change bypasses the fast cache path and re-runs the upsert branches."""
    run_scan(db, library_copy)
    snap = snapshot(db)
    aero = next(t for t in snap["tracks"] if t["title"] == "Aerodynamic")
    album = db.get_library_album(aero["album_id"])
    db.set_album_monitored(album["id"], False, cascade_tracks=False)

    f = next(Path(x["file_path"]) for x in snap["files_by_track"][aero["id"]])
    with f.open("ab") as fh:
        fh.write(b"\x00" * 128)

    status = run_scan(db, library_copy)
    assert status["status"] == "completed"
    assert status["tracks_created"] == 0 and status["albums_created"] == 0
    assert db.get_library_album(album["id"])["monitored"] is False
    assert len(snapshot(db)["files"]) == len(snap["files"])


def test_rescan_adds_new_duplicate_file_to_existing_track(db, library_copy):
    run_scan(db, library_copy)
    before = snapshot(db)
    folder = library_copy / "Daft Punk" / "Discovery"
    shutil.copyfile(folder / "02 Aerodynamic.mp3", folder / "02 Aerodynamic 2.mp3")

    status = run_scan(db, library_copy)
    after = snapshot(db)
    assert (status["artists_created"], status["albums_created"], status["tracks_created"]) == (0, 0, 0)
    assert len(after["files"]) == len(before["files"]) + 1
    assert len(after["tracks"]) == len(before["tracks"])


def test_prune_missing_removes_only_deleted_file_rows(db, library_copy):
    run_scan(db, library_copy)
    before = snapshot(db)
    (library_copy / "Daft Punk" / "Discovery" / "01 One More Time 4.mp3").unlink()

    status = run_scan(db, library_copy, prune_missing=True)
    after = snapshot(db)
    assert status["files_pruned"] == 1
    assert len(after["files"]) == len(before["files"]) - 1
    assert len(after["tracks"]) == len(before["tracks"])


# --------------------------------------------------------------------------------------------------
# 3. Quality detection
# --------------------------------------------------------------------------------------------------

def test_320kbps_mp3_is_detected_from_real_stream(scanned_pristine):
    db, _ = scanned_pristine
    for f in snapshot(db)["files"]:
        assert f["codec"] == "MP3"
        assert f["bitrate"] == 320000
        assert f["sample_rate"] == 44100
        assert f["quality_name"] == AudioQuality.MP3_320.value, f["file_path"]


def test_inspect_reports_quality_full_string(discovery_src):
    meta = inspect_audio_file(discovery_src / "02 Aerodynamic.mp3")
    assert meta["quality_full"] == "MP3 320kbps"
    parsed = parse_release_title(meta["quality_full"])
    assert parsed.quality == "MP3 320"
    assert parsed.bitrate_kbps == 320


def _profile(cutoff: str, allowed: set[str]) -> QualityProfile:
    order = ["FLAC 24bit", "FLAC 16bit", "MP3 320", "AAC 256", "MP3 V0"]
    return QualityProfile(
        id="p",
        name="p",
        cutoff=cutoff,
        items=[QualityProfileItem(quality=q, allowed=q in allowed, weight=1000 - i * 100) for i, q in enumerate(order)],
    )


def test_mp3_320_meets_cutoff_when_cutoff_is_mp3_320():
    parsed = parse_release_title("MP3 320kbps")
    res = evaluate_release(parsed, _profile("MP3 320", {"FLAC 16bit", "MP3 320"}))
    assert res.is_acceptable and res.meets_cutoff


def test_mp3_320_misses_lossless_cutoff():
    parsed = parse_release_title("MP3 320kbps")
    res = evaluate_release(parsed, _profile("FLAC 16bit", {"FLAC 16bit", "MP3 320"}))
    assert res.is_acceptable and not res.meets_cutoff


def test_default_profile_marks_scanned_mp3s_below_cutoff(scanned_pristine):
    """The shipped default profile is lossless-only, so an MP3 library is entirely 'cutoff not met' (upgradeable)."""
    db, _ = scanned_pristine
    assert db.get_default_quality_profile()["cutoff"] == "FLAC 16bit"
    assert {f["cutoff_met"] for f in snapshot(db)["files"]} == {False}


# --------------------------------------------------------------------------------------------------
# 4. Tag date edge cases
# --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["2001", "2001-01-01", "2001-01", "2001-01-01T00:00:00", " 2001 "])
def test_extract_year_handles_date_shapes(raw):
    assert _extract_year(raw) == 2001


@pytest.mark.parametrize("raw", [None, "", "unknown", "01/01"])
def test_extract_year_rejects_garbage(raw):
    assert _extract_year(raw) is None


def test_real_files_with_year_only_and_full_date_both_read_2001(discovery_src):
    """In the real library: Nightvision has TDRC '2001', Aerodynamic has '2001-01-01'."""
    assert inspect_audio_file(discovery_src / "06 Nightvision.mp3")["year"] == 2001
    assert inspect_audio_file(discovery_src / "02 Aerodynamic.mp3")["year"] == 2001


def test_mixed_date_formats_on_one_album_scan_to_one_album_year_2001(db, tmp_path, copy_discovery):
    root = tmp_path / "music"
    album = root / "Daft Punk" / "Discovery"
    a, b = copy_discovery(["02 Aerodynamic.mp3", "06 Nightvision.mp3"], album)
    set_id3(a, TDRC="2001-01-01")
    set_id3(b, TDRC="2001")
    run_scan(db, root)
    snap = snapshot(db)
    assert len(snap["albums"]) == 1
    assert snap["albums"][0]["year"] == 2001

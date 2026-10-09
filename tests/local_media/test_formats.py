"""Non-MP3 formats from the real library: M4A (AAC 128), WAV (untagged), and FLAC (when available).

M4A : Fall Out Boy / From Under the Cork Tree (iTunes AAC, 128 kbps)
WAV : Unknown Artist / Unknown Album / '01 Intro 2.wav' + '01 Intro 3.wav' (no tags at all)
FLAC: the iTunes library holds none (bounded search to depth 4 found none). Point TRACKSEERR_LOCAL_FLAC at any
      .flac file to enable the FLAC tests; otherwise they skip.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from trackseerr.library import inspect_audio_file, write_audio_tags
from trackseerr.quality import parse_release_title

from .conftest import audio_files, configure_roots, copy_media, run_scan, snapshot

pytestmark = pytest.mark.local_media

M4A_DIR = "Fall Out Boy/From Under the Cork Tree"
M4A = ["01 Our Lawyer Made Us Change the Nam.m4a", "03 Dance, Dance 2.m4a"]
WAV_DIR = "Unknown Artist/Unknown Album"
WAV = ["01 Intro 2.wav", "01 Intro 3.wav"]


def _src(media_root: Path, rel: str, names: list[str]) -> Path:
    d = media_root / rel
    if not all((d / n).is_file() for n in names):
        pytest.skip(f"{d} lacks {names}")
    return d


@pytest.fixture
def m4a_root(media_root, tmp_path):
    root = tmp_path / "music"
    copy_media(_src(media_root, M4A_DIR, M4A), M4A, root / M4A_DIR)
    return root


@pytest.fixture
def wav_root(media_root, tmp_path):
    root = tmp_path / "music"
    copy_media(_src(media_root, WAV_DIR, WAV), WAV, root / WAV_DIR)
    return root


# ------------------------------------------------------------------ M4A

def test_m4a_tags_and_stream_properties(m4a_root):
    meta = inspect_audio_file(m4a_root / M4A_DIR / "03 Dance, Dance 2.m4a")
    assert (meta["title"], meta["artist"], meta["album_artist"]) == ("Dance, Dance", "Fall Out Boy", "Fall Out Boy")
    assert (meta["album"], meta["year"], meta["track_number"]) == ("From Under the Cork Tree", 2005, 3)
    assert meta["codec"] == "AAC" and meta["bitrate"] == 128000 and meta["sample_rate"] == 44100
    assert meta["quality_full"] == "AAC 128kbps"


def test_m4a_scan_builds_hierarchy(db, m4a_root):
    status = run_scan(db, m4a_root)
    assert status["status"] == "completed" and status["files_indexed"] == 2
    snap = snapshot(db)
    assert [a["name"] for a in snap["artists"]] == ["Fall Out Boy"]
    assert [(a["title"], a["year"]) for a in snap["albums"]] == [("From Under the Cork Tree", 2005)]
    assert sorted(t["track_number"] for t in snap["tracks"]) == [1, 3]
    assert {f["codec"] for f in snap["files"]} == {"AAC"}


def test_128kbps_aac_is_not_reported_as_aac_256(db, m4a_root):
    run_scan(db, m4a_root)
    assert {f["quality_name"] for f in snapshot(db)["files"]} != {"AAC 256"}


def test_parse_release_title_reads_the_bitrate_for_aac():
    assert parse_release_title("AAC 128kbps").bitrate_kbps == 128


def test_m4a_tag_write_round_trip_on_copy(m4a_root):
    f = m4a_root / M4A_DIR / "03 Dance, Dance 2.m4a"
    assert write_audio_tags(f, {"title": "Dance, Dance (Test)", "artist": "Fall Out Boy", "album": "X", "track_number": 3, "year": 2005})
    after = inspect_audio_file(f)
    assert after["title"] == "Dance, Dance (Test)" and after["album"] == "X"
    assert after["codec"] == "AAC" and after["bitrate"] == 128000


def test_m4a_rename_preview_and_apply_keep_extension(api, m4a_root):
    configure_roots(api.db, m4a_root)
    run_scan(api.db, m4a_root)
    prev = api.post("/api/library/rename/preview", {}).json()
    assert {Path(d["proposed_path"]).suffix for d in prev} == {".m4a"}
    dance = next(d for d in prev if "Dance" in d["current_path"])
    assert Path(dance["proposed_path"]).parent == m4a_root / "Fall Out Boy" / "From Under the Cork Tree (2005)"
    assert Path(dance["proposed_path"]).name.startswith("03 - Dance, Dance")
    res = api.post("/api/library/rename/apply", {"file_ids": [d["file_id"] for d in prev]}).json()
    assert res["errors"] == [] and res["renamed_count"] == 2
    assert all(p.suffix == ".m4a" for p in audio_files(m4a_root)) and len(audio_files(m4a_root)) == 2


# ------------------------------------------------------------------ WAV (untagged)

def test_wav_has_no_tags_but_stream_is_read(wav_root):
    meta = inspect_audio_file(wav_root / WAV_DIR / "01 Intro 2.wav")
    assert meta["title"] is None and meta["artist"] is None and meta["album"] is None
    assert meta["codec"] == "WAV" and meta["bitrate"] == 1411200
    assert (meta["sample_rate"], meta["bits_per_sample"]) == (44100, 16)
    assert meta["quality_full"] == "WAV 16bit 44.1kHz"


def test_wav_scan_falls_back_to_folder_and_file_names(db, wav_root):
    run_scan(db, wav_root)
    snap = snapshot(db)
    assert [a["name"] for a in snap["artists"]] == ["Unknown Artist"]
    assert [a["title"] for a in snap["albums"]] == ["Unknown Album"]
    assert {t["title"] for t in snap["tracks"]} >= {"Intro 2"}   # leading track number parsed off the file stem
    assert {f["codec"] for f in snap["files"]} == {"WAV"}


def test_wav_counts_as_lossless_for_the_default_cutoff(db, wav_root):
    run_scan(db, wav_root)
    files = snapshot(db)["files"]
    assert {f["cutoff_met"] for f in files} == {True}
    # Characterisation: the quality parser has no WAV tier, so '16bit' lands on 'FLAC 16bit'.
    assert {f["quality_name"] for f in files} == {"FLAC 16bit"}


def test_untagged_wavs_with_different_names_stay_separate_tracks(db, wav_root):
    run_scan(db, wav_root)
    snap = snapshot(db)
    assert {t["title"] for t in snap["tracks"]} == {"Intro 2", "Intro 3"}


def test_wav_rename_preview_keeps_wav_extension(api, wav_root):
    configure_roots(api.db, wav_root)
    run_scan(api.db, wav_root)
    prev = api.post("/api/library/rename/preview", {}).json()
    assert prev and {Path(d["proposed_path"]).suffix for d in prev} == {".wav"}
    assert all("Unknown Artist" in d["proposed_path"] for d in prev)


# ------------------------------------------------------------------ FLAC (opt-in file)

@pytest.fixture
def flac(tmp_path) -> Path:
    raw = os.environ.get("TRACKSEERR_LOCAL_FLAC")
    if not raw or not Path(raw).is_file():
        pytest.skip("no FLAC in the iTunes library; set TRACKSEERR_LOCAL_FLAC=<file.flac> to enable")
    dst = tmp_path / "music" / "Artist" / "Album" / Path(raw).name
    dst.parent.mkdir(parents=True)
    shutil.copyfile(raw, dst)
    return dst


def test_flac_tags_quality_scan_and_rename(db, flac):
    meta = inspect_audio_file(flac)
    assert meta["codec"] == "FLAC" and meta["quality_full"].startswith("FLAC")
    root = flac.parents[2]
    run_scan(db, root)
    (f,) = snapshot(db)["files"]
    assert f["codec"] == "FLAC" and f["quality_name"] in ("FLAC 16bit", "FLAC 24bit") and f["cutoff_met"] is True

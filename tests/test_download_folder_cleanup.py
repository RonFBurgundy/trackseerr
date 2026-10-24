"""Tests for empty download-folder pruning and sweeping (real filesystem on tmp_path)."""

import os
from pathlib import Path

from trackseerr.download_folder_cleanup import prune_empty_parents, sweep_empty_dirs


def _make_album(root: Path) -> Path:
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    track = album / "a.flac"
    track.write_bytes(b"x")
    return track


def test_prune_removes_empty_album_and_artist_dirs(tmp_path):
    root = tmp_path / "dl"
    track = _make_album(root)
    track.unlink()
    assert prune_empty_parents([track], root) == 2
    assert not (root / "Artist").exists()
    assert root.is_dir()


def test_prune_stops_at_non_empty_dir(tmp_path):
    root = tmp_path / "dl"
    track = _make_album(root)
    (track.parent / "cover.jpg").write_bytes(b"x")
    track.unlink()
    assert prune_empty_parents([track], root) == 0
    assert track.parent.is_dir()
    assert (root / "Artist").is_dir()


def test_prune_never_removes_root_or_outside_paths(tmp_path):
    root = tmp_path / "dl"
    root.mkdir()
    outside = tmp_path / "other" / "Album"
    outside.mkdir(parents=True)
    assert prune_empty_parents([root / "a.flac", outside / "b.flac"], root) == 0
    assert root.is_dir()
    assert outside.is_dir()


def test_prune_tolerates_missing_dirs(tmp_path):
    root = tmp_path / "dl"
    root.mkdir()
    assert prune_empty_parents([root / "Gone" / "Album" / "a.flac"], root) == 0
    assert root.is_dir()


def test_sweep_removes_old_empty_nested_dirs(tmp_path):
    root = tmp_path / "inc"
    nested = root / "user" / "album"
    nested.mkdir(parents=True)
    old = 1_000_000
    for d in (nested, nested.parent):
        os.utime(d, (old, old))
    assert sweep_empty_dirs(root, 3600, now=old + 7200) == 2
    assert root.is_dir()
    assert not (root / "user").exists()


def test_sweep_keeps_recent_and_non_empty_dirs(tmp_path):
    root = tmp_path / "inc"
    fresh = root / "fresh"
    full = root / "full"
    fresh.mkdir(parents=True)
    full.mkdir()
    (full / "f.flac").write_bytes(b"x")
    os.utime(full, (1, 1))
    assert sweep_empty_dirs(root, 3600) == 0
    assert fresh.is_dir()
    assert (full / "f.flac").is_file()


def test_sweep_skips_symlinked_dirs(tmp_path):
    root = tmp_path / "inc"
    root.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = root / "link"
    link.symlink_to(target, target_is_directory=True)
    assert sweep_empty_dirs(root, 0, now=time_far_future()) == 0
    assert link.is_symlink()
    assert target.is_dir()


def time_far_future() -> float:
    return 4_000_000_000.0


def test_sweep_refuses_filesystem_root():
    assert sweep_empty_dirs(Path("/"), 0) == 0
    assert prune_empty_parents(["/tmp/x/y.flac"], Path("/")) == 0

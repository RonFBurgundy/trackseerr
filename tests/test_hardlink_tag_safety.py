"""Tagging a hardlink-imported library file must never touch the torrent's seeding inode."""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from plex_playlist_sync import acquisition_worker as aw
from plex_playlist_sync.acquisition_worker import ensure_private_copy

from tests.test_manual_import_hold import (  # noqa: F401  (fixtures + helpers)
    MATCHED, _commit, _flac, _held_download, _run_worker, _seed, client, config, db, headers,
)


def _fake_write_tags(path, *args, **kwargs):
    """Stands in for mutagen: mutates the file in place, like a real tag write."""
    with open(path, "ab") as fh:
        fh.write(b"TAGGED")


def test_ensure_private_copy_breaks_link(tmp_path):
    a = tmp_path / "a.flac"
    a.write_bytes(b"original")
    b = tmp_path / "b.flac"
    os.link(a, b)
    ino = a.stat().st_ino
    assert ensure_private_copy(b) is True
    assert b.stat().st_nlink == 1 and a.stat().st_nlink == 1
    assert a.stat().st_ino == ino and b.stat().st_ino != ino
    assert b.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["a.flac", "b.flac"]


def test_ensure_private_copy_noop_when_unshared(tmp_path):
    a = tmp_path / "a.flac"
    a.write_bytes(b"x")
    ino = a.stat().st_ino
    assert ensure_private_copy(a) is True
    assert a.stat().st_ino == ino


def test_ensure_private_copy_failure_returns_false_and_cleans_up(tmp_path):
    a = tmp_path / "a.flac"
    a.write_bytes(b"original")
    b = tmp_path / "b.flac"
    os.link(a, b)
    with patch("plex_playlist_sync.acquisition_worker.shutil.copy2", side_effect=OSError("disk full")):
        assert ensure_private_copy(b) is False
    assert a.read_bytes() == b"original" and b.stat().st_nlink == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.flac", "b.flac"]


def _worker_import(tmp_path, db, *, write_tags: bool):
    music, staging, *_ = _seed(db, tmp_path)
    db.update_media_management_settings({"import_mode": "hardlink", "write_audio_tags": write_tags,
                                         "embed_artwork": False})
    dl = staging / "Daft.Punk.Discovery.FLAC"
    _flac(dl / "good.flac")
    return music, dl / "good.flac"


def test_worker_hardlink_with_tags_leaves_torrent_file_untouched(tmp_path, db):
    music, src = _worker_import(tmp_path, db, write_tags=True)
    before, ino = src.read_bytes(), src.stat().st_ino
    with patch("plex_playlist_sync.acquisition_worker.write_audio_tags", side_effect=_fake_write_tags):
        _run_worker(db, src.parent, tmp_path / "staging", {"good.flac": MATCHED})
    placed = list(music.rglob("*.flac"))
    assert len(placed) == 1
    assert src.read_bytes() == before and src.stat().st_ino == ino
    assert placed[0].read_bytes().endswith(b"TAGGED")
    assert placed[0].stat().st_nlink == 1 and placed[0].stat().st_ino != ino


def test_worker_copy_failure_skips_tags(tmp_path, db):
    music, src = _worker_import(tmp_path, db, write_tags=True)
    before = src.read_bytes()
    calls = []
    with patch("plex_playlist_sync.acquisition_worker.write_audio_tags", side_effect=lambda *a, **k: calls.append(a)), \
         patch("plex_playlist_sync.acquisition_worker.ensure_private_copy", return_value=False):
        _run_worker(db, src.parent, tmp_path / "staging", {"good.flac": MATCHED})
    assert calls == []
    assert src.read_bytes() == before
    assert len(list(music.rglob("*.flac"))) == 1


def test_worker_without_write_tags_keeps_link(tmp_path, db):
    music, src = _worker_import(tmp_path, db, write_tags=False)
    _run_worker(db, src.parent, tmp_path / "staging", {"good.flac": MATCHED})
    placed = list(music.rglob("*.flac"))
    assert len(placed) == 1
    assert placed[0].stat().st_nlink == 2 and placed[0].stat().st_ino == src.stat().st_ino


def _commit_hardlink(tmp_path, db, client, headers, *, write_tags: bool):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    item = {"source_path": str(files[0]), "artist_id": "art-1", "album_id": "alb-1", "track_id": "trk-1",
            "track_title": "One More Time", "track_number": 1, "mode": "hardlink", "write_tags": write_tags}
    return music, files[0], item


def test_manual_commit_hardlink_with_tags_leaves_torrent_file_untouched(tmp_path, db, client, headers):
    music, src, item = _commit_hardlink(tmp_path, db, client, headers, write_tags=True)
    before, ino = src.read_bytes(), src.stat().st_ino
    with patch("plex_playlist_sync.api.routes.library.write_audio_tags", side_effect=_fake_write_tags):
        _commit(client, headers, [item])
    placed = list(music.rglob("*.flac"))
    assert len(placed) == 1
    assert src.read_bytes() == before and src.stat().st_ino == ino
    assert placed[0].read_bytes().endswith(b"TAGGED") and placed[0].stat().st_nlink == 1


def test_manual_commit_copy_failure_skips_tags(tmp_path, db, client, headers):
    music, src, item = _commit_hardlink(tmp_path, db, client, headers, write_tags=True)
    before = src.read_bytes()
    calls = []
    with patch("plex_playlist_sync.api.routes.library.write_audio_tags", side_effect=lambda *a, **k: calls.append(a)), \
         patch("plex_playlist_sync.api.routes.library.ensure_private_copy", return_value=False):
        _commit(client, headers, [item])
    assert calls == [] and src.read_bytes() == before


def test_manual_commit_without_tags_keeps_link(tmp_path, db, client, headers):
    music, src, item = _commit_hardlink(tmp_path, db, client, headers, write_tags=False)
    _commit(client, headers, [item])
    placed = list(music.rglob("*.flac"))
    assert placed[0].stat().st_nlink == 2

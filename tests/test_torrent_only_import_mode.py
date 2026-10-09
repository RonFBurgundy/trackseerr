"""Import mode applies to torrent downloads only; usenet/soulseek are always moved and tagged."""
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from trackseerr import acquisition_worker as aw
from trackseerr.import_files import effective_import_mode, prepare_file_for_tagging
from trackseerr.clients.acquisition import is_torrent_driver_type
from trackseerr.storage import SCHEMA_VERSION

from tests.test_manual_import_hold import (  # noqa: F401  (fixtures + helpers)
    MATCHED, _as_torrent, _commit, _flac, _held_download, _run_worker, _seed, client, config, db, headers,
)
from tests.test_hardlink_tag_safety import _fake_write_tags
from tests.test_settings import (  # noqa: F401
    _auth_headers, app_and_client, seeded_users, test_config, test_db,
)


@pytest.mark.parametrize("client_type,expected", [
    ("qbittorrent", "hardlink"), ("QBittorrent", "hardlink"),
    ("sabnzbd", "move"), ("slskd", "move"), ("lidarr", "move"), ("mystery", "move"), (None, "move"), ("", "move"),
])
def test_effective_import_mode_table(client_type, expected):
    assert effective_import_mode(client_type, {"import_mode": "hardlink"}) == expected


def test_effective_import_mode_follows_setting_for_torrents():
    assert effective_import_mode("qbittorrent", {"import_mode": "copy"}) == "copy"
    assert effective_import_mode("qbittorrent", {}) == "move"
    assert effective_import_mode("qbittorrent", {"import_mode": "bogus"}) == "move"


def test_only_qbittorrent_is_torrent():
    assert is_torrent_driver_type("qbittorrent")
    assert not any(is_torrent_driver_type(t) for t in ("sabnzbd", "slskd", "lidarr", None))


def _worker_setup(tmp_path, db, **settings):
    music, staging, *_ = _seed(db, tmp_path)
    db.update_media_management_settings({"import_mode": "hardlink", "embed_artwork": False, **settings})
    dl = staging / "Daft.Punk.Discovery.FLAC"
    _flac(dl / "good.flac")
    return music, staging, dl / "good.flac"


def test_worker_usenet_download_is_moved_and_tagged_despite_hardlink_setting(tmp_path, db):
    music, staging, src = _worker_setup(tmp_path, db, write_audio_tags=True)
    db.conn.execute("UPDATE download_clients SET driver_type = 'sabnzbd' WHERE id = 'c1'")
    db.conn.commit()
    with patch("trackseerr.acquisition_worker.write_audio_tags", side_effect=_fake_write_tags) as w:
        _run_worker(db, src.parent, staging, {"good.flac": MATCHED})
    placed = list(music.rglob("*.flac"))
    assert len(placed) == 1 and not src.exists()
    assert placed[0].stat().st_nlink == 1
    assert w.called and placed[0].read_bytes().endswith(b"TAGGED")


def test_worker_slskd_download_is_moved_despite_copy_setting(tmp_path, db):
    music, staging, src = _worker_setup(tmp_path, db, write_audio_tags=False, import_mode="copy")
    _run_worker(db, src.parent, staging, {"good.flac": MATCHED})
    assert len(list(music.rglob("*.flac"))) == 1 and not src.exists()


def test_worker_torrent_keep_hardlink_skips_tags(tmp_path, db):
    music, staging, src = _worker_setup(tmp_path, db, write_audio_tags=True, torrent_hardlink_tags="keep_hardlink")
    _as_torrent(db)
    before = src.read_bytes()
    with patch("trackseerr.acquisition_worker.write_audio_tags") as w:
        _run_worker(db, src.parent, staging, {"good.flac": MATCHED})
    placed = list(music.rglob("*.flac"))
    assert len(placed) == 1
    assert placed[0].stat().st_nlink == 2 and src.stat().st_nlink == 2
    assert placed[0].stat().st_ino == src.stat().st_ino and src.read_bytes() == before
    w.assert_not_called()


def test_worker_torrent_copy_and_tag_leaves_torrent_untouched(tmp_path, db):
    music, staging, src = _worker_setup(tmp_path, db, write_audio_tags=True, torrent_hardlink_tags="copy_and_tag")
    _as_torrent(db)
    before, ino = src.read_bytes(), src.stat().st_ino
    with patch("trackseerr.acquisition_worker.write_audio_tags", side_effect=_fake_write_tags):
        _run_worker(db, src.parent, staging, {"good.flac": MATCHED})
    placed = list(music.rglob("*.flac"))
    assert src.read_bytes() == before and src.stat().st_ino == ino and src.stat().st_nlink == 1
    assert placed[0].read_bytes().endswith(b"TAGGED") and placed[0].stat().st_ino != ino


def test_prepare_file_for_tagging_modes(tmp_path, caplog):
    a, b, solo = tmp_path / "a.flac", tmp_path / "b.flac", tmp_path / "solo.flac"
    a.write_bytes(b"x")
    solo.write_bytes(b"y")
    os.link(a, b)
    keep = {"torrent_hardlink_tags": "keep_hardlink"}
    with caplog.at_level("INFO", logger="trackseerr.import_files"):
        assert prepare_file_for_tagging(b, keep) is False
    assert "Kept hardlink; skipped tag writing for" in caplog.text
    assert b.stat().st_nlink == 2
    assert prepare_file_for_tagging(solo, keep) is True
    assert prepare_file_for_tagging(b, {}) is True and b.stat().st_nlink == 1


def test_manual_commit_held_slskd_download_is_moved_even_if_item_asks_hardlink(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    db.update_media_management_settings({"import_mode": "hardlink"})
    item = {"source_path": str(files[0]), "artist_id": "art-1", "album_id": "alb-1", "track_id": "trk-1",
            "track_title": "One More Time", "track_number": 1, "mode": "hardlink", "write_tags": False}
    out = _commit(client, headers, [item])
    assert out["results"][0]["mode"] == "move" and not files[0].exists()


def test_manual_commit_held_torrent_follows_settings(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    _as_torrent(db)
    db.update_media_management_settings({"import_mode": "hardlink"})
    item = {"source_path": str(files[0]), "artist_id": "art-1", "album_id": "alb-1", "track_id": "trk-1",
            "track_title": "One More Time", "track_number": 1, "write_tags": False}
    out = _commit(client, headers, [item])
    assert out["results"][0]["mode"] == "hardlink" and files[0].exists()


def test_manual_commit_torrent_keep_hardlink_skips_tags(tmp_path, db, client, headers):
    music, files = _held_download(db, tmp_path, ["a.flac"])
    _as_torrent(db)
    db.update_media_management_settings({"import_mode": "hardlink", "torrent_hardlink_tags": "keep_hardlink"})
    item = {"source_path": str(files[0]), "artist_id": "art-1", "album_id": "alb-1", "track_id": "trk-1",
            "track_title": "One More Time", "track_number": 1, "write_tags": True}
    with patch("trackseerr.api.routes.library.manual_import.write_audio_tags") as w:
        out = _commit(client, headers, [item])
    assert out["imported_count"] == 1
    w.assert_not_called()
    assert files[0].stat().st_nlink == 2


def test_migration_v58_and_round_trip(test_db):
    assert SCHEMA_VERSION >= 58
    assert test_db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
    cols = {r[1] for r in test_db.conn.execute("PRAGMA table_info(media_management_settings)").fetchall()}
    assert "torrent_hardlink_tags" in cols
    assert test_db.get_media_management_settings()["torrent_hardlink_tags"] == "copy_and_tag"
    test_db._migration_v58(test_db.conn.cursor())  # idempotent
    out = test_db.update_media_management_settings({"torrent_hardlink_tags": "keep_hardlink"})
    assert out["torrent_hardlink_tags"] == "keep_hardlink"
    with pytest.raises(ValueError):
        test_db.update_media_management_settings({"torrent_hardlink_tags": "nope"})


def test_settings_api_round_trip_and_422(app_and_client, test_db, test_config, seeded_users):
    _, api = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    assert api.get("/api/settings/media-management", headers=h).json()["settings"]["torrent_hardlink_tags"] == "copy_and_tag"
    r = api.post("/api/settings/media-management", json={"torrent_hardlink_tags": "keep_hardlink"}, headers=h)
    assert r.status_code == 200 and r.json()["torrent_hardlink_tags"] == "keep_hardlink"
    assert test_db.get_media_management_settings()["torrent_hardlink_tags"] == "keep_hardlink"
    bad = api.post("/api/settings/media-management", json={"torrent_hardlink_tags": "bogus"}, headers=h)
    assert bad.status_code == 422

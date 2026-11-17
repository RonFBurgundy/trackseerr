"""Album merges blocked by duplicate files: recording them and the admin's choice of which copy to keep."""

from pathlib import Path
from typing import Any

import pytest

from tests.test_library_api import (  # noqa: F401  (fixtures are used by name)
    _auth_headers,
    app_and_client,
    seeded_users,
    test_config,
    test_db,
)
from trackseerr import artist_repair, duplicate_choices, release_matching
from trackseerr.recycle_bin import DisposeResult
from trackseerr.storage import SCHEMA_VERSION, Database

SONGS = ("Wouldn't It Be Nice", "Sloop John B")


@pytest.fixture
def env(test_db: Database, tmp_path: Path):
    root = tmp_path / "music"
    root.mkdir()
    test_db.update_media_management_settings({
        "root_folder_path": str(root),
        "recycle_bin_path": str(tmp_path / "bin"),
    })
    test_db.upsert_library_artist({"id": "art", "name": "The Beach Boys", "monitored": True})
    return root, tmp_path / "bin"


def _album(db: Database, album_id: str, title: str, rg: str | None = None) -> None:
    db.upsert_library_album({
        "id": album_id, "artist_id": "art", "title": title, "monitored": True,
        "mb_release_group_id": rg, "mb_release_id": rg and "rel-" + rg,
    })


def _track(db: Database, root: Path, album_id: str, n: int, codec: str | None, size: int = 100) -> Path | None:
    tid = f"{album_id}-t{n}"
    db.upsert_library_track({
        "id": tid, "album_id": album_id, "artist_id": "art", "title": SONGS[n - 1],
        "track_number": n, "disc_number": 1, "monitored": True,
    })
    if codec is None:
        return None
    path = root / album_id / f"{n}.{codec.lower()}"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"x" * size)
    db.upsert_library_file({
        "id": f"{tid}-f", "track_id": tid, "file_path": str(path), "relative_path": f"{album_id}/{path.name}",
        "codec": codec, "bitrate": 320 if codec == "MP3" else None, "size_bytes": size,
    })
    return path


def _pair(db: Database, root: Path, src_codec: str = "FLAC", hold_codec: str = "FLAC"):
    """src album (no release group) and hold album (has one), each with one file on track 1; returns both paths."""
    _album(db, "src", "Pet Sounds (UDSACD)")
    _album(db, "hold", "Pet Sounds", rg="rg1")
    sp = _track(db, root, "src", 1, src_codec)
    hp = _track(db, root, "hold", 1, hold_codec)
    return sp, hp


def _conflict_id(db: Database) -> int:
    release_matching._merge_into_holder(db, "src", "hold", {"merged": 0, "merge_conflict": 0})
    rows = db.list_merge_conflicts()
    assert len(rows) == 1
    return rows[0]["id"]


def _binned(bin_dir: Path) -> list[str]:
    return sorted(p.name for p in bin_dir.rglob("*") if p.is_file()) if bin_dir.exists() else []


def test_fileless_source_track_merges_onto_holder_with_file(test_db, env):
    root, _ = env
    _album(test_db, "src", "Pet Sounds (UDSACD)")
    _album(test_db, "hold", "Pet Sounds", rg="rg1")
    _track(test_db, root, "src", 1, None)
    _track(test_db, root, "hold", 1, "FLAC")
    assert artist_repair.merge_single_album_into(test_db, "src", "hold") == "merged"
    assert test_db.get_library_album("src") is None
    assert len(test_db.list_library_files_for_track("hold-t1")) == 1


def test_conflict_recorded_when_both_have_files(test_db, env):
    root, _ = env
    _pair(test_db, root)
    counts = {"merged": 0, "merge_conflict": 0}
    release_matching._merge_into_holder(test_db, "src", "hold", counts)
    release_matching._merge_into_holder(test_db, "src", "hold", counts)  # a re-run refreshes, never duplicates
    assert counts == {"merged": 0, "merge_conflict": 2}
    rows = test_db.list_merge_conflicts()
    assert len(rows) == 1
    assert (rows[0]["kind"], rows[0]["source_title"], rows[0]["holder_artist"]) == (
        "duplicate_files", "Pet Sounds (UDSACD)", "The Beach Boys",
    )
    pairs = artist_repair.merge_conflict_pairs(test_db, "src", "hold")
    assert [(p["source_file"]["id"], p["holder_file"]["id"]) for p in pairs] == [("src-t1-f", "hold-t1-f")]


def test_successful_merge_clears_conflict_rows(test_db, env):
    root, _ = env
    _pair(test_db, root)
    _conflict_id(test_db)
    test_db.delete_library_file("src-t1-f")  # the source no longer collides
    counts = {"merged": 0, "merge_conflict": 0}
    release_matching._merge_into_holder(test_db, "src", "hold", counts)
    assert counts["merged"] == 1
    assert test_db.list_merge_conflicts(include_dismissed=True) == []


def test_ambiguous_holders_recorded(test_db, env):
    root, _ = env
    _album(test_db, "src", "The Chirping Crickets")
    _album(test_db, "hold", "The Chirping Crickets", rg="rg1")
    _album(test_db, "hold2", "The Chirping Crickets", rg="rg2")
    counts = {"merged": 0, "merge_conflict": 0, "ambiguous": 0}
    release_matching._fold_title_duplicate(test_db, test_db.get_library_album("src"), counts)
    assert counts["ambiguous"] == 1
    rows = test_db.list_merge_conflicts()
    assert sorted((r["holder_album_id"], r["kind"]) for r in rows) == [("hold", "ambiguous"), ("hold2", "ambiguous")]


def test_resolve_keep_existing_recycles_incoming_and_merges(test_db, env):
    root, bin_dir = env
    sp, hp = _pair(test_db, root)
    result = duplicate_choices.resolve_choice(test_db, _conflict_id(test_db), "keep_existing")
    assert result == {"merged": True, "recycled_file_ids": ["src-t1-f"], "kept_file_ids": ["hold-t1-f"]}
    assert hp.exists() and not sp.exists()
    assert _binned(bin_dir) == [sp.name]
    assert test_db.get_library_album("src") is None
    assert test_db.list_merge_conflicts(include_dismissed=True) == []


def test_resolve_keep_incoming_recycles_existing(test_db, env):
    root, bin_dir = env
    sp, hp = _pair(test_db, root)
    result = duplicate_choices.resolve_choice(test_db, _conflict_id(test_db), "keep_incoming")
    assert result["merged"] is True
    assert result["recycled_file_ids"] == ["hold-t1-f"] and result["kept_file_ids"] == ["src-t1-f"]
    assert sp.exists() and not hp.exists()
    assert [f["id"] for f in test_db.list_library_files_for_track("hold-t1")] == ["src-t1-f"]


def test_resolve_prefer_quality_keeps_flac_over_mp3(test_db, env):
    root, _ = env
    sp, hp = _pair(test_db, root, src_codec="MP3", hold_codec="FLAC")
    result = duplicate_choices.resolve_choice(test_db, _conflict_id(test_db), "prefer_quality")
    assert result["kept_file_ids"] == ["hold-t1-f"] and result["recycled_file_ids"] == ["src-t1-f"]
    assert hp.exists() and not sp.exists()


def test_resolve_prefer_quality_tie_keeps_existing(test_db, env):
    root, _ = env
    sp, hp = _pair(test_db, root)
    result = duplicate_choices.resolve_choice(test_db, _conflict_id(test_db), "prefer_quality")
    assert result["merged"] is True
    assert result["kept_file_ids"] == ["hold-t1-f"] and hp.exists() and not sp.exists()


def test_resolve_keep_both_dismisses_and_hides(test_db, env):
    root, _ = env
    sp, hp = _pair(test_db, root)
    cid = _conflict_id(test_db)
    result = duplicate_choices.resolve_choice(test_db, cid, "keep_both")
    assert result == {"merged": False, "recycled_file_ids": [], "kept_file_ids": []}
    assert sp.exists() and hp.exists()
    assert test_db.get_library_album("src") is not None
    assert duplicate_choices.list_choices(test_db) == []
    # a later scan sees the same pair again and must not resurface it
    release_matching._merge_into_holder(test_db, "src", "hold", {"merged": 0, "merge_conflict": 0})
    assert duplicate_choices.list_choices(test_db) == []


def test_resolve_disposal_failure_leaves_row(test_db, env, monkeypatch):
    root, _ = env
    sp, hp = _pair(test_db, root)
    cid = _conflict_id(test_db)
    import trackseerr.file_relink as fr

    monkeypatch.setattr(fr, "dispose_for_settings", lambda *a, **k: DisposeResult("kept", sp, reason="locked"))
    with pytest.raises(duplicate_choices.DisposalError):
        duplicate_choices.resolve_choice(test_db, cid, "keep_existing")
    assert test_db.get_merge_conflict(cid) is not None
    assert sp.exists() and hp.exists()


def test_list_includes_pairs_and_skips_vanished_albums(test_db, env):
    root, _ = env
    _pair(test_db, root)
    _conflict_id(test_db)
    items = duplicate_choices.list_choices(test_db)
    assert len(items) == 1 and items[0]["pairs"][0]["holder_file"]["codec"] == "FLAC"
    test_db.delete_library_album("src")
    assert duplicate_choices.list_choices(test_db) == []


def test_duplicates_routes_admin_only(app_and_client, test_db, test_config, seeded_users, env):
    _, client = app_and_client
    root, _ = env
    _pair(test_db, root)
    cid = _conflict_id(test_db)
    alice = _auth_headers(seeded_users["alice"], test_db, test_config)
    admin = _auth_headers(seeded_users["admin"], test_db, test_config)
    assert client.get("/api/library/duplicates", headers=alice).status_code == 403
    assert client.post(f"/api/library/duplicates/{cid}/resolve", json={"choice": "keep_both"}, headers=alice).status_code == 403
    body = client.get("/api/library/duplicates", headers=admin).json()
    assert body["total"] == 1 and body["items"][0]["pairs"][0]["source_file"]["id"] == "src-t1-f"
    resp = client.post(f"/api/library/duplicates/{cid}/resolve", json={"choice": "keep_existing"}, headers=admin)
    assert resp.status_code == 200 and resp.json()["merged"] is True
    bad = client.post(f"/api/library/duplicates/{cid}/resolve", json={"choice": "nope"}, headers=admin)
    assert bad.status_code == 422


def test_resolve_unknown_id_404(app_and_client, test_db, test_config, seeded_users, env):
    _, client = app_and_client
    admin = _auth_headers(seeded_users["admin"], test_db, test_config)
    resp = client.post("/api/library/duplicates/999/resolve", json={"choice": "keep_both"}, headers=admin)
    assert resp.status_code == 404


def test_migration_v99_creates_table(tmp_path: Path):
    db_file = tmp_path / "v98.db"
    db = Database(str(db_file))
    with db._lock:
        db.conn.execute("DROP TABLE library_merge_conflicts")
        db.conn.execute("DELETE FROM schema_migrations")
        db.conn.execute("INSERT INTO schema_migrations (version) VALUES (98)")
        db.conn.commit()
    db.close()
    db2 = Database(str(db_file))
    try:
        assert db2.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
        cols = {r[1] for r in db2.conn.execute("PRAGMA table_info(library_merge_conflicts)").fetchall()}
        assert {"source_album_id", "holder_album_id", "kind", "dismissed", "detected_at"} <= cols
    finally:
        db2.close()

"""Tests for the one-off artist repair: duplicate merge, wrong-ID clearing and joined-credit refiling."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, Optional

import pytest

from trackseerr import artist_repair
from trackseerr.artist_repair import REPAIR_KV_KEY, run_artist_repair
from trackseerr.backup import create_backup
from trackseerr.clients.mbid_enricher import artist_name_key
from trackseerr.models import LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack
from trackseerr.storage import Database

JAYZ = "f82bcf78-5b69-4622-a5ef-73800768d9ac"
JAXX = "11111111-aaaa-aaaa-aaaa-111111111111"
MENDEL = "22222222-bbbb-bbbb-bbbb-222222222222"
FELIX_REAL = "33333333-cccc-cccc-cccc-333333333333"


class FakeEnricher:
    """Canned MusicBrainz answers; no HTTP."""

    def __init__(
        self,
        names: dict[str, str],
        lookups: Optional[dict[str, Optional[str]]] = None,
        unavailable: Optional[set[str]] = None,
    ) -> None:
        self.names = names
        self.lookups = lookups or {}
        self.unavailable = unavailable or set()
        self.lookup_calls: list[str] = []

    def get_artist_details(self, mbid: str, force: bool = False) -> Optional[dict[str, Any]]:
        name = self.names.get(mbid)
        return {"name": name} if name else None

    def resolve_artist_mbid(self, artist_name: str) -> tuple[bool, Optional[str]]:
        self.lookup_calls.append(artist_name)
        if artist_name in self.unavailable:
            return False, None
        return True, self.lookups.get(artist_name)

    def lookup_artist_mbid(self, artist_name: str) -> Optional[str]:
        return self.resolve_artist_mbid(artist_name)[1]


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "repair.db"))
    yield database
    database.close()


def _artist(db: Database, aid: str, name: str, mbid: Optional[str] = None) -> None:
    db.upsert_library_artist(LibraryArtist(id=aid, name=name, mbid=mbid, monitored=False))


def _album(db: Database, alb: str, aid: str, title: str, rg: Optional[str] = None) -> None:
    db.upsert_library_album(LibraryAlbum(id=alb, artist_id=aid, title=title, mb_release_group_id=rg, mb_release_id=rg and "rel-" + rg))


def _tracks(db: Database, tmp_path: Path, alb: str, aid: str, n: int, with_file: bool, prefix: str = "") -> None:
    for i in range(1, n + 1):
        tid = f"{alb}-{prefix}{i}"
        db.upsert_library_track(
            LibraryTrack(id=tid, album_id=alb, artist_id=aid, title=f"{prefix}Song {i}", track_number=i)
        )
        if with_file:
            f = tmp_path / f"{tid}.flac"
            f.write_bytes(b"x")
            db.upsert_library_file(
                LibraryFile(id=f"file-{tid}", track_id=tid, file_path=str(f), relative_path=f.name, codec="FLAC")
            )


def _names(db: Database) -> list[str]:
    return sorted(str(r["name"]) for r in db.conn.execute("SELECT name FROM library_artists").fetchall())


def _count(db: Database, sql: str, *params: Any) -> int:
    return int(db.conn.execute(sql, params).fetchone()[0])


def _changes(counts: dict[str, int]) -> dict[str, int]:
    return {k: v for k, v in counts.items() if k not in ("ids_kept_alias", "lookups_unavailable") and v}


def test_merges_duplicate_mbid_artists_and_same_title_albums(db: Database, tmp_path: Path):
    _artist(db, "a1", "Jay Z", JAYZ)
    _artist(db, "a2", "JAY-Z", JAYZ)
    _album(db, "bp1", "a1", "The Blueprint")
    _album(db, "bp2", "a2", "The Blueprint", rg="rg-bp")
    _album(db, "rd", "a2", "Reasonable Doubt")
    _tracks(db, tmp_path, "bp1", "a1", 2, True, prefix="x")
    _tracks(db, tmp_path, "bp2", "a2", 11, True)
    db.conn.execute("INSERT INTO tags (id, label) VALUES (1, 'rap'), (2, 'east')")
    db.conn.execute("INSERT INTO artist_tags VALUES ('a1', 1), ('a2', 1), ('a1', 2)")
    db.conn.execute("INSERT INTO library_collections (id, name, clean_name) VALUES ('c1', 'Hits', 'hits')")
    db.conn.execute("INSERT INTO library_collection_albums VALUES ('c1', 'bp1', 0)")
    db.conn.execute("INSERT INTO artist_links (library_artist_id, mbid) VALUES ('a1', ?)", (JAYZ,))
    db.conn.execute(
        "INSERT INTO download_history (id, event, album_id) VALUES ('h1', 'grabbed', 'bp1')"
    )
    db.conn.commit()

    counts = run_artist_repair(db, FakeEnricher({JAYZ: "JAY-Z"}))

    assert counts["artists_merged"] == 1 and counts["albums_merged"] == 1 and counts["errors"] == 0
    assert _names(db) == ["JAY-Z"]
    assert _count(db, "SELECT COUNT(*) FROM library_albums WHERE title = 'The Blueprint'") == 1
    assert _count(db, "SELECT COUNT(*) FROM library_albums WHERE title = 'Reasonable Doubt' AND artist_id = 'a2'") == 1
    assert _count(db, "SELECT COUNT(*) FROM library_tracks WHERE album_id = 'bp2' AND artist_id = 'a2'") == 13
    assert _count(db, "SELECT COUNT(*) FROM library_albums WHERE id = 'bp1'") == 0
    assert _count(db, "SELECT COUNT(*) FROM library_collection_albums WHERE collection_id = 'c1' AND album_id = 'bp2'") == 1
    assert _count(db, "SELECT COUNT(*) FROM artist_tags WHERE artist_id = 'a2'") == 2
    assert _count(db, "SELECT COUNT(*) FROM artist_tags WHERE artist_id = 'a1'") == 0
    assert _count(db, "SELECT COUNT(*) FROM artist_links WHERE library_artist_id = 'a2'") == 1
    assert db.conn.execute("SELECT album_id FROM download_history WHERE id = 'h1'").fetchone()[0] == "bp2"
    assert _count(db, "SELECT COUNT(*) FROM item_events WHERE artist_id = 'a2' AND message LIKE 'Merged artist ''Jay Z''%'") == 1


def test_merge_keeps_the_exact_mb_name_and_fills_gaps(db: Database):
    _artist(db, "m1", "Motley Crue", "mc-id")
    _artist(db, "m2", "Mötley Crüe", None)
    db.conn.execute("UPDATE library_artists SET mbid = 'MC-ID', image_url = 'http://img', monitored = 1 WHERE id = 'm2'")
    db.conn.commit()
    _album(db, "mc-a", "m1", "Dr. Feelgood")  # most albums on the wrong-named row

    counts = run_artist_repair(db, FakeEnricher({"mc-id": "Mötley Crüe", "MC-ID": "Mötley Crüe"}))

    assert counts["artists_merged"] == 1
    row = db.conn.execute("SELECT id, name, image_url, monitored FROM library_artists").fetchone()
    assert (row["id"], row["name"], row["image_url"], row["monitored"]) == ("m2", "Mötley Crüe", "http://img", 1)
    assert _count(db, "SELECT COUNT(*) FROM library_albums WHERE artist_id = 'm2'") == 1


def test_does_not_merge_different_names_sharing_an_id(db: Database):
    _artist(db, "b1", "Basement", JAXX)
    _artist(db, "b2", "Basement Jaxx", JAXX)
    _album(db, "bj", "b1", "Remedy", rg="rg-remedy")  # fileless, from the wrong discography
    enricher = FakeEnricher({JAXX: "Basement Jaxx"}, {"Basement": None, "Basement Jaxx": JAXX})

    counts = run_artist_repair(db, enricher)

    assert counts["artists_merged"] == 0
    assert _names(db) == ["Basement", "Basement Jaxx"]
    assert db.conn.execute("SELECT mbid FROM library_artists WHERE id = 'b1'").fetchone()[0] is None
    assert db.conn.execute("SELECT mbid FROM library_artists WHERE id = 'b2'").fetchone()[0] == JAXX
    assert enricher.lookup_calls == ["Basement"]
    assert counts["ids_cleared"] == 1


def test_clears_wrong_id_and_removes_fileless_wrong_albums(db: Database, tmp_path: Path):
    _artist(db, "f1", "FELIX", MENDEL)
    _album(db, "keep-file", "f1", "My Own Album", rg="rg-own")
    _tracks(db, tmp_path, "keep-file", "f1", 2, True)
    _album(db, "wanted1", "f1", "Elijah", rg="rg-elijah")
    _tracks(db, tmp_path, "wanted1", "f1", 3, False)
    _album(db, "downloading", "f1", "Songs Without Words", rg="rg-sww")
    _tracks(db, tmp_path, "downloading", "f1", 1, False)
    _album(db, "requested", "f1", "Requested One", rg="rg-req")
    db.conn.execute("INSERT INTO download_clients (id, name, driver_type, host_url) VALUES ('c', 'c', 'qbittorrent', 'http://x')")
    db.conn.execute("INSERT INTO active_downloads (id, client_id, title, artist, album_id) VALUES ('d1', 'c', 't', 'a', 'downloading')")
    db.upsert_user("u1", "user", "u@example.com")
    db.conn.execute(
        "INSERT INTO music_requests (id, user_id, item_type, title, artist, catalog_album_id) "
        "VALUES ('r1', 'u1', 'album', 'Requested One', 'FELIX', 'requested')"
    )
    db.conn.commit()

    counts = run_artist_repair(db, FakeEnricher({MENDEL: "Felix Mendelssohn"}, {"FELIX": None}))

    assert counts["ids_cleared"] == 1 and counts["wrong_albums_removed"] == 1 and counts["errors"] == 0
    assert db.conn.execute("SELECT mbid FROM library_artists WHERE id = 'f1'").fetchone()[0] is None
    left = sorted(r[0] for r in db.conn.execute("SELECT id FROM library_albums WHERE artist_id = 'f1'"))
    assert left == ["downloading", "keep-file", "requested"]
    assert _count(db, "SELECT COUNT(*) FROM library_albums WHERE mb_release_group_id IS NOT NULL OR mb_release_id IS NOT NULL") == 0
    assert _count(db, "SELECT COUNT(*) FROM library_tracks WHERE album_id = 'wanted1'") == 0
    assert _count(db, "SELECT COUNT(*) FROM library_tracks WHERE album_id = 'keep-file'") == 2


def test_replaces_wrong_id_with_validated_one(db: Database):
    _artist(db, "f1", "FELIX", MENDEL)
    _album(db, "w", "f1", "Elijah", rg="rg-elijah")
    enricher = FakeEnricher({MENDEL: "Felix Mendelssohn"}, {"FELIX": FELIX_REAL})

    counts = run_artist_repair(db, enricher)

    assert counts["ids_replaced"] == 1 and counts["ids_cleared"] == 0 and counts["wrong_albums_removed"] == 1
    assert db.conn.execute("SELECT mbid FROM library_artists WHERE id = 'f1'").fetchone()[0] == FELIX_REAL


def test_keeps_id_on_alias_match(db: Database):
    _artist(db, "p1", "Prince", "prince-id")
    _album(db, "pa", "p1", "Purple Rain", rg="rg-pr")
    enricher = FakeEnricher({"prince-id": "Prince Rogers Nelson"}, {"Prince": "prince-id"})

    counts = run_artist_repair(db, enricher)

    assert counts["ids_kept_alias"] == 1 and not _changes(counts)
    assert db.conn.execute("SELECT mbid FROM library_artists WHERE id = 'p1'").fetchone()[0] == "prince-id"
    assert db.conn.execute("SELECT mb_release_group_id FROM library_albums WHERE id = 'pa'").fetchone()[0] == "rg-pr"


def test_unknown_musicbrainz_id_is_left_alone(db: Database):
    _artist(db, "u1", "Someone", "gone-id")
    enricher = FakeEnricher({})  # mirror offline / id unknown

    counts = run_artist_repair(db, enricher)

    assert not _changes(counts) and enricher.lookup_calls == []
    assert db.conn.execute("SELECT mbid FROM library_artists WHERE id = 'u1'").fetchone()[0] == "gone-id"


def test_refiles_joined_credit_artist(db: Database):
    _artist(db, "t1", "2Pac")
    _artist(db, "t2", "2Pac; Dr. Dre; Roger Troutman")
    _album(db, "gh", "t2", "Greatest Hits")
    _artist(db, "af1", "AFROJACK", "afro-id")
    _artist(db, "af2", "AFROJACK & Martin Garrix")
    _album(db, "af-a", "af2", "Turn Up")
    _artist(db, "sg", "Simon & Garfunkel")
    _album(db, "sg-a", "sg", "Bookends")
    _artist(db, "jz", "JAY-Z", JAYZ)
    _artist(db, "jf", "JAY‐Z feat. Foxy Brown")
    _artist(db, "ft", "Brand New Act feat. Someone")
    _artist(db, "ct", "Adam Ten, Rafael")
    enricher = FakeEnricher({"afro-id": "AFROJACK", JAYZ: "JAY-Z"}, {})

    counts = run_artist_repair(db, enricher)
    assert counts["lookups_unavailable"] == 0

    assert counts["credits_refiled"] == 4
    assert _names(db) == ["2Pac", "AFROJACK", "Adam Ten, Rafael", "Brand New Act", "JAY-Z", "Simon & Garfunkel"]
    assert db.conn.execute("SELECT artist_id FROM library_albums WHERE id = 'gh'").fetchone()[0] == "t1"
    assert db.conn.execute("SELECT artist_id FROM library_albums WHERE id = 'af-a'").fetchone()[0] == "af1"
    assert db.conn.execute("SELECT artist_id FROM library_albums WHERE id = 'sg-a'").fetchone()[0] == "sg"
    assert _count(db, "SELECT COUNT(*) FROM library_artists WHERE name = 'Brand New Act'") == 1


def test_joined_credit_prefers_target_with_id(db: Database):
    _artist(db, "x1", "Dupe Artist")
    _artist(db, "x2", "Dupe Artist", "x-id")
    _artist(db, "x3", "Dupe Artist; Guest")
    _album(db, "xa", "x3", "Collab")

    run_artist_repair(db, FakeEnricher({"x-id": "Dupe Artist"}))

    assert db.conn.execute("SELECT artist_id FROM library_albums WHERE id = 'xa'").fetchone()[0] == "x2"


def test_repair_is_idempotent(db: Database, tmp_path: Path):
    _artist(db, "a1", "Jay Z", JAYZ)
    _artist(db, "a2", "JAY-Z", JAYZ)
    _album(db, "bp1", "a1", "The Blueprint")
    _album(db, "bp2", "a2", "The Blueprint")
    _artist(db, "f1", "FELIX", MENDEL)
    _album(db, "w", "f1", "Elijah")
    _artist(db, "t1", "2Pac")
    _artist(db, "t2", "2Pac; Dr. Dre")
    _album(db, "gh", "t2", "Greatest Hits")
    enricher = FakeEnricher({JAYZ: "JAY-Z", MENDEL: "Felix Mendelssohn"}, {"FELIX": None})

    first = run_artist_repair(db, enricher)
    second = run_artist_repair(db, enricher)

    assert first["artists_merged"] == 1 and first["ids_cleared"] == 1 and first["credits_refiled"] == 1
    assert not _changes(second)


def test_merge_rolls_back_on_failure(db: Database, monkeypatch: pytest.MonkeyPatch):
    _artist(db, "a1", "Jay Z", JAYZ)
    _artist(db, "a2", "JAY-Z", JAYZ)
    _album(db, "x", "a1", "Only Here")

    def boom(*_a: Any, **_k: Any) -> None:
        import sqlite3

        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(artist_repair, "_repoint_artist_refs", boom)
    counts = run_artist_repair(db, FakeEnricher({JAYZ: "JAY-Z"}))

    assert counts["errors"] == 1 and counts["artists_merged"] == 0
    assert _names(db) == ["JAY-Z", "Jay Z"]
    assert db.conn.execute("SELECT artist_id FROM library_albums WHERE id = 'x'").fetchone()[0] == "a1"


def test_should_stop_halts_repair(db: Database):
    _artist(db, "f1", "FELIX", MENDEL)
    _album(db, "w", "f1", "Elijah")
    enricher = FakeEnricher({MENDEL: "Felix Mendelssohn"}, {"FELIX": None})

    counts = run_artist_repair(db, enricher, should_stop=lambda: True)

    assert not _changes(counts)
    assert db.conn.execute("SELECT mbid FROM library_artists WHERE id = 'f1'").fetchone()[0] == MENDEL


def test_progress_is_reported(db: Database):
    _artist(db, "a", "A", "ida")
    _artist(db, "b", "B", "idb")
    seen: list[tuple[int, Optional[int]]] = []

    run_artist_repair(db, FakeEnricher({"ida": "A", "idb": "B"}), on_progress=lambda c, t: seen.append((c, t)))

    assert seen == [(1, 2), (2, 2)]


def test_name_key_folds_variants():
    assert artist_name_key("Jay Z") == artist_name_key("JAY-Z") == artist_name_key("JAŸ-Z")
    assert artist_name_key("Toots & The Maytals") == artist_name_key("Toots And The Maytals")


def test_create_backup_accepts_pre_repair_kind(db: Database, tmp_path: Path):
    from trackseerr.backup import BACKUP_FILENAME_RE, list_backups

    path = create_backup(db, kind="pre_repair", backup_dir=tmp_path / "bk")

    assert path.name.endswith("_pre_repair.zip") and BACKUP_FILENAME_RE.match(path.name)
    assert zipfile.is_zipfile(path)
    assert [b["kind"] for b in list_backups(backup_dir=tmp_path / "bk")] == ["pre_repair"]
    with pytest.raises(ValueError):
        create_backup(db, kind="bogus", backup_dir=tmp_path / "bk")


class _Run:
    cancelled = False
    message = ""

    def should_stop(self) -> bool:
        return False

    def set_progress(self, current: int, total: Optional[int] = None) -> None:
        pass


def test_repair_and_match_backs_up_repairs_then_matches(db: Database, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.backup
    import trackseerr.mb_metadata_store
    import trackseerr.release_matching

    order: list[str] = []
    enricher = FakeEnricher({})
    monkeypatch.setattr(trackseerr.backup, "create_backup", lambda d, kind="manual", backup_dir=None: order.append(kind))
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: enricher)
    monkeypatch.setattr(artist_repair, "run_artist_repair", lambda *a, **k: order.append("repair") or {"artists_merged": 0})
    monkeypatch.setattr(
        trackseerr.release_matching, "match_album_releases", lambda *a, **k: order.append("match") or {"matched": 2}
    )
    run = _Run()

    artist_repair.run_repair_and_match(db, run)

    assert order == ["pre_repair", "repair", "match"]
    assert "artists_merged=0" in run.message and "matched=2" in run.message


def test_repair_and_match_continues_when_backup_fails(db: Database, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.backup
    import trackseerr.mb_metadata_store
    import trackseerr.release_matching

    def fail(*_a: Any, **_k: Any) -> None:
        raise OSError("read-only")

    ran: list[str] = []
    monkeypatch.setattr(trackseerr.backup, "create_backup", fail)
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: FakeEnricher({}))
    monkeypatch.setattr(trackseerr.release_matching, "match_album_releases", lambda *a, **k: ran.append("m") or {})

    artist_repair.run_repair_and_match(db, _Run())

    assert ran == ["m"]


def test_run_repair_and_match_skips_fill_without_key(db: Database, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.backup
    import trackseerr.mb_metadata_store

    monkeypatch.setattr(trackseerr.backup, "create_backup", lambda *a, **k: None)
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: FakeEnricher({}))
    run = _Run()

    artist_repair.run_repair_and_match(db, run)

    assert "fill: skipped" in run.message
    assert "; search: tracks=0, filled=0" in run.message


def test_run_repair_and_match_runs_search_after_fill(db: Database, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.backup
    import trackseerr.mb_metadata_store
    import trackseerr.recording_search

    monkeypatch.setattr(trackseerr.backup, "create_backup", lambda *a, **k: None)
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: FakeEnricher({}))
    monkeypatch.setattr(
        trackseerr.recording_search,
        "fill_recordings_from_search",
        lambda *a, **k: {"tracks": 3, "filled": 2, "no_match": 1, "unavailable": 0, "errors": 0},
    )
    run = _Run()

    artist_repair.run_repair_and_match(db, run)

    assert "; search: tracks=3, filled=2" in run.message
    assert run.message.index("fill:") < run.message.index("search:") < run.message.index("link:")


def test_run_repair_and_match_runs_fill_with_key(db: Database, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.backup
    import trackseerr.mb_metadata_store
    import trackseerr.release_matching

    db.update_media_management_settings({"acoustid_api_key": "test-acoustid-key"})
    monkeypatch.setattr(trackseerr.backup, "create_backup", lambda *a, **k: None)
    monkeypatch.setattr(trackseerr.mb_metadata_store, "get_shared_enricher", lambda d: FakeEnricher({}))
    called: list[str] = []

    def fake_fill(d, enricher, api_key, should_stop=None, on_progress=None):
        called.append(api_key)
        return {"tracks": 2, "filled": 1, "no_fingerprint": 0, "no_match": 1, "errors": 0}

    monkeypatch.setattr(trackseerr.release_matching, "fill_recordings_from_fingerprints", fake_fill)
    run = _Run()

    artist_repair.run_repair_and_match(db, run)

    assert called == ["test-acoustid-key"]
    assert "fill: tracks=2, filled=1" in run.message


def test_startup_repair_runs_once(db: Database, monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []
    monkeypatch.setattr(artist_repair, "run_repair_and_match", lambda d, run, stop=None: calls.append(1))

    assert db.get_kv(REPAIR_KV_KEY) is None
    assert artist_repair.run_startup_artist_repair(db) is True
    assert db.get_kv(REPAIR_KV_KEY) is not None
    assert artist_repair.run_startup_artist_repair(db) is False
    assert calls == [1]


def test_startup_repair_failure_leaves_marker_unset(db: Database, monkeypatch: pytest.MonkeyPatch):
    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("mirror exploded")

    monkeypatch.setattr(artist_repair, "run_repair_and_match", boom)

    assert artist_repair.run_startup_artist_repair(db) is False
    assert db.get_kv(REPAIR_KV_KEY) is None


def test_startup_repair_skipped_in_lidarr_mode(db: Database, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.library_manager

    monkeypatch.setattr(trackseerr.library_manager, "get_library_mode", lambda d: "lidarr")
    assert artist_repair.start_startup_artist_repair(db) is None


def test_startup_repair_starts_in_native_mode(db: Database, monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []
    monkeypatch.setattr(artist_repair, "run_repair_and_match", lambda d, run, stop=None: calls.append(1))

    thread = artist_repair.start_startup_artist_repair(db)

    assert thread is not None
    thread.join(timeout=10)
    assert calls == [1] and db.get_kv(REPAIR_KV_KEY) is not None


def test_transient_lookup_failure_leaves_id_and_albums_untouched(db: Database):
    _artist(db, "f1", "FELIX", MENDEL)
    _album(db, "w", "f1", "Elijah", rg="rg-elijah")
    enricher = FakeEnricher({MENDEL: "Felix Mendelssohn"}, {}, unavailable={"FELIX"})

    counts = run_artist_repair(db, enricher)

    assert counts["lookups_unavailable"] == 1 and not _changes(counts)
    assert db.conn.execute("SELECT mbid FROM library_artists WHERE id = 'f1'").fetchone()[0] == MENDEL
    row = db.conn.execute("SELECT mb_release_group_id FROM library_albums WHERE id = 'w'").fetchone()
    assert row[0] == "rg-elijah"


def test_comma_split_needs_musicbrainz_to_deny_the_full_name(db: Database):
    _artist(db, "e1", "Earth", "earth-id")
    _artist(db, "e2", "Earth, Wind & Fire")
    _album(db, "ewf", "e2", "That's the Way of the World")
    _artist(db, "tc", "Tyler, The Creator")  # no "Tyler" artist at all
    enricher = FakeEnricher({"earth-id": "Earth"}, {"Earth, Wind & Fire": "ewf-id"})

    counts = run_artist_repair(db, enricher)

    assert counts["credits_refiled"] == 0
    assert db.conn.execute("SELECT artist_id FROM library_albums WHERE id = 'ewf'").fetchone()[0] == "e2"
    assert _count(db, "SELECT COUNT(*) FROM library_artists WHERE name IN ('Earth, Wind & Fire', 'Tyler, The Creator')") == 2


def test_ampersand_split_needs_a_target_with_an_id(db: Database):
    _artist(db, "af1", "AFROJACK")  # no mbid: not enough
    _artist(db, "af2", "AFROJACK & Martin Garrix")

    counts = run_artist_repair(db, FakeEnricher({}, {}))

    assert counts["credits_refiled"] == 0


def test_ampersand_split_skipped_when_lookup_unavailable(db: Database):
    _artist(db, "af1", "AFROJACK", "afro-id")
    _artist(db, "af2", "AFROJACK & Martin Garrix")
    _album(db, "a", "af2", "Turn Up")
    enricher = FakeEnricher({"afro-id": "AFROJACK"}, {}, unavailable={"AFROJACK & Martin Garrix"})

    counts = run_artist_repair(db, enricher)

    assert counts["credits_refiled"] == 0 and counts["lookups_unavailable"] == 1
    assert db.conn.execute("SELECT artist_id FROM library_albums WHERE id = 'a'").fetchone()[0] == "af2"


def test_leading_the_does_not_make_an_id_look_wrong(db: Database):
    _artist(db, "bt", "Beatles", "beatles-id")
    _album(db, "wanted", "bt", "Abbey Road", rg="rg-abbey")
    enricher = FakeEnricher({"beatles-id": "The Beatles"}, {})

    counts = run_artist_repair(db, enricher)

    assert not _changes(counts) and enricher.lookup_calls == []
    assert db.conn.execute("SELECT mbid FROM library_artists WHERE id = 'bt'").fetchone()[0] == "beatles-id"
    assert _count(db, "SELECT COUNT(*) FROM library_albums WHERE id = 'wanted'") == 1

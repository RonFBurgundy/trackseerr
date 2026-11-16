"""Tests for merging one-song playlist albums into the album that holds their release group."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trackseerr.artist_repair import merge_single_album_into
from trackseerr.models import LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "playlist_merge.db"))
    yield database
    database.close()


def _artist(db: Database, aid: str, name: str, mbid: str | None = None) -> None:
    db.upsert_library_artist(LibraryArtist(id=aid, name=name, mbid=mbid, monitored=False))


def _album(
    db: Database, alb: str, aid: str, title: str, rg: str | None = None, monitored: bool = False
) -> None:
    db.upsert_library_album(
        LibraryAlbum(
            id=alb,
            artist_id=aid,
            title=title,
            mb_release_group_id=rg,
            mb_release_id=rg and "rel-" + rg,
            monitored=monitored,
        )
    )


def _track(
    db: Database,
    tmp_path: Path,
    tid: str,
    alb: str,
    aid: str,
    title: str,
    *,
    num: int = 1,
    rec_id: str | None = None,
    with_file: bool = False,
    monitored: bool = False,
) -> None:
    db.upsert_library_track(
        LibraryTrack(
            id=tid,
            album_id=alb,
            artist_id=aid,
            title=title,
            track_number=num,
            mb_recording_id=rec_id,
            monitored=monitored,
        )
    )
    if with_file:
        f = tmp_path / f"{tid}.flac"
        f.write_bytes(b"x")
        db.upsert_library_file(
            LibraryFile(id=f"file-{tid}", track_id=tid, file_path=str(f), relative_path=f.name, codec="FLAC")
        )
        db.conn.execute("UPDATE library_tracks SET has_file = 1 WHERE id = ?", (tid,))
        db.conn.commit()


def _count(db: Database, sql: str, *params: Any) -> int:
    return int(db.conn.execute(sql, params).fetchone()[0])


def test_merge_moves_file_to_matching_album_track(db: Database, tmp_path: Path) -> None:
    _artist(db, "ar1", "The Beatles")
    _album(db, "abbey", "ar1", "Abbey Road", rg="rg-abbey", monitored=False)
    _track(
        db,
        tmp_path,
        "abbey-t2",
        "abbey",
        "ar1",
        "Something",
        num=2,
        rec_id="rec-something",
        with_file=False,
        monitored=False,
    )
    _album(db, "single", "ar1", "Something - Remastered 2009", monitored=True)
    _track(
        db,
        tmp_path,
        "single-t1",
        "single",
        "ar1",
        "Something - Remastered 2009",
        num=1,
        rec_id="rec-something",
        with_file=True,
        monitored=True,
    )

    assert db.album_holding_release_group("ar1", "rg-abbey", "single") == "abbey"
    outcome = merge_single_album_into(db, "single", "abbey")

    assert outcome == "merged"
    assert db.get_library_album("single") is None
    assert db.get_library_track("single-t1") is None

    file_row = db.conn.execute("SELECT track_id FROM library_files WHERE id = 'file-single-t1'").fetchone()
    assert file_row is not None and file_row["track_id"] == "abbey-t2"

    holder_track = db.conn.execute("SELECT has_file, monitored FROM library_tracks WHERE id = 'abbey-t2'").fetchone()
    assert holder_track is not None
    assert int(holder_track["has_file"]) == 1
    assert int(holder_track["monitored"]) == 1

    holder_album = db.conn.execute("SELECT monitored FROM library_albums WHERE id = 'abbey'").fetchone()
    assert holder_album is not None and int(holder_album["monitored"]) == 0


def test_merge_matches_by_title_without_recording(db: Database, tmp_path: Path) -> None:
    _artist(db, "ar1", "The Beatles")
    _album(db, "abbey", "ar1", "Abbey Road", rg="rg-abbey")
    _track(db, tmp_path, "abbey-t2", "abbey", "ar1", "Something", num=2, rec_id=None, with_file=False)
    _album(db, "single", "ar1", "Something - Remastered 2009")
    _track(
        db,
        tmp_path,
        "single-t1",
        "single",
        "ar1",
        "Something (2009 Remaster)",
        num=1,
        rec_id="rec-something",
        with_file=True,
    )

    outcome = merge_single_album_into(db, "single", "abbey")

    assert outcome == "merged"
    assert db.get_library_album("single") is None
    assert db.get_library_track("single-t1") is None
    holder_track = db.conn.execute(
        "SELECT has_file, mb_recording_id FROM library_tracks WHERE id = 'abbey-t2'"
    ).fetchone()
    assert holder_track is not None
    assert int(holder_track["has_file"]) == 1
    assert holder_track["mb_recording_id"] == "rec-something"
    file_row = db.conn.execute("SELECT track_id FROM library_files WHERE id = 'file-single-t1'").fetchone()
    assert file_row is not None and file_row["track_id"] == "abbey-t2"


def test_merge_moves_unmatched_track_onto_holder(db: Database, tmp_path: Path) -> None:
    _artist(db, "ar1", "The Beatles")
    _album(db, "abbey", "ar1", "Abbey Road", rg="rg-abbey")
    _track(db, tmp_path, "abbey-t1", "abbey", "ar1", "Come Together", num=1, rec_id="rec-ct", with_file=False)
    _album(db, "single", "ar1", "Something Single")
    _track(db, tmp_path, "single-t1", "single", "ar1", "Old Brown Shoe", num=1, rec_id="rec-obs", with_file=True)

    outcome = merge_single_album_into(db, "single", "abbey")

    assert outcome == "merged"
    assert db.get_library_album("single") is None
    moved = db.conn.execute("SELECT album_id, artist_id FROM library_tracks WHERE id = 'single-t1'").fetchone()
    assert moved is not None
    assert moved["album_id"] == "abbey"
    assert moved["artist_id"] == "ar1"
    file_row = db.conn.execute("SELECT track_id FROM library_files WHERE id = 'file-single-t1'").fetchone()
    assert file_row is not None and file_row["track_id"] == "single-t1"


def test_merge_conflict_when_holder_track_has_file_changes_nothing(db: Database, tmp_path: Path) -> None:
    _artist(db, "ar1", "The Beatles")
    _album(db, "abbey", "ar1", "Abbey Road", rg="rg-abbey")
    _track(db, tmp_path, "abbey-t2", "abbey", "ar1", "Something", num=2, rec_id="rec-something", with_file=True)
    _album(db, "single", "ar1", "Something - Remastered 2009")
    _track(
        db,
        tmp_path,
        "single-t1",
        "single",
        "ar1",
        "Something - Remastered 2009",
        num=1,
        rec_id="rec-something",
        with_file=True,
    )

    outcome = merge_single_album_into(db, "single", "abbey")

    assert outcome == "conflict"
    assert db.get_library_album("single") is not None
    assert db.get_library_track("single-t1") is not None
    f_single = db.conn.execute("SELECT track_id FROM library_files WHERE id = 'file-single-t1'").fetchone()
    f_abbey = db.conn.execute("SELECT track_id FROM library_files WHERE id = 'file-abbey-t2'").fetchone()
    assert f_single["track_id"] == "single-t1"
    assert f_abbey["track_id"] == "abbey-t2"


def test_merge_repoints_track_and_album_references(db: Database, tmp_path: Path) -> None:
    _artist(db, "ar1", "The Beatles")
    _album(db, "abbey", "ar1", "Abbey Road", rg="rg-abbey")
    _track(db, tmp_path, "abbey-t2", "abbey", "ar1", "Something", num=2, rec_id="rec-something", with_file=False)
    _album(db, "single", "ar1", "Something - Remastered 2009")
    _track(db, tmp_path, "single-t1", "single", "ar1", "Something", num=1, rec_id="rec-something", with_file=True)

    db.conn.execute(
        "INSERT INTO download_history (id, event, album_id, track_id) VALUES ('dh1', 'imported', 'single', 'single-t1')"
    )
    db.upsert_user("u1", "user", "u@example.com")
    db.conn.execute(
        "INSERT INTO music_requests (id, user_id, item_type, title, artist, catalog_album_id, catalog_track_id) "
        "VALUES ('req1', 'u1', 'track', 'Something', 'The Beatles', 'single', 'single-t1')"
    )
    db.conn.execute("INSERT INTO library_collections (id, name, clean_name) VALUES ('col1', 'Mix', 'mix')")
    db.conn.execute("INSERT INTO library_collection_albums VALUES ('col1', 'single', 0)")
    db.replace_track_artists("single-t1", [{"artist_name": "The Beatles", "source": "tags"}])
    db.conn.commit()

    outcome = merge_single_album_into(db, "single", "abbey")

    assert outcome == "merged"
    dh = db.conn.execute("SELECT album_id, track_id FROM download_history WHERE id = 'dh1'").fetchone()
    assert dh is not None and (dh["album_id"], dh["track_id"]) == ("abbey", "abbey-t2")

    req = db.conn.execute(
        "SELECT catalog_album_id, catalog_track_id FROM music_requests WHERE id = 'req1'"
    ).fetchone()
    assert req is not None and (req["catalog_album_id"], req["catalog_track_id"]) == ("abbey", "abbey-t2")

    assert _count(db, "SELECT COUNT(*) FROM library_collection_albums WHERE collection_id = 'col1' AND album_id = 'abbey'") == 1
    assert _count(db, "SELECT COUNT(*) FROM library_collection_albums WHERE album_id = 'single'") == 0
    assert _count(db, "SELECT COUNT(*) FROM library_track_artists WHERE track_id = 'single-t1'") == 0

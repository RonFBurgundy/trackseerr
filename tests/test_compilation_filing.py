"""Tests for filing compilation tracks under Various Artists and junk-titled singles under their real release."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trackseerr.album_track_hydration import clear_negative_cache
from trackseerr.clients.mbid_enricher import VARIOUS_ARTISTS_MBID, MbidEnricherClient
from trackseerr.compilation_filing import (
    home_release_group,
    refile_out_of_various,
    refile_various_album,
    refile_various_albums,
    various_artists_id,
    various_group_for_title,
)
from trackseerr.models import LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack
from trackseerr.release_matching import album_for_recording, link_albums_by_recording
from trackseerr.storage import Database

BOB_MBID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "test_compilation_filing.db"))
    clear_negative_cache()
    yield database
    database.close()


def _group(
    gid: str,
    title: str,
    primary: str = "Album",
    secondary: list[str] | None = None,
    date: str = "2000-01-01",
    artist_ids: list[str] | None = None,
    various: bool = False,
) -> dict[str, Any]:
    return {
        "id": gid,
        "title": title,
        "primary_type": primary,
        "secondary_types": secondary or [],
        "first_release_date": date,
        "artist_ids": artist_ids if artist_ids is not None else ([VARIOUS_ARTISTS_MBID] if various else [BOB_MBID]),
        "is_various": various,
    }


NOW_44 = _group("rg-now44", "Now That's What I Call Music! 44", secondary=["Compilation"], date="1999-11-01", various=True)


def _enricher(
    groups_by_rec: dict[str, list[dict[str, Any]]],
    rg_tracks: dict[str, list[dict[str, Any]]] | None = None,
    search_results: dict[tuple[str, str], list[dict[str, Any]] | None] | None = None,
):
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_recording_release_groups.side_effect = lambda rec: groups_by_rec.get(rec, [])
    enricher.get_release_group_tracks.side_effect = lambda rg, force=False: (rg_tracks or {}).get(rg, [])
    enricher.search_recordings.side_effect = lambda artist, title, limit=10: (search_results or {}).get(
        (artist, title), []
    )
    return enricher


def _seed_album(
    db: Database,
    tmp_path: Path,
    album_id: str,
    title: str,
    tracks: list[dict[str, Any]],
    rg_id: str | None = None,
    artist_id: str = "artist-bob",
    artist_name: str = "Bob Marley vs. Funkstar De Luxe",
    artist_mbid: str | None = BOB_MBID,
    monitored: bool = False,
) -> None:
    if db.get_library_artist(artist_id) is None:
        db.upsert_library_artist(LibraryArtist(id=artist_id, name=artist_name, mbid=artist_mbid, monitored=False))
    db.upsert_library_album(
        LibraryAlbum(
            id=album_id, artist_id=artist_id, title=title, mb_release_group_id=rg_id, monitored=monitored,
            total_tracks=len(tracks),
        )
    )
    for t in tracks:
        tid = f"{album_id}-{t['num']}"
        db.upsert_library_track(
            LibraryTrack(
                id=tid, album_id=album_id, artist_id=artist_id, title=t["title"], track_number=t["num"],
                mb_recording_id=t.get("rec_id"), duration_seconds=t.get("duration_seconds"), monitored=False,
            )
        )
        if t.get("file", True):
            path = tmp_path / f"{tid}.flac"
            path.write_bytes(b"dummy audio")
            db.upsert_library_file(
                LibraryFile(id=f"file-{tid}", track_id=tid, file_path=str(path), relative_path=path.name, codec="FLAC")
            )


NOW_TRACKS = {
    "rg-now44": [
        {"title": "Could I Be In Love", "track_number": 1, "disc_number": 1, "mb_recording_id": "rec-bob"},
        {"title": "Other Song", "track_number": 2, "disc_number": 1, "mb_recording_id": "rec-other"},
        {"title": "Third Song", "track_number": 3, "disc_number": 1, "mb_recording_id": "rec-third"},
    ]
}


def _va_album(db: Database) -> dict[str, Any]:
    va = db.get_library_artist_by_mbid(VARIOUS_ARTISTS_MBID)
    assert va is not None
    holder = db.album_holding_release_group(str(va["id"]), "rg-now44", "")
    assert holder is not None
    album = db.get_library_album(holder)
    assert album is not None
    return album


# --- pure helpers ----------------------------------------------------------------------------------------------


def test_home_release_group_prefers_album_then_single_earliest():
    single = _group("rg-single", "Song", primary="Single", date="1990-01-01")
    late_album = _group("rg-late", "Best Of", date="2010-01-01")
    early_album = _group("rg-early", "First", date="2001-01-01")
    assert home_release_group([single, late_album, early_album], BOB_MBID, "Song")["id"] == "rg-early"
    assert home_release_group([single], BOB_MBID, "Song")["id"] == "rg-single"
    assert home_release_group([], BOB_MBID, "Song") is None


def test_home_release_group_skips_various_and_live():
    live = _group("rg-live", "Live at X", secondary=["Live"], date="1980-01-01")
    various = _group("rg-va", "Hits", various=True, date="1970-01-01")
    other_artist = _group("rg-other", "Other", artist_ids=["22222222-2222-2222-2222-222222222222"])
    studio = _group("rg-studio", "Studio", date="2005-01-01")
    assert home_release_group([live, various, other_artist, studio], BOB_MBID, "Song")["id"] == "rg-studio"
    assert home_release_group([live, various, other_artist], BOB_MBID, "Song") is None


def test_home_release_group_keeps_live_for_live_track():
    live = _group("rg-live", "Live at X", secondary=["Live"], date="1980-01-01")
    studio = _group("rg-studio", "Studio", date="2005-01-01")
    assert home_release_group([live, studio], BOB_MBID, "Song (Live)")["id"] == "rg-live"


def test_home_release_group_own_soundtrack_fallback():
    soundtrack = _group("rg-ost", "Midnight Express", secondary=["Soundtrack"], date="1978-10-06")
    assert home_release_group([soundtrack], BOB_MBID, "Chase", allow_own_compilation=False) is None
    assert home_release_group([soundtrack], BOB_MBID, "Chase", allow_own_compilation=True)["id"] == "rg-ost"


def test_various_group_for_title_matches_now_44():
    other = _group("rg-x", "Ministry Of Sound", various=True)
    assert various_group_for_title([other, NOW_44], "Now That's What I Call Music! 44")["id"] == "rg-now44"
    assert various_group_for_title([other], "Now That's What I Call Music! 44") is None
    assert various_group_for_title([_group("rg-plain", "Now That's What I Call Music! 44")], "Now That's What I Call Music! 44") is None


def test_various_artists_created_when_missing(db: Database):
    assert db.get_library_artist_by_mbid(VARIOUS_ARTISTS_MBID) is None
    va_id = various_artists_id(db)
    va = db.get_library_artist(va_id)
    assert va is not None and va["name"] == "Various Artists"
    assert va["mbid"] == VARIOUS_ARTISTS_MBID
    assert not va["monitored"]
    assert various_artists_id(db) == va_id


# --- link_albums_by_recording ----------------------------------------------------------------------------------


def test_link_files_compilation_under_various_artists(db: Database, tmp_path: Path):
    _seed_album(db, tmp_path, "alb-now", "Now That's What I Call Music! 44",
                [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}])
    enricher = _enricher({"rec-bob": [NOW_44]}, NOW_TRACKS)

    counts = link_albums_by_recording(db, enricher)

    assert counts["filed_compilation"] == 1
    assert counts["merged"] == 1
    assert db.get_library_album("alb-now") is None
    va_album = _va_album(db)
    assert va_album["title"] == "Now That's What I Call Music! 44"
    tracks = {t["mb_recording_id"]: t for t in db.list_library_tracks(album_id=va_album["id"], limit=50)}
    assert set(tracks) == {"rec-bob", "rec-other", "rec-third"}
    assert tracks["rec-bob"]["has_file"]
    credits = db.list_track_artists(str(tracks["rec-bob"]["id"]))
    assert [c["artist_name"] for c in credits] == ["Bob Marley vs. Funkstar De Luxe"]
    assert credits[0]["role"] == "primary"
    for rec in ("rec-other", "rec-third"):
        assert not tracks[rec]["monitored"]
    assert not va_album["monitored"]


def test_link_reuses_existing_various_album(db: Database, tmp_path: Path):
    _seed_album(db, tmp_path, "alb-a", "Now That's What I Call Music! 44",
                [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}])
    _seed_album(db, tmp_path, "alb-b", "Now That's What I Call Music! 44",
                [{"num": 1, "title": "Other Song", "rec_id": "rec-other"}],
                artist_id="artist-other", artist_name="Other Artist", artist_mbid=None)
    enricher = _enricher({"rec-bob": [NOW_44], "rec-other": [NOW_44]}, NOW_TRACKS)

    counts = link_albums_by_recording(db, enricher)

    assert counts["filed_compilation"] == 2
    va = db.get_library_artist_by_mbid(VARIOUS_ARTISTS_MBID)
    assert va is not None
    va_albums = [a for a in db.list_library_albums(artist_id=str(va["id"]), limit=50)]
    assert len(va_albums) == 1
    with_files = [t for t in db.list_library_tracks(album_id=va_albums[0]["id"], limit=50) if t["has_file"]]
    assert {t["mb_recording_id"] for t in with_files} == {"rec-bob", "rec-other"}
    other_credit = db.list_track_artists(str(next(t for t in with_files if t["mb_recording_id"] == "rec-other")["id"]))
    assert other_credit[0]["artist_name"] == "Other Artist"


def test_link_files_junk_title_under_home_release(db: Database, tmp_path: Path):
    _seed_album(db, tmp_path, "alb-junk", "Unknown Album",
                [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}])
    home = _group("rg-home", "Exodus", date="1977-06-03")
    single = _group("rg-single", "Could I Be In Love", primary="Single", date="1977-01-01")
    chart = _group("rg-chart", "Hits", various=True, date="1970-01-01")
    home_tracks = {"rg-home": [
        {"title": "Could I Be In Love", "track_number": 5, "disc_number": 1, "mb_recording_id": "rec-bob"},
        {"title": "Jamming", "track_number": 6, "disc_number": 1, "mb_recording_id": "rec-jam"},
    ]}
    enricher = _enricher({"rec-bob": [chart, single, home]}, home_tracks)

    counts = link_albums_by_recording(db, enricher)

    assert counts["filed_home"] == 1
    assert counts["no_title_match"] == 0
    assert db.get_library_album("alb-junk") is None
    holder_id = db.album_holding_release_group("artist-bob", "rg-home", "")
    assert holder_id is not None
    tracks = {t["mb_recording_id"]: t for t in db.list_library_tracks(album_id=holder_id, limit=50)}
    assert tracks["rec-bob"]["has_file"]
    assert not tracks["rec-jam"]["monitored"]


def test_link_home_conflict_records_choice(db: Database, tmp_path: Path):
    _seed_album(db, tmp_path, "alb-junk", "Unknown Album",
                [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}])
    _seed_album(db, tmp_path, "alb-home", "Exodus",
                [{"num": 5, "title": "Could I Be In Love", "rec_id": "rec-bob"}], rg_id="rg-home")
    enricher = _enricher({"rec-bob": [_group("rg-home", "Exodus")]})

    counts = link_albums_by_recording(db, enricher)

    assert counts["merge_conflict"] == 1
    assert counts["filed_home"] == 0
    assert db.get_library_album("alb-junk") is not None
    assert db.get_library_track("alb-junk-1") is not None
    conflicts = db.list_merge_conflicts()
    assert any(c["source_album_id"] == "alb-junk" and c["holder_album_id"] == "alb-home" for c in conflicts)


def test_link_does_not_link_various_group_under_track_artist(db: Database, tmp_path: Path):
    """A Various Artists group is never linked to a track artist's album (a non-matching title falls through)."""
    _seed_album(db, tmp_path, "alb-two", "Different Name",
                [{"num": 1, "title": "A", "rec_id": "rec-a"}, {"num": 2, "title": "B", "rec_id": "rec-b"}])
    various = _group("rg-va", "Some Hits", various=True)
    enricher = _enricher({"rec-a": [various], "rec-b": [various]})

    counts = link_albums_by_recording(db, enricher)

    assert counts["linked_by_agreement"] == 0
    assert counts["linked"] == 0
    assert counts["no_title_match"] == 1
    album = db.get_library_album("alb-two")
    assert album is not None and not album.get("mb_release_group_id")


# --- refile_various_albums ---------------------------------------------------------------------------------------


def test_refile_various_albums_moves_linked_compilation(db: Database, tmp_path: Path):
    _seed_album(db, tmp_path, "alb-now", "Now That's What I Call Music! 44",
                [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}], rg_id="rg-now44", monitored=True)
    db.set_album_monitored("alb-now", True, by_hand=True)
    enricher = _enricher({"rec-bob": [NOW_44]}, NOW_TRACKS)

    counts = refile_various_albums(db, enricher)

    assert counts["albums"] == 1 and counts["moved"] == 1 and counts["errors"] == 0
    album = db.get_library_album("alb-now")
    va = db.get_library_artist_by_mbid(VARIOUS_ARTISTS_MBID)
    assert va is not None and album is not None
    assert album["artist_id"] == va["id"]
    track = db.get_library_track("alb-now-1")
    assert track is not None and track["artist_id"] == va["id"]
    assert db.list_track_artists("alb-now-1")[0]["artist_name"] == "Bob Marley vs. Funkstar De Luxe"
    assert refile_various_albums(db, enricher)["albums"] == 0


def test_refile_various_albums_merges_into_existing_va_album(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
                [{"num": 2, "title": "Other Song", "rec_id": "rec-other", "file": False}],
                rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID)
    _seed_album(db, tmp_path, "alb-now", "Now That's What I Call Music! 44",
                [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}], rg_id="rg-now44", monitored=True)
    db.set_album_monitored("alb-now", True, by_hand=True)
    enricher = _enricher({"rec-bob": [NOW_44]})

    counts = refile_various_albums(db, enricher)

    assert counts["merged"] == 1 and counts["moved"] == 0
    assert db.get_library_album("alb-now") is None
    moved = [t for t in db.list_library_tracks(album_id="alb-va", limit=50) if t["has_file"]]
    assert [t["mb_recording_id"] for t in moved] == ["rec-bob"]
    assert db.list_track_artists(str(moved[0]["id"]))[0]["artist_name"] == "Bob Marley vs. Funkstar De Luxe"


# --- album_for_recording (new files) ------------------------------------------------------------------------------


def test_new_file_compilation_tag_files_home(db: Database):
    db.upsert_library_artist(LibraryArtist(id="artist-bob", name="Bob Marley vs. Funkstar De Luxe", mbid=BOB_MBID))
    single = _group("rg-single", "Could I Be In Love", primary="Single", artist_ids=[BOB_MBID])
    enricher = _enricher({"rec-bob": [NOW_44, single]})
    with patch("trackseerr.release_matching.mb_metadata_store.get_shared_enricher", return_value=enricher):
        album = album_for_recording(
            db, "artist-bob", "rec-bob", album_title="Now That's What I Call Music! 44", track_title="Could I Be In Love"
        )
    assert album is not None
    assert album["artist_id"] == "artist-bob"
    assert album["mb_release_group_id"] == "rg-single"


def test_new_file_compilation_tag_hand_monitored_va_album(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 2, "title": "Other Song", "rec_id": "rec-other", "file": False}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
        monitored=True,
    )
    db.set_album_monitored("alb-va", True, by_hand=True)
    db.upsert_library_artist(LibraryArtist(id="artist-bob", name="Bob Marley vs. Funkstar De Luxe", mbid=BOB_MBID))
    single = _group("rg-single", "Could I Be In Love", primary="Single", artist_ids=[BOB_MBID])
    enricher = _enricher({"rec-bob": [NOW_44, single]})
    with patch("trackseerr.release_matching.mb_metadata_store.get_shared_enricher", return_value=enricher):
        album = album_for_recording(
            db, "artist-bob", "rec-bob", album_title="Now That's What I Call Music! 44", track_title="Could I Be In Love"
        )
    assert album is not None
    assert album["id"] == "alb-va"
    assert album["artist_id"] == va_id


def test_new_file_compilation_tag_no_home_falls_back_to_va(db: Database):
    db.upsert_library_artist(LibraryArtist(id="artist-bob", name="Bob Marley vs. Funkstar De Luxe", mbid=BOB_MBID))
    enricher = _enricher({"rec-bob": [NOW_44]}, NOW_TRACKS)
    with patch("trackseerr.release_matching.mb_metadata_store.get_shared_enricher", return_value=enricher):
        album = album_for_recording(
            db, "artist-bob", "rec-bob", album_title="Now That's What I Call Music! 44", track_title="Could I Be In Love"
        )
    assert album is not None
    va = db.get_library_artist_by_mbid(VARIOUS_ARTISTS_MBID)
    assert va is not None and album["artist_id"] == va["id"]
    assert album["mb_release_group_id"] == "rg-now44"


def test_album_for_recording_junk_title_under_home(db: Database):
    db.upsert_library_artist(LibraryArtist(id="artist-bob", name="Bob Marley", mbid=BOB_MBID))
    home = _group("rg-home", "Exodus")
    enricher = _enricher({"rec-bob": [home]}, {"rg-home": [
        {"title": "Could I Be In Love", "track_number": 5, "disc_number": 1, "mb_recording_id": "rec-bob"}
    ]})
    with patch("trackseerr.release_matching.mb_metadata_store.get_shared_enricher", return_value=enricher):
        album = album_for_recording(
            db, "artist-bob", "rec-bob", album_title="Beatport Top 100 August 2026", track_title="Could I Be In Love"
        )
        none_without_title = album_for_recording(db, "artist-other", "rec-bob")
    assert album is not None and album["artist_id"] == "artist-bob" and album["title"] == "Exodus"
    assert none_without_title is None


def test_album_for_recording_keeps_title_matching_album_unfiled(db: Database):
    """A file whose album tag matches a real release of the recording is left to the normal album creation."""
    db.upsert_library_artist(LibraryArtist(id="artist-bob", name="Bob Marley", mbid=BOB_MBID))
    enricher = _enricher({"rec-bob": [_group("rg-home", "Exodus")]})
    with patch("trackseerr.release_matching.mb_metadata_store.get_shared_enricher", return_value=enricher):
        assert album_for_recording(db, "artist-bob", "rec-bob", album_title="Exodus", track_title="X") is None


# --- small compilations & refile_out_of_various -----------------------------------------------------------------


ANDROMEDIK_MBID = "22222222-2222-2222-2222-222222222222"
ALICE_MBID = "33333333-3333-3333-3333-333333333333"


def test_refile_small_compilation_files_song_home(db: Database, tmp_path: Path):
    _seed_album(
        db, tmp_path, "alb-now", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}], rg_id="rg-now44",
    )
    single = _group("rg-single", "Could I Be In Love", primary="Single", artist_ids=[BOB_MBID])
    enricher = _enricher({"rec-bob": [NOW_44, single]})

    counts = refile_various_albums(db, enricher)

    assert counts["kept_small"] == 1
    assert counts["filed_home"] == 1
    assert db.get_library_album("alb-now") is None
    va_id = various_artists_id(db)
    assert db.list_library_albums(artist_id=va_id, limit=50) == []
    holder_id = db.album_holding_release_group("artist-bob", "rg-single", "")
    assert holder_id is not None
    tracks = [t for t in db.list_library_tracks(album_id=holder_id, limit=50) if t["has_file"]]
    assert [t["mb_recording_id"] for t in tracks] == ["rec-bob"]


def test_refile_out_of_various_moves_wrongly_linked_album(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-oblivion", "Oblivion",
        [{"num": 1, "title": "Oblivion", "rec_id": "rec-obl"}],
        rg_id="rg-oblivion", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.replace_track_artists("alb-oblivion-1", [
        {"artist_name": "Andromedik", "artist_mbid": ANDROMEDIK_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    oblivion = _group("rg-oblivion", "Oblivion", artist_ids=[ANDROMEDIK_MBID], various=False)
    enricher = _enricher({"rec-obl": [oblivion]})

    counts = refile_out_of_various(db, enricher)

    assert counts["moved_to_artist"] == 1
    andro = db.get_library_artist_by_mbid(ANDROMEDIK_MBID)
    assert andro is not None
    album = db.get_library_album("alb-oblivion")
    assert album is not None and album["artist_id"] == andro["id"]


def test_refile_out_of_various_files_one_song_comp_home(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    home = _group("rg-home", "Exodus", artist_ids=[BOB_MBID])
    enricher = _enricher({"rec-bob": [NOW_44, home]})

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 1
    assert db.get_library_album("alb-va") is None
    bob = db.get_library_artist_by_mbid(BOB_MBID)
    assert bob is not None
    holder = db.album_holding_release_group(str(bob["id"]), "rg-home", "")
    assert holder is not None
    assert any(t["mb_recording_id"] == "rec-bob" and t["has_file"] for t in db.list_library_tracks(album_id=holder, limit=50))


def test_refile_out_of_various_breaks_up_unmonitored_compilation(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    tracks = [{"num": i, "title": f"Song {i}", "rec_id": f"rec-{i}"} for i in range(1, 5)]
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44", tracks,
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    for i in range(1, 5):
        db.replace_track_artists(f"alb-va-{i}", [
            {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
        ])
    home = _group("rg-home", "Exodus", artist_ids=[BOB_MBID])
    enricher = _enricher({f"rec-{i}": [NOW_44, home] for i in range(1, 5)})

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 4
    assert db.get_library_album("alb-va") is None
    bob = db.get_library_artist_by_mbid(BOB_MBID)
    assert bob is not None
    holder = db.album_holding_release_group(str(bob["id"]), "rg-home", "")
    assert holder is not None
    owned = [t for t in db.list_library_tracks(album_id=holder, limit=50) if t["has_file"]]
    assert len(owned) == 4


def test_refile_out_of_various_keeps_hand_monitored_compilation(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    tracks = [{"num": i, "title": f"Song {i}", "rec_id": f"rec-{i}"} for i in range(1, 5)]
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44", tracks,
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
        monitored=True,
    )
    db.set_album_monitored("alb-va", True, by_hand=True)
    for i in range(1, 5):
        db.replace_track_artists(f"alb-va-{i}", [
            {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
        ])
    home = _group("rg-home", "Exodus", artist_ids=[BOB_MBID])
    enricher = _enricher({f"rec-{i}": [NOW_44, home] for i in range(1, 5)})

    counts = refile_out_of_various(db, enricher)

    assert counts["albums"] == 0 and counts["filed_home"] == 0 and counts["moved_to_artist"] == 0
    album = db.get_library_album("alb-va")
    assert album is not None and album["artist_id"] == va_id


def test_auto_monitored_compilation_is_broken_up(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
        monitored=True,
    )
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    home = _group("rg-home", "Exodus", artist_ids=[BOB_MBID])
    enricher = _enricher({"rec-bob": [NOW_44, home]})

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 1
    assert db.get_library_album("alb-va") is None
    bob = db.get_library_artist_by_mbid(BOB_MBID)
    assert bob is not None
    assert db.album_holding_release_group(str(bob["id"]), "rg-home", "") is not None


def test_keep_compilations_setting_keeps_va_album(db: Database, tmp_path: Path):
    db.update_media_management_settings({"keep_compilations_together": True})
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    home = _group("rg-home", "Exodus", artist_ids=[BOB_MBID])
    enricher = _enricher({"rec-bob": [NOW_44, home]})

    counts = refile_out_of_various(db, enricher)

    assert counts["albums"] == 0 and counts["filed_home"] == 0
    album = db.get_library_album("alb-va")
    assert album is not None and album["artist_id"] == va_id

    db.upsert_library_artist(LibraryArtist(id="artist-bob", name="Bob Marley", mbid=BOB_MBID))
    with patch("trackseerr.release_matching.mb_metadata_store.get_shared_enricher", return_value=enricher):
        filed = album_for_recording(
            db, "artist-bob", "rec-bob", album_title="Now That's What I Call Music! 44", track_title="Could I Be In Love"
        )
    assert filed is not None and filed["id"] == "alb-va"


def test_refile_out_of_various_skips_song_without_credit_mbid(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": None, "role": "primary", "position": 0, "source": "tags"}
    ])
    home = _group("rg-home", "Exodus", artist_ids=[BOB_MBID])
    enricher = _enricher({"rec-bob": [NOW_44, home]})

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 0
    album = db.get_library_album("alb-va")
    assert album is not None and album["artist_id"] == va_id


def test_refile_out_of_various_no_release_group_album(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-unk", "Unknown Album",
        [{"num": 1, "title": "Song A", "rec_id": "rec-bob"}, {"num": 2, "title": "Song B", "rec_id": "rec-alice"}],
        rg_id=None, artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.replace_track_artists("alb-unk-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    db.replace_track_artists("alb-unk-2", [
        {"artist_name": "Alice", "artist_mbid": ALICE_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    enricher = _enricher({
        "rec-bob": [_group("rg-bob", "Exodus", artist_ids=[BOB_MBID])],
        "rec-alice": [_group("rg-alice", "Wonderland", artist_ids=[ALICE_MBID])],
    })

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 2
    assert db.get_library_album("alb-unk") is None
    bob = db.get_library_artist_by_mbid(BOB_MBID)
    alice = db.get_library_artist_by_mbid(ALICE_MBID)
    assert bob is not None and alice is not None
    assert db.album_holding_release_group(str(bob["id"]), "rg-bob", "") is not None
    assert db.album_holding_release_group(str(alice["id"]), "rg-alice", "") is not None


LMFAO_MBID = "44444444-4444-4444-4444-444444444444"


def test_out_of_various_credit_without_mbid_searches_home(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Party Rock Anthem", "rec_id": "rec-va-only"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "LMFAO", "artist_mbid": None, "role": "primary", "position": 0, "source": "tags"}
    ])
    single = _group("rg-party-single", "Party Rock Anthem", primary="Single", artist_ids=[LMFAO_MBID])
    enricher = _enricher(
        {"rec-va-only": [NOW_44], "rec-lmfao-studio": [single]},
        search_results={
            ("LMFAO", "Party Rock Anthem"): [
                {
                    "id": "rec-lmfao-studio",
                    "title": "Party Rock Anthem",
                    "score": 100,
                    "artist_ids": [LMFAO_MBID],
                    "artist_names": ["LMFAO"],
                    "artist_credit": "LMFAO",
                }
            ]
        },
    )

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 1
    assert counts["searched_home"] == 1
    assert db.get_library_album("alb-va") is None
    lmfao = db.get_library_artist_by_mbid(LMFAO_MBID)
    assert lmfao is not None and lmfao["name"] == "LMFAO" and not lmfao["monitored"]
    holder = db.album_holding_release_group(str(lmfao["id"]), "rg-party-single", "")
    assert holder is not None
    moved = [t for t in db.list_library_tracks(album_id=holder, limit=50) if t["has_file"]]
    assert len(moved) == 1
    assert moved[0]["mb_recording_id"] == "rec-va-only"


def test_out_of_various_compilation_only_recording_searches_home(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.upsert_library_artist(LibraryArtist(id="artist-bob", name="Bob Marley", mbid=BOB_MBID, monitored=True))
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    home = _group("rg-exodus", "Exodus", artist_ids=[BOB_MBID])
    enricher = _enricher(
        {"rec-va-only": [NOW_44], "rec-bob-studio": [home]},
        search_results={
            ("Bob Marley", "Could I Be In Love"): [
                {
                    "id": "rec-bob-studio",
                    "title": "Could I Be In Love",
                    "score": 100,
                    "artist_ids": [BOB_MBID],
                    "artist_names": ["Bob Marley"],
                    "artist_credit": "Bob Marley",
                }
            ]
        },
    )

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 1
    assert counts["searched_home"] == 1
    assert db.get_library_album("alb-va") is None
    holder = db.album_holding_release_group("artist-bob", "rg-exodus", "")
    assert holder is not None
    moved = [t for t in db.list_library_tracks(album_id=holder, limit=50) if t["has_file"]]
    assert len(moved) == 1 and moved[0]["mb_recording_id"] == "rec-va-only"


def test_out_of_various_search_rejects_other_artist(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    other_home = _group("rg-other", "Other", artist_ids=[ALICE_MBID])
    enricher = _enricher(
        {"rec-va-only": [NOW_44], "rec-other": [other_home]},
        search_results={
            ("Bob Marley", "Could I Be In Love"): [
                {
                    "id": "rec-other",
                    "title": "Could I Be In Love",
                    "score": 100,
                    "artist_ids": [ALICE_MBID],
                    "artist_names": ["Bob Marley"],
                    "artist_credit": "Bob Marley",
                }
            ]
        },
    )

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 0
    assert counts["searched_home"] == 0
    album = db.get_library_album("alb-va")
    assert album is not None and album["artist_id"] == va_id


def test_out_of_various_search_no_candidate_stays(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    enricher = _enricher({"rec-va-only": [NOW_44]}, search_results={("Bob Marley", "Could I Be In Love"): []})

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 0
    assert counts["searched_home"] == 0
    assert counts["errors"] == 0
    album = db.get_library_album("alb-va")
    assert album is not None and album["artist_id"] == va_id


def test_out_of_various_counts_search_no_results(db: Database, tmp_path: Path, caplog):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    enricher = _enricher({"rec-va-only": [NOW_44]}, search_results={("Bob Marley", "Could I Be In Love"): []})

    with caplog.at_level("INFO", logger="trackseerr.compilation_filing"):
        counts = refile_out_of_various(db, enricher)

    assert counts["left_search_no_results"] == 1
    assert any(
        "Could I Be In Love" in r.message and "release groups=1, own-artist groups=0" in r.message
        for r in caplog.records
    )


def test_out_of_various_counts_search_artist_mismatch(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    other_home = _group("rg-other", "Other", artist_ids=[ALICE_MBID])
    enricher = _enricher(
        {"rec-va-only": [NOW_44], "rec-other": [other_home]},
        search_results={
            ("Bob Marley", "Could I Be In Love"): [
                {
                    "id": "rec-other",
                    "title": "Could I Be In Love",
                    "score": 100,
                    "artist_ids": [ALICE_MBID],
                    "artist_names": ["Bob Marley"],
                    "artist_credit": "Bob Marley",
                }
            ]
        },
    )

    counts = refile_out_of_various(db, enricher)

    assert counts["left_search_artist_mismatch"] == 1


def test_out_of_various_counts_no_credit(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    enricher = _enricher({"rec-va-only": [NOW_44]})

    counts = refile_out_of_various(db, enricher)

    assert counts["left_no_credit"] == 1


def test_out_of_various_counts_no_recording(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Mystery Track", "rec_id": None}],
        rg_id=None, artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    enricher = _enricher({})

    counts = refile_out_of_various(db, enricher)

    assert counts["left_no_recording"] == 1


def test_out_of_various_counts_duplicate(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-home", "Exodus",
        [{"num": 5, "title": "Could I Be In Love", "rec_id": "rec-bob"}],
        rg_id="rg-home", artist_id="artist-bob", artist_name="Bob Marley", artist_mbid=BOB_MBID,
    )
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-bob"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    home_group = _group("rg-home", "Exodus", artist_ids=[BOB_MBID])
    enricher = _enricher({"rec-bob": [NOW_44, home_group]})

    counts = refile_out_of_various(db, enricher)

    assert counts["left_duplicate"] == 1
    assert counts["merge_conflict"] == 1


def test_out_of_various_counts_wrong_link_mixed_performers(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-oblivion", "Oblivion",
        [
            {"num": 1, "title": "Oblivion", "rec_id": "rec-obl"},
            {"num": 2, "title": "Other", "rec_id": "rec-other"},
        ],
        rg_id="rg-oblivion", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.replace_track_artists("alb-oblivion-1", [
        {"artist_name": "Andromedik", "artist_mbid": ANDROMEDIK_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    db.replace_track_artists("alb-oblivion-2", [
        {"artist_name": "Alice", "artist_mbid": ALICE_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    oblivion = _group("rg-oblivion", "Oblivion", artist_ids=[ANDROMEDIK_MBID], various=False)
    enricher = _enricher({"rec-obl": [oblivion], "rec-other": [oblivion]})

    counts = refile_out_of_various(db, enricher)

    assert counts["left_wrong_link_mixed_performers"] == 1
    assert counts["skipped"] == 1
    album = db.get_library_album("alb-oblivion")
    assert album is not None and album["artist_id"] == va_id


def test_file_tracks_under_home_without_flag_adds_no_left_counts(db: Database, tmp_path: Path):
    from trackseerr.compilation_filing import file_tracks_under_home

    _seed_album(
        db, tmp_path, "alb-bob", "Unknown",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only"}],
        rg_id=None, artist_id="artist-bob", artist_name="Bob Marley", artist_mbid=BOB_MBID,
    )
    album = db.get_library_album("alb-bob")
    assert album is not None
    counts = {"filed_home": 0, "merge_conflict": 0}
    enricher = _enricher({"rec-va-only": [NOW_44]})

    file_tracks_under_home(db, enricher, album, {"rec-va-only": [NOW_44]}, counts, per_track_artist=False)

    assert not any(k.startswith("left_") for k in counts)


def test_home_release_group_own_compilation_fallback_off_by_default():
    best_of = _group("rg-best", "Legend", secondary=["Compilation"], artist_ids=[BOB_MBID])
    assert home_release_group([best_of], BOB_MBID, "Could I Be In Love") is None
    assert home_release_group([best_of], BOB_MBID, "Could I Be In Love", allow_own_compilation=True) == best_of
    assert home_release_group([NOW_44], BOB_MBID, "Could I Be In Love", allow_own_compilation=True) is None


def test_home_release_group_prefers_studio_over_own_compilation():
    best_of = _group("rg-best", "Legend", secondary=["Compilation"], date="1984-05-08", artist_ids=[BOB_MBID])
    studio = _group("rg-exodus", "Exodus", date="1977-06-03", artist_ids=[BOB_MBID])
    hit = home_release_group([best_of, studio], BOB_MBID, "Could I Be In Love", allow_own_compilation=True)
    assert hit is not None and hit["id"] == "rg-exodus"


def test_out_of_various_search_skips_live_and_files_studio(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only", "duration_seconds": 292.0}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.upsert_library_artist(LibraryArtist(id="artist-bob", name="Bob Marley", mbid=BOB_MBID, monitored=True))
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    live_group = _group("rg-live", "Live", secondary=["Live"])
    home_group = _group("rg-home", "Exodus")
    enricher = _enricher(
        {"rec-va-only": [NOW_44], "rec-live": [live_group], "rec-studio": [home_group]},
        search_results={
            ("Bob Marley", "Could I Be In Love"): [
                {
                    "id": "rec-live",
                    "title": "Could I Be In Love",
                    "score": 100,
                    "length_ms": 297000,
                    "artist_ids": [BOB_MBID],
                    "artist_names": ["Bob Marley"],
                    "artist_credit": "Bob Marley",
                    "release_groups": [live_group],
                },
                {
                    "id": "rec-studio",
                    "title": "Could I Be In Love",
                    "score": 72,
                    "length_ms": 302000,
                    "artist_ids": [BOB_MBID],
                    "artist_names": ["Bob Marley"],
                    "artist_credit": "Bob Marley",
                    "release_groups": [home_group],
                },
            ]
        },
    )

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 1
    assert counts["searched_home"] == 1
    holder = db.album_holding_release_group("artist-bob", "rg-home", "")
    assert holder is not None
    moved = [t for t in db.list_library_tracks(album_id=holder, limit=50) if t["has_file"]]
    assert len(moved) == 1 and moved[0]["mb_recording_id"] == "rec-va-only"


def test_out_of_various_search_ignores_own_recording(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.upsert_library_artist(LibraryArtist(id="artist-bob", name="Bob Marley", mbid=BOB_MBID, monitored=True))
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    home_group = _group("rg-home", "Exodus")
    enricher = _enricher(
        {"rec-va-only": [NOW_44], "rec-studio": [home_group]},
        search_results={
            ("Bob Marley", "Could I Be In Love"): [
                {
                    "id": "rec-va-only",
                    "title": "Could I Be In Love",
                    "score": 100,
                    "artist_ids": [BOB_MBID],
                    "artist_names": ["Bob Marley"],
                    "artist_credit": "Bob Marley",
                    "release_groups": [NOW_44],
                },
                {
                    "id": "rec-studio",
                    "title": "Could I Be In Love",
                    "score": 80,
                    "artist_ids": [BOB_MBID],
                    "artist_names": ["Bob Marley"],
                    "artist_credit": "Bob Marley",
                    "release_groups": [home_group],
                },
            ]
        },
    )

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 1
    rec_calls = [c.args[0] for c in enricher.get_recording_release_groups.call_args_list]
    assert rec_calls.count("rec-va-only") == 1
    assert "rec-studio" in rec_calls


def test_out_of_various_search_tries_at_most_five(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    candidates = [
        {
            "id": f"rec-cand-{i}",
            "title": "Could I Be In Love",
            "score": 90 - i,
            "artist_ids": [BOB_MBID],
            "artist_names": ["Bob Marley"],
            "artist_credit": "Bob Marley",
            "release_groups": [],
        }
        for i in range(7)
    ]
    groups_by_rec = {"rec-va-only": [NOW_44], **{f"rec-cand-{i}": [NOW_44] for i in range(7)}}
    enricher = _enricher(
        groups_by_rec,
        search_results={("Bob Marley", "Could I Be In Love"): candidates},
    )

    counts = refile_out_of_various(db, enricher)

    assert counts["left_search_no_home"] == 1
    rec_calls = [c.args[0] for c in enricher.get_recording_release_groups.call_args_list]
    assert len(rec_calls) <= 6
    assert sum(1 for r in rec_calls if r.startswith("rec-cand-")) == 5


def test_out_of_various_search_keeps_version_guard(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    live_group = _group("rg-live", "Live", secondary=["Live"])
    enricher = _enricher(
        {"rec-va-only": [NOW_44], "rec-live": [live_group]},
        search_results={
            ("Bob Marley", "Could I Be In Love"): [
                {
                    "id": "rec-live",
                    "title": "Could I Be In Love (Live)",
                    "score": 100,
                    "artist_ids": [BOB_MBID],
                    "artist_names": ["Bob Marley"],
                    "artist_credit": "Bob Marley",
                    "release_groups": [live_group],
                }
            ]
        },
    )

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 0
    assert counts["left_search_no_pick"] == 1


def test_out_of_various_files_under_own_best_of_when_only_option(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.upsert_library_artist(LibraryArtist(id="artist-bob", name="Bob Marley", mbid=BOB_MBID, monitored=True))
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    best_of = _group("rg-best", "Legend", secondary=["Compilation"], artist_ids=[BOB_MBID])
    enricher = _enricher({"rec-va-only": [NOW_44, best_of]})

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 1
    holder = db.album_holding_release_group("artist-bob", "rg-best", "")
    assert holder is not None
    moved = [t for t in db.list_library_tracks(album_id=holder, limit=50) if t["has_file"]]
    assert len(moved) == 1 and moved[0]["mb_recording_id"] == "rec-va-only"


def test_out_of_various_prefers_searched_studio_over_own_best_of(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.upsert_library_artist(LibraryArtist(id="artist-bob", name="Bob Marley", mbid=BOB_MBID, monitored=True))
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    best_of = _group("rg-best", "Legend", secondary=["Compilation"], artist_ids=[BOB_MBID])
    home_group = _group("rg-home", "Exodus")
    enricher = _enricher(
        {"rec-va-only": [NOW_44, best_of], "rec-studio": [home_group]},
        search_results={
            ("Bob Marley", "Could I Be In Love"): [
                {
                    "id": "rec-studio",
                    "title": "Could I Be In Love",
                    "score": 90,
                    "artist_ids": [BOB_MBID],
                    "artist_names": ["Bob Marley"],
                    "artist_credit": "Bob Marley",
                    "release_groups": [home_group],
                },
            ]
        },
    )

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 1
    assert counts["searched_home"] == 1
    assert db.album_holding_release_group("artist-bob", "rg-home", "") is not None
    assert db.album_holding_release_group("artist-bob", "rg-best", "") is None


def test_out_of_various_own_best_of_when_search_fails(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-va-only"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.upsert_library_artist(LibraryArtist(id="artist-bob", name="Bob Marley", mbid=BOB_MBID, monitored=True))
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    best_of = _group("rg-best", "Legend", secondary=["Compilation"], artist_ids=[BOB_MBID])
    enricher = _enricher(
        {"rec-va-only": [NOW_44, best_of]},
        search_results={("Bob Marley", "Could I Be In Love"): []},
    )

    counts = refile_out_of_various(db, enricher)

    assert counts["filed_home"] == 1
    assert counts.get("left_search_no_results", 0) == 0
    assert db.album_holding_release_group("artist-bob", "rg-best", "") is not None


def test_refile_various_album_moves_song_home(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-home", "duration_seconds": 292.0}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
    )
    db.upsert_library_artist(LibraryArtist(id="artist-bob", name="Bob Marley", mbid=BOB_MBID, monitored=True))
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    home_group = _group("rg-home", "Exodus")
    enricher = _enricher({"rec-home": [NOW_44, home_group]})

    counts = refile_various_album(db, enricher, "alb-va")

    assert counts["filed_home"] == 1
    holder = db.album_holding_release_group("artist-bob", "rg-home", "")
    assert holder is not None
    moved = [t for t in db.list_library_tracks(album_id=holder, limit=50) if t["has_file"]]
    assert len(moved) == 1 and moved[0]["mb_recording_id"] == "rec-home"


def test_refile_various_album_ignores_non_va_album(db: Database, tmp_path: Path):
    _seed_album(
        db, tmp_path, "alb-bob", "Exodus",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-home"}],
        rg_id="rg-home", artist_id="artist-bob", artist_name="Bob Marley", artist_mbid=BOB_MBID,
    )
    enricher = _enricher({})

    counts = refile_various_album(db, enricher, "alb-bob")

    assert counts["albums"] == 0
    assert counts["filed_home"] == 0
    enricher.get_recording_release_groups.assert_not_called()


def test_refile_various_album_keeps_hand_monitored(db: Database, tmp_path: Path):
    va_id = various_artists_id(db)
    _seed_album(
        db, tmp_path, "alb-va", "Now That's What I Call Music! 44",
        [{"num": 1, "title": "Could I Be In Love", "rec_id": "rec-home"}],
        rg_id="rg-now44", artist_id=va_id, artist_name="Various Artists", artist_mbid=VARIOUS_ARTISTS_MBID,
        monitored=True,
    )
    with db._lock:
        db.conn.execute("UPDATE library_albums SET monitored_by_hand = 1 WHERE id = ?", ("alb-va",))
        db.conn.commit()
    db.upsert_library_artist(LibraryArtist(id="artist-bob", name="Bob Marley", mbid=BOB_MBID, monitored=True))
    db.replace_track_artists("alb-va-1", [
        {"artist_name": "Bob Marley", "artist_mbid": BOB_MBID, "role": "primary", "position": 0, "source": "tags"}
    ])
    home_group = _group("rg-home", "Exodus")
    enricher = _enricher({"rec-home": [NOW_44, home_group]})

    counts = refile_various_album(db, enricher, "alb-va")

    assert counts["filed_home"] == 0
    assert db.album_holding_release_group("artist-bob", "rg-home", "") is None
    va_tracks = [t for t in db.list_library_tracks(album_id="alb-va", limit=50) if t["has_file"]]
    assert len(va_tracks) == 1

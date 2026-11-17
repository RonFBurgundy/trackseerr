"""Tests for library artist and album name lookups using repair folding and title-matching rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from trackseerr.artist_repair import primary_credit
from trackseerr.models import LibraryAlbum, LibraryArtist
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "lookup.db"))
    yield database
    database.close()


def test_artist_lookup_folds_dashes_and_and(db: Database):
    jayz = db.upsert_library_artist(LibraryArtist(id="a-jayz", name="JAY-Z"))
    simon = db.upsert_library_artist(LibraryArtist(id="a-sg", name="Simon & Garfunkel"))
    beatles = db.upsert_library_artist(LibraryArtist(id="a-beatles", name="The Beatles"))

    assert db.get_library_artist_by_name("JAY\u2010Z")["id"] == jayz["id"]
    assert db.get_library_artist_by_name("Jay Z")["id"] == jayz["id"]
    assert db.get_library_artist_by_name("JA\u0178-Z")["id"] == jayz["id"]
    assert db.get_library_artist_by_name("Simon and Garfunkel")["id"] == simon["id"]
    assert db.get_library_artist_by_name("Beatles")["id"] == beatles["id"]


def test_artist_lookup_prefers_mbid_then_oldest(db: Database):
    db.upsert_library_artist(
        LibraryArtist(id="a-old", name="Jay Z", created_at="2020-01-01 00:00:00")
    )
    with_mbid = db.upsert_library_artist(
        LibraryArtist(
            id="a-mbid",
            name="JAY-Z",
            mbid="f82bcf78-5b69-4622-a5ef-73800768d9ac",
            created_at="2022-01-01 00:00:00",
        )
    )
    assert db.get_library_artist_by_name("JAY\u2010Z")["id"] == with_mbid["id"]

    oldest = db.upsert_library_artist(
        LibraryArtist(id="b-1", name="The Prodigy", created_at="2019-01-01 00:00:00")
    )
    db.upsert_library_artist(
        LibraryArtist(id="b-2", name="Prodigy, The", created_at="2021-01-01 00:00:00")
    )
    assert db.get_library_artist_by_name("Prodigy")["id"] == oldest["id"]


def test_artist_lookup_joined_credit_returns_primary(db: Database):
    tupac = db.upsert_library_artist(LibraryArtist(id="a-2pac", name="2Pac"))
    ab = db.upsert_library_artist(LibraryArtist(id="a-ab", name="Above & Beyond"))

    assert db.get_library_artist_by_name("2Pac; Dr. Dre")["id"] == tupac["id"]
    assert db.get_library_artist_by_name("2Pac feat. Dr. Dre")["id"] == tupac["id"]
    assert db.get_library_artist_by_name("2Pac & Dr. Dre")["id"] == tupac["id"]
    assert db.get_library_artist_by_name("2Pac, Dr. Dre")["id"] == tupac["id"]
    assert db.get_library_artist_by_name("Above & Beyond x Zo\u00eb")["id"] == ab["id"]
    assert primary_credit("2Pac; Dr. Dre") == "2Pac"
    assert primary_credit("2Pac feat. Dr. Dre") == "2Pac"
    assert primary_credit("Above & Beyond x Zo\u00eb") == "Above & Beyond"
    assert primary_credit("2Pac") is None


def test_artist_lookup_real_duo_without_primary_is_none(db: Database):
    db.upsert_library_artist(LibraryArtist(id="a-2pac", name="2Pac"))

    assert db.get_library_artist_by_name("Simon & Garfunkel") is None
    assert db.get_library_artist_by_name("Hall & Oates") is None


def test_artist_cache_sees_new_artist(db: Database):
    assert db.get_library_artist_by_name("Beatles") is None

    created = db.upsert_library_artist(LibraryArtist(id="a-beatles", name="The Beatles"))
    hit = db.get_library_artist_by_name("Beatles")
    assert hit is not None and hit["id"] == created["id"]

    db.delete_library_artist(created["id"])
    assert db.get_library_artist_by_name("Beatles") is None


def test_album_lookup_ignores_remaster_suffix(db: Database):
    artist = db.upsert_library_artist(LibraryArtist(id="a-bowie", name="David Bowie"))
    album = db.upsert_library_album(
        LibraryAlbum(id="alb-hd", artist_id=artist["id"], title="Hunky Dory")
    )

    hit = db.get_library_album_by_title(artist["id"], "Hunky Dory (2015 Remaster)")
    assert hit is not None and hit["id"] == album["id"]

    # Reverse direction: stored with remaster suffix, queried with plain title
    remastered = db.upsert_library_album(
        LibraryAlbum(id="alb-low", artist_id=artist["id"], title="Low (2017 Remastered Version)")
    )
    hit_plain = db.get_library_album_by_title(artist["id"], "Low")
    assert hit_plain is not None and hit_plain["id"] == remastered["id"]

    # Several matches -> None
    db.upsert_library_album(
        LibraryAlbum(id="alb-hd-deluxe", artist_id=artist["id"], title="Hunky Dory (Deluxe Edition)")
    )
    assert db.get_library_album_by_title(artist["id"], "Hunky Dory (2015 Remaster)") is None


def test_album_lookup_keeps_live_distinct(db: Database):
    artist = db.upsert_library_artist(LibraryArtist(id="a-bowie", name="David Bowie"))
    db.upsert_library_album(
        LibraryAlbum(id="alb-hd", artist_id=artist["id"], title="Hunky Dory")
    )

    assert db.get_library_album_by_title(artist["id"], "Hunky Dory (Live)") is None
    assert db.get_library_album_by_title(artist["id"], "Hunky Dory (Remix)") is None
    assert db.get_library_album_by_title(artist["id"], "Hunky Dory (Instrumental)") is None


def test_album_lookup_respects_release_group(db: Database):
    artist = db.upsert_library_artist(LibraryArtist(id="a-bowie", name="David Bowie"))
    linked = db.upsert_library_album(
        LibraryAlbum(
            id="alb-hd-rg1",
            artist_id=artist["id"],
            title="Hunky Dory",
            mb_release_group_id="rg-album",
        )
    )

    assert (
        db.get_library_album_by_title(
            artist["id"], "Hunky Dory (2015 Remaster)", release_group_id="rg-other"
        )
        is None
    )
    same_rg = db.get_library_album_by_title(
        artist["id"], "Hunky Dory (2015 Remaster)", release_group_id="rg-album"
    )
    assert same_rg is not None and same_rg["id"] == linked["id"]

    unlinked = db.upsert_library_album(
        LibraryAlbum(
            id="alb-ziggy",
            artist_id=artist["id"],
            title="The Rise and Fall of Ziggy Stardust",
        )
    )
    unlinked_hit = db.get_library_album_by_title(
        artist["id"],
        "The Rise and Fall of Ziggy Stardust (2012 Remaster)",
        release_group_id="rg-ziggy",
    )
    assert unlinked_hit is not None and unlinked_hit["id"] == unlinked["id"]

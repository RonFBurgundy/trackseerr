"""Tests for exclusions store, matching, snapshots, and migration v93."""

from collections.abc import Generator
from pathlib import Path

import pytest

from trackseerr.exclusions import ExclusionHit, find_exclusion, match_key
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path) -> Generator[Database, None, None]:
    database = Database(str(tmp_path / "test.db"))
    try:
        yield database
    finally:
        database.close()


def test_match_key_normalizes() -> None:
    # "The Beatles" == "beatles"
    assert match_key("artist", "The Beatles") == "beatles"
    # Accents folded
    assert match_key("artist", "Béyoncé") == "beyonce"
    assert match_key("artist", "Sigur Rós") == "sigur ros"
    # Album key includes title
    assert match_key("album", "The Beatles", "Abbey Road") == "beatles|abbey road"
    # Track key includes title
    assert match_key("track", "The Beatles", "Yesterday") == "beatles|yesterday"


def test_match_key_rejects_bad_input() -> None:
    # Unknown scope
    with pytest.raises(ValueError, match="Unknown scope"):
        match_key("unsupported", "Artist")

    # Empty normalized artist
    with pytest.raises(ValueError, match="Artist name"):
        match_key("artist", "")
    with pytest.raises(ValueError, match="Artist name"):
        match_key("artist", "   ")
    with pytest.raises(ValueError, match="Artist name"):
        match_key("artist", "!!!")

    # Missing or empty title for album/track
    with pytest.raises(ValueError, match="Title required"):
        match_key("album", "The Beatles", None)
    with pytest.raises(ValueError, match="Title required"):
        match_key("album", "The Beatles", "")
    with pytest.raises(ValueError, match="Title required"):
        match_key("album", "The Beatles", "   ")
    with pytest.raises(ValueError, match="Title required"):
        match_key("track", "The Beatles", None)
    with pytest.raises(ValueError, match="Title required"):
        match_key("track", "The Beatles", "   ")


def test_add_is_idempotent_and_fills_mbid(db: Database) -> None:
    mbid = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
    row1 = db.add_exclusion(
        scope="artist",
        artist_name="Radiohead",
        mbid=None,
        reason="Test reason",
        created_by="tester",
    )
    assert row1["id"].startswith("excl-")
    assert row1["artist_name"] == "Radiohead"
    assert row1["mbid"] is None

    # Idempotent re-add with MBID provided fills in missing MBID
    row2 = db.add_exclusion(
        scope="artist",
        artist_name="Radiohead",
        mbid=mbid,
    )
    assert row2["id"] == row1["id"]
    assert row2["mbid"] == mbid

    # Verify persisted in database
    fetched = db.get_exclusion(row1["id"])
    assert fetched is not None
    assert fetched["id"] == row1["id"]
    assert fetched["mbid"] == mbid


def test_list_search_and_scope_filter(db: Database) -> None:
    db.add_exclusion(scope="artist", artist_name="Radiohead")
    db.add_exclusion(scope="album", artist_name="Pink Floyd", title="The Wall")
    db.add_exclusion(scope="track", artist_name="Daft Punk", title="Get Lucky")

    # List all
    items, total = db.list_exclusions()
    assert total == 3
    assert len(items) == 3

    # Scope filter
    items_art, total_art = db.list_exclusions(scope="artist")
    assert total_art == 1
    assert items_art[0]["artist_name"] == "Radiohead"

    items_alb, total_alb = db.list_exclusions(scope="album")
    assert total_alb == 1
    assert items_alb[0]["title"] == "The Wall"

    # Search filter case-insensitive for artist
    items_search, total_search = db.list_exclusions(search="radio")
    assert total_search == 1
    assert items_search[0]["artist_name"] == "Radiohead"

    # Search filter case-insensitive for title
    items_search_t, total_search_t = db.list_exclusions(search="lucky")
    assert total_search_t == 1
    assert items_search_t[0]["title"] == "Get Lucky"

    # Pagination limit and offset
    items_page, total_page = db.list_exclusions(limit=1, offset=1)
    assert total_page == 3
    assert len(items_page) == 1


def test_delete(db: Database) -> None:
    row = db.add_exclusion(scope="artist", artist_name="Oasis")
    assert db.get_exclusion(row["id"]) is not None

    # Deleting existing returns True
    assert db.delete_exclusion(row["id"]) is True
    assert db.get_exclusion(row["id"]) is None

    # Deleting non-existent returns False
    assert db.delete_exclusion(row["id"]) is False
    assert db.delete_exclusion("excl-nonexistent") is False
    assert db.delete_exclusion("") is False


def test_find_artist_by_mbid(db: Database) -> None:
    mbid = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
    row = db.add_exclusion(scope="artist", artist_name="Radiohead", mbid=mbid)

    # Match by exact MBID
    hit = find_exclusion(db, artist_mbid=mbid)
    assert hit is not None
    assert hit.id == row["id"]
    assert hit.scope == "artist"
    assert hit.artist_name == "Radiohead"

    # Match case-insensitively and with surrounding whitespace
    hit_upper = find_exclusion(db, artist_mbid=f"  {mbid.upper()}  ")
    assert hit_upper is not None
    assert hit_upper.id == row["id"]

    # Different MBID returns None
    assert find_exclusion(db, artist_mbid="00000000-0000-0000-0000-000000000000") is None


def test_find_artist_by_name(db: Database) -> None:
    row = db.add_exclusion(scope="artist", artist_name="The Beatles")

    # Match by artist name
    hit = find_exclusion(db, artist="The Beatles")
    assert hit is not None
    assert hit.id == row["id"]
    assert hit.artist_name == "The Beatles"

    # Normalized variant matches
    hit_norm = find_exclusion(db, artist="beatles")
    assert hit_norm is not None
    assert hit_norm.id == row["id"]


def test_find_artist_in_multi_artist_credit(db: Database) -> None:
    row_b = db.add_exclusion(scope="artist", artist_name="B")
    hit = find_exclusion(db, artist="A feat. B")
    assert hit is not None
    assert hit.id == row_b["id"]
    assert hit.artist_name == "B"

    # Real artist example
    row_dua = db.add_exclusion(scope="artist", artist_name="Dua Lipa")
    hit_dua = find_exclusion(db, artist="Calvin Harris feat. Dua Lipa")
    assert hit_dua is not None
    assert hit_dua.id == row_dua["id"]
    assert hit_dua.artist_name == "Dua Lipa"


def test_find_album_by_release_group_mbid(db: Database) -> None:
    rg_mbid = "b826543b-5544-3d9a-8a83-a7f4577f80db"
    row = db.add_exclusion(scope="album", artist_name="Radiohead", title="OK Computer", mbid=rg_mbid)

    hit = find_exclusion(db, album_mbid=rg_mbid)
    assert hit is not None
    assert hit.id == row["id"]
    assert hit.scope == "album"
    assert hit.title == "OK Computer"

    # Lowercase & stripped check
    hit_upper = find_exclusion(db, album_mbid=f" {rg_mbid.upper()} ")
    assert hit_upper is not None
    assert hit_upper.id == row["id"]


def test_find_album_by_name(db: Database) -> None:
    row = db.add_exclusion(scope="album", artist_name="Pink Floyd", title="The Dark Side of the Moon")

    hit = find_exclusion(db, artist="Pink Floyd", album="The Dark Side of the Moon")
    assert hit is not None
    assert hit.id == row["id"]
    assert hit.scope == "album"
    assert hit.artist_name == "Pink Floyd"
    assert hit.title == "The Dark Side of the Moon"


def test_find_track_by_recording_mbid(db: Database) -> None:
    rec_mbid = "c3f81e62-c128-4e1a-85d1-1efd63f90cb0"
    row = db.add_exclusion(scope="track", artist_name="Radiohead", title="Karma Police", mbid=rec_mbid)

    hit = find_exclusion(db, recording_mbid=rec_mbid)
    assert hit is not None
    assert hit.id == row["id"]
    assert hit.scope == "track"
    assert hit.title == "Karma Police"


def test_find_track_by_name(db: Database) -> None:
    row = db.add_exclusion(scope="track", artist_name="Radiohead", title="Creep")

    hit = find_exclusion(db, artist="Radiohead", title="Creep")
    assert hit is not None
    assert hit.id == row["id"]
    assert hit.scope == "track"
    assert hit.title == "Creep"


def test_album_exclusion_does_not_block_other_albums(db: Database) -> None:
    db.add_exclusion(scope="album", artist_name="The Beatles", title="Abbey Road")

    # Another album by the same artist is not blocked
    hit = find_exclusion(db, artist="The Beatles", album="Let It Be")
    assert hit is None


def test_track_exclusion_does_not_block_same_title_other_artist(db: Database) -> None:
    db.add_exclusion(scope="track", artist_name="Radiohead", title="Creep")

    # Same title by a different artist is not blocked
    hit = find_exclusion(db, artist="TLC", title="Creep")
    assert hit is None


def test_blank_inputs_return_none(db: Database) -> None:
    assert find_exclusion(db) is None
    assert (
        find_exclusion(
            db,
            artist="",
            title="",
            album="",
            artist_mbid="",
            album_mbid="",
            recording_mbid="",
        )
        is None
    )
    assert find_exclusion(db, artist="   ", title="   ") is None
    assert find_exclusion(None, artist="The Beatles") is None


def test_snapshot_invalidated_on_add_and_delete(db: Database) -> None:
    snap1 = db.exclusion_snapshot()
    snap1_repeat = db.exclusion_snapshot()
    assert snap1 is snap1_repeat

    row = db.add_exclusion(scope="artist", artist_name="Blur")
    snap2 = db.exclusion_snapshot()
    assert snap2 is not snap1
    assert snap2.version > snap1.version

    db.delete_exclusion(row["id"])
    snap3 = db.exclusion_snapshot()
    assert snap3 is not snap2
    assert snap3.version > snap2.version


def test_describe_plain_english() -> None:
    # Artist without and with reason
    hit_art = ExclusionHit(id="1", scope="artist", artist_name="The Beatles")
    assert hit_art.describe() == 'Excluded artist "The Beatles"'

    hit_art_r = ExclusionHit(id="1", scope="artist", artist_name="The Beatles", reason="Disliked")
    assert hit_art_r.describe() == 'Excluded artist "The Beatles": Disliked'

    # Album without and with reason
    hit_alb = ExclusionHit(id="2", scope="album", artist_name="The Beatles", title="Abbey Road")
    assert hit_alb.describe() == 'Excluded album "Abbey Road" by The Beatles'

    hit_alb_r = ExclusionHit(
        id="2",
        scope="album",
        artist_name="The Beatles",
        title="Abbey Road",
        reason="Low quality",
    )
    assert hit_alb_r.describe() == 'Excluded album "Abbey Road" by The Beatles: Low quality'

    # Track without and with reason
    hit_trk = ExclusionHit(id="3", scope="track", artist_name="The Beatles", title="Yesterday")
    assert hit_trk.describe() == 'Excluded track "Yesterday" by The Beatles'

    hit_trk_r = ExclusionHit(
        id="3",
        scope="track",
        artist_name="The Beatles",
        title="Yesterday",
        reason="Radio edit",
    )
    assert hit_trk_r.describe() == 'Excluded track "Yesterday" by The Beatles: Radio edit'


def test_match_key_non_latin_artist() -> None:
    k = match_key("artist", "宇多田ヒカル")
    assert k != ""
    assert k == match_key("artist", "宇多田ヒカル")
    assert k == "宇多田ヒカル"


def test_match_key_latin_unchanged() -> None:
    assert match_key("artist", "The Beatles") == "beatles"
    assert match_key("artist", "Beyoncé") == "beyonce"
    assert match_key("album", "The Beatles", "Abbey Road") == "beatles|abbey road"
    assert match_key("track", "The Beatles", "Yesterday") == "beatles|yesterday"


def test_find_non_latin_artist_album_track(db: Database) -> None:
    db.add_exclusion(scope="artist", artist_name="宇多田ヒカル")
    db.add_exclusion(scope="album", artist_name="宇多田ヒカル", title="First Love")
    db.add_exclusion(scope="track", artist_name="宇多田ヒカル", title="Automatic")

    hit_art = find_exclusion(db, artist="宇多田ヒカル")
    assert hit_art is not None
    assert hit_art.scope == "artist"
    assert hit_art.artist_name == "宇多田ヒカル"

    hit_alb = find_exclusion(db, artist="宇多田ヒカル", album="First Love")
    assert hit_alb is not None
    assert hit_alb.artist_name == "宇多田ヒカル"

    hit_trk = find_exclusion(db, artist="Other Artist", title="Automatic", album="Some Album")
    assert hit_trk is None

    hit_trk2 = find_exclusion(db, artist="宇多田ヒカル", title="Automatic")
    assert hit_trk2 is not None


def test_find_non_latin_in_credit(db: Database) -> None:
    db.add_exclusion(scope="artist", artist_name="宇多田ヒカル")
    hit = find_exclusion(db, artist="Artist A & 宇多田ヒカル")
    assert hit is not None
    assert hit.artist_name == "宇多田ヒカル"


def test_hangul_and_cyrillic_names(db: Database) -> None:
    k_hangul = match_key("artist", "방탄소년단")
    assert k_hangul == "방탄소년단"
    db.add_exclusion(scope="artist", artist_name="방탄소년단")
    assert find_exclusion(db, artist="방탄소년단") is not None

    k_cyrillic = match_key("artist", "Кино")
    assert k_cyrillic == "кино"
    db.add_exclusion(scope="artist", artist_name="Кино")
    assert find_exclusion(db, artist="Кино") is not None

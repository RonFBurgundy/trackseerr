"""QualityStore: album tracklist lookups."""

import pytest

from trackseerr.storage import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def test_get_album_track_titles_ordered(db):
    rows = [("c", 2, 1, "Disc Two One"), ("a", 1, 2, "Two"), ("d", 2, 2, "Disc Two Two"), ("b", 1, 1, "One")]
    db.upsert_library_artist({"id": "A1", "name": "A1"})
    db.upsert_library_album({"id": "al", "artist_id": "A1", "title": "al"})
    for tid, disc, num, title in rows:
        db.upsert_library_track(
            {"id": tid, "album_id": "al", "artist_id": "A1", "title": title, "track_number": num, "disc_number": disc}
        )
    assert db.get_album_track_titles("al") == [
        (1, 1, "One"), (1, 2, "Two"), (2, 1, "Disc Two One"), (2, 2, "Disc Two Two"),
    ]
    assert db.get_album_track_titles("missing") == []

"""History records carry the track and library ids so the UI shows artist / album / track and links to the library."""

from unittest.mock import MagicMock

import pytest

from trackseerr.activity_service import (
    lidarr_blocklist_record,
    lidarr_history_record,
    native_history_record,
)
from trackseerr.clients.lidarr import LidarrClient
from trackseerr.storage import Database

BASE = {
    "id": 11,
    "artistId": 3,
    "albumId": 8,
    "artist": {"artistName": "Radiohead"},
    "album": {"title": "OK Computer"},
    "sourceTitle": "Radiohead - OK Computer [FLAC]",
    "date": "2026-10-01T10:00:00Z",
    "data": {},
}


def test_import_event_shows_track_and_ids():
    rec = lidarr_history_record({**BASE, "eventType": "trackFileImported", "track": {"title": "Airbag"}})
    assert rec["track"] == "Airbag" and rec["title"] == "Airbag"
    assert rec["album"] == "OK Computer" and rec["artist"] == "Radiohead"
    assert rec["artist_id"] == "3" and rec["album_id"] == "8"


def test_grab_event_title_is_release_not_album_duplicate():
    rec = lidarr_history_record({**BASE, "eventType": "grabbed"})
    assert rec["track"] is None
    assert rec["title"] == "Radiohead - OK Computer [FLAC]" and rec["title"] != rec["album"]
    assert rec["artist_id"] == "3" and rec["album_id"] == "8"


def test_missing_ids_are_none_not_the_string_none():
    rec = lidarr_history_record({"id": 1, "eventType": "grabbed", "sourceTitle": "x"})
    assert rec["artist_id"] is None and rec["album_id"] is None


def test_blocklist_record_carries_artist_id():
    assert lidarr_blocklist_record({"id": 2, "artistId": 3, "sourceTitle": "x"})["artist_id"] == "3"


def test_get_history_requests_include_track():
    client = LidarrClient("http://lidarr.test:8686", "key-abcdef123456")
    client._get_page = MagicMock(return_value={"records": []})
    client.get_history(1, 25, "date", "desc")
    params = client._get_page.call_args.args[1]
    assert params["includeTrack"] == "true" and params["includeAlbum"] == "true"


@pytest.fixture
def db():
    return Database(":memory:")


def _history(db: Database, hid: str, **cols) -> None:
    cols = {"event": "imported", **cols}
    names = ["id", *cols]
    with db._lock:
        db.conn.execute(
            f"INSERT INTO download_history ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            [hid, *cols.values()],
        )
        db.conn.commit()


def test_native_history_track_and_resolved_library_ids(db):
    db.upsert_library_artist({"id": "art-1", "name": "Radiohead"})
    db.upsert_library_album({"id": "alb-1", "artist_id": "art-1", "title": "OK Computer"})
    db.upsert_library_track({"id": "trk-1", "album_id": "alb-1", "artist_id": "art-1", "title": "Airbag", "track_number": 1})
    _history(db, "h1", item_type="track", track_id="trk-1", artist="Radiohead", album="OK Computer", title="Airbag")
    _history(db, "h2", item_type="album", album_id="alb-1", artist="Radiohead", album="OK Computer", title="OK Computer")
    _history(db, "h3", item_type="album", album_id="gone", artist="X", album="Y", title="Y")
    rows, _ = db.list_download_history(1, 10, "asc")
    recs = {r["id"]: native_history_record(r) for r in rows}
    assert recs["h1"]["track"] == "Airbag"
    assert (recs["h1"]["artist_id"], recs["h1"]["album_id"]) == ("art-1", "alb-1")
    assert recs["h2"]["track"] is None and recs["h2"]["album_id"] == "alb-1"
    assert (recs["h3"]["artist_id"], recs["h3"]["album_id"]) == (None, None)  # deleted album: no dead link

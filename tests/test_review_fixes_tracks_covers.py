"""Review fixes: conservative track dedupe/reuse, Lidarr cover disk cache, SWR key, bulk-edit cascade rules."""

import json
import logging
import time
from unittest.mock import MagicMock

import pytest

from trackseerr import lidarr_library
from trackseerr.clients.lidarr import LidarrApiError
from trackseerr.mediacover import mediacover_service
from trackseerr.storage import Database
from tests.test_monitoring_defaults_bulk_edit import _seed_album, _seed_artist


# ---------------------------------------------------------------------------------------------- track dedupe


def _insert(db, tid, title="Song", clean=None, num=1, disc=1, dur=200.0, foreign=None, mb=None, isrc=None, created="2020-01-01"):
    with db._lock:
        db.conn.execute(
            "INSERT INTO library_tracks (id, album_id, artist_id, title, clean_title, track_number, disc_number, "
            "duration_seconds, monitored, foreign_track_id, mb_recording_id, isrc, created_at) "
            "VALUES (?, 'al', 'A1', ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (tid, title, title.lower() if clean is None else clean, num, disc, dur, foreign, mb, isrc, created),
        )
        db.conn.commit()


def _dedupe(db):
    with db._lock:
        removed = db._dedupe_library_tracks(db.conn.cursor())
        db.conn.commit()
    return removed


def _track_ids(db):
    return {r["id"] for r in db.list_library_tracks(album_id="al", limit=100) if r["id"] not in ("t", "al-t1")}


@pytest.fixture
def test_db(tmp_path):
    database = Database(tmp_path / "t.db")
    yield database
    database.close()


@pytest.fixture
def seeded(test_db):
    _seed_artist(test_db, "A1")
    _seed_album(test_db, "al", "A1", with_file=False)
    return test_db


def test_dedupe_merges_same_title_number_and_close_duration(seeded):
    _insert(seeded, "a", dur=200.0)
    _insert(seeded, "b", dur=201.5, created="2021-01-01")
    assert _dedupe(seeded) == 1
    assert _track_ids(seeded) == {"a"}


@pytest.mark.parametrize(
    "other",
    [
        {"num": 2},
        {"disc": 2},
        {"title": "Other"},
        {"dur": 260.0},
        {"dur": None},
    ],
)
def test_dedupe_does_not_merge_tracks_that_differ(seeded, other):
    _insert(seeded, "a", dur=200.0)
    _insert(seeded, "b", **{"created": "2021-01-01", **other})
    assert _dedupe(seeded) == 0
    assert _track_ids(seeded) == {"a", "b"}


def test_dedupe_does_not_merge_when_both_durations_unknown(seeded):
    _insert(seeded, "a", dur=None)
    _insert(seeded, "b", dur=None)
    assert _dedupe(seeded) == 0


def test_dedupe_does_not_merge_empty_clean_titles(seeded):
    _insert(seeded, "a", title="!!!", clean="", dur=200.0)
    _insert(seeded, "b", title="???", clean="", dur=200.0)
    assert _dedupe(seeded) == 0
    assert _track_ids(seeded) == {"a", "b"}


def test_dedupe_backfills_survivor_identifiers_and_logs_start(seeded, caplog):
    _insert(seeded, "a", foreign=None, mb=None, isrc=None)
    _insert(seeded, "b", foreign="f-1", mb="mb-1", isrc="ISRC1", created="2021-01-01")
    with caplog.at_level(logging.INFO, logger="trackseerr.storage"):
        assert _dedupe(seeded) == 1
    assert any("scanning library_tracks" in r.getMessage() for r in caplog.records)
    row = seeded.conn.execute("SELECT foreign_track_id, mb_recording_id, isrc FROM library_tracks WHERE id='a'").fetchone()
    assert tuple(row) == ("f-1", "mb-1", "ISRC1")


def test_upsert_reuses_row_only_with_matching_known_duration(seeded):
    base = {"album_id": "al", "artist_id": "A1", "title": "Song", "track_number": 3}
    first = seeded.upsert_library_track({**base, "id": "t-a", "duration_seconds": 200.0})
    same = seeded.upsert_library_track({**base, "id": "t-b", "duration_seconds": 201.0})
    longer = seeded.upsert_library_track({**base, "id": "t-c", "duration_seconds": 300.0})
    unknown = seeded.upsert_library_track({**base, "id": "t-d"})
    assert same["id"] == first["id"] == "t-a"  # callers must keep using the RETURNED id
    assert longer["id"] == "t-c" and unknown["id"] == "t-d"


def test_upsert_never_reuses_on_empty_clean_title(seeded):
    base = {"album_id": "al", "artist_id": "A1", "title": "!!!", "track_number": 1, "duration_seconds": 100.0}
    a = seeded.upsert_library_track({**base, "id": "t-a"})
    b = seeded.upsert_library_track({**base, "id": "t-b", "title": "???"})
    assert a["id"] == "t-a" and b["id"] == "t-b"


def test_ingest_caller_keeps_distinct_untagged_tracks_apart(seeded):
    """The ingest/refresh callers pre-pick a uuid and discard the upsert result: that is only safe because reuse
    needs the same non-empty title (which their own lookup already finds). Two same-number, same-duration tracks
    with different titles must stay two rows."""
    for i, title in enumerate(("One", "Two")):
        existing = seeded.get_library_track_by_title("al", title, track_number=1)
        assert existing is None
        seeded.upsert_library_track(
            {"id": f"u-{i}", "album_id": "al", "artist_id": "A1", "title": title, "track_number": 1, "duration_seconds": 90.0}
        )
    assert {"u-0", "u-1"} <= _track_ids(seeded)


# ---------------------------------------------------------------------------------------------- cover cache


@pytest.fixture
def cover_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mediacover_service, "base_dir", tmp_path / "cfg")
    return tmp_path / "cfg"

ID = ("http://lidarr.test", "abc")


def test_cover_roundtrip_and_new_body_never_pairs_with_old_etag(cover_dir):
    lidarr_library.write_cached_cover(ID, "artist", 1, "poster.jpg", b"old", "image/jpeg", 'W/"1"')
    lidarr_library.write_cached_cover(ID, "artist", 1, "poster.jpg", b"new", "image/jpeg", 'W/"2"')
    got = lidarr_library.read_cached_cover(ID, "artist", 1, "poster.jpg")
    assert (got.body, got.etag) == (b"new", 'W/"2"')
    # the superseded body is cleaned up and only one body file remains next to the sidecar
    files = sorted(p.name for p in lidarr_library._cover_path(ID, "artist", 1, "poster.jpg").parent.iterdir())
    assert len(files) == 2 and files[1].endswith(".json")


def test_cover_write_prunes_entries_older_than_twice_ttl(cover_dir):
    lidarr_library.write_cached_cover(ID, "album", 7, "cover.jpg", b"stale", "image/jpeg", 'W/"s"')
    sidecar = lidarr_library._cover_path(ID, "album", 7, "cover.jpg").with_name("cover.jpg.json")
    info = json.loads(sidecar.read_text())
    info["fetched_at"] = time.time() - 2 * lidarr_library.COVER_DISK_TTL_SECONDS - 60
    sidecar.write_text(json.dumps(info))
    lidarr_library.write_cached_cover(ID, "artist", 1, "poster.jpg", b"fresh", "image/jpeg", 'W/"f"')
    assert not sidecar.exists() and not sidecar.with_name(info["body"]).exists()
    assert lidarr_library.read_cached_cover(ID, "artist", 1, "poster.jpg").body == b"fresh"


def test_cover_prune_keeps_entries_between_one_and_two_ttl(cover_dir):
    lidarr_library.write_cached_cover(ID, "album", 7, "cover.jpg", b"aging", "image/jpeg", 'W/"a"')
    sidecar = lidarr_library._cover_path(ID, "album", 7, "cover.jpg").with_name("cover.jpg.json")
    info = json.loads(sidecar.read_text())
    info["fetched_at"] = time.time() - int(1.5 * lidarr_library.COVER_DISK_TTL_SECONDS)
    sidecar.write_text(json.dumps(info))
    lidarr_library.write_cached_cover(ID, "artist", 1, "poster.jpg", b"fresh", "image/jpeg", 'W/"f"')
    got = lidarr_library.read_cached_cover(ID, "album", 7, "cover.jpg")
    assert got is not None and got.body == b"aging" and not got.fresh  # stale-but-usable fallback survives


# ---------------------------------------------------------------------------------------------- SWR


def test_background_refresh_is_single_flight_per_identity_and_kind(monkeypatch):
    started: list[object] = []
    monkeypatch.setattr(lidarr_library, "_run_background", lambda fn: started.append(fn))
    monkeypatch.setattr(lidarr_library, "_refreshing", set())

    def lid(url):
        c = MagicMock()
        c.base_url, c.api_key = url, "k"
        return c

    a, b = lid("http://a"), lid("http://b")
    lidarr_library._refresh_in_background("artists", a)
    lidarr_library._refresh_in_background("artists", a)  # same identity and kind: no-op
    lidarr_library._refresh_in_background("artists", b)  # another Lidarr must not be blocked by the first
    lidarr_library._refresh_in_background("albums", a)
    assert len(started) == 3


# ---------------------------------------------------------------------------------------------- bulk edit


def _client(albums_by_artist, all_albums=None):
    c = MagicMock()
    c.fetch_artist_albums.side_effect = lambda aid: [{"id": i} for i in albums_by_artist.get(aid, [])]
    c.fetch_albums.return_value = all_albums or []
    return c


def test_set_artist_monitored_true_never_cascades(monkeypatch):
    c = _client({1: [10, 11]})
    c.set_artist_monitored.return_value = {"id": 1, "artistName": "A"}
    lidarr_library.set_artist_monitored(c, 1, True, cascade_children=True)
    c.set_albums_monitored.assert_not_called()
    lidarr_library.set_artist_monitored(c, 1, False, cascade_children=True)
    c.set_albums_monitored.assert_called_once_with([10, 11], False)


def test_bulk_edit_small_selection_uses_per_artist_album_fetch():
    c = _client({1: [10], 2: [20, 21]})
    out = lidarr_library.bulk_edit_artists(c, [1, 2], False, None, None, apply_to_albums=True)
    c.fetch_albums.assert_not_called()
    assert [call.args for call in c.fetch_artist_albums.call_args_list] == [(1,), (2,)]
    c.set_albums_monitored.assert_called_once_with([10, 20, 21], False)
    assert out["albums_unmonitored"] == 3


def test_bulk_edit_large_selection_uses_one_album_dump():
    ids = list(range(1, 40))
    c = _client({}, all_albums=[{"id": 100 + i, "artistId": i} for i in ids] + [{"id": 999, "artistId": 5000}])
    out = lidarr_library.bulk_edit_artists(c, ids, False, None, None, apply_to_albums=True)
    c.fetch_artist_albums.assert_not_called()
    assert out["albums_unmonitored"] == len(ids)


def test_bulk_edit_partial_batch_failure_is_logged_and_raised(monkeypatch, caplog):
    monkeypatch.setattr(lidarr_library, "_ALBUM_BATCH", 2)
    c = _client({1: [10, 11, 12, 13, 14]})
    c.set_albums_monitored.side_effect = [None, LidarrApiError("boom")]
    with caplog.at_level(logging.WARNING, logger="trackseerr.lidarr_library"):
        with pytest.raises(LidarrApiError, match="2 of 5 albums were updated"):
            lidarr_library.bulk_edit_artists(c, [1], False, None, None, apply_to_albums=True)
    assert any("batch 2 of 3" in r.getMessage() and "2 of 5" in r.getMessage() for r in caplog.records)

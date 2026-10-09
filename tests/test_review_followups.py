"""Review follow-ups: bulk hydration deadline, hydration single-flight/negative cache, profile preview and deferred
recompute, secondary-type normalisation, re-link vs new-file monitoring. MusicBrainz is always mocked."""

import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from trackseerr import album_track_hydration as hyd
from trackseerr.api.routes.library import albums as albums_routes
from trackseerr.api.routes.library import browse as browse_routes
from trackseerr.api.dependencies import get_mbid_enricher
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.library_monitoring import album_monitored_for_option, normalize_secondary_types
from trackseerr.storage import Database

from tests.test_library_api import (  # noqa: F401
    _auth_headers,
    app_and_client,
    seeded_users,
    test_config,
    test_db,
)
from tests.test_metadata_profiles import _a, _album, _artist, _profile_id
from tests.test_track_level_existing import _artist as _tl_artist
from tests.test_track_level_existing import _file as _tl_file
from tests.test_track_level_existing import _t

TRACKS = [
    {"track_number": 1, "title": "One", "disc_number": 1, "duration_seconds": 10.0, "mb_recording_id": "r1"},
    {"track_number": 2, "title": "Two", "disc_number": 1, "duration_seconds": 20.0, "mb_recording_id": "r2"},
]


def _enricher(tracks=TRACKS, delay: float = 0.0, side_effect=None) -> MagicMock:
    enr = MagicMock(spec=MbidEnricherClient)

    def fetch(_rg: str) -> list[dict[str, Any]]:
        if delay:
            time.sleep(delay)
        if side_effect:
            raise side_effect
        return list(tracks)

    enr.get_release_group_tracks.side_effect = fetch
    return enr


def _bare_albums(db: Database, n: int, monitored: bool = False, option: str = "all") -> list[str]:
    _tl_artist(db, option=option)
    ids = []
    for i in range(n):
        db.upsert_library_album(
            {"id": f"alb{i}", "artist_id": "ar", "title": f"Alb{i}", "monitored": monitored, "mb_release_group_id": f"rg{i}"}
        )
        ids.append(f"alb{i}")
    return ids


# --------------------------------------------------------------------------- 1. bulk deadline

def test_bulk_monitor_has_total_deadline_and_skipped_albums_hydrate_lazily(
    app_and_client, test_db, test_config, seeded_users, monkeypatch
):
    app, client = app_and_client
    enr = _enricher(delay=0.4)
    app.dependency_overrides[get_mbid_enricher] = lambda: enr
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    ids = _bare_albums(test_db, 6)
    monkeypatch.setattr(albums_routes, "BULK_HYDRATE_DEADLINE_SECONDS", 0.6)

    t0 = time.monotonic()
    r = client.post("/api/library/albums/bulk-edit", json={"album_ids": ids, "monitored": True}, headers=h)
    elapsed = time.monotonic() - t0
    assert r.status_code == 200, r.text
    assert r.json() == {"albums_updated": 6}
    assert elapsed < 2.0, elapsed  # 6 x 0.4s serial would be 2.4s
    assert all(test_db.get_library_album(i)["monitored"] for i in ids)  # every album is monitored regardless
    hydrated = [i for i in ids if test_db.list_library_tracks(album_id=i, limit=1)]
    assert 1 <= len(hydrated) < 6
    assert enr.get_release_group_tracks.call_count < 6

    # a skipped album is not negative-cached: opening it later hydrates it, as followers of its monitored flag
    skipped = next(i for i in ids if i not in hydrated)
    assert hyd.hydrate_album_tracks(test_db, _enricher(), skipped) == 2
    assert all(t["monitored"] for t in test_db.list_library_tracks(album_id=skipped))


# --------------------------------------------------------------------------- 2. single-flight / negative cache

def test_concurrent_hydration_of_one_album_fetches_once_and_creates_no_duplicates(test_db: Database):
    (alb,) = _bare_albums(test_db, 1)
    enr = _enricher(delay=0.3)
    results: list[int] = []
    threads = [
        threading.Thread(target=lambda: results.append(hyd.hydrate_album_tracks(test_db, enr, alb))) for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert enr.get_release_group_tracks.call_count == 1
    assert sorted(results) == [0, 0, 0, 2]
    assert len(test_db.list_library_tracks(album_id=alb)) == 2


def test_empty_or_failed_fetch_is_negative_cached_for_ten_minutes(test_db: Database, monkeypatch):
    a0, a1 = _bare_albums(test_db, 2)
    empty, boom = _enricher(tracks=[]), _enricher(side_effect=RuntimeError("mb exploded"))
    for _ in range(3):
        assert hyd.hydrate_album_tracks(test_db, empty, a0) == 0
        assert hyd.hydrate_album_tracks(test_db, boom, a1) == 0
    assert empty.get_release_group_tracks.call_count == 1
    assert boom.get_release_group_tracks.call_count == 1

    assert hyd.NEGATIVE_CACHE_SECONDS == 600.0
    monkeypatch.setattr(hyd, "NEGATIVE_CACHE_SECONDS", 0.0)  # entry now stale: tried again, and success clears it
    good = _enricher()
    assert hyd.hydrate_album_tracks(test_db, good, a0) == 2
    assert good.get_release_group_tracks.call_count == 1


def test_paged_get_skips_a_recently_failed_album(app_and_client, test_db, test_config, seeded_users):
    app, client = app_and_client
    enr = _enricher(tracks=[])
    app.dependency_overrides[get_mbid_enricher] = lambda: enr
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    (alb,) = _bare_albums(test_db, 1)
    for _ in range(3):
        r = client.get("/api/library/tracks/paged", params={"album_id": alb}, headers=h)
        assert r.status_code == 200 and r.json()["total"] == 0
    assert enr.get_release_group_tracks.call_count == 1


def test_hydration_docstring_documents_get_side_effect():
    assert "side effect" in hyd.hydrate_album_tracks.__doc__.lower()
    assert "side effect" in browse_routes.paged_tracks.__doc__.lower()


# --------------------------------------------------------------------------- 3. preview would_change

def _profile_fixture(db: Database) -> int:
    _artist(db, option="all")
    _album(db, "studio")
    _album(db, "live", album_type="live", secondary=["live"])
    _album(db, "off_studio", monitored=False)  # studio album currently unmonitored
    return _profile_id(db, "Studio Albums")


def test_preview_would_change_matches_the_real_recompute_and_writes_nothing(
    app_and_client, test_db, test_config, seeded_users
):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    pid = _profile_fixture(test_db)
    before = [(a["id"], a["monitored"]) for a in test_db.list_library_albums(artist_id="ar")]

    r = client.get(f"/api/library/artists/ar/metadata-profile-preview?profile_id={pid}", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["matching"], body["total"]) == (2, 3)
    assert body["would_change"] == {
        "albums_to_monitor": 1, "albums_to_unmonitor": 1, "tracks_to_monitor": 1, "tracks_to_unmonitor": 1,
    }
    assert [(a["id"], a["monitored"]) for a in test_db.list_library_albums(artist_id="ar")] == before
    assert test_db.get_library_artist("ar")["metadata_profile_id"] is None

    # null / omitted profile_id previews clearing: the recompute under no profile monitors everything ('all')
    r = client.get("/api/library/artists/ar/metadata-profile-preview", headers=h)
    assert r.status_code == 200
    assert r.json()["would_change"] == {
        "albums_to_monitor": 1, "albums_to_unmonitor": 0, "tracks_to_monitor": 1, "tracks_to_unmonitor": 0,
    }
    assert r.json()["matching"] == r.json()["total"] == 3

    # the real recompute lands exactly where the preview said
    test_db.bulk_edit_library_artists(["ar"], metadata_profile_id=pid, apply_monitor_to_albums=True)
    assert (_a(test_db, "studio"), _a(test_db, "live"), _a(test_db, "off_studio")) == (True, False, True)
    assert client.get("/api/library/artists/ar/metadata-profile-preview?profile_id=9999", headers=h).status_code == 404
    assert client.get("/api/library/artists/nope/metadata-profile-preview?profile_id=1", headers=h).status_code == 404


def test_preview_under_existing_counts_tracks_with_files(test_db: Database):
    _artist(test_db, option="existing")
    _album(test_db, "owned", file=True)
    _album(test_db, "wanted_no_file")  # manually monitored, no file: a recompute would unmonitor it
    out = test_db.metadata_profile_preview("ar", None)
    assert out["would_change"] == {
        "albums_to_monitor": 0, "albums_to_unmonitor": 1, "tracks_to_monitor": 0, "tracks_to_unmonitor": 1,
    }


def test_profile_change_without_apply_leaves_albums_alone_via_api(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    pid = _profile_fixture(test_db)
    r = client.put("/api/library/artists/ar/monitored", json={"monitored": True, "metadata_profile_id": pid}, headers=h)
    assert r.status_code == 200, r.text
    assert test_db.get_library_artist("ar")["metadata_profile_id"] == pid
    assert (_a(test_db, "live"), _a(test_db, "off_studio")) == (True, False)  # untouched
    r = client.put(
        "/api/library/artists/ar/monitored",
        json={"monitored": True, "metadata_profile_id": pid, "apply_monitor_to_albums": True}, headers=h,
    )
    assert (_a(test_db, "live"), _a(test_db, "off_studio")) == (False, True)


# --------------------------------------------------------------------------- 4. pending flag lifecycle

def _pending(db: Database) -> bool:
    return bool(db.get_library_artist("ar")["pending_profile_recompute"])


@pytest.mark.parametrize("how", ["track", "bulk_tracks", "album", "bulk_albums", "artist_recompute"])
def test_manual_edits_and_artist_recompute_clear_pending_flag(test_db: Database, how: str):
    _artist(test_db, option="all")
    _album(test_db, "a")
    test_db.set_pending_profile_recompute("ar", True)
    {
        "track": lambda: test_db.set_track_monitored("a-t1", False),
        "bulk_tracks": lambda: test_db.bulk_set_tracks_monitored(["a-t1"], False),
        "album": lambda: test_db.set_album_monitored("a", False),
        "bulk_albums": lambda: test_db.bulk_set_albums_monitored(["a"], False),
        "artist_recompute": lambda: test_db.bulk_edit_library_artists(["ar"], apply_monitor_to_albums=True),
    }[how]()
    assert not _pending(test_db)


def test_unrelated_track_edit_keeps_other_artists_pending(test_db: Database):
    _artist(test_db, aid="ar")
    _artist(test_db, aid="other")
    _album(test_db, "a")
    _album(test_db, "b", aid="other")
    test_db.set_pending_profile_recompute("ar", True)
    test_db.set_track_monitored("b-t1", False)
    assert _pending(test_db)


def test_finish_pending_recompute_is_one_transaction_and_runs_once(test_db: Database):
    pid = _profile_fixture(test_db)
    test_db.bulk_edit_library_artists(["ar"], metadata_profile_id=pid)
    # no pending flag: nothing happens
    assert test_db.finish_pending_profile_recompute("ar") is False
    assert _a(test_db, "live") is True

    test_db.set_pending_profile_recompute("ar", True)
    with test_db._lock:  # unknown secondary types everywhere: not ready, flag kept
        test_db.conn.execute("UPDATE library_albums SET secondary_types = NULL")
        test_db.conn.commit()
    assert test_db.finish_pending_profile_recompute("ar") is False and _pending(test_db)

    test_db.upsert_library_album({"id": "live", "artist_id": "ar", "title": "live", "album_type": "live",
                                  "secondary_types": ["live"], "mb_release_group_id": "rg-live"}, preserve_monitoring=True)
    assert test_db.finish_pending_profile_recompute("ar") is True
    assert not _pending(test_db) and _a(test_db, "live") is False and test_db.get_library_track("live-t1")["monitored"] is False
    test_db.set_album_monitored("live", True)
    assert test_db.finish_pending_profile_recompute("ar") is False  # once only
    assert _a(test_db, "live") is True


def test_finish_pending_rolls_back_flag_with_the_recompute(test_db: Database, monkeypatch):
    """If the recompute fails mid-way, neither the album changes nor the flag clear persist."""
    pid = _profile_fixture(test_db)
    test_db.bulk_edit_library_artists(["ar"], metadata_profile_id=pid)
    test_db.upsert_library_album({"id": "live", "artist_id": "ar", "title": "live", "album_type": "live",
                                  "secondary_types": ["live"], "mb_release_group_id": "rg-live"}, preserve_monitoring=True)
    test_db.set_pending_profile_recompute("ar", True)
    import sqlite3

    real = test_db.conn

    class Boom:
        def __init__(self) -> None:
            self.n = 0

        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

        def execute(self, sql: str, *a: Any) -> Any:
            if sql.lstrip().startswith("UPDATE library_tracks"):
                raise sqlite3.OperationalError("disk full")
            return real.execute(sql, *a)

    monkeypatch.setattr(test_db, "_ensure_connection", lambda: Boom())
    with pytest.raises(sqlite3.OperationalError):
        test_db.finish_pending_profile_recompute("ar")
    monkeypatch.undo()
    assert _pending(test_db) and _a(test_db, "live") is True


# --------------------------------------------------------------------------- 6. profile + monitored together

def test_artist_put_with_profile_and_monitored_applies_both(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    pid = _profile_fixture(test_db)
    r = client.put(
        "/api/library/artists/ar/monitored",
        json={"monitored": False, "metadata_profile_id": pid, "apply_monitor_to_albums": True}, headers=h,
    )
    assert r.status_code == 200, r.text
    art = test_db.get_library_artist("ar")
    assert art["monitored"] is False and art["metadata_profile_id"] == pid
    assert not any(a["monitored"] for a in test_db.list_library_albums(artist_id="ar"))

    # without apply: monitored changes and cascades (cascade_children default), profile written
    _artist(test_db, aid="b")
    _album(test_db, "ba", aid="b")
    r = client.put("/api/library/artists/b/monitored", json={"monitored": False, "metadata_profile_id": pid}, headers=h)
    assert test_db.get_library_artist("b")["monitored"] is False and test_db.get_library_artist("b")["metadata_profile_id"] == pid
    assert _a(test_db, "ba") is False
    # cascade_children=false: only the flag
    _artist(test_db, aid="c")
    _album(test_db, "ca", aid="c")
    client.put("/api/library/artists/c/monitored",
               json={"monitored": False, "cascade_children": False, "metadata_profile_id": pid}, headers=h)
    assert test_db.get_library_artist("c")["monitored"] is False and _a(test_db, "ca") is True


# --------------------------------------------------------------------------- 7. bulk edit "existing"

def test_bulk_existing_recomputes_only_artists_whose_option_changes(app_and_client, test_db, test_config, seeded_users):
    _, client = app_and_client
    h = _auth_headers(seeded_users["admin"], test_db, test_config)
    _artist(test_db, aid="same", option="existing")
    _artist(test_db, aid="chg", option="all")
    _album(test_db, "sa", aid="same")  # manually monitored, no file
    _album(test_db, "ca", aid="chg")
    r = client.post("/api/library/artists/bulk-edit",
                    json={"artist_ids": ["same", "chg"], "monitor_option": "existing"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["artists_updated"] == 2
    assert _a(test_db, "sa") is True and test_db.get_library_track("sa-t1")["monitored"] is True  # left alone
    assert _a(test_db, "ca") is False  # recomputed (no files)

    r = client.post("/api/library/artists/bulk-edit",
                    json={"all": True, "monitor_option": "existing"}, headers=h)
    assert _a(test_db, "sa") is True  # all=true path: still nothing changes option

    r = client.post("/api/library/artists/bulk-edit",
                    json={"artist_ids": ["same"], "monitor_option": "existing", "apply_monitor_to_albums": True}, headers=h)
    assert _a(test_db, "sa") is False  # explicit true recomputes everyone


# --------------------------------------------------------------------------- 8. secondary types

def test_normalize_secondary_types_helper():
    assert normalize_secondary_types([" Live ", "LIVE", "", None, "Remix", "remix"]) == ["live", "remix"]
    assert normalize_secondary_types([]) == []
    assert normalize_secondary_types(None) is None
    assert normalize_secondary_types("live") is None and normalize_secondary_types({"a": 1}) is None


def test_every_writer_stores_normalised_secondary_types(test_db: Database):
    _artist(test_db)
    test_db.upsert_library_album({"id": "x", "artist_id": "ar", "title": "x", "secondary_types": [" Live", "LIVE", "Remix"]})
    assert test_db.get_library_album("x")["secondary_types"] == ["live", "remix"]


def test_sql_twin_multi_secondary_malformed_and_non_list_json(test_db: Database):
    prof = test_db.create_metadata_profile("Live+Studio", ["album"], ["live", "studio"])
    _artist(test_db, option="all", profile=prof["id"])
    cases = {
        "studio": ("album", []),
        "live": ("album", ["live"]),
        "live_remix": ("album", ["live", "remix"]),
        "malformed_live": ("live", None),   # raw JSON below is garbage: unknown -> inferred from album_type 'live'
        "malformed_album": ("album", None),
        "scalar_json": ("compilation", None),  # raw '"live"' is valid JSON but not a list -> unknown -> inference
        "object_json": ("album", None),
    }
    for aid, (t, sec) in cases.items():
        _album(test_db, aid, album_type=t, secondary=sec)
    with test_db._lock:
        for aid, raw in {"malformed_live": "{not json", "malformed_album": "[\"live\"", "scalar_json": '"live"',
                         "object_json": '{"a": 1}'}.items():
            test_db.conn.execute("UPDATE library_albums SET secondary_types = ? WHERE id = ?", (raw, aid))
        test_db.conn.commit()
    test_db.bulk_edit_library_artists(["ar"], apply_monitor_to_albums=True)
    profile = test_db.get_metadata_profile(prof["id"])
    for aid, (t, _sec) in cases.items():
        decoded = test_db.get_library_album(aid)["secondary_types"]  # Python decode: malformed / non-list -> None
        expected = album_monitored_for_option(
            "all", artist_monitored=True, album_type=t, has_files=False, release_date=None, year=2000,
            artist_added_at="2020-01-01", profile=profile, secondary_types=decoded,
        )
        assert _a(test_db, aid) is expected, aid
    assert _a(test_db, "live_remix") is False  # remix not allowed
    assert _a(test_db, "live") is True and _a(test_db, "studio") is True
    assert _a(test_db, "malformed_live") is True  # inferred live: allowed by this profile
    assert _a(test_db, "scalar_json") is False  # inferred compilation: not allowed (python and sql agree)


# --------------------------------------------------------------------------- 10. re-link is not a new file

def test_relinking_a_known_path_to_another_track_does_not_remonitor(test_db: Database):
    _tl_artist(test_db)
    for aid, tid in (("a1", "a1-t"), ("a2", "a2-t")):
        test_db.upsert_library_album({"id": aid, "artist_id": "ar", "title": aid, "monitored": False})
        test_db.upsert_library_track(
            {"id": tid, "album_id": aid, "artist_id": "ar", "title": tid, "track_number": 1, "monitored": False}
        )
    _tl_file(test_db, "a1-t")  # first ever link of /m/a1-t.flac: monitors a1-t
    assert _t(test_db, "a1-t") is True

    # dedupe merge / re-resolve: the same path (same or a fresh id) now points at another track
    test_db.upsert_library_file({"id": "f-a1-t", "track_id": "a2-t", "file_path": "/m/a1-t.flac",
                                 "relative_path": "a1-t.flac", "codec": "FLAC", "quality_name": "FLAC", "size_bytes": 1})
    assert _t(test_db, "a2-t") is False and test_db.get_library_album("a2")["monitored"] is False
    test_db.upsert_library_files_batch([{"track_id": "a2-t", "file_path": "/m/a1-t.flac", "relative_path": "x",
                                         "codec": "FLAC", "quality_name": "FLAC", "size_bytes": 1}])
    assert _t(test_db, "a2-t") is False
    # a brand new path still counts as new
    test_db.upsert_library_file({"id": "fresh", "track_id": "a2-t", "file_path": "/m/new.flac", "relative_path": "n",
                                 "codec": "FLAC", "quality_name": "FLAC", "size_bytes": 1})
    assert _t(test_db, "a2-t") is True

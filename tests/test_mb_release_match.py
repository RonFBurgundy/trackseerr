"""Tests for MusicBrainz release matching and the mb_release_match background task."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.library import FingerprintUnavailable
from trackseerr.models import LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack
from trackseerr.release_matching import (
    choose_release,
    fill_recordings_from_fingerprints,
    link_albums_by_recording,
    match_album_releases,
    merge_title_duplicates,
)
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path):
    db_file = tmp_path / "test_mb_match.db"
    database = Database(str(db_file))
    yield database
    database.close()


def _make_release(
    rel_id: str,
    track_count: int,
    status: str = "Official",
    date: str | None = "2000-01-01",
    recordings: list[str] | None = None,
    titles: list[str] | None = None,
) -> dict:
    tracks = []
    rec_list = recordings or []
    title_list = titles or []
    for i in range(1, track_count + 1):
        rec_id = rec_list[i - 1] if i - 1 < len(rec_list) else f"rec-{rel_id}-{i}"
        title = title_list[i - 1] if i - 1 < len(title_list) else f"Track {i}"
        tracks.append({
            "position": i,
            "number": str(i),
            "title": title,
            "recording_id": rec_id,
            "length": 180000,
        })
    return {
        "id": rel_id,
        "title": f"Release {rel_id}",
        "status": status,
        "date": date,
        "country": "US",
        "track_count": track_count,
        "media": [{"position": 1, "tracks": tracks}],
    }


# =============================================================================
# choose_release unit tests
# =============================================================================


def test_choose_release_most_owned_recordings_wins():
    owned = [
        {"recording_id": "rec-1", "disc_number": 1, "track_number": 1, "title": "Track 1"},
        {"recording_id": "rec-2", "disc_number": 1, "track_number": 2, "title": "Track 2"},
    ]
    rel1 = _make_release("rel-1", 10, recordings=["rec-1", "other"])
    rel2 = _make_release("rel-2", 10, recordings=["rec-1", "rec-2"])

    chosen = choose_release([rel1, rel2], owned)
    assert chosen is not None
    assert chosen["id"] == "rel-2"


def test_choose_release_tie_closest_track_count():
    owned = [
        {"recording_id": "rec-1", "disc_number": 1, "track_number": 1, "title": "Track 1"},
        {"recording_id": "rec-2", "disc_number": 1, "track_number": 2, "title": "Track 2"},
    ]
    # Both have 2 matching recordings. Target track count is 10.
    rel1 = _make_release("rel-1", 12, recordings=["rec-1", "rec-2"])
    rel2 = _make_release("rel-2", 10, recordings=["rec-1", "rec-2"])

    chosen = choose_release([rel1, rel2], owned, catalog_track_count=10)
    assert chosen is not None
    assert chosen["id"] == "rel-2"


def test_choose_release_tie_official_status():
    owned = [
        {"recording_id": "rec-1", "disc_number": 1, "track_number": 1, "title": "Track 1"},
    ]
    rel1 = _make_release("rel-1", 10, status="Promotion", recordings=["rec-1"])
    rel2 = _make_release("rel-2", 10, status="Official", recordings=["rec-1"])

    chosen = choose_release([rel1, rel2], owned)
    assert chosen is not None
    assert chosen["id"] == "rel-2"


def test_choose_release_tie_earliest_date_and_id():
    owned = [
        {"recording_id": "rec-1", "disc_number": 1, "track_number": 1, "title": "Track 1"},
    ]
    rel1 = _make_release("rel-1", 10, date="2005-01-01", recordings=["rec-1"])
    rel2 = _make_release("rel-2", 10, date="1999-05-15", recordings=["rec-1"])

    chosen = choose_release([rel1, rel2], owned)
    assert chosen is not None
    assert chosen["id"] == "rel-2"

    # Same date -> alphabetical id
    rel_a = _make_release("rel-a", 10, date="1999-05-15", recordings=["rec-1"])
    rel_b = _make_release("rel-b", 10, date="1999-05-15", recordings=["rec-1"])
    assert choose_release([rel_b, rel_a], owned)["id"] == "rel-a"

    # Present date vs missing date -> present date wins
    rel_nodate = _make_release("rel-nodate", 10, date=None, recordings=["rec-1"])
    assert choose_release([rel_nodate, rel1], owned)["id"] == "rel-1"


def test_choose_release_no_overlap_single_release():
    owned = [
        {"recording_id": "rec-owned", "disc_number": 1, "track_number": 1, "title": "Track 1"},
    ]
    rel1 = _make_release("rel-single", 10, recordings=["rec-other"])

    chosen = choose_release([rel1], owned)
    assert chosen is not None
    assert chosen["id"] == "rel-single"


def test_choose_release_no_overlap_several_releases():
    owned = [
        {"recording_id": "rec-owned", "disc_number": 1, "track_number": 1, "title": "Track 1"},
    ]
    rel1 = _make_release("rel-1", 10, recordings=["rec-other-1"])
    rel2 = _make_release("rel-2", 10, recordings=["rec-other-2"])

    chosen = choose_release([rel1, rel2], owned)
    assert chosen is None


def test_choose_release_empty_releases():
    assert choose_release([], []) is None


# =============================================================================
# match_album_releases tests
# =============================================================================


def _seed_album_with_tracks(
    db: Database,
    tmp_path: Path,
    album_id: str,
    rg_id: str | None,
    tracks: list[dict],
    mb_release_id: str | None = None,
    artist_name: str = "Test Artist",
) -> None:
    if db.get_library_artist("artist-1") is None or artist_name != "Test Artist":
        db.upsert_library_artist(
            LibraryArtist(id="artist-1", name=artist_name, monitored=True)
        )
    db.upsert_library_album(
        LibraryAlbum(
            id=album_id,
            artist_id="artist-1",
            title=f"Album {album_id}",
            mb_release_group_id=rg_id,
            mb_release_id=mb_release_id,
            total_tracks=len(tracks),
        )
    )
    for t in tracks:
        tid = f"{album_id}-{t['num']}"
        db.upsert_library_track(
            LibraryTrack(
                id=tid,
                album_id=album_id,
                artist_id="artist-1",
                title=t["title"],
                track_number=t["num"],
                disc_number=t.get("disc", 1),
                mb_recording_id=t.get("rec_id"),
            )
        )
        if t.get("file", True):
            flac_path = tmp_path / f"{tid}.flac"
            flac_path.write_bytes(b"dummy audio")
            db.upsert_library_file(
                LibraryFile(
                    id=f"file-{tid}",
                    track_id=tid,
                    file_path=str(flac_path),
                    relative_path=flac_path.name,
                    codec="FLAC",
                )
            )


def test_match_album_releases_sets_release_and_fills_recording(db: Database, tmp_path: Path):
    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-1",
        rg_id="rg-1",
        tracks=[
            {"num": 1, "title": "Track One", "rec_id": "rec-1", "file": True},
            {"num": 2, "title": "Track Two", "rec_id": None, "file": True},
        ],
    )

    enricher = MagicMock(spec=MbidEnricherClient)
    release = _make_release(
        "rel-match-1",
        track_count=2,
        recordings=["rec-1", "rec-2"],
        titles=["Track One", "Track Two"],
    )
    enricher.get_release_group_releases.return_value = [release]

    counts = match_album_releases(db, enricher)
    assert counts == {
        "albums": 1,
        "matched": 1,
        "no_match": 0,
        "no_releases": 0,
        "recordings_filled": 1,
        "errors": 0,
    }

    album = db.get_library_album("alb-1")
    assert album is not None
    assert album["mb_release_id"] == "rel-match-1"

    t2 = db.get_library_track("alb-1-2")
    assert t2 is not None
    assert t2["mb_recording_id"] == "rec-2"


def test_match_album_releases_skips_album_with_existing_release_id(db: Database, tmp_path: Path):
    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-existing",
        rg_id="rg-1",
        tracks=[{"num": 1, "title": "Track One", "rec_id": "rec-1", "file": True}],
        mb_release_id="rel-already-set",
    )

    enricher = MagicMock(spec=MbidEnricherClient)
    counts = match_album_releases(db, enricher)

    enricher.get_release_group_releases.assert_not_called()
    assert counts["albums"] == 0
    assert counts["matched"] == 0


def test_match_album_releases_stops_when_should_stop_returns_true(db: Database, tmp_path: Path):
    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-stop-1",
        rg_id="rg-1",
        tracks=[{"num": 1, "title": "Track 1", "rec_id": "rec-1", "file": True}],
    )
    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-stop-2",
        rg_id="rg-2",
        tracks=[{"num": 1, "title": "Track 1", "rec_id": "rec-2", "file": True}],
    )

    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_release_group_releases.side_effect = [
        [_make_release("rel-1", 1, recordings=["rec-1"])],
        [_make_release("rel-2", 1, recordings=["rec-2"])],
    ]

    stop_mock = MagicMock(side_effect=[False, True])
    counts = match_album_releases(db, enricher, should_stop=stop_mock)

    assert counts["albums"] == 1
    assert counts["matched"] == 1


def test_match_album_releases_counts_errors(db: Database, tmp_path: Path, monkeypatch):
    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-err",
        rg_id="rg-1",
        tracks=[{"num": 1, "title": "Track 1", "rec_id": "rec-1", "file": True}],
    )

    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_release_group_releases.return_value = [_make_release("rel-1", 1, recordings=["rec-1"])]

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "set_album_mb_release_id", _boom)

    counts = match_album_releases(db, enricher)
    assert counts["errors"] == 1
    assert counts["albums"] == 0


def test_match_album_releases_counts_no_releases_and_no_match(db: Database, tmp_path: Path):
    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-no-rel",
        rg_id="rg-empty",
        tracks=[{"num": 1, "title": "Track 1", "rec_id": "rec-1", "file": True}],
    )
    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-no-match",
        rg_id="rg-mismatch",
        tracks=[{"num": 1, "title": "Track 1", "rec_id": "rec-owned", "file": True}],
    )

    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_release_group_releases.side_effect = [
        [],  # no releases returned
        [
            _make_release("rel-a", 1, recordings=["rec-x"]),
            _make_release("rel-b", 1, recordings=["rec-y"]),
        ],  # 2 releases, none match owned -> no_match
    ]

    counts = match_album_releases(db, enricher)
    assert counts["albums"] == 2
    assert counts["no_releases"] == 1
    assert counts["no_match"] == 1
    assert counts["matched"] == 0


# =============================================================================
# Enricher paging & caching test
# =============================================================================


def test_enricher_get_release_group_releases_paging_and_caching():
    enricher = MbidEnricherClient(base_url="https://api.brainzmash.cc")

    def _make_raw_releases(start: int, count: int) -> list[dict]:
        return [
            {
                "id": f"rel-{i}",
                "title": f"Release {i}",
                "status": "Official",
                "date": "2020-01-01",
                "country": "US",
                "track-count": 2,
                "media": [
                    {
                        "position": 1,
                        "tracks": [
                            {"position": 1, "number": "1", "title": f"T{i}-1", "recording": {"id": f"rec-{i}-1"}},
                            {"position": 2, "number": "2", "title": f"T{i}-2", "recording": {"id": f"rec-{i}-2"}},
                        ],
                    }
                ],
            }
            for i in range(start, start + count)
        ]

    page1_releases = _make_raw_releases(1, 100)
    page2_releases = _make_raw_releases(101, 50)

    resp1 = MagicMock()
    resp1.status_code = 200
    resp1.json.return_value = {"release-count": 150, "releases": page1_releases}

    resp2 = MagicMock()
    resp2.status_code = 200
    resp2.json.return_value = {"release-count": 150, "releases": page2_releases}

    enricher._request = MagicMock(side_effect=[resp1, resp2])

    releases = enricher.get_release_group_releases("rg-paged-uuid")
    assert len(releases) == 150
    assert enricher._request.call_count == 2

    # Verify first page request params
    first_call_params = enricher._request.call_args_list[0][1]["params"]
    assert first_call_params["offset"] == 0
    assert first_call_params["limit"] == 100

    # Verify second page request params
    second_call_params = enricher._request.call_args_list[1][1]["params"]
    assert second_call_params["offset"] == 100
    assert second_call_params["limit"] == 100

    # Second call should hit the cache without calling _request
    cached = enricher.get_release_group_releases("rg-paged-uuid")
    assert len(cached) == 150
    assert enricher._request.call_count == 2


def _rg_page(count_total, n, start=0):
    resp = MagicMock()
    resp.status_code = 200
    body = {"releases": [
        {"id": f"rel-{i}", "title": f"R{i}", "status": "Official", "date": "2020-01-01",
         "country": "US", "track-count": 1,
         "media": [{"position": 1, "tracks": [
             {"position": 1, "number": "1", "title": f"T{i}", "recording": {"id": f"rec-{i}"}}]}]}
        for i in range(start, start + n)
    ]}
    if count_total is not None:
        body["release-count"] = count_total
    resp.json.return_value = body
    return resp


def test_enricher_rg_releases_keeps_paging_after_short_page():
    enricher = MbidEnricherClient(base_url="https://api.brainzmash.cc")
    enricher._request = MagicMock(side_effect=[
        _rg_page(75, 28, 0), _rg_page(75, 28, 28), _rg_page(75, 19, 56),
    ])
    releases = enricher.get_release_group_releases("rg-short-pages")
    assert len(releases) == 75
    assert enricher._request.call_count == 3
    offsets = [c[1]["params"]["offset"] for c in enricher._request.call_args_list]
    assert offsets == [0, 28, 56]


def test_enricher_rg_releases_stops_on_empty_page():
    enricher = MbidEnricherClient(base_url="https://api.brainzmash.cc")
    enricher._request = MagicMock(side_effect=[_rg_page(50, 20, 0), _rg_page(50, 0)])
    releases = enricher.get_release_group_releases("rg-empty-page")
    assert len(releases) == 20
    assert enricher._request.call_count == 2


def test_enricher_rg_releases_short_page_without_count_stops():
    enricher = MbidEnricherClient(base_url="https://api.brainzmash.cc")
    enricher._request = MagicMock(side_effect=[_rg_page(None, 10, 0)])
    releases = enricher.get_release_group_releases("rg-no-count")
    assert len(releases) == 10
    assert enricher._request.call_count == 1


# =============================================================================
# API dispatch test
# =============================================================================


def test_task_manager_dispatches_mb_release_match(monkeypatch, tmp_path):
    import threading
    from fastapi.testclient import TestClient
    from trackseerr.api.app import create_app
    from trackseerr.auth import create_session_token, get_or_create_secret_key
    from trackseerr.config import Config
    import trackseerr.release_matching

    from trackseerr.api.dependencies import get_config, get_db

    database = Database(str(tmp_path / "api_test.db"))
    sec_key = get_or_create_secret_key(data_dir=str(tmp_path))
    database.upsert_user("admin-1", "admin", "admin@example.com", is_admin=True)
    token = create_session_token("admin-1", "admin", is_admin=True, secret_key=sec_key)
    database.create_session(token, "admin-1")

    cfg = Config(plex_url="http://plex.invalid:32400", plex_token="t", data_dir=str(tmp_path))
    app = create_app(db=database, config=cfg)
    app.dependency_overrides[get_db] = lambda: database
    app.dependency_overrides[get_config] = lambda: cfg
    client = TestClient(app)

    called_event = threading.Event()

    def fake_match(db, enricher, should_stop=None, on_progress=None):
        called_event.set()
        return {"albums": 1, "matched": 1, "no_match": 0, "no_releases": 0, "recordings_filled": 0, "errors": 0}

    monkeypatch.setattr(trackseerr.release_matching, "match_album_releases", fake_match)

    list_resp = client.get("/api/system/tasks", cookies={"session_token": token})
    assert list_resp.status_code == 200
    task_ids = {t["id"] for t in list_resp.json()}
    assert "mb_release_match" in task_ids
    assert "acoustid_contribute" in task_ids

    resp = client.post("/api/system/tasks/mb_release_match/run", cookies={"session_token": token})
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert called_event.wait(timeout=5.0)
    database.close()


def test_match_reports_progress(db: Database, tmp_path: Path):
    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-p1",
        rg_id="rg-p1",
        tracks=[{"num": 1, "title": "Track 1", "file": True}],
    )
    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-p2",
        rg_id="rg-p2",
        tracks=[{"num": 1, "title": "Track 1", "file": True}],
    )

    enricher = MagicMock(spec=MbidEnricherClient)
    rel1 = _make_release("rel-p1", track_count=1)
    rel2 = _make_release("rel-p2", track_count=1)
    enricher.get_release_group_releases.side_effect = lambda rgid: [rel1] if rgid == "rg-p1" else [rel2]

    calls: list[tuple[int, int | None]] = []
    counts = match_album_releases(
        db,
        enricher,
        on_progress=lambda c, t: calls.append((c, t)),
    )

    assert counts["albums"] == 2
    assert len(calls) >= 3
    assert calls[0] == (0, 2)
    assert calls[-1] == (2, 2)
    assert calls == [(0, 2), (1, 2), (2, 2)]


# =============================================================================
# fill_recordings_from_fingerprints tests
# =============================================================================


def test_fill_picks_match_inside_release_group(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.rename_verification

    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-rg",
        rg_id="rg-1",
        tracks=[{"num": 1, "title": "Song One", "rec_id": None, "file": True}],
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_release_group_releases.return_value = [
        _make_release("rel-1", track_count=1, recordings=["rec-B"], titles=["Song One"])
    ]
    monkeypatch.setattr(
        trackseerr.rename_verification,
        "verify_file",
        lambda _db, _file, _key: {
            "status": "ok",
            "matches": [
                {"recording_id": "rec-A", "score": 0.99, "title": "Song One"},
                {"recording_id": "rec-B", "score": 0.95, "title": "Song One"},
            ],
            "cached": True,
        },
    )

    counts = fill_recordings_from_fingerprints(db, enricher, "test-key")
    assert counts["filled"] == 1
    assert counts["tracks"] == 1
    track = db.get_library_track("alb-rg-1")
    assert track is not None
    assert track["mb_recording_id"] == "rec-B"


def test_fill_without_group_requires_title_and_score(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.rename_verification

    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-norg",
        rg_id=None,
        tracks=[
            {"num": 1, "title": "Exact Song", "rec_id": None, "file": True},
            {"num": 2, "title": "Expected Title", "rec_id": None, "file": True},
            {"num": 3, "title": "Low Score Song", "rec_id": None, "file": True},
        ],
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    responses = {
        "file-alb-norg-1": {
            "status": "ok",
            "matches": [{"recording_id": "rec-ok", "score": 0.95, "title": "Exact Song"}],
            "cached": True,
        },
        "file-alb-norg-2": {
            "status": "ok",
            "matches": [{"recording_id": "rec-diff", "score": 0.98, "title": "Completely Different"}],
            "cached": True,
        },
        "file-alb-norg-3": {
            "status": "ok",
            "matches": [{"recording_id": "rec-low", "score": 0.80, "title": "Low Score Song"}],
            "cached": True,
        },
    }
    monkeypatch.setattr(
        trackseerr.rename_verification,
        "verify_file",
        lambda _db, file_row, _key: responses[file_row["id"]],
    )

    counts = fill_recordings_from_fingerprints(db, enricher, "test-key")
    assert counts["tracks"] == 3
    assert counts["filled"] == 1
    assert counts["no_match"] == 2
    assert db.get_library_track("alb-norg-1")["mb_recording_id"] == "rec-ok"
    assert db.get_library_track("alb-norg-2")["mb_recording_id"] is None
    assert db.get_library_track("alb-norg-3")["mb_recording_id"] is None


def test_fill_skips_recording_used_in_same_album(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.rename_verification

    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-dup",
        rg_id=None,
        tracks=[
            {"num": 1, "title": "Same Song", "rec_id": "rec-already", "file": True},
            {"num": 2, "title": "Same Song", "rec_id": None, "file": True},
        ],
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    monkeypatch.setattr(
        trackseerr.rename_verification,
        "verify_file",
        lambda _db, _file, _key: {
            "status": "ok",
            "matches": [{"recording_id": "rec-already", "score": 0.99, "title": "Same Song"}],
            "cached": True,
        },
    )

    counts = fill_recordings_from_fingerprints(db, enricher, "test-key")
    assert counts["tracks"] == 1
    assert counts["filled"] == 0
    assert counts["no_match"] == 1
    assert db.get_library_track("alb-dup-2")["mb_recording_id"] is None


def test_fill_no_fingerprint_counts(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.rename_verification

    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-nofp",
        rg_id=None,
        tracks=[{"num": 1, "title": "Mystery Track", "rec_id": None, "file": True}],
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    monkeypatch.setattr(
        trackseerr.rename_verification,
        "verify_file",
        lambda _db, _file, _key: {"status": "no_result"},
    )

    counts = fill_recordings_from_fingerprints(db, enricher, "test-key")
    assert counts["tracks"] == 1
    assert counts["no_fingerprint"] == 1
    assert counts["filled"] == 0


def test_fill_stops_when_fingerprinting_unavailable(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.rename_verification

    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-unavail",
        rg_id=None,
        tracks=[{"num": 1, "title": "Track 1", "rec_id": None, "file": True}],
    )
    enricher = MagicMock(spec=MbidEnricherClient)

    def _raise_unavail(_db, _file, _key):
        raise FingerprintUnavailable("fpcalc missing")

    monkeypatch.setattr(trackseerr.rename_verification, "verify_file", _raise_unavail)

    counts = fill_recordings_from_fingerprints(db, enricher, "test-key")
    assert counts["filled"] == 0


def test_fill_accepts_small_wording_difference(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.rename_verification

    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-trex",
        rg_id=None,
        artist_name="T. Rex",
        tracks=[{"num": 1, "title": "King Of The Mountain Cometh", "rec_id": None, "file": True}],
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    monkeypatch.setattr(
        trackseerr.rename_verification,
        "verify_file",
        lambda _db, _file, _key: {
            "status": "ok",
            "matches": [
                {
                    "recording_id": "rec-king",
                    "score": 1.00,
                    "title": "The King of This Mountain Cometh",
                    "artist": "T.Rex",
                }
            ],
            "cached": True,
        },
    )

    counts = fill_recordings_from_fingerprints(db, enricher, "test-key")
    assert counts["filled"] == 1
    assert db.get_library_track("alb-trex-1")["mb_recording_id"] == "rec-king"


def test_fill_accepts_year_mix_label(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.rename_verification

    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-stones",
        rg_id=None,
        artist_name="The Rolling Stones",
        tracks=[
            {"num": 1, "title": "Sister Morphine (2009 Mix)", "rec_id": None, "file": True},
            {"num": 2, "title": "Bitch (2009 Mix)", "rec_id": None, "file": True},
        ],
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    responses = {
        "file-alb-stones-1": {
            "status": "ok",
            "matches": [{"recording_id": "rec-sister", "score": 0.98, "title": "Sister Morphine"}],
            "cached": True,
        },
        "file-alb-stones-2": {
            "status": "ok",
            "matches": [{"recording_id": "rec-bitch", "score": 0.97, "title": "Bitch"}],
            "cached": True,
        },
    }
    monkeypatch.setattr(
        trackseerr.rename_verification,
        "verify_file",
        lambda _db, file_row, _key: responses[file_row["id"]],
    )

    counts = fill_recordings_from_fingerprints(db, enricher, "test-key")
    assert counts["filled"] == 2
    assert db.get_library_track("alb-stones-1")["mb_recording_id"] == "rec-sister"
    assert db.get_library_track("alb-stones-2")["mb_recording_id"] == "rec-bitch"


def test_fill_accepts_spelling_with_lower_score(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.rename_verification

    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-tonite",
        rg_id=None,
        artist_name="April Wine",
        tracks=[
            {
                "num": 1,
                "title": "Tonite Is a Wonderful Time to Fall in Love",
                "rec_id": None,
                "file": True,
            }
        ],
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    monkeypatch.setattr(
        trackseerr.rename_verification,
        "verify_file",
        lambda _db, _file, _key: {
            "status": "ok",
            "matches": [
                {
                    "recording_id": "rec-tonite",
                    "score": 0.87,
                    "title": "Tonight Is a Wonderful Time to Fall in Love",
                    "artist": "April Wine",
                }
            ],
            "cached": True,
        },
    )

    counts = fill_recordings_from_fingerprints(db, enricher, "test-key")
    assert counts["filled"] == 1
    assert db.get_library_track("alb-tonite-1")["mb_recording_id"] == "rec-tonite"


def test_fill_rejects_dub_edit(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.rename_verification

    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-dub",
        rg_id=None,
        artist_name="The RAH Band",
        tracks=[
            {
                "num": 1,
                "title": "Spend The Night (feat. Dannielle Gaha) - H Man Dub Edit",
                "rec_id": None,
                "file": True,
            }
        ],
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    monkeypatch.setattr(
        trackseerr.rename_verification,
        "verify_file",
        lambda _db, _file, _key: {
            "status": "ok",
            "matches": [
                {
                    "recording_id": "rec-spend",
                    "score": 0.97,
                    "title": "Spend the Night",
                    "artist": "The RAH Band",
                }
            ],
            "cached": True,
        },
    )

    counts = fill_recordings_from_fingerprints(db, enricher, "test-key")
    assert counts["filled"] == 0
    assert counts["no_match"] == 1
    assert db.get_library_track("alb-dub-1")["mb_recording_id"] is None


def test_fill_rejects_close_title_wrong_artist(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.rename_verification

    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-wrong-artist",
        rg_id=None,
        artist_name="T. Rex",
        tracks=[{"num": 1, "title": "King Of The Mountain Cometh", "rec_id": None, "file": True}],
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    monkeypatch.setattr(
        trackseerr.rename_verification,
        "verify_file",
        lambda _db, _file, _key: {
            "status": "ok",
            "matches": [
                {
                    "recording_id": "rec-wrong",
                    "score": 0.99,
                    "title": "The King of This Mountain Cometh",
                    "artist": "Other Artist",
                }
            ],
            "cached": True,
        },
    )

    counts = fill_recordings_from_fingerprints(db, enricher, "test-key")
    assert counts["filled"] == 0
    assert counts["no_match"] == 1
    assert db.get_library_track("alb-wrong-artist-1")["mb_recording_id"] is None


def test_fill_rejects_close_title_without_artist(db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import trackseerr.rename_verification

    _seed_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-no-artist",
        rg_id=None,
        artist_name="T. Rex",
        tracks=[{"num": 1, "title": "King Of The Mountain Cometh", "rec_id": None, "file": True}],
    )
    enricher = MagicMock(spec=MbidEnricherClient)
    monkeypatch.setattr(
        trackseerr.rename_verification,
        "verify_file",
        lambda _db, _file, _key: {
            "status": "ok",
            "matches": [
                {
                    "recording_id": "rec-no-artist",
                    "score": 0.99,
                    "title": "The King of This Mountain Cometh",
                }
            ],
            "cached": True,
        },
    )

    counts = fill_recordings_from_fingerprints(db, enricher, "test-key")
    assert counts["filled"] == 0
    assert counts["no_match"] == 1
    assert db.get_library_track("alb-no-artist-1")["mb_recording_id"] is None


# =============================================================================
# link_albums_by_recording single-album merge tests
# =============================================================================


def _seed_named_album_with_tracks(
    db: Database,
    tmp_path: Path,
    album_id: str,
    title: str,
    rg_id: str | None,
    tracks: list[dict],
) -> None:
    if db.get_library_artist("artist-1") is None:
        db.upsert_library_artist(LibraryArtist(id="artist-1", name="The Beatles", monitored=False))
    db.upsert_library_album(
        LibraryAlbum(
            id=album_id,
            artist_id="artist-1",
            title=title,
            mb_release_group_id=rg_id,
            total_tracks=len(tracks),
            monitored=False,
        )
    )
    for t in tracks:
        tid = f"{album_id}-{t['num']}"
        db.upsert_library_track(
            LibraryTrack(
                id=tid,
                album_id=album_id,
                artist_id="artist-1",
                title=t["title"],
                track_number=t["num"],
                disc_number=t.get("disc", 1),
                mb_recording_id=t.get("rec_id"),
                monitored=False,
            )
        )
        if t.get("file", True):
            flac_path = tmp_path / f"{tid}.flac"
            flac_path.write_bytes(b"dummy audio")
            db.upsert_library_file(
                LibraryFile(
                    id=f"file-{tid}",
                    track_id=tid,
                    file_path=str(flac_path),
                    relative_path=flac_path.name,
                    codec="FLAC",
                )
            )


def test_link_rg_taken_merges_single_into_holder(db: Database, tmp_path: Path):
    _seed_named_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-holder",
        title="Abbey Road",
        rg_id="rg-abbey",
        tracks=[{"num": 2, "title": "Something", "rec_id": "rec-something", "file": False}],
    )
    _seed_named_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-single",
        title="Abbey Road (2009 Remaster)",
        rg_id=None,
        tracks=[{"num": 1, "title": "Something", "rec_id": "rec-something", "file": True}],
    )

    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_recording_release_groups.return_value = [
        {
            "id": "rg-abbey",
            "title": "Abbey Road",
            "primary_type": "Album",
            "secondary_types": [],
            "first_release_date": "1969-09-26",
        }
    ]

    counts = link_albums_by_recording(db, enricher)
    assert counts["merged"] == 1
    assert counts["rg_taken"] == 0
    assert db.get_library_album("alb-single") is None
    holder_track = db.get_library_track("alb-holder-2")
    assert holder_track is not None and holder_track["has_file"] is True


def test_link_no_title_match_merges_when_group_held(db: Database, tmp_path: Path):
    _seed_named_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-holder",
        title="Abbey Road",
        rg_id="rg-abbey",
        tracks=[{"num": 2, "title": "Something", "rec_id": "rec-something", "file": False}],
    )
    _seed_named_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-single",
        title="Something - Remastered 2009",
        rg_id=None,
        tracks=[{"num": 1, "title": "Something - Remastered 2009", "rec_id": "rec-something", "file": True}],
    )

    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_recording_release_groups.return_value = [
        {
            "id": "rg-abbey",
            "title": "Abbey Road",
            "primary_type": "Album",
            "secondary_types": [],
            "first_release_date": "1969-09-26",
        }
    ]

    counts = link_albums_by_recording(db, enricher)
    assert counts["merged"] == 1
    assert counts["no_title_match"] == 0
    assert db.get_library_album("alb-single") is None
    holder_track = db.get_library_track("alb-holder-2")
    assert holder_track is not None and holder_track["has_file"] is True


def test_link_does_not_merge_large_album(db: Database, tmp_path: Path):
    _seed_named_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-holder",
        title="Abbey Road",
        rg_id="rg-abbey",
        tracks=[{"num": 2, "title": "Something", "rec_id": "rec-something", "file": False}],
    )
    _seed_named_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-large",
        title="Abbey Road Sessions",
        rg_id=None,
        tracks=[
            {"num": 1, "title": "Come Together", "rec_id": "rec-ct", "file": True},
            {"num": 2, "title": "Something", "rec_id": "rec-something", "file": True},
            {"num": 3, "title": "Maxwell's Silver Hammer", "rec_id": "rec-msh", "file": True},
            {"num": 4, "title": "Oh! Darling", "rec_id": "rec-od", "file": True},
        ],
    )

    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_recording_release_groups.return_value = [
        {
            "id": "rg-abbey",
            "title": "Abbey Road",
            "primary_type": "Album",
            "secondary_types": [],
            "first_release_date": "1969-09-26",
        }
    ]

    counts = link_albums_by_recording(db, enricher)
    assert counts["merged"] == 0
    assert counts["rg_taken"] == 1
    assert db.get_library_album("alb-large") is not None


def test_link_ignores_year_suffix_in_album_title(db: Database, tmp_path: Path):
    _seed_named_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-end",
        title="The E•N•D (2009)",
        rg_id=None,
        tracks=[{"num": 1, "title": "Boom Boom Pow", "rec_id": "rec-bbp", "file": True}],
    )

    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_recording_release_groups.return_value = [
        {
            "id": "rg-end",
            "title": "The E.N.D.",
            "primary_type": "Album",
            "secondary_types": [],
            "first_release_date": "2009-06-03",
        }
    ]

    counts = link_albums_by_recording(db, enricher)
    assert counts["linked"] == 1
    assert counts["linked_by_agreement"] == 0
    album = db.get_library_album("alb-end")
    assert album is not None and album["mb_release_group_id"] == "rg-end"


def test_link_by_agreement_when_titles_differ(db: Database, tmp_path: Path):
    _seed_named_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-agree",
        title="Custom Box Set Name",
        rg_id=None,
        tracks=[
            {"num": 1, "title": "Come Together", "rec_id": "rec-1", "file": True},
            {"num": 2, "title": "Something", "rec_id": "rec-2", "file": True},
        ],
    )

    shared_rg = {
        "id": "rg-abbey",
        "title": "Abbey Road",
        "primary_type": "Album",
        "secondary_types": [],
        "first_release_date": "1969-09-26",
    }
    other_rg = {
        "id": "rg-comp",
        "title": "1967-1970",
        "primary_type": "Album",
        "secondary_types": ["Compilation"],
        "first_release_date": "1973-04-02",
    }
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_recording_release_groups.side_effect = lambda rec_id: (
        [shared_rg, other_rg] if rec_id == "rec-1" else [shared_rg]
    )

    counts = link_albums_by_recording(db, enricher)
    assert counts["linked"] == 0
    assert counts["linked_by_agreement"] == 1
    album = db.get_library_album("alb-agree")
    assert album is not None and album["mb_release_group_id"] == "rg-abbey"


def test_no_agreement_when_groups_differ(db: Database, tmp_path: Path):
    _seed_named_album_with_tracks(
        db,
        tmp_path,
        album_id="alb-disagree",
        title="Unmatched Mixtape",
        rg_id=None,
        tracks=[
            {"num": 1, "title": "Song A", "rec_id": "rec-a", "file": True},
            {"num": 2, "title": "Song B", "rec_id": "rec-b", "file": True},
        ],
    )

    rg_a = {
        "id": "rg-a",
        "title": "Album A",
        "primary_type": "Album",
        "secondary_types": [],
        "first_release_date": "1990-01-01",
    }
    rg_b = {
        "id": "rg-b",
        "title": "Album B",
        "primary_type": "Album",
        "secondary_types": [],
        "first_release_date": "1991-01-01",
    }
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_recording_release_groups.side_effect = lambda rec_id: (
        [rg_a] if rec_id == "rec-a" else [rg_b]
    )

    counts = link_albums_by_recording(db, enricher)
    assert counts["linked"] == 0
    assert counts["linked_by_agreement"] == 0
    assert counts["no_title_match"] == 1
    album = db.get_library_album("alb-disagree")
    assert album is not None and album["mb_release_group_id"] is None







# =============================================================================
# Title duplicate merging
# =============================================================================


def _rg_enricher(rg_id: str, title: str) -> MagicMock:
    enricher = MagicMock(spec=MbidEnricherClient)
    enricher.get_recording_release_groups.return_value = [
        {
            "id": rg_id,
            "title": title,
            "primary_type": "Album",
            "secondary_types": [],
            "first_release_date": "1971-12-17",
        }
    ]
    return enricher


def _numbered(count: int, *, file: bool, rec: bool = True) -> list[dict]:
    return [
        {"num": n, "title": f"Song {n}", "rec_id": f"rec-{n}" if rec else None, "file": file}
        for n in range(1, count + 1)
    ]


def test_title_duplicate_merges_large_album(db: Database, tmp_path: Path):
    _seed_named_album_with_tracks(
        db, tmp_path, "alb-holder", "Hunky Dory", "rg-x", _numbered(11, file=False)
    )
    _seed_named_album_with_tracks(
        db, tmp_path, "alb-src", "Hunky Dory  (2015 Remaster)", None, _numbered(9, file=True)
    )

    counts = merge_title_duplicates(db)

    assert counts["merged"] == 1
    assert db.get_library_album("alb-src") is None
    holder_track = db.get_library_track("alb-holder-3")
    assert holder_track is not None and holder_track["has_file"] is True


def test_title_duplicate_merges_single_without_recording(db: Database, tmp_path: Path):
    _seed_named_album_with_tracks(
        db, tmp_path, "alb-holder", "Violator", "rg-y", _numbered(9, file=False, rec=False)
    )
    _seed_named_album_with_tracks(
        db,
        tmp_path,
        "alb-src",
        "Violator (2006 Remaster)",
        None,
        [{"num": 1, "title": "Song 1", "rec_id": None, "file": True}],
    )

    counts = merge_title_duplicates(db)

    assert counts["merged"] == 1
    assert db.get_library_album("alb-src") is None


def test_title_duplicate_ambiguous_skips(db: Database, tmp_path: Path):
    _seed_named_album_with_tracks(db, tmp_path, "alb-h1", "Greatest Hits", "rg-1", _numbered(3, file=False))
    _seed_named_album_with_tracks(db, tmp_path, "alb-h2", "Greatest Hits", "rg-2", _numbered(3, file=False))
    _seed_named_album_with_tracks(
        db, tmp_path, "alb-src", "Greatest Hits (Remastered)", None, _numbered(3, file=True)
    )

    counts = merge_title_duplicates(db)

    assert counts["ambiguous"] == 1
    assert counts["merged"] == 0
    assert db.get_library_album("alb-src") is not None


def test_title_duplicate_conflict_skips(db: Database, tmp_path: Path):
    _seed_named_album_with_tracks(db, tmp_path, "alb-holder", "Violator", "rg-y", _numbered(2, file=True))
    _seed_named_album_with_tracks(
        db, tmp_path, "alb-src", "Violator (2006 Remaster)", None, _numbered(2, file=True)
    )

    counts = merge_title_duplicates(db)

    assert counts["merge_conflict"] == 1
    assert counts["merged"] == 0
    assert db.get_library_album("alb-src") is not None
    assert db.get_library_track("alb-src-1") is not None


def test_link_rg_taken_merges_large_album_with_matching_title(db: Database, tmp_path: Path):
    _seed_named_album_with_tracks(
        db, tmp_path, "alb-holder", "Hunky Dory", "rg-x", _numbered(6, file=False)
    )
    _seed_named_album_with_tracks(
        db, tmp_path, "alb-src", "Hunky Dory (2015 Remaster)", None, _numbered(5, file=True)
    )

    counts = link_albums_by_recording(db, _rg_enricher("rg-x", "Some Other Title"))

    assert counts["merged"] == 1
    assert counts["rg_taken"] == 0
    assert db.get_library_album("alb-src") is None


def test_link_rg_taken_keeps_large_album_with_other_title(db: Database, tmp_path: Path):
    _seed_named_album_with_tracks(
        db, tmp_path, "alb-holder", "Hunky Dory", "rg-x", _numbered(6, file=False)
    )
    _seed_named_album_with_tracks(
        db, tmp_path, "alb-src", "Live in Paris", None, _numbered(5, file=True)
    )

    counts = link_albums_by_recording(db, _rg_enricher("rg-x", "Hunky Dory"))

    assert counts["merged"] == 0
    assert counts["rg_taken"] == 1
    assert db.get_library_album("alb-src") is not None

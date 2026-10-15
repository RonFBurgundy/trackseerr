"""Tests for MusicBrainz release matching and the mb_release_match background task."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.models import LibraryAlbum, LibraryArtist, LibraryFile, LibraryTrack
from trackseerr.release_matching import choose_release, match_album_releases
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
) -> None:
    if db.get_library_artist("artist-1") is None:
        db.upsert_library_artist(
            LibraryArtist(id="artist-1", name="Test Artist", monitored=True)
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

    def fake_match(db, enricher, should_stop=None):
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


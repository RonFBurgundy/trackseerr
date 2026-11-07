"""Tests verifying that exclusions are enforced across request submission, list monitoring,
acquisition coordinator, pending releases, backlog sweep, and library upserts.
"""

from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trackseerr.acquisition_coordinator import AcquisitionCoordinator, acquisition_coordinator
from trackseerr.backlog_worker import WantedBacklogWorker
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.item_history import TRIGGER_WANTED, GrabTrigger
from trackseerr.list_monitoring import ListItem, apply_list_item
from trackseerr.models import (
    AcquisitionSearchResult,
    DownloadClientConfig,
    MusicRequest,
    RequestStatus,
    UserPermission,
)
from trackseerr import pending_worker
from trackseerr.request_submission import (
    RequestRejected,
    submit_batch_requests,
    submit_track_request,
)
from trackseerr.storage import Database


@pytest.fixture
def db(tmp_path: Path) -> Generator[Database, None, None]:
    database = Database(str(tmp_path / "test.db"))
    database.upsert_user("admin-1", "admin", "a@example.com", is_admin=True)
    database.upsert_user("user-alice", "alice", "alice@example.com", is_admin=False)
    database.update_account_settings({"default_quota_tracks": 100, "default_quota_albums": 100})
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def alice(db: Database) -> dict:
    return db.get_user("user-alice")


@pytest.fixture
def admin(db: Database) -> dict:
    return db.get_user("admin-1")


def _cand(title: str = "Test Release", did: str = "did-1", protocol: str = "torrent") -> AcquisitionSearchResult:
    return AcquisitionSearchResult(
        download_id=did,
        title=title,
        artist="Radiohead",
        download_url="http://example.com/dl",
        size_bytes=1000,
        source="TestIdx",
        protocol=protocol,
        seeders=10,
    )


# 1. User track request refused when track excluded
def test_user_track_request_refused_when_track_excluded(db: Database, alice: dict) -> None:
    db.add_exclusion(scope="track", artist_name="Radiohead", title="Creep")

    with pytest.raises(RequestRejected) as exc_info:
        submit_track_request(db, None, alice, title="Creep", artist="Radiohead")

    assert exc_info.value.code == "excluded"
    assert exc_info.value.status_code == 409
    assert "Creep" in exc_info.value.detail


# 2. User request refused when artist excluded (album and track)
def test_user_request_refused_when_artist_excluded(db: Database, alice: dict) -> None:
    db.add_exclusion(scope="artist", artist_name="Radiohead")

    # Track request
    with pytest.raises(RequestRejected) as exc_track:
        submit_track_request(db, None, alice, title="Karma Police", artist="Radiohead", item_type="track")
    assert exc_track.value.code == "excluded"
    assert exc_track.value.status_code == 409
    assert "Radiohead" in exc_track.value.detail

    # Album request
    with pytest.raises(RequestRejected) as exc_album:
        submit_track_request(db, None, alice, title="OK Computer", artist="Radiohead", item_type="album")
    assert exc_album.value.code == "excluded"
    assert exc_album.value.status_code == 409
    assert "Radiohead" in exc_album.value.detail


# 3. Batch skips excluded items and keeps others; batch all excluded raises
def test_batch_skips_excluded_items_and_keeps_others(db: Database, alice: dict) -> None:
    db.add_exclusion(scope="track", artist_name="Radiohead", title="Creep")

    items = [
        {"title": "Creep", "artist": "Radiohead", "item_type": "track"},
        {"title": "Airbag", "artist": "Radiohead", "item_type": "track"},
    ]
    submissions = submit_batch_requests(db, None, alice, items)
    assert len(submissions) == 1
    assert submissions[0].request["title"] == "Airbag"


def test_batch_all_excluded_raises(db: Database, alice: dict) -> None:
    db.add_exclusion(scope="track", artist_name="Radiohead", title="Creep")
    db.add_exclusion(scope="track", artist_name="Radiohead", title="Karma Police")

    items = [
        {"title": "Creep", "artist": "Radiohead", "item_type": "track"},
        {"title": "Karma Police", "artist": "Radiohead", "item_type": "track"},
    ]
    with pytest.raises(RequestRejected) as exc_info:
        submit_batch_requests(db, None, alice, items)

    assert exc_info.value.code == "excluded"
    assert exc_info.value.status_code == 409
    assert "Creep" in exc_info.value.detail


# 4. Playlist track mode skips excluded song (applied with note, no request row)
def test_playlist_track_mode_skips_excluded_song(db: Database, alice: dict) -> None:
    db.add_exclusion(scope="track", artist_name="Radiohead", title="Karma Police")

    item = ListItem(artist_name="Radiohead", track_title="Karma Police", album_title="OK Computer", kind="track")
    res = apply_list_item(db, None, item, mode="track", requested_by=alice)

    assert res.status == "applied"
    assert res.applied_level == "track"
    assert res.error is not None
    assert "Karma Police" in res.error
    assert len(db.list_requests()) == 0


# 5. List artist mode skips excluded artist and album mode skips excluded album
def test_list_artist_mode_skips_excluded_artist(db: Database) -> None:
    art_mbid = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
    db.add_exclusion(scope="artist", artist_name="Radiohead", mbid=art_mbid)

    enr = MagicMock(spec=MbidEnricherClient)
    enr.lookup_artist_mbid.return_value = art_mbid
    enr.get_artist_details.return_value = {"id": art_mbid}

    item = ListItem(kind="artist", artist_name="Radiohead", mbid=art_mbid, artist_mbid=art_mbid)
    res = apply_list_item(db, None, item, mode="artist", enricher=enr)

    assert res.status == "skipped"
    assert "Radiohead" in (res.error or "")
    assert db.list_library_artists() == []


def test_list_album_mode_skips_excluded_album(db: Database) -> None:
    art_mbid = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
    alb_mbid = "bbbbbbbb-0000-0000-0000-00000000000b"
    db.add_exclusion(scope="album", artist_name="Radiohead", title="OK Computer", mbid=alb_mbid)

    enr = MagicMock(spec=MbidEnricherClient)
    enr.lookup_album_mbids.return_value = {"mb_release_group_id": alb_mbid, "mb_artist_id": art_mbid}
    enr.lookup_artist_mbid.return_value = art_mbid

    item = ListItem(kind="album", artist_name="Radiohead", album_title="OK Computer", mbid=alb_mbid, artist_mbid=art_mbid)
    res = apply_list_item(db, None, item, mode="album", enricher=enr)

    assert res.status == "skipped"
    assert "OK Computer" in (res.error or "")
    assert db.list_library_albums() == []


# 6. Search and grab returns excluded without searching
def test_search_and_grab_returns_excluded_without_searching(db: Database) -> None:
    db.add_exclusion(scope="track", artist_name="Radiohead", title="Creep")

    coord = AcquisitionCoordinator()
    with patch.object(coord, "search_all_indexers") as mock_search, patch(
        "trackseerr.acquisition_coordinator.get_acquisition_driver"
    ) as mock_driver:
        res = coord.search_and_grab(
            artist="Radiohead",
            title="Creep",
            album="Pablo Honey",
            item_type="track",
            db=db,
            trigger=GrabTrigger(TRIGGER_WANTED),
        )

    assert res.get("success") is False
    assert res.get("excluded") is True
    assert 'Excluded track "Creep" by Radiohead' in res.get("message", "")
    mock_search.assert_not_called()
    mock_driver.assert_not_called()


# 7. Sweep counts excluded and skips search, event message contains excluded
def test_sweep_counts_excluded_and_skips_search(db: Database, alice: dict) -> None:
    db.create_indexer(
        {
            "id": "idx-sw",
            "name": "Prowlarr",
            "indexer_type": "torznab",
            "host_url": "http://127.0.0.1:9696/1",
            "api_key": "key",
            "enabled": True,
        }
    )
    db.create_download_client(
        {
            "id": "c-sw",
            "name": "qBit",
            "driver_type": "qbittorrent",
            "host_url": "http://127.0.0.1:8080",
            "enabled": True,
        }
    )

    db.add_exclusion(scope="track", artist_name="Artist A", title="Track A")

    req_a = MusicRequest(
        id="req-sw-a",
        user_id=alice["id"],
        item_type="track",
        title="Track A",
        artist="Artist A",
        status=RequestStatus.PENDING,
    )
    req_b = MusicRequest(
        id="req-sw-b",
        user_id=alice["id"],
        item_type="track",
        title="Track B",
        artist="Artist B",
        status=RequestStatus.PENDING,
    )
    db.create_request(req_a)
    db.create_request(req_b)

    worker = WantedBacklogWorker()
    worker.pace_delay = 0.0

    mock_grab = MagicMock(return_value={"success": True, "download_id": "dl-1"})
    with patch.object(acquisition_coordinator, "search_and_grab", mock_grab):
        stats = worker.poll_once(db)

    assert stats["items_excluded"] == 1
    assert mock_grab.call_count == 1
    assert mock_grab.call_args[1]["title"] == "Track B"

    events = db.conn.execute("SELECT * FROM system_events WHERE event_type = 'backlog_sweep'").fetchall()
    assert len(events) >= 1
    assert "1 excluded" in events[-1]["message"]


# 8. Pending release dropped when excluded
def test_pending_release_dropped_when_excluded(db: Database) -> None:
    row = db.upsert_pending_release(
        {
            "item_key": "track:Radiohead|Creep",
            "title": "Radiohead - Creep [FLAC]",
            "artist_name": "Radiohead",
            "album": "Pablo Honey",
            "item_type": "track",
            "protocol": "torrent",
            "quality": "FLAC",
            "format_score": 100,
            "payload": {
                "candidate": {"title": "Radiohead - Creep [FLAC]", "protocol": "torrent", "artist": "Radiohead"},
                "item_title": "Creep",
            },
            "rank": [100],
            "added_at": "2026-11-01T00:00:00Z",
            "release_at": "2026-11-01T00:10:00Z",
        }
    )
    assert len(db.list_pending_releases()) == 1

    # Add exclusion after it was parked
    db.add_exclusion(scope="track", artist_name="Radiohead", title="Creep")

    now = datetime(2026, 11, 1, 0, 15, tzinfo=timezone.utc)
    stats = pending_worker.release_due(db, now=now)
    assert stats["dropped"] == 1
    assert stats["released"] == 0
    assert len(db.list_pending_releases()) == 0


# 9. Upsert album/artist forces unmonitored when excluded
def test_upsert_album_forces_unmonitored_when_excluded(db: Database) -> None:
    art = db.upsert_library_artist({"id": "art-rad", "name": "Radiohead", "monitored": True})
    assert art["monitored"] is True

    db.add_exclusion(scope="album", artist_name="Radiohead", title="OK Computer")

    # Inserting new excluded album forces unmonitored
    alb = db.upsert_library_album({"id": "alb-okc", "artist_id": "art-rad", "title": "OK Computer", "monitored": True})
    assert alb["monitored"] is False

    # Updating with preserve_monitoring=True still forces unmonitored
    alb_updated = db.upsert_library_album(
        {"id": "alb-okc", "artist_id": "art-rad", "title": "OK Computer", "monitored": True},
        preserve_monitoring=True,
    )
    assert alb_updated["monitored"] is False

    # Non-excluded album monitors normally
    alb_bends = db.upsert_library_album(
        {"id": "alb-bends", "artist_id": "art-rad", "title": "The Bends", "monitored": True}
    )
    assert alb_bends["monitored"] is True


def test_upsert_artist_forces_unmonitored_when_excluded(db: Database) -> None:
    db.add_exclusion(scope="artist", artist_name="Radiohead")

    art = db.upsert_library_artist({"id": "art-rad", "name": "Radiohead", "monitored": True})
    assert art["monitored"] is False

    art_updated = db.upsert_library_artist(
        {"id": "art-rad", "name": "Radiohead", "monitored": True},
        preserve_monitoring=True,
    )
    assert art_updated["monitored"] is False

    # Non-excluded artist monitors normally
    art_other = db.upsert_library_artist({"id": "art-port", "name": "Portishead", "monitored": True})
    assert art_other["monitored"] is True


# 10. Non-excluded paths unchanged
def test_non_excluded_paths_unchanged(db: Database, alice: dict) -> None:
    db.add_exclusion(scope="track", artist_name="Radiohead", title="Creep")

    sub = submit_track_request(db, None, alice, title="Karma Police", artist="Radiohead")
    assert sub.request["title"] == "Karma Police"
    reqs = db.list_requests()
    assert len(reqs) == 1
    assert reqs[0]["title"] == "Karma Police"


# 11. Grab candidate not blocked (manual grab path works for excluded track)
def test_grab_candidate_not_blocked(db: Database) -> None:
    db.add_exclusion(scope="track", artist_name="Radiohead", title="Creep")

    db.create_download_client(
        DownloadClientConfig(
            id="c-qbit",
            name="qBit",
            driver_type="qbittorrent",
            host_url="http://127.0.0.1:8080",
            enabled=True,
        )
    )

    coord = AcquisitionCoordinator()
    candidate = _cand("Radiohead - Creep [FLAC]")

    mock_driver = MagicMock()
    mock_driver.download.return_value = "hash-1234567890"

    with patch("trackseerr.acquisition_coordinator.get_acquisition_driver", return_value=mock_driver):
        res = coord.grab_candidate(
            db,
            candidate,
            parsed_quality="FLAC",
            score=100,
            artist="Radiohead",
            album="Pablo Honey",
            item_type="track",
            request_id=None,
            track_id=None,
            album_id=None,
            trigger=GrabTrigger("manual"),
        )

    assert res.get("success") is True
    assert res.get("excluded") is None
    mock_driver.download.assert_called_once()

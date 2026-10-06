"""Pending-release hardening: atomic claim, canonical keys, re-validation, TTL, retry backoff, profile edits."""

from datetime import datetime, timedelta, timezone
import logging
import threading
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.item_history import GrabTrigger
from plex_playlist_sync import delay_gate, pending_worker
from plex_playlist_sync.acquisition_coordinator import AcquisitionCoordinator
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import AcquisitionSearchResult, MusicRequest, RequestStatus
from plex_playlist_sync.storage import Database

HQ = "profile-high-quality"
HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SECRET_URL = "http://nzb.example/get?apikey=SUPERSECRETKEY123"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: int) -> None:
        self.now = self.now + timedelta(minutes=minutes)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(delay_gate, "utcnow", c)
    return c


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def clients(db):
    db.create_download_client(
        {"id": "c-qbit", "name": "qBit", "driver_type": "qbittorrent", "host_url": "http://q:8080", "enabled": True}
    )


def cand(title="Nirvana - Nevermind [FLAC]", did="id-1"):
    return AcquisitionSearchResult(
        download_id=did, title=title, artist="Nirvana", album="Nevermind", size_bytes=300_000_000,
        magnet_url=f"magnet:?xt=urn:btih:{HASH}", download_url=SECRET_URL, source="torznab", protocol="torrent", seeders=10,
    )


def set_default(db, torrent=60):
    d = db.list_delay_profiles()[-1]
    return db.update_delay_profile(
        d["id"],
        {"name": d["name"], "preferred_protocol": "torrent", "delays": {"usenet": 0, "torrent": torrent, "soulseek": 0},
         "bypass_if_highest_quality": False, "bypass_if_above_score": None, "tags": []},
    )


def grab(db, candidates, **kw):
    coord = AcquisitionCoordinator()
    driver = MagicMock()
    driver.download.return_value = HASH
    with patch.object(coord, "search_all_indexers", return_value=candidates), patch(
        "plex_playlist_sync.acquisition_coordinator.get_acquisition_driver", return_value=driver
    ):
        res = coord.search_and_grab(
            artist="Nirvana", title="Nevermind", album="Nevermind", db=db, quality_profile_id=HQ,
            trigger=kw.pop("trigger", GrabTrigger("request")), **kw
        )
    return res, driver


def park(db, **kw):
    set_default(db)
    res, _ = grab(db, [cand()], **kw)
    assert res.get("delayed") is True
    return db.list_pending_releases()[0]


def tick(db, clock, minutes=0):
    clock.advance(minutes)
    driver = MagicMock()
    driver.download.return_value = HASH
    with patch("plex_playlist_sync.acquisition_coordinator.get_acquisition_driver", return_value=driver):
        return pending_worker.release_due(db, now=clock.now), driver


# ---------------------------------------------------------------------------------------------- 1. atomic claim


def test_claim_is_exclusive_across_threads(db, clock):
    row = park(db)
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(db.claim_pending_release(row["id"]))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len([r for r in results if r is not None]) == 1
    assert db.list_pending_releases() == []


def test_grab_pending_twice_grabs_once(db, clients, clock):
    row = park(db)
    driver = MagicMock()
    driver.download.return_value = HASH
    with patch("plex_playlist_sync.acquisition_coordinator.get_acquisition_driver", return_value=driver):
        first = pending_worker.grab_pending(db, row)
        second = pending_worker.grab_pending(db, row)
    assert first["success"] is True
    assert second["success"] is False and second["claimed_elsewhere"] is True
    driver.download.assert_called_once()


def test_gate_elapsed_branch_does_not_grab_a_claimed_row(db, clients, clock):
    row = park(db)
    clock.advance(61)
    assert db.claim_pending_release(row["id"]) is not None  # the tick is mid-grab
    res, driver = grab(db, [cand()])
    assert res["success"] is False
    driver.download.assert_not_called()


def test_gate_elapsed_grab_failure_restores_row(db, clock):  # no client configured -> grab fails
    park(db)
    clock.advance(61)
    res, _ = grab(db, [cand()])
    assert res["success"] is False
    rows = db.list_pending_releases()
    assert len(rows) == 1 and rows[0]["added_at"] == "2026-01-01T12:00:00Z"


def test_endpoint_and_tick_race_grabs_once(db, clients, clock):
    row = park(db)
    clock.advance(61)
    driver = MagicMock()
    driver.download.return_value = HASH
    with patch("plex_playlist_sync.acquisition_coordinator.get_acquisition_driver", return_value=driver):
        pending_worker.grab_pending(db, row, count_failure=False)  # endpoint wins
        stats = pending_worker.release_due(db, now=clock.now)  # tick's snapshot is empty now
    assert stats["released"] == 0
    driver.download.assert_called_once()


# ---------------------------------------------------------------------------------------------- 2. canonical keys


def test_item_key_prefers_album_then_track_then_request():
    assert delay_gate.item_key("r", "a", "t", "x", "y", None) == "album:a"
    assert delay_gate.item_key("r", None, "t", "x", "y", None) == "track:t"
    assert delay_gate.item_key("r", None, None, "x", "y", None) == "req:r"


def test_rss_then_backlog_share_one_row(db, clock):
    first = park(db, request_id="req-1")  # RSS style: request id only
    assert first["item_key"] == "req:req-1"
    clock.advance(10)
    res, _ = grab(db, [cand()], request_id="req-1", album_id="alb-1")  # backlog style
    assert res.get("delayed") is True
    rows = db.list_pending_releases()
    assert len(rows) == 1
    assert rows[0]["item_key"] == "album:alb-1" and rows[0]["request_id"] == "req-1" and rows[0]["album_id"] == "alb-1"
    assert rows[0]["added_at"] == first["added_at"] and rows[0]["release_at"] == first["release_at"]


def _req(db, rid="req-1"):
    db.upsert_user("u1", "bob", "b@x.tv", is_admin=False)
    db.create_request(MusicRequest(id=rid, user_id="u1", item_type="album", title="Nevermind", artist="Nirvana"))


def test_any_grab_clears_every_matching_row(db, clients, clock):
    set_default(db)
    _req(db)
    ts = {"added_at": "2026-01-01T12:00:00Z", "release_at": "2026-01-01T13:00:00Z", "title": "t"}
    db.upsert_pending_release({"item_key": "k-req", "request_id": "req-1", **ts})
    db.upsert_pending_release({"item_key": "k-album", "request_id": "req-1", "album_id": "alb-1", **ts})
    db.upsert_pending_release({"item_key": "k-other", "album_id": "alb-9", **ts})
    res, _ = grab(db, [cand()], bypass_delay=True, request_id="req-1")
    assert res["success"] is True
    assert [r["item_key"] for r in db.list_pending_releases()] == ["k-other"]


def test_clear_pending_for_item_matches_album_and_track_ids(db):
    ts = {"added_at": "2026-01-01T12:00:00Z", "release_at": "2026-01-01T13:00:00Z", "title": "t"}
    db.upsert_pending_release({"item_key": "k-a", "album_id": "alb-1", **ts})
    db.upsert_pending_release({"item_key": "k-t", "track_id": "trk-1", **ts})
    db.upsert_pending_release({"item_key": "k-o", "track_id": "trk-2", **ts})
    assert db.clear_pending_for_item(None, "alb-1", "trk-1") == 2
    assert db.clear_pending_for_item() == 0
    assert [r["item_key"] for r in db.list_pending_releases()] == ["k-o"]


@pytest.fixture
def api(db, tmp_path):
    cfg = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    client = TestClient(app)
    secret = get_or_create_secret_key(data_dir=cfg.data_dir)
    user = db.upsert_user("admin-1", "admin_user", "a@x.tv", is_admin=True)
    token = create_session_token(user_id=user["id"], username=user["username"], is_admin=True, secret_key=secret)
    db.create_session(token, user["id"], {"auth": "test"})
    return client, {"Authorization": f"Bearer {token}"}


def test_manual_grab_without_request_id_clears_by_album_id(api, db, clients, clock):
    client, admin = api
    park(db, album_id="alb-1")
    payload = {
        "release": {"id": "rel-1", "title": "Nirvana - Nevermind [FLAC]", "indexer_name": "Idx", "protocol": "torrent",
                    "size_bytes": 1, "parsed_quality": "FLAC 16bit", "is_acceptable": True, "score": 1,
                    "meets_cutoff": True, "magnet_url": f"magnet:?xt=urn:btih:{HASH}"},
        "artist": "Nirvana", "title": "Nevermind", "album_id": "alb-1",
    }
    driver = MagicMock()
    driver.download.return_value = HASH
    with patch("plex_playlist_sync.api.routes.acquisition.get_acquisition_driver", return_value=driver):
        r = client.post("/api/acquisition/grab", json=payload, headers=admin)
    assert r.status_code == 200
    assert db.list_pending_releases() == []


def test_migration_normalizes_legacy_keys_and_is_idempotent(db):
    base = {"title": "t", "release_at": "2026-01-01T13:00:00Z"}
    db.upsert_pending_release({**base, "item_key": "req:r1", "request_id": "r1", "album_id": "a1", "added_at": "2026-01-01T11:00:00Z"})
    db.upsert_pending_release({**base, "item_key": "album:a1", "album_id": "a1", "added_at": "2026-01-01T12:00:00Z"})
    db.upsert_pending_release({**base, "item_key": "req:r2", "request_id": "r2", "added_at": "2026-01-01T12:00:00Z"})
    db.upsert_pending_release({**base, "item_key": "name:x|y|", "added_at": "2026-01-01T12:00:00Z"})
    for _ in range(2):
        cur = db.conn.cursor()
        db._migration_v53(cur)
        db.conn.commit()
    rows = sorted((r["item_key"], r["added_at"]) for r in db.list_pending_releases())
    assert rows == [("album:a1", "2026-01-01T11:00:00Z"), ("name:x|y|", "2026-01-01T12:00:00Z"),
                    ("req:r2", "2026-01-01T12:00:00Z")]


# ---------------------------------------------------------------------------------------------- 3. re-validation


def _assert_dropped(db, clock, caplog, why):
    caplog.set_level(logging.INFO)
    stats, driver = tick(db, clock, 61)
    driver.download.assert_not_called()
    assert stats["dropped"] == 1 and stats["released"] == 0
    assert db.list_pending_releases() == []
    assert any("Dropped pending release" in r.message and why in r.message for r in caplog.records)
    assert "SUPERSECRETKEY123" not in caplog.text


@pytest.mark.parametrize("status", ["rejected", "available"])
def test_dead_request_is_dropped(db, clients, clock, caplog, status):
    db.upsert_user("u1", "bob", "b@x.tv", is_admin=False)
    db.create_request(MusicRequest(id="req-1", user_id="u1", item_type="album", title="Nevermind", artist="Nirvana"))
    park(db, request_id="req-1", album_id="alb-1")
    db.update_request_status("req-1", RequestStatus(status))
    _assert_dropped(db, clock, caplog, f"request is {status}")


def test_deleted_request_is_dropped(db, clients, clock, caplog):
    park(db, request_id="ghost")
    _assert_dropped(db, clock, caplog, "request no longer exists")


def test_item_with_file_is_dropped(db, clients, clock, caplog):
    park(db, album_id="alb-1")
    with patch.object(db, "library_item_has_file", return_value=True):
        _assert_dropped(db, clock, caplog, "already has a file")


def test_active_download_is_dropped(db, clients, clock, caplog):
    park(db, album_id="alb-1")
    db.create_active_download({"id": "dl-x", "client_id": "c-qbit", "title": "Nirvana - Nevermind [FLAC]",
                               "artist": "Nirvana", "status": "downloading"})
    _assert_dropped(db, clock, caplog, "active download")


def test_blocklisted_release_is_dropped(db, clients, clock, caplog):
    park(db, album_id="alb-1")
    db.add_to_blocklist("Nirvana - Nevermind [FLAC]", release_guid="id-1")
    _assert_dropped(db, clock, caplog, "blocklisted")


def test_expired_row_is_dropped(db, clients, clock, caplog):
    park(db)
    caplog.set_level(logging.INFO)
    clock.advance(60 + 24 * 60 - 1)  # just inside max(24h, 2x60min) past release_at
    stats, driver = tick(db, clock)
    assert stats["released"] == 1  # still valid
    park_again = park(db)
    clock.advance(60 + 24 * 60 + 5)
    stats, driver = tick(db, clock)
    assert stats["dropped"] == 1 and driver.download.call_count == 0
    assert db.list_pending_releases() == [] and park_again
    assert any("expired" in r.message for r in caplog.records)


def test_ttl_scales_with_long_delays(db, clients, clock):
    set_default(db, torrent=3 * 24 * 60)  # 3 days -> TTL 6 days past release_at
    res, _ = grab(db, [cand()])
    assert res.get("delayed")
    clock.advance(3 * 24 * 60 + 5 * 24 * 60)
    stats, driver = tick(db, clock)
    assert stats["released"] == 1 and stats["dropped"] == 0


def test_failures_back_off_then_drop_with_event(db, clock, caplog):  # no client -> every grab fails
    caplog.set_level(logging.INFO)
    park(db, album_id="alb-1")
    clock.advance(61)
    waits = []
    for attempt in range(1, 5):
        stats = pending_worker.release_due(db, now=clock.now)
        assert stats["failed"] == 1
        row = db.list_pending_releases()[0]
        assert row["attempts"] == attempt
        nxt = delay_gate.parse_ts(row["next_attempt_at"]) - clock.now
        waits.append(int(nxt.total_seconds() // 60))
        # inside the backoff window the row is not due
        assert pending_worker.release_due(db, now=clock.now)["due"] == 0
        clock.advance(waits[-1])
    assert waits == [1, 2, 4, 8]
    stats = pending_worker.release_due(db, now=clock.now)  # 5th failure
    assert db.list_pending_releases() == []
    items, _total = db.list_events(limit=10)
    assert any(e["event_type"] == "pending_release_dropped" for e in items)
    assert any("failed 5 times" in r.message for r in caplog.records)
    assert "SUPERSECRETKEY123" not in caplog.text
    assert all("SUPERSECRETKEY123" not in str(e) for e in items)


def test_backoff_is_capped():
    assert [pending_worker.backoff_minutes(n) for n in (1, 2, 3, 4, 5, 6, 7, 10)] == [1, 2, 4, 8, 16, 30, 30, 30]


def test_pending_list_never_returns_payload(api, db, clock):
    client, admin = api
    park(db, album_id="alb-1")
    r = client.get("/api/acquisition/pending", headers=admin)
    assert r.status_code == 200 and r.json()
    assert "SUPERSECRETKEY123" not in r.text and "payload" not in r.text


# ---------------------------------------------------------------------------------------------- 4. profile edit


def test_editing_profile_delays_recomputes_parked_release_at(db, clock):
    row = park(db, album_id="alb-1")
    assert row["release_at"] == "2026-01-01T13:00:00Z"
    clock.advance(10)
    set_default(db, torrent=120)
    assert db.list_pending_releases()[0]["release_at"] == "2026-01-01T14:00:00Z"
    set_default(db, torrent=0)
    assert db.list_pending_releases()[0]["release_at"] == "2026-01-01T12:00:00Z"


# ---------------------------------------------------------------------------------------------- 5. None-safe rank


def test_rank_beats_is_none_safe():
    assert delay_gate.rank_beats([1, None], [1, 2]) is False
    assert delay_gate.rank_beats([1, 2], [1, None]) is True
    assert delay_gate.rank_beats([None], [None]) is False
    assert delay_gate.rank_beats([2, "a"], [2, 1]) in (True, False)  # mixed types do not raise
    assert delay_gate.rank_beats([1, 2, 3], [1, 2]) is True


def test_apply_gate_with_none_in_parked_rank(db, clock):
    row = park(db, album_id="alb-1")
    db.conn.execute("UPDATE pending_releases SET rank_json = ? WHERE id = ?", ('[null, 1, null]', row["id"]))
    db.conn.commit()
    res, _ = grab(db, [cand()], album_id="alb-1")  # must not raise TypeError
    assert res.get("delayed") is True

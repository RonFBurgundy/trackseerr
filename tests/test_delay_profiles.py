"""Delay profiles (migration v50), the delay gate, pending releases, protocol-aware ranking and their APIs."""

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.item_history import GrabTrigger
from plex_playlist_sync import delay_gate, pending_worker
from plex_playlist_sync.acquisition_coordinator import AcquisitionCoordinator
from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.backlog_worker import RSSSyncWorker
from plex_playlist_sync.config import Config
from plex_playlist_sync.decision_engine import PROTOCOL_PREFERENCE, rank_key
from plex_playlist_sync.models import (
    AcquisitionSearchResult,
    EvaluationResult,
    MusicRequest,
    RequestStatus,
)
from plex_playlist_sync.storage import SCHEMA_VERSION, Database

HQ = "profile-high-quality"  # FLAC 24/16, MP3 320, AAC 256, MP3 V0 allowed; FLAC 24bit is the top tier
DP = "/api/settings/delay-profiles"
PEND = "/api/acquisition/pending"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4"


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
    db.create_download_client(
        {"id": "c-sab", "name": "SAB", "driver_type": "sabnzbd", "host_url": "http://s:8080", "enabled": True}
    )


def cand(title, protocol="torrent", seeders=10, did=None):
    return AcquisitionSearchResult(
        download_id=did or f"id-{abs(hash(title)) % 10**8}",
        title=title,
        artist="Nirvana",
        album="Nevermind",
        size_bytes=300_000_000,
        magnet_url=f"magnet:?xt=urn:btih:{HASH}" if protocol == "torrent" else None,
        download_url=None if protocol == "torrent" else "http://nzb/x.nzb",
        source="torznab" if protocol == "torrent" else "newznab",
        protocol=protocol,
        seeders=seeders if protocol == "torrent" else None,
    )


def set_default(db, **kw):
    d = db.get_delay_profile(db.list_delay_profiles()[-1]["id"])
    body = {
        "name": d["name"],
        "preferred_protocol": kw.get("preferred_protocol", d["preferred_protocol"]),
        "delays": kw.get("delays", d["delays"]),
        "bypass_if_highest_quality": kw.get("bypass_if_highest_quality", d["bypass_if_highest_quality"]),
        "bypass_if_above_score": kw.get("bypass_if_above_score", d["bypass_if_above_score"]),
        "tags": [],
    }
    return db.update_delay_profile(d["id"], body)


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


# --------------------------------------------------------------------------------------------- migration


def test_v50_migration_seeds_default_and_pending_table(tmp_path):
    assert SCHEMA_VERSION >= 51
    path = str(tmp_path / "m.db")
    Database(path).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE delay_profiles")
    conn.execute("DROP TABLE pending_releases")
    conn.execute("DELETE FROM schema_migrations WHERE version >= 50")
    conn.commit()
    conn.close()
    d = Database(path)
    try:
        assert d.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
        profiles = d.list_delay_profiles()
        assert len(profiles) == 1
        p = profiles[0]
        assert p["is_default"] and p["tags"] == [] and p["preferred_protocol"] == "usenet"
        assert p["delays"] == {"usenet": 0, "torrent": 0, "soulseek": 0}
        assert p["bypass_if_highest_quality"] is True and p["bypass_if_above_score"] is None
        cols = {r[1] for r in d.conn.execute("PRAGMA table_info(pending_releases)")}
        assert {"title", "payload_json", "protocol", "quality", "format_score", "album_id", "track_id", "artist_name",
                "added_at", "release_at", "reason"} <= cols
    finally:
        d.close()
    d = Database(path)  # re-opening does not duplicate the default
    try:
        assert len(d.list_delay_profiles()) == 1
    finally:
        d.close()


def test_fresh_install_seeded_quality_profiles_use_minus_100(db):
    assert {p["min_format_score"] for p in db.list_quality_profiles()} == {-100}


# --------------------------------------------------------------------------------------------- selection


def _profiles(db):
    db.create_delay_profile({"name": "Metal", "preferred_protocol": "torrent", "delays": {"usenet": 0, "torrent": 30, "soulseek": 0},
                             "bypass_if_highest_quality": False, "bypass_if_above_score": None, "tags": ["Metal"]})
    db.create_delay_profile({"name": "Jazz", "preferred_protocol": "soulseek", "delays": {"usenet": 0, "torrent": 0, "soulseek": 5},
                             "bypass_if_highest_quality": False, "bypass_if_above_score": None, "tags": ["jazz", "metal"]})
    return db.list_delay_profiles()


def test_select_profile_first_tag_match_in_order_else_default(db):
    profiles = _profiles(db)
    assert [p["name"] for p in profiles] == ["Metal", "Jazz", "Default"]
    assert delay_gate.select_delay_profile(profiles, ["metal"])["name"] == "Metal"  # first in order wins
    assert delay_gate.select_delay_profile(profiles, ["JAZZ"])["name"] == "Jazz"  # case-insensitive
    assert delay_gate.select_delay_profile(profiles, ["pop"])["is_default"]
    assert delay_gate.select_delay_profile(profiles, [])["is_default"]


def test_artist_without_tags_field_uses_default(db):
    _profiles(db)
    assert delay_gate.artist_tags(db, "Nirvana") == []
    assert delay_gate.resolve_delay_profile(db, "Nirvana")["is_default"]


def test_artist_tags_read_from_artist_tags_table(db):
    _profiles(db)
    db.conn.execute("INSERT INTO library_artists (id, name, clean_name) VALUES ('a1', 'Sepultura', 'sepultura')")
    db.conn.commit()
    db.set_artist_tags("a1", [db.get_tag_by_label("metal")["id"]])
    assert delay_gate.resolve_delay_profile(db, "Sepultura")["name"] == "Metal"


# --------------------------------------------------------------------------------------------- ranking


def _res(tier=1, fs=0):
    return EvaluationResult(True, 0, [], "FLAC 16bit", True, format_score=fs, tier=tier, kbps_distance=0.0)


def test_rank_key_uses_preferred_protocol():
    r = _res()
    assert PROTOCOL_PREFERENCE[0] == "usenet"
    assert rank_key("usenet", 0, r) > rank_key("torrent", 50, r)
    assert rank_key("torrent", 50, r, "torrent") > rank_key("usenet", 0, r, "torrent")
    assert rank_key("soulseek", 0, r, "soulseek") > rank_key("usenet", 0, r, "soulseek") > rank_key("torrent", 0, r, "soulseek")
    assert rank_key("usenet", 0, _res(fs=5), "torrent") < rank_key("torrent", 0, _res(fs=6), "torrent")  # score still first


def test_profile_preference_changes_the_grab_choice(db, clients):
    candidates = [cand("Nirvana - Nevermind [FLAC]", "torrent", 99, "t1"), cand("Nirvana - Nevermind [FLAC]", "usenet", None, "u1")]
    res, _ = grab(db, candidates)
    assert res["success"] and res["client"] == "SAB"
    set_default(db, preferred_protocol="torrent")
    res, _ = grab(db, candidates)
    assert res["success"] and res["client"] == "qBit"


# --------------------------------------------------------------------------------------------- the gate


def test_delay_holds_then_releases_on_tick(db, clients, clock):
    set_default(db, preferred_protocol="torrent", delays={"usenet": 0, "torrent": 60, "soulseek": 0},
                bypass_if_highest_quality=False)
    res, driver = grab(db, [cand("Nirvana - Nevermind [FLAC]")])
    assert res["success"] is False and res["delayed"] is True
    assert "releases at 2026-01-01T13:00:00Z" in res["message"]
    driver.download.assert_not_called()
    rows = db.list_pending_releases()
    assert len(rows) == 1
    row = rows[0]
    assert row["added_at"] == "2026-01-01T12:00:00Z" and row["release_at"] == "2026-01-01T13:00:00Z"
    assert row["protocol"] == "torrent" and row["quality"] and row["artist_name"] == "Nirvana"

    # not due yet
    clock.advance(30)
    driver = MagicMock()
    driver.download.return_value = HASH
    with patch("plex_playlist_sync.acquisition_coordinator.get_acquisition_driver", return_value=driver):
        assert pending_worker.release_due(db, now=clock.now) == {"due": 0, "released": 0, "failed": 0, "dropped": 0}
        assert len(db.list_pending_releases()) == 1
        clock.advance(31)
        stats = pending_worker.release_due(db, now=clock.now)
    assert stats == {"due": 1, "released": 1, "failed": 0, "dropped": 0}
    driver.download.assert_called_once()
    assert db.list_pending_releases() == []
    assert len(db.list_active_downloads()) == 1


def test_failed_pending_grab_stays_queued(db, clock):  # no download client configured
    set_default(db, preferred_protocol="torrent", delays={"usenet": 0, "torrent": 10, "soulseek": 0}, bypass_if_highest_quality=False)
    grab(db, [cand("Nirvana - Nevermind [FLAC]")])
    clock.advance(11)
    stats = pending_worker.release_due(db, now=clock.now)
    assert stats["failed"] == 1 and stats["released"] == 0
    assert len(db.list_pending_releases()) == 1


def test_better_candidate_replaces_pending_and_keeps_window(db, clients, clock):
    set_default(db, preferred_protocol="torrent", delays={"usenet": 0, "torrent": 60, "soulseek": 0}, bypass_if_highest_quality=False)
    grab(db, [cand("Nirvana - Nevermind [MP3 320]", did="mp3")])
    first = db.list_pending_releases()[0]
    clock.advance(20)
    res, _ = grab(db, [cand("Nirvana - Nevermind [MP3 320]", did="mp3"), cand("Nirvana - Nevermind [FLAC]", did="flac")])
    assert res.get("delayed") is True
    rows = db.list_pending_releases()
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"]
    assert "FLAC" in rows[0]["title"] and rows[0]["quality"] != first["quality"]
    assert rows[0]["added_at"] == first["added_at"] and rows[0]["release_at"] == first["release_at"]  # window not restarted

    clock.advance(10)  # a worse candidate does not displace the parked FLAC
    grab(db, [cand("Nirvana - Nevermind [MP3 320]", did="mp3")])
    rows = db.list_pending_releases()
    assert len(rows) == 1 and rows[0]["title"].endswith("[FLAC]") and rows[0]["release_at"] == first["release_at"]


def test_search_after_window_grabs_and_clears_pending(db, clients, clock):
    set_default(db, preferred_protocol="torrent", delays={"usenet": 0, "torrent": 60, "soulseek": 0}, bypass_if_highest_quality=False)
    grab(db, [cand("Nirvana - Nevermind [FLAC]")])
    clock.advance(61)
    res, driver = grab(db, [cand("Nirvana - Nevermind [FLAC]")])
    assert res["success"] is True
    driver.download.assert_called_once()
    assert db.list_pending_releases() == []


def test_bypass_highest_quality(db, clients, clock):
    set_default(db, preferred_protocol="torrent", delays={"usenet": 0, "torrent": 60, "soulseek": 0}, bypass_if_highest_quality=True)
    res, _ = grab(db, [cand("Nirvana - Nevermind [FLAC 24bit]")])
    assert res["success"] is True and db.list_pending_releases() == []
    # not the top tier -> held
    res, _ = grab(db, [cand("Nirvana - Nevermind [MP3 320]")])
    assert res.get("delayed") is True


def test_bypass_format_score_threshold(db, clients, clock):
    set_default(db, preferred_protocol="torrent", delays={"usenet": 0, "torrent": 60, "soulseek": 0}, bypass_if_highest_quality=False)
    grab(db, [cand("Nirvana - Nevermind [MP3 320]")])
    fs = db.list_pending_releases()[0]["format_score"]
    db.conn.execute("DELETE FROM pending_releases")
    db.conn.commit()
    set_default(db, bypass_if_above_score=fs + 1)
    res, _ = grab(db, [cand("Nirvana - Nevermind [MP3 320]")])
    assert res.get("delayed") is True
    set_default(db, bypass_if_above_score=fs)
    res, _ = grab(db, [cand("Nirvana - Nevermind [MP3 320]")])
    assert res["success"] is True and db.list_pending_releases() == []


def test_zero_delay_protocol_grabs_and_drops_stale_pending(db, clients, clock):
    set_default(db, preferred_protocol="torrent", delays={"usenet": 0, "torrent": 60, "soulseek": 0}, bypass_if_highest_quality=False)
    grab(db, [cand("Nirvana - Nevermind [FLAC]", "torrent", did="t")])
    assert len(db.list_pending_releases()) == 1
    set_default(db, preferred_protocol="usenet")
    res, _ = grab(db, [cand("Nirvana - Nevermind [FLAC]", "torrent", did="t"), cand("Nirvana - Nevermind [FLAC]", "usenet", did="u")])
    assert res["success"] is True and res["client"] == "SAB"
    assert db.list_pending_releases() == []


def test_manual_grab_bypasses_delay(db, clients, clock):
    set_default(db, preferred_protocol="torrent", delays={"usenet": 0, "torrent": 60, "soulseek": 0}, bypass_if_highest_quality=False)
    res, driver = grab(db, [cand("Nirvana - Nevermind [FLAC]")], bypass_delay=True)
    assert res["success"] is True
    driver.download.assert_called_once()
    assert db.list_pending_releases() == []


def test_rss_holds_release_behind_delay(db, clients, clock):
    set_default(db, preferred_protocol="torrent", delays={"usenet": 0, "torrent": 60, "soulseek": 0}, bypass_if_highest_quality=False)
    db.upsert_user("u1", "bob", "b@x.tv", is_admin=False)
    db.create_indexer({"id": "i1", "name": "Idx", "indexer_type": "torznab", "host_url": "http://i", "enabled": True})
    db.create_request(MusicRequest(id="req-1", user_id="u1", item_type="album", title="Nevermind", artist="Nirvana",
                                   album="Nevermind", status=RequestStatus.PENDING))
    idx = MagicMock()
    idx.fetch_recent.return_value = [cand("Nirvana - Nevermind (1991) [FLAC 16bit]")]
    client = MagicMock()
    client.download.return_value = HASH
    with patch("plex_playlist_sync.backlog_worker.get_indexer_driver", return_value=idx), patch(
        "plex_playlist_sync.backlog_worker.get_acquisition_driver", return_value=client
    ):
        stats = RSSSyncWorker().poll_once(db)
    assert stats["grabs_triggered"] == 0
    client.download.assert_not_called()
    rows = db.list_pending_releases()
    assert len(rows) == 1 and rows[0]["request_id"] == "req-1" and rows[0]["release_at"] == "2026-01-01T13:00:00Z"
    assert db.get_request("req-1")["status"] == "pending"


# --------------------------------------------------------------------------------------------- API fixtures


@pytest.fixture
def api(db, tmp_path):
    cfg = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path))
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    client = TestClient(app)
    secret = get_or_create_secret_key(data_dir=cfg.data_dir)

    def headers(user):
        token = create_session_token(user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret)
        db.create_session(token, user["id"], {"auth": "test"})
        return {"Authorization": f"Bearer {token}"}

    admin = headers(db.upsert_user("admin-1", "admin_user", "a@x.tv", is_admin=True))
    alice = headers(db.upsert_user("alice-1", "alice", "al@x.tv", is_admin=False))
    return client, admin, alice, cfg


def body(**kw):
    b = {"name": "Metal", "preferred_protocol": "torrent", "delays": {"usenet": 0, "torrent": 30, "soulseek": 0},
         "bypass_if_highest_quality": False, "bypass_if_above_score": 50, "tags": ["metal", " Metal ", "rock"]}
    b.update(kw)
    return b


# --------------------------------------------------------------------------------------------- delay profile API


@pytest.mark.parametrize("method,path", [("get", DP), ("post", DP), ("post", DP + "/reorder"), ("get", PEND)])
def test_delay_api_authz(api, method, path):
    client, admin, alice, _ = api
    kw = {"json": {"ids": []}} if method == "post" else {}
    assert getattr(client, method)(path, **kw).status_code == 401
    assert getattr(client, method)(path, headers=alice, **kw).status_code == 403


def test_delay_api_gateway_tier_blocked(api):
    client, admin, _, _ = api
    client.app.dependency_overrides[get_config] = lambda: Config(plex_url="http://x", plex_token="t", data_dir="/tmp", role="gateway")
    assert client.get(DP, headers=admin).status_code in (403, 404)
    assert client.get(PEND, headers=admin).status_code in (403, 404)


def test_delay_api_list_shape_and_default(api):
    client, admin, _, _ = api
    rows = client.get(DP, headers=admin).json()
    assert len(rows) == 1
    d = rows[0]
    assert set(d) == {"id", "order", "name", "preferred_protocol", "delays", "bypass_if_highest_quality",
                      "bypass_if_above_score", "tags", "is_default"}
    assert d["is_default"] and d["tags"] == [] and d["preferred_protocol"] == "usenet"
    assert d["delays"] == {"usenet": 0, "torrent": 0, "soulseek": 0} and d["bypass_if_highest_quality"] is True


def test_delay_api_crud_and_default_sorts_last(api):
    client, admin, _, _ = api
    r = client.post(DP, json=body(), headers=admin)
    assert r.status_code == 200
    created = r.json()
    assert created["tags"] == ["metal", "rock"] and created["is_default"] is False and created["bypass_if_above_score"] == 50
    r2 = client.post(DP, json=body(name="Jazz", tags=["jazz"]), headers=admin).json()
    names = [p["name"] for p in client.get(DP, headers=admin).json()]
    assert names == ["Metal", "Jazz", "Default"]
    assert [p["order"] for p in client.get(DP, headers=admin).json()][:2] == [1, 2]

    upd = client.put(f"{DP}/{created['id']}", json=body(name="Metal2", preferred_protocol="soulseek", bypass_if_above_score=None), headers=admin)
    assert upd.status_code == 200 and upd.json()["name"] == "Metal2" and upd.json()["preferred_protocol"] == "soulseek"
    assert upd.json()["bypass_if_above_score"] is None
    assert client.put(f"{DP}/9999", json=body(), headers=admin).status_code == 404

    assert client.delete(f"{DP}/{r2['id']}", headers=admin).status_code == 200
    assert client.delete(f"{DP}/{r2['id']}", headers=admin).status_code == 404


def test_delay_api_default_cannot_be_deleted_and_keeps_no_tags(api):
    client, admin, _, _ = api
    default = client.get(DP, headers=admin).json()[0]
    assert client.delete(f"{DP}/{default['id']}", headers=admin).status_code == 400
    r = client.put(f"{DP}/{default['id']}", json=body(name="Default", tags=["x"]), headers=admin)
    assert r.status_code == 200 and r.json()["tags"] == [] and r.json()["is_default"] is True
    assert r.json()["delays"]["torrent"] == 30
    assert len(client.get(DP, headers=admin).json()) == 1


@pytest.mark.parametrize(
    "patch_",
    [
        {"preferred_protocol": "ftp"},
        {"delays": {"usenet": -1, "torrent": 0, "soulseek": 0}},
        {"delays": {"usenet": 10**9, "torrent": 0, "soulseek": 0}},
        {"delays": {"usenet": "soon", "torrent": 0, "soulseek": 0}},
        {"bypass_if_above_score": "high"},
        {"tags": "metal"},
    ],
)
def test_delay_api_validation(api, patch_):
    client, admin, _, _ = api
    assert client.post(DP, json=body(**patch_), headers=admin).status_code == 422


def test_delay_api_reorder(api):
    client, admin, _, _ = api
    a = client.post(DP, json=body(name="A", tags=["a"]), headers=admin).json()
    b = client.post(DP, json=body(name="B", tags=["b"]), headers=admin).json()
    c = client.post(DP, json=body(name="C", tags=["c"]), headers=admin).json()
    default = client.get(DP, headers=admin).json()[-1]
    r = client.post(f"{DP}/reorder", json={"ids": [c["id"], a["id"], b["id"]]}, headers=admin)
    assert r.status_code == 200 and [p["name"] for p in r.json()] == ["C", "A", "B", "Default"]
    # the default may be included; it stays last
    r = client.post(f"{DP}/reorder", json={"ids": [b["id"], default["id"], c["id"], a["id"]]}, headers=admin)
    assert r.status_code == 200 and [p["name"] for p in r.json()] == ["B", "C", "A", "Default"]
    # missing, unknown or duplicated ids are rejected and change nothing
    for ids in ([a["id"]], [a["id"], b["id"], c["id"], 999], [a["id"], a["id"], b["id"], c["id"]]):
        assert client.post(f"{DP}/reorder", json={"ids": ids}, headers=admin).status_code == 400
    assert [p["name"] for p in client.get(DP, headers=admin).json()] == ["B", "C", "A", "Default"]


def test_new_quality_profile_defaults_min_format_score_to_minus_100(api):
    client, admin, _, _ = api
    r = client.post(
        "/api/settings/quality-profiles",
        json={"name": "Fresh", "cutoff": "FLAC 16bit", "items": [{"quality": "FLAC 16bit", "allowed": True}]},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    assert r.json()["min_format_score"] == -100


# --------------------------------------------------------------------------------------------- pending API


def _park(db, clients, clock, request_id=None, album_id="alb-1"):
    set_default(db, preferred_protocol="torrent", delays={"usenet": 0, "torrent": 60, "soulseek": 0}, bypass_if_highest_quality=False)
    grab(db, [cand("Nirvana - Nevermind [FLAC]")], album_id=album_id, request_id=request_id)
    return db.list_pending_releases()[0]


def test_pending_list_shape(api, db, clients, clock):
    client, admin, _, _ = api
    assert client.get(PEND, headers=admin).json() == []
    row = _park(db, clients, clock)
    items = client.get(PEND, headers=admin).json()
    assert len(items) == 1
    assert set(items[0]) == {"id", "title", "album_id", "artist_name", "protocol", "quality", "format_score",
                             "added_at", "release_at", "reason"}
    assert items[0]["id"] == row["id"] and items[0]["album_id"] == "alb-1" and items[0]["artist_name"] == "Nirvana"
    assert items[0]["protocol"] == "torrent" and items[0]["release_at"] == "2026-01-01T13:00:00Z"
    assert "Delayed 60 min" in items[0]["reason"]


def test_pending_drop(api, db, clients, clock):
    client, admin, alice, _ = api
    row = _park(db, clients, clock)
    assert client.delete(f"{PEND}/{row['id']}", headers=alice).status_code == 403
    assert client.delete(f"{PEND}/{row['id']}", headers=admin).status_code == 200
    assert client.delete(f"{PEND}/{row['id']}", headers=admin).status_code == 404
    assert db.list_pending_releases() == []


def test_pending_grab_now(api, db, clients, clock):
    client, admin, alice, _ = api
    row = _park(db, clients, clock, album_id=None)
    assert client.post(f"{PEND}/{row['id']}/grab", headers=alice).status_code == 403
    assert client.post(f"{PEND}/99999/grab", headers=admin).status_code == 404
    driver = MagicMock()
    driver.download.return_value = HASH
    with patch("plex_playlist_sync.acquisition_coordinator.get_acquisition_driver", return_value=driver):
        r = client.post(f"{PEND}/{row['id']}/grab", headers=admin)
    assert r.status_code == 200 and r.json()["success"] is True and r.json()["download_id"]
    driver.download.assert_called_once()
    assert db.list_pending_releases() == []
    assert len(db.list_active_downloads()) == 1


def test_pending_grab_now_without_client_is_400_and_keeps_row(api, db, clock):
    client, admin, _, _ = api
    row = _park(db, None, clock, album_id=None)
    r = client.post(f"{PEND}/{row['id']}/grab", headers=admin)
    assert r.status_code == 400
    assert len(db.list_pending_releases()) == 1


# --------------------------------------------------------------------------------------------- manual search / grab


def test_manual_search_orders_by_rank_key_with_profile_protocol(api, db):
    client, admin, _, _ = api
    candidates = [
        cand("Nirvana - Nevermind [FLAC]", "torrent", 500, "t-flac"),
        cand("Nirvana - Nevermind [FLAC]", "usenet", None, "u-flac"),
        cand("Nirvana - Nevermind [MP3 320]", "usenet", None, "u-mp3"),
    ]

    def order():
        with patch("plex_playlist_sync.api.routes.acquisition.acquisition_coordinator.search_all_indexers", return_value=candidates):
            r = client.post("/api/acquisition/search", json={"artist": "Nirvana", "title": "Nevermind", "quality_profile_id": HQ}, headers=admin)
        assert r.status_code == 200, r.text
        return [x["id"] for x in r.json()["results"]]

    # usenet preferred (default): the usenet FLAC beats the equal torrent FLAC despite 500 seeders; MP3 last
    assert order() == ["u-flac", "t-flac", "u-mp3"]
    set_default(db, preferred_protocol="torrent")
    assert order() == ["t-flac", "u-flac", "u-mp3"]


def test_manual_search_sort_key_is_rank_key(api, db):
    """Quality tier outranks everything and unacceptable releases sort last, exactly as rank_key orders them."""
    client, admin, _, _ = api
    candidates = [
        cand("Nirvana - Nevermind [MP3 320]", "usenet", None, "mp3"),
        cand("Nirvana - Nevermind [FLAC 24bit]", "torrent", 1, "flac24"),
        cand("Nirvana - Nevermind [MP3 128]", "usenet", None, "bad"),
    ]
    with patch("plex_playlist_sync.api.routes.acquisition.acquisition_coordinator.search_all_indexers", return_value=candidates), \
         patch("plex_playlist_sync.api.routes.acquisition.candidate_rank", wraps=__import__("plex_playlist_sync.acquisition_coordinator", fromlist=["x"]).candidate_rank) as spy:
        r = client.post("/api/acquisition/search", json={"artist": "Nirvana", "title": "Nevermind", "quality_profile_id": HQ}, headers=admin)
    ids = [x["id"] for x in r.json()["results"]]
    assert ids[0] == "flac24" and ids[-1] == "bad"
    assert spy.call_count == 3
    assert all(c.args[2] == "usenet" for c in spy.call_args_list)  # the default profile's preferred protocol is passed


def test_manual_grab_endpoint_bypasses_delay_and_clears_pending(api, db, clients, clock):
    client, admin, _, _ = api
    db.upsert_user("u1", "bob", "b@x.tv", is_admin=False)
    db.create_request(MusicRequest(id="req-9", user_id="u1", item_type="track", title="Nevermind", artist="Nirvana"))
    set_default(db, preferred_protocol="torrent", delays={"usenet": 0, "torrent": 60, "soulseek": 0}, bypass_if_highest_quality=False)
    grab(db, [cand("Nirvana - Nevermind [FLAC]")], request_id="req-9")
    assert len(db.list_pending_releases()) == 1
    payload = {
        "release": {"id": "rel-1", "title": "Nirvana - Nevermind [FLAC]", "indexer_name": "Idx", "protocol": "torrent",
                    "size_bytes": 1, "parsed_quality": "FLAC 16bit", "is_acceptable": True, "score": 1, "meets_cutoff": True,
                    "magnet_url": f"magnet:?xt=urn:btih:{HASH}"},
        "artist": "Nirvana", "title": "Nevermind", "request_id": "req-9",
    }
    driver = MagicMock()
    driver.download.return_value = HASH
    with patch("plex_playlist_sync.api.routes.acquisition.get_acquisition_driver", return_value=driver):
        r = client.post("/api/acquisition/grab", json=payload, headers=admin)
    assert r.status_code == 200 and r.json()["success"] is True
    driver.download.assert_called_once()
    assert db.list_pending_releases() == []

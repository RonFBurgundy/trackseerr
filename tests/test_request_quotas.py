"""Per-type request quotas, discography batches, per-type auto-approve and the v28 migration."""

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import get_config, get_db
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.clients.core_client import CoreClient
from plex_playlist_sync.config import Config
from plex_playlist_sync.models import MusicRequest, RequestStatus, UserPermission
from plex_playlist_sync.request_submission import (
    RequestRejected,
    effective_quota_limits,
    submit_batch_requests,
    submit_track_request,
)
from plex_playlist_sync.storage import Database

SECRET = "s" * 40
REQ = int(UserPermission.REQUEST)
AUTO_TRACK = int(UserPermission.AUTO_APPROVE)
AUTO_ALBUM = int(UserPermission.AUTO_APPROVE_ALBUM)
AUTO_DISCO = int(UserPermission.AUTO_APPROVE_DISCOGRAPHY)


@pytest.fixture
def db():
    d = Database(":memory:")
    d.upsert_user("admin-1", "root", "a@x.tv", is_admin=True)
    d.upsert_user("1001", "alice", "al@x.tv", is_admin=False)
    d.upsert_user("1002", "bob", "bo@x.tv", is_admin=False)
    yield d
    d.close()


@pytest.fixture
def cfg(tmp_path):
    return Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), auto_approve_requests=False)


def _client(db: Database, config: Config) -> TestClient:
    app = create_app(db=db, config=config)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    return TestClient(app)


def _headers(db: Database, config: Config, user_id: str) -> dict[str, str]:
    user = db.get_user(user_id)
    key = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=bool(user["is_admin"]), secret_key=key
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(db, cfg):
    return {
        "db": db,
        "cfg": cfg,
        "client": _client(db, cfg),
        "alice": _headers(db, cfg, "1001"),
        "bob": _headers(db, cfg, "1002"),
        "admin": _headers(db, cfg, "admin-1"),
    }


def _settings(db: Database, **kw: int) -> None:
    db.update_account_settings({f"default_quota_{k}": v for k, v in kw.items()})


def _single(env, who: str, item_type: str, n: int, artist: str = "Band"):
    return env["client"].post(
        "/api/requests",
        json={"item_type": item_type, "title": f"{item_type}-{n}", "artist": f"{artist}-{n}"},
        headers=env[who],
    )


def _albums(artist: str, count: int, start: int = 0) -> list[dict[str, Any]]:
    return [{"item_type": "album", "title": f"{artist} Album {i}", "artist": artist} for i in range(start, start + count)]


def _discography(env, who: str, artist: str, items: list[dict[str, Any]]):
    return env["client"].post(
        "/api/requests/batch", json={"kind": "discography", "artist": artist, "requests": items}, headers=env[who]
    )


def _used(db: Database, user_id: str, days: int = 7) -> dict[str, int]:
    return db.count_user_requests_by_type(user_id, days)


def _backdate(db: Database, request_id: str, days: int) -> None:
    with db._lock:
        db.conn.execute(
            "UPDATE music_requests SET created_at = datetime('now', ?) WHERE id = ?", (f"-{days} days", request_id)
        )
        db.conn.commit()


# --------------------------------------------------------------------------- per-type limits


def test_each_type_has_its_own_limit_and_exact_error_wording(env):
    _settings(env["db"], tracks=2, albums=1, discographies=1, window_days=7)
    for n in range(2):
        assert _single(env, "alice", "track", n).status_code == 201
    res = _single(env, "alice", "track", 9)
    assert res.status_code == 400
    assert res.json()["detail"] == "Request quota reached for tracks (2 per 7 days)"
    # the album quota is untouched by the tracks
    assert _single(env, "alice", "album", 0).status_code == 201
    res = _single(env, "alice", "album", 1)
    assert res.status_code == 400
    assert res.json()["detail"] == "Request quota reached for albums (1 per 7 days)"
    assert _used(env["db"], "1001") == {"track": 2, "album": 1, "discography": 0}


def test_discography_limit_error_wording(env):
    _settings(env["db"], discographies=1)
    assert _discography(env, "alice", "Muse", _albums("Muse", 3)).status_code == 201
    res = _discography(env, "alice", "Radiohead", _albums("Radiohead", 2))
    assert res.status_code == 400
    assert res.json()["detail"] == "Request quota reached for discographies (1 per 7 days)"
    assert [r["artist"] for r in env["db"].list_requests(user_id="1001")] == ["Muse"] * 3


def test_zero_limit_blocks_immediately_and_quotas_are_per_user(env):
    _settings(env["db"], tracks=0)
    assert _single(env, "alice", "track", 0).status_code == 400
    env["db"].update_user_admin_fields("1002", {"quota_tracks": 1})
    assert _single(env, "bob", "track", 0).status_code == 201
    assert _single(env, "bob", "track", 1).status_code == 400
    assert _single(env, "alice", "track", 2).status_code == 400


def test_override_beats_default_and_null_falls_back(env):
    db = env["db"]
    _settings(db, albums=1)
    assert effective_quota_limits(db, "1001")["albums"] == 1
    db.update_user_admin_fields("1001", {"quota_albums": 3})
    assert effective_quota_limits(db, "1001")["albums"] == 3
    for n in range(3):
        assert _single(env, "alice", "album", n).status_code == 201
    assert _single(env, "alice", "album", 3).status_code == 400
    # the other user still has the default
    assert _single(env, "bob", "album", 0).status_code == 201
    assert _single(env, "bob", "album", 1).status_code == 400
    db.update_user_admin_fields("1001", {"quota_albums": None})
    assert effective_quota_limits(db, "1001")["albums"] == 1
    # a lowered override applies to usage that already exists
    db.update_user_admin_fields("1001", {"quota_albums": 2})
    assert _single(env, "alice", "album", 9).status_code == 400


def test_window_excludes_old_requests_and_override_window_applies(env):
    db = env["db"]
    _settings(db, tracks=1, window_days=7)
    first = _single(env, "alice", "track", 0).json()["id"]
    assert _single(env, "alice", "track", 1).status_code == 400
    _backdate(db, first, 8)  # outside the 7 day window
    assert _single(env, "alice", "track", 2).status_code == 201
    # a per-user 30 day window pulls the old request back in
    db.update_user_admin_fields("1002", {"quota_window_days": 30})
    old = _single(env, "bob", "track", 0).json()["id"]
    _backdate(db, old, 20)
    assert _single(env, "bob", "track", 1).status_code == 400
    _backdate(db, old, 31)
    assert _single(env, "bob", "track", 2).status_code == 201


def test_rejected_cancelled_do_not_count_but_pending_processing_approved_available_do(env):
    db = env["db"]
    _settings(db, tracks=10)
    ids = {}
    for status in ("pending", "processing", "approved", "available", "rejected", "cancelled"):
        ids[status] = f"req-{status}"
        db.create_request(MusicRequest(id=ids[status], user_id="1001", item_type="track", title=status, artist="X",
                                       status=RequestStatus.PENDING))
        with db._lock:
            db.conn.execute("UPDATE music_requests SET status = ? WHERE id = ?", (status, ids[status]))
            db.conn.commit()
    assert _used(db, "1001")["track"] == 4
    # cancelling by deletion frees the slot as well
    assert _single(env, "alice", "track", 0).status_code == 201
    assert env["client"].delete(f"/api/requests/{ids['pending']}", headers=env["alice"]).status_code == 200
    assert _used(db, "1001")["track"] == 4


def test_duplicate_is_409_and_does_not_consume_quota(env):
    _settings(env["db"], tracks=2)
    body = {"item_type": "track", "title": "Same", "artist": "Band"}
    assert env["client"].post("/api/requests", json=body, headers=env["alice"]).status_code == 201
    assert env["client"].post("/api/requests", json=body, headers=env["alice"]).status_code == 409
    assert _used(env["db"], "1001")["track"] == 1


# --------------------------------------------------------------------------- discography batches


def test_discography_consumes_one_unit_and_no_album_units(env):
    db = env["db"]
    _settings(db, albums=1, discographies=2)
    res = _discography(env, "alice", "Muse", _albums("Muse", 12))
    assert res.status_code == 201
    created = res.json()["created"]
    assert res.json()["count"] == 12 and len(created) == 12
    batch_ids = {r["batch_id"] for r in created}
    assert len(batch_ids) == 1 and next(iter(batch_ids)).startswith("batch-")
    assert {r["batch_kind"] for r in created} == {"discography"}
    assert all(r["item_type"] == "album" and r["status"] == "pending" for r in created)
    assert _used(db, "1001") == {"track": 0, "album": 0, "discography": 1}
    # the single album allowance is still completely free
    assert _single(env, "alice", "album", 0).status_code == 201
    assert _used(db, "1001") == {"track": 0, "album": 1, "discography": 1}
    # a second discography uses the second unit; a third is refused
    assert _discography(env, "alice", "Bjork", _albums("Bjork", 2)).status_code == 201
    assert _used(db, "1001")["discography"] == 2
    assert _discography(env, "alice", "Beck", _albums("Beck", 2)).status_code == 400


def test_discography_works_with_zero_album_quota(env):
    _settings(env["db"], albums=0, discographies=1)
    assert _single(env, "alice", "album", 0).status_code == 400
    assert _discography(env, "alice", "Muse", _albums("Muse", 5)).status_code == 201


def test_discography_artist_match_is_case_insensitive_and_trimmed(env):
    items = _albums("MUSE", 2) + [{"item_type": "album", "title": "Third", "artist": "  muse "}]
    res = _discography(env, "alice", "Muse", items)
    assert res.status_code == 201 and res.json()["count"] == 3


def test_discography_with_51_albums_is_422_and_creates_nothing(env):
    res = _discography(env, "alice", "Muse", _albums("Muse", 51))
    assert res.status_code == 422
    assert env["db"].list_requests(user_id="1001") == []
    assert _discography(env, "alice", "Muse", _albums("Muse", 50)).status_code == 201


def test_discography_exactly_50_albums_is_one_unit(env):
    res = _discography(env, "alice", "Muse", _albums("Muse", 50))
    assert res.status_code == 201 and res.json()["count"] == 50
    assert _used(env["db"], "1001") == {"track": 0, "album": 0, "discography": 1}


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "discography", "artist": "Muse", "requests": _albums("Muse", 2) + _albums("Other", 1)},
        {"kind": "discography", "artist": "Muse", "requests": _albums("Other", 2)},
        {"kind": "discography", "requests": _albums("Muse", 2)},
        {"kind": "discography", "artist": "  ", "requests": _albums("Muse", 2)},
        {"kind": "discography", "artist": "Muse",
         "requests": _albums("Muse", 1) + [{"item_type": "track", "title": "T", "artist": "Muse"}]},
        {"kind": "everything", "artist": "Muse", "requests": _albums("Muse", 2)},
        {"kind": "discography", "artist": "Muse", "requests": []},
    ],
)
def test_discography_validation_is_422_and_creates_nothing(env, payload):
    res = env["client"].post("/api/requests/batch", json=payload, headers=env["alice"])
    assert res.status_code == 422
    assert env["db"].list_requests(user_id="1001") == []


def test_plain_batch_over_50_items_is_422(env):
    items = [{"item_type": "track", "title": f"T{i}", "artist": "A"} for i in range(51)]
    assert env["client"].post("/api/requests/batch", json={"requests": items}, headers=env["admin"]).status_code == 422
    assert env["db"].list_requests() == []


def test_discography_where_every_album_is_a_duplicate_creates_nothing_and_costs_nothing(env):
    db = env["db"]
    _settings(db, discographies=1)
    assert _discography(env, "alice", "Muse", _albums("Muse", 3)).status_code == 201
    again = _discography(env, "alice", "Muse", _albums("Muse", 3))
    assert again.status_code == 201 and again.json()["count"] == 0  # idempotent, no second unit needed
    assert _used(db, "1001")["discography"] == 1


def test_rejected_discography_stops_counting(env):
    db = env["db"]
    _settings(db, discographies=1)
    created = _discography(env, "alice", "Muse", _albums("Muse", 2)).json()["created"]
    assert _discography(env, "alice", "Beck", _albums("Beck", 1)).status_code == 400
    for r in created:
        db.update_request_status(r["id"], RequestStatus.REJECTED)
    assert _used(db, "1001")["discography"] == 0
    assert _discography(env, "alice", "Beck", _albums("Beck", 1)).status_code == 201


def test_plain_batch_consumes_each_items_own_type_all_or_nothing(env):
    db = env["db"]
    _settings(db, tracks=2, albums=1)
    items = [
        {"item_type": "track", "title": "T1", "artist": "A"},
        {"item_type": "track", "title": "T2", "artist": "A"},
        {"item_type": "album", "title": "Al1", "artist": "B"},
    ]
    res = env["client"].post("/api/requests/batch", json={"requests": items}, headers=env["alice"])
    assert res.status_code == 201 and res.json()["count"] == 3
    assert _used(db, "1001") == {"track": 2, "album": 1, "discography": 0}
    more = [{"item_type": "track", "title": "T3", "artist": "A"}, {"item_type": "album", "title": "Al2", "artist": "B"}]
    refused = env["client"].post("/api/requests/batch", json={"requests": more}, headers=env["alice"])
    assert refused.status_code == 400
    assert refused.json()["detail"] == "Request quota reached for tracks (2 per 7 days)"
    assert _used(db, "1001") == {"track": 2, "album": 1, "discography": 0}


def test_plain_batch_with_artist_field_but_no_kind_is_not_a_discography(env):
    res = env["client"].post(
        "/api/requests/batch", json={"artist": "Muse", "requests": _albums("Muse", 2)}, headers=env["alice"]
    )
    assert res.status_code == 201
    assert {r["batch_kind"] for r in res.json()["created"]} == {None}
    assert _used(env["db"], "1001") == {"track": 0, "album": 2, "discography": 0}


def test_batch_requires_request_permission(env):
    env["db"].update_user_admin_fields("1001", {"permissions": 32})
    assert _discography(env, "alice", "Muse", _albums("Muse", 2)).status_code == 403


# --------------------------------------------------------------------------- auto-approve per type


def _statuses(db: Database, user_id: str) -> dict[str, str]:
    return {r["title"]: r["status"] for r in db.list_requests(user_id=user_id)}


def test_bit_4_approves_tracks_only(env):
    db = env["db"]
    db.update_user_admin_fields("1001", {"permissions": REQ | AUTO_TRACK})
    assert _single(env, "alice", "track", 0).json()["status"] == "processing"
    assert _single(env, "alice", "album", 0).json()["status"] == "pending"
    disco = _discography(env, "alice", "Muse", _albums("Muse", 2)).json()["created"]
    assert {r["status"] for r in disco} == {"pending"}


def test_bit_8_approves_albums_only(env):
    db = env["db"]
    db.update_user_admin_fields("1001", {"permissions": REQ | AUTO_ALBUM})
    assert _single(env, "alice", "album", 0).json()["status"] == "processing"
    assert _single(env, "alice", "track", 0).json()["status"] == "pending"
    disco = _discography(env, "alice", "Muse", _albums("Muse", 2)).json()["created"]
    assert {r["status"] for r in disco} == {"pending"}


def test_bit_64_approves_discography_only(env):
    db = env["db"]
    db.update_user_admin_fields("1001", {"permissions": REQ | AUTO_DISCO})
    disco = _discography(env, "alice", "Muse", _albums("Muse", 3)).json()["created"]
    assert {r["status"] for r in disco} == {"processing"}
    assert _single(env, "alice", "album", 0).json()["status"] == "pending"
    assert _single(env, "alice", "track", 0).json()["status"] == "pending"


def test_no_bits_means_everything_is_pending(env):
    assert _single(env, "alice", "track", 0).json()["status"] == "pending"
    assert _single(env, "alice", "album", 0).json()["status"] == "pending"
    disco = _discography(env, "alice", "Muse", _albums("Muse", 2)).json()["created"]
    assert {r["status"] for r in disco} == {"pending"}


def test_plain_batch_items_follow_their_own_bit(env):
    env["db"].update_user_admin_fields("1001", {"permissions": REQ | AUTO_TRACK})
    items = [
        {"item_type": "track", "title": "T1", "artist": "A"},
        {"item_type": "album", "title": "Al1", "artist": "B"},
    ]
    res = env["client"].post("/api/requests/batch", json={"requests": items}, headers=env["alice"])
    assert {r["title"]: r["status"] for r in res.json()["created"]} == {"T1": "processing", "Al1": "pending"}


def test_global_auto_approve_and_admin_approve_every_type(env, cfg):
    cfg.auto_approve_requests = True
    assert _single(env, "alice", "album", 0).json()["status"] == "processing"
    disco = _discography(env, "alice", "Muse", _albums("Muse", 2)).json()["created"]
    assert {r["status"] for r in disco} == {"processing"}
    cfg.auto_approve_requests = False
    assert _single(env, "admin", "track", 0).json()["status"] == "processing"
    assert _discography(env, "admin", "Beck", _albums("Beck", 2)).json()["created"][0]["status"] == "processing"


def test_account_endpoint_reports_the_same_auto_approve_rule(env):
    env["db"].update_user_admin_fields("1001", {"permissions": REQ | AUTO_ALBUM})
    body = env["client"].get("/api/account", headers=env["alice"]).json()
    assert body["auto_approve"] == {"tracks": False, "albums": True, "discographies": False}
    env["db"].update_user_admin_fields("1001", {"permissions": REQ | AUTO_TRACK})
    assert env["client"].get("/api/account", headers=env["alice"]).json()["auto_approve"] == {
        "tracks": True, "albums": False, "discographies": False,
    }


def test_mixes_style_submission_uses_track_quota_and_bit_4(db, cfg):
    _settings(db, tracks=1, albums=0)
    db.update_user_admin_fields("1001", {"permissions": REQ | AUTO_TRACK})
    user = db.get_user("1001")
    first = submit_track_request(db, cfg, user, "T1", "A", source="mix", item_type="track")
    assert first.status == RequestStatus.PROCESSING
    with pytest.raises(RequestRejected) as exc:
        submit_track_request(db, cfg, user, "T2", "A", source="mix", item_type="track")
    assert exc.value.code == "quota" and exc.value.status_code == 400
    assert exc.value.detail == "Request quota reached for tracks (1 per 7 days)"


# --------------------------------------------------------------------------- admins are unlimited


def test_admin_is_unlimited_for_every_type(env):
    _settings(env["db"], tracks=0, albums=0, discographies=0)
    env["db"].update_user_admin_fields("admin-1", {"quota_tracks": 0})
    for n in range(3):
        assert _single(env, "admin", "track", n).status_code == 201
        assert _single(env, "admin", "album", n).status_code == 201
    assert _discography(env, "admin", "Muse", _albums("Muse", 2)).status_code == 201
    assert _discography(env, "admin", "Beck", _albums("Beck", 2)).status_code == 201
    assert env["client"].get("/api/account", headers=env["admin"]).json()["quotas"]["tracks"] is None


# --------------------------------------------------------------------------- /api/account and /api/users/me agree


def test_account_used_counts_come_from_the_same_counting_function(env):
    db = env["db"]
    _settings(db, tracks=5, albums=5, discographies=2)
    _single(env, "alice", "track", 0)
    _single(env, "alice", "track", 1)
    _single(env, "alice", "album", 0)
    _discography(env, "alice", "Muse", _albums("Muse", 4))
    doc = env["client"].get("/api/account", headers=env["alice"]).json()["quotas"]
    assert doc["used"] == {"tracks": 2, "albums": 1, "discographies": 1}
    assert doc["tracks"] == 5 and doc["albums"] == 5 and doc["discographies"] == 2 and doc["window_days"] == 7
    stored = db.count_user_requests_by_type("1001", 7)
    assert doc["used"] == {"tracks": stored["track"], "albums": stored["album"], "discographies": stored["discography"]}
    # patching the counting function changes what the account reports: nothing is computed twice
    with patch.object(Database, "count_user_requests_by_type",
                      return_value={"track": 9, "album": 8, "discography": 7}) as counted:
        patched = env["client"].get("/api/account", headers=env["alice"]).json()["quotas"]["used"]
    assert counted.called and patched == {"tracks": 9, "albums": 8, "discographies": 7}


def test_admin_listing_usage_matches_account_usage(env):
    _single(env, "alice", "track", 0)
    _discography(env, "alice", "Muse", _albums("Muse", 2))
    listing = {u["username"]: u for u in env["client"].get("/api/admin/users", headers=env["admin"]).json()}
    account = env["client"].get("/api/account", headers=env["alice"]).json()["quotas"]["used"]
    assert listing["alice"]["usage"] == account == {"tracks": 1, "albums": 0, "discographies": 1}


def test_users_me_stays_coherent_with_per_type_quotas(env):
    db = env["db"]
    _settings(db, albums=4, window_days=7)
    db.update_user_admin_fields("1001", {"quota_albums": 6, "quota_window_days": 14})
    _single(env, "alice", "album", 0)
    _single(env, "alice", "track", 0)
    me = env["client"].get("/api/users/me", headers=env["alice"]).json()
    assert me["request_limit_quota"] == 6  # legacy alias of the album override
    assert me["quota_limit"] == 6 and me["rolling_days"] == 14
    assert me["active_requests"] == 1 and me["remaining_quota"] == 5
    assert me["quotas"] == env["client"].get("/api/account", headers=env["alice"]).json()["quotas"]


# --------------------------------------------------------------------------- concurrency (threaded, per type)


def _race(db: Database, work, workers: int = 8) -> list[Any]:
    real_create = db.create_request
    barrier = threading.Barrier(workers)
    results: list[Any] = []

    def slow_create(req):
        time.sleep(0.03)  # widen the check-then-insert window
        return real_create(req)

    def run(i: int) -> None:
        barrier.wait()
        try:
            results.append(work(i))
        except RequestRejected as exc:
            results.append(exc)

    with patch.object(db, "create_request", side_effect=slow_create):
        threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    return results


def test_concurrent_track_requests_cannot_exceed_quota(db, cfg):
    _settings(db, tracks=3)
    user = db.get_user("1001")
    results = _race(db, lambda i: submit_track_request(db, cfg, user, f"T{i}", f"A{i}", item_type="track"))
    refused = [r for r in results if isinstance(r, RequestRejected)]
    assert len(results) - len(refused) == 3 and len(refused) == 5
    assert all(r.status_code == 400 and "tracks (3 per 7 days)" in r.detail for r in refused)
    assert _used(db, "1001")["track"] == 3


def test_concurrent_album_requests_cannot_exceed_quota(db, cfg):
    _settings(db, albums=2)
    user = db.get_user("1001")
    results = _race(db, lambda i: submit_track_request(db, cfg, user, f"Al{i}", f"A{i}", item_type="album"))
    assert len([r for r in results if not isinstance(r, RequestRejected)]) == 2
    assert _used(db, "1001")["album"] == 2


def test_concurrent_discography_batches_cannot_exceed_quota(db, cfg):
    _settings(db, discographies=1, albums=0)
    user = db.get_user("1001")
    results = _race(
        db,
        lambda i: submit_batch_requests(db, cfg, user, _albums(f"Artist{i}", 3), kind="discography", artist=f"Artist{i}"),
        workers=6,
    )
    refused = [r for r in results if isinstance(r, RequestRejected)]
    assert len(results) - len(refused) == 1 and len(refused) == 5
    assert _used(db, "1001") == {"track": 0, "album": 0, "discography": 1}
    assert len(db.list_requests(user_id="1001")) == 3


def test_concurrent_mixed_batches_cannot_exceed_either_type(db, cfg):
    _settings(db, tracks=4, albums=2)
    user = db.get_user("1001")

    def work(i: int):
        items = [
            {"item_type": "track", "title": f"T{i}a", "artist": f"A{i}"},
            {"item_type": "track", "title": f"T{i}b", "artist": f"A{i}"},
            {"item_type": "album", "title": f"Al{i}", "artist": f"A{i}"},
        ]
        return submit_batch_requests(db, cfg, user, items)

    results = _race(db, work, workers=6)
    ok = [r for r in results if not isinstance(r, RequestRejected)]
    assert len(ok) == 2  # tracks allow 2 batches of 2, albums allow 2 of 1
    assert _used(db, "1001") == {"track": 4, "album": 2, "discography": 0}


# --------------------------------------------------------------------------- gateway forwarding


def test_gateway_forwards_discography_kind_and_artist_unchanged(db, tmp_path):
    cfg = Config(
        plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), role="gateway",
        internal_core_secret=SECRET, trackseerr_core_url="http://core.internal:5251",
    )
    client = _client(db, cfg)
    headers = _headers(db, cfg, "1001")
    payload = {"kind": "discography", "artist": "Muse", "requests": _albums("Muse", 3)}
    seen: dict[str, Any] = {}

    def fake_call(self, method, path, payload=None, query=None, user_info=None):
        seen.update(method=method, path=path, payload=payload, user=user_info)
        return httpx.Response(201, json={"created": [], "count": 0}, request=httpx.Request(method, "http://core/x"))

    with patch.object(CoreClient, "_json_call", fake_call):
        res = client.post("/api/requests/batch", json=payload, headers=headers)
    assert res.status_code == 201
    assert seen["method"] == "POST" and seen["path"] == "/api/requests/batch"
    assert seen["payload"]["kind"] == "discography" and seen["payload"]["artist"] == "Muse"
    assert [r["title"] for r in seen["payload"]["requests"]] == [f"Muse Album {i}" for i in range(3)]
    assert seen["user"]["id"] == "1001"
    assert db.list_requests() == []  # the gateway stores nothing itself


def test_gateway_relays_core_quota_refusal_and_validation(db, tmp_path):
    cfg = Config(
        plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), role="gateway",
        internal_core_secret=SECRET, trackseerr_core_url="http://core.internal:5251",
    )
    client = _client(db, cfg)
    headers = _headers(db, cfg, "1001")

    def refuse(self, method, path, payload=None, query=None, user_info=None):
        return httpx.Response(
            400, json={"detail": "Request quota reached for discographies (1 per 7 days)"},
            request=httpx.Request(method, "http://core/x"),
        )

    with patch.object(CoreClient, "_json_call", refuse):
        res = client.post(
            "/api/requests/batch", json={"kind": "discography", "artist": "Muse", "requests": _albums("Muse", 2)},
            headers=headers,
        )
    assert res.status_code == 400 and res.json()["detail"].startswith("Request quota reached for discographies")
    # an oversized batch is refused before it is ever forwarded
    with patch.object(CoreClient, "_json_call") as forwarded:
        big = client.post("/api/requests/batch", json={"kind": "discography", "artist": "Muse",
                                                       "requests": _albums("Muse", 51)}, headers=headers)
    assert big.status_code == 422 and not forwarded.called


def test_forwarded_request_to_core_uses_core_quotas_and_bits(db, tmp_path):
    from plex_playlist_sync import internal_auth

    internal_auth._nonce_cache.clear()
    cfg = Config(
        plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), role="core",
        internal_core_secret=SECRET, trackseerr_core_url="http://core.internal:5251",
    )
    client = _client(db, cfg)
    _settings(db, discographies=1)
    db.update_user_admin_fields("1001", {"permissions": REQ | AUTO_DISCO})
    body = json.dumps({"kind": "discography", "artist": "Muse", "requests": _albums("Muse", 2)}).encode()

    def post(payload: bytes):
        h = internal_auth.sign_assertion(SECRET, "POST", "/api/requests/batch", "1001", "alice", payload)
        h["Content-Type"] = "application/json"
        return client.post("/api/requests/batch", content=payload, headers=h)

    first = post(body)
    assert first.status_code == 201
    assert {r["status"] for r in first.json()["created"]} == {"processing"}
    assert {r["batch_kind"] for r in first.json()["created"]} == {"discography"}
    other = json.dumps({"kind": "discography", "artist": "Beck", "requests": _albums("Beck", 2)}).encode()
    assert post(other).status_code == 400


# --------------------------------------------------------------------------- migration v28


def _downgrade_to_v27(path: Path) -> None:
    d = Database(str(path))
    d.close()
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute("DROP INDEX IF EXISTS idx_requests_batch")
    conn.execute("ALTER TABLE music_requests DROP COLUMN batch_id")
    conn.execute("ALTER TABLE music_requests DROP COLUMN batch_kind")
    conn.execute("DELETE FROM schema_migrations WHERE version >= 28")
    conn.commit()
    conn.close()


def _seed_legacy_users(path: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(str(path))
    rows = [
        # id, username, permissions, legacy quota, legacy days, new overrides (tracks, albums, window)
        ("legacy-cap", "capped", 34, 7, 7, None, None, None),
        ("legacy-days", "dayed", 34, 12, 30, None, None, None),
        ("legacy-none", "plain", 34, None, 7, None, None, None),
        ("legacy-wins", "newstyle", 34, 5, 21, 9, None, 3),
        ("legacy-auto", "autoer", 34 | 4, None, 7, None, None, None),
        ("legacy-album", "albumer", 34 | 8, None, 7, None, None, None),
        ("legacy-disco", "discoer", 34 | 64, None, 7, None, None, None),
    ]
    for uid, name, perms, rl, rd, qt, qa, qw in rows:
        conn.execute(
            """
            INSERT INTO users (id, username, permissions, request_limit_quota, request_limit_days,
                               quota_tracks, quota_albums, quota_window_days)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (uid, name, perms, rl, rd, qt, qa, qw),
        )
    conn.commit()
    conn.close()


def test_v28_migrates_legacy_single_quota_window_and_auto_approve_bits(tmp_path, monkeypatch):
    monkeypatch.delenv("USER_REQUEST_QUOTA", raising=False)
    path = tmp_path / "legacy.db"
    _downgrade_to_v27(path)
    _seed_legacy_users(path)
    d = Database(str(path))  # opening runs migration v28 for real
    try:
        top = d.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        assert top == 38
        cols = {r[1] for r in d.conn.execute("PRAGMA table_info(music_requests)")}
        assert {"batch_id", "batch_kind"} <= cols
        ov = d.get_user_quota_overrides
        # the old single cap becomes the track AND album override; discographies keep the default
        assert ov("legacy-cap") == {"quota_tracks": 7, "quota_albums": 7, "quota_discographies": None,
                                    "quota_window_days": None}  # 7 days is the old default: not an override
        assert ov("legacy-days") == {"quota_tracks": 12, "quota_albums": 12, "quota_discographies": None,
                                     "quota_window_days": 30}
        assert ov("legacy-none") == {"quota_tracks": None, "quota_albums": None, "quota_discographies": None,
                                     "quota_window_days": None}
        # values already set by an admin on the new columns are never overwritten
        assert ov("legacy-wins") == {"quota_tracks": 9, "quota_albums": 5, "quota_discographies": None,
                                     "quota_window_days": 3}
        # bit 4 used to approve every type: its holders keep that and gain the explicit bits 8 and 64
        assert d.get_user("legacy-auto")["permissions"] == 34 | 4 | 8 | 64
        assert d.get_user("legacy-album")["permissions"] == 34 | 8  # albums only, as before
        assert d.get_user("legacy-disco")["permissions"] == 34 | 64
        assert d.get_user("legacy-none")["permissions"] == 34
        assert d.get_account_settings()["default_quota_tracks"] == 25
        assert effective_quota_limits(d, "legacy-cap") == {"tracks": 7, "albums": 7, "discographies": 1,
                                                          "window_days": 7}
        # the legacy PUT/GET fields now read from the new columns
        assert d.get_user("legacy-days")["request_limit_quota"] == 12
        assert d.get_user("legacy-days")["request_limit_days"] == 30
    finally:
        d.close()


def test_v28_is_idempotent_when_run_again(tmp_path, monkeypatch):
    monkeypatch.delenv("USER_REQUEST_QUOTA", raising=False)
    path = tmp_path / "again.db"
    _downgrade_to_v27(path)
    _seed_legacy_users(path)
    d = Database(str(path))
    try:
        before = d.get_user("legacy-auto")["permissions"]
        d._migration_v28(d.conn.cursor())  # a second run must not change anything
        d.conn.commit()
        assert d.get_user("legacy-auto")["permissions"] == before
        assert d.get_user_quota_overrides("legacy-wins")["quota_tracks"] == 9
    finally:
        d.close()


@pytest.mark.parametrize(
    "env_value,expected",
    [("40", 40), ("25", 25), ("0", 25), ("abc", 25), ("", 25)],
)
def test_v28_seeds_defaults_from_legacy_env_quota(tmp_path, monkeypatch, env_value, expected):
    monkeypatch.setenv("USER_REQUEST_QUOTA", env_value)
    path = tmp_path / "env.db"
    _downgrade_to_v27(path)
    d = Database(str(path))
    try:
        settings = d.get_account_settings()
        assert settings["default_quota_tracks"] == expected
        assert settings["default_quota_albums"] == (expected if expected != 25 else 10)
        assert settings["default_quota_discographies"] == 1
    finally:
        d.close()


def test_v28_does_not_override_defaults_an_admin_already_changed(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_REQUEST_QUOTA", "40")
    path = tmp_path / "custom.db"
    _downgrade_to_v27(path)
    d = Database(str(path))
    d.update_account_settings({"default_quota_tracks": 12})
    d.conn.execute("DELETE FROM schema_migrations WHERE version >= 28")
    d.conn.commit()
    d.close()
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute("DROP INDEX IF EXISTS idx_requests_batch")
    conn.execute("ALTER TABLE music_requests DROP COLUMN batch_id")
    conn.execute("ALTER TABLE music_requests DROP COLUMN batch_kind")
    conn.commit()
    conn.close()
    d2 = Database(str(path))
    try:
        assert d2.get_account_settings()["default_quota_tracks"] == 12
    finally:
        d2.close()


def test_fresh_database_has_batch_columns_and_defaults(db):
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(music_requests)")}
    assert {"batch_id", "batch_kind"} <= cols
    assert db.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 38
    created = db.create_request(MusicRequest(id="r", user_id="1001", item_type="album", title="t", artist="a"))
    assert created["batch_id"] is None and created["batch_kind"] is None

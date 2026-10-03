"""Tests for tailored mixes: Deezer parsing, compilation, generate/sync, quota, worker and API."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient

from plex_playlist_sync.api.app import create_app
from plex_playlist_sync.api.dependencies import (
    get_config,
    get_db,
    get_discovery_client,
    get_plex_client,
)
from plex_playlist_sync.auth import create_session_token, get_or_create_secret_key
from plex_playlist_sync.clients.discovery import DiscoveryClient
from plex_playlist_sync.config import Config
from plex_playlist_sync.mix_worker import MixWorker, is_due
from plex_playlist_sync.models import RequestStatus, SyncResult, UserPermission
from plex_playlist_sync.storage import Database
from plex_playlist_sync.tailored_mixes import (
    InsufficientHistoryError,
    compile_user_mix,
    generate_and_sync,
)

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------


class FakeDiscovery:
    """Seeds A, B -> related artists R-A1..., each with deterministic top tracks."""

    def __init__(self, related_count=3, tracks_per=5, extra_top=None):
        self.related_count = related_count
        self.tracks_per = tracks_per
        self.extra_top = extra_top or {}

    def search_artist(self, name):
        return {"id": f"deezer:artist:{sum(ord(c) for c in name) + 1000}", "name": name}

    def get_related_artists(self, artist_id, limit=20):
        return [{"id": f"{artist_id}{i}", "name": f"Rel{artist_id}-{i}"} for i in range(self.related_count)]

    def get_artist_top_tracks(self, artist_id, limit=10):
        if artist_id in self.extra_top:
            return self.extra_top[artist_id]
        return [
            {"title": f"T{artist_id}-{i}", "artist": f"Rel{artist_id}", "album": f"Alb{i}"}
            for i in range(self.tracks_per)
        ]


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def config(tmp_path):
    return Config(plex_url="http://plex", plex_token="tok", data_dir=str(tmp_path))


@pytest.fixture
def users(db):
    return {
        "admin": db.upsert_user("u-admin", "ronadmin", "a@x.io", is_admin=True),
        "alice": db.upsert_user("1001", "Alice", "al@x.io", is_admin=False),
        "bob": db.upsert_user("u-bob", "bob", "b@x.io", is_admin=False),
    }


def add_listens(db, user_id, artist, titles, days_ago=1):
    for i, title in enumerate(titles):
        db.insert_listen(
            user_id,
            artist=artist,
            title=title,
            album="Album",
            played_at=NOW - timedelta(days=days_ago, minutes=i),
            source="plex_history",
        )


def seed_history(db, user_id, n_artists=2, tracks=6):
    for a in range(n_artists):
        add_listens(db, user_id, f"Seed{a}", [f"Fav{a}-{i}" for i in range(tracks)])


def make_mix(db, user_id, **kw):
    kw.setdefault("mix_type", "discover_weekly")
    kw.setdefault("name", "Test Mix")
    return db.create_mix_config(user_id, **kw)


# ---------------------------------------------------------------------------
# Deezer parsing
# ---------------------------------------------------------------------------


def _resp(body, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


class TestDeezerParsing:
    def test_related_artists_parsed_and_cached(self):
        client = DiscoveryClient()
        client.session = MagicMock()
        client.session.get.return_value = _resp(
            {"data": [{"id": 5, "name": "Foo"}, {"id": 6, "name": " Bar "}, {"name": "NoId"}, {"id": 7, "name": ""}]}
        )
        out = client.get_related_artists("deezer:artist:42", limit=5)
        assert out == [{"id": "5", "name": "Foo"}, {"id": "6", "name": "Bar"}]
        assert "artist/42/related" in client.session.get.call_args[0][0]
        client.get_related_artists("42", limit=5)
        assert client.session.get.call_count == 1

    def test_top_tracks_parsed(self):
        client = DiscoveryClient()
        client.session = MagicMock()
        client.session.get.return_value = _resp(
            {
                "data": [
                    {"title": "Song", "artist": {"name": "Art"}, "album": {"title": "Alb"}},
                    {"title": "Bare"},
                    {"title": ""},
                ]
            }
        )
        out = client.get_artist_top_tracks("42", limit=10)
        assert out == [
            {"title": "Song", "artist": "Art", "album": "Alb"},
            {"title": "Bare", "artist": "Unknown Artist", "album": None},
        ]
        assert "artist/42/top" in client.session.get.call_args[0][0]

    def test_network_error_and_bad_id_return_empty(self):
        client = DiscoveryClient()
        client.session = MagicMock()
        client.session.get.side_effect = requests.ConnectionError("down")
        assert client.get_related_artists("42") == []
        assert client.get_artist_top_tracks("42") == []
        assert client.get_related_artists("not-a-number") == []
        client.session.get.side_effect = None
        client.session.get.return_value = _resp({}, status=500)
        assert client.get_artist_top_tracks("43") == []


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


class TestCompile:
    @pytest.mark.parametrize("ratio,n_disc", [(0.0, 0), (0.5, 10), (1.0, 20)])
    def test_blend_ratio_exact_counts(self, db, users, ratio, n_disc):
        uid = users["alice"]["id"]
        seed_history(db, uid, n_artists=2, tracks=15)
        mix = make_mix(db, uid, track_count=20, discovery_ratio=ratio)
        tracks = compile_user_mix(db, FakeDiscovery(related_count=4, tracks_per=5), mix)
        assert len(tracks) == 20
        assert sum(t.origin == "discovery" for t in tracks) == n_disc
        assert sum(t.origin == "familiar" for t in tracks) == 20 - n_disc

    def test_interleave_is_deterministic_and_spread(self, db, users):
        uid = users["alice"]["id"]
        seed_history(db, uid, n_artists=2, tracks=15)
        mix = make_mix(db, uid, track_count=20, discovery_ratio=0.5)
        a = compile_user_mix(db, FakeDiscovery(), mix)
        b = compile_user_mix(db, FakeDiscovery(), mix)
        assert [(t.artist, t.title) for t in a] == [(t.artist, t.title) for t in b]
        origins = [t.origin for t in a]
        assert "discovery" in origins[:3] and "familiar" in origins[:3]

    def test_topup_when_discovery_short(self, db, users):
        uid = users["alice"]["id"]
        seed_history(db, uid, n_artists=2, tracks=15)
        mix = make_mix(db, uid, track_count=20, discovery_ratio=0.5)
        tracks = compile_user_mix(db, FakeDiscovery(related_count=1, tracks_per=2), mix)
        disc = [t for t in tracks if t.origin == "discovery"]
        assert len(disc) == 4  # 2 seeds * 1 related * 2 tracks
        assert len(tracks) == 20

    def test_topup_when_familiar_short(self, db, users):
        uid = users["alice"]["id"]
        seed_history(db, uid, n_artists=2, tracks=2)  # only 4 familiar tracks
        mix = make_mix(db, uid, track_count=20, discovery_ratio=0.2)
        tracks = compile_user_mix(db, FakeDiscovery(related_count=4, tracks_per=5), mix)
        assert sum(t.origin == "familiar" for t in tracks) == 4
        assert len(tracks) == 20

    def test_heard_excluded_and_dedup(self, db, users):
        uid = users["alice"]["id"]
        add_listens(db, uid, "Seed0", ["Fav"])
        # Seed0 id = sum(ord)+1000; first related track: Rel<id>0 ... build duplicates + heard
        disc = FakeDiscovery(related_count=1, tracks_per=0)
        sid = disc.search_artist("Seed0")["id"].rsplit(":", 1)[-1]
        rid = f"{sid}0"
        disc.extra_top[rid] = [
            {"title": "Heard", "artist": "Other", "album": None},
            {"title": "New", "artist": "Other", "album": None},
            {"title": "new", "artist": "OTHER", "album": None},
        ]
        add_listens(db, uid, "Other", ["Heard"], days_ago=40)
        mix = make_mix(db, uid, track_count=10, discovery_ratio=1.0)
        tracks = compile_user_mix(db, disc, mix)
        disc_titles = [t.title for t in tracks if t.origin == "discovery"]
        assert disc_titles == ["New"]

    def test_seed_artists_excluded_from_discover_weekly_only(self, db, users):
        uid = users["alice"]["id"]
        add_listens(db, uid, "Seed0", ["Fav"])
        disc = FakeDiscovery(related_count=1, tracks_per=0)
        sid = disc.search_artist("Seed0")["id"].rsplit(":", 1)[-1]
        disc.extra_top[f"{sid}0"] = [
            {"title": "Own", "artist": "Seed0", "album": None},
            {"title": "Other", "artist": "Someone", "album": None},
        ]
        dw = compile_user_mix(db, disc, make_mix(db, uid, track_count=5, discovery_ratio=1.0))
        assert [t.title for t in dw if t.origin == "discovery"] == ["Other"]
        db_mix = make_mix(db, uid, mix_type="daily_blend", name="DB", track_count=5, discovery_ratio=1.0)
        blend = compile_user_mix(db, disc, db_mix)
        assert {t.title for t in blend if t.origin == "discovery"} == {"Own", "Other"}

    def test_artist_radio_requires_seed(self, db, users):
        uid = users["alice"]["id"]
        with pytest.raises(ValueError):
            make_mix(db, uid, mix_type="artist_radio", name="R")
        with pytest.raises(ValueError):
            compile_user_mix(
                db, FakeDiscovery(), {"id": "x", "user_id": uid, "mix_type": "artist_radio", "seed_artist": None}
            )

    def test_artist_radio_works_without_history(self, db, users):
        uid = users["alice"]["id"]
        mix = make_mix(db, uid, mix_type="artist_radio", name="R", seed_artist="Seed0", track_count=6, discovery_ratio=1.0)
        tracks = compile_user_mix(db, FakeDiscovery(related_count=2, tracks_per=3), mix)
        assert len(tracks) == 6
        assert all(t.origin == "discovery" for t in tracks)

    def test_empty_history_raises(self, db, users):
        mix = make_mix(db, users["alice"]["id"])
        with pytest.raises(InsufficientHistoryError):
            compile_user_mix(db, FakeDiscovery(), mix)

    def test_daily_blend_caps_window_at_three_days(self, db, users):
        uid = users["alice"]["id"]
        add_listens(db, uid, "OldArtist", ["x"], days_ago=10)
        mix = make_mix(db, uid, mix_type="daily_blend", name="DB", seed_window_days=14)
        with pytest.raises(InsufficientHistoryError):
            compile_user_mix(db, FakeDiscovery(), mix)
        wk = make_mix(db, uid, name="W", seed_window_days=14)
        assert compile_user_mix(db, FakeDiscovery(), wk)


# ---------------------------------------------------------------------------
# generate_and_sync
# ---------------------------------------------------------------------------


def avail_fn(missing_titles=()):
    def _fn(db, artist_name=None, album_title=None, track_title=None, foreign_id=None):
        return {"status": "missing" if track_title in missing_titles else "available"}

    return _fn


def fake_plex(results=None):
    plex = MagicMock()
    plex.sync_playlist_to_users.return_value = results or [
        SyncResult(playlist_name="x", total_tracks=1, matched_tracks=1, missing_tracks=0, success=True)
    ]
    return plex


@pytest.fixture
def gen_env(db, users, config):
    uid = users["alice"]["id"]
    seed_history(db, uid, n_artists=1, tracks=10)
    return SimpleNamespace(db=db, uid=uid, config=config, disc=FakeDiscovery(related_count=2, tracks_per=5))


def run_generate(env, mix, plex, missing=()):
    with patch("plex_playlist_sync.tailored_mixes.get_item_availability", side_effect=avail_fn(missing)), patch(
        "plex_playlist_sync.request_submission.acquisition_coordinator"
    ) as coord:
        coord.search_and_grab.return_value = {"success": False}
        res = generate_and_sync(env.db, plex, env.disc, mix, env.config)
    return res, coord


class TestGenerate:
    def test_availability_split_and_sync(self, gen_env):
        mix = make_mix(gen_env.db, gen_env.uid, track_count=10, discovery_ratio=0.5)
        plex = fake_plex()
        res, _ = run_generate(gen_env, mix, plex, missing={"Fav0-0", "Fav0-1"})
        assert res.total == 10
        assert res.available == 8 and res.missing == 2
        assert res.synced and res.sync_error is None
        assert res.acquisitions_queued == 0
        statuses = {t["title"]: t["status"] for t in res.tracks}
        assert statuses["Fav0-0"] == "missing"
        playlist = plex.sync_playlist_to_users.call_args.kwargs["playlist"]
        assert playlist.name == "Test Mix"
        assert len(playlist.tracks) == 8
        assert plex.sync_playlist_to_users.call_args.kwargs["target_usernames"] == ["Alice"]
        stored = json.loads(gen_env.db.get_mix_config(mix["id"])["last_result_json"])
        assert stored["total"] == 10
        assert gen_env.db.get_mix_config(mix["id"])["last_generated_at"]

    def test_protected_error_reported_not_raised(self, gen_env):
        mix = make_mix(gen_env.db, gen_env.uid, track_count=10)
        plex = fake_plex(
            [SyncResult("x", 5, 0, 0, success=False, error="Protected: playlist is owned by Plexamp")]
        )
        res, _ = run_generate(gen_env, mix, plex)
        assert res.synced is False
        assert res.sync_error.startswith("Protected:")

    def test_plex_exception_reported(self, gen_env):
        mix = make_mix(gen_env.db, gen_env.uid, track_count=10)
        plex = MagicMock()
        plex.sync_playlist_to_users.side_effect = requests.ConnectionError("plex down")
        res, _ = run_generate(gen_env, mix, plex)
        assert res.synced is False and res.sync_error == "Plex sync failed (ConnectionError)"

    def test_insufficient_history_propagates(self, db, users, config):
        mix = make_mix(db, users["bob"]["id"])
        with pytest.raises(InsufficientHistoryError):
            generate_and_sync(db, fake_plex(), FakeDiscovery(), mix, config)

    def test_auto_acquire_creates_approved_requests_with_quota(self, gen_env):
        db = gen_env.db
        db.update_user_governance(gen_env.uid, permissions=int(UserPermission.DEFAULT | UserPermission.AUTO_APPROVE))
        all_missing = {f"Fav0-{i}" for i in range(10)}
        db.upsert_quality_profile({"id": "qp-1", "name": "Lossless"})
        mix = make_mix(
            db, gen_env.uid, track_count=10, discovery_ratio=0.0, auto_acquire_missing=True,
            max_weekly_acquisitions=3, quality_profile_id="qp-1",
        )
        res, _ = run_generate(gen_env, mix, fake_plex(), missing=all_missing)
        assert res.acquisitions_queued == 3
        assert res.quota_remaining == 0
        assert sum(t["status"] == "queued" for t in res.tracks) == 3
        assert sum(t["status"] == "missing" for t in res.tracks) == 7
        since = (NOW - timedelta(days=7)).isoformat()
        assert db.count_mix_acquisitions_since(gen_env.uid, since) == 3
        reqs = [r for r in db.list_requests() if r["user_id"] == gen_env.uid]
        assert len(reqs) == 3
        assert all(r["status"] == RequestStatus.PROCESSING.value and r["item_type"] == "track" for r in reqs)
        # Non-admin owners cannot choose a quality profile: it is dropped.
        assert all(r["quality_profile_id"] is None for r in reqs)

        # A second run must not exceed the quota.
        res2, _ = run_generate(gen_env, mix, fake_plex(), missing=all_missing)
        assert res2.acquisitions_queued == 0
        assert db.count_mix_acquisitions_since(gen_env.uid, since) == 3

    def test_quota_counted_across_users_mixes(self, gen_env):
        db = gen_env.db
        all_missing = {f"Fav0-{i}" for i in range(10)}
        m1 = make_mix(db, gen_env.uid, name="One", track_count=10, discovery_ratio=0.0,
                      auto_acquire_missing=True, max_weekly_acquisitions=4)
        m2 = make_mix(db, gen_env.uid, name="Two", mix_type="daily_blend", track_count=10, discovery_ratio=0.0,
                      auto_acquire_missing=True, max_weekly_acquisitions=4)
        r1, _ = run_generate(gen_env, m1, fake_plex(), missing=all_missing)
        r2, _ = run_generate(gen_env, m2, fake_plex(), missing=all_missing)
        assert r1.acquisitions_queued == 4
        assert r2.acquisitions_queued == 0
        assert db.count_mix_acquisitions_since(gen_env.uid, (NOW - timedelta(days=7)).isoformat()) == 4

    def test_no_acquisition_when_disabled(self, gen_env):
        mix = make_mix(gen_env.db, gen_env.uid, track_count=10, discovery_ratio=0.0)
        res, coord = run_generate(gen_env, mix, fake_plex(), missing={f"Fav0-{i}" for i in range(10)})
        assert res.acquisitions_queued == 0
        coord.search_and_grab.assert_not_called()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class TestWorker:
    def test_due_logic_per_type(self):
        now = NOW
        def row(t, ago, enabled=True):
            return {"id": "m", "mix_type": t, "enabled": enabled,
                    "last_generated_at": (now - ago).isoformat(timespec="seconds") if ago is not None else None}
        assert is_due(row("discover_weekly", None), now)
        assert not is_due(row("discover_weekly", timedelta(days=6, hours=23)), now)
        assert is_due(row("discover_weekly", timedelta(days=7, seconds=5)), now)
        assert not is_due(row("daily_blend", timedelta(hours=23)), now)
        assert is_due(row("daily_blend", timedelta(days=1, seconds=5)), now)
        assert not is_due(row("artist_radio", timedelta(hours=2)), now)
        assert is_due(row("artist_radio", timedelta(days=2)), now)
        assert not is_due(row("daily_blend", None, enabled=False), now)

    def test_run_iteration_isolates_errors(self, db, users, config):
        uid = users["alice"]["id"]
        bad = make_mix(db, uid, name="Bad", mix_type="daily_blend")
        good = make_mix(db, uid, name="Good")
        calls = []

        def fake_gen(db_, plex, disc, row, cfg):
            calls.append(row["id"])
            if row["id"] == bad["id"]:
                raise RuntimeError("boom")

        worker = MixWorker()
        with patch("plex_playlist_sync.mix_worker.generate_and_sync", side_effect=fake_gen):
            out = worker.run_iteration(db, config, None, FakeDiscovery())
        assert set(calls) == {bad["id"], good["id"]}
        assert out == {"due": 2, "generated": 1, "errors": 1}

    def test_run_iteration_skips_not_due_and_insufficient(self, db, users, config):
        uid = users["alice"]["id"]
        make_mix(db, uid, name="Empty")  # no history -> InsufficientHistoryError, skipped not errored
        worker = MixWorker()
        out = worker.run_iteration(db, config, None, FakeDiscovery())
        assert out == {"due": 1, "generated": 0, "errors": 0}
        recent = make_mix(db, uid, name="Recent", mix_type="daily_blend")
        db.record_mix_result(recent["id"], "{}")
        with patch("plex_playlist_sync.mix_worker.generate_and_sync") as gen:
            worker.run_iteration(db, config, None, FakeDiscovery(), now=NOW + timedelta(hours=1))
        assert recent["id"] not in [c.args[3]["id"] for c in gen.call_args_list]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def auth_headers(user, db, config):
    secret = get_or_create_secret_key(data_dir=config.data_dir)
    token = create_session_token(
        user_id=user["id"], username=user["username"], is_admin=user["is_admin"], secret_key=secret
    )
    db.create_session(token, user["id"], {"auth": "test"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api(db, config, users):
    app = create_app(db=db, config=config)
    plex = fake_plex()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_discovery_client] = lambda: FakeDiscovery()
    app.dependency_overrides[get_plex_client] = lambda: plex
    client = TestClient(app)
    client.plex = plex
    client.h = {k: auth_headers(u, db, config) for k, u in users.items()}
    return client


class TestApi:
    def test_crud(self, api, users):
        h = api.h["alice"]
        r = api.post("/api/mixes", json={"mix_type": "discover_weekly"}, headers=h)
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Discover Weekly · TrackSeerr"
        assert body["user_id"] == users["alice"]["id"]
        assert "last_result_json" not in body and body["last_generated_at"] is None
        mid = body["id"]
        assert [m["id"] for m in api.get("/api/mixes", headers=h).json()] == [mid]
        up = api.put(f"/api/mixes/{mid}", json={"track_count": 40, "enabled": False}, headers=h)
        assert up.status_code == 200 and up.json()["track_count"] == 40 and up.json()["enabled"] is False
        assert api.delete(f"/api/mixes/{mid}", headers=h).status_code == 204
        assert api.get("/api/mixes", headers=h).json() == []

    def test_validation_errors_422(self, api):
        h = api.h["alice"]
        assert api.post("/api/mixes", json={"mix_type": "artist_radio"}, headers=h).status_code == 422
        assert api.post("/api/mixes", json={"mix_type": "discover_weekly", "track_count": 3}, headers=h).status_code == 422
        assert api.post("/api/mixes", json={"mix_type": "nope"}, headers=h).status_code == 422
        ok = api.post("/api/mixes", json={"mix_type": "discover_weekly"}, headers=h).json()
        assert api.put(f"/api/mixes/{ok['id']}", json={"discovery_ratio": 2}, headers=h).status_code == 422

    def test_unknown_quality_profile_422(self, api):
        r = api.post("/api/mixes", json={"mix_type": "discover_weekly", "quality_profile_id": "nope"}, headers=api.h["admin"])
        assert r.status_code == 422

    def test_artist_radio_default_name(self, api):
        r = api.post("/api/mixes", json={"mix_type": "artist_radio", "seed_artist": "Radiohead"}, headers=api.h["alice"])
        assert r.status_code == 201 and r.json()["name"] == "Radiohead Radio · TrackSeerr"

    def test_requires_auth(self, api):
        assert api.get("/api/mixes").status_code == 401

    def test_other_users_mix_404_for_non_admin(self, api, db, users):
        mix = make_mix(db, users["alice"]["id"])
        h = api.h["bob"]
        mid = mix["id"]
        assert api.put(f"/api/mixes/{mid}", json={"name": "x"}, headers=h).status_code == 404
        assert api.delete(f"/api/mixes/{mid}", headers=h).status_code == 404
        assert api.post(f"/api/mixes/{mid}/preview", headers=h).status_code == 404
        assert api.post(f"/api/mixes/{mid}/generate", headers=h).status_code == 404
        assert api.get(f"/api/mixes/{mid}/result", headers=h).status_code == 404
        assert api.get("/api/mixes", headers=h).json() == []
        assert api.get("/api/mixes", params={"user_id": users["alice"]["id"]}, headers=h).json() == []

    def test_non_admin_cannot_create_for_other_user(self, api, users):
        r = api.post("/api/mixes", json={"mix_type": "discover_weekly", "user_id": users["alice"]["id"]}, headers=api.h["bob"])
        assert r.json()["user_id"] == users["bob"]["id"]

    def test_admin_override(self, api, db, users):
        mix = make_mix(db, users["alice"]["id"])
        h = api.h["admin"]
        assert api.get("/api/mixes", headers=h).json() == []
        listed = api.get("/api/mixes", params={"user_id": users["alice"]["id"]}, headers=h).json()
        assert [m["id"] for m in listed] == [mix["id"]]
        created = api.post("/api/mixes", json={"mix_type": "daily_blend", "user_id": users["bob"]["id"]}, headers=h)
        assert created.status_code == 201 and created.json()["user_id"] == users["bob"]["id"]
        assert api.put(f"/api/mixes/{mix['id']}", json={"name": "Renamed"}, headers=h).json()["name"] == "Renamed"
        assert api.delete(f"/api/mixes/{mix['id']}", headers=h).status_code == 204

    def test_forwarded_principal_is_not_admin(self, db, users):
        from plex_playlist_sync.api.routes.mixes import _is_admin

        assert _is_admin({"is_admin": True}) is True
        assert _is_admin({"is_admin": True, "forwarded": True}) is False

    def test_preview(self, api, db, users):
        seed_history(db, users["alice"]["id"], n_artists=1, tracks=8)
        mix = make_mix(db, users["alice"]["id"], track_count=10)
        r = api.post(f"/api/mixes/{mix['id']}/preview", headers=api.h["alice"])
        assert r.status_code == 200
        tracks = r.json()["tracks"]
        assert len(tracks) == 10
        assert set(tracks[0]) == {"artist", "title", "album", "origin"}
        api.plex.sync_playlist_to_users.assert_not_called()

    def test_preview_empty_history_409(self, api, db, users):
        mix = make_mix(db, users["alice"]["id"])
        assert api.post(f"/api/mixes/{mix['id']}/preview", headers=api.h["alice"]).status_code == 409

    def test_generate_202_then_result(self, api, db, users):
        seed_history(db, users["alice"]["id"], n_artists=1, tracks=8)
        mix = make_mix(db, users["alice"]["id"], track_count=10)
        h = api.h["alice"]
        assert api.get(f"/api/mixes/{mix['id']}/result", headers=h).status_code == 404
        with patch("plex_playlist_sync.tailored_mixes.get_item_availability", side_effect=avail_fn()):
            r = api.post(f"/api/mixes/{mix['id']}/generate", headers=h)
        assert r.status_code == 202 and r.json() == {"status": "queued"}
        res = api.get(f"/api/mixes/{mix['id']}/result", headers=h)
        assert res.status_code == 200
        body = res.json()
        assert body["mix_id"] == mix["id"] and body["total"] == 10 and body["synced"] is True
        assert api.plex.sync_playlist_to_users.called

    def test_generate_empty_history_409(self, api, db, users):
        mix = make_mix(db, users["alice"]["id"])
        assert api.post(f"/api/mixes/{mix['id']}/generate", headers=api.h["alice"]).status_code == 409


# ---------------------------------------------------------------------------
# Request policy, caps and rate limits (security)
# ---------------------------------------------------------------------------


def _missing(env):
    return {f"Fav0-{i}" for i in range(10)}


class TestMixRequestPolicy:
    def test_without_auto_approve_requests_are_pending(self, gen_env):
        db = gen_env.db
        mix = make_mix(db, gen_env.uid, track_count=10, discovery_ratio=0.0, auto_acquire_missing=True,
                       max_weekly_acquisitions=3)
        res, coord = run_generate(gen_env, mix, fake_plex(), missing=_missing(gen_env))
        assert res.acquisitions_queued == 3
        reqs = [r for r in db.list_requests() if r["user_id"] == gen_env.uid]
        assert len(reqs) == 3 and all(r["status"] == RequestStatus.PENDING.value for r in reqs)
        coord.search_and_grab.assert_not_called()

    def test_request_quota_respected_through_mixes(self, gen_env):
        db = gen_env.db
        db.update_user_governance(gen_env.uid, request_limit_quota=2)
        mix = make_mix(db, gen_env.uid, track_count=10, discovery_ratio=0.0, auto_acquire_missing=True,
                       max_weekly_acquisitions=10)
        res, _ = run_generate(gen_env, mix, fake_plex(), missing=_missing(gen_env))
        assert res.acquisitions_queued == 2
        assert len([r for r in db.list_requests() if r["user_id"] == gen_env.uid]) == 2

    def test_existing_active_requests_count_against_quota(self, gen_env):
        db = gen_env.db
        db.update_user_governance(gen_env.uid, request_limit_quota=1)
        from plex_playlist_sync.models import MusicRequest

        db.create_request(MusicRequest(id="req-pre", user_id=gen_env.uid, item_type="track", title="Z",
                                       artist="Z", status=RequestStatus.PENDING))
        mix = make_mix(db, gen_env.uid, track_count=10, discovery_ratio=0.0, auto_acquire_missing=True)
        res, _ = run_generate(gen_env, mix, fake_plex(), missing=_missing(gen_env))
        assert res.acquisitions_queued == 0

    def test_max_weekly_clamped_to_user_quota_for_non_admin(self, gen_env):
        db = gen_env.db
        db.update_user_governance(gen_env.uid, request_limit_quota=2)
        mix = make_mix(db, gen_env.uid, track_count=10, discovery_ratio=0.0, auto_acquire_missing=True,
                       max_weekly_acquisitions=50)
        res, _ = run_generate(gen_env, mix, fake_plex(), missing=_missing(gen_env))
        assert res.acquisitions_queued == 2 and res.quota_remaining == 0

    def test_concurrent_generates_do_not_exceed_cap(self, gen_env):
        import threading

        db = gen_env.db
        mixes = [
            make_mix(db, gen_env.uid, name=f"M{i}", track_count=10, discovery_ratio=0.0,
                     auto_acquire_missing=True, max_weekly_acquisitions=3)
            for i in range(6)
        ]
        errors = []

        def work(m):
            try:
                generate_and_sync(db, fake_plex(), gen_env.disc, m, gen_env.config)
            except Exception as exc:  # surface thread failures to the assertion below
                errors.append(exc)

        with patch("plex_playlist_sync.tailored_mixes.get_item_availability", side_effect=avail_fn(_missing(gen_env))), \
                patch("plex_playlist_sync.request_submission.acquisition_coordinator"):
            threads = [threading.Thread(target=work, args=(m,)) for m in mixes]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        assert errors == []
        assert db.count_mix_acquisitions_since(gen_env.uid, (NOW - timedelta(days=7)).isoformat()) == 3
        assert len([r for r in db.list_requests() if r["user_id"] == gen_env.uid]) == 3


class TestMixApiLimits:
    def test_eleventh_mix_is_409_for_non_admin(self, api):
        h = api.h["alice"]
        for _ in range(10):
            assert api.post("/api/mixes", json={"mix_type": "discover_weekly"}, headers=h).status_code == 201
        assert api.post("/api/mixes", json={"mix_type": "discover_weekly"}, headers=h).status_code == 409
        assert api.post("/api/mixes", json={"mix_type": "discover_weekly"}, headers=api.h["bob"]).status_code == 201

    def test_non_admin_quality_profile_ignored_and_returned_null(self, api, db):
        db.upsert_quality_profile({"id": "qp-1", "name": "Lossless"})
        h = api.h["alice"]
        r = api.post("/api/mixes", json={"mix_type": "discover_weekly", "quality_profile_id": "qp-1"}, headers=h)
        assert r.status_code == 201 and r.json()["quality_profile_id"] is None
        up = api.put(f"/api/mixes/{r.json()['id']}", json={"quality_profile_id": "qp-1"}, headers=h)
        assert up.status_code == 200 and up.json()["quality_profile_id"] is None
        adm = api.post("/api/mixes", json={"mix_type": "discover_weekly", "quality_profile_id": "qp-1"},
                       headers=api.h["admin"])
        assert adm.json()["quality_profile_id"] == "qp-1"

    def test_max_weekly_clamped_on_write(self, api, db, users):
        db.update_user_governance(users["alice"]["id"], request_limit_quota=4)
        r = api.post("/api/mixes", json={"mix_type": "discover_weekly", "max_weekly_acquisitions": 50},
                     headers=api.h["alice"])
        assert r.json()["max_weekly_acquisitions"] == 4

    def test_generate_rate_limit_and_in_flight(self, api, db, users):
        from plex_playlist_sync.api.routes import mixes as mixes_route

        seed_history(db, users["alice"]["id"])
        h = api.h["alice"]
        mid = api.post("/api/mixes", json={"mix_type": "discover_weekly"}, headers=h).json()["id"]
        with patch.object(mixes_route, "_run_generation"):
            assert api.post(f"/api/mixes/{mid}/generate", headers=h).status_code == 202
            # still "in flight" (the patched runner never clears it)
            assert api.post(f"/api/mixes/{mid}/generate", headers=h).status_code == 409
            mixes_route._generating.discard(mid)
            assert api.post(f"/api/mixes/{mid}/generate", headers=h).status_code == 429
            # admins are exempt from the cooldown
            mixes_route._generating.discard(mid)
            assert api.post(f"/api/mixes/{mid}/generate", headers=api.h["admin"]).status_code == 202

    def test_generate_rate_limit_uses_last_generated_at(self, api, db, users):
        from plex_playlist_sync.api.routes import mixes as mixes_route

        seed_history(db, users["alice"]["id"])
        h = api.h["alice"]
        mid = api.post("/api/mixes", json={"mix_type": "discover_weekly"}, headers=h).json()["id"]
        db.record_mix_result(mid, "{}")
        with patch.object(mixes_route, "_run_generation"):
            assert api.post(f"/api/mixes/{mid}/generate", headers=h).status_code == 429


SECRET_URL = "http://plex:32400/playlists?X-Plex-Token=SECRET failed"


class TestNoTokenLeaks:
    def test_generate_sync_exception_not_logged_or_stored(self, gen_env, caplog):
        mix = make_mix(gen_env.db, gen_env.uid, track_count=10)
        plex = MagicMock()
        plex.sync_playlist_to_users.side_effect = requests.ReadTimeout(SECRET_URL)
        with caplog.at_level("DEBUG"):
            res, _ = run_generate(gen_env, mix, plex)
        assert res.sync_error == "Plex sync failed (ReadTimeout)"
        assert "SECRET" not in caplog.text
        assert "SECRET" not in gen_env.db.get_mix_config(mix["id"])["last_result_json"]

    def test_sync_result_error_text_is_redacted(self, gen_env):
        mix = make_mix(gen_env.db, gen_env.uid, track_count=10)
        plex = fake_plex([SyncResult("x", 5, 0, 0, success=False, error=f"User Alice: {SECRET_URL}")])
        res, _ = run_generate(gen_env, mix, plex)
        assert "SECRET" not in res.sync_error
        assert "SECRET" not in gen_env.db.get_mix_config(mix["id"])["last_result_json"]

    def test_worker_iteration_failure_not_logged(self, db, users, config, caplog):
        make_mix(db, users["alice"]["id"])
        with patch("plex_playlist_sync.mix_worker.generate_and_sync", side_effect=requests.ReadTimeout(SECRET_URL)):
            with caplog.at_level("DEBUG"):
                out = MixWorker().run_iteration(db, config, None, FakeDiscovery())
        assert out["errors"] == 1
        assert "SECRET" not in caplog.text

    def test_worker_plex_connect_failure_not_logged(self, config, caplog):
        config.plex_url, config.plex_token = "http://plex", "tok"
        with patch("plex_playlist_sync.mix_worker.PlexClient", side_effect=requests.ConnectionError(SECRET_URL)):
            with caplog.at_level("DEBUG"):
                assert MixWorker()._get_plex(config) is None
        assert "SECRET" not in caplog.text

    def test_background_generation_failure_not_logged(self, db, users, config, caplog):
        from plex_playlist_sync.api.routes import mixes as mixes_route

        mix = make_mix(db, users["alice"]["id"])
        with patch.object(mixes_route, "generate_and_sync", side_effect=requests.ReadTimeout(SECRET_URL)):
            with caplog.at_level("DEBUG"):
                mixes_route._run_generation(db, None, FakeDiscovery(), mix["id"], config)
        assert "SECRET" not in caplog.text


def _enable_native(db):
    return patch.multiple(
        db,
        list_download_clients=lambda: [{"enabled": True, "driver_type": "qbittorrent"}],
        list_indexers=lambda: [{"enabled": True}],
    )


def _lock_free_from_other_thread(user_id):
    import threading

    from plex_playlist_sync.request_submission import user_request_lock

    out = []

    def probe():
        lock = user_request_lock(user_id)
        got = lock.acquire(timeout=1)
        out.append(got)
        if got:
            lock.release()

    t = threading.Thread(target=probe)
    t.start()
    t.join()
    return out[0]


class TestLockNotHeldAcrossNetwork:
    def test_submit_track_request_grabs_outside_lock(self, db, users, config):
        from plex_playlist_sync.request_submission import submit_track_request

        seen = []

        def grab(**kw):
            seen.append(_lock_free_from_other_thread(users["admin"]["id"]))
            return {"success": False}

        with _enable_native(db), patch("plex_playlist_sync.request_submission.acquisition_coordinator") as coord:
            coord.search_and_grab.side_effect = grab
            submit_track_request(db, config, users["admin"], "T", "A")
        assert seen == [True]

    def test_mix_acquisition_grabs_outside_lock(self, gen_env):
        gen_env.config.auto_approve_requests = True
        mix = make_mix(gen_env.db, gen_env.uid, track_count=10, discovery_ratio=0.0,
                       auto_acquire_missing=True, max_weekly_acquisitions=2)
        seen = []

        def grab(**kw):
            seen.append(_lock_free_from_other_thread(gen_env.uid))
            return {"success": False}

        with _enable_native(gen_env.db), patch(
            "plex_playlist_sync.tailored_mixes.get_item_availability", side_effect=avail_fn(_missing(gen_env))
        ), patch("plex_playlist_sync.request_submission.acquisition_coordinator") as coord:
            coord.search_and_grab.side_effect = grab
            res = generate_and_sync(gen_env.db, fake_plex(), gen_env.disc, mix, gen_env.config)
        assert res.acquisitions_queued == 2
        assert seen == [True, True]


def test_generate_cooldown_map_prunes_expired_entries(api, db, users):
    import time

    from plex_playlist_sync.api.routes import mixes as mixes_route

    seed_history(db, users["alice"]["id"])
    h = api.h["alice"]
    mid = api.post("/api/mixes", json={"mix_type": "discover_weekly"}, headers=h).json()["id"]
    mixes_route._last_generate_request["stale-mix"] = time.monotonic() - 10_000
    try:
        with patch.object(mixes_route, "_run_generation"):
            api.post(f"/api/mixes/{mid}/generate", headers=h)
        assert "stale-mix" not in mixes_route._last_generate_request
        assert mid in mixes_route._last_generate_request
    finally:
        mixes_route._last_generate_request.pop("stale-mix", None)
        mixes_route._last_generate_request.pop(mid, None)
        mixes_route._generating.discard(mid)

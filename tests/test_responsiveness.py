"""Regression tests for the production stall: remote cover fetches blocking request threads / DB, and the
artist refresh worker sweeping the whole library at boot. All HTTP is mocked."""

from datetime import datetime, timedelta, timezone
import threading
import time
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest
import requests

import trackseerr.mediacover as mc
from trackseerr.api.app import create_app
from trackseerr.api.dependencies import get_config, get_db
from trackseerr.artist_refresh_worker import ArtistRefreshWorker
from trackseerr.auth import create_session_token, get_or_create_secret_key
from trackseerr.config import Config
from trackseerr.mediacover import MediaCoverService
from trackseerr.storage import Database

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
URL = "https://coverartarchive.org/release-group/abc/front-500"


@pytest.fixture
def svc(tmp_path):
    return MediaCoverService(base_dir=tmp_path)


def _ok_response():
    r = MagicMock()
    r.status_code = 200
    r.iter_content.return_value = [JPEG]
    return r


def test_ensure_artwork_does_not_block_on_slow_fetch(svc):
    release = threading.Event()

    def slow_get(*a, **k):
        release.wait(10)
        return _ok_response()

    with patch.object(mc.requests, "get", side_effect=slow_get):
        t0 = time.monotonic()
        assert svc.ensure_artwork("album_cover", "a1", URL) is None
        assert time.monotonic() - t0 < 0.5
        release.set()
        assert svc.wait_idle(5)
    # Next call finds the file the background job wrote.
    assert svc.ensure_artwork("album_cover", "a1", URL) == svc.get_album_cover_path("a1")


def test_inflight_dedupe_and_bounded_queue(svc, monkeypatch):
    release = threading.Event()
    calls = []

    def slow_get(*a, **k):
        calls.append(1)
        release.wait(10)
        return _ok_response()

    monkeypatch.setattr(mc, "_MAX_PENDING", 3)
    with patch.object(mc.requests, "get", side_effect=slow_get):
        assert svc.schedule_cache(svc.get_album_cover_path("x"), URL + "1") is True
        assert svc.schedule_cache(svc.get_album_cover_path("x"), URL + "1") is False  # deduped
        assert svc.schedule_cache(svc.get_album_cover_path("y"), URL + "2") is True
        assert svc.schedule_cache(svc.get_album_cover_path("z"), URL + "3") is True
        assert svc.schedule_cache(svc.get_album_cover_path("w"), URL + "4") is False  # queue full: dropped
        release.set()
        assert svc.wait_idle(5)


def test_negative_cache_skips_failed_url(svc):
    target = svc.get_album_cover_path("n1")
    with patch.object(mc.requests, "get", side_effect=requests.ReadTimeout("t")) as g:
        assert svc.cache_image(target, URL) is False
        assert svc.cache_image(target, URL) is False
        assert g.call_count == 1
    # Expiry allows a retry.
    svc._negative[URL] = time.monotonic() - 1
    with patch.object(mc.requests, "get", return_value=_ok_response()) as g:
        assert svc.cache_image(target, URL) is True
        assert g.call_count == 1


def test_host_circuit_breaker_opens_and_recovers(svc):
    with patch.object(mc.requests, "get", side_effect=requests.ConnectTimeout("t")) as g:
        for i in range(mc._BREAKER_THRESHOLD):
            svc.cache_image(svc.get_album_cover_path(f"b{i}"), f"{URL}/{i}")
        assert g.call_count == mc._BREAKER_THRESHOLD
        # A brand-new URL on the same host is now skipped without a request.
        assert svc.cache_image(svc.get_album_cover_path("fresh"), f"{URL}/fresh") is False
        assert g.call_count == mc._BREAKER_THRESHOLD
    # Other hosts unaffected.
    with patch.object(mc.requests, "get", return_value=_ok_response()) as g:
        assert svc.cache_image(svc.get_album_cover_path("other"), "https://i.scdn.co/image/x") is True
    # Breaker expires -> half-open -> success closes it.
    for h in list(svc._host_open_until):
        svc._host_open_until[h] = time.monotonic() - 1
    with patch.object(mc.requests, "get", return_value=_ok_response()):
        assert svc.cache_image(svc.get_album_cover_path("rec"), f"{URL}/rec") is True
    assert "coverartarchive.org" not in svc._host_open_until


def test_fetch_uses_short_connect_timeout(svc):
    with patch.object(mc.requests, "get", return_value=_ok_response()) as g:
        svc.cache_image(svc.get_album_cover_path("t"), URL)
    assert g.call_args.kwargs["timeout"] == (3.0, 5.0)


def test_db_usable_while_fetch_in_progress(tmp_path, svc):
    db = Database(str(tmp_path / "t.db"))
    started, release = threading.Event(), threading.Event()

    def slow_get(*a, **k):
        started.set()
        release.wait(10)
        return _ok_response()

    try:
        with patch.object(mc.requests, "get", side_effect=slow_get):
            svc.ensure_artwork("album_cover", "d1", URL)
            assert started.wait(5)
            t0 = time.monotonic()
            got = []
            th = threading.Thread(target=lambda: got.append(db.get_kv("k")))
            th.start()
            th.join(2)
            assert not th.is_alive() and time.monotonic() - t0 < 1.5
            release.set()
            assert svc.wait_idle(5)
    finally:
        release.set()
        db.close()


def test_cover_endpoint_returns_fast_while_remote_hangs(tmp_path):
    db = Database(str(tmp_path / "e.db"))
    cfg = Config(plex_url="http://127.0.0.1:32400", plex_token="t", data_dir=str(tmp_path), role="all-in-one")
    db.upsert_user("admin-1", "admin", "a@x", is_admin=True)
    db.upsert_library_artist({"id": "ar1", "name": "A", "image_url": URL})
    db.upsert_library_album({"id": "al1", "artist_id": "ar1", "title": "T", "cover_url": URL})
    key = get_or_create_secret_key(data_dir=str(tmp_path))
    tok = create_session_token(user_id="admin-1", username="admin", is_admin=True, secret_key=key)
    db.create_session(session_id=tok, user_id="admin-1")
    app = create_app(db=db, config=cfg)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_config] = lambda: cfg
    client = TestClient(app)
    release = threading.Event()
    local_svc = MediaCoverService(base_dir=tmp_path / "mc")
    try:
        with patch("trackseerr.api.routes.library._shared.mediacover_service", local_svc), patch.object(
            mc.requests, "get", side_effect=lambda *a, **k: release.wait(10)
        ):
            for path in ("/api/library/albums/al1/cover", "/api/library/artists/ar1/image"):
                t0 = time.monotonic()
                resp = client.get(path, cookies={"session_token": tok}, follow_redirects=False)
                assert time.monotonic() - t0 < 1.5, path
                assert resp.status_code in (200, 307), (path, resp.status_code)
            release.set()
            local_svc.wait_idle(5)
    finally:
        release.set()
        db.close()


# ---------------------------------------------------------------- refresh worker
def _seed_artists(db, n=3):
    for i in range(n):
        db.upsert_library_artist({"id": f"w{i}", "name": f"W{i}"})


def test_worker_first_cycle_is_delayed(tmp_path):
    db = Database(str(tmp_path / "w.db"))
    _seed_artists(db)
    w = ArtistRefreshWorker()
    try:
        with patch.object(w, "refresh_once") as ro:
            assert w.start(db=db, initial_delay=60)
            time.sleep(0.4)
            assert ro.call_count == 0
        w.stop()
        assert not w.is_running()
        with patch.object(w, "refresh_once") as ro:
            w2 = ArtistRefreshWorker()
            with patch.object(w2, "refresh_once") as ro2:
                w2.start(db=db, initial_delay=0, interval_seconds=3600)
                deadline = time.time() + 3
                while ro2.call_count == 0 and time.time() < deadline:
                    time.sleep(0.02)
                assert ro2.call_count >= 1
                assert ro2.call_args.kwargs.get("only_stale") is True
                w2.stop()
    finally:
        w.stop()
        db.close()


def test_worker_skips_recently_refreshed_artists(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    _seed_artists(db, 3)
    now = datetime.now(timezone.utc)
    db.set_kv("artist_refresh:last:w0", now.isoformat())  # fresh -> skipped
    db.set_kv("artist_refresh:last:w1", (now - timedelta(days=2)).isoformat())  # stale -> refreshed
    # w2 never refreshed -> refreshed
    w = ArtistRefreshWorker()
    w.pace_delay = 0
    seen = []
    with patch(
        "trackseerr.artist_refresh.refresh_single_artist",
        side_effect=lambda artist_id, **k: seen.append(artist_id) or {"success": True},
    ):
        res = w.refresh_once(db=db, discovery_client=MagicMock(), enricher=MagicMock(), only_stale=True)
        assert sorted(seen) == ["w1", "w2"]
        assert res["artists_checked"] == 2
        # Persisted: a second scheduled sweep (e.g. after restart) finds nothing stale.
        seen.clear()
        w.refresh_once(db=db, discovery_client=MagicMock(), enricher=MagicMock(), only_stale=True)
        assert seen == []
        # A manual run (default) still refreshes everything.
        w.refresh_once(db=db, discovery_client=MagicMock(), enricher=MagicMock())
        assert sorted(seen) == ["w0", "w1", "w2"]
    db.close()

"""Unit tests for metadata source resilience: circuit breakers, Deezer pacing, quota handling, and L2 caching."""

import copy
import datetime
from email.utils import format_datetime
import logging
from pathlib import Path
import threading
import time
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
import requests

from trackseerr.clients.discovery import (
    DiscoveryClient,
    _reset_deezer_pacer,
)
from trackseerr.clients.mbid_enricher import (
    MbidEnricherClient,
    _HostGovernor,
    _governor,
    _parse_retry_after,
    _reset_governors,
)
from trackseerr.mb_metadata_store import (
    MbMetadataStore,
    get_shared_discovery_client,
    get_shared_enricher,
    reset_shared_clients,
    reset_shared_discovery_client,
    reset_shared_enricher,
)
from trackseerr.storage import Database

pytestmark = pytest.mark.real_mbid_enricher


@pytest.fixture
def test_db(tmp_path: Path):
    """Isolated disk-backed database for testing."""
    db_file = tmp_path / "test_resilience.db"
    db = Database(str(db_file))
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _reset_resilience_environment():
    """Resets global pacer, governors, and shared singletons before and after each test."""
    _reset_deezer_pacer()
    _reset_governors()
    reset_shared_clients()
    yield
    _reset_deezer_pacer()
    _reset_governors()
    reset_shared_clients()


# =============================================================================
# 1. JSON Decode Failure Handling
# =============================================================================


def test_json_decode_failure_logged(test_db: Database, caplog: pytest.LogCaptureFixture) -> None:
    """When an upstream response contains malformed JSON, ValueError is logged as a warning and handled cleanly."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://musicbrainz.org", store=store)

    bad_resp = MagicMock(status_code=200)
    bad_resp.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")

    with caplog.at_level(logging.WARNING):
        with patch.object(client._session, "get", return_value=bad_resp):
            res = client.lookup_artist_mbid("Radiohead")
            assert res is None

    assert any("JSON decode failed" in record.message for record in caplog.records)


# =============================================================================
# 2. Circuit Breaker Behavior
# =============================================================================


def test_breaker_opens_after_threshold_and_skips_mirror(test_db: Database) -> None:
    """Five consecutive upstream failures trip the circuit breaker; subsequent requests skip the mirror."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.cc", min_interval=0.0, store=store)

    err_resp = MagicMock(status_code=500)
    err_resp.headers = {}
    ok_resp = MagicMock(status_code=200)
    ok_resp.json.return_value = {"artists": [{"id": "mbid-123"}]}

    def mock_get(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        if "api.brainzmash.cc" in url:
            return err_resp
        return ok_resp

    # Trigger 5 failures on mirror (fallback to musicbrainz.org succeeds)
    with patch.object(client._session, "get", side_effect=mock_get):
        for _ in range(5):
            client._request("https://api.brainzmash.cc/ws/2/artist?query=test")

    stats = client.stats()
    assert stats["breaker_state"]["api.brainzmash.cc"] == "open"
    assert stats["breaker_state"]["musicbrainz.org"] == "closed"
    assert not client.source_available("api.brainzmash.cc")
    assert client.source_available("musicbrainz.org")

    # The 6th request should skip mirror and route directly to musicbrainz.org
    with patch.object(client._session, "get", return_value=ok_resp) as mock_network:
        resp = client._request("https://api.brainzmash.cc/ws/2/artist?query=test")
        assert resp is not None
        assert resp.status_code == 200
        # Verify the call was made to musicbrainz.org, not api.brainzmash.cc
        assert mock_network.call_count == 1
        called_url = mock_network.call_args[0][0]
        assert "musicbrainz.org" in called_url
        assert "api.brainzmash.cc" not in called_url


def test_breaker_both_open_no_network(test_db: Database) -> None:
    """When both mirror and fallback circuit breakers are open, requests fail immediately without network calls."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.cc", min_interval=0.0, store=store)

    # Force trip both breakers
    client._breakers["api.brainzmash.cc"]._state = "open"
    client._breakers["api.brainzmash.cc"]._last_failure_time = time.monotonic()
    client._breakers["musicbrainz.org"]._state = "open"
    client._breakers["musicbrainz.org"]._last_failure_time = time.monotonic()

    with patch.object(client._session, "get") as mock_get:
        resp = client._request("https://api.brainzmash.cc/ws/2/artist?query=test")
        assert resp is None
        assert mock_get.call_count == 0


def test_breaker_half_open_recovers(test_db: Database) -> None:
    """After cooldown, circuit breaker enters half-open and closes upon a successful request."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.cc", min_interval=0.0, store=store)

    breaker = client._breakers["api.brainzmash.cc"]
    breaker._state = "open"
    # Set _last_failure_time to 305 seconds ago (cooldown is 300s)
    breaker._last_failure_time = time.monotonic() - 305.0

    assert breaker.state == "half_open"

    ok_resp = MagicMock(status_code=200)
    with patch.object(client._session, "get", return_value=ok_resp):
        resp = client._request("https://api.brainzmash.cc/ws/2/artist?query=test")
        assert resp is not None
        assert resp.status_code == 200

    assert breaker.state == "closed"
    assert breaker._consecutive_failures == 0


def test_404_does_not_trip_breaker(test_db: Database) -> None:
    """HTTP 404 responses represent legitimate missing resources and do not count toward breaker failure threshold."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.cc", min_interval=0.0, store=store)

    not_found_resp = MagicMock(status_code=404)
    not_found_resp.headers = {}

    with patch.object(client._session, "get", return_value=not_found_resp):
        for _ in range(10):
            client._request("https://api.brainzmash.cc/ws/2/artist/nonexistent")

    breaker = client._breakers["api.brainzmash.cc"]
    assert breaker.state == "closed"
    assert breaker._consecutive_failures == 0


def test_mirror_403s_open_breaker_and_use_fallback(
    test_db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """Mirror returning 403 five times opens its circuit breaker; the 6th call goes directly to musicbrainz.org."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.cc", min_interval=0.0, store=store)

    resp_403 = MagicMock(status_code=403)
    resp_403.headers = {}
    ok_resp = MagicMock(status_code=200)
    ok_resp.json.return_value = {"artists": [{"id": "mbid-123"}]}

    def mock_get(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        if "api.brainzmash.cc" in url:
            return resp_403
        return ok_resp

    with caplog.at_level(logging.WARNING):
        with patch.object(client._session, "get", side_effect=mock_get):
            for _ in range(5):
                resp = client._request("https://api.brainzmash.cc/ws/2/artist?query=test")
                assert resp is not None
                assert resp.status_code == 200

    breaker = client._breakers["api.brainzmash.cc"]
    assert breaker.state == "open"
    assert breaker._consecutive_failures == 5
    assert not client.source_available("api.brainzmash.cc")
    assert client.source_available("musicbrainz.org")

    # Verify warning log when breaker opened on 403
    warning_logs = [rec.message for rec in caplog.records if rec.levelno == logging.WARNING]
    assert any(
        "403" in msg and "refusing requests" in msg and "musicbrainz.org" in msg
        for msg in warning_logs
    )

    # The 6th call should go straight to musicbrainz.org without touching the mirror
    with patch.object(client._session, "get", return_value=ok_resp) as mock_network:
        resp = client._request("https://api.brainzmash.cc/ws/2/artist?query=test")
        assert resp is not None
        assert resp.status_code == 200
        assert mock_network.call_count == 1
        called_url = mock_network.call_args[0][0]
        assert "musicbrainz.org" in called_url
        assert "api.brainzmash.cc" not in called_url


def test_single_mirror_403_does_not_open_breaker(test_db: Database) -> None:
    """A single HTTP 403 increments consecutive failures but does not trip the circuit breaker."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.cc", min_interval=0.0, store=store)

    resp_403 = MagicMock(status_code=403)
    resp_403.headers = {}
    ok_resp = MagicMock(status_code=200)

    def mock_get(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        if "api.brainzmash.cc" in url:
            return resp_403
        return ok_resp

    with patch.object(client._session, "get", side_effect=mock_get):
        resp = client._request("https://api.brainzmash.cc/ws/2/artist?query=test")
        assert resp is not None
        assert resp.status_code == 200

    breaker = client._breakers["api.brainzmash.cc"]
    assert breaker.state == "closed"
    assert breaker._consecutive_failures == 1
    assert client.source_available("api.brainzmash.cc")


def test_musicbrainz_retry_after_honored_up_to_30s(test_db: Database) -> None:
    """musicbrainz.org honors Retry-After up to 30s cap on HTTP 429/503; higher values are capped at 30s."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://musicbrainz.org", min_interval=0.0, store=store)

    resp_429 = MagicMock(status_code=429)
    resp_429.headers = {"Retry-After": "15"}

    resp_200 = MagicMock(status_code=200)

    # 1. Retry-After: 15s should sleep 15s
    with patch.object(client._session, "get", side_effect=[resp_429, resp_200]):
        with patch("time.sleep") as mock_sleep:
            resp = client._request("https://musicbrainz.org/ws/2/artist?query=test", max_retries=1)
            assert resp == resp_200
            mock_sleep.assert_called_once_with(15.0)

    # 2. Retry-After: 120s should be capped at 30s for musicbrainz.org
    _reset_governors()
    resp_429_large = MagicMock(status_code=429)
    resp_429_large.headers = {"Retry-After": "120"}

    with patch.object(client._session, "get", side_effect=[resp_429_large, resp_200]):
        with patch("time.sleep") as mock_sleep:
            resp = client._request("https://musicbrainz.org/ws/2/artist?query=test", max_retries=1)
            assert resp == resp_200
            mock_sleep.assert_called_once_with(30.0)


# =============================================================================
# 3. Deezer Pacer & Quota Handling
# =============================================================================


def test_deezer_quota_body_retries_once(test_db: Database) -> None:
    """Deezer HTTP 200 with error code 4 is recognized as a quota error and retried once after 5s."""
    store = MbMetadataStore(test_db)
    client = DiscoveryClient(store=store)

    quota_resp = MagicMock(status_code=200)
    quota_resp.json.return_value = {"error": {"code": 4, "message": "Quota limit exceeded", "type": "Exception"}}

    success_resp = MagicMock(status_code=200)
    success_resp.json.return_value = {
        "id": 12345,
        "title": "Abbey Road",
        "artist": {"id": 1, "name": "The Beatles"},
        "tracks": {"data": []},
    }

    with patch.object(client.session, "get", side_effect=[quota_resp, success_resp]):
        with patch("time.sleep") as mock_sleep:
            res = client.get_album_details("deezer:album:12345")
            assert res is not None
            assert res["title"] == "Abbey Road"
            mock_sleep.assert_called_once_with(5.0)


def test_deezer_429_retries_once(test_db: Database) -> None:
    """Deezer HTTP 429 status code is recognized as rate limit and retried once after 5s."""
    store = MbMetadataStore(test_db)
    client = DiscoveryClient(store=store)

    resp_429 = MagicMock(status_code=429)
    resp_429.json.side_effect = ValueError("No JSON")

    success_resp = MagicMock(status_code=200)
    success_resp.json.return_value = {
        "id": 12345,
        "title": "Abbey Road",
        "artist": {"id": 1, "name": "The Beatles"},
        "tracks": {"data": []},
    }

    with patch.object(client.session, "get", side_effect=[resp_429, success_resp]):
        with patch("time.sleep") as mock_sleep:
            res = client.get_album_details("deezer:album:12345")
            assert res is not None
            assert res["title"] == "Abbey Road"
            mock_sleep.assert_called_once_with(5.0)


def test_deezer_pacer_limits_rate() -> None:
    """Deezer pacer ensures no more than 40 requests in a 5-second sliding window."""
    _reset_deezer_pacer()
    client = DiscoveryClient()

    ok_resp = MagicMock(status_code=200)
    ok_resp.json.return_value = {"data": []}

    sleep_calls: list[float] = []

    def fake_sleep(duration: float) -> None:
        sleep_calls.append(duration)

    with patch.object(client.session, "get", return_value=ok_resp):
        with patch("time.sleep", side_effect=fake_sleep):
            # Fire 41 requests rapidly in loop
            for _ in range(41):
                client._deezer_get("https://api.deezer.com/chart/0/tracks")

    # The 41st request must trigger a pacer sleep
    assert len(sleep_calls) >= 1
    assert sleep_calls[0] > 0.0


# =============================================================================
# 4. Discovery L2 Caching & Force Bypass
# =============================================================================


def test_deezer_album_details_persist_across_instances(test_db: Database) -> None:
    """Album details fetched by one DiscoveryClient instance persist to L2 store and are retrieved by another."""
    store = MbMetadataStore(test_db)
    client1 = DiscoveryClient(store=store)

    raw_album = {
        "id": 54321,
        "title": "OK Computer",
        "artist": {"id": 10, "name": "Radiohead"},
        "tracks": {"data": []},
    }
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = raw_album

    with patch.object(client1.session, "get", return_value=mock_resp) as mock_get:
        res1 = client1.get_album_details("deezer:album:54321")
        assert res1 is not None
        assert res1["title"] == "OK Computer"
        assert mock_get.call_count == 1

    # Second instance sharing the same store
    client2 = DiscoveryClient(store=store)
    with patch.object(client2.session, "get") as mock_get2:
        res2 = client2.get_album_details("deezer:album:54321")
        assert res2 is not None
        assert res2["title"] == "OK Computer"
        assert mock_get2.call_count == 0  # Served strictly from L2 cache

    assert client2.stats()["cache_hits"] >= 1


def test_deezer_error_not_persisted(test_db: Database) -> None:
    """Failed lookups (e.g. 404 or upstream errors) are not written to the persistent L2 store."""
    store = MbMetadataStore(test_db)
    client = DiscoveryClient(store=store)

    err_resp = MagicMock(status_code=404)
    with patch.object(client.session, "get", return_value=err_resp):
        res = client.get_album_details("deezer:album:999999")
        assert res is None

    # Check store directly
    hit, cached = store.get("dz:album:deezer:album:999999")
    assert not hit
    assert cached is None


def test_deezer_force_bypasses_cache(test_db: Database) -> None:
    """Passing force=True bypasses L1 and L2 cache, re-queries upstream, and updates cache."""
    store = MbMetadataStore(test_db)
    client = DiscoveryClient(store=store)

    resp_v1 = MagicMock(status_code=200)
    resp_v1.json.return_value = {
        "id": 111,
        "title": "Version 1",
        "artist": {"id": 1, "name": "Artist"},
        "tracks": {"data": []},
    }

    with patch.object(client.session, "get", return_value=resp_v1):
        res1 = client.get_album_details("deezer:album:111")
        assert res1 is not None
        assert res1["title"] == "Version 1"

    resp_v2 = MagicMock(status_code=200)
    resp_v2.json.return_value = {
        "id": 111,
        "title": "Version 2 (Remastered)",
        "artist": {"id": 1, "name": "Artist"},
        "tracks": {"data": []},
    }

    with patch.object(client.session, "get", return_value=resp_v2) as mock_get:
        res2 = client.get_album_details("deezer:album:111", force=True)
        assert res2 is not None
        assert res2["title"] == "Version 2 (Remastered)"
        assert mock_get.call_count == 1


# =============================================================================
# 5. Shared Discovery Client Singleton Lifecycle
# =============================================================================


def test_shared_discovery_client_singleton(test_db: Database, tmp_path: Path) -> None:
    """get_shared_discovery_client provides a singleton instance per database and rebuilds on reset."""
    c1 = get_shared_discovery_client(test_db)
    c2 = get_shared_discovery_client(test_db)
    assert c1 is c2

    # Reset clears singleton
    reset_shared_discovery_client()
    c3 = get_shared_discovery_client(test_db)
    assert c3 is not c1

    # Different database gets separate instance
    other_db = Database(str(tmp_path / "other.db"))
    try:
        c4 = get_shared_discovery_client(other_db)
        assert c4 is not c3
    finally:
        other_db.close()


# =============================================================================
# 6. Mirror Rate Limits, Pacing, Concurrency, and Resilience
# =============================================================================


def test_mirror_paced_to_configured_rps(monkeypatch: pytest.MonkeyPatch, test_db: Database) -> None:
    """Configuring MUSICBRAINZ_MIRROR_RPS paces calls to 1/rps interval."""
    monkeypatch.setenv("MUSICBRAINZ_MIRROR_RPS", "2.0")
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.cc", store=store)

    ok_resp = MagicMock(status_code=200)
    ok_resp.json.return_value = {"artists": []}

    sleeps: list[float] = []
    current_time = [1000.0]

    def fake_time() -> float:
        return current_time[0]

    def fake_sleep(duration: float) -> None:
        sleeps.append(duration)
        current_time[0] += duration

    with patch("time.monotonic", side_effect=fake_time), \
         patch("time.sleep", side_effect=fake_sleep), \
         patch.object(client._session, "get", return_value=ok_resp):
        res1 = client._request("https://api.brainzmash.cc/ws/2/artist?query=test1")
        res2 = client._request("https://api.brainzmash.cc/ws/2/artist?query=test2")

    assert res1 is ok_resp
    assert res2 is ok_resp
    assert len(sleeps) == 1
    assert pytest.approx(sleeps[0], rel=1e-2) == 0.5


def test_mirror_concurrency_capped(test_db: Database) -> None:
    """Requests to mirror are concurrency-capped by BoundedSemaphore."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.cc", store=store)

    max_in_flight = 0
    current_in_flight = 0
    lock = threading.Lock()
    started_gate = threading.Event()
    release_gate = threading.Event()
    threads_arrived = 0

    def fake_get(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        nonlocal max_in_flight, current_in_flight, threads_arrived
        with lock:
            current_in_flight += 1
            if current_in_flight > max_in_flight:
                max_in_flight = current_in_flight
            threads_arrived += 1
            if threads_arrived == 2:
                started_gate.set()

        release_gate.wait(timeout=5.0)

        with lock:
            current_in_flight -= 1
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"artists": []}
        return resp

    # Concurrency is 2 by default
    with patch.object(client._session, "get", side_effect=fake_get):
        threads = [
            threading.Thread(
                target=client._request,
                args=(f"https://api.brainzmash.cc/ws/2/artist?query=t{i}",),
            )
            for i in range(4)
        ]
        for t in threads:
            t.start()

        assert started_gate.wait(timeout=3.0)
        time.sleep(0.1)
        with lock:
            assert max_in_flight == 2
            assert current_in_flight == 2

        release_gate.set()
        for t in threads:
            t.join(timeout=3.0)

    assert max_in_flight == 2


def test_mirror_429_retry_after_pauses_and_falls_back(test_db: Database) -> None:
    """When mirror returns 429 with Retry-After: 120, client does not retry mirror, falls back, pauses mirror, and next request goes straight to fallback."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.cc", store=store)

    resp_429 = MagicMock(status_code=429)
    resp_429.headers = {"Retry-After": "120"}

    resp_fallback = MagicMock(status_code=200)
    resp_fallback.json.return_value = {"artists": [{"id": "mbid-456"}]}

    calls: list[str] = []

    def mock_get(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        calls.append(url)
        if "api.brainzmash.cc" in url:
            return resp_429
        return resp_fallback

    with patch.object(client._session, "get", side_effect=mock_get):
        res1 = client._request("https://api.brainzmash.cc/ws/2/artist?query=test")
        assert res1 is resp_fallback
        assert calls == [
            "https://api.brainzmash.cc/ws/2/artist?query=test",
            "https://musicbrainz.org/ws/2/artist?query=test",
        ]

        gov = _governor("api.brainzmash.cc")
        assert gov.paused()
        assert 110.0 < gov.pause_remaining() <= 120.0

        calls.clear()
        res2 = client._request("https://api.brainzmash.cc/ws/2/artist?query=test2")
        assert res2 is resp_fallback
        assert calls == ["https://musicbrainz.org/ws/2/artist?query=test2"]


def test_retry_after_http_date_parsed() -> None:
    """Retry-After HTTP-date is parsed as remaining seconds, clamped to 600s."""
    now = datetime.datetime.now(datetime.timezone.utc)

    # 45 seconds in the future
    future_45 = now + datetime.timedelta(seconds=45)
    parsed_45 = _parse_retry_after(format_datetime(future_45, usegmt=True))
    assert 40.0 <= parsed_45 <= 46.0

    # Past date returns 0.0
    past = now - datetime.timedelta(seconds=60)
    assert _parse_retry_after(format_datetime(past, usegmt=True)) == 0.0

    # Far future (> 600s) clamped to 600s
    future_1000 = now + datetime.timedelta(seconds=1000)
    assert _parse_retry_after(format_datetime(future_1000, usegmt=True)) == 600.0

    # Numeric seconds
    assert _parse_retry_after("120") == 120.0
    assert _parse_retry_after("750") == 600.0


def test_retry_after_missing_defaults_30s() -> None:
    """Missing or unparseable Retry-After defaults to 30.0s."""
    assert _parse_retry_after(None) == 30.0
    assert _parse_retry_after("") == 30.0
    assert _parse_retry_after("   ") == 30.0
    assert _parse_retry_after("invalid-date-string") == 30.0
    assert _parse_retry_after("foo bar") == 30.0


def test_requests_last_hour_counts_and_expires() -> None:
    """Governor tracks hourly request counts in a deque and expires entries older than 3600s."""
    gov = _HostGovernor("mirror.test.org")
    current_time = [10_000.0]

    with patch("time.monotonic", side_effect=lambda: current_time[0]):
        assert gov.requests_last_hour() == 0

        gov.record_request()
        gov.record_request()
        gov.record_request()
        assert gov.requests_last_hour() == 3

        # Advance 1800s, record 2 more
        current_time[0] += 1800.0
        gov.record_request()
        gov.record_request()
        assert gov.requests_last_hour() == 5

        # Advance to 10000 + 3601 = 13601 (first 3 expire)
        current_time[0] = 13_601.0
        assert gov.requests_last_hour() == 2

        # Advance past 1800 + 3600 = 5400s (all expire)
        current_time[0] = 20_000.0
        assert gov.requests_last_hour() == 0


def test_invalid_env_values_fall_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Invalid env values log a warning and fall back to defaults; out-of-range values are clamped."""
    monkeypatch.setenv("MUSICBRAINZ_MIRROR_RPS", "not_a_float")
    monkeypatch.setenv("MUSICBRAINZ_MIRROR_CONCURRENCY", "not_an_int")

    with caplog.at_level(logging.WARNING):
        gov = _HostGovernor("test-mirror.cc")

    assert gov.rate_limit_rps == 3.0
    assert gov.max_concurrency == 2
    assert any("MUSICBRAINZ_MIRROR_RPS" in r.message for r in caplog.records)
    assert any("MUSICBRAINZ_MIRROR_CONCURRENCY" in r.message for r in caplog.records)

    # Test clamping: RPS (0.5 - 20), concurrency (1 - 8)
    monkeypatch.setenv("MUSICBRAINZ_MIRROR_RPS", "0.1")
    monkeypatch.setenv("MUSICBRAINZ_MIRROR_CONCURRENCY", "0")
    gov_low = _HostGovernor("low-mirror.cc")
    assert gov_low.rate_limit_rps == 0.5
    assert gov_low.max_concurrency == 1

    monkeypatch.setenv("MUSICBRAINZ_MIRROR_RPS", "50.0")
    monkeypatch.setenv("MUSICBRAINZ_MIRROR_CONCURRENCY", "20")
    gov_high = _HostGovernor("high-mirror.cc")
    assert gov_high.rate_limit_rps == 20.0
    assert gov_high.max_concurrency == 8


def test_governor_shared_across_client_instances(test_db: Database) -> None:
    """Multiple client instances hitting the same host share the same governor state."""
    store = MbMetadataStore(test_db)
    client1 = MbidEnricherClient(base_url="https://api.brainzmash.cc", store=store)
    client2 = MbidEnricherClient(base_url="https://api.brainzmash.cc", store=store)

    ok_resp = MagicMock(status_code=200)
    ok_resp.json.return_value = {"artists": []}

    with patch.object(client1._session, "get", return_value=ok_resp), \
         patch.object(client2._session, "get", return_value=ok_resp):
        client1._request("https://api.brainzmash.cc/ws/2/artist?query=c1")
        client2._request("https://api.brainzmash.cc/ws/2/artist?query=c2")

    stats1 = client1.stats()
    stats2 = client2.stats()

    assert stats1["hosts"]["api.brainzmash.cc"]["requests_last_hour"] == 2
    assert stats2["hosts"]["api.brainzmash.cc"]["requests_last_hour"] == 2


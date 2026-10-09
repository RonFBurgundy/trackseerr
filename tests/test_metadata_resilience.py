"""Unit tests for metadata source resilience: circuit breakers, Deezer pacing, quota handling, and L2 caching."""

import copy
import logging
from pathlib import Path
import time
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
import requests

from trackseerr.clients.discovery import (
    DiscoveryClient,
    _reset_deezer_pacer,
)
from trackseerr.clients.mbid_enricher import MbidEnricherClient
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
    """Resets global pacer and shared singletons before and after each test."""
    _reset_deezer_pacer()
    reset_shared_clients()
    yield
    _reset_deezer_pacer()
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
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", min_interval=0.0, store=store)

    err_resp = MagicMock(status_code=500)
    err_resp.headers = {}
    ok_resp = MagicMock(status_code=200)
    ok_resp.json.return_value = {"artists": [{"id": "mbid-123"}]}

    def mock_get(url: str, *args: Any, **kwargs: Any) -> MagicMock:
        if "api.brainzmash.org" in url:
            return err_resp
        return ok_resp

    # Trigger 5 failures on mirror (fallback to musicbrainz.org succeeds)
    with patch.object(client._session, "get", side_effect=mock_get):
        for _ in range(5):
            client._request("https://api.brainzmash.org/ws/2/artist?query=test")

    stats = client.stats()
    assert stats["breaker_state"]["api.brainzmash.org"] == "open"
    assert stats["breaker_state"]["musicbrainz.org"] == "closed"
    assert not client.source_available("api.brainzmash.org")
    assert client.source_available("musicbrainz.org")

    # The 6th request should skip mirror and route directly to musicbrainz.org
    with patch.object(client._session, "get", return_value=ok_resp) as mock_network:
        resp = client._request("https://api.brainzmash.org/ws/2/artist?query=test")
        assert resp is not None
        assert resp.status_code == 200
        # Verify the call was made to musicbrainz.org, not api.brainzmash.org
        assert mock_network.call_count == 1
        called_url = mock_network.call_args[0][0]
        assert "musicbrainz.org" in called_url
        assert "api.brainzmash.org" not in called_url


def test_breaker_both_open_no_network(test_db: Database) -> None:
    """When both mirror and fallback circuit breakers are open, requests fail immediately without network calls."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", min_interval=0.0, store=store)

    # Force trip both breakers
    client._breakers["api.brainzmash.org"]._state = "open"
    client._breakers["api.brainzmash.org"]._last_failure_time = time.monotonic()
    client._breakers["musicbrainz.org"]._state = "open"
    client._breakers["musicbrainz.org"]._last_failure_time = time.monotonic()

    with patch.object(client._session, "get") as mock_get:
        resp = client._request("https://api.brainzmash.org/ws/2/artist?query=test")
        assert resp is None
        assert mock_get.call_count == 0


def test_breaker_half_open_recovers(test_db: Database) -> None:
    """After cooldown, circuit breaker enters half-open and closes upon a successful request."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", min_interval=0.0, store=store)

    breaker = client._breakers["api.brainzmash.org"]
    breaker._state = "open"
    # Set _last_failure_time to 305 seconds ago (cooldown is 300s)
    breaker._last_failure_time = time.monotonic() - 305.0

    assert breaker.state == "half_open"

    ok_resp = MagicMock(status_code=200)
    with patch.object(client._session, "get", return_value=ok_resp):
        resp = client._request("https://api.brainzmash.org/ws/2/artist?query=test")
        assert resp is not None
        assert resp.status_code == 200

    assert breaker.state == "closed"
    assert breaker._consecutive_failures == 0


def test_404_does_not_trip_breaker(test_db: Database) -> None:
    """HTTP 404 responses represent legitimate missing resources and do not count toward breaker failure threshold."""
    store = MbMetadataStore(test_db)
    client = MbidEnricherClient(base_url="https://api.brainzmash.org", min_interval=0.0, store=store)

    not_found_resp = MagicMock(status_code=404)
    not_found_resp.headers = {}

    with patch.object(client._session, "get", return_value=not_found_resp):
        for _ in range(10):
            client._request("https://api.brainzmash.org/ws/2/artist/nonexistent")

    breaker = client._breakers["api.brainzmash.org"]
    assert breaker.state == "closed"
    assert breaker._consecutive_failures == 0


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

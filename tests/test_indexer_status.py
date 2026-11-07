"""Tests for in-memory indexer backoff and status registry."""

from datetime import datetime, timedelta, timezone

import pytest

from trackseerr.clients.acquisition.torznab import IndexerRateLimited
from trackseerr.indexer_status import (
    API_LIMIT_MIN_SECONDS,
    BACKOFF_STEPS_SECONDS,
    indexer_status,
)


@pytest.fixture(autouse=True)
def _reset_indexer_status():
    indexer_status.reset()
    yield
    indexer_status.reset()


def test_first_failure_does_not_pause():
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    state = indexer_status.record_failure("idx-1", RuntimeError("Network blip"), now=now)

    assert state.failures == 1
    assert state.paused_until is None
    assert indexer_status.paused_until("idx-1", now=now) is None


def test_failures_escalate_through_steps():
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)

    # 1st failure: 0s
    s1 = indexer_status.record_failure("idx-2", RuntimeError("Fail 1"), now=now)
    assert s1.failures == 1
    assert s1.paused_until is None

    # 2nd failure: 60s
    s2 = indexer_status.record_failure("idx-2", RuntimeError("Fail 2"), now=now)
    assert s2.failures == 2
    assert s2.paused_until == now + timedelta(seconds=BACKOFF_STEPS_SECONDS[1])
    assert s2.paused_until == now + timedelta(seconds=60)
    assert indexer_status.paused_until("idx-2", now=now) == s2.paused_until

    # 3rd failure: 300s
    s3 = indexer_status.record_failure("idx-2", RuntimeError("Fail 3"), now=now)
    assert s3.failures == 3
    assert s3.paused_until == now + timedelta(seconds=BACKOFF_STEPS_SECONDS[2])
    assert s3.paused_until == now + timedelta(seconds=300)

    # Escalates up to 20th failure: clamped to last step (86400s)
    s20 = None
    for i in range(4, 21):
        s20 = indexer_status.record_failure("idx-2", RuntimeError(f"Fail {i}"), now=now)

    assert s20 is not None
    assert s20.failures == 20
    assert s20.paused_until == now + timedelta(seconds=86400)


def test_success_resets():
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    indexer_status.record_failure("idx-3", RuntimeError("Fail 1"), now=now)
    indexer_status.record_failure("idx-3", RuntimeError("Fail 2"), now=now)
    assert indexer_status.paused_until("idx-3", now=now) is not None

    indexer_status.record_success("idx-3")
    assert indexer_status.paused_until("idx-3", now=now) is None
    assert indexer_status.snapshot("idx-3") is None


def test_retry_after_extends_pause():
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    exc = IndexerRateLimited("Rate limit hit", retry_after=600)
    state = indexer_status.record_failure("idx-4", exc, now=now)

    assert state.failures == 1
    assert state.paused_until == now + timedelta(seconds=600)
    assert indexer_status.paused_until("idx-4", now=now) == now + timedelta(seconds=600)


def test_api_limit_code_floors_one_hour():
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    exc = IndexerRateLimited("API limit reached", code=500, retry_after=None)
    state = indexer_status.record_failure("idx-5", exc, now=now)

    assert state.failures == 1
    assert state.paused_until is not None
    assert (state.paused_until - now).total_seconds() >= API_LIMIT_MIN_SECONDS
    assert (state.paused_until - now).total_seconds() >= 3600


def test_paused_until_expires():
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    indexer_status.record_failure("idx-6", RuntimeError("Fail 1"), now=now)
    indexer_status.record_failure("idx-6", RuntimeError("Fail 2"), now=now)

    # Within pause window
    assert indexer_status.paused_until("idx-6", now=now + timedelta(seconds=30)) is not None

    # After pause window (60s step expired)
    assert indexer_status.paused_until("idx-6", now=now + timedelta(seconds=61)) is None


def test_last_error_redacted():
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    exc = RuntimeError("HTTP 401 https://indexer.example.com/api?apikey=SECRET123KEY failed")
    state = indexer_status.record_failure("idx-7", exc, now=now)

    assert "SECRET123KEY" not in state.last_error

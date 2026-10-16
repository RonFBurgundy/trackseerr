"""Tests for the media-server refresh coalescing scheduler."""

import logging
from unittest.mock import MagicMock

import pytest

from trackseerr.config import Config
from trackseerr.media_servers import refresh_scheduler
from trackseerr.media_servers.base import MediaServer, MediaServerError


@pytest.fixture(autouse=True)
def reset_scheduler():
    refresh_scheduler.reset_for_tests()
    yield
    refresh_scheduler.reset_for_tests()


def test_burst_of_requests_fires_once(monkeypatch):
    t = 1000.0
    monkeypatch.setattr(refresh_scheduler, "_monotonic", lambda: t)
    target = MagicMock(spec=MediaServer)
    target.kind = "plex"

    for i in range(30):
        refresh_scheduler.request_library_refresh(target, reason=f"reason_{i}")

    assert refresh_scheduler._scheduler._timer is not None
    assert refresh_scheduler.flush_now() is True
    target.refresh_library.assert_called_once()


def test_latest_server_wins(monkeypatch):
    t = 1000.0
    monkeypatch.setattr(refresh_scheduler, "_monotonic", lambda: t)
    target1 = MagicMock(spec=MediaServer)
    target1.kind = "plex"
    target2 = MagicMock(spec=MediaServer)
    target2.kind = "jellyfin"

    refresh_scheduler.request_library_refresh(target1, reason="first")
    refresh_scheduler.request_library_refresh(target2, reason="second")

    assert refresh_scheduler.flush_now() is True
    target1.refresh_library.assert_not_called()
    target2.refresh_library.assert_called_once()


def test_second_burst_waits_for_window(monkeypatch):
    current_time = 1000.0
    monkeypatch.setattr(refresh_scheduler, "_monotonic", lambda: current_time)
    refresh_scheduler.configure(10)  # window = 600s
    target = MagicMock(spec=MediaServer)
    target.kind = "plex"

    refresh_scheduler.request_library_refresh(target, reason="first")
    assert refresh_scheduler.flush_now() is True
    target.refresh_library.assert_called_once()

    # Advance time by 30 seconds
    current_time += 30.0
    refresh_scheduler.request_library_refresh(target, reason="second")

    # Expected delay = window (600s) - 30s elapsed = 570s
    expected_delay = 600.0 - 30.0
    delay = refresh_scheduler.pending_delay()
    assert delay is not None
    assert abs(delay - expected_delay) < 1e-3


def test_zero_window_disables(caplog):
    refresh_scheduler.configure(0)
    target = MagicMock(spec=MediaServer)
    target.kind = "plex"

    with caplog.at_level(logging.DEBUG):
        refresh_scheduler.request_library_refresh(target, reason="test")
        # Second call to verify it logs at DEBUG once
        refresh_scheduler.request_library_refresh(target, reason="test2")

    assert refresh_scheduler.pending_delay() is None
    assert refresh_scheduler._scheduler._timer is None
    assert refresh_scheduler.flush_now() is False
    target.refresh_library.assert_not_called()
    assert caplog.text.count("disabled (window=0)") == 1


def test_none_server_is_noop():
    refresh_scheduler.request_library_refresh(None, reason="test")
    assert refresh_scheduler.pending_delay() is None
    assert refresh_scheduler._scheduler._timer is None
    assert refresh_scheduler.flush_now() is False


def test_refresh_error_is_logged_not_raised(caplog):
    target = MagicMock(spec=MediaServer)
    target.kind = "plex"
    target.refresh_library.side_effect = MediaServerError("Server exploded", safe_detail="Server exploded")

    refresh_scheduler.request_library_refresh(target, reason="test")
    with caplog.at_level(logging.WARNING):
        assert refresh_scheduler.flush_now() is True

    target.refresh_library.assert_called_once()
    assert "Server exploded" in caplog.text or "rescan failed" in caplog.text
    assert refresh_scheduler._scheduler._last_fired is not None


def test_info_logging_on_successful_refresh(caplog):
    target = MagicMock(spec=MediaServer)
    target.kind = "plex"

    refresh_scheduler.request_library_refresh(target, reason="import")
    refresh_scheduler.request_library_refresh(target, reason="rename")
    refresh_scheduler.request_library_refresh(target, reason="import")
    refresh_scheduler.request_library_refresh(target, reason="retag")
    refresh_scheduler.request_library_refresh(target, reason="scan")

    with caplog.at_level(logging.INFO):
        assert refresh_scheduler.flush_now() is True

    target.refresh_library.assert_called_once()
    assert "Asked plex to rescan its music library (5 change(s) batched: import, rename, retag)" in caplog.text


def test_config_parses_refresh_minutes(monkeypatch):
    monkeypatch.setenv("PLEX_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "token")

    monkeypatch.setenv("MEDIA_SERVER_REFRESH_MINUTES", "5")
    assert Config.from_env().media_server_refresh_minutes == 5

    monkeypatch.setenv("MEDIA_SERVER_REFRESH_MINUTES", "abc")
    assert Config.from_env().media_server_refresh_minutes == 10

    monkeypatch.setenv("MEDIA_SERVER_REFRESH_MINUTES", "-3")
    assert Config.from_env().media_server_refresh_minutes == 0

    monkeypatch.delenv("MEDIA_SERVER_REFRESH_MINUTES", raising=False)
    assert Config.from_env().media_server_refresh_minutes == 10

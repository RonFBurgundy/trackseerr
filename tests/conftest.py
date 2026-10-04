"""Pytest configuration and global fixtures for TrackSeerr test suite."""

import os

# Default to legacy UI for backwards compatibility with existing frontend tests.
# New React SPA tests explicitly unset or set TRACKSEERR_LEGACY_UI to '0'.
os.environ["TRACKSEERR_LEGACY_UI"] = "1"

import pytest

# The Lidarr contract tests need a real Lidarr: not even collected unless RUN_INTEGRATION=1 (docs/INTEGRATION_TESTS.md).
collect_ignore_glob = [] if os.environ.get("RUN_INTEGRATION") == "1" else ["integration/*"]


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_core_client: opt out of the autouse CoreClient.session_status mock so the real signed "
        "gateway->core call path runs (see tests/test_gateway_core_e2e.py)",
    )


@pytest.fixture(autouse=True)
def _gateway_session_status_allows_by_default(request, monkeypatch):
    """Gateway tests have no real core: let session-status say "valid" unless a test overrides it.

    Opt out with ``@pytest.mark.real_core_client`` (module-level ``pytestmark`` works too); the real
    ``CoreClient.session_status`` then runs. The session-status cache is still cleared around the test.
    """
    from plex_playlist_sync.api import dependencies
    from plex_playlist_sync.clients.core_client import CoreClient

    dependencies.clear_session_status_cache()
    if request.node.get_closest_marker("real_core_client") is None:
        monkeypatch.setattr(CoreClient, "session_status", lambda self, user_id, issued, **kw: (200, {"valid": True}))
    yield
    dependencies.clear_session_status_cache()


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """Tiered ``create_app`` / ``cli.main`` call ``setup_logging``, which attaches handlers bound to the
    per-test captured ``sys.stdout``. Left on the root logger they hold a closed stream and break whichever
    test runs next (order-dependent failures under xdist). Remove whatever a test added and restore the level."""
    import logging

    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in before_handlers and type(handler).__name__ != "LogRingBuffer":
            root.removeHandler(handler)
            handler.close()
    root.setLevel(before_level)


@pytest.fixture(autouse=True)
def _reset_library_manager_guard():
    """The work-guard counters are module state; a test that dies mid-work must not leave later tests unable to switch."""
    from plex_playlist_sync import library_manager

    with library_manager._guard_lock:
        for mode in library_manager._in_flight:
            library_manager._in_flight[mode] = 0
    yield


@pytest.fixture(autouse=True)
def _reset_lidarr_add_defaults_cache():
    """Root-folder defaults are cached per process (keyed by URL and key); tests reusing a URL must not share them."""
    from plex_playlist_sync.clients.lidarr import invalidate_add_defaults

    invalidate_add_defaults()
    yield
    invalidate_add_defaults()

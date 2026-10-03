"""Pytest configuration and global fixtures for TrackSeerr test suite."""

import os

# Default to legacy UI for backwards compatibility with existing frontend tests.
# New React SPA tests explicitly unset or set TRACKSEERR_LEGACY_UI to '0'.
os.environ["TRACKSEERR_LEGACY_UI"] = "1"

import pytest


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
        monkeypatch.setattr(CoreClient, "session_status", lambda self, user_id, issued: (200, {"valid": True}))
    yield
    dependencies.clear_session_status_cache()

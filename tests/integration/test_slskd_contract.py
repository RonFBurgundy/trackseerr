"""Contract tests for the slskd driver against a REAL slskd (opt-in, see docs/INTEGRATION_TESTS.md).

Skipped unless ``SLSKD_IT_URL`` is set. Also set ``SLSKD_IT_API_KEY``.
"""

import os

import pytest

from trackseerr.clients.acquisition.slskd import SlskdDriver

URL = os.environ.get("SLSKD_IT_URL", "").rstrip("/")
API_KEY = os.environ.get("SLSKD_IT_API_KEY", "")

pytestmark = pytest.mark.skipif(not URL, reason="SLSKD_IT_URL not set (needs a throwaway slskd)")


def test_valid_api_key_succeeds():
    if not API_KEY:
        pytest.skip("SLSKD_IT_API_KEY not set")
    ok, msg = SlskdDriver(URL, api_key=API_KEY).test_connection()
    assert ok is True, msg
    assert msg.startswith("slskd ")


def test_wrong_api_key_fails_auth():
    ok, msg = SlskdDriver(URL, api_key="definitely-not-the-key-000000").test_connection()
    assert ok is False
    assert "Authentication failed" in msg


def test_download_roots_non_empty():
    if not API_KEY:
        pytest.skip("SLSKD_IT_API_KEY not set")
    roots = SlskdDriver(URL, api_key=API_KEY).get_download_roots()
    assert roots

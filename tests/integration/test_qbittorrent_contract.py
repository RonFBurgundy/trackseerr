"""qBittorrent driver against a REAL qBittorrent (docs/INTEGRATION_TESTS.md). Opt-in: set QBIT_IT_URL.

Env: QBIT_IT_URL (the WebUI's own port), QBIT_IT_USER (default admin), QBIT_IT_PASSWORD.
Never adds torrents.
"""

import os

import pytest

from trackseerr.clients.acquisition.qbittorrent import QbittorrentDriver

pytestmark = pytest.mark.integration

URL = os.environ.get("QBIT_IT_URL", "")
USER = os.environ.get("QBIT_IT_USER", "admin")
PASSWORD = os.environ.get("QBIT_IT_PASSWORD", "")

if not URL:
    pytest.skip("QBIT_IT_URL not set", allow_module_level=True)


def test_right_password_connects() -> None:
    ok, msg = QbittorrentDriver(URL, USER, PASSWORD).test_connection()
    assert ok, msg
    assert msg.startswith("qBittorrent")


def test_wrong_password_fails() -> None:
    ok, _msg = QbittorrentDriver(URL, USER, PASSWORD + "-wrong").test_connection()
    assert ok is False


def test_download_roots_is_a_list() -> None:
    driver = QbittorrentDriver(URL, USER, PASSWORD)
    assert isinstance(driver.get_download_roots(), list)

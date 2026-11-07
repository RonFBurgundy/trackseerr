"""qBittorrent driver against a REAL qBittorrent (docs/INTEGRATION_TESTS.md).

Uses the qbit_target session fixture. Adds only magnets with a random 40-hex infohash
and no trackers (never touches peers or the internet).
"""

import secrets
import time

import httpx
import pytest

from trackseerr.clients.acquisition.qbittorrent import QbittorrentDriver
from trackseerr.models import AcquisitionSearchResult, DownloadStatus

pytestmark = pytest.mark.integration


def test_right_password_connects(qbit_target: tuple[str, str, str]) -> None:
    url, user, password = qbit_target
    ok, msg = QbittorrentDriver(url, user, password).test_connection()
    assert ok, msg
    assert msg.startswith("qBittorrent")


def test_wrong_password_fails(qbit_target: tuple[str, str, str]) -> None:
    url, user, password = qbit_target
    ok, _msg = QbittorrentDriver(url, user, password + "-wrong").test_connection()
    assert ok is False


def test_download_roots_is_a_list(qbit_target: tuple[str, str, str]) -> None:
    url, user, password = qbit_target
    driver = QbittorrentDriver(url, user, password)
    assert isinstance(driver.get_download_roots(), list)


def test_magnet_add_status_and_remove(qbit_target: tuple[str, str, str]) -> None:
    url, user, password = qbit_target
    driver = QbittorrentDriver(url, user, password)
    infohash = secrets.token_hex(20)
    magnet = f"magnet:?xt=urn:btih:{infohash}&dn=trackseerr-test"
    res = AcquisitionSearchResult(
        download_id=infohash,
        title="trackseerr-test",
        artist="test-artist",
        magnet_url=magnet,
    )
    returned_hash = driver.download(res)
    assert returned_hash == infohash.lower()
    try:
        deadline = time.monotonic() + 15.0
        status_dict = None
        while time.monotonic() < deadline:
            st = driver.get_status(returned_hash)
            if st.get("status") in (DownloadStatus.DOWNLOADING.value, DownloadStatus.QUEUED.value):
                status_dict = st
                break
            time.sleep(0.5)
        assert status_dict is not None, f"Expected non-terminal status within 15s, got: {driver.get_status(returned_hash)}"
    finally:
        assert driver.cancel(returned_hash) is True

    with httpx.Client(timeout=10.0) as client:
        torrents = driver._get_json(client, f"/api/v2/torrents/info?hashes={returned_hash}")
        assert torrents == []


def test_category_is_created_and_used(qbit_target: tuple[str, str, str]) -> None:
    url, user, password = qbit_target
    driver = QbittorrentDriver(url, user, password, category="trackseerr")
    infohash = secrets.token_hex(20)
    magnet = f"magnet:?xt=urn:btih:{infohash}&dn=trackseerr-cat-test"
    res = AcquisitionSearchResult(
        download_id=infohash,
        title="trackseerr-cat-test",
        artist="test-artist",
        magnet_url=magnet,
    )
    download_id = driver.download(res)
    try:
        deadline = time.monotonic() + 15.0
        cat = None
        with httpx.Client(timeout=10.0) as client:
            while time.monotonic() < deadline:
                items = driver._get_json(client, f"/api/v2/torrents/info?hashes={download_id}")
                if items and isinstance(items, list):
                    cat = items[0].get("category")
                    if cat == "trackseerr":
                        break
                time.sleep(0.5)
        assert cat == "trackseerr", f"Expected category 'trackseerr', got {cat!r}"
    finally:
        driver.cancel(download_id)


def test_set_share_limits_accepts_values(qbit_target: tuple[str, str, str]) -> None:
    url, user, password = qbit_target
    driver = QbittorrentDriver(url, user, password)
    infohash = secrets.token_hex(20)
    magnet = f"magnet:?xt=urn:btih:{infohash}&dn=trackseerr-limits-test"
    res = AcquisitionSearchResult(
        download_id=infohash,
        title="trackseerr-limits-test",
        artist="test-artist",
        magnet_url=magnet,
    )
    download_id = driver.download(res)
    try:
        ok = driver.set_share_limits(download_id, ratio=2.5, seed_time_minutes=180)
        assert ok is True
    finally:
        driver.cancel(download_id)

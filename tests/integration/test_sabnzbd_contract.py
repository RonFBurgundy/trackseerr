"""SABnzbd driver against a REAL SABnzbd (docs/INTEGRATION_TESTS.md).

Uses the sab_target session fixture. Never contacts external Usenet news servers or indexers.
"""

import time

import pytest

from trackseerr.clients.acquisition.sabnzbd import SabnzbdDriver
from trackseerr.models import AcquisitionSearchResult, DownloadStatus

pytestmark = pytest.mark.integration


def test_valid_key_connects(sab_target: tuple[str, str]) -> None:
    url, key = sab_target
    ok, msg = SabnzbdDriver(url, api_key=key).test_connection()
    assert ok, msg
    assert msg.startswith("SABnzbd")


def test_wrong_key_fails(sab_target: tuple[str, str]) -> None:
    url, _key = sab_target
    ok, _msg = SabnzbdDriver(url, api_key="wrong_test_key_12345").test_connection()
    assert ok is False


def test_download_roots_is_a_list(sab_target: tuple[str, str]) -> None:
    url, key = sab_target
    driver = SabnzbdDriver(url, api_key=key, category="trackseerr")
    roots = driver.get_download_roots()
    assert isinstance(roots, list)
    assert len(roots) > 0


def test_unfetchable_nzb_ends_failed(sab_target: tuple[str, str]) -> None:
    url, key = sab_target
    driver = SabnzbdDriver(url, api_key=key, category="trackseerr")
    result = AcquisitionSearchResult(
        download_id="test-unfetchable-nzb",
        title="Test Unfetchable Track",
        artist="Test Artist",
        download_url="http://127.0.0.1:8080/nonexistent.nzb",
    )
    nzo_id = driver.download(result)
    assert nzo_id
    try:
        deadline = time.monotonic() + 60.0
        final_status = None
        while time.monotonic() < deadline:
            st = driver.get_status(nzo_id)
            if st.get("status") == DownloadStatus.FAILED.value:
                final_status = st
                break
            time.sleep(1.0)
        assert final_status is not None, f"Expected failed status within 60s, got: {driver.get_status(nzo_id)}"
        assert final_status.get("error_message"), "Expected non-empty error_message on failed download"
    finally:
        driver.cancel(nzo_id)
        driver.cleanup_completed(nzo_id, delete_files=True)

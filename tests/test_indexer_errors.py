"""Tests for Torznab / Newznab typed indexer errors and search mode fallbacks."""

from unittest.mock import MagicMock, patch
import httpx
import pytest

from trackseerr.clients.acquisition.torznab import (
    IndexerAuthError,
    IndexerRateLimited,
    IndexerUnavailable,
    TorznabDriver,
    reset_search_mode_cache,
)

TORZNAB_RESULTS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Prowlarr Search</title>
    <item>
      <title>Pink Floyd - The Dark Side of the Moon (1973) [FLAC]</title>
      <guid>http://prowlarr:9696/1/details/101</guid>
      <size>280000000</size>
      <category>3040</category>
      <enclosure url="magnet:?xt=urn:btih:fedcba9876543210fedcba9876543210fedcba98&amp;dn=Pink+Floyd" length="280000000" type="application/x-bittorrent" />
      <torznab:attr name="seeders" value="25" />
      <torznab:attr name="peers" value="30" />
    </item>
  </channel>
</rss>
"""


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_search_mode_cache()
    yield
    reset_search_mode_cache()


def _make_driver() -> TorznabDriver:
    return TorznabDriver("http://prowlarr:9696/1/api", api_key="prowlarrkey")


def _make_resp(status_code: int = 200, text: str = "", headers: dict = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    return resp


def test_error_reply_api_limit_raises_rate_limited():
    driver = _make_driver()
    xml = '<error code="500" description="Request limit reached"/>'
    resp = _make_resp(200, xml)

    with patch("httpx.Client.get", return_value=resp):
        with pytest.raises(IndexerRateLimited) as exc_info:
            driver.search(artist="Pink Floyd")
        assert exc_info.value.code == 500
        assert "Request limit reached" in str(exc_info.value)


def test_error_reply_bad_key_raises_auth():
    driver = _make_driver()
    xml = '<error code="100" description="Incorrect user credentials"/>'
    resp = _make_resp(200, xml)

    with patch("httpx.Client.get", return_value=resp):
        with pytest.raises(IndexerAuthError) as exc_info:
            driver.search(artist="Pink Floyd")
        assert exc_info.value.code == 100
        assert "Incorrect user credentials" in str(exc_info.value)
        assert "apikey" not in str(exc_info.value).lower()


def test_http_429_reads_retry_after():
    driver = _make_driver()
    resp = _make_resp(429, "Rate limited", headers={"Retry-After": "120"})

    with patch("httpx.Client.get", return_value=resp):
        with pytest.raises(IndexerRateLimited) as exc_info:
            driver.search(artist="Pink Floyd")
        assert exc_info.value.code == 429
        assert exc_info.value.retry_after == 120


def test_http_401_raises_auth():
    driver = _make_driver()
    resp = _make_resp(401, "Unauthorized")

    with patch("httpx.Client.get", return_value=resp):
        with pytest.raises(IndexerAuthError) as exc_info:
            driver.search(artist="Pink Floyd")
        assert exc_info.value.code == 401


def test_http_503_raises_unavailable():
    driver = _make_driver()
    resp = _make_resp(503, "Service Unavailable")

    with patch("httpx.Client.get", return_value=resp):
        with pytest.raises(IndexerUnavailable) as exc_info:
            driver.search(artist="Pink Floyd")
        assert exc_info.value.code == 503
        assert "503" in str(exc_info.value)


def test_network_error_raises_unavailable():
    driver = _make_driver()
    with patch("httpx.Client.get", side_effect=httpx.ConnectError("Connection refused")):
        with pytest.raises(IndexerUnavailable) as exc_info:
            driver.search(artist="Pink Floyd")
        assert "Connection refused" in str(exc_info.value)


def test_unreadable_xml_raises_unavailable():
    driver = _make_driver()
    resp = _make_resp(200, "not valid xml <<<")

    with patch("httpx.Client.get", return_value=resp):
        with pytest.raises(IndexerUnavailable) as exc_info:
            driver.search(artist="Pink Floyd")
        assert "Unreadable reply from indexer" in str(exc_info.value)


def test_music_unsupported_falls_back_to_search():
    driver = _make_driver()
    caps_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <caps>
      <server title="Indexer" />
      <searching>
        <music-search available="no" />
      </searching>
    </caps>
    """
    search_results_xml = TORZNAB_RESULTS_XML

    urls_requested = []

    def fake_get(url, **kwargs):
        urls_requested.append(url)
        if "t=caps" in url:
            return _make_resp(200, caps_xml)
        if "t=search" in url:
            return _make_resp(200, search_results_xml)
        return _make_resp(404, "Not Found")

    with patch("httpx.Client.get", side_effect=fake_get):
        results = driver.search(artist="Pink Floyd", album="The Dark Side of the Moon")
        assert len(results) == 1
        assert "Pink Floyd" in results[0].title
        assert any("t=search" in u for u in urls_requested)
        assert not any("t=music" in u for u in urls_requested)


def test_music_error_202_retries_with_search_and_caches():
    driver = _make_driver()
    error_203_xml = '<error code="203" description="Function not available"/>'
    search_results_xml = TORZNAB_RESULTS_XML

    urls_requested = []

    def fake_get(url, **kwargs):
        urls_requested.append(url)
        if "t=caps" in url:
            return _make_resp(500, "Caps probe failed")
        if "t=music" in url:
            return _make_resp(200, error_203_xml)
        if "t=search" in url:
            return _make_resp(200, search_results_xml)
        return _make_resp(404, "Not Found")

    with patch("httpx.Client.get", side_effect=fake_get):
        # First search: caps fails -> tries t=music -> gets 203 -> retries t=search -> succeeds
        results1 = driver.search(artist="Pink Floyd", album="The Dark Side of the Moon")
        assert len(results1) == 1
        assert "Pink Floyd" in results1[0].title

        first_search_urls = list(urls_requested)
        assert any("t=caps" in u for u in first_search_urls)
        assert any("t=music" in u for u in first_search_urls)
        assert any("t=search" in u for u in first_search_urls)

        # Second search: cached mode is search, should go straight to t=search
        urls_requested.clear()
        results2 = driver.search(artist="Pink Floyd", album="The Dark Side of the Moon")
        assert len(results2) == 1
        assert len(urls_requested) == 1
        assert "t=search" in urls_requested[0]
        assert "t=caps" not in urls_requested[0]
        assert "t=music" not in urls_requested[0]


def test_caps_failure_defaults_to_music():
    driver = _make_driver()
    search_results_xml = TORZNAB_RESULTS_XML

    urls_requested = []

    def fake_get(url, **kwargs):
        urls_requested.append(url)
        if "t=caps" in url:
            return _make_resp(500, "Caps error")
        if "t=music" in url:
            return _make_resp(200, search_results_xml)
        return _make_resp(404, "Not Found")

    with patch("httpx.Client.get", side_effect=fake_get):
        results = driver.search(artist="Pink Floyd", album="The Dark Side of the Moon")
        assert len(results) == 1
        assert any("t=caps" in u for u in urls_requested)
        assert any("t=music" in u for u in urls_requested)
        assert not any("t=search" in u for u in urls_requested)


def test_test_connection_reports_error_description():
    driver = _make_driver()
    xml = '<error code="100" description="Incorrect user credentials"/>'
    resp = _make_resp(200, xml)

    with patch("httpx.Client.get", return_value=resp):
        ok, msg = driver.test_connection()
        assert ok is False
        assert msg == "Incorrect user credentials"


def test_fetch_recent_error_reply_returns_empty():
    driver = _make_driver()
    xml = '<error code="500" description="Request limit reached"/>'
    resp = _make_resp(200, xml)

    with patch("httpx.Client.get", return_value=resp):
        results = driver.fetch_recent()
        assert results == []

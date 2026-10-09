"""Unit tests for Unified Acquisition Drivers and Indexers.

Covers:
- slskd driver (test_connection, search, download, get_status, cancel)
- SABnzbd driver (test_connection, download, get_status, cancel)
- qBittorrent driver (test_connection, download, get_status, cancel)
- Torznab indexer (test_connection, search with XML parsing)
- LidarrAdapter driver (wrapping LidarrClient)
- Driver and Indexer factories (get_acquisition_driver, get_indexer_driver)
- SSRF defense rejection across all drivers and indexers
"""

from unittest.mock import MagicMock, patch
import pytest
import httpx

from trackseerr.clients.acquisition.base import AcquisitionDriver
from trackseerr.clients.acquisition.slskd import SlskdDriver
from trackseerr.clients.acquisition.sabnzbd import SabnzbdDriver
from trackseerr.clients.acquisition.qbittorrent import QbittorrentDriver
from trackseerr.clients.acquisition.torznab import TorznabDriver
from trackseerr.clients.acquisition.lidarr_adapter import LidarrAdapter
from trackseerr.clients.acquisition import get_acquisition_driver, get_indexer_driver
from trackseerr.models import (
    AcquisitionSearchResult,
    DownloadClientConfig,
    DownloadDriverType,
    DownloadStatus,
    IndexerConfig,
)


# ---------------------------------------------------------------------------
# SSRF Validation Tests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://user:pass@192.168.1.1:8080",
        "ftp://example.com/files",
        "file:///etc/passwd",
    ],
)
def test_driver_ssrf_rejection(unsafe_url: str):
    """Ensures drivers refuse to connect or search with prohibited/SSRF URLs."""
    slskd = SlskdDriver(unsafe_url, api_key="password")
    ok, msg = slskd.test_connection()
    assert ok is False
    assert "SSRF" in msg

    sab = SabnzbdDriver(unsafe_url, api_key="secret")
    ok, msg = sab.test_connection()
    assert ok is False
    assert "SSRF" in msg

    qbit = QbittorrentDriver(unsafe_url, "admin", "adminadmin")
    ok, msg = qbit.test_connection()
    assert ok is False
    assert "SSRF" in msg

    torznab = TorznabDriver(unsafe_url, api_key="secret")
    ok, msg = torznab.test_connection()
    assert ok is False
    assert "SSRF" in msg


# ---------------------------------------------------------------------------
# slskd Driver Tests
# ---------------------------------------------------------------------------
def test_slskd_test_connection_success():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '{"version": "0.20.0"}'
    mock_resp.json.return_value = {"version": "0.20.0"}

    with patch("httpx.Client.get", return_value=mock_resp):
        ok, msg = driver.test_connection()
        assert ok is True
        assert "0.20.0" in msg


def test_slskd_test_connection_failure():
    driver = SlskdDriver("http://slskd.local:5030", api_key="wrongkey")
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized"

    with patch("httpx.Client.get", return_value=mock_resp):
        ok, msg = driver.test_connection()
        assert ok is False
        assert "Authentication failed" in msg


def test_slskd_search_music():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")

    init_resp = MagicMock()
    init_resp.status_code = 200
    init_resp.json.return_value = {"id": "search-uuid-123"}

    # slskd returns a list of user response objects
    poll_resp = MagicMock()
    poll_resp.status_code = 200
    poll_resp.json.return_value = [
        {
            "username": "music_fan",
            "uploadSpeed": 500000,
            "files": [
                {
                    "filename": "Music/Radiohead - Kid A/01 - Everything In Its Right Place.flac",
                    "size": 25000000,
                    "bitRate": 950,
                    "extension": "flac",
                },
                {
                    "filename": "Music/Radiohead - Kid A/cover.jpg",
                    "size": 50000,
                    "extension": "jpg",
                },
            ],
        }
    ]

    with patch("httpx.Client.post", return_value=init_resp):
        with patch("httpx.Client.get", return_value=poll_resp):
            results = driver.search(artist="Radiohead", title="Kid A")
            assert len(results) == 1
            r = results[0]
            assert r.title == "Kid A"
            assert "Everything In Its Right Place" in r.extra["filename"]
            assert r.source == "slskd"
            assert r.format == "flac"
            assert r.bit_rate == 950
            assert r.size_bytes == 25000000


def test_slskd_download_and_status():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    mock_post_resp = MagicMock()
    mock_post_resp.status_code = 200

    item = AcquisitionSearchResult(
        download_id="music_fan::Music%2Ftrack.flac",
        title="track.flac",
        artist="Artist",
        source="slskd",
        extra={"username": "music_fan", "filename": "Music/track.flac", "size": 25000000},
    )

    with patch("httpx.Client.post", return_value=mock_post_resp):
        dl_id = driver.download(item)
        assert "music_fan" in dl_id

    mock_get_resp = MagicMock()
    mock_get_resp.status_code = 200
    mock_get_resp.json.return_value = [
        {
            "username": "music_fan",
            "directories": [
                {
                    "files": [
                        {
                            "id": "file-123",
                            "filename": "Music/track.flac",
                            "state": "Completed, Succeeded",
                            "bytesTransferred": 25000000,
                            "size": 25000000,
                            "averageSpeed": 100000,
                        }
                    ]
                }
            ],
        }
    ]

    with patch("httpx.Client.get", return_value=mock_get_resp):
        status = driver.get_status(dl_id)
        assert status["status"] == DownloadStatus.COMPLETED.value
        assert status["progress"] == 100.0


def test_slskd_cancel():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    mock_del_resp = MagicMock()
    mock_del_resp.status_code = 200

    with patch("httpx.Client.request", return_value=mock_del_resp):
        success = driver.cancel("music_fan::Music%2Ftrack.flac")
        assert success is True


# ---------------------------------------------------------------------------
# SABnzbd Driver Tests
# ---------------------------------------------------------------------------
def test_sabnzbd_test_connection_success():
    driver = SabnzbdDriver("http://sabnzbd:8080", api_key="secretapikey")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"version": "4.2.1"}

    with patch("httpx.Client.get", return_value=mock_resp):
        ok, msg = driver.test_connection()
        assert ok is True
        assert "4.2.1" in msg


def test_sabnzbd_download_and_status():
    driver = SabnzbdDriver("http://sabnzbd:8080", api_key="secretapikey", category="music")
    mock_add_resp = MagicMock()
    mock_add_resp.status_code = 200
    mock_add_resp.json.return_value = {"status": True, "nzo_ids": ["SABnzbd_nzo_abc123"]}

    item = AcquisitionSearchResult(
        download_id="nzb-1",
        title="Discovery",
        artist="Daft Punk",
        download_url="https://indexer.com/getnzb/12345.nzb",
        source="sabnzbd",
    )

    with patch("httpx.Client.get", return_value=mock_add_resp):
        nzo_id = driver.download(item)
        assert nzo_id == "SABnzbd_nzo_abc123"

    # Status check
    mock_q_resp = MagicMock()
    mock_q_resp.status_code = 200
    mock_q_resp.json.return_value = {
        "queue": {
            "slots": [
                {
                    "nzo_id": "SABnzbd_nzo_abc123",
                    "status": "Downloading",
                    "percentage": "45.0",
                    "filename": "Album.nzb",
                    "mb": "100",
                    "kbpersec": "1500",
                }
            ]
        }
    }

    with patch("httpx.Client.get", return_value=mock_q_resp):
        status = driver.get_status(nzo_id)
        assert status["status"] == DownloadStatus.DOWNLOADING.value
        assert status["progress"] == 45.0


def test_sabnzbd_download_unsafe_url_rejected():
    driver = SabnzbdDriver("http://sabnzbd:8080", api_key="secret")
    item = AcquisitionSearchResult(
        download_id="nzb-evil",
        title="Evil Track",
        artist="Evil Artist",
        download_url="http://169.254.169.254/latest/meta-data",
        source="sabnzbd",
    )
    with pytest.raises(ValueError, match="Unsafe download URL"):
        driver.download(item)


def test_sabnzbd_cancel():
    driver = SabnzbdDriver("http://sabnzbd:8080", api_key="secretapikey")
    mock_del_resp = MagicMock()
    mock_del_resp.status_code = 200
    mock_del_resp.json.return_value = {"status": True}

    with patch("httpx.Client.get", return_value=mock_del_resp):
        success = driver.cancel("SABnzbd_nzo_abc123")
        assert success is True


# ---------------------------------------------------------------------------
# qBittorrent Driver Tests
# ---------------------------------------------------------------------------
def test_qbittorrent_test_connection_success():
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "adminadmin")

    login_resp = MagicMock()
    login_resp.status_code = 200
    login_resp.text = "Ok."
    login_resp.cookies = {"SID": "valid_session_cookie"}

    ver_resp = MagicMock()
    ver_resp.status_code = 200
    ver_resp.text = "v4.6.3"

    with patch("httpx.Client.post", return_value=login_resp):
        with patch("httpx.Client.get", return_value=ver_resp):
            ok, msg = driver.test_connection()
            assert ok is True
            assert "v4.6.3" in msg


def test_qbittorrent_download_and_status():
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "adminadmin")

    login_resp = MagicMock()
    login_resp.status_code = 200
    login_resp.text = "Ok."
    login_resp.cookies = {"SID": "test_sid"}

    add_resp = MagicMock()
    add_resp.status_code = 200
    add_resp.text = "Ok."

    item = AcquisitionSearchResult(
        download_id="0123456789abcdef0123456789abcdef01234567",
        title="RAM",
        artist="Daft Punk",
        magnet_url="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Music",
        source="qbittorrent",
    )

    with patch("httpx.Client.post", side_effect=[login_resp, add_resp]):
        torrent_id = driver.download(item)
        assert torrent_id == "0123456789abcdef0123456789abcdef01234567"

    # Status check
    info_resp = MagicMock()
    info_resp.status_code = 200
    info_resp.json.return_value = [
        {
            "hash": "0123456789abcdef0123456789abcdef01234567",
            "name": "Album",
            "state": "downloading",
            "progress": 0.725,
            "dlspeed": 500000,
            "eta": 120,
            "content_path": "/downloads/Album",
        }
    ]

    with patch("httpx.Client.get", return_value=info_resp):
        status = driver.get_status(torrent_id)
        assert status["status"] == DownloadStatus.DOWNLOADING.value
        assert status["progress"] == 72.5
        assert status["speed_bps"] == 500000


# ---------------------------------------------------------------------------
# Torznab Indexer Tests
# ---------------------------------------------------------------------------
TORZNAB_CAPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server title="Prowlarr Music Indexer" version="1.0" />
  <searching>
    <music-search available="yes" supportedParams="q,artist,album,track" />
  </searching>
  <categories>
    <category id="3000" name="Audio" />
    <category id="3010" name="Audio/MP3" />
    <category id="3020" name="Audio/Lossless" />
  </categories>
</caps>
"""

TORZNAB_RESULTS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <title>Prowlarr Search</title>
    <item>
      <title>Pink Floyd - The Dark Side of the Moon (1973) [FLAC]</title>
      <guid>http://prowlarr:9696/1/details/101</guid>
      <size>280000000</size>
      <category>3020</category>
      <enclosure url="magnet:?xt=urn:btih:fedcba9876543210fedcba9876543210fedcba98&amp;dn=Pink+Floyd" length="280000000" type="application/x-bittorrent" />
      <torznab:attr name="seeders" value="25" />
      <torznab:attr name="peers" value="30" />
    </item>
  </channel>
</rss>
"""


def test_torznab_test_connection_success():
    indexer = TorznabDriver("http://prowlarr:9696/1/api", api_key="prowlarrkey")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = TORZNAB_CAPS_XML

    with patch("httpx.Client.get", return_value=mock_resp):
        ok, msg = indexer.test_connection()
        assert ok is True
        assert "Online" in msg


def test_torznab_search_music():
    indexer = TorznabDriver("http://prowlarr:9696/1/api", api_key="prowlarrkey")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = TORZNAB_RESULTS_XML

    with patch("httpx.Client.get", return_value=mock_resp):
        results = indexer.search(artist="Pink Floyd", album="The Dark Side of the Moon")
        assert len(results) == 1
        item = results[0]
        assert "Pink Floyd" in item.title
        assert item.source == "torznab"
        assert item.seeders == 25
        assert item.quality_str == "FLAC"
        assert item.download_url is not None
        assert "magnet:" in item.download_url


# ---------------------------------------------------------------------------
# Lidarr Adapter Tests
# ---------------------------------------------------------------------------
def test_lidarr_adapter():
    adapter = LidarrAdapter("http://lidarr:8686", "validkey")

    mock_client = MagicMock()
    mock_client.test_connection.return_value = {"online": True, "app_name": "Lidarr", "version": "2.1.0"}
    mock_client.add_artist_and_albums.return_value = {"status": "success", "message": "Added"}
    adapter.client = mock_client

    # Test connection
    ok, msg = adapter.test_connection()
    assert ok is True
    assert "2.1.0" in msg

    # Search
    results = adapter.search(artist="The Beatles", album="Abbey Road")
    assert len(results) == 1
    assert results[0].artist == "The Beatles"
    assert results[0].album == "Abbey Road"

    # Download
    dl_id = adapter.download(results[0])
    assert "lidarr::The Beatles::Abbey Road" in dl_id

    # Status
    mock_queue_resp = MagicMock()
    mock_queue_resp.status_code = 200
    mock_queue_resp.json.return_value = {
        "records": [
            {
                "artist": {"artistName": "The Beatles"},
                "album": {"title": "Abbey Road"},
                "size": 1000,
                "sizeleft": 200,
                "status": "Downloading",
                "timeleft_sec": 60,
            }
        ]
    }

    with patch("httpx.Client.get", return_value=mock_queue_resp):
        status = adapter.get_status(dl_id)
        assert status["status"] == DownloadStatus.DOWNLOADING.value
        assert status["progress"] == 80.0

    # Cancel
    mock_del_resp = MagicMock()
    mock_del_resp.status_code = 200
    with patch("httpx.Client.get", return_value=mock_queue_resp):
        with patch("httpx.Client.delete", return_value=mock_del_resp):
            assert adapter.cancel(dl_id) is True


# ---------------------------------------------------------------------------
# Factory Helper Tests
# ---------------------------------------------------------------------------
def test_get_acquisition_driver_factory():
    cfg_slskd = {
        "id": "1",
        "name": "Slskd",
        "driver_type": DownloadDriverType.SLSKD.value,
        "host_url": "http://slskd:5030",
        "username": "user",
        "password": "pwd",
    }
    driver = get_acquisition_driver(cfg_slskd)
    assert isinstance(driver, SlskdDriver)

    cfg_sab = {
        "id": "2",
        "name": "Sab",
        "driver_type": DownloadDriverType.SABNZBD.value,
        "host_url": "http://sabnzbd:8080",
        "api_key": "sabkey",
    }
    driver_sab = get_acquisition_driver(cfg_sab)
    assert isinstance(driver_sab, SabnzbdDriver)

    cfg_qbit = {
        "id": "3",
        "name": "Qbit",
        "driver_type": DownloadDriverType.QBITTORRENT.value,
        "host_url": "http://qbit:8080",
        "username": "admin",
        "password": "pwd",
    }
    driver_qbit = get_acquisition_driver(cfg_qbit)
    assert isinstance(driver_qbit, QbittorrentDriver)


def test_get_indexer_driver_factory():
    cfg_torznab = {
        "id": "1",
        "name": "Prowlarr",
        "indexer_type": "torznab",
        "host_url": "http://prowlarr:9696/1/api",
        "api_key": "prowlarrkey",
    }
    driver = get_indexer_driver(cfg_torznab)
    assert isinstance(driver, TorznabDriver)

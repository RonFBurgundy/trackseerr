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

import json
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
_SLSKD_APP = {
    "version": {"full": "0.26.1.0 (0.26.1.0+71dcc7a6)", "current": "0.26.1.0", "latest": "0.26.1.0", "isUpdateAvailable": False},
    "server": {"state": "None", "isConnected": False, "isLoggedIn": True},
}


def _slskd_resp(status: int, payload=None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status
    resp.text = text or (json.dumps(payload) if payload is not None else "")
    resp.json.return_value = payload
    return resp


def test_slskd_test_connection_success():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    with patch("httpx.Client.get", return_value=_slskd_resp(200, _SLSKD_APP)) as get:
        ok, msg = driver.test_connection()
    assert ok is True
    assert msg == "slskd 0.26.1.0"
    assert get.call_args.args[0] == "http://slskd.local:5030/api/v0/application"
    assert get.call_args.kwargs["headers"]["X-API-KEY"] == "secretkey"


def test_slskd_test_connection_failure():
    driver = SlskdDriver("http://slskd.local:5030", api_key="wrongkey")
    with patch("httpx.Client.get", return_value=_slskd_resp(401, text="Unauthorized")):
        ok, msg = driver.test_connection()
        assert ok is False
        assert "Authentication failed" in msg


def test_slskd_test_connection_not_logged_in_still_ok():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    payload = {**_SLSKD_APP, "server": {"state": "None", "isConnected": False, "isLoggedIn": False}}
    with patch("httpx.Client.get", return_value=_slskd_resp(200, payload)):
        ok, msg = driver.test_connection()
    assert ok is True
    assert "slskd 0.26.1.0" in msg
    assert "not logged in" in msg


def test_slskd_test_connection_404_reports_http():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    with patch("httpx.Client.get", return_value=_slskd_resp(404, text="Not Found")):
        ok, msg = driver.test_connection()
    assert ok is False
    assert msg.startswith("HTTP 404")


def test_slskd_connect_error_message():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    with patch("httpx.Client.get", side_effect=httpx.ConnectError("Connection refused")):
        ok, msg = driver.test_connection()
    assert ok is False
    assert msg == "Could not reach slskd at http://slskd.local:5030: ConnectError: Connection refused"


def test_slskd_failed_test_logs_warning(caplog):
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    with caplog.at_level("WARNING", logger="trackseerr.clients.acquisition.slskd"):
        with patch("httpx.Client.get", return_value=_slskd_resp(404, text="Not Found")):
            driver.test_connection()
    records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(records) == 1
    text = records[0].getMessage()
    assert "http://slskd.local:5030" in text and "404" in text and "Not Found" in text
    assert "secretkey" not in text


_SLSKD_RESPONSES = [
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
_SLSKD_DONE = {"isComplete": True, "state": "Completed, TimedOut"}
_SLSKD_BUSY = {"isComplete": False, "state": "InProgress"}
_SLSKD_MOD = "trackseerr.clients.acquisition.slskd"


@pytest.fixture(autouse=True)
def _reset_slskd_pacing():
    import trackseerr.clients.acquisition.slskd as mod
    mod._last_search_at = 0.0
    yield
    mod._last_search_at = 0.0


def _slskd_search(driver, gets, post=None, delete=None, **kw):
    post = post or _slskd_resp(200, {"id": "search-uuid-123"})
    delete = delete or _slskd_resp(204)
    with patch("httpx.Client.post", return_value=post) as p, \
            patch("httpx.Client.get", side_effect=gets) as g, \
            patch("httpx.Client.delete", return_value=delete) as d, \
            patch(f"{_SLSKD_MOD}.time.sleep") as sl:
        results = driver.search(artist="Radiohead", title="Kid A", **kw)
    return results, p, g, d, sl


def test_slskd_search_music():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    results, _, _, _, _ = _slskd_search(
        driver, [_slskd_resp(200, _SLSKD_DONE), _slskd_resp(200, _SLSKD_RESPONSES)]
    )
    assert len(results) == 1
    r = results[0]
    assert r.title == "01 - Everything In Its Right Place [FLAC 16bit]"
    assert "Everything In Its Right Place" in r.extra["filename"]
    assert r.source == "slskd"
    assert r.format == "flac"
    assert r.bit_rate == 950
    assert r.size_bytes == 25000000


def test_slskd_search_polls_until_complete():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    gets = [
        _slskd_resp(200, _SLSKD_BUSY),
        _slskd_resp(200, _SLSKD_BUSY),
        _slskd_resp(200, _SLSKD_DONE),
        _slskd_resp(200, _SLSKD_RESPONSES),
    ]
    results, _, g, _, sl = _slskd_search(driver, gets)
    assert len(results) == 1
    assert sl.call_count == 2
    urls = [c.args[0] for c in g.call_args_list]
    assert urls.count("http://slskd.local:5030/api/v0/searches/search-uuid-123/responses") == 1
    assert urls[-1].endswith("/responses")


def test_slskd_search_sends_timeout_and_filter():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    _, p, _, _, _ = _slskd_search(
        driver, [_slskd_resp(200, _SLSKD_DONE), _slskd_resp(200, _SLSKD_RESPONSES)]
    )
    body = p.call_args.kwargs["json"]
    assert body["searchTimeout"] == 12000
    assert body["filterResponses"] is True


def test_slskd_search_deletes_search_after():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    _, _, _, d, _ = _slskd_search(
        driver, [_slskd_resp(200, _SLSKD_DONE), _slskd_resp(200, _SLSKD_RESPONSES)]
    )
    d.assert_called_once()
    assert d.call_args.args[0] == "http://slskd.local:5030/api/v0/searches/search-uuid-123"


def test_slskd_search_deletes_even_when_responses_fail():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    gets = [_slskd_resp(200, _SLSKD_DONE), httpx.ConnectError("boom")]
    results, _, _, d, _ = _slskd_search(driver, gets)
    assert results == []
    d.assert_called_once()


def test_slskd_search_deadline_stops_polling():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey", min_search_interval=0)

    def gets():
        while True:
            yield _slskd_resp(200, _SLSKD_BUSY)

    # monotonic: started, deadline calc, then a loop check inside the deadline, then past it, then summary calls.
    ticks = iter([0.0, 0.0, 1.0, 100.0, 100.0, 100.0, 100.0])
    seq = [_slskd_resp(200, _SLSKD_BUSY), _slskd_resp(200, _SLSKD_RESPONSES)]
    with patch("httpx.Client.post", return_value=_slskd_resp(200, {"id": "search-uuid-123"})), \
            patch("httpx.Client.get", side_effect=seq) as g, \
            patch("httpx.Client.delete", return_value=_slskd_resp(204)), \
            patch(f"{_SLSKD_MOD}.time.sleep"), \
            patch(f"{_SLSKD_MOD}.time.monotonic", side_effect=lambda: next(ticks)):
        results = driver.search(artist="Radiohead", title="Kid A")
    assert len(results) == 1
    assert g.call_count == 2


def _run_overrun(ticks, seq):
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey", min_search_interval=0)
    it = iter(ticks)
    with patch("httpx.Client.post", return_value=_slskd_resp(200, {"id": "search-uuid-123"})), \
            patch("httpx.Client.get", side_effect=seq) as g, \
            patch("httpx.Client.delete", return_value=_slskd_resp(204)), \
            patch(f"{_SLSKD_MOD}.time.sleep"), \
            patch(f"{_SLSKD_MOD}.time.monotonic", side_effect=lambda: next(it, 1000.0)):
        results = driver.search(artist="Radiohead", title="Kid A")
    return results, g


def test_slskd_search_overruns_while_in_progress():
    # soft deadline = timeout+5 (17 by default); 20.0 is past it but inside the overrun window.
    ticks = [0.0, 0.0, 1.0, 2.0, 20.0, 21.0, 21.0, 21.0, 21.0]
    seq = [_slskd_resp(200, _SLSKD_BUSY), _slskd_resp(200, _SLSKD_BUSY),
           _slskd_resp(200, _SLSKD_DONE), _slskd_resp(200, _SLSKD_RESPONSES)]
    results, g = _run_overrun(ticks, seq)
    assert len(results) == 1
    assert g.call_count == 4


def test_slskd_search_hard_deadline_stops_overrun():
    ticks = [0.0, 0.0, 1.0, 20.0, 25.0, 500.0, 500.0, 500.0, 500.0]
    seq = [_slskd_resp(200, _SLSKD_BUSY)] * 3 + [_slskd_resp(200, _SLSKD_RESPONSES)]
    results, g = _run_overrun(ticks, seq)
    assert len(results) == 1
    assert g.call_count <= 4


def test_slskd_search_logs_summary(caplog):
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    with caplog.at_level("INFO", logger=_SLSKD_MOD):
        _slskd_search(driver, [_slskd_resp(200, _SLSKD_DONE), _slskd_resp(200, _SLSKD_RESPONSES)])
    line = next(r.getMessage() for r in caplog.records if "finished" in r.getMessage())
    assert "Radiohead Kid A" in line
    assert "responses=1" in line and "files=2" in line and "audio_kept=1" in line


@pytest.mark.parametrize(
    "f,expected",
    [
        ({"extension": "flac", "bitDepth": 24, "sampleRate": 96000}, "FLAC 24bit"),
        ({"extension": "flac", "bitDepth": 16, "sampleRate": 44100}, "FLAC 16bit"),
        ({"extension": "flac"}, "FLAC 16bit"),
        ({"extension": "mp3", "bitRate": 320}, "MP3 320"),
        ({"extension": "mp3", "bitRate": 245, "isVariableBitRate": True}, "MP3 V0"),
        ({"extension": "mp3", "bitRate": 190, "isVariableBitRate": True}, "MP3 V2"),
        ({"extension": "m4a", "bitRate": 256}, "AAC 256"),
        ({"extension": "m4a", "bitDepth": 16, "bitRate": 900}, "ALAC"),
        ({"extension": "aac", "bitRate": 256}, "AAC 256"),
        ({"extension": "ogg", "bitRate": 192}, "OGG Vorbis"),
        ({"extension": "opus"}, "Opus"),
    ],
)
def test_slskd_quality_tag_parses(f, expected):
    from trackseerr.quality import parse_release_title
    tag = SlskdDriver._quality_tag(f)
    assert parse_release_title(f"Song [{tag}]").quality == expected


def _peer(name="p", files=None, **kw):
    files = files or [{"filename": "a\\\\b\\\\01 Song.flac", "size": 1, "extension": "flac"}]
    return {"username": name, "files": files, **kw}


def _search_peers(driver, peers):
    results, *_ = _slskd_search(driver, [_slskd_resp(200, _SLSKD_DONE), _slskd_resp(200, peers)])
    return results


def test_slskd_result_title_has_quality_tag_and_extra():
    driver = SlskdDriver("http://slskd.local:5030", api_key="k")
    f = {"filename": "x\\\\03 In the Air.flac", "size": 9, "extension": "flac", "bitDepth": 24,
         "sampleRate": 96000, "length": 245}
    peers = [_peer("u", [f], hasFreeUploadSlot=True, queueLength=3, uploadSpeed=1000)]
    r = _search_peers(driver, peers)[0]
    assert r.title.endswith("[FLAC 24bit 96kHz]")
    assert r.extra["bit_depth"] == 24 and r.extra["sample_rate"] == 96000
    assert r.extra["length_seconds"] == 245
    assert r.extra["peer"] == {"has_free_slot": True, "queue_length": 3, "upload_speed": 1000}


def test_slskd_filters_long_queue_peer(caplog):
    driver = SlskdDriver("http://slskd.local:5030", api_key="k", max_peer_queue=10)
    with caplog.at_level("INFO", logger=_SLSKD_MOD):
        r = _search_peers(driver, [_peer("slow", queueLength=11), _peer("ok", queueLength=10)])
    assert [x.extra["username"] for x in r] == ["ok"]
    assert any("peers_filtered=1" in rec.getMessage() for rec in caplog.records)


def test_slskd_filters_slow_peer():
    driver = SlskdDriver("http://slskd.local:5030", api_key="k", min_upload_speed_kbps=1000)
    r = _search_peers(driver, [_peer("slow", uploadSpeed=50_000), _peer("fast", uploadSpeed=500_000)])
    assert [x.extra["username"] for x in r] == ["fast"]


def test_slskd_filters_ignored_user_case_insensitive():
    driver = SlskdDriver("http://slskd.local:5030", api_key="k", ignored_users=["BadUser"])
    r = _search_peers(driver, [_peer("baduser"), _peer("gooduser")])
    assert [x.extra["username"] for x in r] == ["gooduser"]


def test_slskd_search_paces_consecutive_searches():
    driver = SlskdDriver("http://slskd.local:5030", api_key="k", min_search_interval=5.0)
    with patch(f"{_SLSKD_MOD}.time.monotonic", return_value=100.0):
        _, _, _, _, sl1 = _slskd_search(driver, [_slskd_resp(200, _SLSKD_DONE), _slskd_resp(200, [])])
        _, _, _, _, sl2 = _slskd_search(driver, [_slskd_resp(200, _SLSKD_DONE), _slskd_resp(200, [])])
    assert not any(c.args == (5.0,) for c in sl1.call_args_list)
    assert any(c.args[0] == pytest.approx(5.0) for c in sl2.call_args_list)


def test_get_acquisition_driver_passes_slskd_peer_settings():
    extra = {"max_peer_queue": 7, "min_upload_speed_kbps": 256, "ignored_users": ["A", "b"],
             "search_timeout": 20, "min_search_interval": 2}
    cfg = {"driver_type": "slskd", "host_url": "http://slskd.local:5030", "api_key": "k",
           "extra_settings_json": json.dumps(extra)}
    d = get_acquisition_driver(cfg)
    assert (d.max_peer_queue, d.min_upload_speed_kbps, d.search_timeout, d.min_search_interval) == (7, 256, 20.0, 2.0)
    assert d.ignored_users == {"a", "b"}
    cfg["extra_settings_json"] = json.dumps({"ignored_users": "Foo, bar", "max_peer_queue": "x"})
    d = get_acquisition_driver(cfg)
    assert d.ignored_users == {"foo", "bar"} and d.max_peer_queue == 50


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


_SLSKD_DL_ID = "user1::Music%2Ftrack.flac"


def _slskd_downloads(state: str, **extra):
    f = {"id": "abc", "filename": "Music/track.flac", "state": state, "size": 1000, "bytesTransferred": 0}
    f.update(extra)
    return [{"username": "user1", "directories": [{"directory": "Music", "files": [f]}]}]


def _slskd_status(state: str, **extra):
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    with patch("httpx.Client.get", return_value=_slskd_resp(200, _slskd_downloads(state, **extra))):
        return driver.get_status(_SLSKD_DL_ID)


def test_slskd_status_completed_succeeded():
    status = _slskd_status("Completed, Succeeded", bytesTransferred=1000)
    assert status["status"] == DownloadStatus.COMPLETED.value
    assert status["progress"] == 100.0


def test_slskd_status_share_alias_root_file_has_no_alias_folder():
    from urllib.parse import quote
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey", download_dir="/downloads")
    filename = "@@lusox\\Rafael - Song.flac"
    f = {
        "id": "abc",
        "filename": filename,
        "state": "Completed, Succeeded",
        "size": 1000,
        "bytesTransferred": 1000,
    }
    payload = [{"username": "user1", "directories": [{"directory": "@@lusox", "files": [f]}]}]
    dl_id = f"user1::{quote(filename)}"
    with patch("httpx.Client.get", return_value=_slskd_resp(200, payload)):
        status = driver.get_status(dl_id)
    assert status["status"] == DownloadStatus.COMPLETED.value
    assert status["source_path"] == "/downloads/Rafael - Song.flac"


def test_slskd_status_nested_under_alias_keeps_album_folder():
    from urllib.parse import quote
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey", download_dir="/downloads")
    filename = "@@lusox\\Music\\Album\\01 - X.flac"
    f = {
        "id": "abc",
        "filename": filename,
        "state": "Completed, Succeeded",
        "size": 1000,
        "bytesTransferred": 1000,
    }
    payload = [{"username": "user1", "directories": [{"directory": "@@lusox\\Music\\Album", "files": [f]}]}]
    dl_id = f"user1::{quote(filename)}"
    with patch("httpx.Client.get", return_value=_slskd_resp(200, payload)):
        status = driver.get_status(dl_id)
    assert status["status"] == DownloadStatus.COMPLETED.value
    assert status["source_path"] == "/downloads/Album/01 - X.flac"


def test_slskd_status_reported_local_filename_wins():
    from urllib.parse import quote
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey", download_dir="/downloads")
    filename = "@@lusox\\Rafael - Song.flac"
    f = {
        "id": "abc",
        "filename": filename,
        "localFilename": "/custom/path/Song.flac",
        "state": "Completed, Succeeded",
        "size": 1000,
        "bytesTransferred": 1000,
    }
    payload = [{"username": "user1", "directories": [{"directory": "@@lusox", "files": [f]}]}]
    dl_id = f"user1::{quote(filename)}"
    with patch("httpx.Client.get", return_value=_slskd_resp(200, payload)):
        status = driver.get_status(dl_id)
    assert status["status"] == DownloadStatus.COMPLETED.value
    assert status["source_path"] == "/custom/path/Song.flac"


@pytest.mark.parametrize("suffix", ["Errored", "Rejected", "TimedOut"])
def test_slskd_status_completed_errored_is_failed(suffix):
    status = _slskd_status(f"Completed, {suffix}")
    assert status["status"] == DownloadStatus.FAILED.value
    assert suffix in status["error_message"]


def test_slskd_status_queued_remotely():
    status = _slskd_status("Queued, Remotely")
    assert status["status"] == DownloadStatus.QUEUED.value
    assert status["remote_state"] == "Queued, Remotely"
    assert status["transfer_id"] == "abc"


def test_slskd_status_missing_flag():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    with patch("httpx.Client.get", return_value=_slskd_resp(200, [])):
        status = driver.get_status(_SLSKD_DL_ID)
    assert status["status"] == DownloadStatus.QUEUED.value
    assert status["missing"] is True


def test_slskd_cancel_deletes_by_transfer_id():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    user = _slskd_downloads("InProgress")[0]
    with patch("httpx.Client.get", return_value=_slskd_resp(200, user)), \
            patch("httpx.Client.delete", return_value=_slskd_resp(204)) as d:
        assert driver.cancel(_SLSKD_DL_ID) is True
    assert d.call_args.args[0] == "http://slskd.local:5030/api/v0/transfers/downloads/user1/abc"
    assert d.call_args.kwargs["params"] == {"remove": "true"}


def test_slskd_cancel_not_found_returns_false():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    with patch("httpx.Client.get", return_value=_slskd_resp(200, {"username": "user1", "directories": []})), \
            patch("httpx.Client.delete") as d:
        assert driver.cancel(_SLSKD_DL_ID) is False
    d.assert_not_called()


# --- slskd folder candidates -------------------------------------------------
_FOLDER_DIR = "Music\\Artist\\Album (2011)"


def _folder_files(n=3, directory=_FOLDER_DIR):
    return [
        {"filename": f"{directory}\\{i:02d} Track.flac", "size": 1000 * i, "bitRate": 900,
         "extension": "flac", "bitDepth": 16, "sampleRate": 44100, "length": 100 + i}
        for i in range(1, n + 1)
    ]


def _folder_id(directory=_FOLDER_DIR):
    from urllib.parse import quote
    return f"user1::dir::{quote(directory)}"


def test_slskd_search_emits_folder_candidate():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    files = _folder_files(3) + _folder_files(1, "Music\\Artist\\Other")
    payload = [{"username": "user1", "files": files}]
    results, _, _, _, _ = _slskd_search(driver, [_slskd_resp(200, _SLSKD_DONE), _slskd_resp(200, payload)])
    assert len([r for r in results if r.item_type == "track"]) == 4
    folders = [r for r in results if r.item_type == "album"]
    assert len(folders) == 1
    r = folders[0]
    assert r.download_id == r.download_url == _folder_id()
    assert r.title.startswith("Album (2011) [FLAC")
    assert r.size_bytes == 6000
    assert r.extra["track_count"] == 3
    assert r.extra["filename"] == r.extra["directory"] == _FOLDER_DIR
    assert r.extra["total_length_seconds"] == 306
    assert [f["filename"] for f in r.extra["files"]] == sorted(f["filename"] for f in r.extra["files"])
    assert set(r.extra["peer"]) == {"has_free_slot", "queue_length", "upload_speed"}


def test_slskd_folder_candidate_needs_two_files():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    payload = [{"username": "user1", "files": _folder_files(1)}]
    results, _, _, _, _ = _slskd_search(driver, [_slskd_resp(200, _SLSKD_DONE), _slskd_resp(200, payload)])
    assert [r.item_type for r in results] == ["track"]


def test_slskd_download_folder_queues_all_files():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    entries = [{"filename": f["filename"], "size": f["size"], "length_seconds": f["length"]} for f in _folder_files(3)]
    item = AcquisitionSearchResult(
        download_id=_folder_id(), title="Album", artist="A", item_type="album", source="slskd",
        extra={"username": "user1", "directory": _FOLDER_DIR, "files": entries},
    )
    with patch("httpx.Client.post", return_value=_slskd_resp(201)) as post:
        assert driver.download(item) == _folder_id()
    assert post.call_count == 1
    assert post.call_args.args[0] == "http://slskd.local:5030/api/v0/transfers/downloads/user1"
    assert post.call_args.kwargs["json"] == [{"filename": e["filename"], "size": e["size"]} for e in entries]


def _folder_downloads(*files):
    return [{"username": "user1", "directories": [
        {"directory": "Other", "files": []},
        {"directory": _FOLDER_DIR, "files": list(files)},
    ]}]


def _tf(i, state, size=1000, done=0, **kw):
    f = {"id": f"t{i}", "filename": f"{_FOLDER_DIR}\\{i:02d}.flac", "state": state, "size": size, "bytesTransferred": done}
    f.update(kw)
    return f


def _folder_status(*files, root="/downloads"):
    driver = SlskdDriver("http://slskd.local:5030", api_key="k", download_dir=root)
    with patch("httpx.Client.get", return_value=_slskd_resp(200, _folder_downloads(*files))):
        return driver.get_status(_folder_id())


def test_slskd_folder_status_all_succeeded_gives_folder_path():
    st = _folder_status(*[_tf(i, "Completed, Succeeded", done=1000) for i in (1, 2)])
    assert st["status"] == DownloadStatus.COMPLETED.value
    assert st["progress"] == 100.0
    assert st["source_path"] == "/downloads/Album (2011)"


def test_slskd_folder_status_share_alias_dir_has_no_folder_path():
    alias_dir = "@@lusox"
    files = [
        {"id": "t1", "filename": f"{alias_dir}\\01.flac", "state": "Completed, Succeeded", "size": 1000, "bytesTransferred": 1000},
        {"id": "t2", "filename": f"{alias_dir}\\02.flac", "state": "Completed, Succeeded", "size": 1000, "bytesTransferred": 1000},
    ]
    driver = SlskdDriver("http://slskd.local:5030", api_key="k", download_dir="/downloads")
    payload = [{"username": "user1", "directories": [{"directory": alias_dir, "files": files}]}]
    with patch("httpx.Client.get", return_value=_slskd_resp(200, payload)):
        st = driver.get_status(_folder_id(alias_dir))
    assert st["status"] == DownloadStatus.COMPLETED.value
    assert st["progress"] == 100.0
    assert st["source_path"] is None


def test_slskd_folder_status_partial_progress():
    st = _folder_status(
        _tf(1, "Completed, Succeeded", done=1000), _tf(2, "InProgress", done=500), _tf(3, "Queued, Remotely"),
    )
    assert st["status"] == DownloadStatus.DOWNLOADING.value
    assert st["progress"] == 50.0
    assert st["size_bytes"] == 3000


def test_slskd_folder_status_failed_when_one_errored_and_rest_done():
    st = _folder_status(_tf(1, "Completed, Succeeded", done=1000), _tf(2, "Completed, Errored"))
    assert st["status"] == DownloadStatus.FAILED.value
    assert st["error_message"].startswith("1 of 2 files failed:")


def test_slskd_folder_status_all_queued_remotely():
    st = _folder_status(_tf(1, "Queued, Remotely"), _tf(2, "Queued, Locally"))
    assert st["status"] == DownloadStatus.QUEUED.value
    assert st["remote_state"] == "Queued, Remotely"


def test_slskd_folder_status_missing():
    driver = SlskdDriver("http://slskd.local:5030", api_key="k")
    with patch("httpx.Client.get", return_value=_slskd_resp(200, [])):
        st = driver.get_status(_folder_id())
    assert st["status"] == DownloadStatus.QUEUED.value
    assert st["missing"] is True


def test_slskd_cancel_folder_deletes_each_transfer():
    driver = SlskdDriver("http://slskd.local:5030", api_key="k")
    user = _folder_downloads(_tf(1, "InProgress"), _tf(2, "Queued, Remotely"))[0]
    with patch("httpx.Client.get", return_value=_slskd_resp(200, user)), \
            patch("httpx.Client.delete", return_value=_slskd_resp(204)) as d:
        assert driver.cancel(_folder_id()) is True
    urls = [c.args[0] for c in d.call_args_list]
    assert urls == [
        "http://slskd.local:5030/api/v0/transfers/downloads/user1/t1",
        "http://slskd.local:5030/api/v0/transfers/downloads/user1/t2",
    ]
    assert all(c.kwargs["params"] == {"remove": "true"} for c in d.call_args_list)


def test_slskd_folder_status_done_plus_queued_is_downloading():
    st = _folder_status(_tf(1, "Completed, Succeeded", done=1000), _tf(2, "Queued, Remotely"))
    assert st["status"] == DownloadStatus.DOWNLOADING.value


def test_slskd_cleanup_completed_track_by_id():
    driver = SlskdDriver("http://slskd.local:5030", api_key="k")
    user = _slskd_downloads("Completed, Succeeded")[0]
    with patch("httpx.Client.get", return_value=_slskd_resp(200, user)), \
            patch("httpx.Client.delete", return_value=_slskd_resp(204)) as d:
        assert driver.cleanup_completed(_SLSKD_DL_ID, delete_files=True) is True
    assert d.call_args.args[0] == "http://slskd.local:5030/api/v0/transfers/downloads/user1/abc"
    assert d.call_args.kwargs["params"] == {"remove": "true"}
    assert "json" not in d.call_args.kwargs


def test_slskd_cleanup_completed_folder():
    driver = SlskdDriver("http://slskd.local:5030", api_key="k")
    user = _folder_downloads(_tf(1, "Completed, Succeeded"), _tf(2, "Completed, Succeeded"))[0]
    with patch("httpx.Client.get", return_value=_slskd_resp(200, user)), \
            patch("httpx.Client.delete", side_effect=[_slskd_resp(204), _slskd_resp(404)]) as d:
        assert driver.cleanup_completed(_folder_id()) is True
    assert d.call_count == 2
    with patch("httpx.Client.get", return_value=_slskd_resp(200, {"username": "user1", "directories": []})), \
            patch("httpx.Client.delete") as d2:
        assert driver.cleanup_completed(_folder_id()) is True
    d2.assert_not_called()
    with patch("httpx.Client.get", side_effect=httpx.ConnectError("boom")):
        assert driver.cleanup_completed(_folder_id()) is False


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


def test_qbittorrent5_login_204_qbt_sid_cookie():
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "adminadmin")
    login_resp = MagicMock()
    login_resp.status_code = 204
    login_resp.text = ""
    login_resp.cookies = {"QBT_SID_8080": "abc123"}
    ver_resp = MagicMock()
    ver_resp.status_code = 200
    ver_resp.text = "v5.0.1"
    with patch("httpx.Client.post", return_value=login_resp):
        with patch("httpx.Client.get", return_value=ver_resp):
            ok, msg = driver.test_connection()
    assert ok is True
    assert msg == "qBittorrent v5.0.1"
    assert driver._cookie == "QBT_SID_8080=abc123"


def test_qbittorrent_login_fails_body():
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "wrong")
    resp = MagicMock()
    resp.status_code = 200
    resp.text = "Fails."
    resp.cookies = {}
    with patch("httpx.Client.post", return_value=resp):
        ok, msg = driver.test_connection()
    assert ok is False
    assert "invalid credentials" in msg


def test_qbittorrent_401_message_mentions_port():
    driver = QbittorrentDriver("http://qbittorrent:8081", "admin", "x")
    resp = MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"
    resp.cookies = {}
    with patch("httpx.Client.post", return_value=resp):
        ok, msg = driver.test_connection()
    assert ok is False
    assert "WebUI port" in msg


def test_qbittorrent_connect_error_not_reported_as_auth():
    import httpx

    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "x")
    with patch("httpx.Client.post", side_effect=httpx.ConnectError("refused")):
        ok, msg = driver.test_connection()
    assert ok is False
    assert "Could not reach" in msg
    assert "ConnectError" in msg
    assert "credentials" not in msg


def test_qbittorrent_failed_test_logs_warning(caplog):
    import logging

    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "s3cretpw")
    resp = MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"
    resp.cookies = {}
    with caplog.at_level(logging.WARNING):
        with patch("httpx.Client.post", return_value=resp):
            driver.test_connection()
    records = [r for r in caplog.records if "connection test failed" in r.getMessage()]
    assert len(records) == 1
    text = records[0].getMessage()
    assert "http://qbittorrent:8080" in text
    assert "401" in text
    assert "s3cretpw" not in text


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


# ---------------------------------------------------------------------------
# Auth fixes: qBittorrent CSRF/ban, SABnzbd API key probe
# ---------------------------------------------------------------------------
def test_qbittorrent_login_sends_referer_and_handles_ban():
    driver = QbittorrentDriver("http://qbittorrent:8080", "admin", "wrong")
    resp = MagicMock()
    resp.status_code = 403
    resp.text = "Your IP address has been banned"
    resp.cookies = {}
    with patch("httpx.Client.post", return_value=resp) as post:
        ok, msg = driver.test_connection()
    assert ok is False
    assert "banned" in msg
    assert post.call_args.kwargs["headers"]["Referer"] == "http://qbittorrent:8080"


def test_sabnzbd_bad_api_key_fails_test():
    driver = SabnzbdDriver("http://sabnzbd:8080", api_key="wrong")
    version = MagicMock()
    version.status_code = 200
    version.json.return_value = {"version": "4.2.1"}
    queue = MagicMock()
    queue.status_code = 200
    queue.json.return_value = {"status": False, "error": "API Key Incorrect"}
    with patch("httpx.Client.get", side_effect=[version, queue]):
        ok, msg = driver.test_connection()
    assert ok is False
    assert "API key" in msg


def test_slskd_get_incomplete_root_reads_options():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    payload = {"directories": {"downloads": " /data/downloads ", "incomplete": " /data/incomplete "}}
    with patch("httpx.Client.get", return_value=_slskd_resp(200, payload)) as get:
        assert driver.get_incomplete_root() == "/data/incomplete"
        assert driver.get_download_roots() == ["/data/downloads"]
        assert driver.get_incomplete_root() == "/data/incomplete"
    assert get.call_count == 1


def test_slskd_get_incomplete_root_none_when_absent():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    with patch("httpx.Client.get", return_value=_slskd_resp(200, {"directories": {"downloads": "/d"}})):
        assert driver.get_incomplete_root() is None
        assert driver.get_download_roots() == ["/d"]
    unreadable = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    with patch("httpx.Client.get", return_value=_slskd_resp(500, text="boom")):
        assert unreadable.get_incomplete_root() is None


# ---------------------------------------------------------------------------
# slskd chat (private message) methods
# ---------------------------------------------------------------------------
_CHAT_URL = "http://slskd.local:5030/api/v0/conversations/Bob%20X"


def test_slskd_get_conversation_messages_normalizes():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    payload = {
        "username": "Bob X",
        "messages": [
            {"id": 7, "username": "Bob X", "message": "hi", "direction": "In", "timestamp": "2026-01-01T00:00:00Z", "isAcknowledged": True},
            {"id": 8, "username": "me", "message": "yo", "direction": "OUT", "timestamp": "2026-01-01T00:01:00Z"},
            {"id": 9, "username": "Bob X", "message": "bad direction", "direction": "sideways"},
            {"username": "Bob X", "message": "no id", "direction": "In"},
            "garbage",
        ],
    }
    with patch("httpx.Client.get", return_value=_slskd_resp(200, payload)) as get:
        msgs = driver.get_conversation_messages("Bob X")
    assert get.call_args.args[0] == _CHAT_URL
    assert get.call_args.kwargs["params"] == {"includeMessages": "true"}
    assert msgs == [
        {"id": "7", "username": "Bob X", "message": "hi", "direction": "in", "timestamp": "2026-01-01T00:00:00Z", "acknowledged": True},
        {"id": "8", "username": "me", "message": "yo", "direction": "out", "timestamp": "2026-01-01T00:01:00Z", "acknowledged": False},
    ]


def test_slskd_get_conversation_messages_bad_payload_empty():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    for payload in ({"messages": "nope"}, ["x"], None):
        with patch("httpx.Client.get", return_value=_slskd_resp(200, payload)):
            assert driver.get_conversation_messages("Bob X") == []
    with patch("httpx.Client.get", return_value=_slskd_resp(404, text="nf")):
        assert driver.get_conversation_messages("Bob X") == []
    with patch("httpx.Client.get", return_value=_slskd_resp(200, ["a", {"u": 1}])):
        assert driver.list_conversations() == [{"u": 1}]
    with patch("httpx.Client.get", return_value=_slskd_resp(200, {"x": 1})):
        assert driver.list_conversations() == []


def test_slskd_send_private_message_posts_json_string():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    with patch("httpx.Client.post", return_value=_slskd_resp(201)) as post:
        assert driver.send_private_message("Bob X", "hello") is True
    assert post.call_args.args[0] == _CHAT_URL
    assert post.call_args.kwargs["json"] == "hello"
    assert post.call_args.kwargs["headers"]["X-API-KEY"] == "secretkey"
    with patch("httpx.Client.post", return_value=_slskd_resp(500)):
        assert driver.send_private_message("Bob X", "hello") is False


def test_slskd_send_private_message_refuses_empty_or_long():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    with patch("httpx.Client.post") as post:
        assert driver.send_private_message("Bob X", "   ") is False
        assert driver.send_private_message("Bob X", "x" * 201) is False
    post.assert_not_called()
    with patch("httpx.Client.post", return_value=_slskd_resp(200)):
        assert driver.send_private_message("Bob X", "x" * 200) is True


def test_slskd_acknowledge_conversation():
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    with patch("httpx.Client.put", return_value=_slskd_resp(204)) as put:
        assert driver.acknowledge_conversation("Bob X") is True
    assert put.call_args.args[0] == _CHAT_URL
    with patch("httpx.Client.put", return_value=_slskd_resp(404)):
        assert driver.acknowledge_conversation("Bob X") is False


def test_slskd_chat_http_error_returns_empty(caplog):
    driver = SlskdDriver("http://slskd.local:5030", api_key="secretkey")
    err = httpx.ConnectError("refused")
    with caplog.at_level("WARNING", logger="trackseerr.clients.acquisition.slskd"):
        with patch("httpx.Client.get", side_effect=err):
            assert driver.list_conversations() == []
            assert driver.get_conversation_messages("Bob X") == []
        with patch("httpx.Client.put", side_effect=err):
            assert driver.acknowledge_conversation("Bob X") is False
        with patch("httpx.Client.post", side_effect=err):
            assert driver.send_private_message("Bob X", "hi") is False
    assert len([r for r in caplog.records if "ConnectError" in r.getMessage()]) == 4
    assert all("secretkey" not in r.getMessage() for r in caplog.records)

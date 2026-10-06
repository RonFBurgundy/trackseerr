"""Per-driver get_download_roots (qBittorrent, SABnzbd, slskd) and slskd completed source_path derivation."""
from unittest.mock import MagicMock, patch

import httpx

from plex_playlist_sync.clients.acquisition.lidarr_adapter import LidarrAdapter
from plex_playlist_sync.clients.acquisition.qbittorrent import QbittorrentDriver
from plex_playlist_sync.clients.acquisition.sabnzbd import SabnzbdDriver
from plex_playlist_sync.clients.acquisition.slskd import SlskdDriver
from plex_playlist_sync.clients.acquisition.torznab import TorznabDriver


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = str(payload)
    return r


def _router(routes):
    """side_effect for httpx.Client.get: first route whose key is a substring of the URL wins."""
    def fn(url, *a, **kw):
        for key, resp in routes.items():
            if key in url:
                return resp
        raise AssertionError(f"unexpected GET {url}")
    return fn


# ----------------------------------------------------------------- qBittorrent
def test_qbit_roots_save_path_and_category_never_temp():
    d = QbittorrentDriver("http://qbit.local:8080", category="music")
    routes = {
        "/app/preferences": _resp({"save_path": "/data/torrents", "temp_path": "/data/incomplete", "temp_path_enabled": True}),
        "/torrents/categories": _resp({"music": {"name": "music", "savePath": "/data/torrents/music"}}),
    }
    with patch("httpx.Client.get", side_effect=_router(routes)):
        roots = d.get_download_roots()
    assert roots == ["/data/torrents", "/data/torrents/music"]
    assert "/data/incomplete" not in roots
    assert d.last_roots_error is None


def test_qbit_roots_category_without_save_path():
    d = QbittorrentDriver("http://qbit.local:8080", category="music")
    routes = {
        "/app/preferences": _resp({"save_path": "/data/torrents"}),
        "/torrents/categories": _resp({"music": {"name": "music", "savePath": ""}}),
    }
    with patch("httpx.Client.get", side_effect=_router(routes)):
        assert d.get_download_roots() == ["/data/torrents"]


def test_qbit_roots_failure_returns_empty_and_records_error():
    d = QbittorrentDriver("http://qbit.local:8080")
    with patch("httpx.Client.get", side_effect=httpx.ConnectError("refused")):
        assert d.get_download_roots() == []
    assert "refused" in (d.last_roots_error or "")


def test_qbit_roots_http_error_status():
    d = QbittorrentDriver("http://qbit.local:8080")
    with patch("httpx.Client.get", return_value=_resp({}, status=500)):
        assert d.get_download_roots() == []
    assert "500" in (d.last_roots_error or "")


# -------------------------------------------------------------------- SABnzbd
def test_sab_roots_complete_dir_and_category_dir():
    d = SabnzbdDriver("http://sab.local:8080", api_key="k", category="music")
    routes = {
        "section=misc": _resp({"config": {"misc": {"complete_dir": "/data/usenet", "download_dir": "/data/usenet/incomplete"}}}),
        "section=categories": _resp({"config": {"categories": [
            {"name": "movies", "dir": "/data/usenet/movies"},
            {"name": "music", "dir": "/data/usenet/music"},
        ]}}),
    }
    with patch("httpx.Client.get", side_effect=_router(routes)):
        roots = d.get_download_roots()
    assert roots == ["/data/usenet", "/data/usenet/music"]
    assert "/data/usenet/incomplete" not in roots


def test_sab_roots_relative_complete_dir_resolved_against_download_dir_base():
    d = SabnzbdDriver("http://sab.local:8080", api_key="k", category="music")
    routes = {
        "section=misc": _resp({"config": {"misc": {"complete_dir": "Downloads/complete", "download_dir": "/data/Downloads/incomplete"}}}),
        "section=categories": _resp({"config": {"categories": [{"name": "music", "dir": "music"}]}}),
    }
    with patch("httpx.Client.get", side_effect=_router(routes)):
        roots = d.get_download_roots()
    assert roots == ["/data/Downloads/complete", "/data/Downloads/complete/music"]


def test_sab_roots_failure():
    d = SabnzbdDriver("http://sab.local:8080", api_key="k")
    with patch("httpx.Client.get", side_effect=httpx.ReadTimeout("slow")):
        assert d.get_download_roots() == []
    assert d.last_roots_error


# ---------------------------------------------------------------------- slskd
def test_slskd_roots_downloads_only_never_incomplete():
    d = SlskdDriver("http://slskd.local:5030", api_key="k")
    opts = {"directories": {"incomplete": "/data/slskd/incomplete", "downloads": "/data/slskd/complete/music"}}
    with patch("httpx.Client.get", return_value=_resp(opts)):
        assert d.get_download_roots() == ["/data/slskd/complete/music"]


def test_slskd_roots_cached_on_driver():
    d = SlskdDriver("http://slskd.local:5030", api_key="k")
    opts = {"directories": {"downloads": "/dl"}}
    with patch("httpx.Client.get", return_value=_resp(opts)) as get:
        d.get_download_roots()
        d.get_download_roots()
    assert get.call_count == 1


def test_slskd_roots_failure_and_missing_field():
    d = SlskdDriver("http://slskd.local:5030", api_key="k")
    with patch("httpx.Client.get", side_effect=httpx.ConnectError("down")):
        assert d.get_download_roots() == []
    assert "down" in (d.last_roots_error or "")
    d2 = SlskdDriver("http://slskd.local:5030", api_key="k")
    with patch("httpx.Client.get", return_value=_resp({"directories": {}})):
        assert d2.get_download_roots() == []
    assert d2.last_roots_error


def test_slskd_download_dir_is_only_an_override():
    d = SlskdDriver("http://slskd.local:5030", api_key="k", download_dir="/override")
    with patch("httpx.Client.get", return_value=_resp({"directories": {"downloads": "/dl"}})):
        assert d.get_download_roots() == ["/dl", "/override"]
    assert SlskdDriver("http://slskd.local:5030").download_dir is None


def _transfers(directory, filename, state="Completed, Succeeded"):
    return [{"username": "bob", "directories": [{"directory": directory, "files": [
        {"filename": filename, "state": state, "size": 10, "bytesTransferred": 10}]}]}]


def _status(d, payload, download_id):
    def get(url, *a, **kw):
        if "/options" in url:
            return _resp({"directories": {"downloads": "/data/slskd/complete/music"}})
        return _resp(payload)
    with patch("httpx.Client.get", side_effect=get):
        return d.get_status(download_id)


def test_slskd_source_path_windows_backslashes():
    d = SlskdDriver("http://slskd.local:5030", api_key="k")
    fn = "@@abc\\Music\\Daft Punk\\Discovery\\01 One More Time.flac"
    st = _status(d, _transfers("@@abc\\Music\\Daft Punk\\Discovery", fn), f"bob::{fn}")
    assert st["status"] == "completed"
    assert st["source_path"] == "/data/slskd/complete/music/Discovery/01 One More Time.flac"


def test_slskd_source_path_posix_and_derived_without_directory_field():
    d = SlskdDriver("http://slskd.local:5030", api_key="k")
    fn = "/share/Album X/02 Track.mp3"
    payload = [{"username": "bob", "directories": [{"files": [{"filename": fn, "state": "Completed, Succeeded", "size": 1}]}]}]
    st = _status(d, payload, f"bob::{fn}")
    assert st["source_path"] == "/data/slskd/complete/music/Album X/02 Track.mp3"


def test_slskd_source_path_none_while_downloading():
    d = SlskdDriver("http://slskd.local:5030", api_key="k")
    fn = "a\\b\\c.flac"
    st = _status(d, _transfers("a\\b", fn, state="InProgress"), f"bob::{fn}")
    assert st["status"] == "downloading"
    assert st["source_path"] is None


def test_slskd_source_path_override_dir():
    d = SlskdDriver("http://slskd.local:5030", api_key="k", download_dir="/mnt/override")
    fn = "x\\Alb\\t.flac"
    st = _status(d, _transfers("x\\Alb", fn), f"bob::{fn}")
    assert st["source_path"] == "/mnt/override/Alb/t.flac"


def test_slskd_source_path_none_when_downloads_root_unreadable():
    d = SlskdDriver("http://slskd.local:5030", api_key="k")
    fn = "x\\Alb\\t.flac"

    def get(url, *a, **kw):
        if "/options" in url:
            raise httpx.ConnectError("no options")
        return _resp(_transfers("x\\Alb", fn))
    with patch("httpx.Client.get", side_effect=get):
        st = d.get_status(f"bob::{fn}")
    assert st["status"] == "completed"
    assert st["source_path"] is None


# --------------------------------------------------------------- defaults
def test_default_roots_empty_for_lidarr_and_torznab():
    assert LidarrAdapter("http://lidarr.local:8686", api_key="k").get_download_roots() == []
    assert TorznabDriver("http://idx.local", api_key="k").get_download_roots() == []

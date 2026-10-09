import os
from unittest.mock import patch

from trackseerr.config import Config, _parse_bool, _split_ids


def test_parse_bool():
    assert _parse_bool("1") is True
    assert _parse_bool("true") is True
    assert _parse_bool("TRUE") is True
    assert _parse_bool("yes") is True
    assert _parse_bool("y") is True
    assert _parse_bool("on") is True
    assert _parse_bool("0") is False
    assert _parse_bool("false") is False
    assert _parse_bool("no") is False
    assert _parse_bool(None, default=True) is True
    assert _parse_bool(None, default=False) is False


def test_split_ids():
    assert _split_ids("") == []
    assert _split_ids(None) == []
    # Comma and space separated
    assert _split_ids("id1, id2 id3;id4") == ["id1", "id2", "id3", "id4"]
    # Spotify URI
    assert _split_ids("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M") == ["37i9dQZF1DXcBWIGoYBM5M"]
    # Spotify URL
    assert _split_ids("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=123") == ["37i9dQZF1DXcBWIGoYBM5M"]
    # Deezer URL
    assert _split_ids("https://www.deezer.com/us/playlist/1313621735") == ["1313621735"]


def test_config_from_env():
    env = {
        "PLEX_URL": "http://192.168.1.100:32400",
        "PLEX_TOKEN": "secret_token",
        "PLEX_VERIFY_SSL": "0",
        "RUN_ONCE": "1",
        "SPOTIFY_CLIENT_ID": "sp_client",
        "SPOTIFY_CLIENT_SECRET": "sp_secret",
        "SPOTIFY_USER_ID": "user_123",
        "SPOTIFY_PLAYLIST_ID": "pl_1, pl_2",
        "DEEZER_USER_ID": "9999",
        "DEEZER_PLAYLIST_ID": "111 222",
    }
    with patch.dict(os.environ, env, clear=True):
        cfg = Config.from_env()
        assert cfg.plex_url == "http://192.168.1.100:32400"
        assert cfg.plex_token == "secret_token"
        assert cfg.plex_verify_ssl is False
        assert cfg.run_once is True
        assert cfg.has_spotify is True
        assert cfg.has_deezer is True
        assert cfg.spotify_playlist_ids == ["pl_1", "pl_2"]
        assert cfg.deezer_playlist_ids == ["111", "222"]


def test_config_ssl_fallback_flags():
    # Test IGNORE_SSL fallback
    with patch.dict(os.environ, {"PLEX_URL": "http://localhost", "PLEX_TOKEN": "tok", "IGNORE_SSL": "1"}, clear=True):
        cfg = Config.from_env()
        assert cfg.plex_verify_ssl is False

    # Test CRON flag
    with patch.dict(os.environ, {"PLEX_URL": "http://localhost", "PLEX_TOKEN": "tok", "CRON": "1"}, clear=True):
        cfg = Config.from_env()
        assert cfg.run_once is True

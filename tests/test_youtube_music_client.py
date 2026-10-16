"""Tests for YouTube Music client."""

from unittest.mock import MagicMock, patch

import pytest
import requests
from ytmusicapi.exceptions import YTMusicError, YTMusicServerError, YTMusicUserError

from trackseerr.clients.youtube_music import (
    MAX_PLAYLIST_TRACKS,
    YouTubeMusicClient,
    get_shared_youtube_music_client,
)


@pytest.fixture
def mock_ytmusic():
    with patch("trackseerr.clients.youtube_music.ytmusicapi.YTMusic") as mock_cls:
        instance = MagicMock()
        mock_cls.return_value = instance
        yield instance


def test_playlist_tracks_maps_songs(mock_ytmusic):
    mock_ytmusic.get_playlist.return_value = {
        "tracks": [
            {
                "videoId": "abc1234",
                "title": "Starman",
                "artists": [{"name": "David Bowie", "id": "1"}],
                "album": {"name": "The Rise and Fall of Ziggy Stardust", "id": "2"},
                "duration_seconds": 250,
                "isAvailable": True,
            }
        ]
    }
    client = YouTubeMusicClient()
    tracks = client.get_playlist_tracks("PL1234567890123")

    mock_ytmusic.get_playlist.assert_called_once_with("PL1234567890123", limit=10_000)
    assert len(tracks) == 1
    t = tracks[0]
    assert t.title == "Starman"
    assert t.artist == "David Bowie"
    assert t.album == "The Rise and Fall of Ziggy Stardust"
    assert t.url == "https://music.youtube.com/watch?v=abc1234"
    assert t.duration_seconds == 250.0


def test_topic_suffix_stripped(mock_ytmusic):
    mock_ytmusic.get_playlist.return_value = {
        "tracks": [
            {
                "videoId": "abc1234",
                "title": "Heroes",
                "artists": [{"name": "David Bowie - Topic", "id": "1"}],
                "album": {"name": "Heroes", "id": "2"},
                "duration_seconds": 360,
                "isAvailable": True,
            }
        ]
    }
    client = YouTubeMusicClient()
    tracks = client.get_playlist_tracks("PL1234567890123")
    assert len(tracks) == 1
    assert tracks[0].artist == "David Bowie"


def test_unavailable_items_skipped(mock_ytmusic):
    mock_ytmusic.get_playlist.return_value = {
        "tracks": [
            {
                "videoId": "vid_unavail",
                "title": "Unavailable Track",
                "artists": [{"name": "Artist"}],
                "isAvailable": False,
            },
            {
                "videoId": None,
                "title": "Header / separator item",
                "artists": [{"name": "Artist"}],
                "isAvailable": True,
            },
            {
                "videoId": "vid_avail",
                "title": "Available Track",
                "artists": [{"name": "Artist"}],
                "album": {"name": "Album"},
                "isAvailable": True,
            },
        ]
    }
    client = YouTubeMusicClient()
    tracks = client.get_playlist_tracks("PL1234567890123")
    assert len(tracks) == 1
    assert tracks[0].title == "Available Track"
    assert tracks[0].url == "https://music.youtube.com/watch?v=vid_avail"


def test_video_title_cleanup_strips_official_video(mock_ytmusic):
    mock_ytmusic.get_playlist.return_value = {
        "tracks": [
            {
                "videoId": "vid1",
                "title": "Bohemian Rhapsody (Official Music Video) [HD]",
                "artists": [{"name": "Queen"}],
                "album": None,
                "duration_seconds": 354,
                "isAvailable": True,
            },
            {
                "videoId": "vid2",
                "title": "Easy On Me (Lyric Video)",
                "artists": [{"name": "Adele"}],
                "album": None,
                "duration_seconds": 224,
                "isAvailable": True,
            },
            {
                "videoId": "vid3",
                "title": "Song Title (Visualizer) [4K]",
                "artists": [{"name": "Artist"}],
                "album": None,
                "isAvailable": True,
            },
        ]
    }
    client = YouTubeMusicClient()
    tracks = client.get_playlist_tracks("PL1234567890123")
    assert len(tracks) == 3
    assert tracks[0].title == "Bohemian Rhapsody"
    assert tracks[1].title == "Easy On Me"
    assert tracks[2].title == "Song Title"


def test_video_title_split_artist_dash_title(mock_ytmusic):
    mock_ytmusic.get_playlist.return_value = {
        "tracks": [
            {
                "videoId": "vid1",
                "title": "Daft Punk - One More Time (Official Video)",
                "artists": [{"name": "Daft Punk"}],
                "album": None,
                "isAvailable": True,
            },
            {
                "videoId": "vid2",
                "title": "Rick Astley - Never Gonna Give You Up (Official Music Video)",
                "artists": [{"name": "Warner Records"}],
                "album": None,
                "isAvailable": True,
            },
            {
                "videoId": "vid3",
                "title": "Adele - Hello",
                "artists": [{"name": "AdeleVEVO"}],
                "album": None,
                "isAvailable": True,
            },
        ]
    }
    client = YouTubeMusicClient()
    tracks = client.get_playlist_tracks("PL1234567890123")
    assert len(tracks) == 3
    assert tracks[0].artist == "Daft Punk"
    assert tracks[0].title == "One More Time"

    assert tracks[1].artist == "Rick Astley"
    assert tracks[1].title == "Never Gonna Give You Up"

    assert tracks[2].artist == "Adele"
    assert tracks[2].title == "Hello"


def test_album_tracks_titles_untouched(mock_ytmusic):
    mock_ytmusic.get_playlist.return_value = {
        "tracks": [
            {
                "videoId": "vid1",
                "title": "One More Time (Official Video)",
                "artists": [{"name": "Daft Punk"}],
                "album": {"name": "Discovery"},
                "isAvailable": True,
            }
        ]
    }
    client = YouTubeMusicClient()
    tracks = client.get_playlist_tracks("PL1234567890123")
    assert len(tracks) == 1
    assert tracks[0].title == "One More Time (Official Video)"
    assert tracks[0].album == "Discovery"


def test_playlist_metadata_and_poster(mock_ytmusic):
    mock_ytmusic.get_playlist.return_value = {
        "title": "Best of 2026",
        "description": "Chart-topping hits",
        "thumbnails": [
            {"url": "https://img.youtube.com/thumb_sm.jpg", "width": 120, "height": 120},
            {"url": "https://img.youtube.com/thumb_lg.jpg", "width": 640, "height": 640},
            {"url": "https://img.youtube.com/thumb_md.jpg", "width": 320, "height": 320},
        ],
    }
    client = YouTubeMusicClient()
    pl = client.get_playlist_by_id("PL1234567890123", suffix=" - YouTube Music")

    mock_ytmusic.get_playlist.assert_called_once_with("PL1234567890123", limit=10_000)
    assert pl is not None
    assert pl.id == "PL1234567890123"
    assert pl.name == "Best of 2026 - YouTube Music"
    assert pl.description == "Chart-topping hits"
    assert pl.poster == "https://img.youtube.com/thumb_lg.jpg"


def test_server_error_returns_empty_and_logs(mock_ytmusic, caplog):
    mock_ytmusic.get_playlist.side_effect = YTMusicError("YouTube Music server error")
    client = YouTubeMusicClient()

    pl = client.get_playlist_by_id("PL1234567890123")
    assert pl is None

    tracks = client.get_playlist_tracks("PL1234567890123")
    assert tracks == []

    # Verify requests.RequestException handling
    mock_ytmusic.get_playlist.side_effect = requests.RequestException("Network timeout")
    assert client.get_playlist_by_id("PL1234567890123") is None
    assert client.get_playlist_tracks("PL1234567890123") == []


def test_shared_singleton():
    c1 = get_shared_youtube_music_client()
    c2 = get_shared_youtube_music_client()
    assert c1 is c2


def test_tracks_capped_at_max_and_warns(mock_ytmusic, caplog):
    mock_ytmusic.get_playlist.return_value = {
        "title": "Huge Playlist",
        "trackCount": 12000,
        "tracks": [
            {
                "videoId": "vid1",
                "title": "Track 1",
                "artists": [{"name": "Artist 1"}],
                "album": {"name": "Album 1"},
                "isAvailable": True,
            }
        ],
    }
    client = YouTubeMusicClient()
    with caplog.at_level("WARNING"):
        tracks = client.get_playlist_tracks("PL_HUGE")

    mock_ytmusic.get_playlist.assert_called_once_with("PL_HUGE", limit=MAX_PLAYLIST_TRACKS)
    assert len(tracks) == 1
    warning_records = [r for r in caplog.records if r.levelname == "WARNING" and "exceeds cap" in r.message]
    assert len(warning_records) == 1
    assert "PL_HUGE" in warning_records[0].message
    assert "12000" in warning_records[0].message
    assert str(MAX_PLAYLIST_TRACKS) in warning_records[0].message


def test_metadata_then_tracks_fetches_once(mock_ytmusic):
    mock_ytmusic.get_playlist.return_value = {
        "title": "Cached Playlist",
        "description": "Cached Description",
        "thumbnails": [{"url": "https://img.youtube.com/thumb.jpg", "width": 100, "height": 100}],
        "tracks": [
            {
                "videoId": "vid1",
                "title": "Song",
                "artists": [{"name": "Artist"}],
                "album": {"name": "Album"},
                "isAvailable": True,
            }
        ],
    }
    client = YouTubeMusicClient(cache_ttl_seconds=300.0)
    pl = client.get_playlist_by_id("PL_CACHED")
    tracks = client.get_playlist_tracks("PL_CACHED")

    mock_ytmusic.get_playlist.assert_called_once_with("PL_CACHED", limit=MAX_PLAYLIST_TRACKS)
    assert pl is not None
    assert pl.name == "Cached Playlist"
    assert len(tracks) == 1
    assert tracks[0].title == "Song"


def test_cache_expires_after_ttl(mock_ytmusic):
    mock_ytmusic.get_playlist.return_value = {
        "title": "Expiring Playlist",
        "tracks": [],
    }
    client = YouTubeMusicClient(cache_ttl_seconds=300.0)

    with patch("trackseerr.clients.youtube_music.time.monotonic", side_effect=[1000.0, 1001.0, 1302.0, 1303.0]):
        # Call 1: monotonic = 1000.0 on check, 1001.0 on store
        client.get_playlist_by_id("PL_EXP")
        assert mock_ytmusic.get_playlist.call_count == 1

        # Call 2: monotonic = 1302.0 (> 1001.0 + 300), entry expired, fetch again, stored at 1303.0
        client.get_playlist_by_id("PL_EXP")
        assert mock_ytmusic.get_playlist.call_count == 2


def test_retries_on_503_then_succeeds(mock_ytmusic):
    mock_ytmusic.get_playlist.side_effect = [
        YTMusicServerError("Server Error (503 Service Unavailable)"),
        {
            "title": "Recovered Playlist",
            "tracks": [],
        },
    ]
    client = YouTubeMusicClient()
    with patch("trackseerr.clients.youtube_music._sleep") as mock_sleep:
        pl = client.get_playlist_by_id("PL_RETRY")

    assert mock_ytmusic.get_playlist.call_count == 2
    mock_sleep.assert_called_once_with(1)
    assert pl is not None
    assert pl.name == "Recovered Playlist"


def test_private_playlist_not_retried(mock_ytmusic):
    mock_ytmusic.get_playlist.side_effect = YTMusicUserError("Playlist is private or does not exist")
    client = YouTubeMusicClient()
    with patch("trackseerr.clients.youtube_music._sleep") as mock_sleep:
        pl = client.get_playlist_by_id("PL_PRIVATE")
        tracks = client.get_playlist_tracks("PL_PRIVATE")

    mock_sleep.assert_not_called()
    # Not retried: each method made 1 call because failed fetches are not cached
    assert mock_ytmusic.get_playlist.call_count == 2
    assert pl is None
    assert tracks == []


def test_gives_up_after_three_attempts(mock_ytmusic):
    mock_ytmusic.get_playlist.side_effect = YTMusicServerError("Gateway Timeout (504)")
    client = YouTubeMusicClient()
    with patch("trackseerr.clients.youtube_music._sleep") as mock_sleep:
        pl = client.get_playlist_by_id("PL_TIMEOUT")

    assert mock_ytmusic.get_playlist.call_count == 3
    assert mock_sleep.call_count == 2
    mock_sleep.assert_any_call(1)
    mock_sleep.assert_any_call(2)
    assert pl is None


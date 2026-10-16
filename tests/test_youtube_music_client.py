"""Tests for YouTube Music client."""

from unittest.mock import MagicMock, patch

import pytest
import requests
from ytmusicapi.exceptions import YTMusicError

from trackseerr.clients.youtube_music import (
    YouTubeMusicClient,
    clean_video_title,
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

    mock_ytmusic.get_playlist.assert_called_once_with("PL1234567890123", limit=None)
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

    mock_ytmusic.get_playlist.assert_called_once_with("PL1234567890123")
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

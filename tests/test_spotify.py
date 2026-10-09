from unittest.mock import MagicMock, patch

from trackseerr.clients.spotify import SpotifyClient


@patch("trackseerr.clients.spotify.SpotifyClientCredentials")
@patch("trackseerr.clients.spotify.spotipy.Spotify")
def test_spotify_user_playlists_pagination(mock_sp_class, mock_creds):
    mock_sp = MagicMock()
    mock_sp_class.return_value = mock_sp

    # Simulate pagination: page 1 has next, page 2 has no next
    page1 = {
        "items": [{"id": f"pl_{i}", "name": f"Playlist {i}", "images": []} for i in range(50)],
        "next": "https://api.spotify.com/v1/users/user/playlists?offset=50",
    }
    page2 = {
        "items": [{"id": f"pl_{i}", "name": f"Playlist {i}", "images": []} for i in range(50, 65)],
        "next": None,
    }
    mock_sp.user_playlists.return_value = page1
    mock_sp.next.return_value = page2

    client = SpotifyClient("cid", "csecret")
    playlists = client.get_user_playlists("my_user", suffix=" - Spotify")

    assert len(playlists) == 65
    assert playlists[0].id == "pl_0"
    assert playlists[0].name == "Playlist 0 - Spotify"
    assert playlists[64].id == "pl_64"
    mock_sp.next.assert_called_once_with(page1)


@patch("trackseerr.clients.spotify.SpotifyClientCredentials")
@patch("trackseerr.clients.spotify.spotipy.Spotify")
def test_spotify_playlist_by_id(mock_sp_class, mock_creds):
    mock_sp = MagicMock()
    mock_sp_class.return_value = mock_sp

    mock_sp.playlist.return_value = {
        "id": "37i9dQZF1DXcBWIGoYBM5M",
        "name": "Today's Top Hits",
        "description": "Jung Kook is on top!",
        "images": [{"url": "http://img.com/tth.jpg"}],
    }

    client = SpotifyClient("cid", "csecret")
    pl = client.get_playlist_by_id("37i9dQZF1DXcBWIGoYBM5M")

    assert pl is not None
    assert pl.id == "37i9dQZF1DXcBWIGoYBM5M"
    assert pl.name == "Today's Top Hits"
    assert pl.poster == "http://img.com/tth.jpg"


@patch("trackseerr.clients.spotify.SpotifyClientCredentials")
@patch("trackseerr.clients.spotify.spotipy.Spotify")
def test_spotify_playlist_tracks_pagination(mock_sp_class, mock_creds):
    mock_sp = MagicMock()
    mock_sp_class.return_value = mock_sp

    page1 = {
        "items": [
            {
                "track": {
                    "name": "Track A",
                    "artists": [{"name": "Artist A"}],
                    "album": {"name": "Album A"},
                    "external_urls": {"spotify": "http://spotify.com/a"},
                }
            }
        ],
        "next": "next_url",
    }
    page2 = {
        "items": [
            {
                "track": {
                    "name": "Track B",
                    "artists": [{"name": "Artist B"}],
                    "album": {"name": "Album B"},
                    "external_urls": {"spotify": "http://spotify.com/b"},
                }
            }
        ],
        "next": None,
    }
    mock_sp.playlist_items.return_value = page1
    mock_sp.next.return_value = page2

    client = SpotifyClient("cid", "csecret")
    tracks = client.get_playlist_tracks("pl_123")

    assert len(tracks) == 2
    assert tracks[0].title == "Track A"
    assert tracks[1].title == "Track B"

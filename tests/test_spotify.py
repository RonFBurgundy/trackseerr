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


@patch("trackseerr.clients.spotify.SpotifyClientCredentials")
@patch("trackseerr.clients.spotify.spotipy.Spotify")
@patch("trackseerr.clients.spotify.SpotifyWebScraper")
def test_playlist_by_id_falls_back_to_scraper_on_404(mock_scraper_class, mock_sp_class, mock_creds):
    from spotipy.exceptions import SpotifyException
    from trackseerr.models import Playlist

    mock_sp = MagicMock()
    mock_sp_class.return_value = mock_sp
    mock_sp.playlist.side_effect = SpotifyException(404, -1, "not found")

    mock_scraper = MagicMock()
    mock_scraper_class.return_value = mock_scraper
    fallback_pl = Playlist(id="37i9dQZF1DXcBWIGoYBM5M", name="Scraped Playlist")
    mock_scraper.get_playlist_by_id.return_value = fallback_pl

    client = SpotifyClient("cid", "csecret")
    result = client.get_playlist_by_id("37i9dQZF1DXcBWIGoYBM5M", suffix=" - Spotify")

    assert result == fallback_pl
    mock_scraper.get_playlist_by_id.assert_called_once_with("37i9dQZF1DXcBWIGoYBM5M", " - Spotify")


@patch("trackseerr.clients.spotify.SpotifyClientCredentials")
@patch("trackseerr.clients.spotify.spotipy.Spotify")
@patch("trackseerr.clients.spotify.SpotifyWebScraper")
def test_playlist_tracks_falls_back_to_scraper_on_403(mock_scraper_class, mock_sp_class, mock_creds):
    from spotipy.exceptions import SpotifyException
    from trackseerr.models import Track

    mock_sp = MagicMock()
    mock_sp_class.return_value = mock_sp
    mock_sp.playlist_items.side_effect = SpotifyException(403, -1, "forbidden")

    mock_scraper = MagicMock()
    mock_scraper_class.return_value = mock_scraper
    fallback_tracks = [Track(title="Scraped Track", artist="Scraped Artist", album="Scraped Album")]
    mock_scraper.get_playlist_tracks.return_value = fallback_tracks

    client = SpotifyClient("cid", "csecret")
    result = client.get_playlist_tracks("37i9dQZF1DXcBWIGoYBM5M")

    assert result == fallback_tracks
    mock_scraper.get_playlist_tracks.assert_called_once_with("37i9dQZF1DXcBWIGoYBM5M")


@patch("trackseerr.clients.spotify.SpotifyClientCredentials")
@patch("trackseerr.clients.spotify.spotipy.Spotify")
@patch("trackseerr.clients.spotify.SpotifyWebScraper")
def test_playlist_by_id_other_errors_do_not_fall_back(mock_scraper_class, mock_sp_class, mock_creds):
    from spotipy.exceptions import SpotifyException

    mock_sp = MagicMock()
    mock_sp_class.return_value = mock_sp
    mock_sp.playlist.side_effect = SpotifyException(500, -1, "internal error")

    mock_scraper = MagicMock()
    mock_scraper_class.return_value = mock_scraper

    client = SpotifyClient("cid", "csecret")
    result = client.get_playlist_by_id("37i9dQZF1DXcBWIGoYBM5M")

    assert result is None
    mock_scraper_class.assert_not_called()
    mock_scraper.get_playlist_by_id.assert_not_called()


@patch("trackseerr.clients.spotify.SpotifyClientCredentials")
@patch("trackseerr.clients.spotify.spotipy.Spotify")
@patch("trackseerr.clients.spotify.SpotifyWebScraper")
def test_scraper_created_once(mock_scraper_class, mock_sp_class, mock_creds):
    from spotipy.exceptions import SpotifyException
    from trackseerr.models import Playlist, Track

    mock_sp = MagicMock()
    mock_sp_class.return_value = mock_sp
    mock_sp.playlist.side_effect = SpotifyException(404, -1, "not found")
    mock_sp.playlist_items.side_effect = SpotifyException(403, -1, "forbidden")

    mock_scraper = MagicMock()
    mock_scraper_class.return_value = mock_scraper
    mock_scraper.get_playlist_by_id.return_value = Playlist(id="pl_1", name="P1")
    mock_scraper.get_playlist_tracks.return_value = [Track(title="T1", artist="A1", album="Alb1")]

    client = SpotifyClient("cid", "csecret")
    client.get_playlist_by_id("pl_1")
    client.get_playlist_tracks("pl_1")

    assert mock_scraper_class.call_count == 1


from unittest.mock import MagicMock, patch

from trackseerr.clients.deezer import DeezerClient


class MockDeezerTrack:
    def __init__(self, title, artist, album, link):
        self.title = title
        self.artist = MagicMock(name=artist)
        self.artist.name = artist
        self.album = MagicMock(title=album)
        self.album.title = album
        self.link = link


class MockDeezerPlaylist:
    def __init__(self, pl_id, title, desc, pic, tracks=None):
        self.id = pl_id
        self.title = title
        self.description = desc
        self.picture_big = pic
        self.tracks = tracks or []


@patch("trackseerr.clients.deezer.deezer.Client")
def test_deezer_user_playlists(mock_client_class):
    mock_dz = MagicMock()
    mock_client_class.return_value = mock_dz

    pl1 = MockDeezerPlaylist(12345, "Rock Anthems", "Classic rock", "http://dz.com/pic.jpg")
    mock_user = MagicMock()
    mock_user.get_playlists.return_value = [pl1]
    mock_dz.get_user.return_value = mock_user

    client = DeezerClient()
    playlists = client.get_user_playlists("9999", suffix=" - Deezer")

    assert len(playlists) == 1
    assert playlists[0].id == "12345"
    assert playlists[0].name == "Rock Anthems - Deezer"
    assert playlists[0].poster == "http://dz.com/pic.jpg"


@patch("trackseerr.clients.deezer.deezer.Client")
def test_deezer_playlist_tracks(mock_client_class):
    mock_dz = MagicMock()
    mock_client_class.return_value = mock_dz

    t1 = MockDeezerTrack("Dracula", "Tame Impala", "Dracula Single", "https://deezer.com/t1")
    pl = MockDeezerPlaylist(12345, "Indie", "", "", tracks=[t1])
    mock_dz.get_playlist.return_value = pl

    client = DeezerClient()
    tracks = client.get_playlist_tracks("12345")

    assert len(tracks) == 1
    assert tracks[0].title == "Dracula"
    assert tracks[0].artist == "Tame Impala"
    assert tracks[0].album == "Dracula Single"
    assert tracks[0].url == "https://deezer.com/t1"

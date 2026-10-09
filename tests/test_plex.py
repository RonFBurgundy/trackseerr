from pathlib import Path
from unittest.mock import MagicMock, patch

from plexapi.exceptions import NotFound

from trackseerr.clients.plex import PlexClient, _WARNED_MISSING_SECTIONS, clean_title, music_sections
from trackseerr.config import Config
from trackseerr.media_servers.plex import PlexMediaServer
from trackseerr.models import Playlist, Track


def test_clean_title():
    assert clean_title("Bohemian Rhapsody (2011 Remaster)") == "Bohemian Rhapsody"
    assert clean_title("Song Name (feat. Drake)") == "Song Name"
    assert clean_title("Hotel California - Remastered 2013") == "Hotel California"
    assert clean_title("Comfortably Numb [Deluxe Edition]") == "Comfortably Numb"
    assert clean_title("Clean Title") == "Clean Title"


class MockArtist:
    def __init__(self, title):
        self.title = title


class MockAlbum:
    def __init__(self, title):
        self.title = title


class MockPlexTrack:
    def __init__(self, title, artist, album):
        self.title = title
        self._artist = MockArtist(artist)
        self._album = MockAlbum(album)

    def artist(self):
        return self._artist

    def album(self):
        return self._album


@patch("trackseerr.clients.plex.PlexServer")
def test_plex_client_ssl_verification(mock_server):
    # Verify SSL True
    PlexClient("https://plex.example.com", "token", verify_ssl=True)
    session_arg = mock_server.call_args[1].get("session")
    assert session_arg.verify is True

    # Verify SSL False
    PlexClient("https://plex.example.com", "token", verify_ssl=False)
    session_arg2 = mock_server.call_args[1].get("session")
    assert session_arg2.verify is False


@patch("trackseerr.clients.plex.PlexServer")
def test_match_track_direct(mock_server):
    client = PlexClient("http://localhost:32400", "token")
    mock_track = MockPlexTrack("Karma Police", "Radiohead", "OK Computer")
    client.server.search.return_value = [mock_track]

    t = Track(title="Karma Police", artist="Radiohead", album="OK Computer")
    matched = client.match_track(t)
    assert matched is mock_track


@patch("trackseerr.clients.plex.PlexServer")
def test_match_track_fallback_cleaned_title(mock_server):
    client = PlexClient("http://localhost:32400", "token")
    mock_track = MockPlexTrack("Karma Police", "Radiohead", "OK Computer")
    # First search fails, second search (cleaned title) returns match
    client.server.search.side_effect = [[], [mock_track]]

    t = Track(title="Karma Police (2017 Remaster)", artist="Radiohead", album="OK Computer")
    matched = client.match_track(t)
    assert matched is mock_track
    assert client.server.search.call_count == 2


@patch("trackseerr.clients.plex.PlexServer")
def test_sync_playlist_creates_new(mock_server, tmp_path):
    client = PlexClient("http://localhost:32400", "token")
    mock_track = MockPlexTrack("Song 1", "Artist 1", "Album 1")
    client.server.search.return_value = [mock_track]

    # Playlist doesn't exist initially
    client.server.playlist.side_effect = [NotFound("Not found"), MagicMock()]

    playlist = Playlist(
        id="p1",
        name="Test Playlist",
        description="A great test playlist",
        poster="http://img.com/p.jpg",
        tracks=[Track("Song 1", "Artist 1", "Album 1")],
    )

    result = client.sync_playlist(playlist, data_dir=str(tmp_path))
    assert result.success is True
    assert result.matched_tracks == 1
    assert result.missing_tracks == 0
    client.server.createPlaylist.assert_called_once()


@patch("trackseerr.clients.plex.PlexServer")
def test_sync_playlist_missing_tracks_csv(mock_server, tmp_path):
    client = PlexClient("http://localhost:32400", "token")
    # No matches found
    client.server.search.return_value = []

    playlist = Playlist(
        id="p1",
        name="Missing Only",
        tracks=[Track("Missing Song", "Unknown Artist", "Unknown Album", "http://spotify.com/1")],
    )

    result = client.sync_playlist(playlist, write_missing_as_csv=True, data_dir=str(tmp_path))
    assert result.success is False
    assert result.missing_tracks == 1

    csv_file = tmp_path / "Missing Only.csv"
    assert csv_file.exists()
    content = csv_file.read_text()
    assert "Missing Song" in content


@patch("trackseerr.clients.plex.PlexServer")
def test_get_user_server_admin_vs_switch_user(mock_server):
    client = PlexClient("http://localhost:32400", "token")
    client.server.myPlexAccount.return_value.username = "Boss"
    assert client.is_admin_username("boss") is True
    assert client.get_user_server("BOSS") is client.server
    client.server.switchUser.assert_not_called()

    other = client.get_user_server("alice")
    client.server.switchUser.assert_called_once_with("alice")
    assert other is client.server.switchUser.return_value


def test_music_sections_preferred_title_selects_section():
    server = MagicMock()
    sec1 = MagicMock(title="Standard Music", type="artist")
    sec2 = MagicMock(title="Lossless Music", type="artist")
    sec3 = MagicMock(title="Audiobooks", type="audiobook")
    server.library.sections.return_value = [sec1, sec2, sec3]

    result = music_sections(server, preferred="Lossless Music")
    assert result == [sec2, sec1]


def test_music_sections_case_insensitive():
    server = MagicMock()
    sec1 = MagicMock(title="Standard Music", type="artist")
    sec2 = MagicMock(title="Lossless Music", type="artist")
    server.library.sections.return_value = [sec1, sec2]

    result = music_sections(server, preferred="lossless music")
    assert result == [sec2, sec1]


def test_music_sections_missing_title_falls_back_with_warning(caplog):
    import logging
    server = MagicMock()
    sec1 = MagicMock(title="Standard Music", type="artist")
    sec2 = MagicMock(title="Lossless Music", type="artist")
    server.library.sections.return_value = [sec1, sec2]

    _WARNED_MISSING_SECTIONS.clear()
    with caplog.at_level(logging.WARNING):
        result = music_sections(server, preferred="Vinyl Collection")

    assert result == [sec1, sec2]
    assert any("Vinyl Collection" in record.message and "Standard Music" in record.message for record in caplog.records)

    # Calling again logs only once
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        result2 = music_sections(server, preferred="Vinyl Collection")
    assert result2 == [sec1, sec2]
    assert not any("Vinyl Collection" in record.message for record in caplog.records)


def test_music_sections_unset_keeps_current_first_section_behavior():
    server = MagicMock()
    sec1 = MagicMock(title="Alpha Music", type="artist")
    sec2 = MagicMock(title="Beta Music", type="artist")
    server.library.sections.return_value = [sec1, sec2]

    assert music_sections(server, preferred=None) == [sec1, sec2]
    assert music_sections(server, preferred="") == [sec1, sec2]
    assert music_sections(server, preferred="   ") == [sec1, sec2]


def test_config_plex_music_section_from_env(monkeypatch):
    monkeypatch.setenv("PLEX_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "token")
    monkeypatch.setenv("PLEX_MUSIC_SECTION", "  Flac Library  ")
    cfg = Config.from_env()
    assert cfg.plex_music_section == "Flac Library"

    monkeypatch.setenv("PLEX_MUSIC_SECTION", "   ")
    cfg2 = Config.from_env()
    assert cfg2.plex_music_section is None

    monkeypatch.delenv("PLEX_MUSIC_SECTION", raising=False)
    cfg3 = Config.from_env()
    assert cfg3.plex_music_section is None


@patch("trackseerr.clients.plex.PlexServer")
def test_plex_media_server_honors_preferred_section(mock_server):
    client = PlexClient("http://localhost:32400", "token", music_section="Hi-Fi")
    sec1 = MagicMock(title="Music", type="artist")
    sec2 = MagicMock(title="Hi-Fi", type="artist")
    client.server.library.sections.return_value = [sec1, sec2]

    adapter = PlexMediaServer(client)
    sections = adapter._music_sections()
    assert sections == [sec2, sec1]


@patch("trackseerr.clients.plex.PlexServer")
def test_plex_client_smart_mix_tracks_honors_preferred_section(mock_server):
    client = PlexClient("http://localhost:32400", "token", music_section="Special")
    sec1 = MagicMock(title="General", type="artist")
    sec2 = MagicMock(title="Special", type="artist")
    mock_track = MagicMock(ratingKey="123", title="Track 1", viewCount=5, lastViewedAt=None)
    mock_track.artist.return_value.title = "Artist 1"
    mock_track.album.return_value.title = "Album 1"
    sec2.searchTracks.return_value = [mock_track]
    client.server.library.sections.return_value = [sec1, sec2]

    tracks = client.get_smart_mix_tracks("heavy_rotation")
    assert len(tracks) == 1
    assert tracks[0]["title"] == "Track 1"
    sec2.searchTracks.assert_called_once()
    sec1.searchTracks.assert_not_called()


@patch("trackseerr.clients.plex.PlexServer")
def test_get_media_server_honors_plex_music_section(mock_server):
    from trackseerr.media_servers import get_media_server

    cfg = Config(
        plex_url="http://localhost:32400",
        plex_token="token",
        plex_music_section="FLAC Music",
    )
    # 1. Fresh instantiation
    adapter = get_media_server(cfg)
    assert isinstance(adapter, PlexMediaServer)
    assert adapter._music_section == "FLAC Music"
    assert adapter._client.music_section == "FLAC Music"

    # 2. Existing client provided without music_section inherits config
    client = PlexClient("http://localhost:32400", "token")
    adapter2 = get_media_server(cfg, plex_client=client)
    assert isinstance(adapter2, PlexMediaServer)
    assert adapter2._music_section == "FLAC Music"


@patch("trackseerr.clients.plex.PlexServer")
def test_plex_client_refresh_music_library_honors_preferred_section(mock_server):
    client = PlexClient("http://localhost:32400", "token", music_section="Lossless")
    sec1 = MagicMock(title="Standard", type="artist")
    sec2 = MagicMock(title="Lossless", type="artist")
    client.server.library.sections.return_value = [sec1, sec2]

    assert client.refresh_music_library() is True
    sec2.update.assert_called_once()
    sec1.update.assert_not_called()



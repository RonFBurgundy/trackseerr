"""Clients package for Plex, Spotify, and Deezer."""

from .deezer import DeezerClient
from .lidarr import LidarrClient
from .plex import PlexClient
from .spotify import SpotifyClient
from .spotify_scraper import SpotifyWebScraper

__all__ = ["PlexClient", "SpotifyClient", "SpotifyWebScraper", "DeezerClient", "LidarrClient"]

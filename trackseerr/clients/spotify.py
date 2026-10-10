import logging
from typing import List, Optional

import requests
import spotipy
from requests.adapters import HTTPAdapter
from spotipy.oauth2 import SpotifyClientCredentials
from urllib3.util import Retry

from ..models import Playlist, Track
from .spotify_scraper import SpotifyWebScraper

logger = logging.getLogger(__name__)


class SpotifyClient:
    """Manages interactions with Spotify Web API with auto-pagination and retry backoff."""

    def __init__(self, client_id: str, client_secret: str, retries: int = 3, backoff_factor: float = 0.5):
        self.client_id = client_id
        self.client_secret = client_secret
        self._scraper: Optional[SpotifyWebScraper] = None

        # Configure session with robust backoff for 429 rate limits and 5xx errors
        session = requests.Session()
        retry_strategy = Retry(
            total=retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        auth_manager = SpotifyClientCredentials(
            client_id=client_id,
            client_secret=client_secret,
        )
        self.sp = spotipy.Spotify(auth_manager=auth_manager, requests_session=session)
        logger.info("Configured Spotify API client with retry backoff adapter")

    def _fallback(self) -> SpotifyWebScraper:
        """Lazily create and return the keyless SpotifyWebScraper instance."""
        if self._scraper is None:
            self._scraper = SpotifyWebScraper()
        return self._scraper

    def get_user_playlists(self, user_id: str, suffix: str = "") -> List[Playlist]:
        """Fetch all playlists owned or followed by the specified Spotify user ID, with pagination."""
        playlists = []
        logger.info("Fetching playlists for Spotify user: %s", user_id)
        try:
            page = self.sp.user_playlists(user_id, limit=50)
            while page:
                for item in page.get("items", []):
                    if not item:
                        continue
                    poster_url = ""
                    images = item.get("images") or []
                    if images and len(images) > 0 and images[0].get("url"):
                        poster_url = images[0]["url"]

                    playlists.append(
                        Playlist(
                            id=item["id"],
                            name=f"{item['name']}{suffix}",
                            description=item.get("description") or "",
                            poster=poster_url,
                        )
                    )
                if page.get("next"):
                    page = self.sp.next(page)
                else:
                    page = None
            logger.info("Retrieved %d total playlists for Spotify user '%s'", len(playlists), user_id)
        except Exception as e:
            logger.error("Failed to retrieve Spotify playlists for user '%s': %s", user_id, e)

        return playlists

    def get_playlist_by_id(self, playlist_id: str, suffix: str = "") -> Optional[Playlist]:
        """Fetch metadata for a single playlist by its ID or URI."""
        try:
            data = self.sp.playlist(playlist_id, fields="id,name,description,images")
            poster_url = ""
            images = data.get("images") or []
            if images and len(images) > 0 and images[0].get("url"):
                poster_url = images[0]["url"]

            return Playlist(
                id=data["id"],
                name=f"{data['name']}{suffix}",
                description=data.get("description") or "",
                poster=poster_url,
            )
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status in (403, 404):
                logger.info(
                    "Spotify API refused playlist %s (HTTP %s); reading it through the public web player instead",
                    playlist_id,
                    e.http_status,
                )
                return self._fallback().get_playlist_by_id(playlist_id, suffix)
            logger.error("Failed to fetch Spotify playlist with ID '%s': %s", playlist_id, e)
            return None
        except Exception as e:
            logger.error("Failed to fetch Spotify playlist with ID '%s': %s", playlist_id, e)
            return None

    def get_playlist_tracks(self, playlist_id: str) -> List[Track]:
        """Retrieve all tracks within a playlist with dynamic pagination."""
        tracks = []
        try:
            # Modern API endpoint supporting pagination
            results = self.sp.playlist_items(
                playlist_id,
                additional_types=["track"],
                limit=100,
            )
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status in (403, 404):
                logger.info(
                    "Spotify API refused playlist %s (HTTP %s); reading it through the public web player instead",
                    playlist_id,
                    e.http_status,
                )
                return self._fallback().get_playlist_tracks(playlist_id)
            logger.error("Failed to fetch tracks for Spotify playlist '%s': %s", playlist_id, e)
            return []
        except Exception as e:
            logger.error("Failed to fetch tracks for Spotify playlist '%s': %s", playlist_id, e)
            return []

        try:
            while results:
                for item in results.get("items", []):
                    track_data = item.get("track") if item else None
                    if not track_data or not track_data.get("name"):
                        continue

                    title = track_data["name"]
                    artist = track_data["artists"][0]["name"] if track_data.get("artists") else "Unknown Artist"
                    album = track_data["album"]["name"] if track_data.get("album") else ""
                    url = track_data.get("external_urls", {}).get("spotify", "")

                    duration_ms = track_data.get("duration_ms")
                    duration = float(duration_ms) / 1000.0 if isinstance(duration_ms, (int, float)) else None
                    tracks.append(Track(title=title, artist=artist, album=album, url=url, duration_seconds=duration))

                if results.get("next"):
                    try:
                        results = self.sp.next(results)
                    except spotipy.exceptions.SpotifyException as e:
                        if e.http_status in (403, 404):
                            logger.warning(
                                "Spotify API refused next page for playlist %s (HTTP %s); returning collected tracks",
                                playlist_id,
                                e.http_status,
                            )
                            break
                        raise
                else:
                    results = None

            logger.debug("Fetched %d tracks for Spotify playlist '%s'", len(tracks), playlist_id)
        except Exception as e:
            logger.error("Failed to fetch tracks for Spotify playlist '%s': %s", playlist_id, e)

        return tracks

    def fetch_all_playlists(
        self,
        user_id: Optional[str] = None,
        playlist_ids: Optional[List[str]] = None,
        suffix: str = "",
    ) -> List[Playlist]:
        """Fetch all targeted playlists (user + specific IDs) and populate their tracks."""
        collected: List[Playlist] = []
        seen_ids = set()

        if user_id:
            user_playlists = self.get_user_playlists(user_id, suffix=suffix)
            for p in user_playlists:
                if p.id not in seen_ids:
                    seen_ids.add(p.id)
                    collected.append(p)

        if playlist_ids:
            for pid in playlist_ids:
                if pid not in seen_ids:
                    p = self.get_playlist_by_id(pid, suffix=suffix)
                    if p:
                        seen_ids.add(p.id)
                        collected.append(p)

        # Populate tracks for each playlist
        for pl in collected:
            pl.tracks = self.get_playlist_tracks(pl.id)

        return collected

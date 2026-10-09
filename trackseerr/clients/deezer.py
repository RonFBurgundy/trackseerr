import logging
from typing import List, Optional

import deezer

from ..models import Playlist, Track

logger = logging.getLogger(__name__)


class DeezerClient:
    """Manages interactions with Deezer API using modern deezer-python."""

    def __init__(self):
        self.client = deezer.Client()
        logger.info("Initialized Deezer API client")

    def get_user_playlists(self, user_id: str, suffix: str = "") -> List[Playlist]:
        """Fetch all public playlists for a Deezer numerical user ID."""
        playlists = []
        logger.info("Fetching playlists for Deezer user: %s", user_id)
        try:
            user = self.client.get_user(user_id)
            for item in user.get_playlists():
                title = getattr(item, "title", None) or (item.as_dict().get("title") if hasattr(item, "as_dict") else "")
                pl_id = str(getattr(item, "id", None) or (item.as_dict().get("id") if hasattr(item, "as_dict") else ""))
                desc = getattr(item, "description", None) or (item.as_dict().get("description", "") if hasattr(item, "as_dict") else "")
                poster = (
                    getattr(item, "picture_big", None)
                    or getattr(item, "picture_medium", "")
                    or (item.as_dict().get("picture_big", "") if hasattr(item, "as_dict") else "")
                )

                if pl_id and title:
                    playlists.append(
                        Playlist(
                            id=pl_id,
                            name=f"{title}{suffix}",
                            description=desc or "",
                            poster=poster or "",
                        )
                    )
            logger.info("Retrieved %d playlists for Deezer user '%s'", len(playlists), user_id)
        except Exception as e:
            logger.error("Failed to retrieve Deezer playlists for user '%s': %s", user_id, e)

        return playlists

    def get_playlist_by_id(self, playlist_id: str, suffix: str = "") -> Optional[Playlist]:
        """Fetch metadata for a single Deezer playlist by numerical ID."""
        try:
            pl = self.client.get_playlist(playlist_id)
            title = getattr(pl, "title", None) or (pl.as_dict().get("title") if hasattr(pl, "as_dict") else "")
            desc = getattr(pl, "description", None) or (pl.as_dict().get("description", "") if hasattr(pl, "as_dict") else "")
            poster = (
                getattr(pl, "picture_big", None)
                or getattr(pl, "picture_medium", "")
                or (pl.as_dict().get("picture_big", "") if hasattr(pl, "as_dict") else "")
            )
            return Playlist(
                id=str(playlist_id),
                name=f"{title}{suffix}",
                description=desc or "",
                poster=poster or "",
            )
        except Exception as e:
            logger.error("Failed to fetch Deezer playlist with ID '%s': %s", playlist_id, e)
            return None

    def get_playlist_tracks(self, playlist_id: str) -> List[Track]:
        """Fetch all tracks for a Deezer playlist."""
        tracks = []
        try:
            pl = self.client.get_playlist(playlist_id)
            raw_tracks = getattr(pl, "tracks", None)
            if raw_tracks is None and hasattr(pl, "get_tracks"):
                raw_tracks = pl.get_tracks()

            if raw_tracks:
                for item in raw_tracks:
                    title = getattr(item, "title", None) or (item.as_dict().get("title") if hasattr(item, "as_dict") else "")
                    artist_obj = getattr(item, "artist", None)
                    if artist_obj and hasattr(artist_obj, "name"):
                        artist = artist_obj.name
                    elif isinstance(artist_obj, dict):
                        artist = artist_obj.get("name", "Unknown Artist")
                    else:
                        artist = "Unknown Artist"

                    album_obj = getattr(item, "album", None)
                    if album_obj and hasattr(album_obj, "title"):
                        album = album_obj.title
                    elif isinstance(album_obj, dict):
                        album = album_obj.get("title", "")
                    else:
                        album = ""

                    url = getattr(item, "link", None) or (item.as_dict().get("link", "") if hasattr(item, "as_dict") else "")

                    if title:
                        raw_duration = getattr(item, "duration", None)
                        duration = float(raw_duration) if isinstance(raw_duration, (int, float)) else None
                        tracks.append(Track(title=title, artist=artist, album=album, url=url or "", duration_seconds=duration))

            logger.debug("Fetched %d tracks for Deezer playlist '%s'", len(tracks), playlist_id)
        except Exception as e:
            logger.error("Failed to fetch tracks for Deezer playlist '%s': %s", playlist_id, e)

        return tracks

    def fetch_all_playlists(
        self,
        user_id: Optional[str] = None,
        playlist_ids: Optional[List[str]] = None,
        suffix: str = "",
    ) -> List[Playlist]:
        """Fetch all targeted playlists (user + explicit IDs) and populate tracks."""
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

        for pl in collected:
            pl.tracks = self.get_playlist_tracks(pl.id)

        return collected

import csv
import logging
import re
import sqlite3
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, List, Optional, Tuple

import requests
import urllib.parse
import urllib3
from plexapi.exceptions import BadRequest, NotFound, Unauthorized
from plexapi.server import PlexServer

from ..models import Playlist, SyncResult, Track
from ..security import is_safe_image_url, safe_data_path
from ..missing_csv import delete_missing_csv, write_missing_csv
from ..redaction import redact_text, safe_exc

logger = logging.getLogger(__name__)


def clean_title(title: str) -> str:
    """Strip remaster info, feature tags, and bracketed metadata for fallback search."""
    # Remove text in parentheses or brackets mentioning remaster, live, bonus, feat, etc.
    cleaned = re.sub(
        r"[\(\[](.*?)(remaster|remix|feat|ft\.|live|deluxe|version|edition|mono|stereo|anniversary)(.*?)[\)\]]",
        "",
        title,
        flags=re.IGNORECASE,
    )
    # Remove trailing ' - Remastered...' or ' - Live...'
    cleaned = re.sub(
        r"\s*-\s*(remastered|remaster|live|deluxe|radio edit|mono|stereo).*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # If title has parentheses without tags, also strip for clean fallback
    cleaned = re.sub(r"[\(\[].*?[\)\]]", "", cleaned)
    return cleaned.strip()


PLEX_ERRORS = (NotFound, BadRequest, Unauthorized, requests.exceptions.RequestException)


class PlaylistProtectedError(Exception):
    """Raised when a sync/copy would overwrite a playlist TrackSeerr must not touch."""


class MixNotFoundError(Exception):
    """Raised when a Plexamp mix cannot be resolved (or has no tracks)."""


def is_smart_playlist(playlist: Any) -> bool:
    """True only when Plex explicitly reports the playlist as smart."""
    return getattr(playlist, "smart", False) is True


def _as_utc(value: datetime) -> datetime:
    """Normalise to an aware UTC datetime. Naive values: Plex's are local time, so treat naive as local."""
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc)


def _parse_sqlite_utc(value: Any) -> Optional[datetime]:
    """Parse a SQLite CURRENT_TIMESTAMP string (UTC) into an aware datetime, or None when unparseable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def classify_playlist_owner(
    db: Any, plex_user: str, title: str, smart: bool, added_at: Optional[datetime] = None
) -> str:
    """Classify a first-seen playlist.

    smart -> plexamp. A title match against a TrackSeerr playlist targeting this user counts only when that row
    was actually synced before (``last_synced_at`` set) and the Plex playlist (when ``added_at`` is known) was not
    created before the row. Otherwise -> user.
    """
    if smart:
        return "plexamp"
    try:
        match = db.find_playlist_by_name_for_username(title, plex_user) if db is not None else None
    except sqlite3.Error as e:
        logger.warning("Could not check legacy TrackSeerr playlists for '%s': %s", title, e)
        return "user"
    if not match or not match.get("last_synced_at"):
        return "user"
    if isinstance(added_at, datetime):
        created = _parse_sqlite_utc(match.get("created_at"))
        if created is not None and _as_utc(added_at) < created:
            return "user"
    return "trackseerr"


class PlexClient:
    """Manages interactions with the Plex Media Server."""

    def __init__(self, base_url: str, token: str, verify_ssl: bool = True, timeout: int = 30):
        self.base_url = base_url
        self.token = token
        self.verify_ssl = verify_ssl

        session = requests.Session()
        if not verify_ssl:
            session.verify = False
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            logger.warning("Plex SSL verification disabled (verify=False)")

        try:
            self.server = PlexServer(base_url, token, session=session, timeout=timeout)
            logger.info("Successfully connected to Plex Server: %s", getattr(self.server, "friendlyName", base_url))
        except Exception as e:
            logger.error("Failed to connect to Plex Server at %s: %s", redact_text(base_url), safe_exc(e))
            raise

    @property
    def machine_identifier(self) -> str:
        """Return the unique machine identifier of the Plex Media Server."""
        return str(getattr(self.server, "machineIdentifier", "") or "")

    def get_home_users(self) -> List[dict]:
        """Retrieve all Plex Home and managed users including the admin user.

        Queries self.server.myPlexAccount().users and includes the admin user
        (self.server.myPlexAccount().username). Handles non-PlexPass or non-PlexHome
        configurations gracefully.

        Returns:
            List of dicts: [{'id': ..., 'username': ..., 'email': ..., 'thumb': ..., 'is_admin': ...}, ...]
        """
        home_users: List[dict] = []
        try:
            account = self.server.myPlexAccount()
        except Exception as e:
            logger.warning("Could not access myPlexAccount (non-PlexPass or offline): %s", safe_exc(e))
            admin_name = str(getattr(self.server, "friendlyName", "Admin") or "Admin")
            return [
                {
                    "id": "admin",
                    "username": admin_name,
                    "email": "",
                    "thumb": "",
                    "is_admin": True,
                }
            ]

        # Admin user
        admin_username = str(getattr(account, "username", "") or "Admin")
        admin_id = str(getattr(account, "id", "") or "admin")
        admin_email = str(getattr(account, "email", "") or "")
        admin_thumb = str(getattr(account, "thumb", "") or "")
        home_users.append(
            {
                "id": admin_id,
                "username": admin_username,
                "email": admin_email,
                "thumb": admin_thumb,
                "is_admin": True,
            }
        )

        # Home / managed users
        try:
            users_attr = getattr(account, "users", None)
            if callable(users_attr):
                users_list = users_attr()
            elif isinstance(users_attr, (list, tuple)):
                users_list = list(users_attr)
            else:
                users_list = []

            for u in users_list:
                u_name = (
                    getattr(u, "username", None)
                    or getattr(u, "title", None)
                    or getattr(u, "name", "")
                )
                if not u_name:
                    continue
                # Skip duplicate admin entry if present in users
                if str(u_name).lower() == admin_username.lower():
                    continue
                u_id = str(getattr(u, "id", "") or u_name)
                u_email = str(getattr(u, "email", "") or "")
                u_thumb = str(getattr(u, "thumb", "") or "")
                home_users.append(
                    {
                        "id": u_id,
                        "username": str(u_name),
                        "email": u_email,
                        "thumb": u_thumb,
                        "is_admin": False,
                    }
                )
        except Exception as e:
            logger.warning("Could not retrieve home users from myPlexAccount: %s", safe_exc(e))

        return home_users

    def match_track(
        self, track: Track, threshold: float = 0.9, db: Optional[Any] = None
    ) -> Optional[object]:
        """Search Plex library for a matching track using match overrides or fuzzy title, artist, and album comparison."""
        # 1. Match Memory override check
        if db is not None:
            try:
                override = db.get_match_override(track.title, track.artist)
                if override and override.get("plex_rating_key"):
                    target_key = int(override["plex_rating_key"])
                    fetched = self.server.fetchItem(target_key)
                    if fetched:
                        logger.debug("Applied match memory override for '%s - %s'", track.artist, track.title)
                        return fetched
            except Exception as e:
                logger.debug("Failed match override fetch for '%s': %s", track.title, safe_exc(e))

        candidates = []
        try:
            candidates = self.server.search(track.title, mediatype="track", limit=10)
        except BadRequest as e:
            logger.debug("BadRequest searching for %s: %s", track.title, safe_exc(e))

        # Check initial search results
        matched = self._eval_candidates(candidates, track, threshold)
        if matched:
            return matched

        # Fallback: clean title if it contained qualifiers/remaster text
        stripped_title = clean_title(track.title)
        if stripped_title and stripped_title.lower() != track.title.lower():
            logger.debug("Retrying search with sanitized title: '%s' -> '%s'", track.title, stripped_title)
            try:
                fallback_candidates = self.server.search(stripped_title, mediatype="track", limit=10)
                matched = self._eval_candidates(fallback_candidates, track, threshold)
                if matched:
                    return matched
            except BadRequest as e:
                logger.debug("BadRequest searching for sanitized title %s: %s", stripped_title, safe_exc(e))

        return None

    def _eval_candidates(self, candidates: List[object], track: Track, threshold: float) -> Optional[object]:
        target_artist = track.artist.lower().strip()
        target_album = track.album.lower().strip()

        for candidate in candidates:
            try:
                cand_artist = candidate.artist().title.lower().strip() if candidate.artist() else ""
                artist_ratio = SequenceMatcher(None, cand_artist, target_artist).quick_ratio()
                if artist_ratio >= threshold:
                    return candidate

                cand_album = candidate.album().title.lower().strip() if candidate.album() else ""
                album_ratio = SequenceMatcher(None, cand_album, target_album).quick_ratio()
                if album_ratio >= threshold:
                    return candidate
            except (AttributeError, IndexError, Exception) as e:
                logger.debug("Candidate comparison error for track '%s': %s", track.title, safe_exc(e))
                continue

        return None

    def match_playlist_tracks(
        self, tracks: List[Track], threshold: float = 0.9, db: Optional[Any] = None
    ) -> Tuple[List[object], List[Track]]:
        """Match a list of tracks against the Plex library."""
        available_tracks = []
        missing_tracks = []

        for track in tracks:
            match = self.match_track(track, threshold=threshold, db=db)
            if match is not None:
                available_tracks.append(match)
            else:
                missing_tracks.append(track)

        return available_tracks, missing_tracks

    def update_or_create_playlist(
        self,
        name: str,
        tracks: List[object],
        description: str = "",
        poster_url: str = "",
        append: bool = False,
        add_description: bool = True,
        add_poster: bool = True,
        server: Optional[object] = None,
        admin_server: Optional[object] = None,
        db: Optional[Any] = None,
        username: Optional[str] = None,
        skip_rating_keys: Optional[set] = None,
    ) -> object:
        """Create or update a playlist on Plex with given tracks and metadata.

        Overwrite protection: smart playlists are never touched; when ``db`` and ``username`` are given,
        playlists registered as owned by the user or Plexamp are never touched either. Unregistered,
        collisions without a registry row are classified first and only claimed when legacy-TrackSeerr.
        Raises PlaylistProtectedError when the existing playlist must not be modified.
        """
        srv = server if server is not None else self.server
        admin = admin_server if admin_server is not None else self.server
        who = (username or "").lower()
        if db is not None and not who:
            raise ValueError("username is required when db is provided")

        try:
            plex_playlist = srv.playlist(name)
        except NotFound:
            plex_playlist = None

        if plex_playlist is not None:
            rating_key = str(getattr(plex_playlist, "ratingKey", "") or "")
            if skip_rating_keys and rating_key in {str(k) for k in skip_rating_keys}:
                raise PlaylistProtectedError(
                    f"Skipped: '{name}' is the adopted source playlist in {username}'s profile"
                )
            if is_smart_playlist(plex_playlist):
                logger.warning("Refusing to overwrite smart playlist '%s' in %s's profile", name, username)
                raise PlaylistProtectedError(
                    f"Protected: '{name}' is owned by plexamp in {username}'s profile"
                )
            if db is not None:
                row = db.get_plex_registry_row(who, rating_key)
                if row and row["owner"] in ("user", "plexamp"):
                    logger.warning(
                        "Refusing to overwrite '%s' (owner=%s) in %s's profile", name, row["owner"], username
                    )
                    raise PlaylistProtectedError(
                        f"Protected: '{name}' is owned by {row['owner']} in {username}'s profile"
                    )
                if not row:
                    owner = classify_playlist_owner(
                        db, who, name, False, added_at=getattr(plex_playlist, "addedAt", None)
                    )
                    if owner != "trackseerr":
                        logger.warning(
                            "Refusing to overwrite unregistered '%s' (owner=%s) in %s's profile", name, owner, username
                        )
                        raise PlaylistProtectedError(
                            f"Protected: '{name}' is owned by {owner} in {username}'s profile"
                        )
                    logger.info("Claiming legacy playlist '%s' for TrackSeerr in %s's profile", name, username)
            logger.info("Found existing Plex playlist '%s'", name)
            if not append:
                plex_playlist.removeItems(plex_playlist.items())
            plex_playlist.addItems(tracks)
            logger.info("Updated tracks for playlist '%s'", name)
        else:
            logger.info("Creating new Plex playlist '%s'", name)
            srv.createPlaylist(title=name, items=tracks)
            plex_playlist = srv.playlist(name)

        if db is not None:
            try:
                db.upsert_plex_registry(
                    who,
                    str(getattr(plex_playlist, "ratingKey", "") or ""),
                    name,
                    "regular",
                    "trackseerr",
                )
            except sqlite3.Error as e:
                logger.warning("Failed to record registry row for '%s': %s", name, e)

        if add_description and description:
            try:
                # Strip raw HTML tags if present in description
                clean_desc = re.sub(r"<[^>]+>", "", description).strip()
                plex_playlist.edit(summary=clean_desc)
                logger.debug("Updated summary for playlist '%s'", name)
            except Exception as e:
                logger.warning("Failed to update summary for '%s': %s", name, safe_exc(e))

        if add_poster and poster_url and is_safe_image_url(poster_url):
            try:
                plex_playlist.uploadPoster(url=poster_url)
                logger.debug("Updated poster for playlist '%s'", name)
            except Exception as e:
                logger.warning(
                    "Failed to upload poster for '%s' via user session: %s. Attempting admin fallback...",
                    name,
                    e,
                )
                # Injects poster using admin server session if user upload fails
                # (bypassing managed user 401 permission bug via admin /library/metadata/<ratingKey>/posters?url=...)
                rating_key = getattr(plex_playlist, "ratingKey", None)
                if rating_key and admin is not None:
                    try:
                        encoded_url = urllib.parse.quote_plus(poster_url)
                        key = f"/library/metadata/{rating_key}/posters?url={encoded_url}"
                        post_method = getattr(getattr(admin, "_session", requests), "post", requests.post)
                        admin.query(key, method=post_method)
                        logger.info("Successfully injected poster for playlist '%s' via admin session", name)
                    except Exception as admin_err:
                        logger.warning(
                            "Failed to inject poster for '%s' via admin fallback: %s",
                            name,
                            safe_exc(admin_err),
                        )

        return plex_playlist

    def write_missing_csv(self, missing_tracks: List[Track], playlist_name: str, data_dir: str = "/data") -> None:
        """Write missing tracks to CSV file in data directory with formula injection defense."""
        write_missing_csv(missing_tracks, playlist_name, data_dir)

    def delete_missing_csv(self, playlist_name: str, data_dir: str = "/data") -> None:
        """Delete previously written missing CSV if all tracks now match."""
        delete_missing_csv(playlist_name, data_dir)

    def sync_playlist(
        self,
        playlist: Playlist,
        append: bool = False,
        add_description: bool = True,
        add_poster: bool = True,
        write_missing_as_csv: bool = False,
        data_dir: str = "/data",
        threshold: float = 0.9,
    ) -> SyncResult:
        """Execute full match and sync for a single playlist."""
        logger.info("Syncing playlist '%s' (%d tracks)", playlist.name, len(playlist.tracks))
        matched, missing = self.match_playlist_tracks(playlist.tracks, threshold=threshold)

        if not matched:
            logger.warning("No tracks in playlist '%s' could be matched in Plex library", playlist.name)
            if write_missing_as_csv and missing:
                self.write_missing_csv(missing, playlist.name, data_dir=data_dir)
            return SyncResult(
                playlist_name=playlist.name,
                total_tracks=len(playlist.tracks),
                matched_tracks=0,
                missing_tracks=len(missing),
                success=False,
                error="Zero tracks matched in Plex library",
            )

        try:
            self.update_or_create_playlist(
                name=playlist.name,
                tracks=matched,
                description=playlist.description,
                poster_url=playlist.poster,
                append=append,
                add_description=add_description,
                add_poster=add_poster,
            )

            if write_missing_as_csv:
                if missing:
                    self.write_missing_csv(missing, playlist.name, data_dir=data_dir)
                else:
                    self.delete_missing_csv(playlist.name, data_dir=data_dir)

            return SyncResult(
                playlist_name=playlist.name,
                total_tracks=len(playlist.tracks),
                matched_tracks=len(matched),
                missing_tracks=len(missing),
                success=True,
            )
        except Exception as e:
            logger.error("Error creating/updating playlist '%s': %s", playlist.name, safe_exc(e))
            logger.debug("Playlist create/update traceback", exc_info=True)
            return SyncResult(
                playlist_name=playlist.name,
                total_tracks=len(playlist.tracks),
                matched_tracks=len(matched),
                missing_tracks=len(missing),
                success=False,
                error=safe_exc(e),
            )

    def sync_playlist_to_users(
        self,
        playlist: Playlist,
        target_usernames: List[str],
        append: bool = False,
        add_description: bool = True,
        add_poster: bool = True,
        write_missing_as_csv: bool = False,
        data_dir: str = "/data",
        threshold: float = 0.9,
        db: Optional[Any] = None,
        skip_rating_keys: Optional[set] = None,
    ) -> List[SyncResult]:
        """Synchronize a playlist across multiple Plex user profiles.

        Matches tracks once against the server library.
        For each target user:
          - If admin username, syncs to admin.
          - If managed/home user, uses user_server = self.server.switchUser(username)
            and creates/updates playlist in that profile.
          - Injects poster using admin server session if user upload fails (bypassing
            managed user 401 permission bug via admin /library/metadata/<ratingKey>/posters?url=...).
        """
        if not target_usernames:
            logger.info("No target users specified for playlist '%s'", playlist.name)
            return []

        logger.info(
            "Syncing playlist '%s' (%d tracks) to %d user(s): %s",
            playlist.name,
            len(playlist.tracks),
            len(target_usernames),
            target_usernames,
        )

        matched, missing = self.match_playlist_tracks(playlist.tracks, threshold=threshold, db=db)

        if not matched:
            logger.warning(
                "No tracks in playlist '%s' could be matched in Plex library",
                playlist.name,
            )
            if write_missing_as_csv and missing:
                self.write_missing_csv(missing, playlist.name, data_dir=data_dir)

            return [
                SyncResult(
                    playlist_name=playlist.name,
                    total_tracks=len(playlist.tracks),
                    matched_tracks=0,
                    missing_tracks=len(missing),
                    success=False,
                    error="Zero tracks matched in Plex library",
                )
                for _ in target_usernames
            ]

        if write_missing_as_csv:
            if missing:
                self.write_missing_csv(missing, playlist.name, data_dir=data_dir)
            else:
                self.delete_missing_csv(playlist.name, data_dir=data_dir)

        admin_username = self._get_admin_username()

        results: List[SyncResult] = []
        for username in target_usernames:
            try:
                user_server = self.get_user_server(username, admin_username=admin_username)
                if db is not None:
                    self.refresh_playlist_registry(user_server, username, db)

                self.update_or_create_playlist(
                    name=playlist.name,
                    tracks=matched,
                    description=playlist.description,
                    poster_url=playlist.poster,
                    append=append,
                    add_description=add_description,
                    add_poster=add_poster,
                    server=user_server,
                    admin_server=self.server,
                    db=db,
                    username=username,
                    skip_rating_keys=skip_rating_keys,
                )
                results.append(
                    SyncResult(
                        playlist_name=playlist.name,
                        total_tracks=len(playlist.tracks),
                        matched_tracks=len(matched),
                        missing_tracks=len(missing),
                        success=True,
                    )
                )
            except PlaylistProtectedError as e:
                logger.warning("Playlist '%s' not synced to '%s': %s", playlist.name, username, redact_text(str(e)))
                results.append(
                    SyncResult(
                        playlist_name=playlist.name,
                        total_tracks=len(playlist.tracks),
                        matched_tracks=len(matched),
                        missing_tracks=len(missing),
                        success=False,
                        error=redact_text(str(e)),
                    )
                )
            except Exception as e:
                logger.error("Failed to sync playlist '%s' to user '%s': %s", playlist.name, username, safe_exc(e))
                logger.debug("Playlist user-sync traceback", exc_info=True)
                results.append(
                    SyncResult(
                        playlist_name=playlist.name,
                        total_tracks=len(playlist.tracks),
                        matched_tracks=len(matched),
                        missing_tracks=len(missing),
                        success=False,
                        error=f"User {username}: {safe_exc(e)}",
                    )
                )

        return results

    # -------------------------------------------------------------------------
    # Plex Playlist Control: identity helpers
    # -------------------------------------------------------------------------

    def _get_admin_username(self) -> str:
        """Return the Plex admin account username, or '' when it cannot be determined."""
        try:
            account = self.server.myPlexAccount()
            return str(getattr(account, "username", "") or "")
        except Exception as e:  # myPlexAccount raises assorted plexapi/requests/Unauthorized errors
            logger.warning("Could not determine admin username from myPlexAccount: %s", safe_exc(e))
            return ""

    def is_admin_username(self, username: str, admin_username: Optional[str] = None) -> bool:
        """True when ``username`` is the Plex admin account (case-insensitive)."""
        admin_name = self._get_admin_username() if admin_username is None else admin_username
        if admin_name and username.lower() == admin_name.lower():
            return True
        if not admin_name and username.lower() in (
            "admin",
            str(getattr(self.server, "friendlyName", "") or "").lower(),
        ):
            return True
        return False

    def get_user_server(self, username: str, admin_username: Optional[str] = None) -> Any:
        """Resolve a Plex username to a server handle: admin -> self.server, else switchUser."""
        if self.is_admin_username(username, admin_username):
            return self.server
        return self.server.switchUser(username)

    # -------------------------------------------------------------------------
    # Plex Playlist Control: playlists
    # -------------------------------------------------------------------------

    @staticmethod
    def list_audio_playlists(server: Any) -> List[Any]:
        """List audio playlists (regular and smart) visible to the given server handle."""
        playlists = server.playlists(playlistType="audio")
        return [p for p in playlists if getattr(p, "playlistType", "audio") == "audio"]

    @staticmethod
    def get_playlist(server: Any, rating_key: str) -> Any:
        """Fetch one audio playlist by ratingKey. Raises NotFound when absent or not an audio playlist."""
        try:
            playlist = server.fetchItem(f"/playlists/{int(rating_key)}")
        except ValueError as e:
            raise NotFound(f"Invalid playlist key '{rating_key}'") from e
        if getattr(playlist, "playlistType", "audio") != "audio":
            raise NotFound(f"Playlist {rating_key} is not an audio playlist")
        return playlist

    @staticmethod
    def get_playlist_items(playlist: Any) -> List[Any]:
        return list(playlist.items())

    @staticmethod
    def rename_playlist(playlist: Any, title: str) -> None:
        playlist.edit(title=title)

    @staticmethod
    def delete_playlist(playlist: Any) -> None:
        playlist.delete()

    @staticmethod
    def find_playlist_item(playlist: Any, playlist_item_id: int) -> Any:
        """Locate an item by its playlistItemID. Raises NotFound when absent."""
        for item in playlist.items():
            try:
                if int(getattr(item, "playlistItemID", -1)) == int(playlist_item_id):
                    return item
            except (TypeError, ValueError):
                continue
        raise NotFound(f"Playlist item {playlist_item_id} not found")

    def add_tracks_to_playlist(self, server: Any, playlist: Any, track_rating_keys: List[str]) -> None:
        """Append library tracks (by ratingKey) to a regular playlist."""
        tracks = []
        for key in track_rating_keys:
            item = server.fetchItem(int(key))
            if getattr(item, "type", None) != "track":
                raise ValueError(f"Item {key} is not a track")
            tracks.append(item)
        if tracks:
            playlist.addItems(tracks)

    def remove_playlist_item(self, playlist: Any, playlist_item_id: int) -> None:
        playlist.removeItems([self.find_playlist_item(playlist, playlist_item_id)])

    def move_playlist_item(
        self, playlist: Any, playlist_item_id: int, after_playlist_item_id: Optional[int]
    ) -> None:
        """Move an item after another item, or to the top when ``after_playlist_item_id`` is None."""
        item = self.find_playlist_item(playlist, playlist_item_id)
        if after_playlist_item_id is None:
            playlist.moveItem(item)
        else:
            after_item = self.find_playlist_item(playlist, after_playlist_item_id)
            playlist.moveItem(item, after=after_item)

    def refresh_playlist_registry(self, server: Any, username: str, db: Any) -> List[Tuple[Any, dict]]:
        """Sync the registry with the user's audio playlists; classify first-seen ones, prune vanished ones.

        Returns [(plex_playlist, registry_row), ...].
        """
        who = username.lower()
        out: List[Tuple[Any, dict]] = []
        keys: List[str] = []
        for pl in self.list_audio_playlists(server):
            key = str(getattr(pl, "ratingKey", ""))
            title = str(getattr(pl, "title", "") or "")
            smart = is_smart_playlist(pl)
            kind = "smart" if smart else "regular"
            keys.append(key)
            existing = db.get_plex_registry_row(who, key)
            if existing is None:
                owner = classify_playlist_owner(db, who, title, smart, added_at=getattr(pl, "addedAt", None))
                row = db.upsert_plex_registry(who, key, title, kind, owner)
            else:
                owner = "plexamp" if smart else existing["owner"]
                row = db.upsert_plex_registry(who, key, title, kind, owner)
            out.append((pl, row))
        db.prune_plex_registry(who, keys)
        return out

    def copy_playlist_to_user(
        self,
        source_items: List[Any],
        title: str,
        target_username: str,
        db: Any,
        description: str = "",
    ) -> Tuple[Any, int, int]:
        """Create a static copy of ``source_items`` in the target user's profile (protected path).

        Returns ``(playlist, copied_tracks, omitted_tracks)``; omitted tracks are not visible in the target library
        or have an unusable ratingKey. Raises PlaylistProtectedError on a name collision with a non-TrackSeerr playlist.
        """
        admin_username = self._get_admin_username()
        target_server = self.get_user_server(target_username, admin_username=admin_username)
        # Make sure collisions are classified before the protected write.
        self.refresh_playlist_registry(target_server, target_username, db)
        if target_server is self.server:
            tracks = list(source_items)
        else:
            tracks = []
            for item in source_items:
                item_title = str(getattr(item, "title", "") or "")
                try:
                    tracks.append(target_server.fetchItem(int(item.ratingKey)))
                except NotFound:
                    logger.warning(
                        "Skipping track '%s' (%s) for %s: not visible in the target library",
                        item_title, getattr(item, "ratingKey", "?"), target_username,
                    )
                except (ValueError, TypeError) as e:
                    logger.warning(
                        "Skipping track '%s' (%s) for %s: invalid ratingKey (%s)",
                        item_title, getattr(item, "ratingKey", "?"), target_username, e,
                    )
        omitted = len(source_items) - len(tracks)
        if not tracks:
            raise NotFound("No copyable tracks found in the target profile")
        playlist = self.update_or_create_playlist(
            name=title,
            tracks=tracks,
            description=description,
            add_description=False,
            add_poster=False,
            server=target_server,
            admin_server=self.server,
            db=db,
            username=target_username,
        )
        return playlist, len(tracks), omitted

    # -------------------------------------------------------------------------
    # Plex Playlist Control: Plexamp mixes
    # -------------------------------------------------------------------------

    def _iter_mix_items(self, server: Any) -> List[Tuple[Any, Any]]:
        """Return [(hub, hub_item)] for every 'mix' hub across music sections. Never raises on Plex errors."""
        pairs: List[Tuple[Any, Any]] = []
        try:
            sections = [s for s in server.library.sections() if getattr(s, "type", "") == "artist"]
        except PLEX_ERRORS as e:
            logger.warning("Could not list Plex library sections for mixes: %s", safe_exc(e))
            return pairs
        for section in sections:
            try:
                hubs = section.hubs()
            except PLEX_ERRORS as e:
                logger.warning("Could not load hubs for section '%s': %s", getattr(section, "title", "?"), safe_exc(e))
                continue
            for hub in hubs:
                ident = str(getattr(hub, "hubIdentifier", "") or "").lower()
                hub_title = str(getattr(hub, "title", "") or "").lower()
                if "mix" not in ident and "mix" not in hub_title:
                    continue
                try:
                    hub_items = list(getattr(hub, "items", []) or [])
                except PLEX_ERRORS as e:
                    logger.warning("Could not read items of hub '%s': %s", hub_title, safe_exc(e))
                    continue
                for item in hub_items:
                    pairs.append((hub, item))
        return pairs

    def get_user_mixes(self, username: str, server: Optional[Any] = None) -> List[dict]:
        """List Plexamp mixes (read-only recommendation hubs) for a user. Returns [] on Plex failure."""
        try:
            srv = server if server is not None else self.get_user_server(username)
        except PLEX_ERRORS as e:
            logger.warning("Could not resolve Plex server for '%s' while listing mixes: %s", username, safe_exc(e))
            return []
        mixes: List[dict] = []
        seen: set = set()
        for hub, item in self._iter_mix_items(srv):
            key = str(getattr(item, "key", "") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            count = getattr(item, "leafCount", None)
            mixes.append(
                {
                    "mix_key": key,
                    "title": str(getattr(item, "title", "") or ""),
                    "hub_title": str(getattr(hub, "title", "") or ""),
                    "track_count": int(count) if isinstance(count, int) else None,
                    "thumb_url": None,
                }
            )
        return mixes

    def get_mix_tracks(self, username: str, mix_key: str, server: Optional[Any] = None) -> List[Any]:
        """Resolve a mix by key and return its track objects. Raises MixNotFoundError when unavailable."""
        try:
            srv = server if server is not None else self.get_user_server(username)
        except PLEX_ERRORS as e:
            raise MixNotFoundError(f"Could not open Plex profile for '{username}': {safe_exc(e)}") from e
        for _hub, item in self._iter_mix_items(srv):
            if str(getattr(item, "key", "") or "") != mix_key:
                continue
            try:
                items_fn = getattr(item, "items", None)
                if callable(items_fn):
                    tracks = list(items_fn())
                else:
                    tracks = list(srv.fetchItems(item.key))
            except PLEX_ERRORS as e:
                raise MixNotFoundError(f"Could not load tracks for mix '{mix_key}': {safe_exc(e)}") from e
            tracks = [t for t in tracks if getattr(t, "type", "track") == "track"]
            if not tracks:
                raise MixNotFoundError(f"Mix '{mix_key}' has no tracks")
            return tracks
        raise MixNotFoundError(f"Mix '{mix_key}' not found for '{username}'")

    def save_mix_as_playlist(
        self, db: Any, username: str, mix_key: str, playlist_title: str, server: Optional[Any] = None
    ) -> Any:
        """Create or replace a regular playlist from a mix through the protected write path."""
        srv = server if server is not None else self.get_user_server(username)
        tracks = self.get_mix_tracks(username, mix_key, server=srv)
        self.refresh_playlist_registry(srv, username, db)
        return self.update_or_create_playlist(
            name=playlist_title,
            tracks=tracks,
            add_description=False,
            add_poster=False,
            server=srv,
            admin_server=self.server,
            db=db,
            username=username,
        )

    def refresh_auto_mix_snapshots(self, db: Any) -> int:
        """Re-resolve every auto_refresh snapshot. Missing mixes leave the playlist alone. Returns count refreshed."""
        refreshed = 0
        for snap in db.list_mix_snapshots(auto_refresh_only=True):
            try:
                pl = self.save_mix_as_playlist(
                    db, snap["plex_user"], snap["mix_key"], snap["playlist_title"]
                )
                db.mark_mix_snapshot_refreshed(snap["id"], str(getattr(pl, "ratingKey", "") or "") or None)
                refreshed += 1
            except MixNotFoundError as e:
                logger.warning("Mix snapshot '%s' not refreshed: %s", snap["playlist_title"], redact_text(str(e)))
            except PlaylistProtectedError as e:
                logger.warning("Mix snapshot '%s' not refreshed: %s", snap["playlist_title"], redact_text(str(e)))
            except PLEX_ERRORS as e:
                logger.warning("Mix snapshot '%s' refresh failed: %s", snap["playlist_title"], safe_exc(e))
        return refreshed

    def search_library_tracks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search tracks in Plex music library for manual matching."""
        if not query or not query.strip():
            return []
        clean_query = query.strip()
        try:
            results = self.server.search(clean_query, mediatype="track", limit=limit)
            tracks_out = []
            for item in results:
                artist_name = ""
                try:
                    cand_artist = item.artist()
                    if cand_artist:
                        artist_name = getattr(cand_artist, "title", "")
                except Exception:
                    artist_name = getattr(item, "grandparentTitle", "") or getattr(item, "originalTitle", "")

                album_name = ""
                try:
                    cand_album = item.album()
                    if cand_album:
                        album_name = getattr(cand_album, "title", "")
                except Exception:
                    album_name = getattr(item, "parentTitle", "")

                tracks_out.append(
                    {
                        "rating_key": str(getattr(item, "ratingKey", "")),
                        "title": getattr(item, "title", "Unknown"),
                        "artist": artist_name or "Unknown Artist",
                        "album": album_name or "",
                        "duration": getattr(item, "duration", 0),
                        "thumb": getattr(item, "thumb", ""),
                    }
                )
            return tracks_out
        except Exception as e:
            logger.error("Error searching Plex library tracks for '%s': %s", clean_query, safe_exc(e))
            return []

    def get_smart_mix_tracks(self, mix_type: str, limit: int = 50) -> list[dict[str, Any]]:
        """Extract smart mix track recommendations based on local Plex library statistics.

        Supported mix types:
        - 'heavy_rotation': Top played tracks
        - 'forgotten_favorites': High-rated / frequently-played tracks not listened to in 6+ months
        - 'deep_cuts': Unplayed tracks from your top artists
        """
        try:
            sections = getattr(self.server.library, "sections", lambda: [])()
            music_sections = [s for s in sections if getattr(s, "type", "") == "artist"]
            if music_sections:
                music_section = music_sections[0]
            else:
                music_section = self.server.library.section("Music")
        except Exception as e:
            logger.warning("Could not access music library section: %s", safe_exc(e))
            return []

        out: list[dict[str, Any]] = []

        try:
            if mix_type == "heavy_rotation":
                tracks = music_section.searchTracks(sort="viewCount:desc", limit=limit)
                for t in tracks:
                    if getattr(t, "viewCount", 0) and getattr(t, "viewCount", 0) > 0:
                        out.append(self._format_track_item(t))

            elif mix_type == "forgotten_favorites":
                import datetime

                cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=180)
                tracks = music_section.searchTracks(sort="lastViewedAt:asc", limit=limit * 3)
                for t in tracks:
                    last_played = getattr(t, "lastViewedAt", None)
                    views = getattr(t, "viewCount", 0) or 0
                    rating = getattr(t, "userRating", 0.0) or 0.0
                    if (views >= 3 or rating >= 7.0) and (
                        last_played is None or last_played.replace(tzinfo=datetime.timezone.utc) < cutoff
                    ):
                        out.append(self._format_track_item(t))
                        if len(out) >= limit:
                            break

            elif mix_type == "deep_cuts":
                artists = music_section.search(mediatype="artist", sort="viewCount:desc", limit=15)
                for a in artists:
                    try:
                        unplayed = a.tracks(filters={"viewCount": 0})
                        for ut in unplayed[:4]:
                            out.append(self._format_track_item(ut))
                            if len(out) >= limit:
                                break
                    except Exception:
                        continue
                    if len(out) >= limit:
                        break

            else:
                logger.warning("Unknown smart mix type: %s", mix_type)
        except Exception as e:
            logger.error("Error generating smart mix '%s': %s", mix_type, safe_exc(e))

        return out

    def _format_track_item(self, t: Any) -> dict[str, Any]:
        artist_name = getattr(t, "grandparentTitle", "") or getattr(t, "originalTitle", "")
        if not artist_name:
            try:
                a = t.artist()
                if a:
                    artist_name = getattr(a, "title", "")
            except Exception:
                pass

        album_name = getattr(t, "parentTitle", "")
        if not album_name:
            try:
                al = t.album()
                if al:
                    album_name = getattr(al, "title", "")
            except Exception:
                pass

        return {
            "rating_key": str(getattr(t, "ratingKey", "")),
            "title": getattr(t, "title", "Unknown"),
            "artist": artist_name or "Unknown Artist",
            "album": album_name or "",
            "view_count": getattr(t, "viewCount", 0) or 0,
            "last_viewed_at": str(getattr(t, "lastViewedAt", "")) if getattr(t, "lastViewedAt", None) else None,
        }

    def refresh_music_library(self, section_name: Optional[str] = None) -> bool:
        """Triggers a library section refresh on Plex Media Server."""
        sec_name = section_name or getattr(self, "music_section", "Music") or "Music"
        try:
            if hasattr(self.server, "library"):
                try:
                    sec = self.server.library.section(sec_name)
                    sec.update()
                    logger.info("Triggered Plex section update for '%s'", sec_name)
                    return True
                except Exception:
                    self.server.library.update()
                    logger.info("Triggered general Plex library update")
                    return True
        except Exception as e:
            logger.warning("Could not refresh Plex library '%s': %s", sec_name, safe_exc(e))
        return False

    def test_connection(self) -> tuple[bool, str]:
        """Tests connectivity and responsiveness of the Plex Media Server."""
        try:
            if hasattr(self.server, "query"):
                self.server.query("/", timeout=4.0)
            friendly_name = getattr(self.server, "friendlyName", "Plex Server")
            version = getattr(self.server, "version", "unknown")
            return True, f"Connected to {friendly_name} (v{version})"
        except Exception as e:
            logger.warning("Plex connection test failed: %s", safe_exc(e))
            return False, safe_exc(e)


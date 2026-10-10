"""Playlist management routes with SSRF protection, targeting, and direct track imports."""

import hashlib
import json
import logging
import threading
from typing import Any, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator

from trackseerr.api.dependencies import (
    get_config,
    get_current_user,
    get_db,
    get_deezer_client,
    get_media_client,
    require_media_server,
    get_spotify_client,
    has_permission,
)
from trackseerr.api.schemas.playlists import (
    FeaturedChart,
    ListeningSourcesResponse,
    PlaylistAutoRequestResponse,
    PlaylistDeletedResponse,
    PlaylistEnabledResponse,
    PlaylistImportResponse,
    PlaylistMonitorModeResponse,
    PlaylistRecord,
    PlaylistTargetsResponse,
    SmartMixPreset,
)
from trackseerr.clients.deezer import DeezerClient
from trackseerr.clients.import_lists import listenbrainz as lb_provider
from trackseerr.clients.import_lists.base import ImportListError
from trackseerr.clients.plex import PlexClient
from trackseerr.media_servers import PlaylistSyncOptions, as_media_server, describe_error, plex_extras
from trackseerr.clients.spotify import SpotifyClient
from trackseerr.clients.spotify_scraper import SpotifyWebScraper
from trackseerr.config import MEDIA_SERVER_NONE, Config
from trackseerr.native_match import match_playlist_tracks_native
from trackseerr.library_monitoring import validate_list_monitor_mode
from trackseerr.list_monitoring import apply_playlist_missing_safely
from trackseerr.m3u import parse_m3u
from trackseerr.listening_playlists import (
    PROVIDER_LISTENBRAINZ,
    KIND_LB_CREATED_FOR,
    ListeningSourceError,
    describe_source,
    fetch_listening_tracks,
    list_sources,
    playlist_id_for,
    resolve_created_for_slug,
    validate_source,
)
from trackseerr.models import Playlist, Track, UserPermission
from trackseerr.playlist_policy import initial_monitor_mode_forced, is_listening_playlist, is_smart_collection
from trackseerr.redaction import safe_exc
from trackseerr.security import (
    extract_deezer_id,
    extract_spotify_id,
    is_safe_image_url,
    sanitize_text,
)
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()

# Album and artist modes add to the library with no quota or approval, so only admins may pick them. Track mode
# searches every missing track on its own, so it needs admin or the AUTO_REQUEST_PLAYLISTS permission.
NON_ADMIN_MONITOR_MODES = ("track", "none")
LISTENING_MODE_DETAIL = "Listening playlists only list missing tracks; use auto-request instead"


def _require_mode_allowed(current_user: dict[str, Any], mode: Optional[str]) -> None:
    if mode is None or mode == "none" or current_user.get("is_admin"):
        return
    if mode not in NON_ADMIN_MONITOR_MODES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can set an album or artist monitor mode",
        )
    if not has_permission(current_user, UserPermission.AUTO_REQUEST_PLAYLISTS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You need the auto-request playlist tracks permission to acquire missing tracks automatically",
        )


def _require_auto_request_allowed(current_user: dict[str, Any]) -> None:
    if not has_permission(current_user, UserPermission.AUTO_REQUEST_PLAYLISTS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You need the auto-request playlist tracks permission to request missing tracks automatically",
        )


def _reject_listening_mode(playlist: dict[str, Any], mode: Optional[str]) -> None:
    if mode not in (None, "none"):
        if is_smart_collection(playlist):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Smart collections only list tracks already in your library",
            )
        if is_listening_playlist(playlist):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=LISTENING_MODE_DETAIL)


def _force_list_only_if_not_allowed(db: Database, playlist_id: str, current_user: dict[str, Any]) -> None:
    """A new playlist of a user who may not auto-request is list-only whatever the column default is."""
    forced = initial_monitor_mode_forced(current_user)
    if forced is not None:
        db.set_playlist_monitor_mode(playlist_id, forced)


def _apply_missing_in_background(db: Database, config: Config, playlist_id: str) -> None:
    """Runs the monitor-mode apply (MusicBrainz lookups, Lidarr waits) off the request thread."""
    threading.Thread(
        target=apply_playlist_missing_safely,
        args=(db, config, playlist_id),
        name=f"playlist-monitor-{playlist_id}",
        daemon=True,
    ).start()


class PlaylistCreateRequest(BaseModel):
    url_or_id: str = Field(..., description="Spotify/Deezer URL, URI, or alphanumeric/numeric ID")
    service: Optional[str] = Field(default=None, description="Optional service hint: 'spotify' or 'deezer'")
    targets: Optional[list[str]] = Field(default=None, description="Optional list of target user IDs")


class TrackImportItem(BaseModel):
    title: str = Field(..., description="Track title")
    artist: Optional[str] = Field(default="", description="Artist name")
    album: Optional[str] = Field(default="", description="Album name")
    uri: Optional[str] = Field(default="", description="Optional Spotify/Deezer URI or URL")


class PlaylistDirectImportRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="Playlist name")
    service: Optional[str] = Field(default="spotify", description="Service tag: spotify, deezer, or custom")
    description: Optional[str] = Field(default="", max_length=1000, description="Playlist description")
    poster_url: Optional[str] = Field(default="", description="Cover artwork URL")
    tracks: list[TrackImportItem] = Field(..., min_length=1, max_length=2000, description="List of tracks to import")
    targets: Optional[list[str]] = Field(default=None, description="Optional target user IDs")


class PlaylistTargetsRequest(BaseModel):
    user_ids: list[str] = Field(..., description="List of target user IDs for this playlist")


class PlaylistEnabledRequest(BaseModel):
    """Playlist settings update: ``enabled`` and/or ``monitor_mode`` (at least one)."""

    enabled: Optional[bool] = Field(
        default=None, description="True if playlist should be auto-synced; False if paused/static"
    )
    monitor_mode: Optional[str] = Field(
        default=None, description="What a sync does with missing tracks: track, album, artist or none"
    )

    @field_validator("monitor_mode")
    @classmethod
    def _valid_mode(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_list_monitor_mode(value)

    @model_validator(mode="after")
    def _something_to_change(self) -> "PlaylistEnabledRequest":
        if self.enabled is None and self.monitor_mode is None:
            raise ValueError("Provide enabled and/or monitor_mode")
        return self


class PlaylistMonitorModeRequest(BaseModel):
    monitor_mode: str = Field(..., description="track, album, artist or none")

    @field_validator("monitor_mode")
    @classmethod
    def _valid_mode(cls, value: str) -> str:
        return validate_list_monitor_mode(value)


class ListeningPlaylistCreateRequest(BaseModel):
    provider: Literal["lastfm", "listenbrainz"] = Field(..., description="Linked scrobbling account to read")
    kind: str = Field(
        ...,
        description="Last.fm: loved or top_tracks. ListenBrainz: playlist or created_for",
    )
    ref: str = Field(
        default="",
        max_length=200,
        description="Last.fm top_tracks period, or the ListenBrainz playlist id (for created_for: that playlist or its kind)",
    )
    keep_in_sync: bool = Field(default=True, description="Refresh the playlist on every sync cycle")
    auto_request: bool = Field(
        default=False, description="Request missing tracks automatically (admin or auto-request permission)"
    )


class PlaylistAutoRequestRequest(BaseModel):
    auto_request: bool


class SmartMixRequest(BaseModel):
    mix_type: str = Field(..., description="One of: 'heavy_rotation', 'forgotten_favorites', 'deep_cuts'")
    name: Optional[str] = Field(default=None, max_length=200, description="Custom playlist name")
    targets: Optional[list[str]] = Field(default=None, description="Optional target user IDs")


class M3UImportRequest(BaseModel):
    content: str = Field(..., min_length=1, description="Raw M3U/M3U8 file contents")
    name: Optional[str] = Field(default="Imported M3U Playlist", max_length=200, description="Playlist title")
    description: Optional[str] = Field(default="Imported from M3U playlist file", max_length=1000)
    targets: Optional[list[str]] = Field(default=None, description="Optional target user IDs")


FEATURED_CHARTS = [
    {
        "id": "chart-billboard-hot-100",
        "name": "Billboard Hot 100",
        "service": "spotify",
        "url_or_id": "6UeSakyzhiEt4NB3UAd6NQ",
        "description": "The weekly definitive list of the most popular songs in the United States across all genres.",
        "poster_url": "https://image-cdn-ak.spotifycdn.com/image/ab67706c0000bebb8d0e513812822a1b9448a379",
        "category": "Charts",
    },
    {
        "id": "chart-todays-top-hits",
        "name": "Today's Top Hits",
        "service": "spotify",
        "url_or_id": "37i9dQZF1DXcBWIGoYBM5M",
        "description": "The hottest 50 tracks in the world right now, updated weekly.",
        "poster_url": "https://i.scdn.co/image/ab67706f00000002161f30141f173c35b80a5a3a",
        "category": "Charts",
    },
    {
        "id": "chart-global-viral-50",
        "name": "Viral 50 - Global",
        "service": "spotify",
        "url_or_id": "37i9dQZF1DX2L0iB23Enbq",
        "description": "The most viral tracks across social media and streaming worldwide.",
        "poster_url": "https://i.scdn.co/image/ab67706f00000002c98d697855e347ad68b209c1",
        "category": "Trending",
    },
    {
        "id": "chart-rock-classics",
        "name": "Rock Classics",
        "service": "spotify",
        "url_or_id": "37i9dQZF1DWXRqgorJj26U",
        "description": "Iconic rock anthems and timeless guitar legends through the decades.",
        "poster_url": "https://i.scdn.co/image/ab67706f0000000278b4745cb9ce8ffe32da91f9",
        "category": "Classics",
    },
    {
        "id": "chart-chill-hits",
        "name": "Chill Hits",
        "service": "spotify",
        "url_or_id": "37i9dQZF1DX4WYpdgoIcn6",
        "description": "Kick back with the best relaxed pop, acoustic, and downtempo melodies.",
        "poster_url": "https://i.scdn.co/image/ab67706f000000029bbd352b22bbec2412702581",
        "category": "Mood",
    },
    {
        "id": "chart-deezer-top-worldwide",
        "name": "Deezer Top Worldwide",
        "service": "deezer",
        "url_or_id": "3155776842",
        "description": "The top streamed music tracks globally on Deezer.",
        "poster_url": "https://e-cdns-images.dzcdn.net/images/cover/9082ebca4314c1d76378e9b049d53c73/500x500-000000-80-0-0.jpg",
        "category": "Charts",
    },
]

SMART_MIX_PRESETS = [
    {
        "mix_type": "heavy_rotation",
        "name": "Heavy Rotation",
        "description": "Your most played tracks on Plexamp over recent weeks.",
        "icon": "fire",
    },
    {
        "mix_type": "forgotten_favorites",
        "name": "Forgotten Favorites",
        "description": "Loved and heavily played songs you haven't listened to in the last 6 months.",
        "icon": "clock",
    },
    {
        "mix_type": "deep_cuts",
        "name": "Deep Cuts",
        "description": "Rare and unplayed hidden gems from your favorite library artists.",
        "icon": "sparkles",
    },
]


def _guard_existing_playlist(
    db: Database, playlist_id: str, current_user: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Returns the existing playlist row (or None). Non-admins touching another user's row get 404."""
    existing = db.get_playlist(playlist_id)
    if existing is None:
        return None
    if current_user.get("is_admin"):
        return existing
    owner = existing.get("creator_id")
    if owner is None or str(owner) != str(current_user["id"]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")
    return existing


def _resolve_targets(
    db: Database,
    playlist_id: str,
    existing: Optional[dict[str, Any]],
    current_user: dict[str, Any],
    requested: Optional[list[str]],
) -> list[str]:
    """Targets to persist. Non-admins never wipe other users' targets on an existing playlist."""
    uid = str(current_user["id"])
    if current_user.get("is_admin"):
        return list(requested) if requested is not None else [uid]
    if existing is None:
        return [uid]
    current = db.get_playlist_targets(playlist_id)
    return current if uid in current else [*current, uid]


@router.get("", response_model=list[PlaylistRecord], response_model_exclude_unset=True)
def list_playlists(
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    """Lists playlists. Admins see all; regular users see only playlists they created or that target them."""
    if current_user.get("is_admin"):
        playlists = db.list_playlists()
    else:
        playlists = db.list_playlists(user_id=str(current_user["id"]))

    for p in playlists:
        p["targets"] = db.get_playlist_targets(p["id"])

    return playlists


@router.post("", response_model=PlaylistRecord, response_model_exclude_unset=True, status_code=status.HTTP_201_CREATED)
def create_playlist(
    req: PlaylistCreateRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    spotify_client: Optional[Union[SpotifyClient, SpotifyWebScraper]] = Depends(get_spotify_client),
    deezer_client: Optional[DeezerClient] = Depends(get_deezer_client),
) -> dict[str, Any]:
    """Adds playlist via Spotify/Deezer URL or ID. Validates SSRF with extract_spotify_id / extract_deezer_id.

    Extracts metadata and sets initial targets.
    """
    service_hint = req.service.lower().strip() if req.service else None
    pl_id: Optional[str] = None
    service: Optional[str] = None

    if service_hint == "spotify":
        pl_id = extract_spotify_id(req.url_or_id)
        service = "spotify"
    elif service_hint == "deezer":
        pl_id = extract_deezer_id(req.url_or_id)
        service = "deezer"
    else:
        sp_id = extract_spotify_id(req.url_or_id)
        dz_id = extract_deezer_id(req.url_or_id)
        if sp_id:
            pl_id = sp_id
            service = "spotify"
        elif dz_id:
            pl_id = dz_id
            service = "deezer"

    if not pl_id or not service:
        logger.warning("Rejected invalid or malicious playlist input: '%s'", req.url_or_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Spotify or Deezer playlist URL or ID (SSRF validation failed)",
        )

    existing = _guard_existing_playlist(db, pl_id, current_user)

    # Extract metadata using verified client
    title = f"{service.title()} Playlist {pl_id}"
    description = ""
    poster_url = ""

    if service == "spotify" and spotify_client:
        try:
            meta = spotify_client.get_playlist_by_id(pl_id)
            if meta:
                title = meta.name
                description = meta.description
                poster_url = meta.poster
        except Exception as e:
            logger.warning("Could not fetch Spotify metadata for %s: %s", pl_id, e)
    elif service == "deezer" and deezer_client:
        try:
            meta = deezer_client.get_playlist_by_id(pl_id)
            if meta:
                title = meta.name
                description = meta.description
                poster_url = meta.poster
        except Exception as e:
            logger.warning("Could not fetch Deezer metadata for %s: %s", pl_id, e)

    clean_title = sanitize_text(title) or f"{service.title()} Playlist {pl_id}"
    clean_desc = sanitize_text(description)

    # Upsert playlist into DB with creator_id
    creator_id = str(current_user["id"])
    playlist = db.upsert_playlist(
        playlist_id=pl_id,
        name=clean_title,
        service=service,
        description=clean_desc,
        poster_url=poster_url,
        creator_id=creator_id,
    )

    if existing is None:
        _force_list_only_if_not_allowed(db, pl_id, current_user)
        playlist = db.get_playlist(pl_id) or playlist
    initial_targets = _resolve_targets(db, pl_id, existing, current_user, req.targets)
    db.set_playlist_targets(pl_id, initial_targets)
    playlist["targets"] = db.get_playlist_targets(pl_id)

    return playlist


@router.get("/listening/sources", response_model=ListeningSourcesResponse, response_model_exclude_unset=True)
def get_listening_sources(
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """What the current user can build a playlist from: their own linked Last.fm / ListenBrainz accounts only."""
    sources = list_sources(db, config, str(current_user["id"]))
    sources["can_auto_request"] = has_permission(current_user, UserPermission.AUTO_REQUEST_PLAYLISTS)
    return sources


@router.post("/listening", response_model=PlaylistImportResponse, response_model_exclude_unset=True, status_code=status.HTTP_201_CREATED)
def create_listening_playlist(
    req: ListeningPlaylistCreateRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[Any] = Depends(get_media_client),
) -> dict[str, Any]:
    """Creates a playlist from the current user's own Last.fm or ListenBrainz listening.

    Missing tracks are listed only. ``auto_request`` opts into requesting them and needs admin or the
    auto-request permission. The owner is also the only target, and the monitor mode is always ``none``.
    """
    if req.auto_request:
        _require_auto_request_allowed(current_user)
    user_id = str(current_user["id"])
    try:
        kind, ref = validate_source(req.provider, req.kind, req.ref)
        title: Optional[str] = None
        if req.provider == PROVIDER_LISTENBRAINZ and kind == KIND_LB_CREATED_FOR:
            ref, title = resolve_created_for_slug(db, user_id, ref)
        playlist_id = playlist_id_for(user_id, req.provider, kind, ref)
        draft = {
            "creator_id": user_id,
            "service": req.provider,
            "source_kind": kind,
            "source_ref": ref,
        }
        tracks = fetch_listening_tracks(db, config, draft)
    except ListeningSourceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if req.provider == PROVIDER_LISTENBRAINZ and kind != KIND_LB_CREATED_FOR:
        title = _own_playlist_title(db, user_id, ref)
    name = sanitize_text(describe_source(req.provider, kind, ref, title))
    _guard_existing_playlist(db, playlist_id, current_user)
    tracks_json = json.dumps([{"title": t.title, "artist": t.artist, "album": t.album} for t in tracks])
    db.upsert_playlist(
        playlist_id=playlist_id,
        name=name,
        service=req.provider,
        description="Built from your listening history",
        enabled=req.keep_in_sync,
        creator_id=user_id,
        tracks_json=tracks_json,
    )
    db.set_playlist_source(playlist_id, kind, ref)
    db.set_playlist_monitor_mode(playlist_id, "none")
    db.set_playlist_auto_request(playlist_id, req.auto_request)
    targets = [user_id]
    db.set_playlist_targets(playlist_id, targets)

    matched_count, missing_count = _sync_stored_playlist(
        db, config, plex_client, playlist_id, name, "Built from your listening history", "", tracks, targets
    )
    return {
        "id": playlist_id,
        "name": name,
        "service": req.provider,
        "track_count": len(tracks),
        "matched_count": matched_count,
        "missing_count": missing_count,
        "targets": targets,
        "status": "created",
    }


def _own_playlist_title(db: Database, user_id: str, mbid: str) -> Optional[str]:
    """Title of one of the user's own ListenBrainz playlists, or None when it is someone else's or unreachable."""
    cfg = db.get_scrobble_config(user_id) or {}
    username = str(cfg.get("listenbrainz_username") or "").strip()
    if not username:
        return None
    try:
        for entry in lb_provider.list_user_playlists(username, {"token": str(cfg.get("listenbrainz_token") or "")}):
            if entry["mbid"] == mbid:
                return entry["title"] or None
    except ImportListError as exc:
        logger.info("Could not look up the title of ListenBrainz playlist %s: %s", mbid, exc)
    return None


@router.put("/{playlist_id}/auto-request", response_model=PlaylistAutoRequestResponse, response_model_exclude_unset=True)
def set_playlist_auto_request(
    playlist_id: str,
    req: PlaylistAutoRequestRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Turns automatic requesting of a listening playlist's missing tracks on or off.

    Turning it on needs admin or the auto-request permission (403 otherwise); turning it off is always allowed.
    """
    playlist = db.get_playlist(playlist_id)
    is_admin = bool(current_user.get("is_admin"))
    is_creator = bool(playlist) and str(playlist.get("creator_id")) == str(current_user["id"])
    if not playlist or not (is_admin or is_creator):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")
    if is_smart_collection(playlist):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Smart collections only list tracks already in your library",
        )
    if not is_listening_playlist(playlist):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only listening playlists can auto-request missing tracks"
        )
    if req.auto_request:
        _require_auto_request_allowed(current_user)
    db.set_playlist_auto_request(playlist_id, req.auto_request)
    return {"id": playlist_id, "auto_request": req.auto_request}


@router.get("/featured", response_model=list[FeaturedChart], response_model_exclude_unset=True)
def list_featured_charts(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Returns curated popular charts for 1-click subscription."""
    return FEATURED_CHARTS


@router.get("/smart-mix/presets", response_model=list[SmartMixPreset], response_model_exclude_unset=True)
def get_smart_mix_presets(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Returns available local Smart Mix recipes."""
    return SMART_MIX_PRESETS


@router.post("/smart-mix", response_model=PlaylistImportResponse, response_model_exclude_unset=True, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_media_server)])
def create_smart_mix(
    req: SmartMixRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[Any] = Depends(get_media_client),
) -> dict[str, Any]:
    """Generates a smart playlist in Plex from local listening history."""
    server = as_media_server(plex_client)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Plex Media Server client is not configured",
        )
    mix_client = plex_extras(server)
    if not server.capabilities.mixes or mix_client is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Smart mixes are not supported by the connected media server",
        )

    valid_types = {p["mix_type"]: p for p in SMART_MIX_PRESETS}
    if req.mix_type not in valid_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid mix_type '{req.mix_type}'. Must be one of: {list(valid_types.keys())}",
        )

    preset_info = valid_types[req.mix_type]
    pl_name = sanitize_text(req.name or preset_info["name"])

    tracks = mix_client.get_smart_mix_tracks(req.mix_type, limit=50)
    if not tracks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No eligible tracks found in Plex library for '{preset_info['name']}'. Listen to more music in Plexamp to generate this mix!",
        )

    import_items = [
        TrackImportItem(
            title=t["title"],
            artist=t["artist"],
            album=t.get("album", ""),
        )
        for t in tracks
    ]

    import_req = PlaylistDirectImportRequest(
        name=pl_name,
        service="plex",
        description=preset_info["description"],
        tracks=import_items,
        targets=req.targets,
    )
    return import_playlist_tracks(
        req=import_req,
        current_user=current_user,
        db=db,
        config=config,
        plex_client=plex_client,
    )


@router.put("/{playlist_id}/targets", response_model=PlaylistTargetsResponse, response_model_exclude_unset=True)
def update_playlist_targets(
    playlist_id: str,
    req: PlaylistTargetsRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Updates the target user list for a playlist.

    Admins may set any targets. A non-admin may act only on a playlist they created (404
    otherwise, so existence is not revealed) and may only target themselves or nobody (403).
    """
    playlist = db.get_playlist(playlist_id)
    uid = str(current_user["id"])
    is_admin = bool(current_user.get("is_admin"))
    if not playlist or (not is_admin and str(playlist.get("creator_id")) != uid):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Playlist not found",
        )

    if is_admin:
        new_targets = req.user_ids
    else:
        if any(str(target) != uid for target in req.user_ids):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: you may only target yourself",
            )
        new_targets = [uid] if req.user_ids else []

    db.set_playlist_targets(playlist_id, new_targets)
    return {
        "id": playlist_id,
        "targets": db.get_playlist_targets(playlist_id),
    }


@router.put("/{playlist_id}/enabled", response_model=PlaylistEnabledResponse, response_model_exclude_unset=True)
def set_playlist_enabled(
    playlist_id: str,
    req: PlaylistEnabledRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Toggles playlist auto-sync state between Active (enabled) and Paused (disabled)."""
    playlist = db.get_playlist(playlist_id)
    is_admin = bool(current_user.get("is_admin"))
    is_creator = bool(playlist) and str(playlist.get("creator_id")) == str(current_user["id"])
    if not playlist or not (is_admin or is_creator):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Playlist not found",
        )
    _reject_listening_mode(playlist, req.monitor_mode)
    _require_mode_allowed(current_user, req.monitor_mode)

    if req.enabled is not None:
        db.set_playlist_enabled(playlist_id, req.enabled)
    if req.monitor_mode is not None:
        db.set_playlist_monitor_mode(playlist_id, req.monitor_mode)
    updated = db.get_playlist(playlist_id)
    return {
        "id": playlist_id,
        "enabled": bool(updated.get("enabled", True)) if updated else bool(req.enabled),
        "monitor_mode": str(updated.get("monitor_mode") or "track") if updated else req.monitor_mode,
    }


@router.put("/{playlist_id}/monitor-mode", response_model=PlaylistMonitorModeResponse, response_model_exclude_unset=True)
def set_playlist_monitor_mode(
    playlist_id: str,
    req: PlaylistMonitorModeRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Sets what a sync does with the playlist's missing tracks (track, album, artist or none)."""
    playlist = db.get_playlist(playlist_id)
    is_admin = bool(current_user.get("is_admin"))
    is_creator = bool(playlist) and str(playlist.get("creator_id")) == str(current_user["id"])
    if not playlist or not (is_admin or is_creator):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")
    _reject_listening_mode(playlist, req.monitor_mode)
    _require_mode_allowed(current_user, req.monitor_mode)
    db.set_playlist_monitor_mode(playlist_id, req.monitor_mode)
    return {"id": playlist_id, "monitor_mode": req.monitor_mode}


@router.delete("/{playlist_id}", response_model=PlaylistDeletedResponse, response_model_exclude_unset=True)
def delete_playlist(
    playlist_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Deletes a playlist (admin, or the creator; others get 404)."""
    playlist = db.get_playlist(playlist_id)
    is_admin = bool(current_user.get("is_admin"))
    is_creator = bool(playlist) and str(playlist.get("creator_id")) == str(current_user["id"])
    if not playlist or not (is_admin or is_creator):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Playlist not found",
        )

    db.delete_playlist(playlist_id)
    return {"status": "deleted", "id": playlist_id}


def _sync_stored_playlist(
    db: Database,
    config: Config,
    plex_client: Optional[Any],
    playlist_id: str,
    name: str,
    description: str,
    poster_url: str,
    model_tracks: list[Track],
    targets: list[str],
) -> tuple[int, int]:
    """Pushes a just-stored playlist to the media server (or matches it natively) and records its missing tracks.

    Returns ``(matched, missing)``. Failures are logged and recorded on the playlist, never raised.
    """
    matched_count = 0
    missing_count = 0
    server = as_media_server(plex_client)
    if server and model_tracks:
        target_usernames = []
        for uid in targets:
            user_row = db.get_user(uid)
            if user_row:
                target_usernames.append(user_row["username"])

        if target_usernames:
            model_playlist = Playlist(
                id=playlist_id,
                name=name,
                tracks=model_tracks,
                description=description,
                poster=poster_url,
            )
            try:
                results = server.sync_playlist(
                    model_playlist, target_usernames, PlaylistSyncOptions.from_config(config, db=db)
                )
                matched, missing = server.match_playlist_tracks(
                    model_tracks, threshold=config.search_similarity_threshold
                )
                matched_count = len(matched)
                missing_count = len(missing)
                success = any(r.success for r in results) if results else False
                db.record_sync_result(
                    playlist_id,
                    status="success" if (success and not missing) else ("partial" if success else "error"),
                    missing_tracks=missing,
                )
            except Exception as e:
                logger.error("Error during direct import sync to Plex: %s", describe_error(e))
                logger.debug("Direct import sync traceback", exc_info=True)
                db.record_sync_result(playlist_id, status="error")
            else:
                _apply_missing_in_background(db, config, playlist_id)
    elif config.media_server_type == MEDIA_SERVER_NONE and model_tracks:
        # No media server: nothing to push, but the playlist is still matched against the native library so its
        # missing tracks feed monitoring / wanted.
        try:
            matched, missing = match_playlist_tracks_native(db, model_tracks)
            matched_count = len(matched)
            missing_count = len(missing)
            db.record_sync_result(
                playlist_id,
                status="success" if not missing else "partial",
                missing_tracks=missing,
            )
        except Exception as e:  # the playlist is already stored; the root cause is logged
            logger.error("Native library match failed for imported playlist: %s", safe_exc(e))
            logger.debug("Native match traceback", exc_info=True)
            db.record_sync_result(playlist_id, status="error")
        else:
            _apply_missing_in_background(db, config, playlist_id)
    return matched_count, missing_count


@router.post("/import", response_model=PlaylistImportResponse, response_model_exclude_unset=True, status_code=status.HTTP_201_CREATED)
def import_playlist_tracks(
    req: PlaylistDirectImportRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[Any] = Depends(get_media_client),
) -> dict[str, Any]:
    """Imports playlist and tracks directly from browser helpers, clipboard, or bookmarklet."""
    clean_name = sanitize_text(req.name)
    clean_desc = sanitize_text(req.description or "")
    poster_url = req.poster_url.strip() if req.poster_url else ""
    if poster_url and not is_safe_image_url(poster_url):
        logger.warning("Rejected unsafe or SSRF-prone poster URL: %s", poster_url)
        poster_url = ""

    # Generate stable unique ID based on creator and name hash
    hash_seed = f"{current_user['id']}_{clean_name}_{len(req.tracks)}"
    import_id = f"imp_{hashlib.sha256(hash_seed.encode()).hexdigest()[:12]}"

    tracks_data = [
        {"title": t.title, "artist": t.artist or "", "album": t.album or ""}
        for t in req.tracks
        if t.title.strip()
    ]
    tracks_json_str = json.dumps(tracks_data)

    existing = _guard_existing_playlist(db, import_id, current_user)

    db.upsert_playlist(
        playlist_id=import_id,
        name=clean_name,
        service=req.service.lower() if req.service else "spotify",
        description=clean_desc,
        poster_url=poster_url,
        creator_id=str(current_user["id"]),
        tracks_json=tracks_json_str,
    )
    if existing is None:
        _force_list_only_if_not_allowed(db, import_id, current_user)

    # Admins can target anyone; regular users only add themselves (never wiping others' targets)
    if current_user.get("is_admin") and req.targets is not None:
        targets = req.targets
    else:
        targets = _resolve_targets(db, import_id, existing, current_user, None)

    db.set_playlist_targets(import_id, targets)

    # Convert to Track models
    model_tracks = [
        Track(
            title=sanitize_text(t.title),
            artist=sanitize_text(t.artist or ""),
            album=sanitize_text(t.album or ""),
        )
        for t in req.tracks
        if t.title.strip()
    ]

    matched_count, missing_count = _sync_stored_playlist(
        db, config, plex_client, import_id, clean_name, clean_desc, poster_url, model_tracks, targets
    )

    return {
        "id": import_id,
        "name": clean_name,
        "service": req.service,
        "track_count": len(model_tracks),
        "matched_count": matched_count,
        "missing_count": missing_count,
        "targets": targets,
        "status": "imported",
    }


@router.post("/import/m3u", response_model=PlaylistImportResponse, response_model_exclude_unset=True, status_code=status.HTTP_201_CREATED)
def import_m3u_playlist(
    req: M3UImportRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[Any] = Depends(get_media_client),
) -> dict[str, Any]:
    """Imports a playlist from raw M3U / M3U8 file contents."""
    parsed_tracks = parse_m3u(req.content)
    if not parsed_tracks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not extract any valid tracks from the provided M3U content.",
        )

    import_items = []
    for t in parsed_tracks:
        title, artist = t["title"], t.get("artist", "")
        alt = t.get("alt")
        # An ambiguous 'A - B' display name (iTunes writes 'Title - Artist'): when only the swapped reading names
        # an artist the library knows, use that one.
        if alt and not db.get_library_artist_by_name(artist) and db.get_library_artist_by_name(alt["artist"]):
            title, artist = alt["title"], alt["artist"]
        import_items.append(TrackImportItem(title=title, artist=artist, album=t.get("album", "")))

    import_req = PlaylistDirectImportRequest(
        name=req.name or "Imported M3U Playlist",
        service="m3u",
        description=req.description or "Imported from M3U playlist file",
        tracks=import_items,
        targets=req.targets,
    )

    return import_playlist_tracks(
        req=import_req,
        current_user=current_user,
        db=db,
        config=config,
        plex_client=plex_client,
    )


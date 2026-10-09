"""Background sync routes and SSE live log streaming."""

import asyncio
from datetime import datetime, timezone
import json
import logging
import threading
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from trackseerr.api.dependencies import (
    get_config,
    get_db,
    get_deezer_client,
    get_media_client,
    get_spotify_client,
    require_admin,
    verify_feed_access,
)
from trackseerr.api.schemas.sync import SyncStatusResponse, SyncTriggerResponse, SyncWebhookResponse
from trackseerr.clients.deezer import DeezerClient
from trackseerr.clients.plex import PlexClient
from trackseerr.media_servers import MediaServerError, PlaylistSyncOptions, as_media_server, describe_error
from trackseerr.media_servers.plex import read_playlist_items, refresh_mix_snapshots
from trackseerr.clients.spotify import SpotifyClient
from trackseerr.config import MEDIA_SERVER_NONE, Config
from trackseerr.job_tracker import tracked
from trackseerr.list_monitoring import apply_playlist_missing_safely
from trackseerr.listening_playlists import fetch_listening_tracks
from trackseerr.playlist_policy import is_listening_playlist
from trackseerr.models import Playlist, RequestStatus, Track
from trackseerr.native_match import match_playlist_tracks_native
from trackseerr.storage import Database
from trackseerr.redaction import redact_text, safe_exc

logger = logging.getLogger(__name__)

router = APIRouter()


class BroadcastLogHandler(logging.Handler):
    """Logging handler that thread-safely broadcasts formatted log records to active asyncio queues."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.listeners: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []

    def emit(self, record: logging.LogRecord) -> None:
        # This handler sits on the package logger, ahead of the root redaction filters: redact here too.
        msg = redact_text(self.format(record))
        with self._lock:
            active_listeners = list(self.listeners)

        for loop, q in active_listeners:
            try:
                if loop.is_running():
                    loop.call_soon_threadsafe(self._safe_put, q, msg)
            except Exception:
                pass

    @staticmethod
    def _safe_put(q: asyncio.Queue, msg: str) -> None:
        try:
            q.put_nowait(msg)
        except (asyncio.QueueFull, Exception):
            pass

    def add_listener(self, loop: asyncio.AbstractEventLoop, q: asyncio.Queue) -> None:
        with self._lock:
            self.listeners.append((loop, q))

    def remove_listener(self, q: asyncio.Queue) -> None:
        with self._lock:
            self.listeners = [item for item in self.listeners if item[1] is not q]


class SyncState:
    """Manages active sync status, statistics, and log streaming."""

    def __init__(self) -> None:
        self.is_syncing: bool = False
        self.last_run_at: Optional[str] = None
        self.last_run_stats: dict[str, Any] = {
            "total_playlists": 0,
            "success_count": 0,
            "total_matched": 0,
            "total_missing": 0,
        }
        self.log_handler = BroadcastLogHandler()
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        self.log_handler.setFormatter(formatter)
        logging.getLogger("trackseerr").addHandler(self.log_handler)

    @staticmethod
    def _refresh_adopted_playlist(
        db: Database, plex_client: Optional[PlexClient], pl: dict[str, Any]
    ) -> Optional[set[str]]:
        """Refresh tracks_json of a service='plex' adopted playlist from its source.

        Returns the set of source rating keys that must not be written to (None if unresolved).
        Falls back to the stored snapshot when the source is gone or Plex is unreachable.
        """
        registry = db.get_plex_registry_by_adopted(pl["id"])
        if registry is None:
            logger.warning("Adopted playlist '%s' has no source registry row; using stored snapshot", pl["name"])
            return None
        skip = {str(registry["rating_key"])}
        if plex_client is None:
            return skip
        try:
            items = read_playlist_items(as_media_server(plex_client), registry["plex_user"], registry["rating_key"])
        except MediaServerError as e:
            logger.warning(
                "Source Plex playlist for adopted '%s' unavailable (%s); using stored snapshot", pl["name"], e.safe_detail
            )
            return skip
        snapshot = [
            {
                "title": str(getattr(i, "title", "") or ""),
                "artist": str(getattr(i, "grandparentTitle", "") or ""),
                "album": str(getattr(i, "parentTitle", "") or ""),
            }
            for i in items
        ]
        payload = json.dumps(snapshot)
        db.set_playlist_tracks_json(pl["id"], payload)
        pl["tracks_json"] = payload
        return skip

    @tracked("playlist_sync", "Plex Playlist Sync")
    def execute_sync(  # noqa: C901, PLR0915
        self,
        db: Database,
        config: Config,
        plex_client: Optional[PlexClient],
        spotify_client: Optional[SpotifyClient],
        deezer_client: Optional[DeezerClient],
    ) -> dict[str, Any]:
        """Runs synchronization across all enabled database playlists."""
        if self.is_syncing:
            return {"status": "already_running"}

        self.is_syncing = True
        logger.info("Starting background playlist synchronization cycle")
        stats = {
            "total_playlists": 0,
            "success_count": 0,
            "total_matched": 0,
            "total_missing": 0,
        }

        try:
            server = as_media_server(plex_client)
            if server:
                try:
                    refreshed = refresh_mix_snapshots(server, db)
                    if refreshed:
                        logger.info("Refreshed %d Plexamp mix snapshot(s)", refreshed)
                except MediaServerError as e:
                    logger.warning("Mix snapshot refresh failed: %s", e.safe_detail)
            playlists = db.list_playlists(enabled_only=True)
            stats["total_playlists"] = len(playlists)

            no_media_server = config.media_server_type == MEDIA_SERVER_NONE
            for pl in playlists:
                pl_id = pl["id"]
                target_usernames: list[str] = []
                if not no_media_server:
                    # Target users only matter for pushing to a media server; without one the playlist is
                    # still matched against the native library below.
                    target_uids = db.get_playlist_targets(pl_id)
                    if not target_uids:
                        logger.info("Playlist '%s' has no target users assigned; skipping", pl["name"])
                        continue

                    for uid in target_uids:
                        user_row = db.get_user(uid)
                        if user_row:
                            target_usernames.append(user_row["username"])

                    if not target_usernames:
                        logger.info("No valid usernames found for playlist '%s' targets", pl["name"])
                        continue

                tracks: list[Track] = []
                service = pl.get("service", "spotify")
                skip_rating_keys: Optional[set[str]] = None
                if service == "plex":
                    skip_rating_keys = self._refresh_adopted_playlist(db, plex_client, pl)
                try:
                    if is_listening_playlist(pl):
                        # Re-fetched every sync with the owner's linked account (a created-for playlist
                        # re-resolves to its newest edition); the snapshot keeps the last good list.
                        tracks = fetch_listening_tracks(db, config, pl)
                        db.set_playlist_tracks_json(
                            pl_id, json.dumps([{"title": t.title, "artist": t.artist, "album": t.album} for t in tracks])
                        )
                    elif pl_id.startswith("imp_") or pl.get("tracks_json"):
                        raw_tracks_json = pl.get("tracks_json")
                        if raw_tracks_json:
                            t_dicts = json.loads(raw_tracks_json)
                            tracks = [
                                Track(
                                    title=t.get("title", ""),
                                    artist=t.get("artist", ""),
                                    album=t.get("album", ""),
                                    url=t.get("url", ""),
                                )
                                for t in t_dicts
                                if t.get("title")
                            ]
                    elif service == "spotify" and spotify_client:
                        tracks = spotify_client.get_playlist_tracks(pl_id)
                    elif service == "deezer" and deezer_client:
                        tracks = deezer_client.get_playlist_tracks(pl_id)
                except Exception as e:
                    logger.error("Failed to fetch tracks for playlist '%s' (%s): %s", pl["name"], pl_id, safe_exc(e))
                    db.record_sync_result(pl_id, status="error")
                    continue

                model_playlist = Playlist(
                    id=pl_id,
                    name=pl["name"],
                    tracks=tracks,
                    description=pl.get("description", ""),
                    poster=pl.get("poster_url", ""),
                )

                if no_media_server:
                    # No media server: nothing is pushed, but the source playlist is matched against the native
                    # library so its missing tracks reach monitoring / wanted.
                    try:
                        matched, missing = match_playlist_tracks_native(db, tracks)
                        db.record_sync_result(playlist_id=pl_id, status="success", missing_tracks=missing)
                        apply_playlist_missing_safely(db, config, pl_id)
                        stats["success_count"] += 1
                        stats["total_matched"] += len(matched)
                        stats["total_missing"] += len(missing)
                    except Exception as e:  # one playlist must not stop the cycle; the root cause is logged
                        logger.error("Native library match failed for playlist '%s': %s", pl["name"], safe_exc(e))
                        logger.debug("Native match traceback", exc_info=True)
                        db.record_sync_result(playlist_id=pl_id, status="failed")
                elif server:
                    try:
                        results = server.sync_playlist(
                            model_playlist,
                            target_usernames,
                            PlaylistSyncOptions.from_config(config, db=db, skip_item_ids=skip_rating_keys),
                        )
                        matched, missing = server.match_playlist_tracks(
                            tracks, threshold=config.search_similarity_threshold
                        )
                        success = any(r.success for r in results) if results else False
                        db.record_sync_result(
                            playlist_id=pl_id,
                            status="success" if success else "failed",
                            missing_tracks=missing,
                        )
                        apply_playlist_missing_safely(db, config, pl_id)
                        if success:
                            stats["success_count"] += 1
                        stats["total_matched"] += len(matched)
                        stats["total_missing"] += len(missing)
                    except Exception as e:
                        logger.error("Error syncing playlist '%s' to Plex: %s", pl["name"], describe_error(e))
                        logger.debug("Playlist sync traceback", exc_info=True)
                        db.record_sync_result(playlist_id=pl_id, status="failed")
                else:
                    logger.warning("Plex client not available; recorded simulated sync for '%s'", pl["name"])
                    db.record_sync_result(playlist_id=pl_id, status="unconfigured")

            self.last_run_stats = stats
            self.last_run_at = datetime.now(timezone.utc).isoformat()
            logger.info(
                "Background sync complete: %d/%d succeeded, %d matched, %d missing",
                stats["success_count"],
                stats["total_playlists"],
                stats["total_matched"],
                stats["total_missing"],
            )
            try:
                db.record_event(
                    "sync_completed",
                    f"Playlist sync completed: {stats['total_playlists']} playlists processed ({stats['total_matched']} matched, {stats['total_missing']} missing)",
                    source="SyncCoordinator",
                    severity="info",
                )
            except Exception as ev_err:
                logger.warning("Failed to record sync_completed event: %s", ev_err)
            return {"status": "success", "stats": stats}
        except Exception as e:
            logger.error("Unexpected error during sync cycle: %s", safe_exc(e))
            logger.debug("Sync cycle traceback", exc_info=True)
            return {"status": "error", "error": safe_exc(e)}
        finally:
            self.is_syncing = False


# Shared sync state instance
sync_state = SyncState()


@router.post("", response_model=SyncTriggerResponse, response_model_exclude_unset=True)
def trigger_sync(
    background_tasks: BackgroundTasks,
    _admin: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[Any] = Depends(get_media_client),
    spotify_client: Optional[SpotifyClient] = Depends(get_spotify_client),
    deezer_client: Optional[DeezerClient] = Depends(get_deezer_client),
) -> dict[str, Any]:
    """Triggers sync in background task (runs async without blocking)."""
    if sync_state.is_syncing:
        return {
            "status": "already_running",
            "message": "Synchronization is already running in background",
        }

    background_tasks.add_task(
        sync_state.execute_sync,
        db=db,
        config=config,
        plex_client=plex_client,
        spotify_client=spotify_client,
        deezer_client=deezer_client,
    )

    return {
        "status": "started",
        "message": "Synchronization triggered successfully in background",
    }


@router.get("/status", response_model=SyncStatusResponse, response_model_exclude_unset=True)
def get_sync_status(
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Returns current sync status and last run stats."""
    return {
        "is_syncing": sync_state.is_syncing,
        "last_run_at": sync_state.last_run_at,
        "last_run_stats": sync_state.last_run_stats,
    }


@router.get("/stream")
async def stream_sync_logs(
    request: Request,
    limit: Optional[int] = None,
    _admin: dict[str, Any] = Depends(require_admin),
) -> StreamingResponse:
    """Server-Sent Events (SSE) streaming live log lines (administrators only)."""

    async def event_generator():
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        sync_state.log_handler.add_listener(loop, q)
        count = 0
        try:
            yield "data: Connected to live sync log stream\n\n"
            count += 1
            if limit is not None and count >= limit:
                return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    line = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {line}\n\n"
                    count += 1
                    if limit is not None and count >= limit:
                        break
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            sync_state.log_handler.remove_listener(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/webhook", response_model=SyncWebhookResponse, response_model_exclude_unset=True)
async def handle_sync_webhook(  # noqa: C901
    background_tasks: BackgroundTasks,
    request: Request,
    _auth: dict[str, Any] = Depends(verify_feed_access),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    plex_client: Optional[Any] = Depends(get_media_client),
    spotify_client: Optional[SpotifyClient] = Depends(get_spotify_client),
    deezer_client: Optional[DeezerClient] = Depends(get_deezer_client),
) -> dict[str, Any]:
    """Webhook endpoint for Lidarr, Plex, or external automation triggers.

    When Lidarr completes a track download/import or Plex completes a library scan,
    they can ping this endpoint to trigger immediate playlist sync and re-evaluation.
    Requires the FEED_TOKEN, an API key or an admin session; never a plain user.
    """
    body_preview = ""
    fulfilled_count = 0
    try:
        raw_body = await request.body()
        if raw_body:
            body_preview = raw_body[:200].decode("utf-8", errors="ignore")
            try:
                payload = json.loads(raw_body.decode("utf-8", errors="ignore"))
            except Exception:
                payload = None

            if isinstance(payload, dict):
                artist_name = None
                album_name = None
                track_title = None

                if isinstance(payload.get("artist"), dict):
                    artist_name = payload["artist"].get("name")
                elif isinstance(payload.get("artist"), str):
                    artist_name = payload["artist"]

                if isinstance(payload.get("album"), dict):
                    album_name = payload["album"].get("title")
                elif isinstance(payload.get("album"), str):
                    album_name = payload["album"]
                elif isinstance(payload.get("albums"), list) and payload["albums"]:
                    first_album = payload["albums"][0]
                    album_name = first_album.get("title") if isinstance(first_album, dict) else str(first_album)

                if isinstance(payload.get("track"), dict):
                    track_title = payload["track"].get("title")
                elif isinstance(payload.get("title"), str):
                    track_title = payload["title"]
                elif isinstance(payload.get("trackFiles"), list) and payload["trackFiles"]:
                    first_tf = payload["trackFiles"][0]
                    if isinstance(first_tf, dict) and isinstance(first_tf.get("track"), dict):
                        track_title = first_tf["track"].get("title")

                if artist_name:
                    matching_reqs = db.find_matching_processing_requests(
                        artist=artist_name, album=album_name, title=track_title
                    )
                    for m_req in matching_reqs:
                        db.update_request_status(m_req["id"], RequestStatus.AVAILABLE)
                        fulfilled_count += 1
                        logger.info(
                            "Auto-fulfilled request %s (%s - %s) to AVAILABLE via webhook",
                            m_req["id"],
                            m_req.get("artist"),
                            m_req.get("title"),
                        )
    except Exception as e:
        logger.debug("Error inspecting webhook payload for request fulfillment: %s", e)

    logger.info(
        "Sync webhook received (triggering background sync): %s (auto-fulfilled %d requests)",
        body_preview,
        fulfilled_count,
    )

    if sync_state.is_syncing:
        return {
            "status": "already_running",
            "message": "Synchronization is already in progress",
            "fulfilled_requests": fulfilled_count,
        }

    background_tasks.add_task(
        sync_state.execute_sync,
        db=db,
        config=config,
        plex_client=plex_client,
        spotify_client=spotify_client,
        deezer_client=deezer_client,
    )
    return {
        "status": "triggered",
        "message": "Background synchronization triggered via webhook",
        "fulfilled_requests": fulfilled_count,
    }


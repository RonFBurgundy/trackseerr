"""Missing tracks reporting, CSV export, RSS feeds, and Lidarr integration routes."""

import csv
from email.utils import formatdate
import io
import logging
from typing import Any, Optional
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from plex_playlist_sync.item_history import TRIGGER_PLAYLIST, GrabTrigger
from plex_playlist_sync.acquisition_coordinator import acquisition_coordinator
from plex_playlist_sync.api.dependencies import (
    get_config,
    get_db,
    get_lidarr_client,
    get_media_client,
    require_media_server,
    require_admin,
    verify_feed_access,
)
from plex_playlist_sync.api.schemas.missing import (
    GrabResult,
    LidarrPushResponse,
    LidarrQueueAction,
    LidarrQueueStatus,
    LidarrStatusResponse,
    MatchCreatedResponse,
    MatchDeletedResponse,
    MatchOverride,
    MediaTrackHit,
    MissingTrack,
)
from plex_playlist_sync.clients.lidarr import LidarrClient
from plex_playlist_sync.clients.plex import PlexClient
from plex_playlist_sync.media_servers import as_media_server
from plex_playlist_sync.config import Config
from plex_playlist_sync.lidarr_queue import lidarr_worker
from plex_playlist_sync.lidarr_release import norm_title
from plex_playlist_sync.library_manager import MODE_LIDARR, MODE_NATIVE, ModeChanged, get_library_mode, work_guard
from plex_playlist_sync.storage import Database

# Lidarr outcome -> missing_tracks.lidarr_status for a synchronous push that did not monitor the item.
_PUSH_FAILURE_STATUS = {
    "not_found": "not_found",
    "not_in_metadata_profile": "unavailable",
    "rate_limited": "rate_limited",
}

logger = logging.getLogger(__name__)

router = APIRouter()


class LidarrPushRequest(BaseModel):
    track_ids: Optional[list[int]] = Field(default=None, description="Optional list of specific missing track IDs to push")
    auto_search: Optional[bool] = Field(default=None, description="Override auto_search setting")
    batch_size: Optional[int] = Field(default=None, description="Maximum number of tracks to queue (e.g. 25, 50)")
    trickle: bool = Field(default=False, description="Process asynchronously via paced background trickle worker")
    delay_seconds: Optional[float] = Field(default=None, description="Delay pacing in seconds between lookups")


class MatchOverrideRequest(BaseModel):
    source_title: str = Field(..., min_length=1, max_length=500, description="Title of the track from source playlist")
    source_artist: str = Field(..., min_length=1, max_length=500, description="Artist of the track from source playlist")
    plex_rating_key: str = Field(..., min_length=1, max_length=100, description="Plex ratingKey of matched library track")
    plex_title: str = Field(..., min_length=1, max_length=500, description="Title of matched track in Plex")
    plex_artist: str = Field(..., min_length=1, max_length=500, description="Artist of matched track in Plex")


def _sanitize_csv_cell(value: Any) -> str:
    """Sanitizes CSV cell to prevent formula injection attacks.

    Prefixes leading dangerous characters (=, +, -, @, tab, CR, |) with a single quote,
    even when preceded by whitespace.
    """
    text = str(value if value is not None else "")
    if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "|")):
        return f"'{text}"
    return text


def _filter_missing_for_user(
    all_tracks: list[dict[str, Any]],
    user_context: Optional[dict[str, Any]],
    db: Database,
) -> list[dict[str, Any]]:
    """Applies RBAC filtering: Admins and feed tokens see all; standard users see only their targeted playlists;

    LAN readers without a token see only public/shared playlists.
    """
    if not user_context:
        return []
    if bool(user_context.get("is_admin")):
        return all_tracks
    if user_context.get("id") == "lan_reader":
        all_playlists = db.list_playlists()
        shared_ids = {p["id"] for p in all_playlists if not p.get("creator_id") or p.get("creator_id") in ("admin", "admin_1")}
        return [t for t in all_tracks if t.get("playlist_id") in shared_ids]

    user_playlists = {p["id"] for p in db.list_playlists(user_id=str(user_context["id"]))}
    return [t for t in all_tracks if t.get("playlist_id") in user_playlists]


@router.get("", response_model=list[MissingTrack], response_model_exclude_unset=True)
def get_missing_tracks(
    playlist_id: Optional[str] = Query(default=None, description="Optional playlist ID filter"),
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    """Returns missing tracks list.

    Admins can view all or filtered by playlist_id.
    Regular users only see missing tracks for playlists targeted to them.
    """
    is_admin = bool(current_user.get("is_admin"))

    if not is_admin:
        user_playlists = {p["id"] for p in db.list_playlists(user_id=str(current_user["id"]))}
        if playlist_id:
            if playlist_id not in user_playlists:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to requested playlist missing tracks",
                )
            return db.get_missing_tracks(playlist_id=playlist_id)
        all_tracks = db.get_missing_tracks()
        return [t for t in all_tracks if t["playlist_id"] in user_playlists]

    return db.get_missing_tracks(playlist_id=playlist_id)


@router.get("/csv")
def download_missing_csv(
    playlist_id: Optional[str] = Query(default=None, description="Optional playlist ID filter"),
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> StreamingResponse:
    """Generates and streams a safe CSV download of missing tracks."""
    tracks = get_missing_tracks(playlist_id=playlist_id, current_user=current_user, db=db)

    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)

    # Header
    writer.writerow(["Playlist ID", "Title", "Artist", "Album", "URL", "Created At"])

    for t in tracks:
        writer.writerow([
            _sanitize_csv_cell(t.get("playlist_id", "")),
            _sanitize_csv_cell(t.get("title", "")),
            _sanitize_csv_cell(t.get("artist", "")),
            _sanitize_csv_cell(t.get("album", "")),
            _sanitize_csv_cell(t.get("url", "")),
            _sanitize_csv_cell(t.get("created_at", "")),
        ])

    csv_bytes = output.getvalue().encode("utf-8")
    filename = f"missing_tracks_{playlist_id}.csv" if playlist_id else "missing_tracks.csv"

    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/rss")
def feed_missing_rss(
    playlist_id: Optional[str] = Query(default=None, description="Optional playlist ID filter"),
    token: Optional[str] = Query(default=None, description="Optional feed token or API key"),
    user_context: Optional[dict[str, Any]] = Depends(verify_feed_access),
    db: Database = Depends(get_db),
) -> Response:
    """Generates an RSS 2.0 XML feed of missing music for Lidarr and RSS clients."""
    all_tracks = db.get_missing_tracks(playlist_id=playlist_id)
    filtered = _filter_missing_for_user(all_tracks, user_context, db)

    playlists_map = {p["id"]: p["name"] for p in db.list_playlists()}
    now_rfc822 = formatdate(usegmt=True)

    items_xml = []
    for t in filtered:
        p_name = playlists_map.get(t.get("playlist_id", ""), t.get("playlist_id", ""))
        title = escape(f"{t.get('artist', '')} - {t.get('title', '')}")
        album = escape(t.get("album", "") or "Unknown Album")
        artist = escape(t.get("artist", "") or "Unknown Artist")
        track_title = escape(t.get("title", ""))
        pl_name_esc = escape(p_name)
        guid = f"trackseerr-missing-{t.get('id', 0)}"

        def _clean_cdata(val: Any) -> str:
            return str(val or "").replace("]]>", "]]&gt;")

        desc = (
            f"<![CDATA[Track: {_clean_cdata(track_title)}<br/>Artist: {_clean_cdata(artist)}<br/>Album: {_clean_cdata(album)}<br/>Playlist: {_clean_cdata(pl_name_esc)}]]>"
        )

        item = f"""    <item>
      <title>{title}</title>
      <description>{desc}</description>
      <guid isPermaLink="false">{guid}</guid>
      <pubDate>{now_rfc822}</pubDate>
      <category>{pl_name_esc}</category>
    </item>"""
        items_xml.append(item)

    items_block = "\n".join(items_xml)
    channel_desc = "Missing tracks unmatched in Plex Media Server music library"
    if playlist_id and playlist_id in playlists_map:
        channel_desc += f" for playlist {playlists_map[playlist_id]}"

    rss_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>TrackSeerr - Missing Music</title>
    <description>{escape(channel_desc)}</description>
    <link>http://localhost:5250</link>
    <language>en-us</language>
    <lastBuildDate>{now_rfc822}</lastBuildDate>
{items_block}
  </channel>
</rss>"""

    return Response(content=rss_xml, media_type="application/rss+xml; charset=utf-8")



@router.get("/text")
def feed_missing_text(
    playlist_id: Optional[str] = Query(default=None, description="Optional playlist ID filter"),
    token: Optional[str] = Query(default=None, description="Optional feed token or API key"),
    user_context: Optional[dict[str, Any]] = Depends(verify_feed_access),
    db: Database = Depends(get_db),
) -> Response:
    """Returns a plain text list of missing tracks (one per line)."""
    all_tracks = db.get_missing_tracks(playlist_id=playlist_id)
    filtered = _filter_missing_for_user(all_tracks, user_context, db)
    seen = set()
    lines = []
    for t in filtered:
        line = f"{t.get('artist', '').strip()} - {t.get('title', '').strip()}"
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return Response(content="\n".join(lines) + ("\n" if lines else ""), media_type="text/plain; charset=utf-8")


@router.get("/lidarr/status", response_model=LidarrStatusResponse, response_model_exclude_unset=True)
def get_lidarr_status(
    _current_user: dict[str, Any] = Depends(require_admin),
    config: Config = Depends(get_config),
    db: Database = Depends(get_db),
    lidarr_client: Optional[LidarrClient] = Depends(get_lidarr_client),
) -> dict[str, Any]:
    """Returns Lidarr connection and configuration status from DB or config.

    In native mode Lidarr is not contacted at all: the mode is reported with ``connected`` null.
    """
    if get_library_mode(db) != MODE_LIDARR:
        return {"mode": MODE_NATIVE, "connected": None}
    db_settings = db.get_lidarr_settings()
    url = db_settings.get("url") or config.lidarr_url
    auto_search = (
        db_settings.get("auto_search")
        if db_settings.get("url")
        else config.lidarr_auto_search
    )

    if lidarr_client is None:
        return {
            "configured": False,
            "url": url,
            "auto_search": False,
            "status": {
                "online": False,
                "message": "Lidarr is not configured (configure in Settings -> Lidarr Automation or set LIDARR_URL and LIDARR_API_KEY)",
            },
        }
    conn_result = lidarr_client.test_connection()
    return {
        "configured": True,
        "url": url,
        "auto_search": bool(auto_search),
        "status": conn_result,
    }


@router.post("/lidarr/push", response_model=LidarrPushResponse, response_model_exclude_unset=True)
def push_missing_to_lidarr(
    req: Optional[LidarrPushRequest] = None,
    current_user: dict[str, Any] = Depends(require_admin),
    config: Config = Depends(get_config),
    db: Database = Depends(get_db),
    lidarr_client: Optional[LidarrClient] = Depends(get_lidarr_client),
) -> dict[str, Any]:
    """Pushes missing tracks directly into Lidarr to queue download and monitoring.

    Supports:
    - Background trickle mode (`trickle=True`) with delay pacing and rate-limit backoff.
    - Synchronous push (`trickle=False`) for targeted or immediate single-item updates.
    """
    try:
        with work_guard(db, MODE_LIDARR):
            return _push_missing_to_lidarr(req, current_user, config, db, lidarr_client)
    except ModeChanged as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Library manager is set to TrackSeerr; switch it to Lidarr to push items to Lidarr.",
        ) from exc


def _push_missing_to_lidarr(
    req: Optional[LidarrPushRequest],
    current_user: dict[str, Any],
    config: Config,
    db: Database,
    lidarr_client: Optional[LidarrClient],
) -> dict[str, Any]:
    if lidarr_client is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Lidarr is not configured. Configure in Settings or set LIDARR_URL and LIDARR_API_KEY.",
        )

    all_tracks = db.get_missing_tracks()
    filtered = _filter_missing_for_user(all_tracks, current_user, db)

    target_ids = set(req.track_ids) if (req and req.track_ids is not None) else None
    if target_ids is not None:
        filtered = [t for t in filtered if t.get("id") in target_ids]
    else:
        # If pushing all without explicit IDs, prioritize unmonitored tracks
        unmonitored = [t for t in filtered if t.get("lidarr_status") != "monitored"]
        if unmonitored:
            filtered = unmonitored

    lidarr_settings = db.get_lidarr_settings()
    default_auto_search = lidarr_settings.get("auto_search", config.lidarr_auto_search)
    default_trickle_rate = lidarr_settings.get("trickle_rate_seconds", config.lidarr_trickle_rate_seconds)
    default_batch_size = lidarr_settings.get("trickle_batch_size", config.lidarr_trickle_batch_size)

    batch_size = req.batch_size if (req and req.batch_size is not None) else default_batch_size
    if batch_size and batch_size > 0:
        filtered = filtered[:batch_size]

    should_search = req.auto_search if (req and req.auto_search is not None) else default_auto_search
    use_trickle = req.trickle if (req and req.trickle is not None) else False
    delay = req.delay_seconds if (req and req.delay_seconds is not None) else default_trickle_rate

    # Background trickle mode
    if use_trickle:
        queue_res = lidarr_worker.start_trickle(
            items=filtered,
            client=lidarr_client,
            db=db,
            delay_seconds=delay,
            auto_search=should_search,
            batch_size=batch_size,
        )
        return {
            "status": "queued",
            "trickle": True,
            "queued_count": len(filtered),
            "auto_search": should_search,
            "delay_seconds": delay,
            "message": queue_res.get("message", f"Queued {len(filtered)} tracks into Lidarr background worker"),
            "queue_status": lidarr_worker.get_status(),
        }

    # Synchronous push (for backwards compatibility / single track requests)
    seen = set()
    deduped = []
    for t in filtered:
        key = (
            (t.get("artist") or "").strip().lower(),
            (t.get("album") or "").strip().lower(),
            norm_title(t.get("title")),
        )
        if key not in seen:
            seen.add(key)
            deduped.append(t)

    results = []
    added_count = 0
    monitored_count = 0
    failed_count = 0

    for item in deduped:
        artist = item.get("artist", "").strip()
        album = item.get("album", "").strip()
        title = item.get("title", "").strip()
        res = lidarr_client.search_and_add_track(
            artist_name=artist,
            album_name=album,
            title=title,
            auto_search=should_search,
            album_wait_attempts=1,  # request thread: never sleep waiting for a new artist's albums; a re-push finishes it
        )
        results.append(res)
        track_id = item.get("id")
        if res.get("status") == "added":
            added_count += 1
            if track_id:
                db.update_missing_track_lidarr_status(track_id, "monitored")
        elif res.get("status") == "already_monitored":
            monitored_count += 1
            if track_id:
                db.update_missing_track_lidarr_status(track_id, "monitored")
        else:
            failed_count += 1
            if track_id:
                # unavailable (not in the metadata profile) and not_found are re-checked weekly; rate_limited and
                # error back off (see storage.lidarr_retry_delay).
                db.update_missing_track_lidarr_status(
                    track_id, _PUSH_FAILURE_STATUS.get(str(res.get("status")), "error")
                )

    return {
        "status": "completed",
        "trickle": False,
        "total_requested": len(filtered),
        "deduplicated_items": len(deduped),
        "added": added_count,
        "already_monitored": monitored_count,
        "failed": failed_count,
        "results": results,
    }


@router.get("/lidarr/queue", response_model=LidarrQueueStatus, response_model_exclude_unset=True)
def get_lidarr_queue_status(
    _current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Returns the live status of the Lidarr background trickle worker."""
    return lidarr_worker.get_status()


@router.post("/lidarr/queue/pause", response_model=LidarrQueueAction, response_model_exclude_unset=True)
def pause_lidarr_queue(
    _current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Pauses the Lidarr background trickle worker."""
    res = lidarr_worker.pause()
    status = lidarr_worker.get_status()
    return {**status, "action_status": res.get("status"), "action_message": res.get("message")}


@router.post("/lidarr/queue/resume", response_model=LidarrQueueAction, response_model_exclude_unset=True)
def resume_lidarr_queue(
    _current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Resumes the Lidarr background trickle worker."""
    res = lidarr_worker.resume()
    status = lidarr_worker.get_status()
    return {**status, "action_status": res.get("status"), "action_message": res.get("message")}


@router.post("/lidarr/queue/cancel", response_model=LidarrQueueAction, response_model_exclude_unset=True)
def cancel_lidarr_queue(
    _current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Cancels and stops the Lidarr background trickle worker."""
    res = lidarr_worker.cancel()
    status = lidarr_worker.get_status()
    return {**status, "action_status": res.get("status"), "action_message": res.get("message")}


@router.get("/search", response_model=list[MediaTrackHit], response_model_exclude_unset=True, dependencies=[Depends(require_media_server)])
def search_plex_tracks(
    query: str = Query(..., min_length=1, description="Query string to search Plex library tracks"),
    limit: int = Query(default=15, ge=1, le=50),
    current_user: dict[str, Any] = Depends(require_admin),
    plex_client: Optional[Any] = Depends(get_media_client),
) -> list[dict[str, Any]]:
    """Searches the Plex library for tracks to enable manual matching and correction."""
    server = as_media_server(plex_client)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Plex Media Server client is not configured",
        )
    return server.search_tracks(query, limit=limit)


@router.post("/match", response_model=MatchCreatedResponse, response_model_exclude_unset=True, status_code=status.HTTP_201_CREATED)
def create_match_override(
    req: MatchOverrideRequest,
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Records a manual match override (Match Memory) and removes corresponding missing tracks."""
    override = db.add_match_override(
        source_title=req.source_title,
        source_artist=req.source_artist,
        plex_rating_key=req.plex_rating_key,
        plex_title=req.plex_title,
        plex_artist=req.plex_artist,
        created_by=str(current_user["id"]),
    )
    # Remove from missing_tracks where title and artist match
    with db._lock:
        db.conn.execute(
            "DELETE FROM missing_tracks WHERE LOWER(title) = LOWER(?) AND LOWER(artist) = LOWER(?)",
            (req.source_title.strip(), req.source_artist.strip()),
        )
        db.conn.commit()

    return {"status": "matched", "override": override}


@router.get("/matches", response_model=list[MatchOverride], response_model_exclude_unset=True)
def list_match_overrides(
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    """Lists all stored Match Memory overrides."""
    return db.list_match_overrides()


@router.delete("/match/{override_id}", response_model=MatchDeletedResponse, response_model_exclude_unset=True)
def delete_match_override(
    override_id: int,
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Deletes a Match Memory override."""
    success = db.delete_match_override(override_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Match override not found",
        )
    return {"status": "deleted", "id": override_id}


def _playlist_trigger(db: Database, track: dict[str, Any], admin: dict[str, Any]) -> GrabTrigger:
    """A missing track's grab comes from its playlist (ref = playlist id, label = playlist name)."""
    playlist_id = str(track.get("playlist_id") or "") or None
    playlist = db.get_playlist(playlist_id) if playlist_id else None
    uid = admin.get("id")
    return GrabTrigger(
        TRIGGER_PLAYLIST,
        ref=playlist_id,
        label=(playlist or {}).get("name"),
        actor_user_id=str(uid) if uid and uid != "api_key_user" else None,
    )


@router.post("/{track_id}/grab", response_model=GrabResult, response_model_exclude_unset=True)
def grab_missing_track(
    track_id: int,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Admin-only endpoint to trigger native search and grab for a missing track."""
    track = db.get_missing_track(track_id)
    if not track:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Missing track {track_id} not found",
        )

    try:
        with work_guard(db, MODE_NATIVE):
            return acquisition_coordinator.search_and_grab(
                artist=track["artist"],
                title=track["title"],
                album=track.get("album"),
                item_type="track",
                db=db,
                trigger=_playlist_trigger(db, track, _admin),
            )
    except ModeChanged as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Library manager is set to Lidarr; native grabs are disabled.",
        ) from exc


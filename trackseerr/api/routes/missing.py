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

from trackseerr.item_history import TRIGGER_PLAYLIST, GrabTrigger
from trackseerr.acquisition_coordinator import acquisition_coordinator
from trackseerr.api.dependencies import (
    get_db,
    get_media_client,
    require_media_server,
    require_admin,
    verify_feed_access,
)
from trackseerr.api.schemas.missing import (
    GrabResult,
    MatchCreatedResponse,
    MatchDeletedResponse,
    MatchOverride,
    MediaTrackHit,
    MissingTrack,
)
from trackseerr.media_servers import as_media_server
from trackseerr.library_manager import MODE_NATIVE, ModeChanged, work_guard
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()


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


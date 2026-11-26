"""Missing tracks reporting, CSV export, RSS feeds, and Lidarr integration routes."""

import csv
from email.utils import formatdate
import io
import logging
from typing import Any, Literal, Optional
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from trackseerr.item_history import TRIGGER_PLAYLIST, GrabTrigger
from trackseerr.acquisition_coordinator import acquisition_coordinator
from trackseerr.api.dependencies import (
    get_config,
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
    TrackAlias,
    TrackAliasDeletedResponse,
)
from trackseerr.api.routes.issues import FINAL_STATUSES, _apply_status
from trackseerr.config import Config
from trackseerr.library_search import search_library
from trackseerr.media_servers import MediaServer, as_media_server, registry
from trackseerr.models import IssueStatus
from trackseerr.library_manager import MODE_NATIVE, ModeChanged, work_guard
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter()


class MatchOverrideRequest(BaseModel):
    source_title: str = Field(..., min_length=1, max_length=500, description="Title of the track from source playlist")
    source_artist: str = Field(..., min_length=1, max_length=500, description="Artist of the track from source playlist")
    track_id: Optional[str] = Field(default=None, min_length=1, max_length=100, description="Library track ID for a cross-server library pin")
    plex_rating_key: Optional[str] = Field(default=None, min_length=1, max_length=100, description="Library item id or Plex ratingKey of matched track")
    plex_title: Optional[str] = Field(default=None, min_length=1, max_length=500, description="Title of matched track in media server")
    plex_artist: Optional[str] = Field(default=None, min_length=1, max_length=500, description="Artist of matched track in media server")
    server_id: Optional[str] = Field(default=None, max_length=128, description="Optional media server ID for server-scoped overrides")


class TrackAliasCreateRequest(BaseModel):
    source_title: str = Field(..., min_length=1, max_length=500, description="Title as the source playlist spells it")
    source_artist: str = Field(..., min_length=1, max_length=500, description="Artist as the source playlist credits it")
    kind: Literal["alias", "ignore"] = Field(..., description="alias: match as the target song; ignore: unmatchable")
    target_title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    target_artist: Optional[str] = Field(default=None, min_length=1, max_length=500)
    target_album: Optional[str] = Field(default=None, max_length=500)


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
    channel_desc = "Missing tracks not found in your media server library"
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




def _resolve_search_server(
    server_id: Optional[str],
    plex_client: Optional[Any],
    config: Config,
) -> MediaServer:
    """Resolve the media server adapter for manual track search."""
    sid = server_id.strip() if server_id and server_id.strip() else None
    if sid is not None:
        pair = registry.server_by_id(sid, config=config)
        if pair is not None:
            return pair[1]
        rec = next((r for r in registry.records(config) if r.id == sid), None)
        if rec is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media server not found")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Media server '{rec.name}' is not configured",
        )
    server = as_media_server(plex_client)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Plex Media Server client is not configured",
        )
    return server


def _clear_missing_track(db: Database, source_title: str, source_artist: str) -> None:
    """Delete matching rows from ``missing_tracks`` once a track is matched everywhere."""
    with db._lock:
        db.conn.execute(
            "DELETE FROM missing_tracks WHERE LOWER(title) = LOWER(?) AND LOWER(artist) = LOWER(?)",
            (source_title.strip(), source_artist.strip()),
        )
        db.conn.commit()


@router.get("/search", response_model=list[MediaTrackHit], response_model_exclude_unset=True)
def search_plex_tracks(
    query: str = Query(..., min_length=1, description="Query string to search library tracks"),
    limit: int = Query(default=15, ge=1, le=50),
    source: Literal["library", "server"] = Query(default="server", description="Search Trackseerr's library ('library') or a media server ('server')"),
    server_id: Optional[str] = Query(default=None, description="Optional media server ID (defaults to primary server)"),
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    config: Config = Depends(get_config),
) -> list[dict[str, Any]]:
    """Searches Trackseerr's library or a media server library for tracks to enable manual matching."""
    if source == "library":
        found = search_library(db, query, limit=limit)
        return [
            {
                "track_id": str(t["id"]),
                "title": str(t["title"]),
                "artist": str(t.get("artist_name") or ""),
                "album": t.get("album_title") or None,
            }
            for t in found.get("tracks", [])
        ]
    require_media_server(config)
    server = _resolve_search_server(server_id, plex_client, config)
    return server.search_tracks(query, limit=limit)


@router.post("/match", response_model=MatchCreatedResponse, response_model_exclude_unset=True, status_code=status.HTTP_201_CREATED)
def create_match_override(
    req: MatchOverrideRequest,
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Records a library match pin or manual server override (Match Memory) and clears missing rows."""
    tid = req.track_id.strip() if req.track_id and req.track_id.strip() else None
    sid = req.server_id.strip() if req.server_id and req.server_id.strip() else None

    if tid is not None:
        if sid is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Specify either track_id or server_id, not both",
            )
        if db.get_library_track(tid) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Library track not found")
        override = db.set_library_pin(
            source_title=req.source_title,
            source_artist=req.source_artist,
            track_id=tid,
            created_by=str(current_user["id"]),
        )
        _clear_missing_track(db, req.source_title, req.source_artist)
        return {"status": "matched", "override": override}

    if not (req.plex_rating_key and req.plex_title and req.plex_artist):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="plex_rating_key, plex_title, and plex_artist are required when track_id is not provided",
        )

    if sid is not None:
        srv_row = db.get_media_server_row(sid)
        if srv_row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media server not found")
        override = db.add_server_match_override(
            server_id=sid,
            source_title=req.source_title,
            source_artist=req.source_artist,
            item_id=req.plex_rating_key,
            item_title=req.plex_title,
            item_artist=req.plex_artist,
            created_by=str(current_user["id"]),
        )
        override["server_name"] = srv_row["name"]
        override["kind"] = "server"
    else:
        override = db.add_match_override(
            source_title=req.source_title,
            source_artist=req.source_artist,
            plex_rating_key=req.plex_rating_key,
            plex_title=req.plex_title,
            plex_artist=req.plex_artist,
            created_by=str(current_user["id"]),
        )
        override["kind"] = "legacy"
    # Remove from missing_tracks only for legacy or primary server overrides
    primary = registry.primary_record(config)
    if sid is None or (primary is not None and sid == primary.id):
        _clear_missing_track(db, req.source_title, req.source_artist)

    return {"status": "matched", "override": override}


@router.get("/matches", response_model=list[MatchOverride], response_model_exclude_unset=True)
def list_match_overrides(
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    """Lists all stored Match Memory overrides and cross-server library pins."""
    server_names = {row["id"]: row["name"] for row in db.list_media_server_rows()}
    pins = db.list_library_pins()
    overrides = db.list_match_overrides()
    for override in overrides:
        sid = override.get("server_id")
        override["server_name"] = server_names.get(sid) if sid else None
        override["kind"] = "server" if sid else "legacy"
    combined = pins + overrides
    combined.sort(key=lambda item: (str(item.get("created_at") or ""), int(item.get("id") or 0)), reverse=True)
    return combined


@router.delete("/match/{override_id}", response_model=MatchDeletedResponse, response_model_exclude_unset=True)
def delete_match_override(
    override_id: int,
    server_id: Optional[str] = Query(default=None, description="Optional media server ID for server-scoped overrides"),
    kind: Optional[Literal["library", "server", "legacy"]] = Query(default=None, description="Override kind ('library' for library pins)"),
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Deletes a Match Memory override or library match pin."""
    if kind == "library":
        success = db.delete_library_pin(override_id)
    else:
        sid = server_id.strip() if server_id and server_id.strip() else None
        success = db.delete_match_override(override_id, server_id=sid)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Match override not found",
        )
    return {"status": "deleted", "id": override_id}


@router.get("/aliases", response_model=list[TrackAlias], response_model_exclude_unset=True)
def list_track_aliases(
    status_filter: Optional[Literal["pending", "approved", "rejected"]] = Query(default=None, alias="status"),
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> list[dict[str, Any]]:
    """Lists track aliases, optionally only those in one status."""
    return db.list_track_aliases(status_filter)


@router.post(
    "/aliases", response_model=TrackAlias, response_model_exclude_unset=True, status_code=status.HTTP_201_CREATED
)
def create_track_alias(
    req: TrackAliasCreateRequest,
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Admin-created alias (approved at once): a manual alias or an ``ignore`` (unmatchable) mark."""
    if req.kind == "alias" and not (req.target_title and req.target_artist):
        raise HTTPException(
            status_code=422,
            detail="An alias needs target_title and target_artist",
        )
    is_alias = req.kind == "alias"
    return db.upsert_track_alias(
        source_title=req.source_title,
        source_artist=req.source_artist,
        kind=req.kind,
        status="approved",
        target_title=req.target_title if is_alias else None,
        target_artist=req.target_artist if is_alias else None,
        target_album=req.target_album if is_alias else None,
        decided_by=str(current_user["id"]),
    )


def _close_linked_issue(db: Database, alias: dict[str, Any], issue_status: IssueStatus, admin: dict[str, Any]) -> None:
    """Moves the issue behind a suggested alias to ``issue_status`` unless it is already closed."""
    issue_id = alias.get("issue_id")
    issue = db.get_issue(str(issue_id)) if issue_id else None
    if issue is None or issue["status"] in FINAL_STATUSES:
        return
    try:
        _apply_status(db, issue, issue_status.value, admin, is_admin=True)
    except HTTPException as e:
        logger.warning("Could not set issue %s to %s for track alias %s: %s", issue_id, issue_status.value, alias["id"], e.detail)


def _decide_track_alias(
    alias_id: int, new_status: str, issue_status: IssueStatus, admin: dict[str, Any], db: Database
) -> dict[str, Any]:
    alias = db.set_track_alias_status(alias_id, new_status, str(admin["id"]))
    if alias is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track alias not found")
    _close_linked_issue(db, alias, issue_status, admin)
    return alias


@router.post("/aliases/{alias_id}/approve", response_model=TrackAlias, response_model_exclude_unset=True)
def approve_track_alias(
    alias_id: int,
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Approves an alias (applied from the next sync) and resolves its linked issue."""
    return _decide_track_alias(alias_id, "approved", IssueStatus.RESOLVED, current_user, db)


@router.post("/aliases/{alias_id}/reject", response_model=TrackAlias, response_model_exclude_unset=True)
def reject_track_alias(
    alias_id: int,
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Rejects an alias and closes its linked issue as won't fix."""
    return _decide_track_alias(alias_id, "rejected", IssueStatus.WONT_FIX, current_user, db)


@router.delete("/aliases/{alias_id}", response_model=TrackAliasDeletedResponse, response_model_exclude_unset=True)
def delete_track_alias(
    alias_id: int,
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Deletes an alias."""
    if not db.delete_track_alias(alias_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Track alias not found")
    return {"status": "deleted", "id": alias_id}


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


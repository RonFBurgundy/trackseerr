"""Plex Playlist Control: inspect and manage a Plex user's audio playlists and Plexamp mixes."""

import json
import logging
from contextlib import contextmanager
from typing import Any, Iterator, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from plexapi.exceptions import BadRequest, NotFound, PlexApiException, Unauthorized
from pydantic import BaseModel, Field
import requests

from plex_playlist_sync.api.dependencies import get_current_user, get_db, get_plex_client, require_media_server
from plex_playlist_sync.api.schemas.playlists import PlaylistRecord
from plex_playlist_sync.api.schemas.plex_playlists import (
    MixSnapshot,
    PlaylistCopyResult,
    PlexMix,
    PlexPlaylistItem,
    PlexPlaylistSummary,
    PlexUser,
)
from plex_playlist_sync.clients.plex import (
    MixNotFoundError,
    PlaylistProtectedError,
    PlexClient,
    classify_playlist_owner,
    is_smart_playlist,
)
from plex_playlist_sync.api.routes.playlists import _guard_existing_playlist
from plex_playlist_sync.storage import Database
from plex_playlist_sync.redaction import redact_text, safe_exc

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_media_server)])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class RenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class AddItemsRequest(BaseModel):
    track_rating_keys: list[str] = Field(..., min_length=1, max_length=500)


class MoveItemRequest(BaseModel):
    after_playlist_item_id: Optional[int] = None


class CopyRequest(BaseModel):
    target_users: list[str] = Field(..., min_length=1)
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)


class FlagsRequest(BaseModel):
    ignored: Optional[bool] = None
    owner: Optional[Literal["user", "trackseerr"]] = None


class MixSnapshotRequest(BaseModel):
    mix_key: str = Field(..., min_length=1)
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    auto_refresh: bool = False


class MixSnapshotUpdateRequest(BaseModel):
    auto_refresh: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _plex_errors() -> Iterator[None]:
    """Translate PlexAPI failures into HTTP errors, logging the root cause."""
    try:
        yield
    except NotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=redact_text(str(e)) or "Not found in Plex") from e
    except (BadRequest, Unauthorized, requests.exceptions.RequestException) as e:
        logger.warning("Plex request failed: %s", safe_exc(e))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Plex error: {safe_exc(e)}") from e


def _require_plex(plex: Optional[PlexClient]) -> PlexClient:
    if plex is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Plex is not configured")
    return plex


def _own_username(current_user: dict[str, Any]) -> str:
    return str(current_user.get("username") or "")


def _known_usernames(plex: PlexClient) -> dict[str, str]:
    """Map lowercase username -> canonical username for the admin and every Plex Home user."""
    with _plex_errors():
        return {str(u["username"]).lower(): str(u["username"]) for u in plex.get_home_users()}


def _resolve_user(plex: PlexClient, current_user: dict[str, Any], user: Optional[str]) -> str:
    """Authorize the target Plex user. Returns the canonical username (404 on failure)."""
    own = _own_username(current_user)
    target = (user or own).strip()
    if not target:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Your account has no Plex username")
    if not current_user.get("is_admin"):
        if target.lower() != own.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plex user not found")
        return own
    if target.lower() == own.lower() and plex.is_admin_username(target):
        return target
    known = _known_usernames(plex)
    if target.lower() not in known:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown Plex user '{target}'")
    return known[target.lower()]


def _server_for(plex: PlexClient, username: str) -> Any:
    try:
        return plex.get_user_server(username)
    except (NotFound, BadRequest, Unauthorized, requests.exceptions.RequestException) as e:
        logger.warning("Could not switch to Plex user '%s': %s", username, safe_exc(e))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not access Plex profile for '{username}': {safe_exc(e)}"
        ) from e


def _context(
    plex: Optional[PlexClient], current_user: dict[str, Any], user: Optional[str]
) -> tuple[PlexClient, str, Any]:
    client = _require_plex(plex)
    username = _resolve_user(client, current_user, user)
    return client, username, _server_for(client, username)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _as_int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _summary(pl: Any, plex_user: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "rating_key": str(getattr(pl, "ratingKey", "")),
        "title": str(getattr(pl, "title", "") or ""),
        "kind": "smart" if is_smart_playlist(pl) else "regular",
        "owner": row["owner"],
        "ignored": bool(row["ignored"]),
        "track_count": _as_int(getattr(pl, "leafCount", 0)),
        "duration_ms": _as_int(getattr(pl, "duration", 0)),
        # Plex thumbs need the server token; no token-free image proxy exists for Plex artwork.
        "thumb_url": None,
        "updated_at": _iso(getattr(pl, "updatedAt", None)),
        "trackseerr_playlist_id": row.get("trackseerr_playlist_id"),
        "plex_user": plex_user.lower(),
    }


def _item_dict(item: Any) -> dict[str, Any]:
    return {
        "playlist_item_id": int(getattr(item, "playlistItemID", 0) or 0),
        "rating_key": str(getattr(item, "ratingKey", "")),
        "title": str(getattr(item, "title", "") or ""),
        "artist": str(getattr(item, "grandparentTitle", "") or ""),
        "album": str(getattr(item, "parentTitle", "") or ""),
        "duration_ms": _as_int(getattr(item, "duration", 0)),
    }


def _items_response(plex: PlexClient, server: Any, rating_key: str) -> list[dict[str, Any]]:
    playlist = plex.get_playlist(server, rating_key)
    return [_item_dict(i) for i in plex.get_playlist_items(playlist)]


def _ensure_row(plex: PlexClient, db: Database, username: str, pl: Any) -> dict[str, Any]:
    """Return the registry row for a playlist, classifying and recording it on first sight."""
    key = str(getattr(pl, "ratingKey", ""))
    smart = is_smart_playlist(pl)
    title = str(getattr(pl, "title", "") or "")
    existing = db.get_plex_registry_row(username, key)
    if existing is None:
        return db.upsert_plex_registry(
            username, key, title, "smart" if smart else "regular", classify_playlist_owner(db, username, title, smart)
        )
    return db.upsert_plex_registry(
        username, key, title, "smart" if smart else "regular", "plexamp" if smart else existing["owner"]
    )


def _load_playlist(plex: PlexClient, server: Any, rating_key: str) -> Any:
    with _plex_errors():
        return plex.get_playlist(server, rating_key)


def _require_regular(pl: Any) -> None:
    if is_smart_playlist(pl):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Smart playlists are read-only; track edits are not allowed"
        )


def _snapshot_out(snap: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": snap["id"],
        "plex_user": snap["plex_user"],
        "mix_key": snap["mix_key"],
        "mix_title": snap["mix_title"],
        "playlist_title": snap["playlist_title"],
        "rating_key": snap.get("rating_key"),
        "auto_refresh": bool(snap["auto_refresh"]),
        "last_refreshed_at": snap.get("last_refreshed_at"),
    }


def _authorized_snapshot(
    db: Database, current_user: dict[str, Any], plex: Optional[PlexClient], snapshot_id: str
) -> dict[str, Any]:
    _require_plex(plex)
    snap = db.get_mix_snapshot(snapshot_id)
    if snap is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mix snapshot not found")
    if not current_user.get("is_admin") and snap["plex_user"] != _own_username(current_user).lower():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mix snapshot not found")
    return snap


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/users", response_model=list[PlexUser], response_model_exclude_unset=True)
def list_plex_users(
    current_user: dict[str, Any] = Depends(get_current_user),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> list[dict[str, Any]]:
    client = _require_plex(plex)
    own = _own_username(current_user)
    if not current_user.get("is_admin"):
        return [{"username": own, "is_admin_account": client.is_admin_username(own), "is_self": True}]
    with _plex_errors():
        home = client.get_home_users()
    return [
        {
            "username": str(u["username"]),
            "is_admin_account": bool(u.get("is_admin")),
            "is_self": str(u["username"]).lower() == own.lower(),
        }
        for u in home
    ]


# ---------------------------------------------------------------------------
# Mixes (declared before /{rating_key} routes)
# ---------------------------------------------------------------------------


@router.get("/mixes", response_model=list[PlexMix], response_model_exclude_unset=True)
def list_mixes(
    user: Optional[str] = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> list[dict[str, Any]]:
    client, username, server = _context(plex, current_user, user)
    snaps = {s["mix_key"]: s["id"] for s in db.list_mix_snapshots(plex_user=username)}
    mixes = client.get_user_mixes(username, server=server)
    return [{**m, "snapshot_id": snaps.get(m["mix_key"])} for m in mixes]


@router.get("/mixes/snapshots", response_model=list[MixSnapshot], response_model_exclude_unset=True)
def list_snapshots(
    user: Optional[str] = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> list[dict[str, Any]]:
    client = _require_plex(plex)
    username = _resolve_user(client, current_user, user)
    return [_snapshot_out(s) for s in db.list_mix_snapshots(plex_user=username)]


@router.post("/mixes/snapshot", response_model=MixSnapshot, response_model_exclude_unset=True)
def create_snapshot(
    req: MixSnapshotRequest,
    user: Optional[str] = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> dict[str, Any]:
    client, username, server = _context(plex, current_user, user)
    mix = next((m for m in client.get_user_mixes(username, server=server) if m["mix_key"] == req.mix_key), None)
    if mix is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mix not found")
    playlist_title = (req.title or f"{mix['title']} (Saved)").strip()
    try:
        with _plex_errors():
            playlist = client.save_mix_as_playlist(db, username, req.mix_key, playlist_title, server=server)
    except PlaylistProtectedError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=redact_text(str(e))) from e
    except MixNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=redact_text(str(e))) from e
    snap = db.upsert_mix_snapshot(
        username,
        req.mix_key,
        mix["title"],
        playlist_title,
        str(getattr(playlist, "ratingKey", "") or "") or None,
        req.auto_refresh,
        created_by=str(current_user["id"]),
    )
    return _snapshot_out(snap)


@router.put("/mixes/snapshots/{snapshot_id}", response_model=MixSnapshot, response_model_exclude_unset=True)
def update_snapshot(
    snapshot_id: str,
    req: MixSnapshotUpdateRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> dict[str, Any]:
    _authorized_snapshot(db, current_user, plex, snapshot_id)
    db.set_mix_snapshot_auto_refresh(snapshot_id, req.auto_refresh)
    updated = db.get_mix_snapshot(snapshot_id)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mix snapshot not found")
    return _snapshot_out(updated)


@router.delete("/mixes/snapshots/{snapshot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_snapshot(
    snapshot_id: str,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> Response:
    _authorized_snapshot(db, current_user, plex, snapshot_id)
    db.delete_mix_snapshot(snapshot_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PlexPlaylistSummary], response_model_exclude_unset=True)
def list_plex_playlists(
    user: Optional[str] = None,
    include_ignored: bool = False,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> list[dict[str, Any]]:
    client, username, server = _context(plex, current_user, user)
    with _plex_errors():
        pairs = client.refresh_playlist_registry(server, username, db)
    out = [_summary(pl, username, row) for pl, row in pairs if include_ignored or not row["ignored"]]
    out.sort(key=lambda s: s["title"].lower())
    return out


@router.get("/{rating_key}/items", response_model=list[PlexPlaylistItem], response_model_exclude_unset=True)
def get_playlist_items(
    rating_key: str,
    user: Optional[str] = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> list[dict[str, Any]]:
    client, _username, server = _context(plex, current_user, user)
    with _plex_errors():
        return _items_response(client, server, rating_key)


@router.patch("/{rating_key}", response_model=PlexPlaylistSummary, response_model_exclude_unset=True)
def rename_playlist(
    rating_key: str,
    req: RenameRequest,
    user: Optional[str] = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> dict[str, Any]:
    client, username, server = _context(plex, current_user, user)
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Title must not be blank")
    pl = _load_playlist(client, server, rating_key)
    with _plex_errors():
        client.rename_playlist(pl, title)
        pl = client.get_playlist(server, rating_key)
    row = _ensure_row(client, db, username, pl)
    return _summary(pl, username, row)


@router.delete("/{rating_key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_playlist(
    rating_key: str,
    user: Optional[str] = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> Response:
    client, username, server = _context(plex, current_user, user)
    pl = _load_playlist(client, server, rating_key)
    with _plex_errors():
        client.delete_playlist(pl)
    db.delete_plex_registry(username, rating_key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{rating_key}/items", response_model=list[PlexPlaylistItem], response_model_exclude_unset=True)
def add_playlist_items(
    rating_key: str,
    req: AddItemsRequest,
    user: Optional[str] = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> list[dict[str, Any]]:
    client, _username, server = _context(plex, current_user, user)
    pl = _load_playlist(client, server, rating_key)
    _require_regular(pl)
    for key in req.track_rating_keys:
        if not key.isdigit():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid track rating key '{key}'"
            )
    with _plex_errors():
        try:
            client.add_tracks_to_playlist(server, pl, req.track_rating_keys)
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=redact_text(str(e))) from e
        return _items_response(client, server, rating_key)


@router.delete("/{rating_key}/items/{playlist_item_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_playlist_item(
    rating_key: str,
    playlist_item_id: int,
    user: Optional[str] = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> Response:
    client, _username, server = _context(plex, current_user, user)
    pl = _load_playlist(client, server, rating_key)
    _require_regular(pl)
    with _plex_errors():
        client.remove_playlist_item(pl, playlist_item_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{rating_key}/items/{playlist_item_id}/move", response_model=list[PlexPlaylistItem], response_model_exclude_unset=True)
def move_playlist_item(
    rating_key: str,
    playlist_item_id: int,
    req: MoveItemRequest,
    user: Optional[str] = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> list[dict[str, Any]]:
    client, _username, server = _context(plex, current_user, user)
    pl = _load_playlist(client, server, rating_key)
    _require_regular(pl)
    with _plex_errors():
        client.move_playlist_item(pl, playlist_item_id, req.after_playlist_item_id)
        return _items_response(client, server, rating_key)


@router.post("/{rating_key}/copy", response_model=list[PlaylistCopyResult], response_model_exclude_unset=True)
def copy_playlist(
    rating_key: str,
    req: CopyRequest,
    user: Optional[str] = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> list[dict[str, Any]]:
    client, _username, server = _context(plex, current_user, user)
    own = _own_username(current_user)
    is_admin = bool(current_user.get("is_admin"))
    if not is_admin and any(t.lower() != own.lower() for t in req.target_users):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only admins may copy playlists into other profiles"
        )
    pl = _load_playlist(client, server, rating_key)
    with _plex_errors():
        source_items = client.get_playlist_items(pl)
    title = (req.title or str(getattr(pl, "title", "") or "")).strip()
    known = _known_usernames(client) if is_admin else {own.lower(): own}

    results: list[dict[str, Any]] = []
    for target in dict.fromkeys(req.target_users):
        canonical = known.get(target.lower())
        if canonical is None:
            results.append(
                {
                    "username": target,
                    "success": False,
                    "error": f"Unknown Plex user '{target}'",
                    "copied_tracks": 0,
                    "omitted_tracks": 0,
                }
            )
            continue
        try:
            created, copied, omitted = client.copy_playlist_to_user(source_items, title, canonical, db)
            results.append(
                {
                    "username": canonical,
                    "success": True,
                    "rating_key": str(getattr(created, "ratingKey", "") or ""),
                    "copied_tracks": copied,
                    "omitted_tracks": omitted,
                }
            )
        except PlaylistProtectedError as e:
            results.append(
                {
                    "username": canonical,
                    "success": False,
                    "error": redact_text(str(e)),
                    "copied_tracks": 0,
                    "omitted_tracks": 0,
                }
            )
        except (PlexApiException, requests.exceptions.RequestException, ValueError) as e:
            error_text = safe_exc(e, safe_types=(ValueError,))
            logger.warning("Copy of playlist %s to '%s' failed: %s", rating_key, canonical, error_text)
            results.append(
                {"username": canonical, "success": False, "error": error_text, "copied_tracks": 0, "omitted_tracks": 0}
            )
    return results


@router.post("/{rating_key}/adopt", response_model=PlaylistRecord, response_model_exclude_unset=True, status_code=status.HTTP_201_CREATED)
def adopt_playlist(
    rating_key: str,
    user: Optional[str] = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> dict[str, Any]:
    client, username, server = _context(plex, current_user, user)
    pl = _load_playlist(client, server, rating_key)
    with _plex_errors():
        items = client.get_playlist_items(pl)
    tracks = [
        {
            "title": str(getattr(i, "title", "") or ""),
            "artist": str(getattr(i, "grandparentTitle", "") or ""),
            "album": str(getattr(i, "parentTitle", "") or ""),
        }
        for i in items
    ]
    title = str(getattr(pl, "title", "") or "")
    playlist_id = f"plex_{username.lower()}_{rating_key}"
    _guard_existing_playlist(db, playlist_id, current_user)
    row = _ensure_row(client, db, username, pl)
    db.upsert_playlist(
        playlist_id,
        name=title,
        service="plex",
        description=str(getattr(pl, "summary", "") or ""),
        creator_id=str(current_user["id"]),
        tracks_json=json.dumps(tracks),
    )
    db.upsert_plex_registry(
        username,
        rating_key,
        title,
        row["kind"],
        row["owner"],
        trackseerr_playlist_id=playlist_id,
        update_owner=False,
    )
    created = db.get_playlist(playlist_id)
    if created is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to adopt playlist")
    created["targets"] = db.get_playlist_targets(playlist_id)
    return created


@router.put("/{rating_key}/flags", response_model=PlexPlaylistSummary, response_model_exclude_unset=True)
def set_playlist_flags(
    rating_key: str,
    req: FlagsRequest,
    user: Optional[str] = None,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
    plex: Optional[PlexClient] = Depends(get_plex_client),
) -> dict[str, Any]:
    client, username, server = _context(plex, current_user, user)
    pl = _load_playlist(client, server, rating_key)
    row = _ensure_row(client, db, username, pl)
    if req.owner is not None and is_smart_playlist(pl):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Ownership of smart playlists cannot be changed"
        )
    row = db.upsert_plex_registry(
        username,
        rating_key,
        str(getattr(pl, "title", "") or ""),
        row["kind"],
        req.owner or row["owner"],
        ignored=req.ignored,
    )
    return _summary(pl, username, row)

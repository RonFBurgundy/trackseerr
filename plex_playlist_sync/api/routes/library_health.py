"""Library health: server-vs-disk findings and weak import matches ("Needs review"). Admin only, core tier.

Server paths reveal the host layout, so none of these endpoints are reachable by non-admins.
"""

import logging
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from plex_playlist_sync import library_health
from plex_playlist_sync.api.dependencies import get_active_media_server, get_db, require_admin, require_core_tier
from plex_playlist_sync.media_servers.base import MediaServer
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin), Depends(require_core_tier)])


class DismissRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    scope: Literal["file", "folder"]


class MappingRequest(BaseModel):
    server_prefix: str = Field(max_length=4096)
    local_prefix: str = Field(min_length=1, max_length=4096)


def _music_root(db: Database) -> Path:
    return Path(db.get_media_management_settings().get("root_folder_path") or "/music")


@router.get("", summary="Library health findings, grouped, with the last run")
def get_library_health(
    db: Database = Depends(get_db),
    server: Optional[MediaServer] = Depends(get_active_media_server),
) -> dict[str, Any]:
    findings = db.list_library_health_findings()
    return {
        "findings": [library_health.finding_view(f) for f in findings],
        "groups": library_health.group_findings(findings),
        "last_run": library_health.run_view(db.get_last_library_health_run()),
        "running": library_health.is_running(),
        "mapping": library_health.mapping_view(db),
        "server": {"kind": server.kind, "file_paths": bool(server.capabilities.file_paths)} if server else None,
        "count": len(findings),
        "weekly": db.get_library_health_weekly(),
    }


@router.get("/count", summary="Number of open findings (nav badge)")
def get_library_health_count(db: Database = Depends(get_db)) -> dict[str, int]:
    return {"count": db.count_library_health_findings()}


@router.post("/check", status_code=status.HTTP_202_ACCEPTED, summary="Start a check in the background")
def start_library_health_check(
    db: Database = Depends(get_db),
    server: Optional[MediaServer] = Depends(get_active_media_server),
) -> Any:
    if not library_health.start_check_async(db, server, music_root=_music_root(db)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A library health check is already running")
    return JSONResponse({"started": True}, status_code=status.HTTP_202_ACCEPTED)


@router.post("/dismiss", summary="Ignore a file or folder from now on")
def dismiss_library_health(body: DismissRequest, db: Database = Depends(get_db)) -> dict[str, Any]:
    path = body.path.rstrip("/") if len(body.path) > 1 else body.path
    db.add_library_health_dismissal(body.scope, path)
    removed = (
        db.delete_library_health_finding_by_path(path)
        if body.scope == "file"
        else db.delete_library_health_findings_under(path)
    )
    return {"dismissed": True, "removed": removed}


@router.put("/mapping", summary="Save the media-server path mapping")
def put_library_health_mapping(body: MappingRequest, db: Database = Depends(get_db)) -> dict[str, Any]:
    kind = ""
    existing = db.get_media_server_path_mapping()
    if existing:
        kind = existing.get("server_kind") or ""
    db.set_media_server_path_mapping(
        {"server_prefix": body.server_prefix, "local_prefix": body.local_prefix, "auto": False, "server_kind": kind}
    )
    return {"mapping": library_health.mapping_view(db)}


@router.delete("/mapping", summary="Clear the media-server path mapping")
def delete_library_health_mapping(db: Database = Depends(get_db)) -> dict[str, Any]:
    db.set_media_server_path_mapping(None)
    return {"mapping": None}


class WeeklyRequest(BaseModel):
    enabled: bool


@router.put("/weekly", summary="Turn the weekly check on or off")
def put_library_health_weekly(body: WeeklyRequest, db: Database = Depends(get_db)) -> dict[str, bool]:
    db.set_library_health_weekly(body.enabled)
    return {"weekly": db.get_library_health_weekly()}

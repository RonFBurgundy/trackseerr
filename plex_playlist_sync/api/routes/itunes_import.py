"""Import an iTunes / Apple Music library export (XML) as Trackseerr playlists. Admin-only, Core tier only.

Two steps: ``POST /preview`` uploads the export (the request body is the file itself, streamed to disk under a size
cap) and parses it; ``POST /{import_id}/commit`` starts a background job that matches and creates the playlists;
``GET /{import_id}/status`` reports progress.
"""

import logging
import os
import tempfile
import threading
from typing import Any, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from plex_playlist_sync import itunes_import as imp
from plex_playlist_sync.api.dependencies import get_config, get_db, require_admin, require_core_tier
from plex_playlist_sync.api.schemas.itunes_import import (
    ItunesCommitResponse,
    ItunesImportStatus,
    ItunesPreviewResponse,
)
from plex_playlist_sync.config import Config
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)

# Parsing a large export (plistlib + _build + json) can take GBs of RAM; only one preview is processed at a time.
_PREVIEW_LOCK = threading.Lock()

router = APIRouter(dependencies=[Depends(require_core_tier), Depends(require_admin)])


class PathMapping(BaseModel):
    from_: str = Field(..., alias="from", min_length=1, max_length=1024)
    to: str = Field(..., min_length=1, max_length=1024)

    model_config = {"populate_by_name": True}


class CommitRequest(BaseModel):
    playlists: Union[Literal["all"], list[str]] = "all"
    path_mappings: list[PathMapping] = Field(default_factory=list, max_length=20)
    monitor_mode: Optional[Literal["track", "album", "artist", "none"]] = Field(
        default=None, description="Defaults to 'none' for new playlists; an existing playlist keeps its mode"
    )
    name_prefix: Optional[str] = Field(default=None, max_length=100)
    include_folders: bool = False
    import_play_stats: bool = False


def _parse_upload(path: str) -> dict[str, Any]:
    try:
        return imp.parse_library(path)
    except imp.ItunesImportError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/preview", response_model=ItunesPreviewResponse, response_model_exclude_unset=True)
async def preview_itunes_import(
    request: Request,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """Body = the raw ``iTunes Library.xml`` bytes. Stores the parsed export for 24 h and returns the preview."""
    limit = imp.max_import_bytes()
    too_big = HTTPException(
        status_code=413,
        detail=f"The file is larger than the {limit // (1024 * 1024)} MB import limit (ITUNES_IMPORT_MAX_MB).",
    )
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise too_big
    if not _PREVIEW_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="an import preview is already being processed")
    try:
        os.makedirs(config.data_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="itunes-upload-", suffix=".xml", dir=config.data_dir)
        try:
            total = 0
            with os.fdopen(fd, "wb") as out:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > limit:
                        raise too_big
                    out.write(chunk)
            if total == 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The upload was empty.")
            parsed = await run_in_threadpool(_parse_upload, tmp_path)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError as exc:
                logger.warning("Could not remove iTunes upload temp file: %s", exc)
        import_id = await run_in_threadpool(imp.save_import, config.data_dir, parsed)
        preview = await run_in_threadpool(imp.build_preview, db, parsed)
    finally:
        _PREVIEW_LOCK.release()
    return {"import_id": import_id, **preview}


@router.post("/{import_id}/commit", response_model=ItunesCommitResponse, response_model_exclude_unset=True, status_code=status.HTTP_202_ACCEPTED)
def commit_itunes_import(
    import_id: str,
    req: CommitRequest,
    current_user: dict[str, Any] = Depends(require_admin),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    parsed = imp.load_import(config.data_dir, import_id)
    if parsed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import not found or expired; upload the file again")
    if req.playlists == "all":
        selected = list(parsed["playlists"])
    else:
        wanted = set(req.playlists)
        selected = [p for p in parsed["playlists"] if p["key"] in wanted]
        unknown = wanted - {p["key"] for p in selected}
        if unknown:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown playlist key(s) in selection")
    if not selected:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No playlists selected")
    job_id = imp.start_job(import_id, selected)
    if job_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This import is already running")
    mappings = [{"from": m.from_, "to": m.to} for m in req.path_mappings]
    worker = threading.Thread(
        target=imp.run_import,
        args=(db, config, import_id, parsed, selected),
        kwargs={
            "mappings": mappings,
            "monitor_mode": req.monitor_mode,
            "name_prefix": req.name_prefix,
            "include_folders": req.include_folders,
            "import_play_stats": req.import_play_stats,
            "creator_id": str(current_user["id"]),
        },
        name=f"itunes-import-{import_id[:8]}",
        daemon=True,
    )
    try:
        worker.start()
    except RuntimeError as exc:
        logger.exception("Could not start the iTunes import thread")
        imp.fail_job(import_id, "Could not start the import worker")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not start the import") from exc
    return {"job_id": job_id}


@router.get("/{import_id}/status", response_model=ItunesImportStatus, response_model_exclude_unset=True)
def itunes_import_status(
    import_id: str,
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    if not imp.valid_import_id(import_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import not found")
    job = imp.job_status(import_id)
    if job is None:
        if imp.load_import(config.data_dir, import_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import not found or expired")
        return {"state": "idle", "job_id": None, "total": 0, "done": 0, "playlists": [], "error": None, "play_stats": None}
    return job

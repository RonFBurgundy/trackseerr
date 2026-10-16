"""Endpoints for token preview, batch renaming, and audio retagging."""

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

import httpx

from trackseerr import art_pipeline
from trackseerr.file_relink import RelinkConflict, relink_file, relink_files
from trackseerr.item_history import emit
from trackseerr.redaction import redact_text, safe_exc
from trackseerr.import_files import (
    prepare_file_for_tagging,
    safe_atomic_move,
    _is_safe_cover_url,
)
from trackseerr.api.dependencies import (
    get_db,
    get_media_client,
    require_admin,
    require_core_tier,
    track_admin_actor,
)
from trackseerr.api.schemas.system import TaskActionResult
from trackseerr.api.schemas.library import (
    AcoustidContributionsResponse,
    RenameApplyResponse,
    RenamePreviewItem,
    RenamePreviewResponse,
    RenameRelinkBatchResponse,
    RenameRelinkBatchResult,
    RenameRelinkConflictResponse,
    RenameRelinkFile,
    RenameRelinkResponse,
    RenameSafetyResponse,
    RenameVerifyStatus,
    RetagApplyResponse,
    RetagPreviewItem,
)
from trackseerr.media_servers import as_media_server
from trackseerr.library import (
    _extract_year,
    build_tags_to_write,
    embed_album_artwork,
    find_folder_art,
    inspect_audio_file,
    resolve_collision,
    write_audio_tags,
)
from trackseerr.track_credits import tag_artist_from_credits
from trackseerr.job_tracker import track_job
from trackseerr.task_manager import TRIGGER_MANUAL, record_task_run
from trackseerr.naming import build_track_path
from trackseerr.artist_folders import resolve_artist_folder
from trackseerr.rename_reasons import (
    AUDIO_MISMATCH,
    COLLISION,
    VERIFIED as VERIFIED_REASON,
    classify_rename,
    flag_collisions,
    is_default_selected,
)
from trackseerr.rename_verification import (
    MISMATCH,
    VERIFIED,
    get_verification_status,
    start_verification,
    verdict_for,
)
from trackseerr.storage import Database


logger = logging.getLogger(__name__)

router = APIRouter()

from ._shared import (validate_media_path, native_only, _album_total_discs, _album_owned_discs)
from .models import (RenamePreviewRequest, RenameApplyRequest, RenameVerifyRequest, RenameRelinkRequest, RenameRelinkBatchRequest, RetagPreviewRequest, RetagApplyRequest)

_ART_STEMS = ("cover", "folder", "front")
_ART_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_RENAME_PAGE_SIZE = 1000


def _cached_artist_folder(
    db: Database,
    artist: dict[str, Any],
    media_settings: dict[str, Any],
    caches: dict[str, dict[Any, Any]],
) -> Optional[str]:
    """Resolves and memoises the artist folder name in caches["artist_folders"]."""
    folders = caches.setdefault("artist_folders", {})
    art_id = artist.get("id") or artist.get("name")
    if art_id not in folders:
        folders[art_id] = resolve_artist_folder(db, artist, media_settings)
    return folders[art_id]


def _rename_meta(
    db: Database,
    track: dict[str, Any],
    album: dict[str, Any],
    artist: dict[str, Any],
    f: dict[str, Any],
    current_path: str,
    caches: dict[str, dict[Any, Any]],
    media_settings: dict[str, Any],
) -> dict[str, Any]:
    """Builds the naming-template metadata for a track's file; discs totals are memoised per album in ``caches``.

    An artist whose folder already exists keeps it (unless ``rename_artist_folders`` is on).
    """
    alb_id = track["album_id"]
    discs = caches.setdefault("discs", {})
    if alb_id not in discs:
        discs[alb_id] = _album_owned_discs(db, alb_id)
    return {
        "artist": artist["name"],
        "album_artist": artist["name"],
        "artist_folder": _cached_artist_folder(db, artist, media_settings, caches),
        "album": album["title"],
        "title": track["title"],
        "track_number": track["track_number"],
        "disc_number": track["disc_number"],
        "total_discs": discs[alb_id],
        "year": album.get("year"),
        "release_year": album.get("year"),
        "codec": f.get("codec"),
        "bitrate": f.get("bitrate"),
        "sample_rate": f.get("sample_rate"),
        "bits_per_sample": f.get("bits_per_sample"),
        "quality_full": f.get("quality_name"),
        "file_path": current_path,
        "extension": Path(current_path).suffix,
    }


def _cached_row(
    cache: dict[Any, Any], key: Any, loader: Any
) -> Optional[dict[str, Any]]:
    """Returns a memoised row for ``key``, loading it once (misses are not cached)."""
    if key not in cache:
        row = loader(key)
        if row:
            cache[key] = row
    return cache.get(key)


def _preview_row(
    db: Database,
    t: dict[str, Any],
    f: Optional[dict[str, Any]],
    media_settings: dict[str, Any],
    caches: dict[str, dict[Any, Any]],
) -> Optional[dict[str, Any]]:
    """Builds one rename-preview row for a track's file, or None when it has no file or no catalog artist/album."""
    if not f:
        return None
    current_path = f.get("file_path")
    if not current_path:
        return None
    artist = _cached_row(caches.setdefault("artists", {}), t["artist_id"], db.get_library_artist)
    album = _cached_row(caches.setdefault("albums", {}), t["album_id"], db.get_library_album)
    if not artist or not album:
        return None
    meta = _rename_meta(db, t, album, artist, f, current_path, caches, media_settings)
    proposed_path = build_track_path(meta, media_settings)
    reasons = classify_rename(
        _root_relative(current_path, media_settings), _root_relative(proposed_path, media_settings)
    )
    return {
        "file_id": f["id"],
        "track_id": t["id"],
        "current_path": current_path,
        "proposed_path": proposed_path,
        "needs_rename": os.path.normpath(current_path) != os.path.normpath(proposed_path),
        "reasons": reasons,
        "default_selected": is_default_selected(reasons),
    }


def _root_relative(path: str, media_settings: dict[str, Any]) -> str:
    """``path`` relative to the library root; a path outside the root keeps its absolute segments."""
    norm = os.path.normpath(path)
    root = os.path.normpath(str(media_settings.get("root_folder_path") or "/music"))
    try:
        return str(Path(norm).relative_to(root))
    except ValueError:
        return norm.lstrip("/")


def _flag_collisions(rows: list[dict[str, Any]]) -> None:
    """Adds the ``collision`` reason (and clears ``default_selected``) on rows that share a proposed path."""
    flags = flag_collisions([os.path.normpath(r["proposed_path"]) for r in rows])
    for row, collides in zip(rows, flags):
        if collides:
            row["reasons"] = [*row["reasons"], COLLISION]
            row["default_selected"] = False


def _attach_verification(db: Database, rows: list[dict[str, Any]]) -> None:
    """Adds the cached AcoustID verdict to rows that have a current cache entry, and adjusts reasons/selection."""
    cached = db.get_file_fingerprints([r["file_id"] for r in rows])
    for row in rows:
        fp_row = cached.get(row["file_id"])
        track = db.get_library_track(row["track_id"]) if fp_row else None
        if not fp_row or not track:
            continue
        verdict = verdict_for(db, fp_row, track, row["current_path"])
        if verdict is None:
            continue
        row["verification"] = verdict
        if verdict["status"] == VERIFIED:
            row["reasons"] = [*row["reasons"], VERIFIED_REASON]
            row["default_selected"] = COLLISION not in row["reasons"]
        elif verdict["status"] == MISMATCH:
            row["reasons"] = [*row["reasons"], AUDIO_MISMATCH]
            row["default_selected"] = False


def _preview_targeted_files(
    db: Database,
    file_ids: list[str],
    media_settings: dict[str, Any],
    caches: dict[str, dict[Any, Any]],
) -> list[dict[str, Any]]:
    """Builds preview rows for a specific list of file IDs, including rows that no longer need a rename."""
    rows: list[dict[str, Any]] = []
    for fid in file_ids:
        f = db.get_library_file(fid)
        if not f:
            continue
        track_id = f.get("track_id")
        t = db.get_library_track(str(track_id)) if track_id else None
        if not t:
            continue
        row = _preview_row(db, t, f, media_settings, caches)
        if row:
            rows.append(row)
    return rows


@router.post("/rename/preview", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=RenamePreviewResponse, response_model_exclude_unset=True)
def rename_preview(
    body: Optional[RenamePreviewRequest] = None,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Previews proposed file path changes from the naming templates.

    Targeted file_ids scope returns rows only for those files, including ones already in place, with total = len(items)
    and truncated = False. Album/artist scope returns up to ``limit`` tracks, including ones already in place, along with
    the total track count and whether the results were truncated. Library-wide scope scans the whole library and returns
    only files that need a move (up to ``limit``), reporting the true total count of files needing a rename and whether
    the results were truncated.
    """
    req = body or RenamePreviewRequest()
    limit = req.limit or 200
    media_settings = db.get_media_management_settings()
    caches: dict[str, dict[Any, Any]] = {}

    if req.file_ids is not None:
        preview_diffs = _preview_targeted_files(db, req.file_ids, media_settings, caches)
        _flag_collisions(preview_diffs)
        _attach_verification(db, preview_diffs)
        return {"items": preview_diffs, "total": len(preview_diffs), "truncated": False}

    preview_diffs = []

    if req.album_id or req.artist_id:
        if req.album_id:
            tracks = db.list_library_tracks(album_id=req.album_id, limit=limit)
        else:
            tracks = db.list_library_tracks(artist_id=req.artist_id, limit=limit)
        for t in tracks:
            row = _preview_row(db, t, db.get_library_file_for_track(t["id"]), media_settings, caches)
            if row:
                preview_diffs.append(row)
        _flag_collisions(preview_diffs)
        _attach_verification(db, preview_diffs)
        total = len(tracks)
        truncated = len(tracks) >= limit
        return {"items": preview_diffs, "total": total, "truncated": truncated}

    total = 0
    for t, f in db.iter_library_files_for_rename(_RENAME_PAGE_SIZE):
        row = _preview_row(db, t, f, media_settings, caches)
        if row and row["needs_rename"]:
            total += 1
            if len(preview_diffs) < limit:
                preview_diffs.append(row)
    _flag_collisions(preview_diffs)
    _attach_verification(db, preview_diffs)
    truncated = total > len(preview_diffs)
    return {"items": preview_diffs, "total": total, "truncated": truncated}



_NO_KEY_MESSAGE = "Set an AcoustID API key in Settings → Media Management first"


@router.post("/rename/verify", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=RenameVerifyStatus)
def rename_verify(
    body: RenameVerifyRequest,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Starts a background AcoustID check of the given library files against the tracks they are linked to."""
    key = str(db.get_media_management_settings().get("acoustid_api_key") or "").strip()
    if not key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_NO_KEY_MESSAGE)
    if not body.file_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No files selected to verify")
    if not start_verification(db, body.file_ids, key):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A verification is already running")
    return {**get_verification_status(), "api_key_set": True}


@router.get("/rename/verify/status", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=RenameVerifyStatus)
def rename_verify_status(
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Reports the progress of the current or last AcoustID verification run."""
    key = str(db.get_media_management_settings().get("acoustid_api_key") or "").strip()
    return {**get_verification_status(), "api_key_set": bool(key)}


@router.get(
    "/acoustid/contributions",
    dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)],
    response_model=AcoustidContributionsResponse,
)
def acoustid_contributions(
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Summary of AcoustID fingerprint contribution submissions and user-key configuration."""
    user_key = str(db.get_media_management_settings().get("acoustid_user_key") or "").strip()
    return {
        "counts": db.count_acoustid_submissions(),
        "has_user_key": bool(user_key),
    }


@router.get("/rename/safety", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=RenameSafetyResponse)
def rename_safety(
    plex_client: Optional[Any] = Depends(get_media_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Reports warnings about media-server settings that could drop play history or ratings during a rename."""
    server = as_media_server(plex_client)
    warnings = server.rename_warnings() if server else []
    return {"warnings": warnings}


@router.post(
    "/rename/relink",
    dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)],
    response_model=RenameRelinkResponse,
    responses={409: {"model": RenameRelinkConflictResponse}},
)
def rename_relink(
    body: RenameRelinkRequest,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> Any:
    """Relinks a file to the track its audio really is; a target that already has a file needs a ``conflict`` choice."""
    try:
        return relink_file(db, body.file_id, body.target_track_id, body.conflict)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except RelinkConflict as exc:
        fields = RenameRelinkFile.model_fields
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={
            "detail": "The right track already has a file \u2014 choose which to keep",
            "existing": {k: exc.existing.get(k) for k in fields},
            "incoming": {k: exc.incoming.get(k) for k in fields},
        })
    except OSError as exc:
        logger.error("Relink of %s failed: %s", body.file_id, safe_exc(exc))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=redact_text(str(exc)))


@router.post(
    "/rename/relink/batch",
    dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)],
    response_model=RenameRelinkBatchResponse,
)
def rename_relink_batch(
    body: RenameRelinkBatchRequest,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> Any:
    """Batch-relinks up to 500 files to their target tracks."""
    return relink_files(db, body.items, body.conflict)


def _is_art_file(p: Path) -> bool:
    return p.is_file() and p.stem.lower() in _ART_STEMS and p.suffix.lower() in _ART_EXTS


def _tidy_old_folder(old_parent: Path, new_parent: Path, root_dir: Path) -> None:
    """After a move, carries lone album artwork to the new folder and removes emptied folders under the root.

    Only moves artwork (never overwrites, never deletes files) and only rmdirs empty directories strictly
    inside ``root_dir``.
    """
    try:
        if old_parent == new_parent or not old_parent.is_dir():
            return
        entries = list(old_parent.iterdir())
        if entries and all(_is_art_file(e) for e in entries):
            for art in entries:
                dest = new_parent / art.name
                if dest.exists() or any(
                    sib.name.lower() == art.name.lower() for sib in new_parent.iterdir()
                ):
                    continue
                safe_atomic_move(art, dest)
        if root_dir not in old_parent.parents or any(old_parent.iterdir()):
            return
        old_parent.rmdir()
        grandparent = old_parent.parent
        if grandparent != root_dir and root_dir in grandparent.parents and not any(grandparent.iterdir()):
            grandparent.rmdir()
    except OSError as exc:
        logger.debug("Could not tidy old folder %s: %s", old_parent, exc)


def _mismatch_block(db: Database, fid: str, track: dict[str, Any], current_path_str: str) -> Optional[str]:
    """An error message when the cached AcoustID verdict says the file's audio is not the track it is linked to."""
    verdict = verdict_for(db, db.get_file_fingerprint(fid), track, current_path_str)
    if not verdict or verdict["status"] != MISMATCH:
        return None
    actual = verdict.get("actual_title") or "a different track"
    return (
        f"Skipped '{Path(current_path_str).name}': the audio is {actual}, "
        f"not '{track.get('title')}' — fix the file link first"
    )


@router.post("/rename/apply", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=RenameApplyResponse, response_model_exclude_unset=True)
def rename_apply(
    body: RenameApplyRequest,
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Applies batch renaming to specified library files, moving them to their template-rendered destinations and updating the catalog."""
    renamed_count = 0
    blocked_count = 0
    errors: list[str] = []

    media_settings = db.get_media_management_settings()
    root_folder_str = media_settings.get("root_folder_path") or "/music"
    root_dir = Path(root_folder_str).resolve()

    for fid in body.file_ids:
        f = db.get_library_file(fid)
        if not f:
            errors.append(f"Library file ID '{fid}' not found")
            continue

        current_path_str = f.get("file_path")
        if not current_path_str:
            errors.append(f"Library file '{fid}' has no recorded file path")
            continue

        try:
            current_path = validate_media_path(current_path_str, db=db)
            if not current_path.exists() or not current_path.is_file():
                errors.append(f"File '{current_path_str}' does not exist on disk")
                continue

            track = db.get_library_track(f["track_id"])
            if not track:
                errors.append(f"Track '{f['track_id']}' not found for file '{fid}'")
                continue

            blocked = _mismatch_block(db, fid, track, current_path_str)
            if blocked:
                errors.append(blocked)
                blocked_count += 1
                continue

            album = db.get_library_album(track["album_id"])
            artist = db.get_library_artist(track["artist_id"])
            if not album or not artist:
                errors.append(f"Album or artist not found for track '{track['id']}'")
                continue

            meta = _rename_meta(db, track, album, artist, f, str(current_path), {}, media_settings)
            proposed = build_track_path(meta, media_settings)
            validate_media_path(proposed, db=db)

            if current_path.resolve() == Path(proposed).resolve():
                continue  # Already matches target format

            target_path = resolve_collision(proposed)
            old_parent = current_path.parent
            new_path = safe_atomic_move(current_path, target_path)

            try:
                rel_path = str(new_path.relative_to(root_dir))
            except ValueError:
                rel_path = new_path.name

            db.upsert_library_file({
                "id": fid,
                "track_id": f["track_id"],
                "file_path": str(new_path),
                "relative_path": rel_path,
                "codec": f.get("codec"),
                "bitrate": f.get("bitrate"),
                "sample_rate": f.get("sample_rate"),
                "bits_per_sample": f.get("bits_per_sample"),
                "quality_name": f.get("quality_name"),
                "size_bytes": new_path.stat().st_size if new_path.exists() else f.get("size_bytes", 0),
                "cutoff_met": f.get("cutoff_met", True),
            })
            renamed_count += 1
            emit(
                db, "renamed" if new_path.parent == old_parent else "moved", track_id=str(f["track_id"]),
                message=f"{current_path.name} -> {new_path.name}",
                details={"from": str(current_path), "to": str(new_path)},
            )

            _tidy_old_folder(old_parent, new_path.parent, root_dir)

        except Exception as exc:
            logger.exception("Failed to rename file ID %s: %s", fid, redact_text(str(exc)))
            errors.append(f"Error renaming file '{fid}': {redact_text(str(exc))}")

    if plex_client:
        try:
            as_media_server(plex_client).refresh_library()
        except Exception as exc:
            logger.warning("Error refreshing media-server library: %s", exc)

    return {"renamed_count": renamed_count, "errors": errors, "blocked_count": blocked_count}

TAG_DIFF_FIELDS: tuple[tuple[str, str], ...] = (
    ("artist", "artist"),
    ("albumartist", "album_artist"),
    ("album", "album"),
    ("title", "title"),
    ("date", "date"),
    ("tracknumber", "track_number"),
    ("totaltracks", "total_tracks"),
    ("discnumber", "disc_number"),
    ("totaldiscs", "total_discs"),
    ("musicbrainz_artistid", "musicbrainz_artistid"),
    ("musicbrainz_albumartistid", "musicbrainz_albumartistid"),
    ("musicbrainz_albumid", "musicbrainz_albumid"),
    ("musicbrainz_releasegroupid", "musicbrainz_releasegroupid"),
    ("musicbrainz_trackid", "musicbrainz_trackid"),
)

VARIOUS_ARTISTS_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"


def _resolve_retag_album_artist_mbid(
    db: Database,
    album: dict[str, Any],
    artist: dict[str, Any],
    artist_cache: Optional[dict[str, dict[str, Any]]] = None,
) -> Optional[str]:
    if str(album.get("album_type") or "").strip().lower() == "compilation":
        return VARIOUS_ARTISTS_MBID
    alb_artist_id = album.get("artist_id")
    if not alb_artist_id or alb_artist_id == artist.get("id"):
        return artist.get("mbid")
    cache = artist_cache if artist_cache is not None else {}
    if alb_artist_id not in cache:
        alb_art = db.get_library_artist(alb_artist_id)
        if alb_art:
            cache[alb_artist_id] = alb_art
    album_artist = cache.get(alb_artist_id) or artist
    return album_artist.get("mbid") if album_artist else None


def _inspect_retag_file(
    current_path_str: str,
    root_dir: Path,
    db: Database,
    write_tags_enabled: bool,
    keep_hardlinks: bool,
    f: dict[str, Any],
) -> tuple[Optional[Path], str, Optional[str], dict[str, Any]]:
    rel_path = f.get("relative_path") or Path(current_path_str).name
    skipped_reason: Optional[str] = None
    current_meta: dict[str, Any] = {}

    try:
        p = validate_media_path(current_path_str, db=db)
    except HTTPException:
        return None, rel_path, "Path is outside the configured media roots", {}

    try:
        rel_path = str(p.relative_to(root_dir))
    except ValueError:
        rel_path = f.get("relative_path") or p.name

    if not p.exists() or not p.is_file():
        return p, rel_path, "File does not exist on disk", {}

    if p.suffix.lower() not in (".flac", ".mp3", ".m4a", ".aac", ".mp4", ".ogg", ".opus"):
        return p, rel_path, f"unsupported format ({p.suffix.lower()})", {}

    try:
        current_meta = inspect_audio_file(p)
    except Exception as exc:
        skipped_reason = f"unreadable: {safe_exc(exc)}"

    if skipped_reason is None and keep_hardlinks:
        try:
            if p.stat().st_nlink > 1:
                skipped_reason = "hardlink keep"
        except OSError as exc:
            skipped_reason = f"unreadable: {safe_exc(exc)}"

    if skipped_reason is None and not write_tags_enabled:
        skipped_reason = "write_audio_tags disabled in media management settings"

    return p, rel_path, skipped_reason, current_meta


def _compute_tag_diffs(
    current_meta: dict[str, Any],
    proposed_tags: dict[str, Any],
) -> list[dict[str, Any]]:
    if not current_meta:
        return []
    diffs: list[dict[str, Any]] = []
    for tag_key, meta_key in TAG_DIFF_FIELDS:
        proposed_val = proposed_tags.get(tag_key)
        if proposed_val is None:
            continue
        current_val = current_meta.get(meta_key)
        if current_val is None and meta_key == "album_artist":
            current_val = current_meta.get("albumartist")
        if tag_key == "date" and current_val is None:
            current_val = current_meta.get("year")
        if _tags_differ(tag_key, current_val, proposed_val):
            diffs.append({
                "field": tag_key,
                "current": _format_diff_val(current_val),
                "proposed": _format_diff_val(proposed_val),
            })
    return diffs


def _resolve_track_artist_and_mbid(
    db: Database,
    track_id: Any,
    title: str,
    media_settings: Optional[dict[str, Any]],
) -> tuple[Optional[str], Optional[str]]:
    m_settings = media_settings if media_settings is not None else db.get_media_management_settings()
    try:
        credits = db.list_track_artists(str(track_id))
    except sqlite3.Error as exc:
        logger.warning("Loading track artists for track %s failed: %s", track_id, exc)
        credits = []
    return tag_artist_from_credits(credits, title, m_settings)


def _evaluate_file_retag(
    db: Database,
    t: dict[str, Any],
    f: dict[str, Any],
    root_dir: Path,
    write_tags_enabled: bool,
    keep_hardlinks: bool,
    artist_cache: dict[str, dict[str, Any]],
    album_cache: dict[str, dict[str, Any]],
    discs_cache: Optional[dict[str, int]] = None,
    media_settings: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    current_path_str = f.get("file_path")
    if not current_path_str:
        return None

    art_id = t["artist_id"]
    if art_id not in artist_cache:
        art = db.get_library_artist(art_id)
        if art:
            artist_cache[art_id] = art
    artist = artist_cache.get(art_id)

    alb_id = t["album_id"]
    if alb_id not in album_cache:
        alb = db.get_library_album(alb_id)
        if alb:
            album_cache[alb_id] = alb
    album = album_cache.get(alb_id)

    if not artist or not album:
        return None

    p, rel_path, skipped_reason, current_meta = _inspect_retag_file(
        current_path_str, root_dir, db, write_tags_enabled, keep_hardlinks, f
    )

    if discs_cache is not None and alb_id in discs_cache:
        total_discs = discs_cache[alb_id]
    else:
        total_discs = _album_total_discs(db, alb_id)
        if discs_cache is not None:
            discs_cache[alb_id] = total_discs

    album_artist_mbid = _resolve_retag_album_artist_mbid(db, album, artist, artist_cache)

    track_artist, track_mbid = _resolve_track_artist_and_mbid(db, t["id"], t["title"], media_settings)

    proposed_tags = build_tags_to_write(
        artist=track_artist or artist["name"],
        album_artist=artist["name"],
        album=album["title"],
        title=t["title"],
        date=album.get("release_date") or album.get("year"),
        track_number=t.get("track_number"),
        total_tracks=album.get("total_tracks"),
        disc_number=t.get("disc_number"),
        total_discs=total_discs,
        musicbrainz_artistid=track_mbid or artist.get("mbid"),
        musicbrainz_albumartistid=album_artist_mbid,
        musicbrainz_albumid=album.get("mb_release_id"),
        musicbrainz_releasegroupid=album.get("mb_release_group_id"),
        musicbrainz_trackid=t.get("mb_recording_id"),
    )

    diffs = _compute_tag_diffs(current_meta, proposed_tags)

    return {
        "file_id": str(f["id"]),
        "track_id": str(t["id"]),
        "path": rel_path,
        "resolved_path": p,
        "changes": diffs,
        "diffs": diffs,
        "skipped_reason": skipped_reason,
        "has_diff": bool(diffs),
        "current_meta": current_meta,
        "proposed_tags": proposed_tags,
        "album": album,
        "artist": artist,
    }

def _album_cover_bytes(album: dict[str, Any], file_path: Optional[Path] = None) -> Optional[bytes]:
    """Retrieves cover art image bytes for an album using cached, local, or remote sources."""
    album_id = str(album.get("id") or "")
    if album_id:
        try:
            cached = art_pipeline.cached_art_path("album", album_id)
            if cached.is_file():
                return cached.read_bytes()
        except OSError as exc:
            logger.debug("Retag cover lookup (cached) failed for album %s: %s", album_id, exc)

    if file_path is not None:
        try:
            folder_art = find_folder_art(file_path.parent)
            if folder_art is not None and folder_art.is_file():
                return folder_art.read_bytes()
        except OSError as exc:
            logger.debug("Retag cover lookup (folder) failed for album %s: %s", album_id, exc)

    album_path = album.get("path")
    if album_path:
        try:
            local = art_pipeline.local_folder_art("album", str(album_path))
            if local is not None and local.is_file():
                return local.read_bytes()
        except OSError as exc:
            logger.debug("Retag cover lookup (local) failed for album %s: %s", album_id, exc)

    cover_url = album.get("cover_url")
    if cover_url and _is_safe_cover_url(cover_url):
        try:
            resp = httpx.get(cover_url, timeout=5.0, follow_redirects=True)
            if resp.status_code == 200 and resp.content:
                return resp.content
        except Exception as exc:
            logger.debug("Failed fetching cover art for retag from %s: %s", cover_url, exc)

    return None

def _tags_differ(field: str, current_val: Any, proposed_val: Any) -> bool:
    """Returns True if current and proposed tag values differ."""
    if proposed_val is None:
        return False
    if proposed_val == "" and (current_val is None or current_val == ""):
        return False
    if current_val is None:
        return True

    if field in ("tracknumber", "totaltracks", "discnumber", "totaldiscs"):
        try:
            return int(current_val) != int(proposed_val)
        except (ValueError, TypeError):
            return str(current_val).strip() != str(proposed_val).strip()

    if field == "date":
        c_str = str(current_val).strip()
        p_str = str(proposed_val).strip()
        if c_str == p_str:
            return False
        c_year = _extract_year(c_str)
        p_year = _extract_year(p_str)
        if c_year is not None and p_year is not None and c_year == p_year:
            if len(c_str) == 4 or len(p_str) == 4:
                return False
        return True

    return str(current_val).strip() != str(proposed_val).strip()

def _format_diff_val(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None

@router.post(
    "/retag/preview",
    dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)],
    response_model=list[RetagPreviewItem],
    response_model_exclude_unset=True,
)
def retag_preview(  # noqa: C901, PLR0915
    body: Optional[RetagPreviewRequest] = None,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Previews proposed tag changes per library file (current vs. new for each differing field)."""
    req = body or RetagPreviewRequest()
    limit = min(max(req.limit or 200, 1), 200)
    offset = max(req.offset or 0, 0)

    if req.album_id:
        tracks = db.list_library_tracks(album_id=req.album_id, limit=limit, offset=offset)
    elif req.artist_id:
        tracks = db.list_library_tracks(artist_id=req.artist_id, limit=limit, offset=offset)
    else:
        tracks = db.list_library_tracks(limit=limit, offset=offset)

    media_settings = db.get_media_management_settings()
    root_folder_str = media_settings.get("root_folder_path") or "/music"
    root_dir = Path(root_folder_str).resolve()
    write_tags_enabled = bool(media_settings.get("write_audio_tags", True))
    keep_hardlinks = str(media_settings.get("torrent_hardlink_tags") or "copy_and_tag") == "keep_hardlink"

    artist_cache: dict[str, dict[str, Any]] = {}
    album_cache: dict[str, dict[str, Any]] = {}
    discs_cache: dict[str, int] = {}

    preview_items: list[dict[str, Any]] = []

    for t in tracks:
        f = db.get_library_file_for_track(t["id"])
        if not f:
            continue

        res = _evaluate_file_retag(
            db=db,
            t=t,
            f=f,
            root_dir=root_dir,
            write_tags_enabled=write_tags_enabled,
            keep_hardlinks=keep_hardlinks,
            artist_cache=artist_cache,
            album_cache=album_cache,
            discs_cache=discs_cache,
            media_settings=media_settings,
        )
        if not res:
            continue

        # Files with no differences omitted; plus skipped_reason when the file will be skipped
        if res["diffs"] or (not res["current_meta"] and res["skipped_reason"] is not None):
            preview_items.append({
                "file_id": res["file_id"],
                "track_id": res["track_id"],
                "path": res["path"],
                "changes": res["diffs"],
                "diffs": res["diffs"],
                "skipped_reason": res["skipped_reason"],
            })

    return preview_items


@dataclass
class _RetagRun:
    db: Database
    plex_client: Optional[Any]
    body: RetagApplyRequest
    media_settings: dict[str, Any]
    write_tags_setting: bool
    embed_art_requested: bool
    total: int
    results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    retagged_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    album_retagged_files: dict[str, list[str]] = field(default_factory=dict)


def _update_retag_progress(
    run: _RetagRun,
    idx: int,
    job_handle: Optional[Any] = None,
    run_handle: Optional[Any] = None,
) -> None:
    if job_handle is not None and (idx % 5 == 0 or idx == run.total - 1):
        msg = f"Retagging: {idx + 1}/{run.total} processed ({run.retagged_count} retagged, {run.skipped_count} skipped, {run.error_count} errors)"
        job_handle.update_message(msg)
        if run_handle is not None:
            run_handle.message = msg


def _record_retag_error(run: _RetagRun, fid: str, err: str) -> None:
    run.results.append({"file_id": fid, "status": "error", "message": err, "reason": err})
    run.errors.append(err)
    run.error_count += 1


def _record_retag_skip(run: _RetagRun, fid: str, skip_msg: str) -> None:
    run.results.append({"file_id": fid, "status": "skipped", "message": skip_msg, "reason": skip_msg})
    run.skipped_count += 1


def _retag_one_file(
    run: _RetagRun,
    idx: int,
    fid: str,
    job_handle: Optional[Any] = None,
    run_handle: Optional[Any] = None,
) -> None:
    _update_retag_progress(run, idx, job_handle, run_handle)

    f = run.db.get_library_file(fid)
    if not f:
        _record_retag_error(run, fid, f"Library file ID '{fid}' not found")
        return

    current_path_str = f.get("file_path")
    if not current_path_str:
        _record_retag_error(run, fid, f"Library file '{fid}' has no recorded file path")
        return

    try:
        p = validate_media_path(current_path_str, db=run.db)
        if not p.exists() or not p.is_file():
            _record_retag_error(run, fid, f"File '{current_path_str}' does not exist on disk")
            return

        if p.suffix.lower() not in (".flac", ".mp3", ".m4a", ".aac", ".mp4", ".ogg", ".opus"):
            _record_retag_skip(run, fid, f"unsupported format ({p.suffix.lower()})")
            return

        track = run.db.get_library_track(f["track_id"])
        if not track:
            _record_retag_error(run, fid, f"Track '{f['track_id']}' not found for file '{fid}'")
            return

        album = run.db.get_library_album(track["album_id"])
        artist = run.db.get_library_artist(track["artist_id"])
        if not album or not artist:
            _record_retag_error(run, fid, f"Album or artist not found for track '{track['id']}'")
            return

        # Hard rule: Seeding safety check
        if not prepare_file_for_tagging(p, run.media_settings):
            _record_retag_skip(run, fid, "hardlink keep")
            return

        # Check write tags vs embed art settings
        if not run.write_tags_setting and not run.embed_art_requested:
            _record_retag_skip(run, fid, "write_audio_tags disabled in media management settings")
            return

        total_discs = _album_total_discs(run.db, track["album_id"])
        album_artist_mbid = _resolve_retag_album_artist_mbid(run.db, album, artist)

        track_artist, track_mbid = _resolve_track_artist_and_mbid(
            run.db, track["id"], track["title"], run.media_settings
        )

        tags_to_write = build_tags_to_write(
            artist=track_artist or artist["name"],
            album_artist=artist["name"],
            album=album["title"],
            title=track["title"],
            date=album.get("release_date") or album.get("year"),
            track_number=track.get("track_number"),
            total_tracks=album.get("total_tracks"),
            disc_number=track.get("disc_number"),
            total_discs=total_discs,
            musicbrainz_artistid=track_mbid or artist.get("mbid"),
            musicbrainz_albumartistid=album_artist_mbid,
            musicbrainz_albumid=album.get("mb_release_id"),
            musicbrainz_releasegroupid=album.get("mb_release_group_id"),
            musicbrainz_trackid=track.get("mb_recording_id"),
        )

        cover_bytes = _album_cover_bytes(album, file_path=p) if run.embed_art_requested else None

        if not run.write_tags_setting and run.embed_art_requested and not cover_bytes:
            _record_retag_skip(run, fid, "no album cover art available to embed")
            return

        ok = False
        if run.write_tags_setting:
            ok = write_audio_tags(p, tags=tags_to_write, cover_art_bytes=cover_bytes)
        elif run.embed_art_requested and cover_bytes:
            ok = embed_album_artwork(p, cover_bytes)

        if not ok:
            err = f"Failed to write audio tags to {p.name}"
            logger.warning("Retag apply failed for file %s: write_audio_tags returned False", p)
            _record_retag_error(run, fid, err)
            return

        # Update size on catalog file
        try:
            run.db.upsert_library_file({
                "id": fid,
                "track_id": f["track_id"],
                "file_path": str(p),
                "relative_path": f.get("relative_path"),
                "codec": f.get("codec"),
                "bitrate": f.get("bitrate"),
                "sample_rate": f.get("sample_rate"),
                "bits_per_sample": f.get("bits_per_sample"),
                "quality_name": f.get("quality_name"),
                "size_bytes": p.stat().st_size if p.exists() else f.get("size_bytes", 0),
                "cutoff_met": f.get("cutoff_met", True),
            })
        except Exception as exc:
            logger.warning("Could not update library file stats after retag for %s: %s", fid, exc)

        run.album_retagged_files.setdefault(str(album["id"]), []).append(str(p))
        run.retagged_count += 1
        run.results.append({"file_id": fid, "status": "ok", "message": None, "reason": None})

    except Exception as exc:
        logger.exception("Failed to retag file ID %s (%s): %s", fid, current_path_str, redact_text(str(exc)))
        _record_retag_error(run, fid, f"Error retagging file '{fid}': {redact_text(str(exc))}")


def _finish_retag(
    run: _RetagRun,
    run_handle: Optional[Any] = None,
    job_handle: Optional[Any] = None,
) -> dict[str, Any]:
    # Emit an item-history event per album
    for alb_id, file_paths in run.album_retagged_files.items():
        emit(
            run.db,
            "retagged",
            album_id=str(alb_id),
            message=f"Retagged {len(file_paths)} file(s)",
            details={"count": len(file_paths), "files": file_paths},
        )

    if run.plex_client and run.retagged_count > 0:
        try:
            as_media_server(run.plex_client).refresh_library()
        except Exception as exc:
            logger.warning("Error refreshing media-server library after retag: %s", exc)

    summary = f"Retagged {run.retagged_count} file(s), {run.skipped_count} skipped, {run.error_count} error(s)"
    if job_handle is not None:
        job_handle.message = summary
    if run_handle is not None:
        run_handle.message = summary

    return {
        "results": run.results,
        "retagged_count": run.retagged_count,
        "applied_count": run.retagged_count,
        "skipped_count": run.skipped_count,
        "error_count": run.error_count,
        "errors": run.errors,
    }


@router.post(
    "/retag/apply",
    dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)],
    response_model=RetagApplyResponse,
    response_model_exclude_unset=True,
)
def retag_apply(
    body: RetagApplyRequest,
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Applies bulk tag changes to specified library files, recomputing proposed tags server-side."""
    file_ids = body.file_ids
    if len(file_ids) > 500:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot apply retag to more than 500 files at once",
        )

    if not file_ids:
        return {
            "results": [],
            "retagged_count": 0,
            "applied_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "errors": [],
        }

    def _execute_apply(job_handle: Optional[Any] = None, run_handle: Optional[Any] = None) -> dict[str, Any]:
        media_settings = db.get_media_management_settings()
        run = _RetagRun(
            db=db,
            plex_client=plex_client,
            body=body,
            media_settings=media_settings,
            write_tags_setting=bool(media_settings.get("write_audio_tags", True)),
            embed_art_requested=bool(body.embed_art),
            total=len(file_ids),
        )

        for idx, fid in enumerate(file_ids):
            _retag_one_file(run, idx, fid, job_handle, run_handle)

        return _finish_retag(run, run_handle, job_handle)

    if len(file_ids) > 50:
        with track_job("bulk_retag", "Bulk Retag") as job, record_task_run(db, "bulk_retag", TRIGGER_MANUAL) as run:
            return _execute_apply(job_handle=job, run_handle=run)
    else:
        return _execute_apply()


def retag_library(
    db: Database,
    plex_client: Optional[Any] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, Optional[int]], None]] = None,
) -> dict[str, int]:
    """Iterates through all library files, computes tag diffs, and writes tags for files that differ from the catalog."""
    media_settings = db.get_media_management_settings()
    write_tags_enabled = bool(media_settings.get("write_audio_tags", True))
    if not write_tags_enabled:
        logger.info("write_audio_tags disabled in media management settings; skipping library retag")
        return {"scanned": 0, "retagged": 0, "unchanged": 0, "skipped": 0, "errors": 0}

    total: Optional[int] = None
    if on_progress is not None:
        total = db.count_library_files_with_track()
        on_progress(0, total)

    root_folder_str = media_settings.get("root_folder_path") or "/music"
    root_dir = Path(root_folder_str).resolve()
    keep_hardlinks = str(media_settings.get("torrent_hardlink_tags") or "copy_and_tag") == "keep_hardlink"

    artist_cache: dict[str, dict[str, Any]] = {}
    album_cache: dict[str, dict[str, Any]] = {}
    discs_cache: dict[str, int] = {}

    scanned = 0
    retagged = 0
    unchanged = 0
    skipped = 0
    errors = 0

    run = _RetagRun(
        db=db,
        plex_client=plex_client,
        body=RetagApplyRequest(file_ids=[], embed_art=False),
        media_settings=media_settings,
        write_tags_setting=True,
        embed_art_requested=False,
        total=0,
    )

    for t, f in db.iter_library_files_for_rename(1000):
        if should_stop is not None and should_stop():
            break

        scanned += 1
        if on_progress is not None and scanned % 100 == 0:
            on_progress(scanned, total)

        res = _evaluate_file_retag(
            db=db,
            t=t,
            f=f,
            root_dir=root_dir,
            write_tags_enabled=write_tags_enabled,
            keep_hardlinks=keep_hardlinks,
            artist_cache=artist_cache,
            album_cache=album_cache,
            discs_cache=discs_cache,
            media_settings=media_settings,
        )

        if not res:
            skipped += 1
            continue

        if res["skipped_reason"]:
            skipped += 1
            continue

        if not res["has_diff"]:
            unchanged += 1
            continue

        prev_retagged = run.retagged_count
        prev_skipped = run.skipped_count
        prev_errors = run.error_count

        _retag_one_file(run, scanned - 1, str(f["id"]))

        if run.retagged_count > prev_retagged:
            retagged += 1
        elif run.error_count > prev_errors:
            errors += 1
        elif run.skipped_count > prev_skipped:
            skipped += 1

    _finish_retag(run)

    if on_progress is not None:
        on_progress(scanned, total)

    return {
        "scanned": scanned,
        "retagged": retagged,
        "unchanged": unchanged,
        "skipped": skipped,
        "errors": errors,
    }


@router.post(
    "/retag/library",
    dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)],
    response_model=TaskActionResult,
    response_model_exclude_unset=True,
)
def retag_library_endpoint(
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Triggers whole-library retag as a background task."""
    from trackseerr.api.routes.system.tasks import trigger_library_retag

    return trigger_library_retag(db, plex_client)




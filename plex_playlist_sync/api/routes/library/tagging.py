"""Endpoints for token preview, batch renaming, and audio retagging."""

import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status

import httpx

from plex_playlist_sync import art_pipeline
from plex_playlist_sync.item_history import emit
from plex_playlist_sync.redaction import redact_text, safe_exc
from plex_playlist_sync.acquisition_worker import (
    prepare_file_for_tagging,
    safe_atomic_move,
    _is_safe_cover_url,
)
from plex_playlist_sync.api.dependencies import (
    get_db,
    get_media_client,
    require_admin,
    require_core_tier,
    track_admin_actor,
)
from plex_playlist_sync.api.schemas.library import (
    RenameApplyResponse,
    RenamePreviewItem,
    RetagApplyResponse,
    RetagPreviewItem,
)
from plex_playlist_sync.media_servers import as_media_server
from plex_playlist_sync.library import (
    _extract_year,
    build_tags_to_write,
    embed_album_artwork,
    find_folder_art,
    inspect_audio_file,
    resolve_collision,
    write_audio_tags,
)
from plex_playlist_sync.job_tracker import track_job
from plex_playlist_sync.task_manager import TRIGGER_MANUAL, record_task_run
from plex_playlist_sync.naming import build_track_path
from plex_playlist_sync.storage import Database


logger = logging.getLogger(__name__)

router = APIRouter()

from ._shared import (validate_media_path, native_only, _album_total_discs)
from .models import (RenamePreviewRequest, RenameApplyRequest, RetagPreviewRequest, RetagApplyRequest)

@router.post("/rename/preview", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=list[RenamePreviewItem], response_model_exclude_unset=True)
def rename_preview(
    body: Optional[RenamePreviewRequest] = None,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Previews proposed file path changes based on token naming templates and identifies files needing renaming."""
    req = body or RenamePreviewRequest()
    limit = req.limit or 200

    if req.album_id:
        tracks = db.list_library_tracks(album_id=req.album_id, limit=limit)
    elif req.artist_id:
        tracks = db.list_library_tracks(artist_id=req.artist_id, limit=limit)
    else:
        tracks = db.list_library_tracks(limit=limit)

    media_settings = db.get_media_management_settings()
    preview_diffs: list[dict[str, Any]] = []

    artist_cache: dict[str, dict[str, Any]] = {}
    album_cache: dict[str, dict[str, Any]] = {}

    for t in tracks:
        f = db.get_library_file_for_track(t["id"])
        if not f:
            continue
        current_path = f.get("file_path")
        if not current_path:
            continue

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
            continue

        meta = {
            "artist": artist["name"],
            "album_artist": artist["name"],
            "album": album["title"],
            "title": t["title"],
            "track_number": t["track_number"],
            "disc_number": t["disc_number"],
            "total_discs": _album_total_discs(db, alb_id),
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
        proposed_path = build_track_path(meta, media_settings)
        needs_rename = Path(current_path).resolve() != Path(proposed_path).resolve()
        preview_diffs.append({
            "file_id": f["id"],
            "track_id": t["id"],
            "current_path": current_path,
            "proposed_path": proposed_path,
            "needs_rename": needs_rename,
        })

    return preview_diffs

@router.post("/rename/apply", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=RenameApplyResponse, response_model_exclude_unset=True)
def rename_apply(
    body: RenameApplyRequest,
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Applies batch renaming to specified library files, moving them to their template-rendered destinations and updating the catalog."""
    renamed_count = 0
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

            album = db.get_library_album(track["album_id"])
            artist = db.get_library_artist(track["artist_id"])
            if not album or not artist:
                errors.append(f"Album or artist not found for track '{track['id']}'")
                continue

            meta = {
                "artist": artist["name"],
                "album_artist": artist["name"],
                "album": album["title"],
                "title": track["title"],
                "track_number": track["track_number"],
                "disc_number": track["disc_number"],
                "total_discs": _album_total_discs(db, track["album_id"]),
                "year": album.get("year"),
                "release_year": album.get("year"),
                "codec": f.get("codec"),
                "bitrate": f.get("bitrate"),
                "sample_rate": f.get("sample_rate"),
                "bits_per_sample": f.get("bits_per_sample"),
                "quality_full": f.get("quality_name"),
                "file_path": str(current_path),
                "extension": current_path.suffix,
            }
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

            # Clean up empty parent folder if no other files remain
            try:
                if old_parent.exists() and not any(old_parent.iterdir()):
                    old_parent.rmdir()
            except OSError:
                pass

        except Exception as exc:
            logger.exception("Failed to rename file ID %s: %s", fid, redact_text(str(exc)))
            errors.append(f"Error renaming file '{fid}': {redact_text(str(exc))}")

    if plex_client:
        try:
            as_media_server(plex_client).refresh_library()
        except Exception as exc:
            logger.warning("Error refreshing media-server library: %s", exc)

    return {"renamed_count": renamed_count, "errors": errors}

TAG_DIFF_FIELDS: tuple[tuple[str, str], ...] = (
    ("artist", "artist"),
    ("album", "album"),
    ("title", "title"),
    ("date", "date"),
    ("tracknumber", "track_number"),
    ("totaltracks", "total_tracks"),
    ("discnumber", "disc_number"),
    ("totaldiscs", "total_discs"),
    ("musicbrainz_artistid", "musicbrainz_artistid"),
    ("musicbrainz_albumid", "musicbrainz_albumid"),
    ("musicbrainz_releasegroupid", "musicbrainz_releasegroupid"),
    ("musicbrainz_trackid", "musicbrainz_trackid"),
)

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
def retag_preview(
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

    preview_items: list[dict[str, Any]] = []

    for t in tracks:
        f = db.get_library_file_for_track(t["id"])
        if not f:
            continue
        current_path_str = f.get("file_path")
        if not current_path_str:
            continue

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
            continue

        rel_path = f.get("relative_path") or Path(current_path_str).name
        skipped_reason: Optional[str] = None
        current_meta: dict[str, Any] = {}

        try:
            p = validate_media_path(current_path_str, db=db)
        except HTTPException:
            p = None
            skipped_reason = "Path is outside the configured media roots"

        if p is not None:
            try:
                rel_path = str(p.relative_to(root_dir))
            except ValueError:
                rel_path = f.get("relative_path") or p.name

            if not p.exists() or not p.is_file():
                skipped_reason = "File does not exist on disk"
            elif p.suffix.lower() not in (".flac", ".mp3", ".m4a", ".aac", ".mp4", ".ogg", ".opus"):
                skipped_reason = f"unsupported format ({p.suffix.lower()})"
            else:
                try:
                    current_meta = inspect_audio_file(p)
                except Exception as exc:
                    skipped_reason = f"unreadable: {safe_exc(exc)}"

            if skipped_reason is None:
                if keep_hardlinks:
                    try:
                        if p.stat().st_nlink > 1:
                            skipped_reason = "hardlink keep"
                    except OSError as exc:
                        skipped_reason = f"unreadable: {safe_exc(exc)}"
                if skipped_reason is None and not write_tags_enabled:
                    skipped_reason = "write_audio_tags disabled in media management settings"

        # Compute proposed tags
        total_discs = _album_total_discs(db, alb_id)
        proposed_tags = build_tags_to_write(
            artist=artist["name"],
            album=album["title"],
            title=t["title"],
            date=album.get("release_date") or album.get("year"),
            track_number=t.get("track_number"),
            total_tracks=album.get("total_tracks"),
            disc_number=t.get("disc_number"),
            total_discs=total_discs,
            musicbrainz_artistid=artist.get("mbid"),
            musicbrainz_albumid=album.get("mb_release_id"),
            musicbrainz_releasegroupid=album.get("mb_release_group_id"),
            musicbrainz_trackid=t.get("mb_recording_id"),
        )

        diffs: list[dict[str, Any]] = []
        if current_meta:
            for tag_key, meta_key in TAG_DIFF_FIELDS:
                proposed_val = proposed_tags.get(tag_key)
                if proposed_val is None:
                    continue
                current_val = current_meta.get(meta_key)
                if tag_key == "date" and current_val is None:
                    current_val = current_meta.get("year")
                if _tags_differ(tag_key, current_val, proposed_val):
                    diffs.append({
                        "field": tag_key,
                        "current": _format_diff_val(current_val),
                        "proposed": _format_diff_val(proposed_val),
                    })

        # Files with no differences omitted; plus skipped_reason when the file will be skipped
        if diffs or (not current_meta and skipped_reason is not None):
            preview_items.append({
                "file_id": str(f["id"]),
                "track_id": str(t["id"]),
                "path": rel_path,
                "changes": diffs,
                "diffs": diffs,
                "skipped_reason": skipped_reason,
            })

    return preview_items

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
        write_tags_setting = bool(media_settings.get("write_audio_tags", True))
        embed_art_requested = bool(body.embed_art)

        results: list[dict[str, Any]] = []
        errors: list[str] = []
        retagged_count = 0
        skipped_count = 0
        error_count = 0

        album_retagged_files: dict[str, list[str]] = {}
        total = len(file_ids)

        for idx, fid in enumerate(file_ids):
            if job_handle is not None and (idx % 5 == 0 or idx == total - 1):
                msg = f"Retagging: {idx + 1}/{total} processed ({retagged_count} retagged, {skipped_count} skipped, {error_count} errors)"
                job_handle.update_message(msg)
                if run_handle is not None:
                    run_handle.message = msg

            f = db.get_library_file(fid)
            if not f:
                err = f"Library file ID '{fid}' not found"
                results.append({"file_id": fid, "status": "error", "message": err, "reason": err})
                errors.append(err)
                error_count += 1
                continue

            current_path_str = f.get("file_path")
            if not current_path_str:
                err = f"Library file '{fid}' has no recorded file path"
                results.append({"file_id": fid, "status": "error", "message": err, "reason": err})
                errors.append(err)
                error_count += 1
                continue

            try:
                p = validate_media_path(current_path_str, db=db)
                if not p.exists() or not p.is_file():
                    err = f"File '{current_path_str}' does not exist on disk"
                    results.append({"file_id": fid, "status": "error", "message": err, "reason": err})
                    errors.append(err)
                    error_count += 1
                    continue

                if p.suffix.lower() not in (".flac", ".mp3", ".m4a", ".aac", ".mp4", ".ogg", ".opus"):
                    skip_msg = f"unsupported format ({p.suffix.lower()})"
                    results.append({"file_id": fid, "status": "skipped", "message": skip_msg, "reason": skip_msg})
                    skipped_count += 1
                    continue

                track = db.get_library_track(f["track_id"])
                if not track:
                    err = f"Track '{f['track_id']}' not found for file '{fid}'"
                    results.append({"file_id": fid, "status": "error", "message": err, "reason": err})
                    errors.append(err)
                    error_count += 1
                    continue

                album = db.get_library_album(track["album_id"])
                artist = db.get_library_artist(track["artist_id"])
                if not album or not artist:
                    err = f"Album or artist not found for track '{track['id']}'"
                    results.append({"file_id": fid, "status": "error", "message": err, "reason": err})
                    errors.append(err)
                    error_count += 1
                    continue

                # Hard rule: Seeding safety check
                # Call prepare_file_for_tagging(path, media_settings) and skip if False
                if not prepare_file_for_tagging(p, media_settings):
                    skip_msg = "hardlink keep"
                    results.append({"file_id": fid, "status": "skipped", "message": skip_msg, "reason": skip_msg})
                    skipped_count += 1
                    continue

                # Check write tags vs embed art settings
                if not write_tags_setting and not embed_art_requested:
                    skip_msg = "write_audio_tags disabled in media management settings"
                    results.append({"file_id": fid, "status": "skipped", "message": skip_msg, "reason": skip_msg})
                    skipped_count += 1
                    continue

                # Recompute proposed tags server-side (never trust client values)
                total_discs = _album_total_discs(db, track["album_id"])
                tags_to_write = build_tags_to_write(
                    artist=artist["name"],
                    album=album["title"],
                    title=track["title"],
                    date=album.get("release_date") or album.get("year"),
                    track_number=track.get("track_number"),
                    total_tracks=album.get("total_tracks"),
                    disc_number=track.get("disc_number"),
                    total_discs=total_discs,
                    musicbrainz_artistid=artist.get("mbid"),
                    musicbrainz_albumid=album.get("mb_release_id"),
                    musicbrainz_releasegroupid=album.get("mb_release_group_id"),
                    musicbrainz_trackid=track.get("mb_recording_id"),
                )

                cover_bytes = _album_cover_bytes(album, file_path=p) if embed_art_requested else None

                if not write_tags_setting and embed_art_requested and not cover_bytes:
                    skip_msg = "no album cover art available to embed"
                    results.append({"file_id": fid, "status": "skipped", "message": skip_msg, "reason": skip_msg})
                    skipped_count += 1
                    continue

                ok = False
                if write_tags_setting:
                    ok = write_audio_tags(p, tags=tags_to_write, cover_art_bytes=cover_bytes)
                elif embed_art_requested and cover_bytes:
                    ok = embed_album_artwork(p, cover_bytes)

                if not ok:
                    err = f"Failed to write audio tags to {p.name}"
                    logger.warning("Retag apply failed for file %s: write_audio_tags returned False", p)
                    results.append({"file_id": fid, "status": "error", "message": err, "reason": err})
                    errors.append(err)
                    error_count += 1
                    continue

                # Update size on catalog file
                try:
                    db.upsert_library_file({
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

                album_retagged_files.setdefault(str(album["id"]), []).append(str(p))
                retagged_count += 1
                results.append({"file_id": fid, "status": "ok", "message": None, "reason": None})

            except Exception as exc:
                logger.exception("Failed to retag file ID %s (%s): %s", fid, current_path_str, redact_text(str(exc)))
                err = f"Error retagging file '{fid}': {redact_text(str(exc))}"
                results.append({"file_id": fid, "status": "error", "message": err, "reason": err})
                errors.append(err)
                error_count += 1

        # Emit an item-history event per album
        for alb_id, file_paths in album_retagged_files.items():
            emit(
                db,
                "retagged",
                album_id=str(alb_id),
                message=f"Retagged {len(file_paths)} file(s)",
                details={"count": len(file_paths), "files": file_paths},
            )

        if plex_client and retagged_count > 0:
            try:
                as_media_server(plex_client).refresh_library()
            except Exception as exc:
                logger.warning("Error refreshing media-server library after retag: %s", exc)

        summary = f"Retagged {retagged_count} file(s), {skipped_count} skipped, {error_count} error(s)"
        if job_handle is not None:
            job_handle.message = summary
        if run_handle is not None:
            run_handle.message = summary

        return {
            "results": results,
            "retagged_count": retagged_count,
            "applied_count": retagged_count,
            "skipped_count": skipped_count,
            "error_count": error_count,
            "errors": errors,
        }

    if len(file_ids) > 50:
        with track_job("bulk_retag", "Bulk Retag") as job, record_task_run(db, "bulk_retag", TRIGGER_MANUAL) as run:
            return _execute_apply(job_handle=job, run_handle=run)
    else:
        return _execute_apply()


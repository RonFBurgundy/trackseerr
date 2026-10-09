"""Endpoints for manual import scanning, track matching, and commit pipeline."""

import sqlite3
import logging
import os
from pathlib import Path
from typing import Any, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status


from trackseerr import delay_gate
from trackseerr.acquisition_coordinator import _to_quality_profile
from trackseerr.download_roots import allowed_roots_for_all_clients
from trackseerr.item_history import TRIGGER_MANUAL_IMPORT, GrabTrigger, emit, set_provenance
from trackseerr.redaction import redact_text
from trackseerr.clients.acquisition import get_acquisition_driver, is_torrent_driver_type
from trackseerr.acquisition_import import (
    record_import_events,
)
from trackseerr.import_files import (
    effective_import_mode,
    place_audio_file,
    prepare_file_for_tagging,
    preserves_source,
)
from trackseerr.seed_safety import (
    seed_action,
    settle_transfer_after_import,
)
from trackseerr.track_matching import (
    MATCH_NONE,
    MATCH_STRONG,
    reconcile_audio_file_to_track_scored,
    resolve_download_expected_tracks,
)
from trackseerr.api.dependencies import (
    get_db,
    get_media_client,
    require_admin,
    require_core_tier,
    track_admin_actor,
)
from trackseerr.api.schemas.library import (
    FingerprintResponse,
    ManualImportCandidateTrack,
    ManualImportCommitResponse,
    ManualImportScanItem,
)
from trackseerr.media_servers import as_media_server
from trackseerr.library_monitoring import (
    DEFAULT_MONITOR_OPTION,
)
from trackseerr.models import (
    DownloadStatus,
)
from trackseerr.library import (
    AUDIO_EXTENSIONS,
    fingerprint_audio_file,
    inspect_audio_file,
    parse_filename_track,
    resolve_album_artist,
    resolve_collision,
    write_audio_tags,
)
from trackseerr.naming import build_track_path
from trackseerr.quality import evaluate_release, parse_release_title
from trackseerr.recycle_bin import (
    is_system_dirname,
    is_system_filename,
    log_recycled,
    recycle_in_place_target,
    recycle_replaced_files,
    restore_recycled,
)
from trackseerr.storage import Database, clean_library_name


logger = logging.getLogger(__name__)

router = APIRouter()

from ._shared import (validate_media_path, native_only, _album_total_discs)
from .models import (ManualImportScanRequest, ManualImportCommitRequest, FingerprintRequest)

def _scan_fallback_tags(p: Path) -> dict[str, Any]:
    return {
        "title": p.stem,
        "artist": None,
        "album": None,
        "year": None,
        "track_number": None,
        "disc_number": 1,
        "codec": p.suffix.lstrip(".").upper(),
        "file_path": str(p),
    }

def _candidate_tracks(db: Database, tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tracks in the manual-import picker shape, with artist/album names and whether a library file exists."""
    artist_names: dict[str, str] = {}
    album_titles: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for t in tracks:
        art_id = t.get("artist_id")
        if art_id and art_id not in artist_names:
            art = db.get_library_artist(art_id)
            artist_names[art_id] = art["name"] if art else "Unknown Artist"
        alb_id = t.get("album_id")
        if alb_id and alb_id not in album_titles:
            alb = db.get_library_album(alb_id)
            album_titles[alb_id] = alb["title"] if alb else "Unknown Album"
        out.append({
            "id": t["id"],
            "title": t.get("title"),
            "track_number": t.get("track_number"),
            "disc_number": t.get("disc_number"),
            "album_id": alb_id,
            "album_title": album_titles.get(alb_id) if alb_id else None,
            "artist_id": art_id,
            "artist_name": artist_names.get(art_id) if art_id else None,
            "has_file": db.get_library_file_for_track(t["id"]) is not None,
        })
    return out

def _tracks_without_file(db: Database, tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in tracks if db.get_library_file_for_track(t["id"]) is None]

def _scan_one_file(db: Database, p: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Inspects one file; returns (tags, base scan item with the unscoped tag-based library match)."""
    try:
        inspected = inspect_audio_file(p)
    except Exception as exc:
        logger.warning("Failed to inspect %s: %s", p, exc)
        inspected = _scan_fallback_tags(p)

    title = inspected.get("title")
    artist = resolve_album_artist(inspected, known_artist=lambda n: db.get_library_artist_by_name(n) is not None) or None
    album = inspected.get("album")
    trkn = inspected.get("track_number")

    # Fuzzy/clean search against existing library catalog
    matched_artist = db.get_library_artist_by_name(artist) if artist else None
    matched_album = None
    matched_track = None
    confidence = 0.0

    if matched_artist:
        confidence = 0.4
        if album:
            matched_album = db.get_library_album_by_title(matched_artist["id"], album)
            if matched_album:
                confidence = 0.7
                if title:
                    matched_track = db.get_library_track_by_title(matched_album["id"], title, track_number=trkn)
                    if matched_track:
                        confidence = 1.0

    try:
        size = p.stat().st_size
    except OSError:
        size = 0

    item = {
        "file_path": str(p),
        "filename": p.name,
        "size_bytes": size,
        "tags": inspected,
        "matched_artist_id": matched_artist["id"] if matched_artist else None,
        "matched_artist_name": matched_artist["name"] if matched_artist else (artist or None),
        "matched_album_id": matched_album["id"] if matched_album else None,
        "matched_album_title": matched_album["title"] if matched_album else (album or None),
        "matched_track_id": matched_track["id"] if matched_track else None,
        "matched_track_title": matched_track["title"] if matched_track else (title or None),
        "confidence": round(confidence, 2),
    }
    return inspected, item

def _unscoped_match_fields(db: Database, item: dict[str, Any]) -> dict[str, Any]:
    """match_strength / suggested_track_id / candidate_tracks for a folder-scan item (no candidate scope)."""
    if item["confidence"] == 1.0:
        strength = "strong"
    elif item["matched_album_id"]:
        strength = "weak"
    else:
        strength = "none"
    candidates: list[dict[str, Any]] = []
    if item["matched_album_id"]:
        candidates = _candidate_tracks(db, db.list_library_tracks(album_id=item["matched_album_id"], limit=1000))
    return {
        "match_strength": strength,
        "suggested_track_id": item["matched_track_id"],
        "candidate_tracks": candidates,
    }

def _scoped_match_fields(
    db: Database,
    item: dict[str, Any],
    inspected: dict[str, Any],
    remaining: list[dict[str, Any]],
    all_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Suggests a track from the scoped candidates (consuming it from ``remaining``) and rewrites matched_* to it."""
    meta = dict(inspected)
    meta.setdefault("file_path", item["file_path"])
    track, strength = reconcile_audio_file_to_track_scored(meta, remaining)
    if track is not None:
        remaining.remove(track)
        artist = db.get_library_artist(track["artist_id"]) if track.get("artist_id") else None
        album = db.get_library_album(track["album_id"]) if track.get("album_id") else None
        item.update({
            "matched_artist_id": artist["id"] if artist else None,
            "matched_artist_name": artist["name"] if artist else item["matched_artist_name"],
            "matched_album_id": album["id"] if album else None,
            "matched_album_title": album["title"] if album else item["matched_album_title"],
            "matched_track_id": track["id"],
            "matched_track_title": track["title"],
            "confidence": 1.0 if strength == MATCH_STRONG else 0.7,
        })
    else:
        item.update({"matched_album_id": None, "matched_track_id": None, "confidence": 0.0})
        strength = MATCH_NONE
    return {
        "match_strength": strength,
        "suggested_track_id": track["id"] if track is not None else None,
        "candidate_tracks": all_candidates,
    }

def _walk_audio_files(folder: Path) -> list[Path]:
    found: list[Path] = []
    for root, dirs, files in os.walk(str(folder)):
        dirs[:] = [d for d in dirs if not is_system_dirname(d)]
        for f in sorted(files):
            if is_system_filename(f):
                continue
            p = Path(root) / f
            if p.suffix.lower() in AUDIO_EXTENSIONS:
                found.append(p)
    return found

@router.post("/manual-import/scan", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=list[ManualImportScanItem], response_model_exclude_unset=True)
def manual_import_scan(
    body: Optional[ManualImportScanRequest] = None,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Scans files for manual import, inspects metadata and suggests library matches.

    Scope (first that applies): ``download_id`` (that native download's held files, matched against its tracks that
    have no file), ``file_paths`` (exactly those files), ``album_id`` (the folder scan, matched against that album's
    tracks that have no file), otherwise a plain folder scan with the tag-based match.
    """
    req = body or ManualImportScanRequest()
    files: list[Path] = []
    scoped_tracks: Optional[list[dict[str, Any]]] = None

    if req.download_id:
        download = db.get_active_download(req.download_id)
        if download is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Download '{req.download_id}' not found")
        for raw in download.get("unmatched_files") or []:
            try:
                held = validate_media_path(raw, db=db, purpose="import")
            except HTTPException as exc:
                logger.warning("Skipping held file %s for download %s: %s", raw, req.download_id, exc.detail)
                continue
            if held.is_file():
                files.append(held)
        req_row = db.get_request(download["request_id"]) if download.get("request_id") else None
        _, expected = resolve_download_expected_tracks(db, download, req_row)
        scoped_tracks = _tracks_without_file(db, expected)
        if req.album_id:
            scoped_tracks = _tracks_without_file(db, db.list_library_tracks(album_id=req.album_id, limit=1000))
    elif req.file_paths is not None:
        for raw in req.file_paths:
            p = validate_media_path(raw, db=db, purpose="import")
            if not p.is_file():
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"File does not exist: {raw}")
            files.append(p)
        if req.album_id:
            scoped_tracks = _tracks_without_file(db, db.list_library_tracks(album_id=req.album_id, limit=1000))
    else:
        folder_path = req.folder_path
        if not folder_path:
            mm = db.get_media_management_settings()
            roots = allowed_roots_for_all_clients(db, mm)
            folder_path = next((str(r) for r in roots.usable_roots()), None)
            if not folder_path:
                detail = "; ".join(roots.errors) or "No download folder is known"
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{detail}. Pass a folder_path, or check your download client connection.",
                )
        validated_dir = validate_media_path(folder_path, db=db, purpose="import")
        if not validated_dir.exists() or not validated_dir.is_dir():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Directory does not exist or is not a directory: {folder_path}",
            )
        files = _walk_audio_files(validated_dir)
        if req.album_id:
            scoped_tracks = _tracks_without_file(db, db.list_library_tracks(album_id=req.album_id, limit=1000))

    all_candidates = _candidate_tracks(db, scoped_tracks) if scoped_tracks is not None else []
    remaining = list(scoped_tracks) if scoped_tracks is not None else []

    results: list[dict[str, Any]] = []
    for p in files:
        inspected, item = _scan_one_file(db, p)
        if scoped_tracks is not None:
            item.update(_scoped_match_fields(db, item, inspected, remaining, all_candidates))
        else:
            item.update(_unscoped_match_fields(db, item))
        results.append(item)
    return results

@router.get("/manual-import/album-tracks", dependencies=[Depends(require_core_tier), Depends(native_only)], response_model=list[ManualImportCandidateTrack], response_model_exclude_unset=True)
def manual_import_album_tracks(
    album_id: str = Query(..., min_length=1),
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> list[dict[str, Any]]:
    """An album's tracks in the manual-import picker shape (``has_file`` marks tracks that already have a file)."""
    if db.get_library_album(album_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found")
    return _candidate_tracks(db, db.list_library_tracks(album_id=album_id, limit=1000))

@router.post("/manual-import/commit", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=ManualImportCommitResponse, response_model_exclude_unset=True)
def manual_import_commit(
    body: ManualImportCommitRequest,
    db: Database = Depends(get_db),
    plex_client: Optional[Any] = Depends(get_media_client),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Commits selected manual import items: resolves/creates catalog entities, moves/copies files to destination, tags them, and registers them in the library."""
    set_provenance(
        GrabTrigger(
            TRIGGER_MANUAL_IMPORT, label=_admin.get("username"),
            actor_user_id=str(_admin["id"]) if _admin.get("id") and _admin["id"] != "api_key_user" else None,
        )
    )
    imported_count = 0
    failed_count = 0
    results: list[dict[str, Any]] = []

    download_row: Optional[dict[str, Any]] = None
    if body.download_id:
        download_row = db.get_active_download(body.download_id)
        if download_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=f"Download '{body.download_id}' not found"
            )

    media_settings = db.get_media_management_settings()
    root_folder_str = media_settings.get("root_folder_path") or "/music"
    root_dir = Path(root_folder_str).resolve()

    # Import mode only applies to torrent downloads; a download-scoped commit follows its client's type.
    download_client_type: Optional[str] = None
    if download_row is not None and download_row.get("client_id"):
        client_cfg = db.get_download_client(download_row["client_id"])
        download_client_type = str(client_cfg.get("driver_type") or "") if client_cfg else None

    try:
        client_roots = list(allowed_roots_for_all_clients(db, media_settings).roots)
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("Could not read every download client's folders for the recycle check: %s", redact_text(str(exc)))
        client_roots = []
    batch_placed: set[str] = set()  # files placed by this commit: never recycled as another item's "old" file
    replaced_retired: list[str] = []
    replaced_kept: list[str] = []

    manual_import_tag_cache: dict[str, list[str]] = {}  # artist id -> tag labels, one lookup per artist
    for item in body.items:
        source_str = item.source_path or item.file_path
        if not source_str:
            failed_count += 1
            results.append({"status": "failed", "error": "No source path provided"})
            continue

        try:
            source_path = validate_media_path(source_str, db=db, purpose="import")
            if not source_path.exists() or not source_path.is_file():
                failed_count += 1
                results.append({
                    "source_path": source_str,
                    "status": "failed",
                    "error": "Source file not found on disk",
                })
                continue

            try:
                inspected = inspect_audio_file(source_path)
            except Exception as exc:
                logger.warning("inspect_audio_file failed for %s, using fallback tags: %s", source_path, exc)
                inspected = {
                    "title": source_path.stem,
                    "artist": None,
                    "album": None,
                    "year": None,
                    "track_number": None,
                    "disc_number": 1,
                    "codec": source_path.suffix.lstrip(".").upper(),
                    "file_path": str(source_path),
                }

            # 1. Resolve or Create Artist
            artist_id = item.artist_id
            artist = db.get_library_artist(artist_id) if artist_id else None
            if not artist:
                art_name = (
                    item.artist_name
                    or resolve_album_artist(inspected, known_artist=lambda n: db.get_library_artist_by_name(n) is not None)
                    or "Unknown Artist"
                ).strip()
                artist = db.get_library_artist_by_name(art_name)
                if not artist:
                    artist = db.upsert_library_artist({
                        "id": str(uuid.uuid4()),
                        "name": art_name,
                        "clean_name": clean_library_name(art_name),
                        "monitored": True,
                        "monitor_option": str(media_settings.get("add_monitor_option") or DEFAULT_MONITOR_OPTION),
                    })
            artist_id = artist["id"]

            # 2. Resolve or Create Album
            album_id = item.album_id
            album = db.get_library_album(album_id) if album_id else None
            if not album:
                alb_title = (item.album_title or inspected.get("album") or "Unknown Album").strip()
                album = db.get_library_album_by_title(artist_id, alb_title)
                if not album:
                    alb_year = item.year or inspected.get("year")
                    album = db.upsert_library_album({
                        "id": str(uuid.uuid4()),
                        "artist_id": artist_id,
                        "title": alb_title,
                        "clean_title": clean_library_name(alb_title),
                        "year": alb_year,
                        "monitored": True,
                    })
            album_id = album["id"]

            # 3. Resolve or Create Track
            track_id = item.track_id
            track = db.get_library_track(track_id) if track_id else None
            file_title, file_trkn = parse_filename_track(source_path.stem)
            trkn = item.track_number or inspected.get("track_number") or file_trkn
            disc = item.disc_number or inspected.get("disc_number") or 1
            if not track:
                trk_title = (item.track_title or inspected.get("title") or file_title).strip()
                track = db.get_library_track_by_title(album_id, trk_title, track_number=trkn)
                if not track:
                    track = db.upsert_library_track({
                        "id": str(uuid.uuid4()),
                        "album_id": album_id,
                        "artist_id": artist_id,
                        "title": trk_title,
                        "clean_title": clean_library_name(trk_title),
                        "track_number": trkn or 1,
                        "disc_number": disc,
                        "duration_seconds": inspected.get("duration"),
                        "monitored": True,
                    })
            track_id = track["id"]

            # 3b. Quality profile & Cutoff evaluation
            cutoff_met = True
            quality_name = str(inspected.get("quality_full") or inspected.get("codec") or "Unknown")
            try:
                qp_id = artist.get("quality_profile_id")
                profile_dict = db.get_quality_profile(qp_id) if qp_id else None
                if not profile_dict:
                    profile_dict = db.get_default_quality_profile()
                if profile_dict:
                    qp = _to_quality_profile(profile_dict)
                    quality_input = (
                        inspected.get("quality_full")
                        or inspected.get("codec")
                        or source_path.suffix.lstrip(".").upper()
                    )
                    parsed = parse_release_title(str(quality_input))
                    if parsed.quality == "Unknown" and quality_input:
                        parsed.quality = str(quality_input)
                    fsize = source_path.stat().st_size
                    if artist_id not in manual_import_tag_cache:
                        manual_import_tag_cache[artist_id] = delay_gate.artist_tags(db, artist.get("name"), artist_id)
                    import_artist_tags = manual_import_tag_cache[artist_id]
                    eval_result = evaluate_release(parsed, qp, size_bytes=fsize, artist_tags=import_artist_tags)
                    # A bare quality string scores 0 format points and could never reach ``cutoff_format_score``, so
                    # judge the quality tier only; when the imported release title is known (and its quality matches)
                    # score from it instead, exactly as ``backlog_worker._current_floor`` does.
                    bd = eval_result.breakdown
                    cutoff_met = bool(bd.quality_cutoff_met if bd is not None else eval_result.meets_cutoff)
                    title = db.get_imported_release_title(track_id=track_id, album_id=album_id)
                    if title:
                        titled = parse_release_title(title)
                        if titled.quality in {parsed.quality, str(quality_input)}:
                            cutoff_met = bool(
                                evaluate_release(
                                    titled, qp, size_bytes=fsize, artist_tags=import_artist_tags
                                ).meets_cutoff
                            )
                    quality_name = eval_result.parsed_quality or str(quality_input)
            except Exception as exc:
                logger.warning("Cutoff evaluation error during manual import for %s: %s", source_path, exc)
                cutoff_met = True

            # 4. Resolve destination path
            meta = dict(inspected)
            meta["quality_full"] = quality_name  # same catalog source as rename preview/apply
            meta["artist"] = artist["name"]
            meta["album_artist"] = artist["name"]
            meta["album"] = album["title"]
            meta["title"] = track["title"]
            meta["track_number"] = track["track_number"]
            meta["disc_number"] = track["disc_number"]
            meta["total_discs"] = _album_total_discs(db, album_id, disc, inspected.get("total_discs"))
            if album.get("year"):
                meta["year"] = album["year"]
                meta["release_year"] = album["year"]
            meta["extension"] = source_path.suffix

            target_proposed = build_track_path(meta, media_settings)
            validate_media_path(target_proposed, db=db)

            # A file already registered inside the library is a re-assign, not a new import: it is always moved
            # within the library (never copied or linked from itself), and the old track loses its file record.
            # A hardlinked library file keeps the torrent's inode: renaming a link never touches the other name.
            source_row = db.get_library_file_by_path(str(source_path))
            is_rematch = source_row is not None and source_path.is_relative_to(root_dir)
            pre_recycled: Optional[tuple[Any, dict[str, Any]]] = None
            desired_path = Path(target_proposed).resolve()
            if is_rematch and desired_path == source_path:
                target_dest = source_path  # already at its naming path for the new track
            else:
                if desired_path != source_path and str(desired_path) not in batch_placed:
                    # The track's current file sits on the clean target name: rename it into the bin first so the
                    # new file takes that name instead of ``Name (1).ext`` (restored below if placement fails).
                    pre_recycled = recycle_in_place_target(
                        db, media_settings, root_dir, desired_path, track_id, client_roots
                    )
                target_dest = desired_path if pre_recycled is not None else resolve_collision(target_proposed)

            # 5. Place file
            if is_rematch:
                effective_mode = "move"
            elif download_row is not None:
                # Non-torrent downloads never seed, so they are always moved whatever the item asks for.
                effective_mode = (
                    item.mode or effective_import_mode(download_client_type, media_settings)
                    if is_torrent_driver_type(download_client_type)
                    else "move"
                )
            else:
                effective_mode = item.mode or str(media_settings.get("import_mode") or "move")
            if is_rematch and target_dest == source_path:
                placed_file = source_path
            else:
                try:
                    placed_file = place_audio_file(source_path, target_dest, mode=effective_mode)
                except Exception:
                    if pre_recycled is not None:
                        restore_recycled(pre_recycled[0])  # the replacement never landed: put the old bytes back
                    raise
            newly_placed = placed_file != source_path or not is_rematch
            if pre_recycled is not None:
                # The old file's bytes now live in the bin; its row (same path) must go before the new row is keyed.
                log_recycled(
                    db, pre_recycled[0], placed_file, pre_recycled[1], quality_name,
                    title=str(track.get("title") or ""), download_id=body.download_id,
                    issue_id=body.issue_id, retired=replaced_retired,
                    source="ManualImport", log_prefix="Manual import",
                )
                try:
                    db.delete_library_file(str(pre_recycled[1]["id"]))
                except sqlite3.Error as del_err:
                    logger.warning("Could not remove stale library file row %s: %s", pre_recycled[1].get("id"), redact_text(str(del_err)))
            if is_rematch and source_row is not None and str(placed_file) != str(source_row["file_path"]):
                db.delete_library_file(str(source_row["id"]))

            # 6. Write audio tags if requested
            write_tags = item.write_tags
            if write_tags is None:
                write_tags = bool(media_settings.get("write_audio_tags", True))
            if write_tags and not prepare_file_for_tagging(placed_file, media_settings):
                # A shared inode (torrent seeding link) must never be rewritten; the copy failed, so skip tags.
                write_tags = False
            if write_tags:
                try:
                    write_audio_tags(placed_file, meta)
                except Exception as exc:
                    logger.warning("Error writing tags to %s: %s", placed_file, exc)

            try:
                rel_path = str(placed_file.relative_to(root_dir))
            except ValueError:
                rel_path = placed_file.name

            existing_f = db.get_library_file_by_path(str(placed_file))
            file_id = str(existing_f["id"]) if existing_f else str(uuid.uuid4())
            db.upsert_library_file({
                "id": file_id,
                "track_id": track_id,
                "file_path": str(placed_file),
                "relative_path": rel_path,
                "codec": meta.get("codec") or placed_file.suffix.lstrip(".").upper(),
                "bitrate": meta.get("bitrate"),
                "sample_rate": meta.get("sample_rate"),
                "bits_per_sample": meta.get("bits_per_sample"),
                "quality_name": quality_name,
                "size_bytes": placed_file.stat().st_size if placed_file.exists() else 0,
                "cutoff_met": cutoff_met,
            })

            superseded: list[dict[str, Any]] = []
            if newly_placed:
                # A file really landed for this track: every other file it had is superseded (rename to the bin,
                # same rules and ``file_recycled`` event as a worker import). Files from this batch, and the
                # rematched library file itself (moved, not recycled), are never candidates.
                batch_placed.add(str(placed_file))
                skip_ids = {file_id} | ({str(source_row["id"])} if source_row is not None else set())
                superseded = [
                    r for r in db.list_library_files_for_track(track_id)
                    if str(r["id"]) not in skip_ids
                    and str(r.get("file_path") or "") not in batch_placed
                    and str(r.get("file_path") or "") != str(source_path)
                ]
                if superseded:
                    recycle_replaced_files(
                        db, superseded, placed_file, root_dir, media_settings, client_roots, quality_name,
                        replaced_retired, replaced_kept,
                        title=str(track.get("title") or ""), download_id=body.download_id,
                        issue_id=body.issue_id, source="ManualImport", log_prefix="Manual import",
                    )

            if is_rematch:
                if str(placed_file) != str(source_path):
                    emit(
                        db, "moved", track_id=str(track_id), message=f"Moved to {placed_file.name}",
                        details={"from": str(source_path), "to": str(placed_file), "reason": "manual rematch"},
                    )
            elif newly_placed:
                record_import_events(
                    db,
                    {"id": body.download_id, "title": str(track.get("title") or ""),
                     "request_id": download_row.get("request_id") if download_row else None},
                    str(track_id), placed_file, quality_name, meta,
                    superseded + ([pre_recycled[1]] if pre_recycled is not None else []),
                )

            if is_rematch:
                for finding_path in {str(source_path), str(placed_file)}:
                    db.delete_library_health_finding_by_path(finding_path, kind="weak_match")

            # Update album folder path if missing
            if not album.get("path"):
                db.upsert_library_album({
                    "id": album_id,
                    "artist_id": artist_id,
                    "title": album["title"],
                    "path": str(placed_file.parent),
                })

            imported_count += 1
            results.append({
                "source_path": source_str,
                "destination_path": str(placed_file),
                "artist_id": artist_id,
                "album_id": album_id,
                "track_id": track_id,
                "file_id": file_id,
                "status": "imported",
                "mode": effective_mode,
                "rematch": is_rematch,
            })

        except Exception as exc:
            logger.exception("Failed to import %s: %s", source_str, redact_text(str(exc)))
            failed_count += 1
            results.append({
                "source_path": source_str,
                "status": "failed",
                "error": redact_text(str(exc)),
            })

    if body.issue_id and (replaced_retired or replaced_kept):
        try:
            if db.get_issue(body.issue_id):
                comment = "Replacement imported"
                for line in replaced_retired:
                    comment += f"\nRetired old file: {line}"
                for line in replaced_kept:
                    comment += f"\nOld file kept at {line}"
                db.add_issue_comment(body.issue_id, None, comment, is_admin=True, is_system=True, staff=True)
        except sqlite3.Error as issue_err:
            logger.warning("Could not comment on issue %s after manual import: %s", body.issue_id, redact_text(str(issue_err)))

    download_cleared = False
    if download_row is not None and str(download_row.get("status")) == DownloadStatus.WARNING.value:
        download_cleared = _settle_download_after_manual_import(db, download_row, results, media_settings)

    if plex_client:
        try:
            as_media_server(plex_client).refresh_library()
        except Exception as exc:
            logger.warning("Error refreshing media-server library: %s", exc)

    return {
        "imported_count": imported_count,
        "failed_count": failed_count,
        "results": results,
        "download_cleared": download_cleared,
    }

def _settle_download_after_manual_import(
    db: Database,
    download: dict[str, Any],
    results: list[dict[str, Any]],
    media_settings: dict[str, Any],
) -> bool:
    """Drops imported files from a download's held list; settles the transfer once nothing is left to import."""
    done = {str(Path(r["source_path"]).resolve()) for r in results if r.get("status") == "imported" and r.get("source_path")}
    placed_modes = {str(r.get("mode") or "move") for r in results if r.get("status") == "imported" and r.get("destination_path")}
    placed_paths = [str(r["destination_path"]) for r in results if r.get("status") == "imported" and r.get("destination_path")]
    if placed_paths:
        # Record what this commit placed so the seed-cleanup safety gate can verify the library copies later.
        record_mode = "move" if "move" in placed_modes else ("hardlink" if "hardlink" in placed_modes else "copy")
        db.add_download_placed_files(download["id"], placed_paths, record_mode)
        download = db.get_active_download(download["id"]) or download
    held = [p for p in download.get("unmatched_files") or []]
    still_held = [p for p in held if str(Path(p).resolve()) not in done and os.path.isfile(p)]
    db.set_download_unmatched_files(download["id"], still_held)
    if still_held:
        db.update_download_status(
            download["id"],
            status=DownloadStatus.WARNING.value,
            error_message=f"{len(still_held)} file(s) couldn't be matched — manual import required",
        )
        return False
    modes = [str(r.get("mode") or "move") for r in results if r.get("status") == "imported"]
    # Any move-mode file has left the torrent's folder, so seeding retention no longer applies.
    effective_mode = "move" if not modes or not all(preserves_source(m) for m in modes) else modes[0]
    download = db.get_active_download(download["id"]) or download  # fresh placed/unmatched records for the safety gate
    new_status = _govern_download_at_client(db, download, media_settings, effective_mode)
    fields: dict[str, Any] = {"status": new_status, "error_message": ""}
    if new_status == DownloadStatus.COMPLETED.value:
        # target_path marks it already imported, so the worker's governance branch removes it once limits are met.
        fields["target_path"] = ", ".join(sorted({str(Path(r["destination_path"]).parent) for r in results if r.get("destination_path")}))
    db.update_download_status(download["id"], **fields)
    return True

def _govern_download_at_client(
    db: Database, download: dict[str, Any], media_settings: dict[str, Any], import_mode: str
) -> str:
    """Runs the worker's shared post-import governance for a manual import; returns the status to record."""
    if seed_action(media_settings) == "keep":
        return DownloadStatus.IMPORTED.value
    client_id = download.get("client_id")
    if not client_id:
        return DownloadStatus.IMPORTED.value
    target_lookup = download.get("download_hash") or download["id"]
    try:
        client_cfg = db.get_download_client(client_id)
        if not client_cfg:
            logger.warning("Cannot clean up %s: download client %s no longer exists", download["id"], client_id)
            return DownloadStatus.IMPORTED.value
        driver = get_acquisition_driver(client_cfg)
        try:
            status_dict: Optional[dict[str, Any]] = driver.get_status(target_lookup)
        except Exception as exc:  # driver errors span HTTP, auth and parsing
            logger.warning(
                "Keeping transfer %s: could not fetch seeding status (%s)", target_lookup, redact_text(str(exc))
            )
            return DownloadStatus.COMPLETED.value
        if not status_dict:
            logger.warning("Keeping transfer %s: driver returned no status", target_lookup)
            return DownloadStatus.COMPLETED.value
        return settle_transfer_after_import(driver, target_lookup, media_settings, import_mode, status_dict, download, db)
    except Exception as exc:  # the commit has already succeeded; never fail it over client governance
        logger.warning("Error settling transfer %s: %s", target_lookup, redact_text(str(exc)))
        return DownloadStatus.COMPLETED.value

@router.post("/manual-import/fingerprint", dependencies=[Depends(require_core_tier), Depends(native_only), Depends(track_admin_actor)], response_model=FingerprintResponse, response_model_exclude_unset=True)
def fingerprint_file(
    body: FingerprintRequest,
    db: Database = Depends(get_db),
    _admin: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    """Fingerprints an audio file on-demand via AcoustID without routine scanner overhead."""
    validated_file = validate_media_path(body.file_path, db=db, purpose="import")
    if not validated_file.exists() or not validated_file.is_file():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File does not exist: {body.file_path}",
        )
    settings = db.get_media_management_settings()
    fp = fingerprint_audio_file(
        str(validated_file),
        api_key=settings.get("acoustid_api_key"),
    )
    if fp:
        library_track: Optional[dict[str, Any]] = None
        if fp.get("recording_id"):
            row = db.get_library_track_by_mb_recording_id(fp["recording_id"])
            if row:
                artist_row = db.get_library_artist(row["artist_id"]) if row.get("artist_id") else None
                library_track = {
                    "id": row["id"],
                    "title": row.get("title"),
                    "album_id": row.get("album_id"),
                    "artist": artist_row["name"] if artist_row else None,
                }
        return {"success": True, "fingerprint": fp, "library_track": library_track}
    return {
        "success": False,
        "message": "Fingerprinting unavailable or no match found",
    }


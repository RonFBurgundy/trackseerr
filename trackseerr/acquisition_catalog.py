"""Import catalog mixin for AcquisitionWorker."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any


from trackseerr import delay_gate
from trackseerr.acquisition_coordinator import _to_quality_profile
from trackseerr.library import (
    inspect_audio_file,
)
from trackseerr.models import (
    DownloadStatus,
    LibraryAlbum,
    LibraryArtist,
    LibraryFile,
    LibraryTrack,
)
from trackseerr.redaction import safe_exc
from trackseerr.quality import evaluate_release, parse_release_title
from trackseerr.storage import Database


logger = logging.getLogger(__name__)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trackseerr.acquisition_import import _ImportJob, _PollContext

class ImportCatalogMixin:
    """Import catalog mixin for AcquisitionWorker."""
    def _record_imported_files(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
    ) -> None:
        """Update download status and look up replacement issue."""
        job.target_summary = job.imported_paths[0] if job.imported_paths else None
        db.update_download_status(
            job.download_id,
            status=DownloadStatus.IMPORTING.value,
            target_path=job.target_summary,
        )

        # An issue-driven replacement retires the track's previous file(s) once the new row is in.
        job.replacement_issue_id = None
        job.replaced_retired = []
        job.replaced_kept = []
        try:
            job.replacement_issue_id = db.get_download_replacement_issue(str(job.download_id))
        except sqlite3.Error as ri_err:
            logger.warning("Could not look up replacement issue for %s: %s", job.download_id, safe_exc(ri_err))

    def _resolve_catalog_rows(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        placed_p: Path,
        f_meta: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Resolve or upsert artist, album, and track records for an unmatched placed file."""
        assert job.root_path is not None
        artist_name = (
            f_meta.get("artist")
            or job.item.get("artist")
            or (job.req.get("artist") if job.req else None)
            or "Unknown Artist"
        ).strip()

        # 1. Resolve / upsert LibraryArtist
        artist_row = None
        existing_track = None
        if job.item.get("track_id"):
            existing_track = db.get_library_track(job.item["track_id"])
            if existing_track:
                artist_row = db.get_library_artist(existing_track["artist_id"])
        if not artist_row:
            artist_row = db.get_library_artist_by_name(artist_name)
        if not artist_row:
            artist_id = str(uuid.uuid4())
            artist_folder = (
                str(placed_p.parent.parent)
                if placed_p.parent != job.root_path
                else str(placed_p.parent)
            )
            artist_row = db.upsert_library_artist(
                LibraryArtist(
                    id=artist_id,
                    name=artist_name,
                    path=artist_folder,
                    monitor_option=_scan_monitor_option(ctx.media_settings),
                ),
                preserve_monitoring=True,
            )
        artist_id = artist_row["id"]

        # 2. Resolve / upsert LibraryAlbum
        album_title = (
            f_meta.get("album")
            or (job.item.get("title") if job.item.get("item_type") == "album" else None)
            or (job.req.get("album") or job.req.get("title") if job.req else None)
            or "Unknown Album"
        ).strip()
        year_val = f_meta.get("year")
        if year_val is None and job.req and job.req.get("release_date"):
            rdate = str(job.req["release_date"]).strip()
            if len(rdate) >= 4 and rdate[:4].isdigit():
                year_val = int(rdate[:4])

        album_row = None
        if job.item.get("album_id"):
            album_row = db.get_library_album(job.item["album_id"])
        elif job.item.get("track_id") and existing_track:
            album_row = db.get_library_album(existing_track["album_id"])
        if not album_row:
            album_row = db.get_library_album_by_title(artist_id, album_title)
        if not album_row:
            album_id = str(uuid.uuid4())
            album_row = db.upsert_library_album(
                LibraryAlbum(
                    id=album_id,
                    artist_id=artist_id,
                    title=album_title,
                    year=year_val,
                    path=str(placed_p.parent),
                )
            )
        album_id = album_row["id"]

        # 3. Resolve / upsert LibraryTrack
        track_row = None
        if job.item.get("track_id"):
            track_row = existing_track or db.get_library_track(job.item["track_id"])
        if not track_row:
            track_title = (
                f_meta.get("title")
                or job.item.get("title")
                or (job.req.get("title") if job.req else None)
                or placed_p.stem
            ).strip()
            track_num = int(f_meta.get("track_number") or 1)
            track_row = db.get_library_track_by_title(
                album_id, track_title, track_number=track_num
            )
            if not track_row:
                track_id = str(uuid.uuid4())
                track_row = db.upsert_library_track(
                    LibraryTrack(
                        id=track_id,
                        album_id=album_id,
                        artist_id=artist_id,
                        title=track_title,
                        track_number=track_num,
                        disc_number=int(f_meta.get("disc_number") or 1),
                        duration_seconds=(
                            float(f_meta["duration"])
                            if f_meta.get("duration") is not None
                            else None
                        ),
                    )
                )
        return artist_row, album_row, track_row

    def _upsert_catalog_for_file(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        placed_str: str,
        import_tag_cache: dict[str, list[str]],
    ) -> None:
        """Upsert library artist, album, track, and file records for a placed audio file."""
        assert job.root_path is not None
        try:
            placed_p = Path(placed_str).resolve()
            try:
                f_meta = inspect_audio_file(placed_p)
            except Exception as insp_err:
                logger.warning(
                    "Could not inspect placed audio file %s: %s; using fallback metadata",
                    placed_p,
                    insp_err,
                )
                f_meta = {}

            matched_track = job.placed_to_track.get(placed_str)
            if matched_track and job.target_album:
                artist_id = job.target_album["artist_id"]
                artist_row = db.get_library_artist(artist_id) or {
                    "id": artist_id,
                    "name": job.item.get("artist") or "Unknown Artist",
                    "quality_profile_id": None,
                }
                album_id = job.target_album["id"]
                album_row = job.target_album
                track_id = matched_track["id"]
                track_row = matched_track
                db.set_track_monitored(track_id, True)
            else:
                artist_row, album_row, track_row = self._resolve_catalog_rows(db, ctx, job, placed_p, f_meta)
                artist_id = artist_row["id"]
                album_id = album_row["id"]
                track_id = track_row["id"]

            # 4. Evaluate cutoff against artist's quality profile (or default)
            qp_id = artist_row.get("quality_profile_id") or (
                job.req.get("quality_profile_id") if job.req else None
            )
            prof_dict = db.get_quality_profile(qp_id) if qp_id else None
            if not prof_dict:
                prof_dict = db.get_default_quality_profile()

            parsed = parse_release_title(job.item.get("title") or placed_p.name)
            if parsed.quality == "Unknown":
                parsed.quality = _quality_from_codec(f_meta) or parsed.quality

            file_size = (
                placed_p.stat().st_size
                if placed_p.exists()
                else int(job.item.get("size_bytes") or 0)
            )
            cutoff_met = True
            quality_str = parsed.quality
            if prof_dict:
                profile_obj = _to_quality_profile(prof_dict)
                if artist_id not in import_tag_cache:
                    import_tag_cache[artist_id] = delay_gate.artist_tags(
                        db, artist_row.get("name"), artist_id
                    )
                eval_res = evaluate_release(
                    release=parsed,
                    profile=profile_obj,
                    size_bytes=file_size,
                    artist_tags=import_tag_cache[artist_id],
                )
                quality_str = eval_res.parsed_quality
                cutoff_met = eval_res.meets_cutoff

            # 5. Upsert LibraryFile
            rel_path = (
                str(placed_p.relative_to(job.root_path))
                if placed_p.is_relative_to(job.root_path)
                else str(placed_p)
            )
            file_id = f"fil-{uuid.uuid4().hex[:12]}"
            # Every other file the track has is superseded by this import (upgrade or issue
            # replacement), except files this same download placed (a multi-file release).
            sibling_paths = {str(Path(p).resolve()) for p in job.imported_paths} | {str(placed_p)}
            old_file_rows = [
                r for r in db.list_library_files_for_track(track_id)
                if str(r.get("file_path") or "") not in sibling_paths
            ]
            in_place = job.recycled_in_place.pop(str(placed_p), None)
            if in_place is not None:
                old_file_rows = [r for r in old_file_rows if str(r["id"]) != str(in_place[1]["id"])]
                self._log_recycled(db, job.item, in_place[0], placed_p, in_place[1], quality_str, ctx.media_settings,
                                   job.replacement_issue_id, job.replaced_retired)
                try:
                    db.delete_library_file(str(in_place[1]["id"]))
                except sqlite3.Error as del_err:
                    logger.warning("Could not remove stale library file row %s: %s", in_place[1].get("id"), safe_exc(del_err))
            db.upsert_library_file(
                LibraryFile(
                    id=file_id,
                    track_id=track_id,
                    file_path=str(placed_p),
                    relative_path=rel_path,
                    codec=f_meta.get("codec") or placed_p.suffix.lstrip(".").upper(),
                    bitrate=int(f_meta["bitrate"]) if f_meta.get("bitrate") is not None else None,
                    sample_rate=int(f_meta["sample_rate"]) if f_meta.get("sample_rate") is not None else None,
                    bits_per_sample=int(f_meta["bits_per_sample"]) if f_meta.get("bits_per_sample") is not None else None,
                    quality_name=quality_str,
                    size_bytes=file_size,
                    cutoff_met=cutoff_met,
                )
            )
            logger.info(
                "Native catalog upserted file %s for track %s (cutoff_met=%s)",
                file_id,
                track_id,
                cutoff_met,
            )
            record_import_events(
                db, job.item, str(track_id), placed_p, quality_str, f_meta,
                ([in_place[1]] if in_place is not None else []) + old_file_rows,
            )
            if old_file_rows:
                self._recycle_replaced_files(
                    db, job.item, job.replacement_issue_id, old_file_rows, placed_p, job.root_path,
                    ctx.media_settings, quality_str, job.replaced_retired, job.replaced_kept,
                )
        except Exception as upsert_err:
            logger.exception(
                "Error upserting native library records for %s: %s",
                placed_str,
                upsert_err,
            )

    def _upsert_native_catalog(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
    ) -> None:
        """Upsert library catalog records for all placed files if library mode is not lidarr."""
        # Native catalog upsert (when library_mode != "lidarr")
        if ctx.media_settings.get("library_mode") != "lidarr":
            # Artist tag labels by artist id, looked up once per artist across the placed files.
            import_tag_cache: dict[str, list[str]] = {}
            for placed_str in job.imported_paths:
                self._upsert_catalog_for_file(db, ctx, job, placed_str, import_tag_cache)



from trackseerr.acquisition_import import (
    _quality_from_codec,
    _scan_monitor_option,
    record_import_events,
)
